import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch

from lotto.config import model_v_settings
from lotto.confluence import _level_break, apply_card_layers
from lotto.features import Candidate
from lotto.models import Bar, ET, Option, Snapshot
from lotto.patterns import Setup
from lotto.setup_confirmation import confirm_setup, ema_level_continuation, five_bar_tod_volume


class SetupConfirmationTests(unittest.TestCase):
    def snapshot(self, closes, *, side="CALL", actual=200, expected=100,
                 premarket_high=None, premarket_low=None, prior_high=None, prior_low=None):
        start = datetime(2026, 10, 28, 9, 30, tzinfo=ET)
        bars = []
        for index, close in enumerate(closes):
            opening = closes[index - 1] if index else close
            bars.append(Bar(start + timedelta(minutes=index + 1), opening,
                            max(opening, close) + .05, min(opening, close) - .05,
                            close, actual, (index + 1) * expected))
        at = bars[-1].end + timedelta(seconds=15)
        option = Option("TEST", date(2026, 10, 28), side, closes[-1], .9, 1.0,
                        200, 100, .3, .1, at - timedelta(seconds=2))
        return Snapshot("TEST", at, closes[-1], at - timedelta(seconds=2), closes[0],
                        tuple(bars), (option,), prior_atr=2,
                        premarket_high=premarket_high, premarket_low=premarket_low,
                        prior_high=prior_high, prior_low=prior_low)

    def test_ema_touch_without_directional_level_cannot_confirm(self):
        snap = self.snapshot([100] * 10 + [100.2, 100.5], prior_high=101.0)
        result = ema_level_continuation(snap, "CALL")
        self.assertFalse(result.ready)
        self.assertIn("level absent", result.reason)

    def test_ema_continuation_requires_reclaim_volume_and_renewed_move(self):
        snap = self.snapshot([101] * 10 + [101.02, 101.3], prior_high=100.5)
        self.assertEqual(five_bar_tod_volume(snap), 2.0)
        self.assertTrue(ema_level_continuation(snap, "CALL").ready)
        quiet = self.snapshot([101] * 10 + [101.02, 101.3], actual=70, prior_high=100.5)
        self.assertFalse(ema_level_continuation(quiet, "CALL").ready)
        stalled = self.snapshot([101] * 10 + [101.02, 101.03], prior_high=100.5)
        self.assertFalse(ema_level_continuation(stalled, "CALL").ready)

    def test_fresh_opening_range_break_is_independent_of_pdh_ema(self):
        snap = self.snapshot([100] * 5 + [100.2, 100.6], prior_high=105)
        setup = Setup("OPENING_BREAK", 100.4, 99.8)
        self.assertTrue(confirm_setup(snap, "CALL", setup).ready)
        self.assertFalse(ema_level_continuation(snap, "CALL").ready)

    def test_old_break_and_building_setup_do_not_post(self):
        snap = self.snapshot([100, 100.2, 100.7] + [100.8] * 6, prior_high=100.5)
        setup = Setup("LEVEL_RECLAIM", 100.5, 100)
        self.assertEqual(confirm_setup(snap, "CALL", setup).state, "expired")
        self.assertTrue(_level_break(snap, snap.bars, 1))
        self.assertFalse(_level_break(snap, snap.bars, 1, fresh_only=True))
        self.assertFalse(confirm_setup(snap, "CALL", replace(setup, building=True)).ready)

    def test_legacy_level_feature_stays_true_when_first_bar_closes_above_level(self):
        snap = self.snapshot([100.6] * 6, prior_high=100.5)
        self.assertTrue(_level_break(snap, snap.bars, 1))
        self.assertFalse(_level_break(snap, snap.bars, 1, fresh_only=True))

    def test_put_mirror_and_intrabar_cross(self):
        snap = self.snapshot([100] * 5 + [99.8, 99.4], side="PUT", prior_low=98)
        setup = Setup("OPENING_BREAK", 99.6, 100.2)
        self.assertTrue(confirm_setup(snap, "PUT", setup).ready)
        prior = self.snapshot([100] * 6, side="PUT")
        crossed = replace(prior, spot=99.4, prior_low=99.6)
        self.assertIsNotNone(confirm_setup(crossed, "PUT", Setup("LEVEL_RECLAIM", 99.6, 100)).event_at)

    def test_score_cannot_compensate_when_confirmation_enabled(self):
        snap = self.snapshot([100] * 6)
        def run(enabled):
            candidate = Candidate(snap, snap.options[0], 130, {}, {}, [], True, setup=None)
            cfg = model_v_settings(setup_confirmation_enabled=enabled)
            gate = {"post_ok": True, "blocked_by": [], "base": {}, "blockers": [],
                    "overrides": [], "spread_band": 0, "stock_bar_met": 1,
                    "optb_volume_waived": 1, "bar_tolerance_used": None}
            with patch("lotto.confluence.card_gate", return_value=gate), \
                 patch("lotto.confluence.additive_features", return_value={"_vol5": 100}), \
                 patch("lotto.model_v.strong_stock_parts", return_value={"rvol": 2, "push": 1, "setups": 1}), \
                 patch("lotto.weights.load_weights", return_value={}), \
                 patch("lotto.weights.score_path", return_value=({}, 130, 10)):
                apply_card_layers(candidate, cfg, False, 130)
            return candidate
        self.assertTrue(run(0).qualifying)
        blocked = run(1)
        self.assertFalse(blocked.qualifying)
        self.assertEqual(blocked.metrics["setup_confirmation_reason"], "no stock setup")
        self.assertIn("setup_unconfirmed", blocked.metrics["gate_blocked_by"])


if __name__ == "__main__":
    unittest.main()
