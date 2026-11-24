"""Afternoon level rule: after 12:30 only completed HOD/LOD, PM, or PD breaks."""
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from lotto.config import Settings, recorded_settings
from lotto.demo import demo_frames
from lotto.engine import Engine
from lotto.features import Candidate
from lotto.main import env_settings
from lotto.models import ET, Bar, Option, Snapshot
from lotto.patterns import Setup
from lotto.scanner_fixes import AFTERNOON_CHECK, afternoon_level, apply_scanner_fixes
from lotto.store import Store


def _bars(closes, highs=None, lows=None, end=None):
    end = end or datetime(2026, 11, 24, 13, 0, tzinfo=ET)
    start = end - timedelta(minutes=len(closes))
    built = []
    for index, close in enumerate(closes):
        bar_end = start + timedelta(minutes=index + 1)
        high = close if highs is None else highs[index]
        low = close if lows is None else lows[index]
        built.append(Bar(bar_end, close, high, low, close, 1000, 10000))
    return tuple(built)


def _snap(bars, symbol="NVDA", spot=None, at=None, **levels):
    at = at or bars[-1].end
    spot = bars[-1].close if spot is None else spot
    return Snapshot(symbol, at, spot, at, 100, bars, (), prior_atr=2, **levels)


def _option(at):
    return Option("NVDA-C", at.date(), "CALL", 100, 0.4, 0.45, 200, 100, 0.3, 0.05, at)


def run_demo(settings):
    store = Store(":memory:")
    alerts = []
    engine = Engine(store, settings)
    for frame in demo_frames():
        alerts.extend(engine.process(frame))
    return store, alerts


class AfternoonFlagTests(unittest.TestCase):
    def test_validation_and_hash_exclusion(self):
        settings = Settings()
        self.assertEqual(settings.afternoon_level_mode, 0)
        self.assertEqual(settings.afternoon_level_start, "12:30")
        self.assertNotIn("afternoon_level_mode", recorded_settings(settings))
        self.assertNotIn("afternoon_level_mode", recorded_settings(Settings(afternoon_level_mode=1)))
        for bad in (3, True, False):
            with self.assertRaises(ValueError):
                Settings(afternoon_level_mode=bad)
        with self.assertRaises(ValueError):
            Settings(afternoon_level_start="25:00")
        with patch.dict(os.environ, {"LOTTO_AFTERNOON_LEVEL_MODE": "1", "LOTTO_AFTERNOON_LEVEL_START": "12:30"}):
            loaded = env_settings()
        self.assertEqual(loaded.afternoon_level_mode, 1)
        self.assertIs(type(loaded.afternoon_level_mode), int)

    def test_mode_one_matches_mode_zero_alerts_and_logs_the_bit(self):
        plain, plain_alerts = run_demo(Settings())
        flagged, flagged_alerts = run_demo(Settings(afternoon_level_mode=1))
        self.assertEqual([item["payload"] for item in flagged_alerts], [item["payload"] for item in plain_alerts])
        self.assertEqual(flagged.summary(), plain.summary())
        self.assertEqual(plain.db.execute("SELECT COUNT(*) FROM fix_shadow").fetchone()[0], 0)
        row = flagged.db.execute("SELECT fix_flags FROM fix_shadow LIMIT 1").fetchone()
        self.assertEqual(json.loads(row["fix_flags"])["al"]["al_block"], 0)


class AfternoonRuleTests(unittest.TestCase):
    def _midday(self, at_clock, symbol="NVDA", spot=None, **levels):
        closes = [100.0] * 12
        highs = [100.4] * 12
        lows = [99.6] * 12
        end = datetime(2026, 11, 24, at_clock[0], at_clock[1], tzinfo=ET)
        bars = _bars(closes, highs, lows, end)
        return _snap(bars, symbol=symbol, spot=spot if spot is not None else 100, at=end, **levels)

    def _apply(self, snap, mode, side="CALL"):
        checks = {}
        state = apply_scanner_fixes(snap, side, Setup("CONTINUATION", 100, 99), {}, checks,
                                    Settings(afternoon_level_mode=mode), None)
        return state, checks

    def test_before_1230_a_mid_range_card_is_allowed(self):
        snap = self._midday((12, 29))
        state, checks = self._apply(snap, 2)
        self.assertEqual(state["al_block"], 0)
        self.assertTrue(checks[AFTERNOON_CHECK])

    def test_at_1230_a_mid_range_card_is_blocked_and_an_index_is_not(self):
        snap = self._midday((12, 30))
        state, checks = self._apply(snap, 2)
        self.assertEqual(state["al_block"], 1)
        self.assertFalse(checks[AFTERNOON_CHECK])
        exempt, exempt_checks = self._apply(self._midday((14, 0), symbol="SPY"), 2)
        self.assertEqual(exempt["al_block"], 0)
        self.assertTrue(exempt_checks[AFTERNOON_CHECK])
        for symbol in ("QQQ", "IWM", "SPX"):
            self.assertEqual(afternoon_level(self._midday((13, 0), symbol=symbol), "CALL",
                                             Settings(afternoon_level_mode=2))["al_block"], 0)

    def test_hod_and_premarket_breaks_pass_and_a_forming_bar_does_not(self):
        end = datetime(2026, 11, 24, 13, 5, tzinfo=ET)
        hod_closes = [100.0] * 10 + [100.4, 101.0, 101.2]
        hod_highs = [100.2] * 10 + [100.5, 101.1, 101.3]
        hod = _snap(_bars(hod_closes, hod_highs, [99.5] * 13, end), at=end)
        state, checks = self._apply(hod, 2)
        self.assertEqual(state["al_hodlod"], 1)
        self.assertEqual(state["al_block"], 0)
        self.assertTrue(checks[AFTERNOON_CHECK])
        # Earlier wick is the session high. The last five closes cross PMH and stay there.
        pm_closes = [100.0] * 8 + [104.0, 104.2, 105.4, 105.6, 105.8]
        pm_highs = [110.0] + [100.3] * 7 + [104.1, 104.3, 105.5, 105.7, 105.9]
        pm = _snap(_bars(pm_closes, pm_highs, [99.0] * 13, end), at=end, premarket_high=105)
        pm_state, _ = self._apply(pm, 2)
        self.assertEqual(pm_state["al_hodlod"], 0)
        self.assertEqual(pm_state["al_pm"], 1)
        self.assertEqual(pm_state["al_block"], 0)
        pd = _snap(_bars(pm_closes, pm_highs, [99.0] * 13, end), at=end, prior_high=105)
        self.assertEqual(self._apply(pd, 2)[0]["al_pd"], 1)
        # Last completed close is still inside. A live spot through the high is the forming minute.
        flat = self._midday((13, 0), spot=110)
        forming, forming_checks = self._apply(flat, 2)
        self.assertGreater(flat.spot, max(bar.high for bar in flat.bars))
        self.assertEqual(forming["al_hodlod"], 0)
        self.assertEqual(forming["al_block"], 1)
        self.assertFalse(forming_checks[AFTERNOON_CHECK])
        shadow, shadow_checks = self._apply(flat, 1)
        self.assertEqual(shadow["al_block"], 1)
        self.assertNotIn(AFTERNOON_CHECK, shadow_checks)

    def test_shadow_row_records_would_block_without_a_new_table(self):
        end = datetime(2026, 11, 24, 13, 0, tzinfo=ET)
        snap = self._midday((13, 0))
        snap = Snapshot(snap.symbol, snap.at, snap.spot, snap.spot_time, snap.prior_close, snap.bars,
                        (_option(end),), prior_atr=2)
        store = Store(":memory:")
        engine = Engine(store, Settings(afternoon_level_mode=1))
        candidate = Candidate(snap, snap.options[0], 80, {}, {}, [], True, setup=Setup("PULLBACK_RESUMPTION", 100, 99))
        candidate.fix_state = afternoon_level(snap, "CALL", engine.settings)
        engine._log_fix_shadow(snap, [candidate])
        flags = json.loads(store.db.execute("SELECT fix_flags FROM fix_shadow").fetchone()[0])
        self.assertEqual(flags["al"]["al_block"], 1)
        self.assertEqual(store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='afternoon_level'").fetchone(), None)


if __name__ == "__main__":
    unittest.main()
