"""Durable SQLite location, additive migrations, and session-coverage checks.

Nothing in this module deletes, renames, or rebuilds a database. A missing live
file is a refusal during the regular session, not a prompt to start a new one.
"""
import json
import os
import sqlite3
import zlib
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

from .models import ET

SCHEMA_VERSION = 2
OPEN_GRACE = timedelta(minutes=10)
CLOSE_GRACE = timedelta(minutes=15)
MAX_INTERNAL_GAP = timedelta(minutes=20)
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)


class PersistenceError(RuntimeError):
    """The process must stop rather than create or rewrite a database."""


def resolve_database_path(mode: str, explicit: str | None = None, env=None) -> str:
    """Explicit --db wins, then LOTTO_DB_PATH, then DATA_DIR.

    Live, report, nightly, and feedback keep the historical live.sqlite3 name so an
    existing Railway volume is opened rather than replaced by a new filename.
    """
    if explicit:
        return explicit
    env = os.environ if env is None else env
    configured = env.get("LOTTO_DB_PATH")
    if configured:
        return configured
    data_dir = Path(env.get("DATA_DIR", "data"))
    shared = {"live", "report", "nightly", "feedback", "backup", "session", "migrate", "exit-replay", "check"}
    name = "live.sqlite3" if mode in shared else f"{mode}.sqlite3"
    return str(data_dir / name)


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def directory_is_mountpoint(path: Path) -> bool:
    """True when path is a distinct mounted filesystem from its parent."""
    try:
        if not path.is_dir():
            return False
        return path.stat().st_dev != path.parent.stat().st_dev
    except FileNotFoundError:
        return False


def read_mount_table() -> list[str]:
    """Mount points from the kernel table. Empty when /proc is unavailable."""
    found = []
    for name in ("/proc/self/mountinfo", "/proc/mounts"):
        try:
            lines = Path(name).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if name.endswith("mountinfo") and len(parts) >= 5:
                found.append(parts[4].replace("\\040", " "))
            elif not name.endswith("mountinfo") and len(parts) >= 2:
                found.append(parts[1].replace("\\040", " "))
        if found:
            return found
    return found


def volume_mount_evidenced(root: Path, mount_table: list[str] | None = None) -> bool:
    """True only when the directory is a real mount, not merely a path prefix.

    Railway documents that RAILWAY_VOLUME_MOUNT_PATH is the attached volume's
    mount path. The string is not evidence that this process can see that mount.
    """
    if directory_is_mountpoint(root):
        return True
    table = read_mount_table() if mount_table is None else mount_table
    try:
        target = root.resolve().as_posix()
    except OSError:
        return False
    return target in table


def _railway(env) -> bool:
    return bool(env.get("RAILWAY_SERVICE_ID"))


def in_regular_session_clock(now: datetime) -> bool:
    """Weekday 09:30–16:00 ET by the clock. This is not the exchange calendar."""
    local = now.astimezone(ET)
    return local.weekday() < 5 and SESSION_OPEN <= local.time() < SESSION_CLOSE


def live_start_blockers(path: str, now: datetime, env=None, mount_table: list[str] | None = None) -> list[str]:
    """Reasons live mode must not open this path. Empty means it may proceed.

    An existing non-empty file is always kept. Creation is the dangerous case:
    on Railway the file has to live on the mounted volume, and during the cash
    session an empty path is refused so a redeploy cannot look like a full day
    that started late.
    """
    env = os.environ if env is None else env
    blockers = []
    database = Path(path)
    if _railway(env):
        # Both variables are injected together only when a volume is attached
        # (Railway service reference). Either one alone is not a volume.
        mount = env.get("RAILWAY_VOLUME_MOUNT_PATH") or ""
        name = env.get("RAILWAY_VOLUME_NAME") or ""
        if not mount or not name:
            blockers.append(
                "Railway volume is not attached. RAILWAY_VOLUME_MOUNT_PATH and "
                "RAILWAY_VOLUME_NAME are set together only when a volume is attached. "
                "A path prefix is not a volume."
            )
        else:
            root = Path(mount)
            if not root.is_dir():
                blockers.append(f"Railway volume {mount} is not a directory. Refusing to create it")
            elif not path_is_under(database, root):
                blockers.append(f"Database {path} is outside the Railway volume {mount}")
            elif env.get("LOTTO_TRUST_VOLUME_MOUNT") != "1" and not volume_mount_evidenced(root, mount_table):
                blockers.append(
                    f"{mount} is not a mounted filesystem. A path prefix is not a volume. "
                    "Attach the Railway volume. LOTTO_TRUST_VOLUME_MOUNT=1 is an unverified override, not proof of a mount."
                )
    file_ready = database.is_file() and database.stat().st_size > 0
    if file_ready:
        return blockers
    if in_regular_session_clock(now) and env.get("LOTTO_ALLOW_EMPTY_LIVE_DB") != "1":
        blockers.append(
            f"Live database {path} is missing or empty during the regular session. "
            "Refusing to create a new one, because that drops the morning and looks like a late start. "
            "Restore the volume file, or set LOTTO_ALLOW_EMPTY_LIVE_DB=1 for a genuine first start"
        )
    elif not database.parent.exists() and _railway(env):
        blockers.append(f"Database directory {database.parent} does not exist. Refusing to create it on Railway")
    return blockers


def require_existing_database(path: str) -> None:
    file = Path(path)
    if not file.is_file() or file.stat().st_size == 0:
        raise PersistenceError(
            f"Database {path} is missing or empty. Refusing to create a replacement. "
            "Pass the existing file with --db or LOTTO_DB_PATH."
        )


def connect_readonly(path: str) -> sqlite3.Connection:
    require_existing_database(path)
    uri = "file:" + quote(Path(path).resolve().as_posix(), safe="/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def backup_database(source: str, dest: str) -> dict:
    """Consistent SQLite backup. Never overwrites dest and never modifies source pages."""
    require_existing_database(source)
    target = Path(dest)
    if target.exists():
        raise PersistenceError(f"Backup destination {dest} already exists. Refusing to replace it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    uri = "file:" + quote(Path(source).resolve().as_posix(), safe="/") + "?mode=ro"
    src = sqlite3.connect(uri, uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise PersistenceError(f"Backup of {source} failed integrity_check: {check}")
    except Exception:
        dst.close()
        src.close()
        target.unlink(missing_ok=True)
        raise
    dst.close()
    src.close()
    return {"source": str(Path(source).resolve()), "dest": str(target.resolve()), "bytes": target.stat().st_size}


def table_exists(connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def refuse_newer_schema(path: str) -> int:
    """Read the schema through a read-only connection. Raise before any writable open."""
    connection = connect_readonly(path)
    try:
        version = schema_version(connection)
    finally:
        connection.close()
    if version > SCHEMA_VERSION:
        raise PersistenceError(
            f"Database schema {version} is newer than this build ({SCHEMA_VERSION}). "
            "Refusing to open it for write so an older build cannot stamp a downgrade. "
            "Deploy the matching build, or restore a backup of this file."
        )
    return version


def schema_version(connection) -> int:
    versions = []
    try:
        versions.append(int(connection.execute("PRAGMA user_version").fetchone()[0]))
    except sqlite3.Error:
        pass
    if table_exists(connection, "schema_meta"):
        row = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if row is not None:
            try:
                versions.append(int(row[0]))
            except (TypeError, ValueError):
                versions.append(SCHEMA_VERSION + 1)
    return max(versions, default=0)


def parse_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(ET)


def _day_bounds(day: str) -> tuple[str, str]:
    start = datetime.fromisoformat(day) - timedelta(days=1)
    end = datetime.fromisoformat(day) + timedelta(days=2)
    return start.date().isoformat(), end.date().isoformat()


def _timestamps(connection, table: str, day: str) -> tuple[list[datetime], int]:
    if not table_exists(connection, table):
        return [], 0
    low, high = _day_bounds(day)
    found, unparsed = [], 0
    for row in connection.execute(f"SELECT at FROM {table} WHERE at >= ? AND at < ?", (low, high)):
        parsed = parse_timestamp(row[0])
        if parsed is None:
            unparsed += 1
            continue
        if parsed.date().isoformat() == day:
            found.append(parsed)
    return found, unparsed


def list_session_days(connection) -> list[str]:
    days = set()
    for table in ("snapshots", "discovery"):
        if not table_exists(connection, table):
            continue
        for row in connection.execute(f"SELECT at FROM {table}"):
            parsed = parse_timestamp(row[0])
            if parsed is not None:
                days.add(parsed.date().isoformat())
    return sorted(days)


def _payload_session_end(payload):
    try:
        raw = zlib.decompress(payload) if isinstance(payload, bytes) else payload
        if isinstance(raw, bytes):
            raw = raw.decode()
        data = json.loads(raw)
    except (OSError, zlib.error, json.JSONDecodeError, UnicodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("session_end")
    return parse_timestamp(value) if isinstance(value, str) else None


def _collector_clock(connection, day: str):
    """Union of discovery and snapshot timestamps. Not alerts or option quotes."""
    parsed, counts, unparsed = [], {}, {}
    for table in ("snapshots", "discovery"):
        found, bad = _timestamps(connection, table, day)
        counts[table] = len(found)
        unparsed[table] = bad
        parsed.extend(found)
    return sorted(set(parsed)), counts, unparsed


def _declared_close(connection, day: str, open_at: datetime):
    """Scheduled cash close from snapshot.session_end, else 16:00 ET."""
    default = open_at.replace(hour=16, minute=0, second=0, microsecond=0)
    if not table_exists(connection, "snapshots"):
        return default, "default_16:00", False
    low, high = _day_bounds(day)
    on_day = []
    for (raw_at,) in connection.execute(
            "SELECT at FROM snapshots WHERE at >= ? AND at < ? ORDER BY at", (low, high)):
        parsed = parse_timestamp(raw_at)
        if parsed is not None and parsed.date().isoformat() == day:
            on_day.append(raw_at)
    ends = []
    samples = [on_day[0], on_day[-1]] if on_day else []
    for raw_at in dict.fromkeys(samples):
        row = connection.execute("SELECT payload FROM snapshots WHERE at=? LIMIT 1", (raw_at,)).fetchone()
        if row is None or row[0] is None:
            continue
        end = _payload_session_end(row[0])
        if end is None:
            continue
        local = end.astimezone(ET).replace(second=0, microsecond=0)
        if local.date().isoformat() == day and open_at < local <= default:
            ends.append(local)
    distinct = {item.isoformat() for item in ends}
    if len(distinct) > 1:
        return max(ends), "snapshot.session_end", True
    if len(distinct) == 1:
        return ends[0], "snapshot.session_end", False
    return default, "default_16:00", False


def assess_session(connection, day: str, *, db_path: str | None = None, now: datetime | None = None) -> dict:
    """Whether the collector covered one America/New_York cash session.

    The clock is the union of discovery.at and snapshots.at. Alert times,
    decision times, and an individual contract's option quotes are not coverage.
    A quiet session with heartbeats and no candidates is complete. A weekend is
    closed, not a missing capture. An early close uses snapshot.session_end.
    Exchange holidays are not in this repo: a weekday with no heartbeats stays
    ungraded rather than being called a holiday.
    """
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        raise PersistenceError("Session day must be YYYY-MM-DD")
    clock, source_counts, unparsed = _collector_clock(connection, day)
    snapshots, _bad_snaps = _timestamps(connection, "snapshots", day)
    decisions, bad_decisions = _timestamps(connection, "decisions", day)
    open_at = datetime.fromisoformat(day).replace(hour=9, minute=30, second=0, microsecond=0, tzinfo=ET)
    close_at, close_source, close_disagreement = _declared_close(connection, day, open_at)
    snapshots.sort()
    weekend = open_at.weekday() >= 5
    rth = [item for item in clock if open_at <= item < close_at]
    defects = []
    gap = timedelta(0)
    in_session = False
    if weekend:
        defects.append("weekend")
    elif close_disagreement:
        defects.append("session_end_disagreement")
    elif not rth:
        defects.append("no_collector_heartbeats")
    else:
        if rth[0] > open_at + OPEN_GRACE:
            defects.append("opens_after_grace")
        session_over = now is None or now.astimezone(ET) >= close_at
        in_session = now is not None and open_at <= now.astimezone(ET) < close_at
        if session_over and rth[-1] < close_at - CLOSE_GRACE:
            defects.append("ends_before_close")
        gap = max((b - a for a, b in zip(rth, rth[1:])), default=timedelta(0))
        if gap > MAX_INTERNAL_GAP:
            defects.append("internal_gap")
    if weekend:
        status = "market_closed"
    elif "no_collector_heartbeats" in defects and source_counts.get("snapshots", 0) == 0 and source_counts.get("discovery", 0) == 0:
        status = "empty"
    elif any(item in defects for item in ("opens_after_grace", "ends_before_close", "internal_gap",
                                          "no_collector_heartbeats", "session_end_disagreement")):
        status = "partial"
    elif in_session:
        status = "in_progress"
    else:
        status = "complete"
    capture_defects = {"opens_after_grace", "ends_before_close", "internal_gap",
                       "no_collector_heartbeats", "session_end_disagreement"}
    missing_capture = any(item in defects for item in capture_defects)
    counts = _table_counts(connection, day)
    file = Path(db_path) if db_path else None
    return {
        "day": day,
        "timezone": "America/New_York",
        "status": status,
        "defects": defects,
        "missing_capture": missing_capture,
        "grade_as_full_day": status == "complete",
        "coverage_source": "union of discovery.at and snapshots.at",
        "alerts_used_for_coverage": False,
        "option_quotes_used_for_coverage": False,
        "first_at": snapshots[0].isoformat() if snapshots else None,
        "last_at": snapshots[-1].isoformat() if snapshots else None,
        "first_rth_at": rth[0].isoformat() if rth else None,
        "last_rth_at": rth[-1].isoformat() if rth else None,
        "snapshot_count": len(snapshots),
        "discovery_count": source_counts.get("discovery", 0),
        "heartbeat_count": len(rth),
        "rth_observation_count": len(rth),
        "decision_count": len(decisions),
        "unparsed_decision_timestamps": bad_decisions,
        "unparsed_heartbeat_timestamps": unparsed,
        "largest_rth_gap_seconds": gap.total_seconds(),
        "scheduled_close": None if weekend else close_at.isoformat(),
        "scheduled_close_source": "weekend" if weekend else close_source,
        "open_grace_minutes": OPEN_GRACE.total_seconds() / 60,
        "close_grace_minutes": CLOSE_GRACE.total_seconds() / 60,
        "max_internal_gap_minutes": MAX_INTERNAL_GAP.total_seconds() / 60,
        "quiet_session": status in {"complete", "in_progress"} and (counts.get("alerts") or 0) == 0,
        "session_clock": "collector heartbeats against the cash session; weekends are closed; holidays are not inferred",
        "exchange_calendar": "not in this repository; weekday with no heartbeats is ungraded, not labeled a holiday",
        "db_path": str(file.resolve()) if file and file.exists() else db_path,
        "db_bytes": file.stat().st_size if file and file.is_file() else None,
        "schema_version": schema_version(connection),
        "counts": counts,
    }


def _table_counts(connection, day: str) -> dict:
    counts = {}
    for name in ("alerts", "candidate_evaluations", "discovery", "chain_coverage"):
        if not table_exists(connection, name):
            counts[name] = None
            continue
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({name})")}
        if "day" not in columns:
            counts[name] = None
            continue
        counts[name] = connection.execute(f"SELECT COUNT(*) FROM {name} WHERE day=?", (day,)).fetchone()[0]
    return counts


def file_identity(path: str) -> dict:
    file = Path(path)
    if not file.is_file():
        return {"path": str(file), "exists": False, "bytes": 0, "inode": None, "device": None}
    stat = file.stat()
    return {
        "path": str(file.resolve()),
        "exists": True,
        "bytes": stat.st_size,
        "inode": stat.st_ino,
        "device": stat.st_dev,
    }
