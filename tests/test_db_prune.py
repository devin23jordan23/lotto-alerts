import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from lotto.config import Settings, recorded_settings
from lotto.day_export import et_day
from lotto.db_prune import run_db_prune
from lotto.main import env_settings
from lotto.space import clear_space_state, maybe_space_cleanup, protected_days
from lotto.store import Store

ENV = {
    "LOTTO_S3_ENDPOINT": "https://t3.storageapi.dev",
    "LOTTO_S3_BUCKET": "lotto-bucket-abc123",
    "LOTTO_S3_ACCESS_KEY_ID": "AKIALOTTOEXPORTKEY",
    "LOTTO_S3_SECRET_ACCESS_KEY": "SUPER_SECRET_KEY_MATERIAL",
    "LOTTO_S3_PREFIX": "day-export/",
}
TODAY = date(2026, 11, 18)  # Wednesday; keep 2 protects 11-16, 11-17, 11-18
OLD = "2026-11-10T15:00:00+00:00"
EDGE_15 = "2026-11-16T02:30:00+00:00"  # 2026-11-15 21:30 ET, prefix looks protected
EDGE_13 = "2026-11-14T02:30:00+00:00"  # 2026-11-13 21:30 ET
PROT = "2026-11-16T16:00:00+00:00"
NAIVE = "2026-11-10T15:00:00"


class Resp:
    def __init__(self, status=200, headers=None, body=b""):
        self.status, self.headers, self._body = status, headers or {}, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ExportBucket:
    """HEAD day-export objects and list them. ``sizes`` maps a day to Content-Length."""

    def __init__(self, sizes):
        self.sizes, self.heads, self.gets = dict(sizes), [], []

    def __call__(self, request, timeout=None):
        method = request.get_method()
        assert "SUPER_SECRET" not in str(request.headers)
        if method == "GET":
            self.gets.append(request.full_url)
            parts = [f"<Contents><Key>day-export/live_{day}.sqlite3.gz</Key></Contents>"
                     for day in sorted(self.sizes)]
            body = ("<ListBucketResult><IsTruncated>false</IsTruncated>"
                    + "".join(parts) + "</ListBucketResult>")
            return Resp(body=body.encode())
        if method == "HEAD":
            self.heads.append(request.full_url)
            name = request.full_url.rsplit("/", 1)[-1]
            day = name[len("live_"):-len(".sqlite3.gz")] if name.startswith("live_") else ""
            if day not in self.sizes:
                raise HTTPError(request.full_url, 404, "missing", {}, None)
            return Resp(headers={"Content-Length": str(self.sizes[day]), "ETag": '"abc"'})
        raise AssertionError(method)


def _shadow(conn, ts, side):
    conn.execute("""INSERT INTO finalb_shadow
        (ts, ticker, side, live_decision, live_block_reasons, finalb_decision,
         finalb_block_reasons, live_score, finalb_score, rule_flags, cooldown_state, contract)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (ts, "AAPL", side, "pass", "[]", "pass", "[]", 1.0, 1.0, "{}", "{}", "C"))


def _snap(conn, symbol, at, payload=b"x"):
    conn.execute("INSERT INTO snapshots VALUES (?,?,?)", (symbol, at, payload))


class DbPruneTests(unittest.TestCase):
    def setUp(self):
        clear_space_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "live.sqlite3"

    def tearDown(self):
        clear_space_state()
        self.tmp.cleanup()

    def test_keep_zero_does_not_touch_the_database(self):
        self.db.write_bytes(b"not a database")
        before = self.db.read_bytes()

        def boom(*args, **kwargs):
            raise AssertionError("prune touched the disk or the network")

        with patch.dict(os.environ, {"LOTTO_DB_PRUNE_KEEP_DAYS": "0", "LOTTO_DB_PRUNE_ALLOW_PRE_EXPORT": "0"}), \
                self.assertNoLogs("lotto.db_prune", level="INFO"):
            result = run_db_prune(self.db, TODAY, free=boom, opener=boom)
        self.assertFalse(result["enabled"])
        self.assertEqual(result["deleted"], {})
        self.assertEqual(self.db.read_bytes(), before)
        with patch.dict(os.environ, {}, clear=False), \
                patch("lotto.db_prune.sqlite3.connect", side_effect=AssertionError("db")):
            os.environ.pop("LOTTO_DB_PRUNE_KEEP_DAYS", None)
            self.assertFalse(run_db_prune(self.db, TODAY)["enabled"])
        self.assertEqual(Settings().db_prune_keep_days, 0)
        self.assertEqual(Settings().db_prune_allow_pre_export, 0)
        self.assertNotIn("db_prune_keep_days", recorded_settings(Settings(db_prune_keep_days=4)))
        self.assertNotIn("db_prune_allow_pre_export", recorded_settings(Settings()))
        with patch.dict(os.environ, {"LOTTO_DB_PRUNE_KEEP_DAYS": "9", "LOTTO_DB_PRUNE_ALLOW_PRE_EXPORT": "1"}):
            loaded = env_settings()
        self.assertEqual((loaded.db_prune_keep_days, loaded.db_prune_allow_pre_export), (9, 1))
        self.assertIs(type(loaded.db_prune_keep_days), int)
        for bad in (dict(db_prune_keep_days=-1), dict(db_prune_keep_days=True),
                    dict(db_prune_allow_pre_export=2), dict(db_prune_allow_pre_export=True)):
            with self.assertRaises(ValueError):
                Settings(**bad)

    def test_prunes_only_verified_days_and_leaves_protected_rows(self):
        self.assertEqual(et_day(EDGE_15), "2026-11-15")
        self.assertEqual(et_day(EDGE_13), "2026-11-13")
        self.assertEqual(protected_days(TODAY, 2), {"2026-11-18", "2026-11-17", "2026-11-16"})
        store = Store(str(self.db))
        db = store.db
        big = b"x" * 8000
        _snap(db, "OLD10", OLD, big)
        _snap(db, "EDGE15", EDGE_15, big)
        _snap(db, "EDGE13", EDGE_13, big)
        _snap(db, "MISS12", "2026-11-12T15:00:00+00:00", big)
        _snap(db, "ZERO11", "2026-11-11T15:00:00+00:00", big)
        _snap(db, "PROT16", PROT, b"keep")
        _snap(db, "PROT17", "2026-11-17T15:00:00+00:00", b"keep")
        _snap(db, "PROT18", "2026-11-18T15:00:00+00:00", b"keep")
        _snap(db, "NAIVE", NAIVE, b"keep")
        db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)", ("OLD", OLD, "block", "x", 1, "{}"))
        db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)", ("KEEP", PROT, "block", "x", 1, "{}"))
        db.execute("INSERT INTO discovery VALUES (?,?,?,?,?)", ("2026-11-10", "OLD", OLD, 0, "{}"))
        db.execute("INSERT INTO discovery VALUES (?,?,?,?,?)", ("2026-11-12", "MISS", "2026-11-12T15:00:00+00:00", 0, "{}"))
        db.execute("INSERT INTO discovery VALUES (?,?,?,?,?)", ("2026-11-16", "KEEP", PROT, 0, "{}"))
        db.execute("""INSERT INTO chain_coverage (day, symbol, at, promoted, contracts, spot, flow_side, flow_cluster)
                      VALUES (?,?,?,?,?,?,?,?)""", ("2026-11-10", "OLD", OLD, 0, 1, 1, "CALL", 0))
        db.execute("""INSERT INTO chain_coverage (day, symbol, at, promoted, contracts, spot, flow_side, flow_cluster)
                      VALUES (?,?,?,?,?,?,?,?)""", ("2026-11-16", "KEEP", PROT, 0, 1, 1, "CALL", 0))
        db.execute("INSERT INTO candidate_evaluations VALUES (?,?,?,?,?,?,?,?)",
                   ("2026-11-10", "OLD", OLD, "C", "s", 1, 1, "{}"))
        db.execute("INSERT INTO candidate_evaluations VALUES (?,?,?,?,?,?,?,?)",
                   ("2026-11-16", "KEEP", PROT, "C", "s", 1, 1, "{}"))
        _shadow(db, OLD, "RANK")
        _shadow(db, OLD, "CALL")
        _shadow(db, OLD, "PUT")
        _shadow(db, EDGE_15, "RANK")
        _shadow(db, EDGE_15, "CALL")
        _shadow(db, PROT, "RANK")
        db.execute("""INSERT INTO alerts (id, day, symbol, side, contract, at, entry_ask, payload, snapshot, delivery)
                      VALUES ('a1','2026-11-10','AAPL','CALL','C',?,1,'{}','{}','pending')""", (OLD,))
        db.execute("INSERT INTO milestones VALUES ('a1', 50, ?)", (OLD,))
        db.execute("INSERT INTO health_incidents (opened_at, reason) VALUES (?, 'gap')", (OLD,))
        db.execute("INSERT INTO user_feedback VALUES ('a1','worked','note',?)", (OLD,))
        db.execute("INSERT INTO nightly_runs VALUES ('2026-11-10', ?, '{}')", (OLD,))
        db.execute("CREATE TABLE shadow_cards (day TEXT, note TEXT)")
        db.execute("CREATE TABLE quality (day TEXT, note TEXT)")
        db.execute("INSERT INTO shadow_cards VALUES ('2026-11-10', 'keep')")
        db.execute("INSERT INTO quality VALUES ('2026-11-10', 'keep')")
        db.commit()
        before = self.db.stat().st_size
        pages_before = db.execute("PRAGMA page_count").fetchone()[0]
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        db.close()
        bucket = ExportBucket({
            "2026-11-10": 120, "2026-11-11": 0, "2026-11-13": 80, "2026-11-15": 90,
        })
        with patch.dict(os.environ, ENV), self.assertLogs("lotto.db_prune", level="INFO") as logs:
            result = run_db_prune(self.db, TODAY, keep=2, opener=bucket)
        db = sqlite3.connect(self.db)
        self.addCleanup(db.close)
        text = "\n".join(logs.output)
        self.assertNotIn("SUPER_SECRET", text)
        self.assertIsNone(result["stopped"])
        self.assertEqual(result["deleted"]["snapshots"], 3)
        self.assertEqual(result["deleted"]["decisions"], 1)
        self.assertEqual(result["deleted"]["discovery"], 1)
        self.assertEqual(result["deleted"]["chain_coverage"], 1)
        self.assertEqual(result["deleted"]["candidate_evaluations"], 1)
        self.assertEqual(result["deleted"]["finalb_shadow"], 2)
        self.assertEqual(set(result["pruned_days"]), {"2026-11-10", "2026-11-13", "2026-11-15"})
        reasons = dict(result["skipped"])
        self.assertEqual(reasons["2026-11-12"], "no verified export")
        self.assertEqual(reasons["2026-11-11"], "export empty")
        self.assertIn("page_count", text)
        self.assertIn("freelist_count", text)
        self.assertIn("free ", text)
        self.assertIn("2026-11-12 (no verified export)", text)
        self.assertGreater(result["freelist_after"], result["freelist_before"])
        self.assertEqual(result["page_count_after"], pages_before)
        self.assertLessEqual(self.db.stat().st_size, before)
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        left = {row[0] for row in db.execute("SELECT symbol FROM snapshots")}
        self.assertEqual(left, {"MISS12", "ZERO11", "PROT16", "PROT17", "PROT18", "NAIVE"})
        self.assertEqual(db.execute("SELECT symbol FROM decisions").fetchall(), [("KEEP",)])
        self.assertEqual({row[0] for row in db.execute("SELECT day FROM discovery")}, {"2026-11-12", "2026-11-16"})
        self.assertEqual(db.execute("SELECT day FROM chain_coverage").fetchall(), [("2026-11-16",)])
        self.assertEqual(db.execute("SELECT day FROM candidate_evaluations").fetchall(), [("2026-11-16",)])
        sides = db.execute("SELECT ts, side FROM finalb_shadow ORDER BY id").fetchall()
        self.assertEqual(sides, [(OLD, "CALL"), (OLD, "PUT"), (EDGE_15, "CALL"), (PROT, "RANK")])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM milestones").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM health_incidents").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM user_feedback").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM nightly_runs").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT note FROM shadow_cards").fetchone()[0], "keep")
        self.assertEqual(db.execute("SELECT note FROM quality").fetchone()[0], "keep")
        self.assertTrue(any(url.endswith("live_2026-11-10.sqlite3.gz") for url in bucket.heads))
        self.assertTrue(any(url.endswith("live_2026-11-12.sqlite3.gz") for url in bucket.heads))
        self.assertFalse(any("live_2026-11-16" in url or "live_2026-11-18" in url for url in bucket.heads))
        self.assertTrue(bucket.gets)

    def _symbols(self, path):
        conn = sqlite3.connect(path)
        try:
            return {row[0] for row in conn.execute("SELECT symbol FROM snapshots")}
        finally:
            conn.close()

    def test_pre_export_days_require_the_allow_flag(self):
        def seed(path):
            store = Store(str(path))
            for symbol, at in (("PRE", "2026-11-10T15:00:00+00:00"), ("MID", "2026-11-13T15:00:00+00:00"),
                               ("NEW", "2026-11-14T15:00:00+00:00")):
                _snap(store.db, symbol, at)
            store.db.commit()
            store.db.close()

        seed(self.db)
        bucket = ExportBucket({"2026-11-13": 50})
        with patch.dict(os.environ, ENV):
            os.environ.pop("LOTTO_DB_PRUNE_ALLOW_PRE_EXPORT", None)
            held = run_db_prune(self.db, TODAY, keep=2, opener=bucket)
        self.assertEqual(self._symbols(self.db), {"PRE", "NEW"})
        self.assertIn(("2026-11-10", "older than oldest export 2026-11-13; pre-export not allowed"), held["skipped"])
        self.assertEqual(held["pruned_days"], ["2026-11-13"])
        other = self.root / "allow.sqlite3"
        seed(other)
        with patch.dict(os.environ, ENV):
            allowed = run_db_prune(other, TODAY, keep=2, allow_pre_export=1, opener=bucket)
        self.assertEqual(self._symbols(other), {"NEW"})
        self.assertEqual(set(allowed["pruned_days"]), {"2026-11-10", "2026-11-13"})
        self.assertEqual(dict(allowed["skipped"])["2026-11-14"], "no verified export")

    def test_unconfigured_bucket_deletes_nothing(self):
        store = Store(str(self.db))
        _snap(store.db, "OLD", OLD)
        store.db.commit()
        store.db.close()
        empty = {key: "" for key in ENV}
        with patch.dict(os.environ, empty), self.assertLogs("lotto.db_prune", level="INFO") as logs:
            result = run_db_prune(self.db, TODAY, keep=2, opener=lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
        self.assertEqual(result["deleted"]["snapshots"], 0)
        self.assertEqual(result["skipped"], [("2026-11-10", "export bucket unavailable")])
        self.assertIn("export bucket unavailable", "\n".join(logs.output))
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)

    def test_batches_are_capped_and_cover_shared_timestamps(self):
        store = Store(str(self.db))
        for i in range(5):
            _snap(store.db, f"S{i}", OLD)
        store.db.commit()
        store.db.close()
        bucket = ExportBucket({"2026-11-10": 10})
        with patch.dict(os.environ, ENV), self.assertLogs("lotto.db_prune", level="INFO"):
            result = run_db_prune(self.db, TODAY, keep=2, opener=bucket, batch=2,
                                  free=lambda: 10**9)
        self.assertEqual(result["deleted"]["snapshots"], 5)
        self.assertEqual(result["batches"], 3)
        self.assertEqual(result["max_batch"], 2)
        self.assertIsNone(result["stopped"])
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)

    def test_low_disk_stops_without_a_further_batch(self):
        store = Store(str(self.db))
        for i in range(5):
            _snap(store.db, f"S{i}", OLD)
        _shadow(store.db, OLD, "RANK")
        store.db.execute("""INSERT INTO alerts (id, day, symbol, side, contract, at, entry_ask, payload, snapshot, delivery)
                            VALUES ('a1','2026-11-10','AAPL','CALL','C',?,1,'{}','{}','pending')""", (OLD,))
        store.db.commit()
        store.db.close()

        def free():
            conn = sqlite3.connect(self.db)
            try:
                left = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            finally:
                conn.close()
            return 80_000_000 if left >= 5 else 1_000_000

        bucket = ExportBucket({"2026-11-10": 10})
        with patch.dict(os.environ, ENV), self.assertLogs("lotto.db_prune", level="INFO") as logs:
            result = run_db_prune(self.db, TODAY, keep=2, opener=bucket, batch=2, min_free=40_000_000, free=free)
        self.assertEqual(result["stopped"], "low-disk")
        self.assertEqual(result["deleted"]["snapshots"], 2)
        self.assertLessEqual(result["max_batch"], 2)
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 3)
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM finalb_shadow").fetchone()[0], 1)
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)
        self.assertIn("stopped low-disk", "\n".join(logs.output))

    def test_sqlite_error_stops_cleanly(self):
        store = Store(str(self.db))
        _snap(store.db, "OLD", OLD)
        store.db.commit()
        store.db.close()
        os.chmod(self.db, 0o444)
        try:
            bucket = ExportBucket({"2026-11-10": 10})
            with patch.dict(os.environ, ENV), self.assertLogs("lotto.db_prune", level="INFO") as logs:
                result = run_db_prune(self.db, TODAY, keep=2, opener=bucket, free=lambda: 10**9)
            self.assertEqual(result["stopped"], "sqlite")
            self.assertEqual(result["deleted"]["snapshots"], 0)
            self.assertIn("stopped sqlite", "\n".join(logs.output))
        finally:
            os.chmod(self.db, 0o644)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)
        finally:
            conn.close()

    def test_missing_database_is_not_created(self):
        missing = self.root / "live.sqlite3"
        with self.assertLogs("lotto.db_prune", level="INFO"):
            result = run_db_prune(missing, TODAY, keep=3, opener=lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
        self.assertFalse(missing.exists())
        self.assertEqual(sum(result["deleted"].values()), 0)
        self.assertIsNone(result["stopped"])

    def test_scheduler_runs_prune_after_files_outside_market_hours(self):
        order = []

        def runner(data_dir, today):
            order.append(("files", today))
            return {"failed": 0}

        def prune(database, today, **kwargs):
            order.append(("prune", today, kwargs["keep"], Path(database).name, kwargs["allow_pre_export"]))

        evening = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)  # 17:00 ET
        market = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)  # 10:00 ET
        morning = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)  # 07:00 ET
        both = Settings(space_cleanup=1, db_prune_keep_days=4, db_prune_allow_pre_export=1)
        self.assertIsNone(maybe_space_cleanup(both, self.root, market, runner=runner))
        with patch("lotto.db_prune.run_db_prune", side_effect=prune):
            maybe_space_cleanup(both, self.root, evening, runner=runner).join(5)
            self.assertIsNone(maybe_space_cleanup(both, self.root, evening, runner=runner))
            self.assertIsNone(maybe_space_cleanup(both, self.root, morning, runner=runner))
            next_evening = datetime(2026, 11, 19, 21, 45, tzinfo=timezone.utc)  # 16:45 ET
            maybe_space_cleanup(both, self.root, next_evening, runner=runner).join(5)
        self.assertEqual(order, [
            ("files", date(2026, 11, 18)),
            ("prune", date(2026, 11, 18), 4, "live.sqlite3", True),
            ("files", date(2026, 11, 19)),
            ("prune", date(2026, 11, 19), 4, "live.sqlite3", True),
        ])
        order.clear()
        clear_space_state()
        prune_only = Settings(db_prune_keep_days=3)
        with patch("lotto.db_prune.run_db_prune", side_effect=prune):
            maybe_space_cleanup(prune_only, self.root, evening, runner=runner).join(5)
        self.assertEqual([step[0] for step in order], ["prune"])
        order.clear()
        clear_space_state()

        def fail(data_dir, today):
            order.append("files")
            raise RuntimeError("upload failed")

        with patch("lotto.db_prune.run_db_prune", side_effect=prune), \
                self.assertLogs("lotto.space", level="WARNING"):
            maybe_space_cleanup(Settings(space_cleanup=1, db_prune_keep_days=2), self.root, evening,
                                runner=fail).join(5)
        self.assertEqual(order[0], "files")
        self.assertEqual(order[1][0], "prune")
        self.assertIsNone(maybe_space_cleanup(Settings(), self.root, evening, runner=runner))


if __name__ == "__main__":
    unittest.main()
