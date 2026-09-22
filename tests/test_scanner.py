import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from lotto.config import Settings
from lotto.demo import demo_frames
from lotto.discord import deliver_pending
from lotto.engine import Engine
from lotto.features import candidates, contract_ok
from lotto.models import Snapshot
from lotto.schwab import Schwab
from lotto.store import Store


class ScannerTests(unittest.TestCase):
    def new_store(self, path):
        store = Store(path)
        self.addCleanup(store.db.close)
        return store

    def setUp(self):
        self.frames = list(demo_frames())
        self.store = self.new_store(":memory:")
        self.engine = Engine(self.store)

    def run_frames(self, transform=lambda s: s, frames=None, engine=None):
        alerts = []
        for frame in frames or self.frames:
            alerts.extend((engine or self.engine).process([transform(s) for s in frame]))
        return alerts

    def test_persistent_runner_alerts_once_and_tracks_500_percent(self):
        alerts = self.run_frames()
        self.assertEqual(len(alerts), 1)
        row = self.store.summary()[0]
        self.assertEqual(row["delivery"], "dry_run")
        self.assertAlmostEqual(row["max_return"], 4.95 / .8 - 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM milestones WHERE percent=500").fetchone()[0], 1)
        self.assertIn("POTENTIAL", alerts[0]["payload"]["embeds"][0]["title"])

    def test_extended_stock_is_still_eligible(self):
        self.assertTrue(self.run_frames(lambda s: replace(s, prior_close=80)))

    def test_fading_underlying_does_not_alert(self):
        def fade(s):
            bars = tuple(replace(b, open=200-b.open, high=200-b.low, low=200-b.high, close=200-b.close) for b in s.bars)
            return replace(s, spot=200-s.spot, bars=bars)
        self.assertFalse(self.run_frames(fade))

    def test_put_runner_is_symmetric(self):
        def bearish(s):
            bars = tuple(replace(b, open=200-b.open, high=200-b.low, low=200-b.high, close=200-b.close) for b in s.bars)
            options = tuple(replace(o, symbol=o.symbol.replace("C", "P"), side="PUT", strike=200-o.strike, delta=-o.delta) for o in s.options)
            return replace(s, spot=200-s.spot, bars=bars, options=options, context_return_5m=-.004)
        self.assertTrue(self.run_frames(bearish))

    def test_stale_or_future_options_cannot_alert(self):
        for minutes in (-15, 1):
            with self.subTest(minutes=minutes):
                engine = Engine(self.new_store(":memory:"))
                self.assertFalse(self.run_frames(lambda s: replace(s, options=tuple(
                    replace(o, quote_time=s.at + timedelta(minutes=minutes)) for o in s.options)), engine=engine))

    def test_wide_spreads_cannot_alert(self):
        self.assertFalse(self.run_frames(lambda s: replace(s, options=tuple(replace(o, bid=.1) for o in s.options))))

    def test_missing_baseline_or_sector_cannot_alert(self):
        self.assertFalse(self.run_frames(lambda s: replace(s, bars=tuple(replace(b, expected_cumulative_volume=None) for b in s.bars))))
        self.assertFalse(self.run_frames(lambda s: replace(s, context_return_5m=None), engine=Engine(self.new_store(":memory:"))))

    def test_isolated_option_activity_cannot_alert(self):
        self.assertFalse(self.run_frames(lambda s: replace(s, options=s.options[:1])))

    def test_future_bar_and_missing_open_rejected(self):
        snap = self.frames[30][0]
        self.assertTrue(replace(snap, bars=snap.bars[1:]).problems())
        self.assertTrue(replace(snap, at=snap.at-timedelta(minutes=2)).problems())

    def test_counter_reset_and_poll_gap_invalidate_options_window(self):
        history = [frame[0] for frame in self.frames[15:30]]
        snap = self.frames[30][0]
        self.assertTrue(candidates(snap, history, Settings()))
        reset = replace(history[-3], options=tuple(replace(o, volume=0) for o in history[-3].options))
        history[-3] = reset
        self.assertFalse(candidates(snap, history, Settings()))
        self.assertFalse(candidates(snap, [history[0], history[-1]], Settings()))

    def test_ranking_and_daily_cap_apply_across_symbols(self):
        engine = Engine(self.store, Settings(max_alerts_per_day=2))
        alerts = []
        for frame in self.frames:
            snap = frame[0]
            # Separate contract identities, as real provider identifiers include the ticker.
            expanded = [replace(snap, symbol=symbol, options=tuple(replace(o, symbol=symbol+o.symbol) for o in snap.options))
                        for symbol in ("AAA", "BBB", "CCC")]
            cycle = engine.process(expanded)
            self.assertLessEqual(len(cycle), 1)
            alerts.extend(cycle)
        self.assertEqual(len(alerts), 2)
        self.assertEqual([r["symbol"] for r in self.store.summary()], ["AAA", "BBB"])

    def test_restart_preserves_budget_and_duplicate_suppression(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.db")
            store = self.new_store(path)
            engine = Engine(store)
            first = self.run_frames(frames=self.frames[:33], engine=engine)
            self.assertEqual(len(first), 1)
            restarted_store = self.new_store(path)
            restarted = Engine(restarted_store)
            self.assertFalse(self.run_frames(frames=self.frames[33:], engine=restarted))
            self.assertEqual(len(restarted_store.summary()), 1)

    def test_one_bar_repeated_cannot_confirm(self):
        self.run_frames(frames=self.frames[:26])
        snap = self.frames[25][0]
        for second in (10, 20, 30, 40, 50):
            self.assertFalse(self.engine.process([replace(snap, at=snap.at+timedelta(seconds=second))]))
        self.assertFalse(self.store.summary())

    def test_early_close_stops_new_alerts_before_close(self):
        self.assertFalse(self.run_frames(lambda s: replace(s, session_end=s.at.replace(hour=10, minute=0))))

    def test_snapshot_round_trip(self):
        snap = self.frames[20][0]
        self.assertEqual(Snapshot.from_dict(json.loads(json.dumps(snap.to_dict()))), snap)

    def test_delayed_chain_and_missing_timestamp_are_not_fabricated(self):
        raw = {"symbol": "TEST", "strikePrice": 100, "bid": .5, "ask": .55, "totalVolume": 500,
               "openInterest": 0, "delta": .3, "gamma": .02}
        data = {"callExpDateMap": {"2026-09-21:0": {"100.0": [raw]}}}
        self.assertFalse(Schwab.parse_chain(data))
        raw["quoteTimeInLong"] = self.frames[20][0].at.timestamp()*1000
        self.assertEqual(len(Schwab.parse_chain(data)), 1)
        self.assertFalse(Schwab.parse_chain({**data, "isDelayed": True}))

    def test_dry_run_cannot_send_discord(self):
        self.run_frames()
        with patch("lotto.discord.urlopen") as send:
            deliver_pending(self.store, "https://discord.com/api/webhooks/test/test")
            send.assert_not_called()

    def test_old_pending_alert_expires_without_post(self):
        self.run_frames()
        self.store.db.execute("UPDATE alerts SET delivery='pending', at='2000-01-01T10:00:00+00:00'")
        with patch("lotto.discord.urlopen") as send:
            deliver_pending(self.store, "https://discord.com/api/webhooks/test/test")
            send.assert_not_called()
        self.assertEqual(self.store.summary()[0]["delivery"], "expired")

    def test_end_of_day_uses_ask_to_bid_and_sampled_excursions(self):
        self.run_frames()
        rows = self.store.end_of_day_options("2026-09-21")
        self.assertTrue(rows)
        row = next(item for item in rows if item["contract"] == "DEMO-110C")
        self.assertAlmostEqual(row["entry_ask"], .8)
        self.assertAlmostEqual(row["close_bid"], 4.95)
        self.assertAlmostEqual(row["open_to_close_return"], 4.95 / .8 - 1)
        self.assertGreaterEqual(row["max_return"], row["open_to_close_return"])
        self.assertTrue(row["sampled_path"])

    def test_end_of_day_does_not_fabricate_without_a_valid_exit_bid(self):
        self.run_frames(lambda s: replace(s, options=tuple(replace(o, bid=0) for o in s.options)))
        rows = self.store.end_of_day_options("2026-09-21")
        self.assertTrue(rows)
        self.assertTrue(all(row["close_bid"] == 0 for row in rows))


if __name__ == "__main__":
    unittest.main()
