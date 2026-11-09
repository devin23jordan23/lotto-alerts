import os
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch

from lotto.config import Settings
from lotto.demo import demo_frames
from lotto.features import candidates
from lotto.models import ET, Bar, Option, Snapshot
from lotto.patterns import apply_research_gates, far_otm_time_metrics


def snap_at(hour, minute, spot=100.0, atr=2.0):
    start = datetime(2026, 11, 6, 9, 30, tzinfo=ET)
    at = datetime(2026, 11, 6, hour, minute, tzinfo=ET)
    count = int((at - start).total_seconds() // 60)
    bars = tuple(Bar(end=start + timedelta(minutes=i + 1), open=spot, high=spot + .1,
                     low=spot - .1, close=spot, volume=1000) for i in range(count))
    return Snapshot(symbol="TEST", at=at, spot=spot, spot_time=at, prior_close=spot,
                    bars=bars, options=(), prior_atr=atr)


CLOSE = datetime(2026, 11, 6, 16, 0, tzinfo=ET)


class FarOtmTimeTests(unittest.TestCase):
    def test_units_are_atr_and_calls_and_puts_mirror(self):
        call = far_otm_time_metrics(snap_at(10, 0), 105.0, date(2026, 11, 6), 1, CLOSE)
        put = far_otm_time_metrics(snap_at(10, 0), 95.0, date(2026, 11, 6), -1, CLOSE)
        # Five dollars on a 20-point ATR is 0.25 ATR, not a 5 percent moneyness figure.
        wide = far_otm_time_metrics(replace(snap_at(10, 0), prior_atr=20.0), 105.0, date(2026, 11, 6), 1, CLOSE)
        self.assertAlmostEqual(call["otm_atr"], 2.5)
        self.assertAlmostEqual(put["otm_atr"], call["otm_atr"])
        self.assertAlmostEqual(put["otm_time_atr"], call["otm_time_atr"])
        self.assertAlmostEqual(wide["otm_atr"], 0.25)
        self.assertNotAlmostEqual(wide["otm_atr"], 0.05)
        itm_call = far_otm_time_metrics(snap_at(15, 30), 98.0, date(2026, 11, 6), 1, CLOSE)
        itm_put = far_otm_time_metrics(snap_at(15, 30), 102.0, date(2026, 11, 6), -1, CLOSE)
        self.assertLess(itm_call["otm_time_atr"], 0)
        self.assertLess(itm_put["otm_time_atr"], 0)

    def test_time_scaling_and_the_friday_spy_726_put_dimensions(self):
        early = far_otm_time_metrics(snap_at(10, 0), 101.0, date(2026, 11, 6), 1, CLOSE)
        late = far_otm_time_metrics(snap_at(14, 45), 101.0, date(2026, 11, 6), 1, CLOSE)
        self.assertAlmostEqual(early["otm_atr"], .5)
        self.assertGreater(late["otm_time_atr"], early["otm_time_atr"])
        # Nov 6 12:38 ET SPY 726P. 9.91 points on an 8.9 high-low ATR is about 1.11 ATR.
        # 202 minutes remain, so the time-scaled value is about 1.55, above 0.8.
        hl = replace(snap_at(12, 38, spot=735.91, atr=8.9 * 1.22), prior_hl_atr=8.9)
        measured = far_otm_time_metrics(hl, 726.0, date(2026, 11, 6), -1, CLOSE)
        distance = 735.91 - 726.0
        self.assertEqual(measured["atr_basis"], "hl")
        self.assertAlmostEqual(measured["atr_value"], 8.9)
        self.assertAlmostEqual(measured["minutes_left"], 202)
        self.assertAlmostEqual(measured["otm_atr"], distance / 8.9)
        self.assertAlmostEqual(measured["otm_time_atr"], (distance / 8.9) / (202 / 390) ** .5)
        self.assertGreaterEqual(measured["otm_time_atr"], Settings().far_otm_time_max_atr)
        self.assertNotAlmostEqual(measured["otm_atr"], distance / (8.9 * 1.22))
        floored = far_otm_time_metrics(snap_at(15, 58), 101.0, date(2026, 11, 6), 1, CLOSE)
        self.assertAlmostEqual(floored["minutes_left"], 5)
        closed = far_otm_time_metrics(snap_at(16, 0), 110.0, date(2026, 11, 6), 1, CLOSE)
        self.assertIsNone(closed["otm_time_atr"])
        self.assertEqual(closed["missing"], "at_or_after_close")

    def test_missing_data_and_future_bars_do_not_flag(self):
        self.assertIsNone(far_otm_time_metrics(snap_at(14, 0), 110.0, date(2026, 11, 9), 1, CLOSE)["otm_time_atr"])
        self.assertEqual(far_otm_time_metrics(snap_at(14, 0), 110.0, date(2026, 11, 9), 1, CLOSE)["missing"], "not_0dte")
        missing = far_otm_time_metrics(snap_at(14, 0, atr=None), 110.0, date(2026, 11, 6), 1, CLOSE)
        self.assertIsNone(missing["otm_time_atr"])
        self.assertEqual(missing["missing"], "missing_atr")
        early = snap_at(14, 0, spot=100.0, atr=2.0)
        future = Bar(end=early.at + timedelta(minutes=30), open=100, high=180, low=100, close=180, volume=1)
        poisoned = replace(early, bars=early.bars + (future,))
        clean = far_otm_time_metrics(early, 102.0, date(2026, 11, 6), 1, CLOSE)
        dirty = far_otm_time_metrics(poisoned, 102.0, date(2026, 11, 6), 1, CLOSE)
        self.assertEqual(clean["otm_time_atr"], dirty["otm_time_atr"])
        option = Option("TEST", date(2026, 11, 6), "PUT", 90, .4, .5, 100, 10, -.3, .01, early.at)
        metrics = {"atr_available": 0, "session_range_atr": 0}
        gates = apply_research_gates(metrics, replace(early, prior_atr=None), option, -1,
                                     Settings(far_otm_time_mode=2), CLOSE)
        self.assertFalse(gates["far_otm"])
        self.assertEqual(metrics["provenance"]["far_otm"]["blocks"], 0)
        self.assertEqual(metrics["provenance"]["quotes"]["side"], "PUT")

    def test_modes_default_to_shadow_and_only_mode_2_blocks(self):
        self.assertEqual(Settings().far_otm_time_mode, 1)
        with self.assertRaises(ValueError):
            Settings(far_otm_time_mode=3)
        with self.assertRaises(ValueError):
            Settings(far_otm_time_max_atr=0)
        from lotto.main import env_settings
        with patch.dict(os.environ, {"LOTTO_FAR_OTM_TIME_MODE": "2", "LOTTO_FAR_OTM_TIME_MAX_ATR": "0.9"}):
            toggled = env_settings()
        self.assertEqual(toggled.far_otm_time_mode, 2)
        self.assertAlmostEqual(toggled.far_otm_time_max_atr, .9)
        frames = list(demo_frames())
        history = [frame[0] for frame in frames[15:30]]
        snap = frames[30][0]
        blocked = candidates(snap, history, Settings(far_otm_time_mode=2, far_otm_time_max_atr=1e-9))
        shadow = candidates(snap, history, Settings(far_otm_time_mode=1, far_otm_time_max_atr=1e-9))
        self.assertTrue(blocked)
        for item in blocked:
            self.assertEqual("0DTE strike too far for the time left" in item.blockers, item.metrics["ft_far_otm"] == 1)
            self.assertEqual(item.metrics["provenance"]["far_otm"]["blocks"], item.metrics["ft_far_otm"])
        self.assertFalse(any("0DTE strike too far" in blocker for item in shadow for blocker in item.blockers))
        self.assertEqual([item.metrics["ft_far_otm"] for item in blocked],
                         [item.metrics["ft_far_otm"] for item in shadow])


if __name__ == "__main__":
    unittest.main()
