import json
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from lotto.config import ATR_BASIS_VERSION, Settings
from lotto.demo import demo_frames
from lotto.engine import Engine
from lotto.features import candidates
from lotto.models import ET
from lotto.patterns import dead_market_metrics
from lotto.schwab import Schwab
from lotto.store import Store
from tests.test_dead_market import flat_snapshot


class AtrBasisTests(unittest.TestCase):
    def test_true_range_includes_the_gap_and_high_low_does_not(self):
        client = Schwab.__new__(Schwab)
        client.baselines, client.calendar_cache = {}, {}
        today = datetime(2026, 11, 6, 10, 0, tzinfo=ET)
        prior = [(today - timedelta(days=k)).date() for k in range(16, 0, -1)]
        candles = []
        px = 100.0
        for day in prior:
            px += 3.0
            start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
            for i in range(2):
                candles.append({"datetime": (start + timedelta(minutes=i)).timestamp() * 1000,
                                "open": px, "high": px + 1, "low": px - 1, "close": px, "volume": 100})
        # Today's range must not enter either average.
        huge = today.replace(hour=9, minute=30)
        candles.append({"datetime": huge.timestamp() * 1000, "open": 100, "high": 500, "low": 1, "close": 400, "volume": 100})
        client.candles = Mock(return_value={"candles": candles})
        client.baseline("TEST", today)
        key = ("TEST", today.date())
        self.assertAlmostEqual(client.hl_atrs[key], 2.0)
        self.assertAlmostEqual(client.atrs[key], 4.0)
        meta = client.atr_meta[key]
        expected = [day.isoformat() for day in prior[-14:]]
        self.assertEqual(meta["hl_sessions"], expected)
        self.assertEqual(meta["tr_sessions"], expected)
        self.assertNotIn(today.date().isoformat(), meta["hl_sessions"])
        self.assertEqual(meta["version"], ATR_BASIS_VERSION)
        self.assertLess(meta["hl"], meta["tr"])

    def test_high_low_basis_scales_and_falls_back_without_flipping_the_default(self):
        base = flat_snapshot()
        compared = replace(base, prior_atr=2.0, prior_hl_atr=1.0)
        tr = dead_market_metrics(compared, 1, "tr")
        hl = dead_market_metrics(compared, 1, "hl")
        self.assertAlmostEqual(hl["vwap_abs_atr"], 2 * tr["vwap_abs_atr"])
        self.assertEqual(hl["atr_basis_used"], "hl")
        self.assertEqual(tr["atr_fallback"], 0)
        fallback = dead_market_metrics(replace(compared, prior_hl_atr=None), 1, "hl")
        self.assertEqual(fallback["atr_basis_used"], "tr")
        self.assertEqual(fallback["atr_fallback"], 1)
        self.assertAlmostEqual(fallback["vwap_abs_atr"], tr["vwap_abs_atr"])
        self.assertIsNone(dead_market_metrics(replace(base, prior_atr=None), 1)["net30_atr"])

    def test_modes_and_env(self):
        self.assertEqual(Settings().dm_atr_basis_mode, 1)
        self.assertEqual((Settings().dead_zone_mode, Settings().burst_flat_mode, Settings().spent_mode), (1, 1, 1))
        with self.assertRaises(ValueError):
            Settings(dm_atr_basis_mode=3)
        from lotto.main import env_settings
        with patch.dict(os.environ, {"LOTTO_DM_ATR_BASIS_MODE": "0"}):
            self.assertEqual(env_settings().dm_atr_basis_mode, 0)

    def test_shadow_logs_both_bases_and_does_not_change_blockers_or_posts(self):
        frames = list(demo_frames())
        history = [replace(frame[0], prior_hl_atr=frame[0].prior_atr * .8) for frame in frames[15:30]]
        snap = replace(frames[30][0], prior_hl_atr=frames[30][0].prior_atr * .8)
        off = candidates(snap, history, Settings(dm_atr_basis_mode=0, far_otm_time_mode=0))
        shadow = candidates(snap, history, Settings(dm_atr_basis_mode=1, far_otm_time_mode=1))
        blocking = candidates(snap, history, Settings(dm_atr_basis_mode=2, far_otm_time_mode=1))
        self.assertTrue(off and shadow and blocking)
        self.assertEqual([item.blockers for item in off], [item.blockers for item in shadow])
        self.assertTrue(all("dmhl_dead_zone" in item.metrics for item in shadow))
        self.assertTrue(all(item.metrics["dm_atr_basis"] == "tr" for item in shadow))
        self.assertTrue(all(item.metrics["provenance"]["atr"]["blocking_basis"] == "tr" for item in shadow))
        self.assertFalse(any("dmhl_dead_zone" in item.metrics for item in off))
        self.assertTrue(all(item.metrics["dm_atr_basis"] == "hl" for item in blocking))
        self.assertTrue(all(abs(after.metrics["dm_vwap_abs_atr"] - before.metrics["dm_vwap_abs_atr"] / .8) < 1e-9
                            for before, after in zip(off, blocking)
                            if before.metrics["dm_vwap_abs_atr"] is not None))
        counts = []
        stamped = None
        for settings in (Settings(far_otm_time_mode=0, dm_atr_basis_mode=0), Settings()):
            store = Store(":memory:")
            self.addCleanup(store.db.close)
            engine = Engine(store, settings)
            counts.append(sum(len(engine.process(frame)) for frame in frames))
            if settings.dm_atr_basis_mode == 1:
                row = store.db.execute("SELECT features FROM candidate_evaluations LIMIT 1").fetchone()
                stamped = json.loads(row[0])
                blocked = [json.loads(item[0]).get("blockers", [])
                           for item in store.db.execute("SELECT features FROM candidate_evaluations")]
                self.assertFalse(any("too far for the time left" in blocker or "dead zone" in blocker
                                     for blockers in blocked for blocker in blockers))
        self.assertEqual(counts[0], counts[1])
        self.assertEqual(stamped["metrics"]["provenance"]["atr"]["blocking_basis"], "tr")
        self.assertEqual(stamped["metrics"]["provenance"]["far_otm"]["mode"], 1)
        self.assertIn("bid", stamped["metrics"]["provenance"]["quotes"])
        self.assertEqual(stamped["metrics"]["provenance"]["quotes"]["side"], "CALL")


if __name__ == "__main__":
    unittest.main()
