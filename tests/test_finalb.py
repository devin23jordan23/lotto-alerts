"""FINAL-B rules, shadow isolation, and the c2c597d byte-identical regression."""
import csv
import json
import os
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from lotto.config import Settings
from lotto.demo import demo_frames
from lotto.discovery import Discovery
from lotto.engine import Engine
from lotto.features import Candidate, candidates
from lotto.finalb import (
    AFTER14_REASON,
    CHASE_REASON,
    LEVEL_SIDE_REASON,
    after14_passes,
    alt_discovery_score,
    chase_blocked,
    extension_penalty,
    finalb_cooldown_blocks,
    fresh_box_break,
    fresh_extreme_break,
    level_side_passes,
    session_extreme,
)
from lotto.main import env_settings
from lotto.models import ET, Bar, Snapshot
from lotto.patterns import Setup
from lotto.store import Store
from scripts.finalb_shadow_report import session_rows

GOLDEN = Path(__file__).parent / "fixtures" / "c2c597d_demo_golden.json"
MODES = {
    "level_side_rule_mode": 1,
    "level_break_override_mode": 1,
    "cooldown_mode": 1,
    "after14_mode": 1,
    "chase_block_mode": 1,
    "ext_penalty_mode": 1,
    "discovery_priority_mode": 1,
}


def load_golden():
    return json.loads(GOLDEN.read_text())


def decision_rows(store):
    return [dict(row) for row in store.db.execute(
        "SELECT symbol, at, state, reason, score FROM decisions ORDER BY at, symbol, reason")]


def run_demo(settings):
    frames = list(demo_frames())
    store = Store(":memory:")
    engine = Engine(store, settings)
    alerts = []
    for frame in frames:
        alerts.extend(engine.process(frame))
    return store, engine, alerts


def flat_bars(n, price=100, volume=1000, high_extra=0.05, low_extra=0.05, start=None):
    start = start or datetime(2026, 11, 17, 9, 30, tzinfo=ET)
    bars = []
    for i in range(n):
        end = start + timedelta(minutes=i + 1)
        bars.append(Bar(end, price, price + high_extra, price - low_extra, price, volume, 10000))
    return tuple(bars)


def snap_from(bars, symbol="AAPL", spot=None, at=None, **kwargs):
    at = at or bars[-1].end
    spot = bars[-1].close if spot is None else spot
    base = dict(symbol=symbol, at=at, spot=spot, spot_time=at, prior_close=100, bars=bars, options=(),
                prior_atr=2.0, context_return_5m=0.0, context_time=at, context_label="SMH")
    base.update(kwargs)
    return Snapshot(**base)


class FinalBConfigTests(unittest.TestCase):
    def test_defaults_are_off_and_env_readable(self):
        settings = Settings()
        self.assertEqual((settings.level_side_rule_mode, settings.level_break_override_mode,
                          settings.cooldown_mode, settings.after14_mode, settings.chase_block_mode,
                          settings.ext_penalty_mode, settings.discovery_priority_mode), (0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(settings.level_break_min_local_rvol, 1.5)
        self.assertEqual((settings.extreme_rearm_min_minutes, settings.ext_penalty_points), (5, 15))
        self.assertAlmostEqual(settings.extreme_rearm_atr, 0.05)
        env = {f"LOTTO_{name.upper()}": "1" for name in MODES}
        env["LOTTO_LEVEL_BREAK_MIN_LOCAL_RVOL"] = "1.5"
        env["LOTTO_EXTREME_REARM_MIN_MINUTES"] = "5"
        env["LOTTO_EXTREME_REARM_ATR"] = "0.05"
        env["LOTTO_EXT_PENALTY_POINTS"] = "15"
        with patch.dict(os.environ, env):
            loaded = env_settings()
        for name in MODES:
            self.assertEqual(getattr(loaded, name), 1)
            self.assertIs(type(getattr(loaded, name)), int)

    def test_modes_reject_anything_but_zero_one_two(self):
        with self.assertRaises(ValueError):
            Settings(level_side_rule_mode=3)
        with self.assertRaises(ValueError):
            Settings(chase_block_mode=True)
        with self.assertRaises(ValueError):
            Settings(level_break_min_local_rvol=0)
        with self.assertRaises(ValueError):
            Settings(ext_penalty_points=-1)


class LevelSideTests(unittest.TestCase):
    def setUp(self):
        self.frames = list(demo_frames())
        self.history = [frame[0] for frame in self.frames[:10]]
        self.snap = self.frames[10][0]

    def test_missing_levels_pass_and_or_of_present_levels(self):
        self.assertTrue(level_side_passes(self.snap, "CALL"))
        above = replace(self.snap, prior_high=self.snap.spot + 50, premarket_high=None)
        self.assertFalse(level_side_passes(above, "CALL"))
        either = replace(self.snap, prior_high=self.snap.spot + 50, premarket_high=self.snap.spot - 1)
        self.assertTrue(level_side_passes(either, "CALL"))
        both = replace(self.snap, prior_low=self.snap.spot - 50, premarket_low=self.snap.spot - 40)
        self.assertFalse(level_side_passes(both, "PUT"))
        touched = replace(self.snap, prior_high=self.snap.spot, premarket_high=self.snap.spot + 1)
        self.assertFalse(level_side_passes(touched, "CALL"))

    def test_mode_two_blocks_and_shadow_does_not(self):
        blocked_levels = replace(self.snap, prior_high=self.snap.spot + 10, premarket_high=self.snap.spot + 10)
        live = candidates(blocked_levels, self.history, Settings())
        self.assertTrue(any(item.qualifying for item in live))
        hard = candidates(blocked_levels, self.history, Settings(level_side_rule_mode=2))
        self.assertTrue(hard)
        self.assertTrue(all(LEVEL_SIDE_REASON in item.blockers for item in hard))
        self.assertFalse(any(item.qualifying for item in hard))
        shadow = candidates(blocked_levels, self.history, Settings(level_side_rule_mode=1))
        self.assertTrue(any(item.qualifying for item in shadow))
        self.assertTrue(all(item.finalb["flags"]["level_side"] == 1 for item in shadow))
        self.assertFalse(any(LEVEL_SIDE_REASON in item.blockers for item in shadow))
        self.assertFalse(any(item.finalb["qualifying"] for item in shadow))


class LevelBreakOverrideTests(unittest.TestCase):
    def setUp(self):
        self.frames = list(demo_frames())

    def test_frees_only_score_and_volume_and_only_when_on(self):
        history = [frame[0] for frame in self.frames[:5]]
        snap = self.frames[5][0]
        self.assertTrue(fresh_extreme_break(snap, "CALL"))
        held = candidates(snap, history, Settings(min_score=99))
        self.assertEqual(held[0].blockers, ("score below threshold",))
        self.assertFalse(held[0].qualifying)
        freed = candidates(snap, history, Settings(min_score=99, level_break_override_mode=2))
        self.assertTrue(freed[0].qualifying)
        self.assertNotIn("score below threshold", freed[0].blockers)
        shadow = candidates(snap, history, Settings(min_score=99, level_break_override_mode=1))
        self.assertFalse(shadow[0].qualifying)
        self.assertEqual(shadow[0].blockers, ("score below threshold",))
        self.assertEqual(shadow[0].finalb["flags"]["level_break_override"], 1)
        self.assertTrue(shadow[0].finalb["qualifying"])
        quiet = candidates(snap, history, Settings(min_score=99, level_break_override_mode=2,
                                                   level_break_min_local_rvol=10))
        self.assertFalse(quiet[0].qualifying)
        self.assertIn("score below threshold", quiet[0].blockers)

    def test_other_blocker_keeps_the_card_blocked(self):
        history = [frame[0] for frame in self.frames[15:30]]
        snap = self.frames[30][0]
        held = candidates(snap, history, Settings(min_score=99, level_break_override_mode=2))
        self.assertIn("too far from price trigger", held[0].blockers)
        self.assertIn("score below threshold", held[0].blockers)
        self.assertEqual(held[0].finalb["flags"]["level_break_override"], 0)


class CooldownTests(unittest.TestCase):
    def test_extreme_rearm_math(self):
        bars = flat_bars(10, price=100)
        snap = snap_from(bars, spot=100, prior_atr=2)
        last = {"at": snap.at - timedelta(minutes=6), "side": "CALL", "extreme": 100, "phase": "ACTIVE"}
        blocked, rearm = finalb_cooldown_blocks(snap.at, last, "CALL", False, snap, Settings(cooldown_mode=2))
        self.assertTrue(blocked)
        self.assertFalse(rearm)
        extended = snap_from(bars, spot=100.2, prior_atr=2)
        # 0.05 * 2 = 0.10; spot 100.2 is 0.20 through the stored extreme.
        blocked, rearm = finalb_cooldown_blocks(extended.at, last, "CALL", False, extended, Settings())
        self.assertFalse(blocked)
        self.assertTrue(rearm)
        too_soon = snap_from(bars, spot=100.2, prior_atr=2, at=bars[-1].end)
        early = {**last, "at": too_soon.at - timedelta(minutes=4)}
        blocked, rearm = finalb_cooldown_blocks(too_soon.at, early, "CALL", False, too_soon, Settings())
        self.assertTrue(blocked)
        self.assertFalse(rearm)

    def test_mode_two_refires_and_shadow_does_not_touch_live_cooldown(self):
        frames = list(demo_frames())

        def always(snap, history, settings):
            return [Candidate(snap, snap.options[0], 90.0, {"price_structure": 20.0},
                              {"opening_expansion": 0, "local_rvol_5m": 1.0},
                              ["Stock and options aligned"], True,
                              setup=Setup("CONTINUATION", snap.spot, snap.spot - 1))]

        def fire(mode, indexes):
            store = Store(":memory:")
            engine = Engine(store, Settings(cooldown_mode=mode))
            alerts = []
            with patch("lotto.engine.candidates", side_effect=always):
                for index in indexes:
                    alerts.extend(engine.process(frames[index]))
            return store, engine, alerts

        first = [20, 21]
        later = [26, 27]  # six minutes after the 09:52 alert; demo trend extends HOD
        live_store, live_engine, live_alerts = fire(0, first + later)
        with self.assertLogs("lotto.engine", level="INFO") as logs:
            shadow_store, shadow_engine, shadow_alerts = fire(1, first + later)
        hard_store, hard_engine, hard_alerts = fire(2, first + later)
        self.assertTrue(any("FINAL-B would fire" in line and "DEMO" in line for line in logs.output))
        self.assertEqual(len(live_alerts), 1)
        self.assertEqual(len(shadow_alerts), 1)
        self.assertEqual([row["symbol"] for row in live_store.summary()], ["DEMO"])
        self.assertEqual(len(shadow_store.summary()), 1)
        self.assertGreaterEqual(len(hard_alerts), 2)
        extreme = hard_store.db.execute("SELECT session_extreme FROM alerts ORDER BY at").fetchall()
        self.assertTrue(all(row[0] is not None for row in extreme))
        untouched = live_store.db.execute("SELECT session_extreme FROM alerts").fetchone()[0]
        self.assertIsNone(untouched)
        self.assertEqual(live_engine.rearmed, shadow_engine.rearmed)
        self.assertTrue(shadow_store.db.execute("SELECT COUNT(*) FROM finalb_shadow").fetchone()[0] > 0)
        self.assertEqual(live_store.db.execute("SELECT COUNT(*) FROM finalb_shadow").fetchone()[0], 0)
        fired = shadow_store.db.execute(
            "SELECT finalb_decision, rule_flags FROM finalb_shadow WHERE ts=?",
            (frames[27][0].at.isoformat(),)).fetchone()
        self.assertEqual(fired["finalb_decision"], "pass")
        self.assertEqual(json.loads(fired["rule_flags"])["extreme_rearm"], 1)


class After14Tests(unittest.TestCase):
    def test_exempt_and_clock_and_three_requirements(self):
        bars = []
        start = datetime(2026, 11, 17, 9, 30, tzinfo=ET)
        for i in range(80):
            end = start + timedelta(minutes=i + 1)
            high = 200 if i == 0 else 100.05
            close = 101 if i >= 75 else 100
            volume = 4000 if i >= 77 else 1000
            bars.append(Bar(end, 100, high, 99.9, close, volume, 10000))
        bars = tuple(bars)
        afternoon = bars[-1].end.replace(hour=14, minute=5)
        stock = snap_from(bars, at=afternoon, spot=101, context_return_5m=0.001, context_time=afternoon)
        from lotto.finalb import fresh_close_extreme
        self.assertFalse(fresh_close_extreme(stock, "CALL"))
        self.assertTrue(fresh_box_break(stock, "CALL"))
        self.assertTrue(after14_passes(stock, "CALL", Settings()))
        self.assertFalse(after14_passes(replace(stock, context_return_5m=0.0), "CALL", Settings()))
        self.assertTrue(after14_passes(replace(stock, symbol="SPY", context_return_5m=0.0), "CALL", Settings()))
        morning = snap_from(bars, at=bars[-1].end, spot=101, context_return_5m=0.0)
        self.assertTrue(after14_passes(morning, "CALL", Settings()))
        flat = snap_from(flat_bars(40), at=afternoon, spot=100, context_return_5m=0.0, context_time=afternoon)
        self.assertFalse(after14_passes(flat, "CALL", Settings()))
        shift = timedelta(hours=5)
        history = [self._shift(frame[0], shift) for frame in list(demo_frames())[:10]]
        current = self._shift(list(demo_frames())[10][0], shift)
        self.assertGreaterEqual(current.at.astimezone(ET).hour, 14)
        blocked = candidates(current, history, Settings(after14_mode=2))
        self.assertTrue(blocked)
        self.assertTrue(all(AFTER14_REASON in item.blockers for item in blocked))
        logged = candidates(current, history, Settings(after14_mode=1))
        self.assertTrue(logged)
        self.assertTrue(all(item.finalb["flags"]["after14"] == 1 for item in logged))
        self.assertFalse(any(AFTER14_REASON in item.blockers for item in logged))
        self.assertEqual([item.blockers for item in logged], [item.live_blockers for item in logged])

    def _shift(self, snap, delta):
        bars = tuple(replace(bar, end=bar.end + delta) for bar in snap.bars)
        options = tuple(replace(option, quote_time=option.quote_time + delta) for option in snap.options)
        return replace(
            snap, at=snap.at + delta, spot_time=snap.spot_time + delta, bars=bars, options=options,
            symbol="AAPL", context_return_5m=0.0,
            context_time=(snap.context_time + delta) if snap.context_time else None,
            market_return_5m=0.0,
            market_time=(snap.market_time + delta) if snap.market_time else None,
            session_end=(snap.session_end + delta) if snap.session_end else None)


class ChaseTests(unittest.TestCase):
    def test_straight_leg_blocks_and_a_pullback_does_not(self):
        start = datetime(2026, 11, 17, 9, 30, tzinfo=ET)
        bars = []
        for i in range(30):
            end = start + timedelta(minutes=i + 1)
            price = 100 + i * 0.05
            bars.append(Bar(end, price, price + 0.02, price - 0.01, price + 0.02, 1000, 10000))
        straight = snap_from(tuple(bars), spot=bars[-1].close, prior_atr=1)
        self.assertTrue(chase_blocked(straight, "CALL"))
        pull = list(bars)
        dip = 100 + 10 * 0.05
        pull[15] = Bar(pull[15].end, dip, dip + 0.02, dip - 0.40, dip, 1000, 10000)
        paused = snap_from(tuple(pull), spot=bars[-1].close, prior_atr=1)
        self.assertFalse(chase_blocked(paused, "CALL"))
        self.assertFalse(chase_blocked(replace(straight, prior_atr=None), "CALL"))
        down = []
        for i in range(30):
            end = start + timedelta(minutes=i + 1)
            price = 110 - i * 0.05
            down.append(Bar(end, price, price + 0.01, price - 0.02, price - 0.02, 1000, 10000))
        put = snap_from(tuple(down), spot=down[-1].close, prior_atr=1)
        self.assertTrue(chase_blocked(put, "PUT"))
        self.assertFalse(chase_blocked(put, "CALL"))

    def test_mode_two_adds_the_blocker_and_shadow_does_not(self):
        start = datetime(2026, 11, 17, 9, 30, tzinfo=ET)
        bars = []
        for i in range(30):
            end = start + timedelta(minutes=i + 1)
            price = 100 + i * 0.05
            bars.append(Bar(end, price, price + 0.02, price - 0.01, price + 0.02, 1000, 10000))
        base = list(demo_frames())[10][0]
        snap = replace(base, bars=tuple(bars), spot=bars[-1].close, prior_atr=1, at=bars[-1].end, spot_time=bars[-1].end)
        # The chase predicate is what the card check uses; the demo chain may not qualify on these bars.
        self.assertTrue(chase_blocked(snap, "CALL"))
        overlay_settings = Settings(chase_block_mode=2)
        from lotto.finalb import finalb_overlay
        blocked = finalb_overlay(snap, "CALL", 80, (), {"local_rvol_5m": 1, "pace_rvol": 3,
                                                        "options_window_minutes": 5, "atr_available": 1},
                                 snap.options[0], None, overlay_settings, min_mode=2)
        self.assertIn(CHASE_REASON, blocked["blockers"])
        shadow = finalb_overlay(snap, "CALL", 80, (), {"local_rvol_5m": 1, "pace_rvol": 3,
                                                       "options_window_minutes": 5},
                                snap.options[0], None, Settings(chase_block_mode=1), min_mode=1)
        self.assertIn(CHASE_REASON, shadow["blockers"])
        self.assertEqual(shadow["flags"]["chase_block"], 1)


class ExtPenaltyTests(unittest.TestCase):
    def test_penalty_subtracts_only_when_all_three_conditions_hold(self):
        start = datetime(2026, 11, 17, 9, 30, tzinfo=ET)
        bars = []
        for i in range(20):
            end = start + timedelta(minutes=i + 1)
            price = 100 + i * 0.5
            bars.append(Bar(end, price, price + 0.1, price - 0.05, price + 0.1, 1000, 10000))
        snap = snap_from(tuple(bars), spot=bars[-1].close, prior_atr=4)
        self.assertTrue(extension_penalty(snap, "CALL", {"local_rvol_5m": 2.0}))
        self.assertFalse(extension_penalty(snap, "CALL", {"local_rvol_5m": 1.9}))
        self.assertFalse(extension_penalty(replace(snap, prior_atr=None), "CALL", {"local_rvol_5m": 3}))
        frames = list(demo_frames())
        history = [frame[0] for frame in frames[:19]]
        current = frames[19][0]
        live = candidates(current, history, Settings())
        hard = candidates(current, history, Settings(ext_penalty_mode=2))
        shadow = candidates(current, history, Settings(ext_penalty_mode=1))
        self.assertTrue(live and hard and shadow)
        self.assertAlmostEqual(hard[0].score, round(live[0].score - 15, 2))
        self.assertEqual(shadow[0].score, live[0].score)
        self.assertAlmostEqual(shadow[0].finalb["score"], round(live[0].score - 15, 2))
        self.assertEqual(shadow[0].finalb["flags"]["ext_penalty"], 1)
        self.assertEqual(hard[0].parts["extension_penalty"], -15.0)
        self.assertNotIn("extension_penalty", shadow[0].parts)


class DiscoveryPriorityTests(unittest.TestCase):
    def test_shadow_rank_does_not_change_who_is_scanned(self):
        start = datetime(2026, 11, 17, 10, 0, tzinfo=ET)
        symbols = ["AAA", "BBB"]

        def quotes(at, aaa_price, aaa_high, volume):
            return {
                "AAA": {"price": aaa_price, "open": 100, "previous": 100, "volume": volume,
                        "at": at, "high": aaa_high, "low": 99},
                "BBB": {"price": 100, "open": 100, "previous": 100, "volume": 1000,
                        "at": at, "high": 100, "low": 99},
            }

        def selected(mode):
            discovery = Discovery(4)
            stamps = [start, start + timedelta(minutes=5), start + timedelta(minutes=10)]
            prices = [100, 101, 110]
            highs = [100, 101, 105]
            volumes = [1000, 2000, 4000]
            chosen = None
            for stamp, price, high, volume in zip(stamps, prices, highs, volumes):
                chosen = discovery.update(quotes(stamp, price, high, volume), stamp, set(symbols),
                                          priority_mode=mode)
            return chosen, discovery

        live_names, live = selected(0)
        shadow_names, shadow = selected(1)
        hard_names, hard = selected(2)
        self.assertEqual(live_names, shadow_names)
        self.assertEqual(live_names, hard_names)
        self.assertIsNone(live.finalb_shadow_report)
        row = next(item for item in shadow.finalb_shadow_report["rows"] if item["symbol"] == "AAA")
        self.assertGreater(row["finalb_score"], row["live_score"])
        self.assertEqual(row["delta"], 0)  # both names stay in the same order; the bonus does not reorder these two
        hard_row = next(item for item in hard.finalb_shadow_report["rows"] if item["symbol"] == "AAA")
        self.assertEqual(hard_row["finalb_score"], row["finalb_score"])
        # 5-minute return is present, so the extra points are the new-high acceleration bonus.
        self.assertAlmostEqual(row["finalb_score"] - row["live_score"], 30)

    def test_best_return_falls_back_to_shorter_windows(self):
        now = datetime(2026, 11, 17, 11, 0, tzinfo=ET)
        history = [{"at": now - timedelta(seconds=70), "price": 100, "high": 100, "low": 99}]
        score = alt_discovery_score(110, None, history, now, 110, 99, 0.1, 0.11, None, 0)
        self.assertAlmostEqual(score, 100 * (3 * 0.1 + 0.1 + 0.5 * 0.11))


class ShadowSafetyTests(unittest.TestCase):
    def test_mode_zero_matches_c2c597d_fixture(self):
        golden = load_golden()
        store, engine, alerts = run_demo(Settings())
        self.assertEqual([item["payload"] for item in alerts], golden["alert_payloads"])
        self.assertEqual([item["phase"] for item in alerts], golden["alert_phases"])
        self.assertEqual(decision_rows(store), golden["decisions"])
        self.assertEqual(store.summary(), golden["summary"])
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM finalb_shadow").fetchone()[0], 0)
        snap = list(demo_frames())[20][0]
        self.assertNotIn("spy_return_5m", snap.to_dict())
        self.assertNotIn("qqq_return_5m", snap.to_dict())

    def test_shadow_modes_do_not_change_cards_scores_or_discord(self):
        baseline_store, baseline_engine, baseline_alerts = run_demo(Settings())
        shadow_store, shadow_engine, shadow_alerts = run_demo(Settings(**MODES))
        self.assertEqual([item["payload"] for item in shadow_alerts], [item["payload"] for item in baseline_alerts])
        self.assertEqual(decision_rows(shadow_store), decision_rows(baseline_store))
        self.assertEqual(shadow_store.summary(), baseline_store.summary())
        self.assertEqual(shadow_engine.rearmed, baseline_engine.rearmed)
        self.assertEqual(shadow_engine.cool_since, baseline_engine.cool_since)
        self.assertGreater(shadow_store.db.execute("SELECT COUNT(*) FROM finalb_shadow").fetchone()[0], 0)
        logged = shadow_store.db.execute(
            "SELECT live_score, finalb_score, live_decision, contract FROM finalb_shadow WHERE side!='RANK' LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(logged["contract"])
        self.assertEqual(logged["live_decision"] in {"pass", "block"}, True)

    def test_shadow_error_is_swallowed(self):
        frames = list(demo_frames())
        store = Store(":memory:")
        engine = Engine(store, Settings(level_side_rule_mode=1, cooldown_mode=1))
        with patch("lotto.features.finalb_overlay", side_effect=RuntimeError("shadow blew up")), \
                patch("lotto.engine.finalb_overlay", side_effect=RuntimeError("shadow blew up")):
            alerts = []
            for frame in frames:
                alerts.extend(engine.process(frame))
        clean, _, clean_alerts = run_demo(Settings())
        self.assertEqual([item["payload"] for item in alerts], [item["payload"] for item in clean_alerts])
        self.assertEqual(decision_rows(store), decision_rows(clean))


class ShadowReportTests(unittest.TestCase):
    def test_csv_export_keeps_one_session(self):
        store = Store(":memory:")
        store.record_finalb_shadow([
            {"ts": "2026-11-17T14:01:00-05:00", "ticker": "NVDA", "side": "CALL",
             "live_decision": "block", "live_block_reasons": "[]", "finalb_decision": "pass",
             "finalb_block_reasons": "[]", "live_score": 80, "finalb_score": 80,
             "rule_flags": "{}", "cooldown_state": "{}", "contract": "NVDA-C"},
            {"ts": "2026-11-16T14:01:00-05:00", "ticker": "AMD", "side": "PUT",
             "live_decision": "pass", "live_block_reasons": "[]", "finalb_decision": "block",
             "finalb_block_reasons": "[]", "live_score": 70, "finalb_score": 55,
             "rule_flags": "{}", "cooldown_state": "{}", "contract": "AMD-P"},
        ])
        path = Path("finalb-test.sqlite3")
        self.addCleanup(path.unlink, missing_ok=True)
        # session_rows reads a file path. Dump the memory schema by backup.
        disk = sqlite3.connect(path)
        store.db.backup(disk)
        disk.close()
        rows = session_rows(path, "2026-11-17")
        self.assertEqual([row["ticker"] for row in rows], ["NVDA"])
        out = Path("finalb-shadow-2026-11-17.csv")
        self.addCleanup(out.unlink, missing_ok=True)
        from scripts.finalb_shadow_report import main
        with patch("sys.argv", ["finalb_shadow_report.py", "--db", str(path), "--day", "2026-11-17", "--out", str(out)]):
            main()
        with out.open() as handle:
            written = list(csv.DictReader(handle))
        self.assertEqual(written[0]["ticker"], "NVDA")
        self.assertEqual(written[0]["contract"], "NVDA-C")
        self.assertEqual(session_extreme(snap_from(flat_bars(3), spot=101), "CALL"), 101)


if __name__ == "__main__":
    unittest.main()
