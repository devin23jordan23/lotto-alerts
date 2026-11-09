import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from lotto.exit_replay import build_specs, replay_day, replay_exit
from lotto.main import main
from lotto.models import ET, Option, Snapshot
from lotto.store import Store


def q(minute, bid, ask=None, start=None):
    start = start or datetime(2026, 11, 6, 12, 0, tzinfo=ET)
    return (start + timedelta(minutes=minute), bid, ask if ask is not None else bid + .02)


class ExitReplayTests(unittest.TestCase):
    def test_stop_is_taken_before_a_later_double_and_order_does_not_matter(self):
        start = datetime(2026, 11, 6, 12, 0, tzinfo=ET)
        path = [q(0, 1.0, start=start), q(1, .60, start=start), q(2, 3.0, start=start)]
        stopped = replay_exit(1.0, start, path, stop_return=-.35, target_return=1.0)
        self.assertEqual(stopped["outcome"], "stop")
        self.assertEqual(stopped["exit_bid"], .60)
        self.assertNotIn("stop_crossed_in_gap", stopped["flags"])
        self.assertEqual(replay_exit(1.0, start, list(reversed(path)), stop_return=-.35, target_return=1.0)["exit_at"],
                         stopped["exit_at"])
        early = q(-5, .10, start=start)
        self.assertEqual(replay_exit(1.0, start, [early, *path], stop_return=-.35)["exit_bid"], .60)
        # Exit return is option bid over entry ask. The underlying's direction is not an input.
        self.assertAlmostEqual(stopped["exit_return"], .60 / 1.0 - 1)

    def test_gaps_are_flagged_and_a_quiet_path_is_censored(self):
        start = datetime(2026, 11, 6, 12, 0, tzinfo=ET)
        jumped = replay_exit(1.0, start, [q(0, 1.0, start=start), q(10, .20, start=start)],
                             stop_return=-.35, target_return=.40, max_gap_seconds=120)
        self.assertEqual(jumped["outcome"], "ambiguous")
        self.assertIn("quote_gap", jumped["flags"])
        self.assertIn("stop_crossed_in_gap", jumped["flags"])
        self.assertIn("ordering_unknown", jumped["flags"])
        ran = replay_exit(1.0, start, [q(0, 1.0, start=start), q(10, 2.5, start=start)],
                          stop_return=-.35, target_return=1.0, max_gap_seconds=120)
        self.assertEqual(ran["outcome"], "ambiguous")
        self.assertIn("target_crossed_in_gap", ran["flags"])
        self.assertIn("ordering_unknown", ran["flags"])
        quiet = replay_exit(1.0, start, [q(1, .90, start=start), q(2, .95, start=start)],
                            stop_return=-.35, target_return=.40,
                            session_close=datetime(2026, 11, 6, 16, 0, tzinfo=ET))
        self.assertEqual(quiet["outcome"], "censored")
        self.assertIn("path_censored", quiet["flags"])
        self.assertIn("censored_before_close", quiet["flags"])

    def test_time_stop_does_not_use_a_later_quote_and_bad_prints_are_skipped(self):
        start = datetime(2026, 11, 6, 12, 0, tzinfo=ET)
        held = replay_exit(1.0, start, [q(10, 1.05, start=start), q(31, 1.04, start=start), q(50, 3.0, start=start)],
                           stop_return=-.35, target_return=1.0, time_stop_minutes=30)
        self.assertEqual(held["outcome"], "time_stop")
        self.assertAlmostEqual(held["exit_return"], .04)
        crossed = replay_exit(1.0, start, [q(1, .40, .30, start=start), q(2, .50, start=start)], stop_return=-.35)
        self.assertEqual(crossed["outcome"], "stop")
        self.assertEqual(crossed["exit_bid"], .50)
        self.assertIn("crossed_market", crossed["flags"])
        missing = replay_exit(1.0, start, [(start + timedelta(minutes=1), None, .5), q(2, 1.0, start=start)],
                              stop_return=-.35, target_return=.40)
        self.assertIn("missing_bid", missing["flags"])
        self.assertEqual(missing["outcome"], "censored")
        zero = replay_exit(1.0, start, [q(1, 0.0, .05, start=start)], stop_return=-.35)
        self.assertEqual(zero["outcome"], "stop")
        self.assertEqual(zero["exit_return"], -1)
        with self.assertRaises(ValueError):
            build_specs([.35], [.40], [])

    def test_stored_replay_is_read_only_and_uses_the_option_bid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.sqlite3"
            store = Store(str(path))
            start = datetime(2026, 11, 6, 12, 38, tzinfo=ET)
            contract = "SPY   261106P00726000"
            def sample(minute, bid):
                at = start + timedelta(minutes=minute)
                option = Option(contract, at.date(), "PUT", 726, bid, bid + .05, 200, 10, -.2, .01, at)
                return Snapshot("SPY", at, 735.91, at, 730, (), (option,))
            for item in (sample(0, 1.0), sample(5, .90), sample(20, .90), sample(40, 2.7)):
                store.record(item)
            entry = sample(0, 1.0)
            store.db.execute(
                "INSERT INTO alerts (id,day,symbol,side,contract,at,entry_ask,snapshot,delivery) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("spy", entry.day, "SPY", "PUT", contract, entry.at.isoformat(), 1.0,
                 json.dumps(entry.to_dict()), "sent"))
            store.db.commit()
            starts = store.db.execute("SELECT COUNT(*) FROM runtime_starts").fetchone()[0]
            store.db.close()
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            report = replay_day(str(path), "2026-11-06", build_specs([-.35], [1.0], [15]))
            self.assertFalse(report["live_stops_changed"])
            self.assertFalse(report["full_day_grade"])
            self.assertEqual(report["alerts"][0]["side"], "PUT")
            by_outcome = {item["outcome"] for item in report["alerts"][0]["specs"]}
            self.assertEqual(by_outcome, {"ambiguous", "time_stop"})
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            reopened = Store(str(path))
            self.addCleanup(reopened.db.close)
            self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM runtime_starts").fetchone()[0], starts)
            self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 4)

    def test_cli_refuses_a_partial_day_without_choosing_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "live.sqlite3")
            Store(path).db.close()
            with patch.object(sys, "argv", ["lotto", "exit-replay", "--db", path]):
                with self.assertRaises(SystemExit) as missing:
                    main()
            self.assertEqual(missing.exception.code, 2)
            output = Path(directory) / "out.json"
            with patch.object(sys, "argv", ["lotto", "exit-replay", "--db", path, "--day", "2026-11-06",
                                            "--output", str(output)]):
                with self.assertRaises(SystemExit) as partial:
                    main()
            self.assertEqual(partial.exception.code, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
