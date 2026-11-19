"""Free volume space by archiving old nightly and forensic files to the export bucket.

``space_cleanup`` 0 (the default) returns before touching the disk or the
network. With 1, a daemon thread runs once after startup (outside market hours)
and once per weekday evening. It only walks ``DATA_DIR/nightly/<day>/`` and
``DATA_DIR/forensics/<day>-*`` for days before the two most recent weekdays
prior to today. Each file is uploaded to the same S3-compatible bucket and
credentials the day export uses, under ``<prefix>volume-archive/<relative path>``.
A local file is deleted only after a HEAD of the uploaded object confirms its
size and, when the ETag is a plain MD5, its MD5. The PUT also carries the
file's SHA-256 as ``x-amz-content-sha256`` so the bucket rejects corrupt bodies.
It never touches the database, Schwab tokens, or today's files, and never runs
SQL of any kind. The same thread can run the live-database prune afterwards
(``db_prune``); that is a separate step and stays off unless its own flag is set.
"""
import logging
import os
import re
import shutil
import threading
import time
from datetime import date, datetime, timedelta, timezone
from hashlib import md5, sha256
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .day_export import _encode_key, _object_uri, _s3_config, _s3_open, sigv4_authorization, _EMPTY_SHA256
from .models import ET

LOG = logging.getLogger(__name__)

ARCHIVE_DIR = "volume-archive/"
KEEP_TRADING_DAYS = 2
_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})")
# Never archived or deleted, wherever they appear.
_PROTECTED_NAMES = {"live.sqlite3", "schwab_tokens.json"}
_PROTECTED_SUFFIXES = (".sqlite3", ".sqlite3-journal", ".sqlite3-wal", ".sqlite3-shm", ".lock", ".tmp")
_LOCK = threading.Lock()
_STATE = {"thread": None, "startup_done": False, "nightly_day": None, "retry_at": 0.0}


def clear_space_state():
    with _LOCK:
        _STATE.update(thread=None, startup_done=False, nightly_day=None, retry_at=0.0)


def protected_days(today: date, keep: int = KEEP_TRADING_DAYS) -> set:
    """Today plus the ``keep`` most recent weekdays before it (holidays only widen what is kept)."""
    days = {today.isoformat()}
    cursor = today
    found = 0
    while found < keep:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            days.add(cursor.isoformat())
            found += 1
    return days


def _is_protected(path: Path) -> bool:
    name = path.name
    return name in _PROTECTED_NAMES or name.startswith("schwab") or name.endswith(_PROTECTED_SUFFIXES)


def candidate_files(data_dir, today: date, keep: int = KEEP_TRADING_DAYS) -> list:
    """Regular files eligible for archiving, as (path, relative key) pairs, oldest day first."""
    root = Path(data_dir)
    keep_days = protected_days(today, keep)
    cutoff = min(keep_days)
    found = []

    def eligible(day):
        return day is not None and day < cutoff and day not in keep_days

    nightly = root / "nightly"
    if nightly.is_dir() and not nightly.is_symlink():
        for day_dir in sorted(nightly.iterdir()):
            match = _DAY.match(day_dir.name)
            if (not day_dir.is_dir() or day_dir.is_symlink() or not match
                    or match.group(1) != day_dir.name or not eligible(day_dir.name)):
                continue
            for path in sorted(day_dir.rglob("*")):
                if path.is_file() and not path.is_symlink() and not _is_protected(path):
                    found.append((path, path.relative_to(root).as_posix()))
    forensics = root / "forensics"
    if forensics.is_dir() and not forensics.is_symlink():
        for path in sorted(forensics.iterdir()):
            match = _DAY.match(path.name)
            if (path.is_file() and not path.is_symlink() and match and eligible(match.group(1))
                    and not _is_protected(path)):
                found.append((path, path.relative_to(root).as_posix()))
    return found


def _digests(path: Path):
    sha, mdf = sha256(), md5()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            sha.update(chunk)
            mdf.update(chunk)
    return sha.hexdigest(), mdf.hexdigest()


def _head(cfg, key, opener):
    """(size, etag) of an object, or None when it does not exist."""
    uri = _object_uri(cfg, key)
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    signed = {"host": cfg.host, "x-amz-content-sha256": _EMPTY_SHA256, "x-amz-date": amz_date}
    auth = sigv4_authorization("HEAD", uri, "", signed, _EMPTY_SHA256, access_key=cfg.access_key,
                               secret_key=cfg.secret_key, region=cfg.region)
    request = Request(f"https://{cfg.host}{uri}", method="HEAD", headers={
        "Host": cfg.host, "x-amz-content-sha256": _EMPTY_SHA256, "x-amz-date": amz_date,
        "Authorization": auth})
    try:
        with opener(request, timeout=60) as response:
            status = getattr(response, "status", None)
            headers = response.headers
            response.read()
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError("space cleanup verify was refused") from None
    if status is not None and int(status) == 404:
        return None
    if status is not None and int(status) >= 400:
        raise RuntimeError("space cleanup verify was refused")
    length = headers.get("Content-Length")
    etag = (headers.get("ETag") or "").strip().strip('"').lower()
    return (int(length) if length is not None and str(length).isdigit() else None), etag


def _verified(remote, size, md5_hex) -> bool:
    if remote is None:
        return False
    remote_size, etag = remote
    if remote_size != size:
        return False
    # A single-part PUT's ETag is the body MD5. Other ETag shapes are not comparable,
    # and then size plus the signed SHA-256 payload on PUT is the integrity check.
    if re.fullmatch(r"[0-9a-f]{32}", etag or "") and etag != md5_hex:
        return False
    return True


def _prune_empty(path: Path, stop: Path):
    parent = path.parent
    while parent != stop and stop in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def run_space_cleanup(data_dir, today: date, *, opener=None, keep: int = KEEP_TRADING_DAYS) -> dict:
    """Archive then delete eligible files. Returns counts; one failing file never stops the rest."""
    opener = opener or urlopen
    root = Path(data_dir)
    before = shutil.disk_usage(root).free
    files = candidate_files(root, today, keep)
    result = {"candidates": len(files), "archived": 0, "already_archived": 0, "deleted": 0,
              "failed": 0, "bytes_freed": 0, "free_before": before}
    if files:
        cfg = _s3_config()
        for path, rel in files:
            try:
                size = path.stat().st_size
                sha_hex, md5_hex = _digests(path)
                key = cfg.prefix + ARCHIVE_DIR + rel
                remote = _head(cfg, key, opener)
                if _verified(remote, size, md5_hex):
                    result["already_archived"] += 1
                else:
                    _s3_open(cfg, "PUT", canonical_uri=_object_uri(cfg, key),
                             content_type="application/octet-stream", body_path=path, payload_hash=sha_hex,
                             opener=opener, timeout=600, error="space cleanup upload was refused")
                    if not _verified(_head(cfg, key, opener), size, md5_hex):
                        raise RuntimeError("space cleanup verify mismatch")
                    result["archived"] += 1
                # The file must be unchanged since it was hashed and uploaded.
                if path.stat().st_size != size or _digests(path)[0] != sha_hex:
                    raise RuntimeError("space cleanup source changed")
                path.unlink()
                result["deleted"] += 1
                result["bytes_freed"] += size
                _prune_empty(path, root)
            except Exception as exc:
                result["failed"] += 1
                LOG.warning("Space cleanup skipped %s (%s)", rel, type(exc).__name__)
    result["free_after"] = shutil.disk_usage(root).free
    LOG.info("Space cleanup: %d candidates, %d uploaded, %d already archived, %d deleted, %d failed; "
             "bytes freed %d (%.1f MB); volume free %.1f MB -> %.1f MB",
             result["candidates"], result["archived"], result["already_archived"], result["deleted"],
             result["failed"], result["bytes_freed"], result["bytes_freed"] / 1e6,
             before / 1e6, result["free_after"] / 1e6)
    return result


def _market_hours(local: datetime) -> bool:
    return local.weekday() < 5 and (9, 30) <= (local.hour, local.minute) < (16, 0)


def _prune_keep(settings) -> int:
    value = getattr(settings, "db_prune_keep_days", 0)
    return value if type(value) is int and value > 0 else 0


def maybe_space_cleanup(settings, data_dir, now, *, runner=run_space_cleanup, retry_seconds=1800,
                        nightly_hour=16, nightly_minute=40):
    """Start file cleanup, then db prune, at startup and once per evening.

    Either flag at its default returns before that step. With both off, this
    returns before a thread, the disk, or the database. Market hours never start it.
    """
    try:
        cleanup = getattr(settings, "space_cleanup", 0) == 1
        prune_keep = _prune_keep(settings)
        if not cleanup and not prune_keep:
            return None
        if now.tzinfo is None:
            raise ValueError("space cleanup clock requires a timezone")
        local = now.astimezone(ET)
        if _market_hours(local):
            return None
        today = local.date()
        with _LOCK:
            thread = _STATE["thread"]
            if thread is not None and thread.is_alive():
                return None
            if time.monotonic() < _STATE["retry_at"]:
                return None
            evening = (local.hour, local.minute) >= (nightly_hour, nightly_minute)
            if _STATE["startup_done"] and not (evening and _STATE["nightly_day"] != today.isoformat()):
                return None
            mark_nightly = evening
            allow_pre = getattr(settings, "db_prune_allow_pre_export", 0) == 1

            def target():
                failed = False
                if cleanup:
                    failed = True
                    try:
                        result = runner(data_dir, today)
                        failed = bool(result and result.get("failed"))
                    except Exception as exc:
                        LOG.warning("Space cleanup unavailable (%s)", type(exc).__name__)
                if prune_keep:
                    try:
                        from .db_prune import run_db_prune
                        run_db_prune(Path(data_dir) / "live.sqlite3", today, keep=prune_keep,
                                     allow_pre_export=allow_pre)
                    except Exception as exc:
                        LOG.warning("DB prune unavailable (%s)", type(exc).__name__)
                with _LOCK:
                    _STATE["startup_done"] = True
                    if mark_nightly and not failed:
                        _STATE["nightly_day"] = today.isoformat()
                    _STATE["retry_at"] = time.monotonic() + retry_seconds if failed else 0.0

            name = "lotto-space-cleanup" if cleanup else "lotto-db-prune"
            thread = threading.Thread(target=target, name=name, daemon=True)
            _STATE["thread"] = thread
            thread.start()
            return thread
    except Exception as exc:
        LOG.warning("Space cleanup unavailable (%s)", type(exc).__name__)
        return None
