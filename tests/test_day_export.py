import base64
import gzip
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlparse

from lotto.config import Settings, recorded_settings
from lotto.day_export import (
    clear_day_export_state,
    et_day,
    fetch_access_token,
    gzip_sha256,
    list_archive_names,
    list_s3_archive_names,
    maybe_day_export,
    open_readonly,
    run_day_exports,
    sigv4_authorization,
    slice_session,
    weekday_window,
)
from lotto.demo import demo_frames
from lotto.discord import deliver_pending
from lotto.engine import Engine
from lotto.main import env_settings
from lotto.models import ET, Snapshot
from lotto.store import Store, read_snapshot


SECRET = "SUPER_SECRET_KEY_MATERIAL"
ACCESS = "AKIALOTTOEXPORTKEY"
S3_ENDPOINT = "https://t3.storageapi.dev"
S3_BUCKET = "lotto-bucket-abc123"
S3_HOST = "lotto-bucket-abc123.t3.storageapi.dev"
DAY = "2026-11-02"
OTHER = "2026-11-03"


class Response:
    def __init__(self, body, headers=None):
        self._body = body if isinstance(body, bytes) else body.encode()
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def schema_sql(conn):
    return sorted(row[0] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"))


def column(conn, table, name):
    return sorted(row[0] for row in conn.execute(f"SELECT {name} FROM {table}"))


class DaySliceTests(unittest.TestCase):
    def setUp(self):
        clear_day_export_state()
        self.addCleanup(clear_day_export_state)

    def test_disabled_export_does_not_change_alerts_or_posting(self):
        def collect(settings, hook):
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            path = str(Path(directory.name) / "live.sqlite3")
            store = Store(path)
            self.addCleanup(store.db.close)
            engine = Engine(store, settings, dry_run=False)
            alerts = []
            for frame in demo_frames():
                alerts.extend(engine.process(frame))
            before = Path(path).read_bytes()
            if hook:
                called = []
                def bomb(*_args, **_kwargs):
                    called.append(True)
                    raise AssertionError("export ran")
                scheduled = maybe_day_export(
                    settings, path, datetime(2026, 9, 21, 16, 30, tzinfo=ET), runner=bomb)
                self.assertIsNone(scheduled)
                self.assertEqual(called, [])
                self.assertFalse(any(thread.name == "lotto-day-export" for thread in threading.enumerate()))
            self.assertEqual(Path(path).read_bytes(), before)
            alert_at = datetime.fromisoformat(store.db.execute("SELECT at FROM alerts").fetchone()[0])
            with patch("lotto.discord.datetime") as clock, patch("lotto.discord.urlopen") as send:
                clock.now.return_value = alert_at
                clock.fromisoformat.side_effect = datetime.fromisoformat
                deliver_pending(store, "https://discord.com/api/webhooks/test/test")
            posted = [call.args[0].data for call in send.call_args_list]
            rows = [tuple(row) for row in store.db.execute(
                "SELECT id, day, symbol, contract, at, payload, delivery, phase, parent_id, thesis_id "
                "FROM alerts ORDER BY id")]
            saved = [tuple(row) for row in store.db.execute(
                "SELECT day, symbol, at, contract, qualifying, score, features "
                "FROM candidate_evaluations ORDER BY symbol, at, contract")]
            return alerts, rows, saved, posted

        with patch.dict("os.environ", {"LOTTO_DAY_EXPORT": "0"}, clear=False):
            hooked = collect(env_settings(), hook=True)
        plain = collect(Settings(), hook=False)
        enabled = collect(Settings(day_export=1), hook=False)
        self.assertEqual(hooked[0][0]["phase"], "ACTIVE")
        self.assertEqual(hooked, plain)
        self.assertEqual(enabled[0], plain[0])
        self.assertEqual(enabled[1], plain[1])
        self.assertEqual(enabled[2], plain[2])
        self.assertEqual(enabled[3], plain[3])
        self.assertEqual(len(plain[3]), 1)
        self.assertEqual(plain[1][0][6], "sent")
        stored = json.loads(plain[2][0][6])["settings"]
        self.assertNotIn("day_export", stored)
        self.assertNotIn("day_export_target", stored)
        self.assertEqual(stored, recorded_settings(Settings(day_export=1, day_export_target=1)))

    def test_env_flag_stays_an_int_zero(self):
        with patch.dict("os.environ", {
            "LOTTO_DAY_EXPORT": "0",
            "LOTTO_DAY_EXPORT_HOUR": "16",
            "LOTTO_DAY_EXPORT_MINUTE": "20",
            "LOTTO_DAY_EXPORT_BACKFILL_DAYS": "5",
            "LOTTO_DAY_EXPORT_TARGET": "0",
        }):
            settings = env_settings()
        self.assertEqual(settings.day_export, 0)
        self.assertIs(type(settings.day_export), int)
        self.assertEqual(settings.day_export_target, 0)
        self.assertIs(type(settings.day_export_target), int)
        self.assertEqual((settings.day_export_hour, settings.day_export_minute, settings.day_export_backfill_days), (16, 20, 5))
        with patch.dict("os.environ", {"LOTTO_DAY_EXPORT": "1", "LOTTO_DAY_EXPORT_TARGET": "1"}):
            enabled = env_settings()
        self.assertEqual(enabled.day_export, 1)
        self.assertEqual(enabled.day_export_target, 1)
        self.assertIs(type(enabled.day_export_target), int)
        for bad in ({"LOTTO_DAY_EXPORT": "2"}, {"LOTTO_DAY_EXPORT_HOUR": "24"},
                    {"LOTTO_DAY_EXPORT_MINUTE": "60"}, {"LOTTO_DAY_EXPORT_BACKFILL_DAYS": "-1"},
                    {"LOTTO_DAY_EXPORT_TARGET": "2"}, {"LOTTO_DAY_EXPORT_TARGET": "true"}):
            with patch.dict("os.environ", bad), self.assertRaises(ValueError):
                env_settings()
        with self.assertRaises(ValueError):
            Settings(day_export=True)
        with self.assertRaises(ValueError):
            Settings(day_export_target=True)

    def test_window_includes_recent_weekdays_only_after_the_close(self):
        today = date(2026, 11, 4)
        self.assertEqual(today.weekday(), 2)
        self.assertEqual(weekday_window(today, 5), ["2026-11-02", "2026-11-03", "2026-11-04"])
        self.assertEqual(weekday_window(today, 0), ["2026-11-04"])
        self.assertEqual(weekday_window(date(2026, 11, 2), 5), ["2026-10-29", "2026-10-30", "2026-11-02"])
        closed = datetime(2026, 11, 4, 21, 20, tzinfo=timezone.utc)
        self.assertEqual(closed.astimezone(ET).strftime("%H:%M"), "16:20")
        seen = []
        def runner(database, dates, completed):
            seen.append(list(dates))
            completed.update(dates)
            return 0
        thread = maybe_day_export(Settings(day_export=1, day_export_backfill_days=5),
                                   "/no/such/live.sqlite3", closed, runner=runner)
        thread.join(5)
        self.assertEqual(seen, [["2026-11-02", "2026-11-03", "2026-11-04"]])
        self.assertIsNone(maybe_day_export(Settings(day_export=1), "/no/such/live.sqlite3", closed, runner=runner))
        self.assertEqual(len(seen), 1)
        early = closed - timedelta(minutes=1)
        self.assertIsNone(maybe_day_export(Settings(day_export=1), "/no/such/live.sqlite3", early, runner=runner))
        weekend = datetime(2026, 11, 7, 18, tzinfo=ET)
        self.assertGreaterEqual(weekend.weekday(), 5)
        self.assertIsNone(maybe_day_export(Settings(day_export=1), "/no/such/live.sqlite3", weekend, runner=runner))
        self.assertEqual(len(seen), 1)

    def test_failures_are_swallowed_and_do_not_log_secrets(self):
        def runner(*_args, **_kwargs):
            raise RuntimeError(SECRET)
        when = datetime(2026, 11, 4, 16, 20, tzinfo=ET)
        with self.assertLogs("lotto.day_export", level="WARNING") as logs:
            thread = maybe_day_export(Settings(day_export=1), "/no/such/live.sqlite3", when,
                                       runner=runner, retry_seconds=3600)
            self.assertIsNotNone(thread)
            thread.join(5)
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("RuntimeError", logs.output[0])
        self.assertIsNone(maybe_day_export(Settings(day_export=1), "/no/such/live.sqlite3", when,
                                            runner=runner, retry_seconds=3600))

    def build_fixture(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(str(path))
        late_utc = datetime(2026, 11, 3, 2, 30, tzinfo=timezone.utc)
        early_utc = datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc)
        local = datetime(2026, 11, 2, 10, tzinfo=ET)
        nxt = datetime(2026, 11, 3, 10, tzinfo=ET)
        self.assertEqual(et_day(late_utc.isoformat()), DAY)
        self.assertEqual(et_day(early_utc.isoformat()), "2026-11-01")
        self.assertEqual(late_utc.isoformat()[:10], OTHER)
        def snap(symbol, at):
            return Snapshot(symbol, at, 100, at, 99, (), ())
        for symbol, at in (("IN", late_utc), ("OUT", early_utc), ("LOCAL", local), ("NEXT", nxt)):
            store.record(snap(symbol, at))
        with store.db:
            rows = [
                ("NAIVE", "2026-11-02T15:00:00"),
                ("IN", late_utc.isoformat()),
                ("OUT", early_utc.isoformat()),
                ("LOCAL", local.isoformat()),
                ("NEXT", nxt.isoformat()),
            ]
            rows.extend((f"B{i}", local.isoformat()) for i in range(6))
            store.db.executemany("INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                                 [(symbol, at, "X", "r", 1, "{}") for symbol, at in rows])
            store.db.executemany(
                "INSERT INTO alerts (id, day, symbol, side, contract, at, entry_ask, delivery) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [("keep", DAY, "SPY", "CALL", "C", nxt.isoformat(), 1, "sent"),
                 ("drop", OTHER, "QQQ", "PUT", "P", local.isoformat(), 1, "sent")])
            store.db.executemany("INSERT INTO milestones VALUES (?,?,?)",
                                 [("keep", 100, local.isoformat()), ("drop", 50, nxt.isoformat())])
            store.db.executemany("INSERT INTO discovery VALUES (?,?,?,?,?)",
                                 [(DAY, "SPY", local.isoformat(), 1, "{}"),
                                  (OTHER, "QQQ", nxt.isoformat(), 0, "{}")])
            store.db.executemany("INSERT INTO nightly_runs VALUES (?,?,?)",
                                 [(DAY, nxt.isoformat(), "report"), (OTHER, local.isoformat(), "other")])
            store.db.executemany(
                "INSERT INTO chain_coverage (day,symbol,at,promoted,contracts,spot,flow_side,flow_cluster) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [(DAY, "SPY", local.isoformat(), 1, 4, 100, "CALL", 2),
                 (OTHER, "QQQ", nxt.isoformat(), 0, 1, 90, "PUT", 1)])
            store.db.executemany("INSERT INTO health_incidents (opened_at, reason) VALUES (?,?)",
                                 [(local.isoformat(), "keep"), (nxt.isoformat(), "drop")])
            store.db.executemany("INSERT INTO candidate_evaluations VALUES (?,?,?,?,?,?,?,?)",
                                 [(DAY, "SPY", local.isoformat(), "C", "strategy", 1, 80, "{}"),
                                  (OTHER, "QQQ", nxt.isoformat(), "P", "strategy", 0, 10, "{}")])
            store.db.executemany("INSERT INTO user_feedback VALUES (?,?,?,?)",
                                 [("keep", "worked", "held", local.isoformat()),
                                  ("drop", "failed", "fade", nxt.isoformat())])
            store.db.execute("CREATE TABLE reference_symbols (symbol TEXT PRIMARY KEY)")
            store.db.execute("CREATE INDEX reference_symbols_symbol ON reference_symbols(symbol)")
            store.db.executemany("INSERT INTO reference_symbols VALUES (?)",
                                 [("SPY",), ("QQQ",), ("IWM",), ("DIA",), ("GLD",)])
        store.db.close()

    def test_slice_keeps_schema_and_only_that_et_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "day data" / "live.sqlite3"
            dest = root / "out" / "slice.sqlite3"
            self.build_fixture(source)
            original = source.read_bytes()
            sidecar = [source.parent / name for name in ("live.sqlite3-wal", "live.sqlite3-journal", "live.sqlite3-shm")]
            counts = slice_session(source, dest, DAY, copy_batch=2)
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(all(not path.exists() for path in sidecar))
            readonly = open_readonly(source)
            with self.assertRaises(sqlite3.OperationalError):
                readonly.execute("CREATE TABLE refused(x)")
            readonly.close()
            self.assertEqual(source.read_bytes(), original)
            sliced = sqlite3.connect(dest)
            self.addCleanup(sliced.close)
            self.assertEqual(sliced.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(schema_sql(sqlite3.connect(source)), schema_sql(sliced))
            self.assertEqual(column(sliced, "snapshots", "symbol"), ["IN", "LOCAL"])
            self.assertEqual(read_snapshot(sliced.execute(
                "SELECT payload FROM snapshots WHERE symbol='IN'").fetchone()[0]).symbol, "IN")
            self.assertEqual(column(sliced, "decisions", "symbol"),
                             ["B0", "B1", "B2", "B3", "B4", "B5", "IN", "LOCAL"])
            self.assertEqual(column(sliced, "alerts", "id"), ["keep"])
            self.assertEqual(column(sliced, "milestones", "alert_id"), ["keep"])
            self.assertEqual(column(sliced, "discovery", "symbol"), ["SPY"])
            self.assertEqual(column(sliced, "nightly_runs", "day"), [DAY])
            self.assertEqual(column(sliced, "chain_coverage", "symbol"), ["SPY"])
            self.assertEqual(column(sliced, "health_incidents", "reason"), ["keep"])
            self.assertEqual(column(sliced, "candidate_evaluations", "symbol"), ["SPY"])
            self.assertEqual(column(sliced, "user_feedback", "alert_id"), ["keep"])
            self.assertEqual(column(sliced, "reference_symbols", "symbol"),
                             ["DIA", "GLD", "IWM", "QQQ", "SPY"])
            self.assertEqual(counts["snapshots"], 2)
            self.assertEqual(counts["decisions"], 8)
            self.assertEqual(counts["reference_symbols"], 5)
            self.assertEqual(counts["alerts"], 1)
            first = root / "a.gz"
            second = root / "b.gz"
            self.assertEqual(gzip_sha256(dest, first), gzip_sha256(dest, second))
            self.assertEqual(sha256(first.read_bytes()).hexdigest(), gzip_sha256(dest, second))
            with gzip.open(first, "rb") as packed:
                restored = root / "restored.sqlite3"
                restored.write_bytes(packed.read())
            check = sqlite3.connect(restored)
            self.addCleanup(check.close)
            self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(column(check, "snapshots", "symbol"), ["IN", "LOCAL"])

    def test_drive_upload_is_idempotent_and_cleans_temp_files(self):
        pem = subprocess.check_output(
            ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
            text=True, stderr=subprocess.DEVNULL)
        drive = Drive()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data" / "live.sqlite3"
            work = root / "work"
            self.build_fixture(source)
            original = source.read_bytes()
            env = {
                "GDRIVE_FOLDER_ID": "folder-123",
                "GDRIVE_SA_JSON": json.dumps({
                    "client_email": "exporter@example.iam.gserviceaccount.com",
                    "private_key": pem,
                    "marker": SECRET,
                }),
                "RAILWAY_GIT_COMMIT_SHA": "abc123",
                "LOTTO_DAY_EXPORT_TARGET": "0",
                "LOTTO_S3_ENDPOINT": S3_ENDPOINT,
                "LOTTO_S3_BUCKET": S3_BUCKET,
                "LOTTO_S3_ACCESS_KEY_ID": ACCESS,
                "LOTTO_S3_SECRET_ACCESS_KEY": SECRET,
            }
            with patch.dict("os.environ", env), self.assertLogs("lotto.day_export", level="INFO") as logs:
                failures = run_day_exports(source, [DAY, OTHER], completed := set(), opener=drive, work_dir=work)
            self.assertEqual(failures, 0)
            self.assertEqual(completed, {DAY, OTHER})
            self.assertNotIn(SECRET, "\n".join(logs.output))
            self.assertNotIn("BEGIN PRIVATE KEY", "\n".join(logs.output))
            self.assertNotIn(pem, "\n".join(drive.token_bodies))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(work.iterdir()), [])
            self.assertEqual(sorted(path.name for path in source.parent.iterdir()), ["live.sqlite3"])
            self.assertEqual(drive.puts, 4)
            for day, symbol in ((DAY, "LOCAL"), (OTHER, "NEXT")):
                archive = drive.files[f"live_{day}.sqlite3.gz"]
                manifest = json.loads(drive.files[f"live_{day}.manifest.json"])
                self.assertEqual(manifest["date"], day)
                self.assertEqual(manifest["sha256"], sha256(archive).hexdigest())
                self.assertEqual(manifest["commit"], "abc123")
                self.assertIn("source_mtime", manifest)
                self.assertEqual(manifest["row_counts"]["reference_symbols"], 5)
                with gzip.open(io.BytesIO(archive), "rb") as packed:
                    restored = root / f"{day}.sqlite3"
                    restored.write_bytes(packed.read())
                conn = sqlite3.connect(restored)
                self.addCleanup(conn.close)
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(schema_sql(conn), schema_sql(sqlite3.connect(source)))
                symbols = column(conn, "snapshots", "symbol")
                self.assertIn(symbol, symbols)
                self.assertNotIn("OUT", symbols)
            self.assertEqual(column(sqlite3.connect(root / f"{DAY}.sqlite3"), "snapshots", "symbol"), ["IN", "LOCAL"])
            self.assertEqual(column(sqlite3.connect(root / f"{OTHER}.sqlite3"), "snapshots", "symbol"), ["NEXT"])
            puts = drive.puts
            with patch.dict("os.environ", env):
                again = set()
                failures = run_day_exports(root / "data" / "missing.sqlite3", [DAY, OTHER], again,
                                            opener=drive, work_dir=work)
            self.assertEqual(failures, 0)
            self.assertEqual(again, {DAY, OTHER})
            self.assertEqual(drive.puts, puts)
            self.assertEqual(list(work.iterdir()), [])

    def test_export_refuses_to_write_beside_the_source_database(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "live.sqlite3"
            source.write_bytes(b"")
            called = []
            with self.assertRaises(RuntimeError):
                run_day_exports(source, [DAY], set(), opener=lambda *a, **k: called.append(1), work_dir=source.parent)
            self.assertEqual(called, [])
            self.assertEqual(list(Path(directory).iterdir()), [source])

    def test_service_account_jwt_is_rs256(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "sa.pem"
            subprocess.check_call(
                ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pem = key.read_text()
            seen = []
            def opener(request, timeout=None):
                seen.append(request)
                return Response(b'{"access_token":"tok"}')
            token = fetch_access_token({"client_email": "exporter@example.iam.gserviceaccount.com", "private_key": pem}, opener)
            self.assertEqual(token, "tok")
            assertion = parse_qs(seen[0].data.decode())["assertion"][0]
            self.assertNotIn("BEGIN PRIVATE KEY", seen[0].data.decode())
            signing_input, signature = assertion.rsplit(".", 1)
            signature = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
            header, claims = (json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                              for part in signing_input.split("."))
            self.assertEqual(header, {"alg": "RS256", "typ": "JWT"})
            self.assertEqual(claims["iss"], "exporter@example.iam.gserviceaccount.com")
            self.assertEqual(claims["scope"], "https://www.googleapis.com/auth/drive")
            self.assertEqual(claims["aud"], "https://oauth2.googleapis.com/token")
            message = Path(directory) / "msg"
            signed = Path(directory) / "sig"
            public = Path(directory) / "pub.pem"
            message.write_bytes(signing_input.encode())
            signed.write_bytes(signature)
            subprocess.check_call(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public)],
                                  stdout=subprocess.DEVNULL)
            subprocess.check_call(
                ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(signed), str(message)],
                stdout=subprocess.DEVNULL)

    def test_archive_listing_pages_and_ignores_other_names(self):
        pages = [
            {"files": [{"name": "live_2026-11-02.sqlite3.gz"}, {"name": "notes.txt"}], "nextPageToken": "next"},
            {"files": [{"name": "live_2026-11-02.manifest.json"}, {"name": "live_other.txt"}]},
        ]
        def opener(request, timeout=None):
            payload = pages[1] if "pageToken=next" in request.full_url else pages[0]
            return Response(json.dumps(payload))
        self.assertEqual(
            list_archive_names("token", "folder", opener),
            {"live_2026-11-02.sqlite3.gz", "live_2026-11-02.manifest.json"})

    def test_sigv4_matches_the_aws_get_object_example(self):
        # AWS Signature Version 4 GET Object example:
        # examplebucket/test.txt at 20130524T000000Z, us-east-1.
        payload = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        headers = {
            "host": "examplebucket.s3.amazonaws.com",
            "range": "bytes=0-9",
            "x-amz-content-sha256": payload,
            "x-amz-date": "20130524T000000Z",
        }
        authorization = sigv4_authorization(
            "GET", "/test.txt", "", headers, payload,
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            region="us-east-1",
        )
        self.assertEqual(authorization, (
            "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
            "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
        ))
        self.assertNotIn("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", authorization)

    def test_s3_list_pages_in_path_style_and_ignores_other_keys(self):
        pages = [
            """<?xml version="1.0"?>
            <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
              <IsTruncated>true</IsTruncated>
              <NextContinuationToken>next/page</NextContinuationToken>
              <Contents><Key>day-export/live_2026-11-02.sqlite3.gz</Key></Contents>
              <Contents><Key>notes.txt</Key></Contents>
            </ListBucketResult>""",
            """<ListBucketResult>
              <IsTruncated>false</IsTruncated>
              <Contents><Key>day-export/live_2026-11-02.manifest.json</Key></Contents>
              <Contents><Key>day-export/nested/live_other.sqlite3.gz</Key></Contents>
            </ListBucketResult>""",
        ]
        seen = []

        def opener(request, timeout=None):
            seen.append(request)
            assert_signed(self, request)
            return Response(pages[len(seen) - 1])

        env = {
            "LOTTO_S3_ENDPOINT": S3_ENDPOINT,
            "LOTTO_S3_BUCKET": S3_BUCKET,
            "LOTTO_S3_ACCESS_KEY_ID": ACCESS,
            "LOTTO_S3_SECRET_ACCESS_KEY": SECRET,
            "LOTTO_S3_URL_STYLE": "path",
        }
        with patch.dict("os.environ", env):
            os.environ.pop("LOTTO_S3_PREFIX", None)
            os.environ.pop("LOTTO_S3_REGION", None)
            names = list_s3_archive_names(opener)
        self.assertEqual(names, {"live_2026-11-02.sqlite3.gz", "live_2026-11-02.manifest.json"})
        self.assertEqual(len(seen), 2)
        first, second = (request.full_url for request in seen)
        self.assertTrue(first.startswith(f"{S3_ENDPOINT}/{S3_BUCKET}?"))
        self.assertIn("list-type=2&max-keys=1000&prefix=day-export%2F", first)
        self.assertIn(
            "continuation-token=next%2Fpage&list-type=2&max-keys=1000&prefix=day-export%2F",
            second,
        )
        self.assertEqual(request_headers(seen[0])["host"], "t3.storageapi.dev")
        self.assertIn("/auto/s3/aws4_request", request_headers(seen[0])["authorization"])
        self.assertNotIn(SECRET, "\n".join(request.full_url for request in seen))

    def test_s3_upload_is_idempotent_and_cleans_temp_files(self):
        bucket = Bucket()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data" / "live.sqlite3"
            work = root / "work"
            self.build_fixture(source)
            original = source.read_bytes()
            env = {
                "LOTTO_DAY_EXPORT_TARGET": "1",
                "LOTTO_S3_ENDPOINT": S3_ENDPOINT,
                "LOTTO_S3_BUCKET": S3_BUCKET,
                "LOTTO_S3_ACCESS_KEY_ID": ACCESS,
                "LOTTO_S3_SECRET_ACCESS_KEY": SECRET,
                "LOTTO_S3_REGION": "auto",
                "LOTTO_S3_PREFIX": "day-export/",
                "LOTTO_S3_URL_STYLE": "virtual",
                "RAILWAY_GIT_COMMIT_SHA": "abc123",
            }
            with patch.dict("os.environ", env), self.assertLogs("lotto.day_export", level="INFO") as logs:
                failures = run_day_exports(source, [DAY, OTHER], completed := set(), opener=bucket, work_dir=work)
            text = "\n".join(logs.output)
            self.assertEqual(failures, 0)
            self.assertEqual(completed, {DAY, OTHER})
            self.assertNotIn(SECRET, text)
            self.assertNotIn(ACCESS, text)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(work.iterdir()), [])
            self.assertEqual(sorted(path.name for path in source.parent.iterdir()), ["live.sqlite3"])
            self.assertEqual(bucket.puts, 4)
            self.assertTrue(all(S3_HOST in request.full_url for request in bucket.requests))
            self.assertFalse(any("googleapis" in request.full_url for request in bucket.requests))
            for request in bucket.requests:
                assert_signed(self, request)
                headers = request_headers(request)
                self.assertEqual(headers["host"], S3_HOST)
                if request.get_method() == "PUT":
                    key = unquote(urlparse(request.full_url).path.lstrip("/"))
                    self.assertEqual(headers["x-amz-content-sha256"], sha256(bucket.objects[key]).hexdigest())
                    self.assertTrue(key.startswith("day-export/live_"))
                else:
                    self.assertEqual(headers["x-amz-content-sha256"], sha256(b"").hexdigest())
                    self.assertIn("prefix=day-export%2F", request.full_url)
            for day, symbol in ((DAY, "LOCAL"), (OTHER, "NEXT")):
                archive = bucket.objects[f"day-export/live_{day}.sqlite3.gz"]
                manifest = json.loads(bucket.objects[f"day-export/live_{day}.manifest.json"])
                self.assertEqual(manifest["date"], day)
                self.assertEqual(manifest["sha256"], sha256(archive).hexdigest())
                self.assertEqual(manifest["commit"], "abc123")
                self.assertIn("source_mtime", manifest)
                self.assertEqual(manifest["row_counts"]["reference_symbols"], 5)
                with gzip.open(io.BytesIO(archive), "rb") as packed:
                    restored = root / f"{day}.sqlite3"
                    restored.write_bytes(packed.read())
                conn = sqlite3.connect(restored)
                self.addCleanup(conn.close)
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(schema_sql(conn), schema_sql(sqlite3.connect(source)))
                symbols = column(conn, "snapshots", "symbol")
                self.assertIn(symbol, symbols)
                self.assertNotIn("OUT", symbols)
            self.assertEqual(column(sqlite3.connect(root / f"{DAY}.sqlite3"), "snapshots", "symbol"), ["IN", "LOCAL"])
            self.assertEqual(column(sqlite3.connect(root / f"{OTHER}.sqlite3"), "snapshots", "symbol"), ["NEXT"])
            puts = bucket.puts
            with patch.dict("os.environ", env):
                again = set()
                failures = run_day_exports(root / "data" / "missing.sqlite3", [DAY, OTHER], again,
                                            opener=bucket, work_dir=work)
            self.assertEqual(failures, 0)
            self.assertEqual(again, {DAY, OTHER})
            self.assertEqual(bucket.puts, puts)
            self.assertEqual(list(work.iterdir()), [])

    def test_scheduler_s3_target_does_not_call_drive(self):
        bucket = Bucket()
        names = (
            "live_2026-11-04.sqlite3", "live_2026-11-04.sqlite3.gz", "live_2026-11-04.manifest.json",
            "live_2026-11-04.sqlite3-journal", "live_2026-11-04.sqlite3-wal", "live_2026-11-04.sqlite3-shm",
        )
        for name in names:
            Path("/tmp", name).unlink(missing_ok=True)
        self.addCleanup(lambda: [Path("/tmp", name).unlink(missing_ok=True) for name in names])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "nested" / "live.sqlite3"
            source.parent.mkdir()
            sqlite3.connect(source).close()
            env = {
                "LOTTO_S3_ENDPOINT": S3_ENDPOINT,
                "LOTTO_S3_BUCKET": S3_BUCKET,
                "LOTTO_S3_ACCESS_KEY_ID": ACCESS,
                "LOTTO_S3_SECRET_ACCESS_KEY": SECRET,
            }
            when = datetime(2026, 11, 4, 16, 20, tzinfo=ET)
            settings = Settings(day_export=1, day_export_target=1, day_export_backfill_days=1)
            with patch.dict("os.environ", env), patch("lotto.day_export.urlopen", bucket):
                os.environ.pop("LOTTO_DAY_EXPORT_TARGET", None)
                thread = maybe_day_export(settings, source, when)
                self.assertIsNotNone(thread)
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertIsNone(maybe_day_export(settings, source, when))
        self.assertEqual(bucket.puts, 2)
        self.assertTrue(any(request.get_method() == "GET" for request in bucket.requests))
        self.assertTrue(all(S3_HOST in request.full_url for request in bucket.requests))
        self.assertFalse(any("googleapis" in request.full_url for request in bucket.requests))
        for request in bucket.requests:
            assert_signed(self, request)
        self.assertTrue(all(not (Path("/tmp") / name).exists() for name in names))


def request_headers(request):
    return {key.lower(): value for key, value in request.header_items()}


def assert_signed(test, request, *, secret=SECRET, access=ACCESS, region="auto"):
    headers = request_headers(request)
    parsed = urlparse(request.full_url)
    signed_names = headers["authorization"].split("SignedHeaders=", 1)[1].split(",", 1)[0]
    used = {name: headers[name] for name in signed_names.split(";")}
    expected = sigv4_authorization(
        request.get_method(), parsed.path, parsed.query, used, headers["x-amz-content-sha256"],
        access_key=access, secret_key=secret, region=region,
    )
    test.assertEqual(headers["authorization"], expected)
    test.assertNotIn(secret, request.full_url)
    for value in headers.values():
        test.assertNotIn(secret, value)


class Drive:
    def __init__(self):
        self.files = {}
        self.pending = []
        self.puts = 0
        self.token_bodies = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        method = request.get_method()
        if url.startswith("https://oauth2.googleapis.com/token"):
            self.token_bodies.append(request.data.decode())
            return Response(b'{"access_token":"token-value"}')
        if url.startswith("https://www.googleapis.com/drive/v3/files?"):
            files = [{"name": name} for name in self.files]
            return Response(json.dumps({"files": files}))
        if "uploadType=resumable" in url and method == "POST":
            self.pending.append(json.loads(request.data.decode())["name"])
            return Response(b"", {"Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=1"})
        if method == "PUT":
            name = self.pending.pop(0)
            data = request.data.read() if hasattr(request.data, "read") else request.data
            self.files[name] = data
            self.puts += 1
            return Response(b"{}")
        raise AssertionError(url)


class Bucket:
    def __init__(self):
        self.objects = {}
        self.puts = 0
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        method = request.get_method()
        parsed = urlparse(request.full_url)
        if method == "GET":
            keys = "".join(f"<Contents><Key>{key}</Key></Contents>" for key in sorted(self.objects))
            body = (
                "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                "<ListBucketResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
                f"{keys}<IsTruncated>false</IsTruncated></ListBucketResult>"
            )
            return Response(body)
        if method == "PUT":
            data = request.data.read() if hasattr(request.data, "read") else request.data
            key = unquote(parsed.path.lstrip("/"))
            self.objects[key] = data
            self.puts += 1
            return Response(b"")
        raise AssertionError(request.full_url)


if __name__ == "__main__":
    unittest.main()
