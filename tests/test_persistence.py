import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from lotto.main import main
from lotto.models import ET, Snapshot
from lotto.nightly import maybe_nightly
from lotto.persistence import (
    SCHEMA_VERSION, PersistenceError, assess_session, backup_database, connect_readonly,
    in_regular_session_clock, live_start_blockers, require_existing_database, resolve_database_path,
)
from lotto.store import Store


def snap(at):
    return Snapshot("SPY", at, 100, at, 99, (), ())


def fill(store, day, start=(9, 31), end=(15, 56), step=5):
    clock = datetime.fromisoformat(day).replace(hour=start[0], minute=start[1], tzinfo=ET)
    stop = datetime.fromisoformat(day).replace(hour=end[0], minute=end[1], tzinfo=ET)
    while clock <= stop:
        store.record(snap(clock))
        clock += timedelta(minutes=step)


class DatabasePathTests(unittest.TestCase):
    def test_explicit_path_beats_env_and_live_keeps_the_volume_filename(self):
        env = {"DATA_DIR": "/data", "LOTTO_DB_PATH": "/data/other.sqlite3"}
        self.assertEqual(resolve_database_path("live", "/tmp/exact.sqlite3", env), "/tmp/exact.sqlite3")
        self.assertEqual(resolve_database_path("live", None, env), "/data/other.sqlite3")
        self.assertEqual(resolve_database_path("nightly", None, {"DATA_DIR": "/data"}), "/data/live.sqlite3")
        self.assertEqual(resolve_database_path("demo", None, {"DATA_DIR": "/data"}), "/data/demo.sqlite3")

    def test_regular_session_clock_is_et_weekday_only(self):
        self.assertTrue(in_regular_session_clock(datetime(2026, 11, 6, 11, 0, tzinfo=ET)))
        self.assertFalse(in_regular_session_clock(datetime(2026, 11, 6, 9, 29, tzinfo=ET)))
        self.assertFalse(in_regular_session_clock(datetime(2026, 11, 6, 16, 0, tzinfo=ET)))
        self.assertFalse(in_regular_session_clock(datetime(2026, 11, 8, 11, 0, tzinfo=ET)))

    def test_live_refuses_a_new_file_during_the_session_and_keeps_an_existing_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "live.sqlite3")
            friday = datetime(2026, 11, 6, 11, 0, tzinfo=ET)
            sunday = datetime(2026, 11, 8, 12, 0, tzinfo=ET)
            self.assertTrue(live_start_blockers(path, friday, {}))
            self.assertFalse(live_start_blockers(path, sunday, {}))
            store = Store(path)
            store.record(snap(friday))
            self.addCleanup(store.db.close)
            self.assertFalse(live_start_blockers(path, friday, {}))
            again = Store(path)
            self.addCleanup(again.db.close)
            self.assertEqual(again.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)

    def test_railway_refuses_an_unmounted_directory_and_an_off_volume_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "appdata"
            root.mkdir()
            db = root / "live.sqlite3"
            Store(str(db)).db.close()
            env = {"RAILWAY_SERVICE_ID": "svc", "RAILWAY_VOLUME_MOUNT_PATH": str(root)}
            blocked = live_start_blockers(str(db), datetime(2026, 11, 8, 12, tzinfo=ET), env)
            self.assertTrue(any("not a mounted filesystem" in item for item in blocked))
            trusted = {**env, "LOTTO_TRUST_VOLUME_MOUNT": "1"}
            self.assertFalse(live_start_blockers(str(db), datetime(2026, 11, 8, 12, tzinfo=ET), trusted))
            outside = live_start_blockers("/tmp/not-on-volume.sqlite3", datetime(2026, 11, 8, 12, tzinfo=ET), trusted)
            self.assertTrue(any("outside the Railway volume" in item for item in outside))

    def test_railway_does_not_create_a_missing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing" / "live.sqlite3"
            with patch.dict(os.environ, {"RAILWAY_SERVICE_ID": "svc"}):
                with self.assertRaises(PersistenceError):
                    Store(str(missing))
            self.assertFalse(missing.parent.exists())


class MigrationTests(unittest.TestCase):
    def test_legacy_rows_survive_and_a_newer_schema_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            db = sqlite3.connect(path)
            db.execute("""CREATE TABLE alerts (
                id TEXT PRIMARY KEY, day TEXT, symbol TEXT, side TEXT, contract TEXT,
                at TEXT, entry_ask REAL, payload TEXT, snapshot TEXT, delivery TEXT,
                max_return REAL, min_return REAL, latest_return REAL, last_quote TEXT,
                closed INTEGER DEFAULT 0)""")
            db.execute("INSERT INTO alerts (id,day,symbol,side,contract,at,entry_ask) VALUES ('old','2026-11-06','SPY','PUT','SPY-P','2026-11-06T15:40:00+00:00',.4)")
            db.execute("CREATE TABLE snapshots (symbol TEXT, at TEXT, payload TEXT, PRIMARY KEY(symbol, at))")
            db.execute("INSERT INTO snapshots VALUES ('SPY','2026-11-06T15:40:00+00:00','kept')")
            db.commit()
            db.close()
            inode = path.stat().st_ino
            store = Store(str(path))
            self.addCleanup(store.db.close)
            self.assertEqual(store.summary()[0]["phase"], "LEGACY")
            self.assertEqual(store.db.execute("SELECT payload FROM snapshots").fetchone()[0], "kept")
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(path.stat().st_ino, inode)
            store.db.close()
            newer = sqlite3.connect(path)
            newer.execute("PRAGMA user_version=9")
            newer.commit()
            newer.close()
            with self.assertRaises(PersistenceError):
                Store(str(path))
            check = sqlite3.connect(path)
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(check.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)
            check.close()

    def test_backup_refuses_to_replace_and_leaves_the_source_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "live.sqlite3"
            dest = Path(directory) / "backups" / "live.sqlite3"
            store = Store(str(source))
            store.record(snap(datetime(2026, 11, 6, 10, 40, tzinfo=ET)))
            store.db.close()
            before = source.read_bytes()
            info = backup_database(str(source), str(dest))
            self.assertGreater(info["bytes"], 0)
            self.assertEqual(source.read_bytes(), before)
            copied = sqlite3.connect(dest)
            self.assertEqual(copied.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)
            copied.close()
            with self.assertRaises(PersistenceError):
                backup_database(str(source), str(dest))
            self.assertTrue(dest.exists())
            with self.assertRaises(PersistenceError):
                require_existing_database(str(Path(directory) / "missing.sqlite3"))


class SessionDateTests(unittest.TestCase):
    def test_et_date_handles_utc_rollover_midnight_and_both_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "live.sqlite3"))
            self.addCleanup(store.db.close)
            samples = {
                "rth_est": datetime(2026, 11, 6, 14, 31, tzinfo=timezone.utc),  # 09:31 EST
                "evening_utc_roll": datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc),  # 20:00 EST Nov 6
                "midnight": datetime(2026, 11, 7, 0, 30, tzinfo=ET),
                "rth_edt": datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc),  # 09:31 EDT
            }
            for item in samples.values():
                store.record(snap(item))
            nov6 = assess_session(store.db, "2026-11-06", now=datetime(2026, 11, 6, 21, tzinfo=ET))
            self.assertEqual(nov6["snapshot_count"], 2)
            self.assertEqual(nov6["first_rth_at"][11:16], "09:31")
            self.assertTrue(nov6["last_at"].startswith("2026-11-06T20:00"))
            self.assertNotIn("opens_after_grace", nov6["defects"])
            self.assertIn("ends_before_close", nov6["defects"])
            self.assertFalse(nov6["grade_as_full_day"])
            nov7 = assess_session(store.db, "2026-11-07", now=datetime(2026, 11, 7, 21, tzinfo=ET))
            self.assertEqual(nov7["snapshot_count"], 1)
            self.assertTrue(nov7["first_at"].startswith("2026-11-07T00:30"))
            july = assess_session(store.db, "2026-07-15", now=datetime(2026, 7, 15, 21, tzinfo=ET))
            self.assertEqual(july["first_rth_at"][11:16], "09:31")

    def test_full_session_grades_and_a_friday_open_hole_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "live.sqlite3"))
            self.addCleanup(store.db.close)
            fill(store, "2026-11-05")
            done = assess_session(store.db, "2026-11-05")
            self.assertEqual(done["status"], "complete")
            self.assertTrue(done["grade_as_full_day"])
            fill(store, "2026-11-06", start=(10, 40))
            friday = assess_session(store.db, "2026-11-06")
            self.assertIn("opens_after_grace", friday["defects"])
            self.assertFalse(friday["grade_as_full_day"])
            self.assertEqual(friday["status"], "partial")

    def test_internal_gap_and_naive_timestamps_are_defects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.sqlite3"
            store = Store(str(path))
            fill(store, "2026-11-05", end=(11, 0))
            fill(store, "2026-11-05", start=(12, 0), end=(15, 56))
            gap = assess_session(store.db, "2026-11-05")
            self.assertIn("internal_gap", gap["defects"])
            store.db.execute("INSERT INTO snapshots VALUES ('QQQ','2026-11-05T15:00:00','naive')")
            store.db.commit()
            store.db.close()
            connection = connect_readonly(path)
            self.addCleanup(connection.close)
            flagged = assess_session(connection, "2026-11-05")
            self.assertIn("unparsed_timestamps", flagged["defects"])
            self.assertFalse(flagged["grade_as_full_day"])

    def test_in_progress_morning_is_not_a_finished_grade(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "live.sqlite3"))
            self.addCleanup(store.db.close)
            fill(store, "2026-11-06", end=(10, 30))
            now = datetime(2026, 11, 6, 10, 31, tzinfo=ET)
            running = assess_session(store.db, "2026-11-06", now=now)
            self.assertEqual(running["status"], "in_progress")
            self.assertNotIn("ends_before_close", running["defects"])
            self.assertFalse(running["grade_as_full_day"])

    def test_catchup_uses_the_et_date_after_utc_midnight(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        store.record(snap(datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc)))
        client = Mock()
        client.session.return_value = None
        with patch("lotto.nightly.run_nightly") as run:
            maybe_nightly(store, client, [], "data", datetime(2026, 11, 7, 13, tzinfo=timezone.utc))
        self.assertEqual(run.call_args.args[1], "2026-11-06")


class CommandTests(unittest.TestCase):
    def test_session_and_nightly_require_an_explicit_complete_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "live.sqlite3")
            raw = sqlite3.connect(path)
            raw.execute("CREATE TABLE snapshots (symbol TEXT, at TEXT, payload TEXT)")
            raw.execute("INSERT INTO snapshots VALUES ('SPY','2026-11-06T15:40:00+00:00','x')")
            raw.commit()
            raw.close()
            env = {"DATA_DIR": directory}
            with patch.dict(os.environ, env, clear=False):
                with patch.object(sys, "argv", ["lotto", "nightly", "--db", path]):
                    with self.assertRaises(SystemExit) as missing_day:
                        main()
                self.assertEqual(missing_day.exception.code, 2)
                self.assertIsNone(sqlite3.connect(path).execute(
                    "SELECT 1 FROM sqlite_master WHERE name='schema_meta'").fetchone())
                with patch.object(sys, "argv", ["lotto", "nightly", "--db", path, "--day", "2026-11-06"]):
                    with self.assertRaises(SystemExit) as partial:
                        main()
                self.assertEqual(partial.exception.code, 2)
                self.assertFalse((Path(directory) / "nightly").exists())
                with patch.object(sys, "argv", ["lotto", "session", "--db", path]):
                    with self.assertRaises(SystemExit) as no_day:
                        main()
                self.assertEqual(no_day.exception.code, 2)

    def test_complete_day_can_be_reported_without_deleting_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "live.sqlite3")
            store = Store(path)
            fill(store, "2026-11-05")
            count = store.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            store.db.close()
            with patch.dict(os.environ, {"DATA_DIR": directory}):
                with patch.object(sys, "argv", ["lotto", "session", "--db", path, "--day", "2026-11-05"]):
                    main()
                with patch.object(sys, "argv", ["lotto", "nightly", "--db", path, "--day", "2026-11-05"]):
                    main()
            report = json.loads((Path(directory) / "nightly" / "2026-11-05" / "report.json").read_text())
            self.assertTrue(report["full_day_grade"])
            self.assertIn("not the stop-or-+40%", report["grader_boundary"])
            reopened = sqlite3.connect(path)
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], count)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
