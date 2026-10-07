import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from lotto.demo import demo_frames
from lotto.engine import Engine
from lotto.features import Candidate
from lotto.nightly import run_nightly, trade_high_path
from lotto.patterns import Setup
from lotto.store import Store


class AlertPhaseTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.db.close)
        self.frames = [frame[0] for frame in demo_frames()]

    def candidate(self, snap, building):
        return Candidate(snap, snap.options[0], 78.0, {"price_structure":20.0},
                         {"return_from_open":.01, "return_from_close":.02},
                         ["Stock and options aligned"], True,
                         setup=Setup("COILED_CONTINUATION", snap.spot+.1,
                                     snap.spot-1, building=building))

    def phased_alerts(self):
        engine = Engine(self.store)
        active_at = self.frames[6].at
        with patch("lotto.engine.candidates", side_effect=lambda snap, history, settings:
                   [self.candidate(snap, snap.at < active_at)]):
            alerts = [a for snap in self.frames[:7] for a in engine.process([snap])]
        return alerts

    def test_potential_watch_gets_separate_active_alert_and_fresh_baseline(self):
        alerts = self.phased_alerts()
        self.assertEqual([a["phase"] for a in alerts], ["POTENTIAL", "ACTIVE"])
        self.assertEqual(alerts[1]["parent_id"], alerts[0]["id"])
        self.assertIn("POTENTIAL TRADE WATCH", alerts[0]["payload"]["embeds"][0]["title"])
        self.assertIn("ACTIVE TRADE IDEA", alerts[1]["payload"]["embeds"][0]["title"])
        stored = self.store.summary()
        self.assertEqual([a["phase"] for a in stored], ["POTENTIAL", "ACTIVE"])
        self.assertEqual(stored[0]["thesis_id"], stored[1]["thesis_id"])
        self.assertEqual(stored[1]["parent_id"], stored[0]["id"])
        self.assertEqual(stored[1]["at"], self.frames[6].at.isoformat())
        self.assertEqual(stored[1]["entry_ask"], self.frames[6].options[0].ask)

    def test_watch_can_activate_after_worker_restart_without_repeating_active_alert(self):
        active_at = self.frames[6].at
        with patch("lotto.engine.candidates", side_effect=lambda snap, history, settings:
                   [self.candidate(snap, snap.at < active_at)]):
            first = Engine(self.store)
            watches = [a for snap in self.frames[:6] for a in first.process([snap])]
            restarted = Engine(self.store)
            active = [a for snap in self.frames[6:10] for a in restarted.process([snap])]
        self.assertEqual([a["phase"] for a in watches], ["POTENTIAL"])
        self.assertEqual([a["phase"] for a in active], ["ACTIVE"])
        self.assertEqual(active[0]["parent_id"], watches[0]["id"])

    def test_nightly_counts_active_only_and_keeps_trade_high_windows_separate(self):
        self.phased_alerts()
        first_at = self.frames[5].at
        option = self.frames[5].options[0].symbol
        client = Mock()
        client.candles.return_value = {"candles": [
            {"datetime":int((first_at+timedelta(minutes=1)).timestamp()*1000),
             "high":2.0, "low":.8},
            {"datetime":int((first_at+timedelta(minutes=2)).timestamp()*1000),
             "high":3.0, "low":1.0},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            report = json.loads(run_nightly(self.store, first_at.date().isoformat(),
                                            directory, symbols=(), client=client).read_text())
            offline = json.loads(run_nightly(self.store, first_at.date().isoformat(),
                                             directory, symbols=(), client=None).read_text())
        self.assertEqual(report["alert_phase_counts"], {"POTENTIAL":1, "ACTIVE":1})
        self.assertEqual(report["activated_watches"], 1)
        self.assertEqual(report["active_opportunity_summary"]["alert_count"], 1)
        self.assertEqual(report["potential_opportunity_summary"]["alert_count"], 1)
        trade_highs = report["option_trade_high_feedback"]
        self.assertEqual(len(trade_highs), 2)
        self.assertEqual(trade_highs[1]["phase"], "ACTIVE")
        self.assertEqual(trade_highs[1]["parent_id"], trade_highs[0]["id"])
        self.assertEqual(trade_highs[1]["peak_day_trade_high"], 3.0)
        self.assertEqual(client.candles.call_count, 1)
        self.assertEqual(client.candles.call_args.args[0], option)
        self.assertEqual(offline["active_opportunity_summary"]["observed_count"], 1)

    def test_trade_high_ignores_alert_minute_and_does_not_invent_intrabar_order(self):
        at = self.frames[5].at
        alert = {"id":"test", "symbol":"DEMO", "contract":"DEMO-110C",
                 "phase":"ACTIVE", "parent_id":None, "thesis_id":"test",
                 "at":at.isoformat(), "entry_ask":.50, "delivery":"sent"}
        def bar(minute, high, low):
            return {"datetime":int((at+timedelta(minutes=minute)).timestamp()*1000),
                    "high":high, "low":low}
        result = trade_high_path(alert, [bar(0, 10.0, .01), bar(1, .70, .40),
                                         bar(2, 1.10, .10), bar(31, 1.50, .05)])
        self.assertEqual(result["peak_30m_trade_high"], 1.10)
        self.assertEqual(result["peak_day_trade_high"], 1.50)
        self.assertEqual(result["lowest_trade_before_peak"], .10)
        self.assertEqual(result["opportunity_label"], "Strong")

    def test_missing_option_trade_bars_are_unavailable_not_failed(self):
        self.phased_alerts()
        client = Mock()
        client.candles.return_value = {"candles":[]}
        with tempfile.TemporaryDirectory() as directory:
            report = json.loads(run_nightly(self.store, self.frames[5].day,
                                            directory, symbols=(), client=client).read_text())
        self.assertEqual(report["active_opportunity_summary"]["unobserved_count"], 1)
        self.assertEqual(report["potential_opportunity_summary"]["unobserved_count"], 1)
        self.assertEqual(report["active_opportunity_summary"]["opportunity_labels"], {})
        self.assertEqual(len(report["option_trade_high_errors"]), 1)

    def test_existing_database_alerts_remain_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/"old.db")
            db = sqlite3.connect(path)
            db.execute("""CREATE TABLE alerts (
                id TEXT PRIMARY KEY, day TEXT, symbol TEXT, side TEXT, contract TEXT,
                at TEXT, entry_ask REAL, payload TEXT, snapshot TEXT, delivery TEXT,
                max_return REAL, min_return REAL, latest_return REAL, last_quote TEXT,
                closed INTEGER DEFAULT 0)""")
            db.execute("INSERT INTO alerts (id,day,symbol,side,contract,at,entry_ask) "
                       "VALUES ('old','2026-10-01','SPY','CALL','SPY-C','2026-10-01T10:00:00-04:00',.5)")
            db.commit()
            db.close()
            store = Store(path)
            self.addCleanup(store.db.close)
            self.assertEqual(store.summary()[0]["phase"], "LEGACY")
            self.assertIsNone(store.summary()[0]["thesis_id"])


if __name__ == "__main__":
    unittest.main()
