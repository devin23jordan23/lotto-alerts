import gzip
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timezone
from hashlib import md5, sha256
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from lotto.config import Settings, recorded_settings
from lotto.main import env_settings
from lotto.space import (
    candidate_files,
    clear_space_state,
    maybe_space_cleanup,
    protected_days,
    run_space_cleanup,
)

ENV = {
    "LOTTO_S3_ENDPOINT": "https://t3.storageapi.dev",
    "LOTTO_S3_BUCKET": "lotto-bucket-abc123",
    "LOTTO_S3_ACCESS_KEY_ID": "AKIALOTTOEXPORTKEY",
    "LOTTO_S3_SECRET_ACCESS_KEY": "SUPER_SECRET_KEY_MATERIAL",
    "LOTTO_S3_PREFIX": "day-export/",
}
TODAY = date(2026, 11, 18)  # Wednesday: keeps 11-16 and 11-17


class Resp:
    def __init__(self, status=200, headers=None, body=b""):
        self.status, self.headers, self._body = status, headers or {}, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeS3:
    def __init__(self, corrupt=False, fail_put=()):
        self.objects, self.calls, self.corrupt, self.fail_put = {}, [], corrupt, set(fail_put)

    def __call__(self, request, timeout=None):
        key = request.full_url.split(".t3.storageapi.dev/", 1)[1]
        method = request.get_method()
        self.calls.append((method, key))
        assert "SUPER_SECRET" not in str(request.headers)
        if method == "PUT":
            if any(name in key for name in self.fail_put):
                raise HTTPError(request.full_url, 403, "denied", {}, None)
            body = request.data.read()
            assert sha256(body).hexdigest() == request.get_header("X-amz-content-sha256")
            self.objects[key] = body + (b"x" if self.corrupt else b"")
            return Resp()
        if method == "HEAD":
            if key not in self.objects:
                raise HTTPError(request.full_url, 404, "missing", {}, None)
            body = self.objects[key]
            return Resp(headers={"Content-Length": str(len(body)), "ETag": f'"{md5(body).hexdigest()}"'})
        raise AssertionError(method)


def build(root: Path):
    files = {}
    for day in ("2026-11-10", "2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18"):
        d = root / "nightly" / day / "option-trades"
        d.mkdir(parents=True)
        files[f"nightly/{day}/AAPL-bars.json.gz"] = gzip.compress(day.encode() * 50)
        files[f"nightly/{day}/report.json"] = b'{"day":"' + day.encode() + b'"}'
        files[f"nightly/{day}/option-trades/abc.json.gz"] = gzip.compress(b"t" + day.encode())
    (root / "forensics").mkdir()
    files["forensics/2026-11-12-NVDA.json.gz"] = b"f1" * 100
    files["forensics/2026-11-17-NVDA.json.gz"] = b"f2" * 100
    files["live.sqlite3"] = b"db"
    files["schwab_tokens.json"] = b"{}"
    files["nightly/2026-11-10/report.tmp"] = b"partial"
    for rel, body in files.items():
        (root / rel).write_bytes(body)
    return files


class SpaceCleanupTests(unittest.TestCase):
    def setUp(self):
        clear_space_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.files = build(self.root)

    def tearDown(self):
        clear_space_state()
        self.tmp.cleanup()

    def test_protected_days_keep_two_weekdays(self):
        self.assertEqual(protected_days(TODAY), {"2026-11-18", "2026-11-17", "2026-11-16"})
        self.assertEqual(protected_days(date(2026, 11, 16)), {"2026-11-16", "2026-11-13", "2026-11-12"})

    def test_candidates_exclude_recent_days_db_tokens_and_temp(self):
        rels = {rel for _, rel in candidate_files(self.root, TODAY)}
        self.assertEqual(rels, {
            "nightly/2026-11-10/AAPL-bars.json.gz", "nightly/2026-11-10/report.json",
            "nightly/2026-11-10/option-trades/abc.json.gz",
            "nightly/2026-11-13/AAPL-bars.json.gz", "nightly/2026-11-13/report.json",
            "nightly/2026-11-13/option-trades/abc.json.gz",
            "forensics/2026-11-12-NVDA.json.gz",
        })

    def test_uploads_verifies_then_deletes_and_logs_bytes(self):
        s3 = FakeS3()
        with patch.dict(os.environ, ENV), self.assertLogs("lotto.space", "INFO") as logs:
            result = run_space_cleanup(self.root, TODAY, opener=s3)
        self.assertEqual((result["deleted"], result["failed"]), (7, 0))
        expected = sum(len(b) for r, b in self.files.items() if r.startswith(("nightly/2026-11-10/",
                       "nightly/2026-11-13/", "forensics/2026-11-12")) and not r.endswith(".tmp"))
        self.assertEqual(result["bytes_freed"], expected)
        self.assertIn("bytes freed %d" % expected, "\n".join(logs.output))
        for rel, body in self.files.items():
            if rel.startswith(("nightly/2026-11-13/", "forensics/2026-11-12")) or (
                    rel.startswith("nightly/2026-11-10/") and not rel.endswith(".tmp")):
                self.assertFalse((self.root / rel).exists(), rel)
                self.assertEqual(s3.objects["day-export/volume-archive/" + rel], body)
            else:
                self.assertEqual((self.root / rel).read_bytes(), body, rel)
        self.assertFalse((self.root / "nightly/2026-11-13").exists())
        self.assertTrue((self.root / "nightly/2026-11-10/report.tmp").exists())
        self.assertFalse(any("live.sqlite3" in k or "schwab" in k for k in s3.objects))
        # Second run is a no-op for already-deleted files.
        with patch.dict(os.environ, ENV):
            again = run_space_cleanup(self.root, TODAY, opener=s3)
        self.assertEqual(again["candidates"], 0)

    def test_mismatch_keeps_local_file(self):
        with patch.dict(os.environ, ENV), self.assertLogs("lotto.space", "INFO"):
            result = run_space_cleanup(self.root, TODAY, opener=FakeS3(corrupt=True))
        self.assertEqual((result["deleted"], result["bytes_freed"]), (0, 0))
        self.assertEqual(result["failed"], 7)
        for rel, body in self.files.items():
            self.assertEqual((self.root / rel).read_bytes(), body)

    def test_one_failed_upload_keeps_that_file_only(self):
        with patch.dict(os.environ, ENV), self.assertLogs("lotto.space", "INFO"):
            result = run_space_cleanup(self.root, TODAY, opener=FakeS3(fail_put=("2026-11-12-NVDA",)))
        self.assertEqual((result["deleted"], result["failed"]), (6, 1))
        self.assertTrue((self.root / "forensics/2026-11-12-NVDA.json.gz").exists())

    def test_existing_identical_object_is_not_reuploaded(self):
        s3 = FakeS3()
        rel = "forensics/2026-11-12-NVDA.json.gz"
        s3.objects["day-export/volume-archive/" + rel] = self.files[rel]
        with patch.dict(os.environ, ENV), self.assertLogs("lotto.space", "INFO"):
            result = run_space_cleanup(self.root, TODAY, opener=s3)
        self.assertEqual(result["already_archived"], 1)
        self.assertNotIn(("PUT", "day-export/volume-archive/" + rel), s3.calls)

    def test_flag_default_zero_does_nothing(self):
        self.assertEqual(Settings().space_cleanup, 0)
        called = []
        self.assertIsNone(maybe_space_cleanup(Settings(), self.root, datetime(2026, 11, 18, 23, tzinfo=timezone.utc),
                                              runner=lambda *a: called.append(a)))
        self.assertEqual(called, [])
        self.assertNotIn("space_cleanup", recorded_settings(Settings()))
        with self.assertRaises(ValueError):
            Settings(space_cleanup=2)
        with patch.dict(os.environ, {"LOTTO_SPACE_CLEANUP": "1"}):
            self.assertEqual(env_settings().space_cleanup, 1)

    def test_scheduler_startup_once_then_evening_and_never_market_hours(self):
        on = Settings(space_cleanup=1)
        calls = []
        runner = lambda d, today: calls.append(today) or {"failed": 0}
        market = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)  # 10:00 ET
        self.assertIsNone(maybe_space_cleanup(on, self.root, market, runner=runner))
        startup = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)  # 17:00 ET -> startup + evening
        maybe_space_cleanup(on, self.root, startup, runner=runner).join()
        self.assertIsNone(maybe_space_cleanup(on, self.root, startup, runner=runner))
        morning = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)  # 07:00 ET next day
        self.assertIsNone(maybe_space_cleanup(on, self.root, morning, runner=runner))
        evening = datetime(2026, 11, 19, 21, 45, tzinfo=timezone.utc)  # 16:45 ET
        maybe_space_cleanup(on, self.root, evening, runner=runner).join()
        self.assertEqual(calls, [date(2026, 11, 18), date(2026, 11, 19)])

    def test_cleanup_never_opens_the_database(self):
        with patch.object(sqlite3, "connect", side_effect=AssertionError("db touched")), \
                patch.dict(os.environ, ENV), self.assertLogs("lotto.space", "INFO"):
            run_space_cleanup(self.root, TODAY, opener=FakeS3())


if __name__ == "__main__":
    unittest.main()
