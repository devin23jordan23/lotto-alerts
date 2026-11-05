"""Read-only weekday slices of the live database, uploaded to Drive or S3.

The scanner calls ``maybe_day_export`` once per cycle. With ``day_export`` left at
0 the call returns before it touches the database, the network, or alert state.
When it is 1, a daemon thread runs after the configured America/New_York clock
on weekdays. That thread never writes the data volume: the source is opened
``mode=ro``, and the slice, gzip, and manifest live under ``/tmp`` until upload.
``day_export_target`` 0 keeps the Google Drive upload. 1 signs a single PutObject
for the same archive names on an S3-compatible bucket.
"""
import base64
import gzip
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ElementTree
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
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
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


def _aws_quote(value) -> str:
    return quote(str(value), safe="-_.~")


def _canonical_query(items) -> str:
    encoded = sorted((_aws_quote(key), _aws_quote(value)) for key, value in items)
    return "&".join(f"{key}={value}" for key, value in encoded)


def _encode_key(key: str) -> str:
    return "/".join(_aws_quote(part) for part in key.split("/"))


def _canonical_headers(headers):
    folded = {}
    for name, value in headers.items():
        key = name.strip().lower()
        text = " ".join(str(value).strip().split())
        folded[key] = text if key not in folded else folded[key] + "," + text
    names = sorted(folded)
    return "".join(f"{name}:{folded[name]}\n" for name in names), ";".join(names)


def sigv4_authorization(method, uri, query, headers, payload_hash, *,
                        access_key, secret_key, region, service="s3") -> str:
    """AWS Signature Version 4 Authorization header for one S3 request.

    ``headers`` are the signed headers only. ``query`` is already canonical.
    The secret is an HMAC key and is not copied into the header.
    """
    canonical_headers, signed = _canonical_headers(headers)
    amz_date = None
    for name, value in headers.items():
        if name.strip().lower() == "x-amz-date":
            amz_date = " ".join(str(value).strip().split())
    if not amz_date or len(amz_date) < 8:
        raise RuntimeError("day export signature date is invalid")
    canonical_request = (
        f"{method.upper()}\n{uri}\n{query or ''}\n{canonical_headers}\n{signed}\n{payload_hash}"
    )
    datestamp = amz_date[:8]
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    hashed = sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashed}"
    key = ("AWS4" + secret_key).encode("utf-8")
    for part in (datestamp, region, service, "aws4_request"):
        key = hmac.new(key, part.encode("utf-8"), sha256).digest()
    signature = hmac.new(key, string_to_sign.encode("utf-8"), sha256).hexdigest()
    return (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )


class _S3:
    def __init__(self, bucket, access_key, secret_key, region, prefix, url_style, host):
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.prefix = prefix
        self.url_style = url_style
        self.host = host


def _credential(value):
    if not isinstance(value, str):
        raise RuntimeError("day export is not configured")
    value = value.strip()
    if not value or len(value) > 256 or any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
        raise RuntimeError("day export is not configured")
    return value


def _normalize_prefix(raw):
    if raw is None:
        raw = "day-export/"
    if not isinstance(raw, str):
        raise RuntimeError("day export prefix is invalid")
    if raw.startswith("/") or any(ord(ch) < 33 or ch in "?#\\" for ch in raw):
        raise RuntimeError("day export prefix is invalid")
    if raw and not raw.endswith("/"):
        raw += "/"
    parts = [part for part in raw.split("/") if part]
    if any(part in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts):
        raise RuntimeError("day export prefix is invalid")
    return "".join(part + "/" for part in parts)


def _s3_config() -> _S3:
    endpoint = os.getenv("LOTTO_S3_ENDPOINT", "").strip()
    bucket = os.getenv("LOTTO_S3_BUCKET", "").strip()
    access_raw = os.getenv("LOTTO_S3_ACCESS_KEY_ID", "")
    secret_raw = os.getenv("LOTTO_S3_SECRET_ACCESS_KEY", "")
    if not endpoint or not bucket or not access_raw.strip() or not secret_raw.strip():
        raise RuntimeError("day export is not configured")
    access_key = _credential(access_raw)
    secret_key = _credential(secret_raw)
    region = os.getenv("LOTTO_S3_REGION", "auto")
    region = "auto" if region is None or not region.strip() else region.strip().lower()
    style = os.getenv("LOTTO_S3_URL_STYLE", "virtual")
    style = "virtual" if style is None or not style.strip() else style.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise RuntimeError("day export bucket is invalid")
    if not re.fullmatch(r"[a-z0-9-]{1,32}", region):
        raise RuntimeError("day export region is invalid")
    if style not in ("virtual", "path"):
        raise RuntimeError("day export url style is invalid")
    parsed = urlparse(endpoint)
    if (parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise RuntimeError("day export endpoint is invalid")
    host = f"{bucket}.{parsed.hostname}" if style == "virtual" else parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return _S3(bucket, access_key, secret_key, region, _normalize_prefix(os.getenv("LOTTO_S3_PREFIX")), style, host)


def _list_uri(cfg: _S3) -> str:
    if cfg.url_style == "path":
        return "/" + _aws_quote(cfg.bucket)
    return "/"


def _object_uri(cfg: _S3, key: str) -> str:
    encoded = _encode_key(key)
    if cfg.url_style == "path":
        return "/" + _aws_quote(cfg.bucket) + "/" + encoded
    return "/" + encoded


def _s3_open(cfg: _S3, method, *, canonical_uri, query="", content_type=None, body_path=None,
             payload_hash=None, opener, timeout, error):
    payload_hash = payload_hash or _EMPTY_SHA256
    if not re.fullmatch(r"[0-9a-f]{64}", payload_hash):
        raise RuntimeError(error)
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    signed = {
        "host": cfg.host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if content_type:
        signed["content-type"] = content_type
    authorization = sigv4_authorization(
        method, canonical_uri, query, signed, payload_hash,
        access_key=cfg.access_key, secret_key=cfg.secret_key, region=cfg.region,
    )
    send = {
        "Host": cfg.host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "Authorization": authorization,
    }
    if content_type:
        send["Content-Type"] = content_type
    data = None
    if body_path is not None:
        send["Content-Length"] = str(Path(body_path).stat().st_size)
        data = Path(body_path).open("rb")
    url = f"https://{cfg.host}{canonical_uri}"
    if query:
        url = f"{url}?{query}"
    try:
        request = Request(url, data=data, method=method, headers=send)
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            body = response.read()
        if status is not None and int(status) >= 400:
            raise RuntimeError(error)
        return body or b""
    finally:
        if data is not None:
            data.close()


def _xml_local(tag):
    return tag.rsplit("}", 1)[-1]


def _parse_list(body):
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise RuntimeError("day export listing was refused") from None
    if _xml_local(root.tag) != "ListBucketResult":
        raise RuntimeError("day export listing was refused")
    keys = []
    truncated = False
    token = None
    for child in list(root):
        name = _xml_local(child.tag)
        if name == "Contents":
            for inner in list(child):
                if _xml_local(inner.tag) == "Key" and inner.text:
                    keys.append(inner.text)
        elif name == "IsTruncated":
            truncated = (child.text or "").strip().lower() == "true"
        elif name == "NextContinuationToken" and child.text:
            token = child.text
    return keys, truncated, token


def _object_name(prefix, key):
    if not key.startswith(prefix):
        return None
    name = key[len(prefix):]
    if not name or "/" in name:
        return None
    if name.startswith("live_") and name.endswith((".sqlite3.gz", ".manifest.json")):
        return name
    return None


def list_s3_archive_names(opener=urlopen) -> set:
    """Archive file names already stored under the configured S3 prefix."""
    return _s3_names(_s3_config(), opener)


def _s3_names(cfg: _S3, opener) -> set:
    names = set()
    token = None
    for _ in range(32):
        items = [("list-type", "2"), ("max-keys", "1000"), ("prefix", cfg.prefix)]
        if token:
            items.append(("continuation-token", token))
        body = _s3_open(
            cfg, "GET", canonical_uri=_list_uri(cfg), query=_canonical_query(items),
            opener=opener, timeout=60, error="day export listing was refused",
        )
        keys, truncated, token = _parse_list(body)
        for key in keys:
            name = _object_name(cfg.prefix, key)
            if name:
                names.add(name)
        if not truncated:
            return names
        if not token:
            raise RuntimeError("day export listing was refused")
    raise RuntimeError("day export listing was refused")


def _s3_put(cfg: _S3, name, path, mime, digest, opener):
    _s3_open(
        cfg, "PUT", canonical_uri=_object_uri(cfg, cfg.prefix + name),
        content_type=mime, body_path=path, payload_hash=digest,
        opener=opener, timeout=600, error="day export upload was refused",
    )


def _export_target(value=None) -> int:
    if value is None:
        value = os.getenv("LOTTO_DAY_EXPORT_TARGET", "0")
    if isinstance(value, bool):
        raise ValueError("day_export_target must be 0 or 1")
    if isinstance(value, int):
        target = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        target = int(value.strip())
    else:
        raise ValueError("day_export_target must be 0 or 1")
    if type(target) is not int or target not in (0, 1):
        raise ValueError("day_export_target must be 0 or 1")
    return target


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


def _export_one(database, day, names, upload, work_dir: Path):
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
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True)
        manifest_path.write_text(manifest_text)
        upload(gz_name, gz_path, "application/gzip", digest)
        upload(manifest_name, manifest_path, "application/json", sha256(manifest_text.encode()).hexdigest())
        names.add(gz_name)
        names.add(manifest_name)
        LOG.info("Day export uploaded: %s", day)
    finally:
        _remove(db_path)
        gz_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)


def run_day_exports(database, dates, completed, *, opener=None, work_dir=None, target=None) -> int:
    """Upload each missing session date. Returns how many dates still need a retry."""
    opener = opener or urlopen
    root = Path(work_dir) if work_dir is not None else Path("/tmp")
    source = Path(database).resolve()
    if source.parent == root.resolve():
        raise RuntimeError("day export refuses to write beside the source database")
    root.mkdir(parents=True, exist_ok=True)
    chosen = _export_target(target)
    if chosen == 1:
        cfg = _s3_config()
        names = _s3_names(cfg, opener)
        def upload(name, path, mime, digest, cfg=cfg):
            _s3_put(cfg, name, path, mime, digest, opener)
    else:
        token = fetch_access_token(_credentials(), opener)
        folder = _folder_id()
        names = list_archive_names(token, folder, opener)
        def upload(name, path, mime, digest, token=token, folder=folder):
            upload_file(token, folder, name, path, mime, opener)
    failures = 0
    for day in dates:
        if not _DAY.fullmatch(day):
            failures += 1
            LOG.warning("Day export failed for %s (%s)", day, "ValueError")
            continue
        try:
            _export_one(source, day, names, upload, root)
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
        export = runner
        if runner is run_day_exports:
            chosen = settings.day_export_target
            def export(database, pending, completed, _chosen=chosen):
                return run_day_exports(database, pending, completed, target=_chosen)
        return _start(database, dates, export, retry_seconds)
    except Exception as exc:
        LOG.warning("Day export unavailable (%s)", type(exc).__name__)
        return None
