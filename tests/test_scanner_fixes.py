"""SCANNER-FIXES-1124: mode gates, parity, and F1-F5 / F4 / O1."""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from lotto.config import Settings, recorded_settings
from lotto.day_export import slice_session
from lotto.demo import demo_frames
from lotto.engine import Engine
from lotto.features import Candidate
from lotto.finalb import rearm_atr_measure, rearm_atr_variant
from lotto.main import env_settings
from lotto.models import ET, Bar, Option, Snapshot
from lotto.operations import HealthMonitor
from lotto.patterns import Setup
from lotto.scanner_fixes import (
    FAST_RUN_CHECK,
    MKT_CHECK,
    apply_scanner_fixes,
    fast_run,
    late_move,
    late_move_check,
    market_gate,
    mg_block,
    scanner_fix_lines,
)
from lotto.schwab import Schwab
from lotto.store import Store


def run_demo(settings):
    store = Store(":memory:")
    engine = Engine(store, settings)
    alerts = []
    for frame in demo_frames():
        alerts.extend(engine.process(frame))
    return store, alerts


def bars_from(closes, volumes=None, start=None, high_pad=0.01, low_pad=0.01):
    start = start or datetime(2026, 11, 24, 9, 30, tzinfo=ET)
    volumes = volumes or [1000] * len(closes)
    built = []
    for index, close in enumerate(closes):
        end = start + timedelta(minutes=index + 1)
        built.append(Bar(end, close, close + high_pad, close - low_pad, close, int(volumes[index]), 10000 + index))
    return tuple(built)


def snap_of(bars, symbol="TEST", spot=None, atr=1.0, **kwargs):
    at = bars[-1].end
    spot = bars[-1].close if spot is None else spot
    base = dict(symbol=symbol, at=at, spot=spot, spot_time=at, prior_close=100, bars=bars, options=(),
                prior_atr=atr, context_return_5m=0.0, context_time=at, context_label="SMH",
                session_end=at.replace(hour=16, minute=0))
    base.update(kwargs)
    return Snapshot(**base)


def option_for(at, symbol="TEST-C", side="CALL", delta=0.30, bid=0.40, ask=0.45):
    strike = 100
    return Option(symbol, at.date(), side, strike, bid, ask, 200, 100, delta if side == "CALL" else -abs(delta),
                  0.05, at)


def climb(minute, symbol="TEST", atr=5.0, step=0.02, with_option=True):
    """minute 30 is 10:00. A gentle new-high climb stays under the 1.1 ATR late-move cutoff."""
    closes = [100 + index * step for index in range(minute)]
    built = bars_from(closes, high_pad=0.02, low_pad=0.01)
    at = built[-1].end
    options = (option_for(at),) if with_option else ()
    return snap_of(built, symbol=symbol, spot=closes[-1], atr=atr, options=options,
                   premarket_high=99, prior_high=99, premarket_low=90, prior_low=90)


def aligned_bench(at, atr=10.0):
    """15-minute move of 0.10 ATR and a 5-minute move of 0.05 ATR. Boundary-aligned."""
    closes = [500.0] * 20
    closes[-16] = 500.0
    for index, price in enumerate((500.05, 500.10, 500.15, 500.20, 500.25, 500.30, 500.35, 500.40, 500.45, 500.50)):
        closes[-15 + index] = price
    closes[-5:] = [500.60, 500.70, 500.80, 500.90, 501.00]
    # Last 15 closes run from 500 to 501 (0.10 ATR). Last 5 run from 500.50 to 501 (0.05 ATR).
    start = at - timedelta(minutes=len(closes))
    spy = snap_of(bars_from(closes, start=start), symbol="SPY", spot=closes[-1], atr=atr)
    qqq_closes = [price / 500 * 400 for price in closes]
    qqq = snap_of(bars_from(qqq_closes, start=start), symbol="QQQ", spot=qqq_closes[-1], atr=8.0)
    return {"SPY": spy, "QQQ": qqq}


class ModeValidationTests(unittest.TestCase):
    def test_defaults_are_off_and_flags_are_not_hashed(self):
        settings = Settings()
        self.assertEqual(settings.late_move_mode, 0)
        self.assertEqual(settings.mkt_gate_mode, 0)
        self.assertEqual(settings.stock_first_mode, 0)
        self.assertEqual(settings.fast_run_mode, 0)
        self.assertEqual(settings.token_fallback_mode, 0)
        self.assertEqual(settings.late_move_override_mode, 1)
        self.assertEqual(settings.fast_run_mkt, "alone")
        self.assertEqual(settings.stock_first_cap, 2)
        self.assertAlmostEqual(settings.late_move_atr, 1.1)
        recorded = recorded_settings(settings)
        for key in ("late_move_mode", "late_move_override_mode", "mkt_gate_mode", "stock_first_mode",
                    "fast_run_mode", "fast_run_mkt", "token_fallback_mode", "rearm_atr_mode"):
            self.assertNotIn(key, recorded)
        self.assertIn("late_move_atr", recorded)
        self.assertNotIn("late_move_mode", recorded_settings(Settings(late_move_mode=1)))

    def test_modes_reject_anything_but_zero_one_two(self):
        for name in ("late_move_mode", "mkt_gate_mode", "stock_first_mode", "fast_run_mode", "token_fallback_mode"):
            with self.assertRaises(ValueError):
                Settings(**{name: 3})
            with self.assertRaises(ValueError):
                Settings(**{name: True})
            with self.assertRaises(ValueError):
                Settings(**{name: False})
        with self.assertRaises(ValueError):
            Settings(late_move_override_mode=2)
        with self.assertRaises(ValueError):
            Settings(late_move_override_mode=True)
        with self.assertRaises(ValueError):
            Settings(fast_run_mkt="both")
        with self.assertRaises(ValueError):
            Settings(stock_first_cap=True)
        with patch.dict(os.environ, {"LOTTO_LATE_MOVE_MODE": "2", "LOTTO_FAST_RUN_MKT": "alone",
                                      "LOTTO_MKT_GATE_MODE": "1"}):
            loaded = env_settings()
        self.assertEqual(loaded.late_move_mode, 2)
        self.assertIs(type(loaded.late_move_mode), int)
        self.assertEqual(loaded.fast_run_mkt, "alone")
        self.assertEqual(loaded.mkt_gate_mode, 1)
        with patch.dict(os.environ, {"LOTTO_LATE_MOVE_MODE": "true"}), self.assertRaises(ValueError):
            env_settings()

    def test_mode_one_matches_mode_zero_alerts(self):
        plain, plain_alerts = run_demo(Settings())
        flagged, flagged_alerts = run_demo(Settings(
            late_move_mode=1, mkt_gate_mode=1, stock_first_mode=1, fast_run_mode=1))
        self.assertEqual([item["payload"] for item in flagged_alerts], [item["payload"] for item in plain_alerts])
        self.assertEqual(flagged.summary(), plain.summary())
        self.assertEqual(plain.db.execute("SELECT COUNT(*) FROM fix_shadow").fetchone()[0], 0)
        self.assertGreater(flagged.db.execute("SELECT COUNT(*) FROM fix_shadow").fetchone()[0], 0)
        self.assertEqual(flagged.db.execute("SELECT COUNT(*) FROM stock_first_shadow").fetchone()[0], 0)
        plain_features = plain.db.execute("SELECT features FROM candidate_evaluations").fetchall()
        flagged_features = flagged.db.execute("SELECT features FROM candidate_evaluations").fetchall()
        self.assertNotEqual(plain_features, flagged_features)
        self.assertIn("lm_ext_atr", json.loads(flagged_features[-1][0])["metrics"])


class LateMoveTests(unittest.TestCase):
    def _extended(self, extension, atr=1.0, fresh=False, rvol=1.0, premarket=None):
        # LOD 100. Last close is LOD + extension * ATR. Last three bars print the high.
        last = 100 + extension * atr
        closes = [100.0] * 12 + [last - 0.05, last - 0.02, last]
        volumes = [100] * 12 + [100, int(100 * rvol), int(100 * rvol)]
        if fresh:
            # First close beyond the premarket high is inside the last five bars, and not bar 0.
            closes = [100.0] * 10 + [100.2, 100.4, last - 0.05, last - 0.02, last]
            volumes = [100] * 13 + [int(100 * rvol), int(100 * rvol)]
            # len 15. base is bars[-22:-2] == all but the last two, mostly 100.
        built = bars_from(closes, volumes, high_pad=0.0, low_pad=0.0)
        # Keep the low at 100 and put the high on the last bar.
        bars = list(built)
        end = bars[-1].end
        bars[-1] = Bar(end, last, last, min(last, 100), last, bars[-1].volume, 10000)
        return snap_of(tuple(bars), spot=last, atr=atr, premarket_high=premarket, prior_high=None)

    def test_new_hod_at_1_2_atr_is_blocked_at_mode_2(self):
        snap = self._extended(1.2)
        fields = late_move(snap, "CALL", Settings())
        self.assertGreaterEqual(fields["lm_ext_atr"], 1.1)
        self.assertEqual(fields["lm_hb"], 1)
        self.assertEqual(fields["lm_block"], 1)
        checks = {}
        apply_scanner_fixes(snap, "CALL", Setup("CONTINUATION", snap.spot, snap.spot - 1), {}, checks,
                            Settings(late_move_mode=2), None)
        self.assertIn(late_move_check(Settings()), checks)
        self.assertFalse(checks[late_move_check(Settings())])

    def test_fresh_premarket_break_on_1_6_rvol_is_not_blocked(self):
        snap = self._extended(1.2, fresh=True, rvol=1.6, premarket=100.1)
        fields = late_move(snap, "CALL", Settings())
        self.assertEqual(fields["lm_fresh_lvl"], 1)
        self.assertGreaterEqual(fields["lm_rv2"], 1.5)
        self.assertEqual(fields["lm_block"], 0)
        checks = {}
        apply_scanner_fixes(snap, "CALL", Setup("CONTINUATION", snap.spot, snap.spot - 1), {}, checks,
                            Settings(late_move_mode=2), None)
        self.assertTrue(checks[late_move_check(Settings())])

    def test_one_atr_and_missing_atr_are_not_blocked(self):
        one = self._extended(1.0)
        self.assertEqual(late_move(one, "CALL", Settings())["lm_block"], 0)
        missing = snap_of(one.bars, atr=None)
        self.assertEqual(late_move(missing, "CALL", Settings()),
                         {key: None for key in ("lm_ext_atr", "lm_hb", "lm_fresh_lvl", "lm_rv2", "lm_block")})
        checks = {}
        apply_scanner_fixes(missing, "CALL", None, {}, checks, Settings(late_move_mode=2), None)
        self.assertTrue(checks[late_move_check(Settings())])


class MarketGateTests(unittest.TestCase):
    def test_breakout_in_chop_is_blocked_and_pullbacks_are_exempt(self):
        stock = snap_of(bars_from([100] * 10 + [100.2, 100.4, 101.0], high_pad=0.0), spot=101, atr=1)
        start = stock.at - timedelta(minutes=20)
        flat_bars = bars_from([50] * 20, start=start)
        flat = snap_of(flat_bars, symbol="SPY", spot=50, atr=1)
        bench = {"SPY": flat, "QQQ": snap_of(flat_bars, symbol="QQQ", spot=50, atr=1)}
        gate = market_gate(stock, "CALL", Settings(), bench, 1)
        self.assertEqual(gate["mkt_state"], "chop")
        breakout = Setup("CONTINUATION", 100, 99)
        self.assertEqual(mg_block(gate["mkt_state"], 1, breakout), 1)
        checks = {}
        apply_scanner_fixes(stock, "CALL", breakout, {}, checks, Settings(mkt_gate_mode=2), bench)
        self.assertFalse(checks[MKT_CHECK])
        for name in ("PULLBACK_RESUMPTION", "LEVEL_RECLAIM"):
            exempt = {}
            apply_scanner_fixes(stock, "CALL", Setup(name, 100, 99), {}, exempt, Settings(mkt_gate_mode=2), bench)
            self.assertTrue(exempt[MKT_CHECK], name)

    def test_missing_benchmarks_are_unknown_and_do_not_block(self):
        stock = snap_of(bars_from([100] * 8 + [101, 102, 103]), spot=103, atr=1)
        gate = market_gate(stock, "CALL", Settings(), {}, 1)
        self.assertEqual(gate["mkt_state"], "unknown")
        self.assertEqual(mg_block("unknown", 1, Setup("CONTINUATION", 100, 99)), 0)
        checks = {}
        apply_scanner_fixes(stock, "CALL", Setup("CONTINUATION", 100, 99), {}, checks, Settings(mkt_gate_mode=2), None)
        self.assertTrue(checks[MKT_CHECK])

    def test_boundary_15_and_5_is_aligned(self):
        stock = snap_of(bars_from([100] * 5), atr=1)
        bench = aligned_bench(stock.at)
        gate = market_gate(stock, "CALL", Settings(), bench, 1)
        self.assertAlmostEqual(gate["mkt15"], 0.10, places=2)
        self.assertAlmostEqual(gate["mkt5"], 0.05, places=2)
        self.assertEqual(gate["mkt_state"], "aligned")


class FastRunTests(unittest.TestCase):
    def _straight_through_range(self, pullback=0.0):
        closes = [100.0] * 30
        for step in range(1, 11):
            closes.append(100 + 0.025 * step)  # 100.025 .. 100.25
        level = closes[-4]  # close[-4] is on the level; last three closes are beyond it
        built = []
        start = datetime(2026, 11, 24, 9, 30, tzinfo=ET)
        for index, close in enumerate(closes):
            end = start + timedelta(minutes=index + 1)
            if index < len(closes) - 3:
                high = min(close, level)
                low = high - 0.01
                built.append(Bar(end, high, high, low, high if index != len(closes) - 4 else level, 1000, 10000))
            else:
                high = close + (0.0 if not pullback else 0.0)
                built.append(Bar(end, close, close + 0.01, close - pullback, close, 1000, 10000))
        if pullback:
            # An 0.08 ATR dip off the highest close inside the same 11-close window.
            last = built[-1]
            peak = max(bar.close for bar in built[-11:-1])
            dipped = peak - pullback
            built[-1] = Bar(last.end, dipped, max(peak, last.high), dipped, dipped, last.volume, last.expected_cumulative_volume)
        return snap_of(tuple(built), spot=built[-1].close, atr=1.0, premarket_high=level, prior_high=None)

    def test_straight_run_alone_spiked_pullback_and_missing_market(self):
        settings = Settings(fast_run_mode=1, fast_run_mkt="alone")
        snap = self._straight_through_range()
        flat = snap_of(bars_from([50.0] * len(snap.bars)), symbol="SPY", spot=50, atr=1)
        qqq = snap_of(flat.bars, symbol="QQQ", spot=50, atr=1)
        alone = fast_run(snap, "CALL", settings, {"SPY": flat, "QQQ": qqq})
        self.assertEqual(alone["fr_lvl"], 1)
        self.assertGreaterEqual(alone["fr_run10"], 0.20)
        self.assertGreaterEqual(alone["fr_st10"], 0.60)
        self.assertLessEqual(alone["fr_pb10"], 0.05)
        self.assertEqual(alone["fr_mkt_class"], "alone")
        self.assertEqual(alone["fr_block"], 1)
        up = []
        start = snap.bars[0].end - timedelta(minutes=1)
        for index in range(len(snap.bars)):
            end = start + timedelta(minutes=index + 1)
            price = 50 + 0.02 * index
            up.append(Bar(end, price, price, price, price, 1000, 10000))
        spy = snap_of(tuple(up), symbol="SPY", spot=up[-1].close, atr=1)
        qqq_up = snap_of(tuple(up), symbol="QQQ", spot=up[-1].close, atr=1)
        spiked = fast_run(snap, "CALL", settings, {"SPY": spy, "QQQ": qqq_up})
        self.assertEqual(spiked["fr_mkt_class"], "spiked")
        self.assertEqual(spiked["fr_block"], 0)
        blocked = fast_run(snap, "CALL", Settings(fast_run_mode=2, fast_run_mkt="spiked"), {"SPY": spy, "QQQ": qqq_up})
        self.assertEqual(blocked["fr_block"], 1)
        soft = self._straight_through_range(pullback=0.08)
        faded = fast_run(soft, "CALL", settings, {"SPY": flat, "QQQ": qqq})
        self.assertEqual(faded["fr_fast"], 0)
        self.assertGreater(faded["fr_pb10"], 0.05)
        no_level = snap_of(bars_from([100.0] * 40), spot=100, atr=1, premarket_high=1000, prior_high=1000)
        quiet = fast_run(no_level, "CALL", settings, {"SPY": flat, "QQQ": qqq})
        self.assertEqual(quiet["fr_lvl"], 0)
        self.assertEqual(quiet["fr_fast"], 0)
        missing = fast_run(snap, "CALL", Settings(fast_run_mode=2, fast_run_mkt="all"), None)
        self.assertEqual(missing["fr_mkt_class"], "unknown")
        self.assertEqual(missing["fr_block"], 0)
        checks = {}
        apply_scanner_fixes(snap, "CALL", Setup("CONTINUATION", 100, 99), {}, checks,
                            Settings(fast_run_mode=2, fast_run_mkt="alone"), {"SPY": flat, "QQQ": qqq})
        self.assertFalse(checks[FAST_RUN_CHECK])


class StockFirstTests(unittest.TestCase):
    def engine(self, mode=1, cap=2):
        store = Store(":memory:")
        engine = Engine(store, Settings(stock_first_mode=mode, stock_first_cap=cap, late_move_atr=1.1))
        return store, engine

    def arm(self, engine, snap):
        bench = aligned_bench(snap.at)
        engine.bench.update(bench)

    def test_fires_only_on_the_empty_candidate_path(self):
        demo, alerts = run_demo(Settings(stock_first_mode=1))
        self.assertTrue(alerts)
        self.assertEqual(demo.db.execute("SELECT COUNT(*) FROM stock_first_shadow").fetchone()[0], 0)
        store, engine = self.engine()
        snap = climb(30)
        self.arm(engine, snap)
        engine.process([snap])
        rows = store.db.execute("SELECT * FROM stock_first_shadow").fetchall()
        self.assertTrue(rows)
        self.assertTrue(any(row["side"] == "CALL" and row["cap_blocked"] == 0 and row["no_contract"] == 0 for row in rows))
        self.assertIn("options history", store.db.execute("SELECT reason FROM decisions").fetchone()[0])

    def test_cap_of_two_per_five_minutes_per_side(self):
        store, engine = self.engine(cap=2)
        for minute in (30, 31, 32):
            snap = climb(minute)
            self.arm(engine, snap)
            engine.process([snap])
        calls = store.db.execute(
            "SELECT cap_blocked, cooldown_blocked FROM stock_first_shadow WHERE side='CALL' ORDER BY ts").fetchall()
        self.assertEqual([row["cap_blocked"] for row in calls], [0, 0, 1])
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)

    def test_shares_the_30_minute_cooldown(self):
        store, engine = self.engine()
        snap = climb(30)
        self.arm(engine, snap)
        with store.db:
            store.db.execute(
                """INSERT INTO alerts (id, day, symbol, side, contract, at, entry_ask, delivery)
                   VALUES ('prior','2026-11-24','TEST','CALL','OLD',?,1,'dry_run')""",
                ((snap.at - timedelta(minutes=10)).isoformat(),))
        engine.process([snap])
        row = store.db.execute("SELECT cooldown_blocked FROM stock_first_shadow WHERE side='CALL'").fetchone()
        self.assertEqual(row["cooldown_blocked"], 1)
        later = climb(31)
        self.arm(engine, later)
        with store.db:
            store.db.execute("DELETE FROM alerts")
            store.db.execute(
                """INSERT INTO alerts (id, day, symbol, side, contract, at, entry_ask, delivery)
                   VALUES ('old','2026-11-24','TEST','PUT','OLD',?,1,'dry_run')""",
                ((later.at - timedelta(minutes=40)).isoformat(),))
        engine.process([later])
        fresh = store.db.execute(
            "SELECT cooldown_blocked FROM stock_first_shadow WHERE ts=? AND side='CALL'",
            (later.at.isoformat(),)).fetchone()
        self.assertEqual(fresh["cooldown_blocked"], 0)

    def test_no_contract_is_logged_and_not_posted(self):
        store, engine = self.engine(mode=2)
        snap = climb(30, with_option=False)
        self.arm(engine, snap)
        alerts = engine.process([snap])
        self.assertEqual(alerts, [])
        row = store.db.execute("SELECT no_contract, contract FROM stock_first_shadow WHERE side='CALL'").fetchone()
        self.assertEqual(row["no_contract"], 1)
        self.assertEqual(row["contract"], "")
        self.assertEqual(store.summary(), [])

    def test_after_14_nothing_fires_and_mode_2_posts_before_then(self):
        store, engine = self.engine(mode=2)
        late = climb(275)  # 09:31 + 274 minutes = 14:05
        self.assertGreaterEqual(late.at.astimezone(ET).hour, 14)
        self.arm(engine, late)
        self.assertEqual(engine.process([late]), [])
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM stock_first_shadow").fetchone()[0], 0)
        early_store, early = self.engine(mode=2)
        snap = climb(30)
        self.arm(early, snap)
        alerts = early.process([snap])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["phase"], "ACTIVE")
        self.assertEqual(alerts[0]["payload"]["embeds"][0]["title"], "STOCK-FIRST IDEA (options history thin)")
        self.assertEqual(early_store.summary()[0]["delivery"], "dry_run")
        base = climb(31)
        follow = snap_of(base.bars, spot=base.spot, atr=5, options=(option_for(base.at, bid=0.80, ask=0.85),),
                         premarket_high=99, prior_high=99, premarket_low=90, prior_low=90)
        early.process([follow])
        self.assertGreaterEqual(early_store.db.execute("SELECT COUNT(*) FROM milestones").fetchone()[0], 1)


class RearmFillTests(unittest.TestCase):
    def _qualifying(self, snap, history, settings, bench=None):
        return [Candidate(snap, snap.options[0], 90.0, {"price_structure": 20.0},
                          {"opening_expansion": 0, "local_rvol_5m": 1.0},
                          ["Stock and options aligned"], True,
                          setup=Setup("CONTINUATION", snap.spot, snap.spot - 1))]

    def _flat(self, minute, high=None):
        start = datetime(2026, 11, 17, 9, 30, tzinfo=ET)
        bars = []
        for index in range(minute):
            end = start + timedelta(minutes=index + 1)
            bars.append(Bar(end, 100, 100, 100, 100, 1000, 10000))
        if high is not None:
            last = bars[-1]
            bars[-1] = Bar(last.end, 100, high, 100, 100, 1000, 10000)
        at = bars[-1].end
        spot = high if high is not None else 100
        option = Option("TEST-C", at.date(), "CALL", 100, 1.0, 1.10, 200, 100, 0.30, 0.05, at)
        return Snapshot("TEST", at, spot, at, 100, tuple(bars), (option,), prior_atr=2,
                        context_return_5m=0, context_time=at, context_label="SMH",
                        session_end=at.replace(hour=16, minute=0))

    def _run(self, minutes):
        store = Store(":memory:")
        engine = Engine(store, Settings(cooldown_mode=1, rearm_atr_mode=1))
        with patch("lotto.engine.candidates", side_effect=self._qualifying):
            for minute in minutes:
                engine.process([minute if isinstance(minute, Snapshot) else self._flat(minute)])
        return store

    def test_refire_outside_the_window_is_filled_and_inside_matches_variant(self):
        late = self._run([self._flat(20), self._flat(21), self._flat(46, high=100.20), self._flat(47, high=100.40)])
        row = late.db.execute(
            "SELECT rearm_atr_010, rearm_atr_015, rearm_in_window, rearm_atr_dist, rearm_elapsed_min "
            "FROM finalb_shadow WHERE rearm_atr_010 IS NOT NULL ORDER BY ts DESC").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["rearm_in_window"], 0)
        self.assertEqual((row["rearm_atr_010"], row["rearm_atr_015"]), (1, 1))
        self.assertGreaterEqual(row["rearm_elapsed_min"], 20)
        inside = self._run([self._flat(20), self._flat(21), self._flat(27, high=100.20), self._flat(28, high=100.30)])
        filled = inside.db.execute(
            "SELECT ts, rearm_atr_010, rearm_atr_015, rearm_in_window FROM finalb_shadow "
            "WHERE rearm_in_window=1").fetchone()
        self.assertIsNotNone(filled)
        snap = self._flat(28, high=100.30)
        last = {"at": self._flat(21).at, "side": "CALL", "extreme": 100.0}
        measure = rearm_atr_measure(snap.at, last, "CALL", snap)
        self.assertEqual(measure[2], 1)
        self.assertEqual(
            (filled["rearm_atr_010"], filled["rearm_atr_015"]),
            rearm_atr_variant(snap.at, last, "CALL", snap, Settings(rearm_atr_mode=1)))


class TokenFallbackTests(unittest.TestCase):
    env = {"SCHWAB_TOKEN_BROKER_URL": "https://broker.test/schwab-token", "SCHWAB_TOKEN_BROKER_KEY": "k"}

    def test_broker_503_then_200_keeps_the_token(self):
        error = HTTPError("https://broker.test/schwab-token", 503, "down", {}, None)
        env = {**self.env, "LOTTO_TOKEN_RESILIENCE_MODE": "1"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env, clear=True), \
                patch("lotto.schwab.time.sleep"), \
                patch("lotto.schwab.urlopen", side_effect=[error, self._broker("ok")]) as opened:
            self.assertEqual(Schwab(directory).token(), "ok")
            self.assertEqual(opened.call_count, 2)

    def test_401_does_not_retry_and_still_notifies(self):
        error = HTTPError("https://broker.test/schwab-token", 401, "no", {}, None)
        env = {**self.env, "LOTTO_TOKEN_RESILIENCE_MODE": "1"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env, clear=True), \
                patch("lotto.schwab.time.sleep"), patch("lotto.schwab.urlopen", side_effect=error) as opened:
            with self.assertRaises(RuntimeError):
                Schwab(directory).token()
            self.assertEqual(opened.call_count, 1)
        sent = []
        monitor = HealthMonitor(Store(":memory:"), "https://discord.com/api/webhooks/test/test",
                                sender=lambda _url, payload: sent.append(payload))
        now = datetime(2026, 11, 20, 13, 45, tzinfo=ET)
        with patch.dict(os.environ, {"LOTTO_HEALTH_FAST_MODE": "1"}, clear=True):
            monitor.failed_cycle(now, "Schwab token broker unavailable")
            monitor.failed_cycle(now + timedelta(seconds=1), "Schwab token broker unavailable")
        self.assertEqual(len(sent), 1)
        self.assertIn("unavailable", sent[0]["embeds"][0]["title"])

    def test_fallback_is_used_only_when_the_refresh_token_is_on_disk(self):
        error = HTTPError("https://broker.test/schwab-token", 503, "down", {}, None)
        env = {**self.env, "LOTTO_TOKEN_RESILIENCE_MODE": "1", "LOTTO_TOKEN_FALLBACK_MODE": "1",
               "SCHWAB_CLIENT_ID": "id", "SCHWAB_CLIENT_SECRET": "secret"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env, clear=True), \
                patch("lotto.schwab.time.sleep"), patch("lotto.schwab.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError), self.assertLogs("lotto.schwab", level="WARNING") as logs:
                Schwab(directory).token()
            self.assertNotIn("token fallback used", " ".join(logs.output))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env, clear=True), \
                patch("lotto.schwab.time.sleep"):
            path = Path(directory) / "schwab_tokens.json"
            path.write_text(json.dumps({"refresh_token": "refresh-me"}))
            def open_url(request, timeout=15):
                if "oauth/token" in request.full_url:
                    return self._oauth("direct-token")
                raise error
            with patch("lotto.schwab.urlopen", side_effect=open_url), \
                    self.assertLogs("lotto.schwab", level="WARNING") as logs:
                self.assertEqual(Schwab(directory).token(), "direct-token")
            self.assertIn("token fallback used", " ".join(logs.output))

    def test_repeat_notice_fires_at_10_minutes_and_restart_text_after_15(self):
        sent = []
        monitor = HealthMonitor(Store(":memory:"), "https://discord.com/api/webhooks/test/lotto",
                                sender=lambda url, payload: sent.append((url, payload)))
        now = datetime(2026, 11, 20, 13, 45, tzinfo=ET)
        env = {"LOTTO_TOKEN_FALLBACK_MODE": "1", "LOTTO_HEALTH_FAST_MODE": "1",
               "LOTTO_HEALTH_WEBHOOK": "https://discord.com/api/webhooks/test/hawk"}
        reason = "Schwab token broker unavailable"
        with patch.dict(os.environ, env, clear=True):
            monitor.failed_cycle(now, reason)
            monitor.failed_cycle(now + timedelta(seconds=1), reason)
            self.assertEqual(len(sent), 1)
            monitor.failed_cycle(now + timedelta(minutes=9), reason)
            self.assertEqual(len(sent), 1)
            monitor.failed_cycle(now + timedelta(minutes=10, seconds=1), reason)
            self.assertEqual(len(sent), 2)
            self.assertNotIn("still down", sent[1][1]["embeds"][0]["description"])
            self.assertEqual(sent[1][0], monitor.webhook)
            monitor.failed_cycle(now + timedelta(minutes=20, seconds=1), reason)
            self.assertEqual(len(sent), 3)
            self.assertIn("still down", sent[2][1]["embeds"][0]["description"])
            self.assertIn("restart worker", sent[2][1]["embeds"][0]["description"])
            self.assertEqual(sent[2][0], env["LOTTO_HEALTH_WEBHOOK"])

    def test_self_exit_only_inside_watch_hours(self):
        monitor = HealthMonitor(Store(":memory:"), "https://discord.com/api/webhooks/test/test",
                                sender=lambda *_args: None)
        start = datetime(2026, 11, 20, 13, 45, tzinfo=ET)
        monitor.first_failure = start
        with patch.dict(os.environ, {"LOTTO_TOKEN_FALLBACK_MODE": "1"}, clear=True):
            self.assertFalse(monitor.self_restart_due(start + timedelta(minutes=19), "Schwab token broker unavailable"))
            self.assertFalse(monitor.self_restart_due(datetime(2026, 11, 20, 17, 0, tzinfo=ET),
                                                     "Schwab token broker unavailable"))
            self.assertFalse(monitor.self_restart_due(start + timedelta(minutes=25), "market-data cycle unavailable"))
            self.assertTrue(monitor.self_restart_due(start + timedelta(minutes=20), "Schwab token broker unavailable"))
            with self.assertRaises(SystemExit) as raised:
                monitor.exit_if_token_outage(start + timedelta(minutes=20), "Schwab token broker unavailable")
            self.assertEqual(raised.exception.code, 3)
        policy = json.loads(Path("railway.json").read_text())
        self.assertEqual(policy["deploy"]["restartPolicyType"], "ON_FAILURE")

    def test_fix_shadow_exports_only_that_session(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "live.sqlite3"
            dest = Path(directory) / "slice.sqlite3"
            store = Store(str(source))
            store.record_fix_shadow([
                {"ts": "2026-11-24T10:00:00-05:00", "ticker": "AAA", "side": "CALL", "contract": "C",
                 "live_decision": "pass", "fix_flags": "{}"},
                {"ts": "2026-11-25T10:00:00-05:00", "ticker": "BBB", "side": "PUT", "contract": "P",
                 "live_decision": "block", "fix_flags": "{}"},
            ])
            store.db.close()
            counts = slice_session(source, dest, "2026-11-24")
            sliced = sqlite3.connect(dest)
            self.assertEqual([row[0] for row in sliced.execute("SELECT ticker FROM fix_shadow")], ["AAA"])
            self.assertEqual(counts["fix_shadow"], 1)
            sliced.close()

    def test_nightly_lines_name_each_fix(self):
        store = Store(":memory:")
        day = "2026-11-24"
        at = datetime(2026, 11, 24, 10, 0, tzinfo=ET).isoformat()
        store.record_fix_shadow([{
            "ts": at, "ticker": "TEST", "side": "CALL", "contract": "TEST-C", "live_decision": "pass",
            "lm_block": 1, "mg_block": 0, "fix_flags": json.dumps({"fr": {"fr_fast": 1, "fr_mkt_class": "alone"}}),
        }])
        lines = scanner_fix_lines(store, day)
        self.assertEqual(len(lines), 3)
        self.assertIn("late-move would block 0 cards", lines[0])
        self.assertIn("fast-run", lines[2])
        self.assertIn("alone", lines[2])

    @staticmethod
    def _broker(token):
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(json.dumps({"access_token": token, "expires_in": 240}).encode()))
        response.__exit__ = Mock(return_value=False)
        return response

    @staticmethod
    def _oauth(token):
        response = Mock()
        body = json.dumps({"access_token": token, "expires_in": 1800, "refresh_token": "refresh-me"}).encode()
        response.__enter__ = Mock(return_value=io.BytesIO(body))
        response.__exit__ = Mock(return_value=False)
        return response


if __name__ == "__main__":
    unittest.main()
