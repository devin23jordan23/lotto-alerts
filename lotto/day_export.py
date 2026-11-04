"""Read-only weekday slices of the live database, uploaded to Google Drive.

The scanner calls ``maybe_day_export`` once per cycle. With ``day_export`` left at
0 the call returns before it touches the database, the network, or alert state.
When it is 1, a daemon thread runs after the configured America/New_York clock
on weekdays. That thread never writes the data volume: the source is opened
``mode=ro``, and the slice, gzip, and manifest live under ``/tmp`` until upload.
"""
import base64
import gzip
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from .models import ET

LOG = logging.getLogger(__name__)

_COPY_BATCH = 25
_SCAN_BATCH = 1000
_TIME_COLUMNS = ("at", "opened_at", "recorded_at", "generated_at")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_LOCK = threading.Lock()
_STATE = {"thread": None, "done": set(), "retry_at": 0.0}


def clear_day_export_state():
    with _LOCK:
        _STATE["thread"] = None
        _STATE["done"] = set()
        _STATE["retry_at"] = 0.0


def weekday_window(today: date, backfill_days: int) -> list[str]:
    """Weekdays in the last ``backfill_days`` calendar days, including ``today``.

    ``backfill_days`` below 1 still returns today. The scheduler only calls this
    after today's session cutoff, so every date in the window has ended.
    """
    span = 1 if backfill_days < 1 else backfill_days
    start = today - timedelta(days=span - 1)
    days = []
    cursor = start
    while cursor <= today:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def et_day(value):
    """America/New_York calendar date for an offset timestamp, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(ET).date().isoformat()


def _quote(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise RuntimeError("day export schema name is unsupported")
    return f'"{name}"'


def open_readonly(path) -> sqlite3.Connection:
    """Open a database URI that SQLite itself will refuse to write."""
    source = Path(path).resolve()
    uri = "file:" + quote(source.as_posix(), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    conn.isolation_level = None
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.execute("PRAGMA mmap_size=0")
    conn.execute("PRAGMA cache_size=-2000")
    return conn


def _schema_rows(conn):
    return conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
    ).fetchall()


def _time_column(names):
    for name in _TIME_COLUMNS:
        if name in names:
            return name
    for name in names:
        if name == "at" or name.endswith("_at"):
            return name
    return None


def _insert_rowids(src, dest, table, width, rowids):
    if not rowids:
        return 0
    quoted = _quote(table)
    marks = ",".join("?" * len(rowids))
    rows = src.execute(f"SELECT * FROM {quoted} WHERE rowid IN ({marks})", rowids).fetchall()
    placeholders = ",".join("?" * width)
    with dest:
        dest.executemany(f"INSERT INTO {quoted} VALUES ({placeholders})", rows)
    return len(rows)


def _copy_table(src, dest, table, columns, day, copy_batch):
    names = [column[1] for column in columns]
    quoted = _quote(table)
    total = 0
    if "day" in names:
        last = 0
        while True:
            found = src.execute(
                f'SELECT rowid FROM {quoted} WHERE "day" = ? AND rowid > ? ORDER BY rowid LIMIT ?',
                (day, last, copy_batch),
            ).fetchall()
            if not found:
                break
            last = found[-1][0]
            total += _insert_rowids(src, dest, table, len(names), [row[0] for row in found])
        return total
    time_col = _time_column(names)
    if time_col is None:
        last = 0
        while True:
            found = src.execute(
                f"SELECT rowid FROM {quoted} WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (last, copy_batch),
            ).fetchall()
            if not found:
                break
            last = found[-1][0]
            total += _insert_rowids(src, dest, table, len(names), [row[0] for row in found])
        return total
    last = 0
    pending = []
    qtime = _quote(time_col)
    while True:
        scanned = src.execute(
            f"SELECT rowid, {qtime} FROM {quoted} WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (last, _SCAN_BATCH),
        ).fetchall()
        if not scanned:
            break
        last = scanned[-1][0]
        for rowid, raw in scanned:
            if et_day(raw) != day:
                continue
            pending.append(rowid)
            if len(pending) >= copy_batch:
                total += _insert_rowids(src, dest, table, len(names), pending)
                pending = []
    total += _insert_rowids(src, dest, table, len(names), pending)
    return total


def slice_session(source, dest, day: str, *, copy_batch: int = _COPY_BATCH) -> dict:
    """Copy one ET session date into a new database and return per-table counts.

    Tables with a ``day`` column keep ``day = <session>``. Other tables keep rows
    whose first timestamp column falls on that America/New_York date. Tables with
    neither are reference data and are copied whole. Reads are batched and the
    source connection is read-only, so a slice cannot rewrite the live file.
    """
    if not _DAY.fullmatch(day):
        raise RuntimeError("day export date is invalid")
    if copy_batch < 1 or copy_batch > 200:
        raise RuntimeError("day export batch is invalid")
    source_path = Path(source).resolve()
    dest_path = Path(dest)
    if dest_path.resolve() == source_path:
        raise RuntimeError("day export refuses to write the source database")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.unlink(missing_ok=True)
    src = open_readonly(source_path)
    out = sqlite3.connect(dest_path)
    counts = {}
    try:
        schema = _schema_rows(src)
        for kind, name, sql in schema:
            if kind == "table":
                out.execute(sql)
        out.commit()
        for kind, name, sql in schema:
            if kind != "table":
                continue
            columns = src.execute(f"PRAGMA table_info({_quote(name)})").fetchall()
            counts[name] = _copy_table(src, out, name, columns, day, copy_batch)
        for kind, name, sql in schema:
            if kind != "table":
                out.execute(sql)
        out.commit()
        check = out.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError("day slice failed integrity check")
    except Exception:
        out.close()
        src.close()
        dest_path.unlink(missing_ok=True)
        raise
    out.close()
    src.close()
    return counts


def gzip_sha256(source, dest) -> str:
    """Stream ``source`` into a gzip file and return the sha256 of the gzip bytes."""
    hasher = sha256()
    with open(dest, "wb") as raw:
        writer = _HashWriter(raw, hasher)
        with gzip.GzipFile(filename="", mode="wb", fileobj=writer, compresslevel=1, mtime=0) as packed:
            with open(source, "rb") as incoming:
                while chunk := incoming.read(1024 * 1024):
                    packed.write(chunk)
    return hasher.hexdigest()


class _HashWriter:
    def __init__(self, raw, hasher):
        self.raw = raw
        self.hasher = hasher

    def write(self, data):
        self.hasher.update(data)
        self.raw.write(data)
        return len(data)


def code_commit() -> str:
    for name in ("RAILWAY_GIT_COMMIT_SHA", "GITHUB_SHA"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return "unknown"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _der_read(data, offset):
    if offset >= len(data):
        raise ValueError("private key")
    tag = data[offset]
    offset += 1
    if offset >= len(data):
        raise ValueError("private key")
    length = data[offset]
    offset += 1
    if length & 0x80:
        nbytes = length & 0x7F
        if nbytes == 0 or nbytes > 4 or offset + nbytes > len(data):
            raise ValueError("private key")
        length = int.from_bytes(data[offset:offset + nbytes], "big")
        offset += nbytes
    if length < 0 or offset + length > len(data):
        raise ValueError("private key")
    return tag, data[offset:offset + length], offset + length


def _der_children(data):
    items = []
    offset = 0
    while offset < len(data):
        tag, value, offset = _der_read(data, offset)
        items.append((tag, value))
    return items


def _rsa_integers(pem: str):
    body = "".join(line.strip() for line in pem.splitlines() if line and not line.startswith("-----"))
    try:
        der = base64.b64decode(body, validate=True)
        children = _der_children(_der_read(der, 0)[1])
    except (ValueError, TypeError):
        raise ValueError("private key") from None
    if len(children) >= 3 and children[1][0] == 0x30 and children[2][0] == 0x04:
        children = _der_children(_der_read(children[2][1], 0)[1])
    numbers = [int.from_bytes(value, "big") for tag, value in children if tag == 0x02]
    if len(numbers) < 4:
        raise ValueError("private key")
    return numbers[1], numbers[3]


def rsa_sha256_sign(pem: str, message: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 SHA-256. Service-account keys stay in memory."""
    modulus, exponent = _rsa_integers(pem)
    digest = sha256(message).digest()
    padded = _SHA256_PREFIX + digest
    size = (modulus.bit_length() + 7) // 8
    if size < len(padded) + 11:
        raise ValueError("private key")
    encoded = b"\x00\x01" + (b"\xff" * (size - len(padded) - 3)) + b"\x00" + padded
    signature = pow(int.from_bytes(encoded, "big"), exponent, modulus)
    return signature.to_bytes(size, "big")


def fetch_access_token(credentials: dict, opener=urlopen) -> str:
    if not isinstance(credentials, dict):
        raise RuntimeError("day export credentials are unreadable")
    email = credentials.get("client_email")
    pem = credentials.get("private_key")
    if not isinstance(email, str) or not isinstance(pem, str) or not email or not pem:
        raise RuntimeError("day export credentials are unreadable")
    issued = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64url(json.dumps({
        "iss": email,
        "scope": _DRIVE_SCOPE,
        "aud": _TOKEN_URL,
        "iat": issued,
        "exp": issued + 3600,
    }, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    assertion = signing_input.decode() + "." + _b64url(rsa_sha256_sign(pem, signing_input))
    body = urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }).encode()
    request = Request(_TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with opener(request, timeout=30) as response:
        payload = json.loads(response.read().decode())
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("day export authorization failed")
    return token


def list_archive_names(token: str, folder_id: str, opener=urlopen) -> set:
    names = set()
    page = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false and name contains 'live_'",
            "fields": "nextPageToken,files(name)",
            "pageSize": "1000",
            "spaces": "drive",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "corpora": "allDrives",
        }
        if page:
            params["pageToken"] = page
        request = Request(
            "https://www.googleapis.com/drive/v3/files?" + urlencode(params),
            headers={"Authorization": f"Bearer {token}"},
        )
        with opener(request, timeout=60) as response:
            payload = json.loads(response.read().decode())
        for item in payload.get("files") or []:
            name = item.get("name") or ""
            if name.startswith("live_") and (name.endswith(".sqlite3.gz") or name.endswith(".manifest.json")):
                names.add(name)
        page = payload.get("nextPageToken")
        if not page:
            return names


def upload_file(token: str, folder_id: str, name: str, path: Path, mime: str, opener=urlopen):
    size = path.stat().st_size
    meta = json.dumps({"name": name, "parents": [folder_id]}).encode()
    start = Request(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true",
        data=meta,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime,
            "X-Upload-Content-Length": str(size),
        },
    )
    with opener(start, timeout=60) as response:
        location = response.headers["Location"]
        response.read()
    if urlparse(location).scheme != "https":
        raise RuntimeError("day export upload session was refused")
    with path.open("rb") as raw:
        put = Request(location, data=raw, method="PUT", headers={
            "Content-Type": mime,
            "Content-Length": str(size),
        })
        with opener(put, timeout=600) as response:
            response.read()


def _folder_id():
    folder = os.getenv("GDRIVE_FOLDER_ID", "").strip()
    if not folder or not all(ch.isalnum() or ch in "-_" for ch in folder):
        raise RuntimeError("day export folder is invalid")
    return folder


def _credentials():
    raw = os.getenv("GDRIVE_SA_JSON", "")
    if not raw:
        raise RuntimeError("day export is not configured")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("day export credentials are unreadable") from None
    return parsed


def _remove(path: Path):
    path.unlink(missing_ok=True)
    for suffix in ("-journal", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _export_one(database, day, token, folder, names, opener, work_dir: Path):
    gz_name = f"live_{day}.sqlite3.gz"
    manifest_name = f"live_{day}.manifest.json"
    if gz_name in names:
        if manifest_name not in names:
            LOG.warning("Day export archive present without manifest: %s", day)
        else:
            LOG.info("Day export already uploaded: %s", day)
        return
    db_path = work_dir / f"live_{day}.sqlite3"
    gz_path = work_dir / gz_name
    manifest_path = work_dir / manifest_name
    try:
        counts = slice_session(database, db_path, day)
        digest = gzip_sha256(db_path, gz_path)
        _remove(db_path)
        manifest = {
            "date": day,
            "row_counts": counts,
            "sha256": digest,
            "source_mtime": datetime.fromtimestamp(Path(database).stat().st_mtime, timezone.utc).isoformat(),
            "commit": code_commit(),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        upload_file(token, folder, gz_name, gz_path, "application/gzip", opener)
        upload_file(token, folder, manifest_name, manifest_path, "application/json", opener)
        names.add(gz_name)
        names.add(manifest_name)
        LOG.info("Day export uploaded: %s", day)
    finally:
        _remove(db_path)
        gz_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)


def run_day_exports(database, dates, completed, *, opener=None, work_dir=None) -> int:
    """Upload each missing session date. Returns how many dates still need a retry."""
    opener = opener or urlopen
    root = Path(work_dir) if work_dir is not None else Path("/tmp")
    source = Path(database).resolve()
    if source.parent == root.resolve():
        raise RuntimeError("day export refuses to write beside the source database")
    root.mkdir(parents=True, exist_ok=True)
    token = fetch_access_token(_credentials(), opener)
    names = list_archive_names(token, _folder_id(), opener)
    failures = 0
    for day in dates:
        if not _DAY.fullmatch(day):
            failures += 1
            LOG.warning("Day export failed for %s (%s)", day, "ValueError")
            continue
        try:
            _export_one(source, day, token, _folder_id(), names, opener, root)
            completed.add(day)
        except Exception as exc:
            failures += 1
            LOG.warning("Day export failed for %s (%s)", day, type(exc).__name__)
    return failures


def _start(database, dates, runner, retry_seconds):
    with _LOCK:
        thread = _STATE["thread"]
        if thread is not None and thread.is_alive():
            return None
        if time.monotonic() < _STATE["retry_at"]:
            return None
        pending = [day for day in dates if day not in _STATE["done"]]
        if not pending:
            return None

        def target():
            completed = set()
            failures = 1
            try:
                result = runner(database, pending, completed)
                failures = 0 if result is None else int(result)
            except Exception as exc:
                LOG.warning("Day export unavailable (%s)", type(exc).__name__)
            with _LOCK:
                _STATE["done"].update(completed)
                _STATE["retry_at"] = time.monotonic() + retry_seconds if failures else 0.0

        thread = threading.Thread(target=target, name="lotto-day-export", daemon=True)
        _STATE["thread"] = thread
        thread.start()
        return thread


def maybe_day_export(settings, database, now, *, runner=run_day_exports, retry_seconds=900):
    """Schedule a background export, or return immediately when the flag is off."""
    try:
        if settings.day_export != 1:
            return None
        if now.tzinfo is None:
            raise ValueError("day export clock requires a timezone")
        local = now.astimezone(ET)
        if local.weekday() >= 5:
            return None
        cutoff = local.replace(
            hour=settings.day_export_hour, minute=settings.day_export_minute,
            second=0, microsecond=0,
        )
        if local < cutoff:
            return None
        dates = weekday_window(local.date(), settings.day_export_backfill_days)
        return _start(database, dates, runner, retry_seconds)
    except Exception as exc:
        LOG.warning("Day export unavailable (%s)", type(exc).__name__)
        return None
