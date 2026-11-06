import os
import unittest
from unittest.mock import patch
from dataclasses import replace
from datetime import datetime, timedelta

from lotto.config import Settings
from lotto.demo import demo_frames
from lotto.engine import Engine
from lotto.features import candidates
from lotto.models import ET, Bar, Snapshot
from lotto.patterns import dead_market_metrics
from lotto.store import Store


def flat_snapshot(direction=1, burst=False):
    """Midday (12:00 ET) stock pinned to VWAP for 150 bars; optional last-5-bar volume burst."""
    start = datetime(2026, 11, 5, 9, 30, tzinfo=ET)
    bars = []
    for i in range(150):
        px = 100 + .01 * ((-1) ** i)
        vol = 5000 if (burst and i >= 145) else 1000
        bars.append(Bar(end=start + timedelta(minutes=i + 1), open=px, high=px + .02, low=px - .02, close=px, volume=vol))
    at = start + timedelta(minutes=150)
    return Snapshot(symbol="TEST", at=at, spot=bars[-1].close, spot_time=at, prior_close=100,
                    bars=tuple(bars), options=(), prior_atr=2.0)


class DeadMarketTests(unittest.TestCase):
    def test_flat_midday_is_mirrored(self):
        for direction in (1, -1):
            d = dead_market_metrics(flat_snapshot(direction), direction)
            self.assertEqual(d["session_minute"], 150)
            self.assertLess(d["net30_atr"], Settings().dead_zone_max_net30_atr)
            self.assertLess(d["vwap_abs_atr"], Settings().dead_zone_max_vwap_atr)

    def test_burst_ratio(self):
        d = dead_market_metrics(flat_snapshot(burst=True), 1)
        self.assertGreaterEqual(d["volume_ratio_5_25"], Settings().burst_flat_min_volume_ratio)
        self.assertLess(d["net5_atr"], Settings().burst_flat_max_net5_atr)

    def test_missing_atr_never_blocks(self):
        d = dead_market_metrics(replace(flat_snapshot(), prior_atr=None), 1)
        self.assertIsNone(d["net30_atr"])

    def test_modes_validated(self):
        with self.assertRaises(ValueError):
            Settings(dead_zone_mode=3)

    def test_shadow_mode_does_not_change_demo_alerts(self):
        frames = list(demo_frames())
        counts = []
        for mode in (0, 1):
            store = Store(":memory:"); self.addCleanup(store.db.close)
            s = Settings(dead_zone_mode=mode, burst_flat_mode=mode, spent_mode=mode)
            engine = Engine(store, s)
            counts.append(sum(len(engine.process(frame)) for frame in frames))
        self.assertEqual(counts[0], counts[1])

    def test_block_mode_adds_blocker_when_flagged(self):
        frames = list(demo_frames())
        history = [f[0] for f in frames[15:30]]
        snap = frames[30][0]
        loose = Settings(dead_zone_mode=2, dead_zone_start_minute=0, dead_zone_end_minute=389,
                         dead_zone_max_net30_atr=1e9, dead_zone_max_vwap_atr=1e9)
        blocked = candidates(snap, history, loose)
        self.assertTrue(blocked)
        self.assertTrue(all("dead zone: flat 30m hugging VWAP (11:00-14:30)" in c.blockers for c in blocked))
        shadow = candidates(snap, history, replace(loose, dead_zone_mode=1))
        self.assertTrue(all(c.metrics["dm_dead_zone"] == 1 for c in shadow))
        self.assertFalse(any("dead zone" in b for c in shadow for b in c.blockers))


class DeadMarketConfigTests(unittest.TestCase):
    def test_shadow_safe_defaults(self):
        s = Settings()
        # Merge-safe shadow. Live block is env-only: LOTTO_DEAD_ZONE_MODE=2, LOTTO_BURST_FLAT_MODE=2.
        self.assertEqual((s.dead_zone_mode, s.burst_flat_mode, s.spent_mode), (1, 1, 1))
        self.assertIs(type(s.dead_zone_mode), int)
        self.assertIs(type(s.burst_flat_mode), int)
        self.assertIs(type(s.spent_mode), int)
        self.assertEqual((s.dead_zone_start_minute, s.dead_zone_end_minute), (90, 300))
        self.assertAlmostEqual(s.dead_zone_max_net30_atr, .12)
        self.assertAlmostEqual(s.dead_zone_max_vwap_atr, .226)
        self.assertAlmostEqual(s.burst_flat_min_volume_ratio, 1.268)
        self.assertAlmostEqual(s.burst_flat_max_net5_atr, .116)

    def test_env_toggles(self):
        from lotto.main import env_settings
        with patch.dict(os.environ, {"LOTTO_DEAD_ZONE_MODE": "0", "LOTTO_BURST_FLAT_MODE": "1", "LOTTO_SPENT_MODE": "2",
                                     "LOTTO_DEAD_ZONE_MAX_NET30_ATR": "0.1"}):
            s = env_settings()
        self.assertEqual((s.dead_zone_mode, s.burst_flat_mode, s.spent_mode), (0, 1, 2))
        self.assertAlmostEqual(s.dead_zone_max_net30_atr, .1)


if __name__ == "__main__":
    unittest.main()
