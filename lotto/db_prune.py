"""Delete old high-churn rows from the live database so SQLite can reuse the pages.

``LOTTO_DB_PRUNE_KEEP_DAYS`` 0 (the default) returns before the database, the
disk, or the network. With N > 0, keep today plus the N most recent weekdays
and delete older rows from the high-churn tables only. Each day is deleted only
after a HEAD confirms that day's ``live_<day>.sqlite3.gz`` export exists and is
nonempty. Days older than the oldest export object are skipped unless
``LOTTO_DB_PRUNE_ALLOW_PRE_EXPORT=1``. Deletes are small transactions. There is
no VACUUM: the freelist is the point, and the volume has no room for a rewrite.
Timestamp tables that already have an index are walked by that index, so the
snapshot payload is not read while the evening scan looks for old days.
"""
import logging
import os
import re
import shutil
import sqlite3
from datetime import date
from pathlib import Path
from urllib.request import urlopen

from .day_export import et_day, list_s3_archive_names
from .space import _head, _s3_config, protected_days

LOG = logging.getLogger(__name__)

BATCH = 2000
MIN_FREE_BYTES = 40_000_000
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# (table, mode, column, rank_only). mode "day" matches the column; "ts" uses the ET date.
_TABLES = (
    ("snapshots", "ts", "at", False),
    ("decisions", "ts", "at", False),
    ("discovery", "day", "day", False),
    ("chain_coverage", "day", "day", False),
    ("candidate_evaluations", "day", "day", False),
    ("finalb_shadow", "ts", "ts", True),
    ("fix_shadow", "ts", "ts", False),
    ("stock_first_shadow", "ts", "ts", False),
)
_NAMES = tuple(table for table, _, _, _ in _TABLES)


def _q(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise RuntimeError("db prune table is unsupported")
    return f'"{name}"'


def _env_int(name: str) -> int:
    raw = os.getenv(name, "0")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw or not raw.lstrip("-").isdigit():
        return 0
    return int(raw)


def _disabled() -> dict:
    return {"enabled": False, "deleted": {}, "pruned_days": [], "skipped": [], "stopped": None,
            "batches": 0, "max_batch": 0}


def _blank(keep: int) -> dict:
    return {"enabled": True, "keep": keep, "deleted": {name: 0 for name in _NAMES},
            "pruned_days": [], "skipped": [], "stopped": None, "batches": 0, "max_batch": 0,
            "page_count_before": None, "page_count_after": None,
            "freelist_before": None, "freelist_after": None,
            "free_before": None, "free_after": None, "day_rows": {}}


def _counts(conn):
    return (int(conn.execute("PRAGMA page_count").fetchone()[0]),
            int(conn.execute("PRAGMA freelist_count").fetchone()[0]))


def _rollback(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _present(conn) -> set:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _time_indexed(conn, table, column) -> bool:
    """True when an index can walk ``column`` without reading the row body."""
    for row in conn.execute(f"PRAGMA index_list({_q(table)})"):
        info = conn.execute(f"PRAGMA index_info({_q(row[1])})").fetchall()
        if info and info[0][2] == column:
            return True
    return False


def _scan_ts(conn, table, column, rank_only, indexed, batch):
    """Yield ``(rowid, timestamp)`` batches. Indexed tables seek ``(time, rowid)``."""
    quoted, col = _q(table), _q(column)
    if indexed:
        rank = '"side"=\'RANK\' AND ' if rank_only else ""
        sql = (f"SELECT rowid, {col} FROM {quoted} WHERE {rank}({col}, rowid) > (?, ?) "
               f"ORDER BY {col}, rowid LIMIT ?")
        key, rowid = "", 0
        while True:
            rows = conn.execute(sql, (key, rowid, batch)).fetchall()
            if not rows:
                return
            yield rows
            prev = (key, rowid)
            rowid, raw = rows[-1]
            key = raw if isinstance(raw, str) else ""
            if (key, rowid) == prev:
                return
    rank = ' AND "side"=\'RANK\'' if rank_only else ""
    sql = f"SELECT rowid, {col} FROM {quoted} WHERE rowid > ?{rank} ORDER BY rowid LIMIT ?"
    last = 0
    while True:
        rows = conn.execute(sql, (last, batch)).fetchall()
        if not rows:
            return
        yield rows
        last = rows[-1][0]


def _candidate_days(conn, present, cutoff: str, batch: int) -> set:
    days = set()
    for table, mode, column, rank_only in _TABLES:
        if table not in present:
            continue
        quoted, col = _q(table), _q(column)
        if mode == "day":
            for (day,) in conn.execute(f"SELECT DISTINCT {col} FROM {quoted}"):
                if isinstance(day, str) and _DAY.fullmatch(day) and day < cutoff:
                    days.add(day)
            continue
        indexed = _time_indexed(conn, table, column)
        for rows in _scan_ts(conn, table, column, rank_only, indexed, batch):
            for _, raw in rows:
                day = et_day(raw)
                if day and day < cutoff:
                    days.add(day)
    return days


def _oldest_export(opener):
    found = []
    for name in list_s3_archive_names(opener):
        if name.startswith("live_") and name.endswith(".sqlite3.gz"):
            day = name[len("live_"):-len(".sqlite3.gz")]
            if _DAY.fullmatch(day):
                found.append(day)
    return min(found) if found else None


def _classify(days, opener, allow: bool):
    if not days:
        return [], []
    try:
        cfg = _s3_config()
    except Exception as exc:
        LOG.warning("DB prune export check unavailable (%s)", type(exc).__name__)
        return [], [(day, "export bucket unavailable") for day in days]
    try:
        oldest = _oldest_export(opener)
    except Exception as exc:
        LOG.warning("DB prune export list unavailable (%s)", type(exc).__name__)
        oldest = None
    eligible, skipped = [], []
    for day in days:
        key = cfg.prefix + f"live_{day}.sqlite3.gz"
        try:
            remote = _head(cfg, key, opener)
        except Exception as exc:
            LOG.warning("DB prune export check failed for %s (%s)", day, type(exc).__name__)
            skipped.append((day, "export check failed"))
            continue
        size = remote[0] if remote else None
        if isinstance(size, int) and size > 0:
            eligible.append(day)
            continue
        if remote is not None:
            skipped.append((day, "export empty"))
            continue
        if oldest is not None and day < oldest:
            if allow:
                eligible.append(day)
            else:
                skipped.append((day, f"older than oldest export {oldest}; pre-export not allowed"))
            continue
        skipped.append((day, "no verified export"))
    return eligible, skipped


def _stop_for_disk(free_fn, min_free, result) -> bool:
    try:
        free = free_fn()
    except OSError as exc:
        LOG.warning("DB prune disk check failed (%s)", type(exc).__name__)
        result["stopped"] = "disk"
        return True
    if free < min_free:
        result["stopped"] = "low-disk"
        return True
    return False


def _note(result, table, day, n):
    if n <= 0:
        return
    result["deleted"][table] += n
    result["day_rows"][day] = result["day_rows"].get(day, 0) + n
    result["batches"] += 1
    if n > result["max_batch"]:
        result["max_batch"] = n


def _delete_day_table(conn, table, column, days, batch, free_fn, min_free, result):
    quoted, col = _q(table), _q(column)
    sql = (f"DELETE FROM {quoted} WHERE rowid IN "
           f"(SELECT rowid FROM {quoted} WHERE {col}=? LIMIT ?)")
    for day in days:
        while not result["stopped"]:
            if _stop_for_disk(free_fn, min_free, result):
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute(sql, (day, batch)).rowcount
                conn.execute("COMMIT")
            except sqlite3.Error:
                _rollback(conn)
                raise
            if n <= 0:
                break
            _note(result, table, day, n)
            if n < batch:
                break


def _delete_ts_table(conn, table, column, rank_only, days, batch, free_fn, min_free, result):
    quoted = _q(table)
    rank = ' AND "side"=\'RANK\'' if rank_only else ""
    eligible = set(days)
    indexed = _time_indexed(conn, table, column)
    for rows in _scan_ts(conn, table, column, rank_only, indexed, batch):
        if result["stopped"] or _stop_for_disk(free_fn, min_free, result):
            return
        grouped = {}
        for rowid, raw in rows:
            day = et_day(raw)
            if day in eligible:
                grouped.setdefault(day, []).append(rowid)
        if not grouped:
            continue
        if _stop_for_disk(free_fn, min_free, result):
            return
        ids = [rowid for group in grouped.values() for rowid in group]
        marks = ",".join("?" * len(ids))
        delete = f"DELETE FROM {quoted} WHERE rowid IN ({marks}){rank}"
        conn.execute("BEGIN IMMEDIATE")
        try:
            n = conn.execute(delete, ids).rowcount
            conn.execute("COMMIT")
        except sqlite3.Error:
            _rollback(conn)
            raise
        if n < 0:
            n = len(ids)
        if n <= 0:
            continue
        result["deleted"][table] += n
        result["batches"] += 1
        if n > result["max_batch"]:
            result["max_batch"] = n
        for day, group in grouped.items():
            result["day_rows"][day] = result["day_rows"].get(day, 0) + len(group)


def _log(result):
    deleted = " ".join(f"{name}={result['deleted'][name]}" for name in _NAMES)
    pruned = ",".join(result["pruned_days"]) or "-"
    skipped = ", ".join(f"{day} ({reason})" for day, reason in result["skipped"]) or "-"
    pages = _span(result.get("page_count_before"), result.get("page_count_after"))
    freelist = _span(result.get("freelist_before"), result.get("freelist_after"))
    free = f"{_mb(result.get('free_before'))} -> {_mb(result.get('free_after'))}"
    tail = f"; stopped {result['stopped']}" if result.get("stopped") else ""
    LOG.info("DB prune: deleted %s; pruned %s; skipped %s; page_count %s freelist_count %s; free %s%s",
             deleted, pruned, skipped, pages, freelist, free, tail)


def _span(before, after):
    if before is None and after is None:
        return "n/a"
    return f"{before} -> {after}"


def _mb(value):
    if value is None:
        return "n/a"
    return f"{value / 1e6:.1f} MB"


def _finish(result, eligible):
    pruned, skipped = [], list(result["skipped"])
    for day in eligible:
        if result["day_rows"].get(day, 0) > 0:
            pruned.append(day)
        elif result["stopped"]:
            skipped.append((day, f"stopped {result['stopped']}"))
        else:
            pruned.append(day)
    result["pruned_days"] = pruned
    result["skipped"] = skipped
    return result


def run_db_prune(database, today, *, keep=None, allow_pre_export=None, opener=None,
                 batch=BATCH, min_free=MIN_FREE_BYTES, free=None) -> dict:
    """Prune one evening of old rows. ``keep`` <= 0 returns before opening the database."""
    if keep is None:
        keep = _env_int("LOTTO_DB_PRUNE_KEEP_DAYS")
    if type(keep) is not int or keep <= 0:
        return _disabled()
    if allow_pre_export is None:
        allow = _env_int("LOTTO_DB_PRUNE_ALLOW_PRE_EXPORT") == 1
    else:
        allow = allow_pre_export is True or (type(allow_pre_export) is int and allow_pre_export == 1)
    if type(batch) is not int or not 1 <= batch <= BATCH:
        batch = BATCH
    if not isinstance(today, date):
        raise TypeError("db prune requires a date")
    path = Path(database)
    protected = protected_days(today, keep)
    cutoff = min(protected)
    result = _blank(keep)
    if path.is_symlink() or not path.is_file():
        _log(result)
        return result
    free_fn = free or (lambda: shutil.disk_usage(path).free)
    opener = opener or urlopen
    conn = None
    eligible = []
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.isolation_level = None
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA mmap_size=0")
        conn.execute("PRAGMA cache_size=-2000")
        conn.execute("PRAGMA temp_store=MEMORY")
        present = _present(conn)
        result["page_count_before"], result["freelist_before"] = _counts(conn)
        result["free_before"] = free_fn()
        days = sorted(day for day in _candidate_days(conn, present, cutoff, batch) if day not in protected)
        eligible, result["skipped"] = _classify(days, opener, allow)
        result["day_rows"] = {day: 0 for day in eligible}
        if eligible and _stop_for_disk(free_fn, min_free, result):
            pass
        else:
            for table, mode, column, rank_only in _TABLES:
                if result["stopped"] or table not in present or not eligible:
                    continue
                if mode == "day":
                    _delete_day_table(conn, table, column, eligible, batch, free_fn, min_free, result)
                else:
                    _delete_ts_table(conn, table, column, rank_only, eligible, batch, free_fn, min_free, result)
        result["page_count_after"], result["freelist_after"] = _counts(conn)
    except sqlite3.Error as exc:
        if conn is not None:
            _rollback(conn)
            if result["page_count_after"] is None:
                try:
                    result["page_count_after"], result["freelist_after"] = _counts(conn)
                except sqlite3.Error:
                    pass
        result["stopped"] = "sqlite"
        LOG.warning("DB prune stopped (%s)", type(exc).__name__)
    finally:
        if conn is not None:
            conn.close()
    if result["free_after"] is None:
        try:
            result["free_after"] = free_fn()
        except OSError:
            pass
    _finish(result, eligible)
    _log(result)
    return result
