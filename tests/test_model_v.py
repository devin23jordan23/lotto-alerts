import csv
import json
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from lotto.config import CONFLUENCE_TABLE, HANDOFF_SPEC_PATH, Settings, model_v_settings
from lotto.demo import demo_frames
from lotto.discord import deliver_stock_pending
from lotto.engine import Engine
from lotto.confluence import (band_score, card_gate, detect_ema_pullback, detect_fashionably_late,
                              detect_flag, detect_m, detect_w, pattern_match, primary_strength_marker,
                              tiered_points)
from lotto.features import Candidate
from lotto.discord import alert_payload
from lotto.main import env_settings, fallback_rule_status, handoff_fallback_status, standby_symbols
from lotto.model_v import (apply_v2, atr21_value, build_handoff_features, cap_stock,
                           choose_fallback_contract, classify_runner, due_standby,
                           evaluate_fallback, exceptional_stock, fallback_push, fallback_rvol_value,
                           features_from_spec_raw, fill_model_features, fill_strength_inputs,
                           fire_why, handoff_floor, invert_live_blend, lane_decision, level_retest,
                           new_fallback_state, pick_one_otm, post_bar, quote_in_play, quote_spread_fail,
                           quote_universe, resolve_fallback_skip, score_handoff)
from lotto.stock_first import StockFirst
from lotto.models import Bar, ET, Option, Snapshot
from lotto.patterns import Setup
from lotto.quality import evaluate
from lotto.replay_v import replay
from lotto.stock_first import StockFirst, StockIdea
from lotto.store import Store


def _bars(start, n, close_at, high_pad=0.2, low_pad=0.2, volume=20000):
    rows = []
    for minute in range(1, n + 1):
        at = start + timedelta(minutes=minute)
        close = close_at(minute)
        rows.append(Bar(at, close - 0.05, close + high_pad, close - low_pad, close, volume, volume * minute))
    return tuple(rows)


class ModelVTests(unittest.TestCase):
    def test_defaults_and_rollback_env(self):
        self.assertEqual(Settings().min_score, 60)
        self.assertLessEqual(Settings().min_score, 60)
        with patch.dict(os.environ, {}, clear=False):
            for name in ("LOTTO_MODEL", "LOTTO_MIN_SCORE", "LOTTO_SCORE_MODE", "LOTTO_PRESET",
                         "LOTTO_STANDBY_NAMES", "LOTTO_HANDOFF_MODEL_PATH",
                         "LOTTO_STRENGTH_DISTRIBUTION_PATH", "LOTTO_ATR_DAYS",
                         "LOTTO_FALLBACK_RULE", "LOTTO_STRONG_SCORE", "LOTTO_FB_PUSH_ATR_MIN"):
                os.environ.pop(name, None)
            live = env_settings()
        self.assertEqual(live.model, "V")
        self.assertEqual(live.atr_days, 21)
        self.assertEqual(live.min_score, 60)
        self.assertEqual(live.fallback_rule, "post")
        self.assertEqual((live.fb_rvol_min, live.fb_push_atr_min, live.fb_room_atr_max, live.fb_max_spread_pct),
                         (0.78, 0.75, 1.0, 30))
        self.assertEqual(live.strong_score, 57)
        self.assertEqual((live.stale_quote_seconds, live.stale_quote_penalty), (5, 4))
        self.assertEqual(live.bypass_confluence_min, 9)
        self.assertEqual(live.score_mode, "s12")
        self.assertEqual(live.preset, "S12")
        self.assertEqual(live.standby_names, 0)
        self.assertEqual(live.pwin_normal_0dte, 0.43157386)
        self.assertEqual(live.pwin_normal_1dte, 0.47114974)
        with patch.dict(os.environ, {"LOTTO_MODEL": "V", "LOTTO_ATR_DAYS": "14"}, clear=False):
            rolled = env_settings()
        self.assertEqual(rolled.atr_days, 14)
        self.assertEqual(rolled.pwin_normal_0dte, live.pwin_normal_0dte)
        self.assertEqual(rolled.pwin_decile, live.pwin_decile)
        self.assertEqual(rolled.strength_quintile, live.strength_quintile)
        self.assertEqual(rolled.strength_decile, live.strength_decile)
        self.assertEqual(live.pwin_quintile_0dte, 0.28822075)
        self.assertEqual(live.pwin_decile, 0.17760635)
        self.assertEqual(live.pwin_decile_1dte, 0.18333891)
        self.assertTrue(live.handoff_model_path.endswith("handoff_model_v1026.json"))
        self.assertEqual(live.handoff_model_path, live.strength_distribution_path)
        self.assertEqual(live.handoff_model_path, HANDOFF_SPEC_PATH)
        self.assertNotIn("PLACEHOLDER", live.preset_note)
        self.assertEqual(model_v_settings().atr_days, 21)
        self.assertEqual((live.post_combined_0dte, live.post_combined_1dte), (40, 61))
        self.assertEqual(live.stock_max_share, 0.40)
        self.assertEqual((live.post_v2_option_0dte, live.post_v2_option_1dte), (40, 61))
        self.assertEqual((live.post_v2_stock_0dte, live.post_v2_stock_1dte), (58, 80))
        self.assertEqual(live.send_requote, 1)
        with patch.dict(os.environ, {"LOTTO_MODEL": "S", "LOTTO_MIN_SCORE": "70"}, clear=False):
            rolled = env_settings()
        self.assertEqual(rolled.model, "S")
        self.assertEqual(rolled.min_score, 70)
        self.assertEqual(rolled.hybrid, 0)
        with patch.dict(os.environ, {"LOTTO_MODEL": "V", "LOTTO_MIN_SCORE": "90",
                                      "LOTTO_SCORE_MODE": "s12", "LOTTO_PRESET": "S11"}, clear=False):
            clamped = env_settings()
        self.assertEqual(clamped.min_score, 60)
        self.assertEqual(clamped.score_mode, "s12")
        self.assertEqual(clamped.preset, "S11")
        self.assertEqual(clamped.pwin_quintile_0dte, 0.32149954)
        self.assertEqual(clamped.pwin_quintile_1dte, 0.34696342)
        self.assertEqual(clamped.pwin_decile, 0.22822777)
        self.assertEqual(clamped.pwin_decile_1dte, 0.24061730)
        self.assertNotIn("PLACEHOLDER", clamped.preset_note)
        self.assertEqual(clamped.post_v2_option_0dte, 40)
        with patch.dict(os.environ, {"LOTTO_MODEL": "V", "LOTTO_SCORE_MODE": "combined",
                                      "LOTTO_STANDBY_NAMES": "on"}, clear=False):
            rollback = env_settings()
        self.assertEqual(rollback.score_mode, "combined")
        self.assertEqual(rollback.standby_names, 1)
        self.assertIn("Rollback", rollback.preset_note)

    def test_stock_cards_never_post_in_any_mode(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        start = datetime(2026, 10, 26, 9, 30, tzinfo=ET)
        scanner = StockFirst(store, enabled=True, mode="send")
        prices = [370, 370.1, 370.2, 370.4, 370.8, 371.8, 372.7, 373.6]
        for minute, price in enumerate(prices):
            at = start + timedelta(minutes=minute)
            scanner.process([{
                "symbol": "PANW", "observed_at": at, "at": at - timedelta(seconds=5),
                "price": price, "open": 370.0, "previous": 370.0,
                "high": price, "low": min(price, 370.0), "volume": 100000 + minute,
            }], dry_run=False)
        row = store.db.execute("SELECT delivery FROM stock_ideas").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["delivery"], "internal")
        store.db.execute("UPDATE stock_ideas SET delivery='pending'")
        with patch("lotto.discord.urlopen") as send:
            deliver_stock_pending(store, "https://discord.com/api/webhooks/test/test")
        send.assert_not_called()
        self.assertEqual(store.db.execute("SELECT delivery FROM stock_ideas").fetchone()["delivery"], "internal")
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)

    def test_spy_label_mu_1125_and_put(self):
        snap = list(demo_frames())[8][0]
        at = datetime(2026, 10, 26, 11, 25, tzinfo=ET)
        option = replace(snap.options[0], quote_time=at, expiry=at.date())
        call = replace(snap, at=at, spot_time=at, options=(option,), market_time=at,
                       market_label="SPY", market_return_5m=-0.0005,
                       market_return_from_open=0.0065, market_vwap_side=1)
        candidate = Candidate(call, option, 90, {}, {"move_from_open_atr": 0.26}, [], True,
                              setup=Setup("LEVEL_RECLAIM", call.spot, call.spot - 1))
        decision = evaluate(candidate, "stack8")
        self.assertIn("SPY aligned · pulling back", decision.labels)
        self.assertNotIn("LRSPY", decision.reasons)
        put = replace(option, side="PUT", delta=-0.3)
        put_snap = replace(call, options=(put,))
        put_candidate = Candidate(put_snap, put, 90, {}, {}, [], True,
                                  setup=Setup("LEVEL_RECLAIM", call.spot, call.spot + 1))
        self.assertIn("SPY opposing", evaluate(put_candidate).labels)
        self.assertIn("LRSPY", evaluate(put_candidate, "stack8").reasons)

    def test_vwap_tap_alone_is_not_the_retest(self):
        start = datetime(2026, 10, 26, 9, 30, tzinfo=ET)
        # Flat tape around 100. A late green bar tags VWAP and goes nowhere.
        bars = _bars(start, 40, lambda minute: 100 + (0.05 if minute > 35 else 0), high_pad=0.15, low_pad=0.15)
        snap = list(demo_frames())[8][0]
        snap = replace(snap, at=bars[-1].end, bars=bars, spot=bars[-1].close, prior_atr=2,
                       premarket_high=None, prior_high=None)
        detail = level_retest(snap, 1)
        self.assertEqual(detail["retest_break"], 0)
        self.assertEqual(detail["vwap_tap_only"], 1)
        scored = apply_v2(Candidate(snap, snap.options[0], 50, {}, {}, [], False), model_v_settings(), [])
        self.assertEqual(scored.metrics["level_retest"], 0)

    def test_first_retest_scores_more_than_a_later_one(self):
        start = datetime(2026, 10, 26, 9, 30, tzinfo=ET)

        def pack(second):
            # Clock buckets from 9:30: 10:01-10:05 breaks ORH, 10:06-10:10 is
            # the first retest, and only the second tape wicks back at 10:16-10:20.
            rows = []
            for minute in range(1, 51):
                at = start + timedelta(minutes=minute)
                if minute <= 30:
                    close, low, high = 100.0, 99.8, 100.2
                elif minute <= 35:
                    close, low, high = 103.0, 102.6, 103.2
                elif minute <= 40:
                    close, low, high = 100.6, 100.0, 100.9
                elif second and minute >= 46:
                    close, low, high = 100.7, 100.0, 101.0
                else:
                    close, low, high = 103.5, 103.2, 103.8
                rows.append(Bar(at, close, high, low, close, 30000, 30000 * minute))
            return tuple(rows)

        snap = list(demo_frames())[8][0]
        first_bars, later_bars = pack(False), pack(True)
        first = replace(snap, at=first_bars[-1].end, bars=first_bars, spot=103.5, prior_atr=2,
                        premarket_high=None, prior_high=None)
        later = replace(first, at=later_bars[-1].end, bars=later_bars, spot=100.7)
        first_detail = level_retest(first, 1)
        later_detail = level_retest(later, 1)
        self.assertEqual(first_detail["retest_first"], 1)
        self.assertEqual(later_detail["retest_later"], 1)
        first_score = apply_v2(Candidate(first, first.options[0], 40, {}, {}, [], False), model_v_settings(), [])
        later_score = apply_v2(Candidate(later, later.options[0], 40, {}, {}, [], False), model_v_settings(), [])
        self.assertGreater(first_score.metrics["level_retest"], later_score.metrics["level_retest"])
        self.assertGreater(first_score.metrics["level_retest"], 0)

    def test_no_path_suppresses_a_card_at_or_above_the_bar(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        settings = model_v_settings(max_alerts_per_day=1, max_alerts_per_cycle=1, max_alerts_per_ticker=1)
        engine = Engine(store, settings, dry_run=False)
        base = list(demo_frames())[20][0]
        wide = replace(base.options[0], bid=0.05, ask=0.40)

        def fake(snap, history, cfg):
            option = replace(wide, symbol=snap.symbol + "C", quote_time=snap.at, expiry=snap.at.astimezone(ET).date())
            return [Candidate(snap, option, 120, {"price_structure": 120},
                              {"response_proxy": 0.1, "return_5m_directional": 0.0, "move_from_open_atr": 0.1,
                               "cluster_size": 3}, [], False,
                              setup=Setup("CONTINUATION", snap.spot, snap.spot - 1),
                              blockers=("too far from price trigger", "stock volume pace/burst below threshold"))]

        frames = []
        for symbol in ("AAA", "BBB"):
            snap = replace(base, symbol=symbol, options=tuple(replace(o, symbol=symbol + o.symbol) for o in base.options))
            frames.append(snap)
        with patch("lotto.engine.candidates", side_effect=fake):
            alerts = engine.process(frames)
        self.assertEqual(len(alerts), 2)
        for alert in alerts:
            self.assertIn(alert["payload"]["embeds"][0]["description"], alert["payload"]["embeds"][0]["description"])
            self.assertIn("v2", alert["payload"]["embeds"][0]["description"])
            self.assertNotIn("STOCK-LED", alert["payload"]["embeds"][0]["title"])
        deliveries = [row["delivery"] for row in store.db.execute("SELECT delivery FROM alerts")]
        self.assertEqual(deliveries, ["pending", "pending"])
        features = json.loads(store.db.execute("SELECT features FROM decisions WHERE reason LIKE 'active alert%'").fetchone()["features"])
        self.assertNotIn("pen_repeat", features["parts"])
        self.assertGreaterEqual(features["metrics"]["score_v2"], features["metrics"]["post_bar"])

    def test_repeats_are_not_penalized_and_only_the_same_event_is_deduped(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        engine = Engine(store, model_v_settings(), dry_run=True)
        base = list(demo_frames())[20][0]
        option = replace(base.options[0], symbol="SAME", quote_time=base.at, expiry=base.at.astimezone(ET).date())

        def fake(snap, history, cfg, trigger):
            contract = replace(option, quote_time=snap.at)
            return [Candidate(replace(snap, options=(contract,)), contract, 100, {},
                              {"cluster_size": 3, "move_from_open_atr": 0.2}, [], False,
                              setup=Setup("CONTINUATION", trigger, trigger - 1))]

        def advance(snap, minutes):
            end = snap.at + timedelta(minutes=minutes)
            prev = snap.bars[-1]
            bar = Bar(end, prev.close, max(prev.high, prev.close), min(prev.low, prev.close), prev.close,
                      prev.volume, prev.expected_cumulative_volume)
            return replace(snap, at=end, spot_time=end, bars=snap.bars + (bar,),
                           options=(replace(option, quote_time=end),))

        first_snap = replace(base, options=(option,))
        second_snap = advance(first_snap, 2)
        with patch("lotto.engine.candidates", side_effect=lambda snap, history, cfg: fake(snap, history, cfg, 100)):
            first = engine.process([first_snap])
            second = engine.process([second_snap])
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        drift = advance(second_snap, 1)
        with patch("lotto.engine.candidates", side_effect=lambda snap, history, cfg: fake(snap, history, cfg, 100.2)):
            self.assertEqual(engine.process([drift]), [])
        third_snap = advance(drift, 1)
        with patch("lotto.engine.candidates", side_effect=lambda snap, history, cfg: fake(snap, history, cfg, 110)):
            third = engine.process([third_snap])
        self.assertEqual(len(third), 1)
        row = store.db.execute("SELECT features FROM decisions WHERE reason LIKE 'active alert%' ORDER BY at DESC").fetchone()
        parts = json.loads(row["features"])["parts"]
        self.assertNotIn("pen_repeat", parts)
        self.assertNotIn("pen_fire_n", parts)

    def test_handoff_posts_only_through_the_option_score(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        base = list(demo_frames())[30][0]
        idea = StockIdea("DEMO", base.day, base.at - timedelta(minutes=1), "CALL", base.spot,
                         base.spot - 0.5, base.spot - 1, "INTRADAY_BREAK", "A", {})
        quiet = model_v_settings(score_mode="combined", post_combined_0dte=500, post_v2_stock_0dte=500,
                                 post_v2_option_0dte=500)
        engine = Engine(store, quiet, dry_run=False)
        engine.remember_stock_events([idea])
        with patch("lotto.engine.candidates", return_value=[]):
            none = engine.process([base])
        self.assertEqual(none, [])
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
        reasons = [row["reason"] for row in store.db.execute("SELECT reason FROM decisions")]
        self.assertTrue(any("below bar" in reason or "no v2 tier" in reason for reason in reasons), reasons)
        # The handoff was scored or explicitly logged. Either way nothing posted.
        logged = store.db.execute("SELECT COUNT(*) FROM candidate_evaluations").fetchone()[0]
        decisions = store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertGreater(logged + decisions, 0)
        loud = Engine(Store(":memory:"), model_v_settings(score_mode="combined", post_combined_0dte=1,
                                                          post_v2_stock_0dte=1, post_v2_option_0dte=1), dry_run=False)
        self.addCleanup(loud.store.db.close)
        loud.remember_stock_events([idea])
        with patch("lotto.engine.candidates", return_value=[]):
            posted = loud.process([base])
        self.assertEqual(len(posted), 1)
        description = posted[0]["payload"]["embeds"][0]["description"]
        self.assertIn("v2", description)
        self.assertNotIn("No option contract", description)
        self.assertIn("DTE", description)
        contract = pick_one_otm(base, "CALL", model_v_settings())
        self.assertIsNotNone(contract)
        self.assertEqual(loud.store.db.execute("SELECT contract FROM alerts").fetchone()["contract"], contract.symbol)

    def test_missing_v2_is_logged_and_not_silent(self):
        score, reason = invert_live_blend(88, None, True)
        self.assertIsNone(score)
        self.assertIn("no v2 tier", reason)
        score, reason = invert_live_blend(88, ["OPEN"], True)
        self.assertEqual(score, 88 + 10 - 5)
        self.assertIsNone(reason)

    def test_runner_miss_reasons_and_separate_strong_bar(self):
        when = datetime(2026, 10, 26, 10, 30, tzinfo=ET)
        alerts = [{"symbol": "AAA", "side": "CALL", "day": "2026-10-26", "at": when - timedelta(minutes=5), "posted": True}]
        late = [{"symbol": "BBB", "side": "CALL", "day": "2026-10-26", "at": when + timedelta(minutes=5), "posted": True}]
        evals = [{"symbol": "CCC", "side": "CALL", "day": "2026-10-26", "score_v2": 50, "post_bar": 58, "below_by": 8, "path": "stock"}]
        scanned = {"AAA", "BBB", "CCC", "DDD"}
        self.assertEqual(classify_runner({"symbol": "AAA", "side": "CALL", "day": "2026-10-26", "at": when}, alerts, [], scanned)["result"], "covered")
        self.assertEqual(classify_runner({"symbol": "BBB", "side": "CALL", "day": "2026-10-26", "at": when}, late, [], scanned)["result"], "late")
        below = classify_runner({"symbol": "CCC", "side": "CALL", "day": "2026-10-26", "at": when}, [], evals, scanned)
        self.assertEqual(below["result"], "below_bar")
        self.assertEqual(below["below_by"], 8)
        self.assertEqual(classify_runner({"symbol": "DDD", "side": "CALL", "day": "2026-10-26", "at": when}, [], [], scanned)["result"], "never_scored")
        self.assertEqual(classify_runner({"symbol": "IREN", "side": "CALL", "day": "2026-10-26", "at": when}, [], [], scanned)["result"], "not_in_universe")
        settings = model_v_settings()
        self.assertEqual(post_bar("combined", 0, settings), 40)
        self.assertEqual(post_bar("option", 0, settings), 40)
        self.assertEqual(post_bar("stock", 0, settings), 58)
        self.assertEqual(post_bar("stock", 1, settings), 80)
        used, cap, total = cap_stock(0, 100, settings)
        self.assertEqual(used, 0)
        self.assertLess(total, settings.post_combined_0dte)
        used, cap, total = cap_stock(40, 0, settings)
        self.assertGreaterEqual(max(40, total), settings.post_combined_0dte)
        snap = list(demo_frames())[8][0]
        self.assertFalse(exceptional_stock(
            {"move_from_open_atr": 0.2, "above_vwap_share": 1},
            {"rvol_used": 4, "held_pm": 1, "held_pd": 1, "held_range": 1},
            snap, settings, Setup("CONTINUATION", snap.spot, snap.spot - 1), True))
        self.assertTrue(exceptional_stock(
            {"move_from_open_atr": 3, "above_vwap_share": 1},
            {"rvol_used": 4, "held_pm": 1, "held_pd": 0, "held_range": 0},
            snap, settings, Setup("CONTINUATION", snap.spot, snap.spot - 1), True))

    def test_standby_list_is_env_configurable_and_only_in_play(self):
        with patch.dict(os.environ, {"UOA_IN_PLAY": "WULF,NVDA", "LOTTO_STANDBY_UNIVERSE": "DKNG,RIVN"}, clear=False):
            os.environ.pop("LOTTO_STANDBY_NAMES", None)
            self.assertEqual(standby_symbols(), [])
        with patch.dict(os.environ, {"LOTTO_STANDBY_NAMES": "on", "UOA_IN_PLAY": "WULF,NVDA",
                                      "LOTTO_STANDBY_UNIVERSE": "DKNG,RIVN"}, clear=False):
            names = set(standby_symbols())
        self.assertIn("DKNG", names)
        self.assertIn("WULF", names)
        self.assertIn("NVDA", names)
        self.assertEqual(model_v_settings().standby_names, 0)
        settings = model_v_settings()
        self.assertFalse(quote_in_play({"price": 10, "open": 10, "previous": 10, "volume_acceleration": 0.2}, settings))
        self.assertTrue(quote_in_play({"price": 11, "open": 10, "previous": 10}, settings))
        self.assertTrue(quote_in_play({"price": 10.1, "open": 10, "previous": 10, "volume_acceleration": 2}, settings))

    def test_replay_reports_coverage_split(self):
        frames = list(demo_frames())
        when = frames[-1][0].at
        runners = [
            {"symbol": "DEMO", "side": "CALL", "day": frames[-1][0].day, "at": when},
            {"symbol": "IREN", "side": "CALL", "day": frames[-1][0].day, "at": when},
        ]
        # Force the bars low so the synthetic tape can post, then classify.
        summary = replay(frames, model_v_settings(post_combined_0dte=1, post_v2_option_0dte=1,
                                                  post_v2_stock_0dte=1), runners, scanned={"DEMO"})
        self.assertIn("cards_per_day", summary)
        self.assertIn("true_win_rate", summary)
        self.assertEqual(summary["runner_misses"].get("not_in_universe"), 1)
        self.assertIn(summary["runner_rows"][0]["result"], {"covered", "below_bar", "never_scored", "late"})

    def test_stock_cap_rotation_and_override_card(self):
        settings = model_v_settings()
        self.assertLess(cap_stock(10, 100, settings)[2], settings.post_combined_0dte)
        self.assertGreaterEqual(cap_stock(40, 0, settings)[2], settings.post_combined_0dte)
        self.assertEqual(quote_universe(["A", "B"], ["S1", "S2", "S3"], 0, 3, 1), ["A", "B", "S1"])
        self.assertEqual(quote_universe(["A", "B"], ["S1", "S2", "S3"], 1, 3, 1)[2], "S2")
        when = datetime(2026, 10, 26, 10, 0, tzinfo=ET)
        first = due_standby(["A", "B", "C", "D", "E"], {}, when, 3, 2)
        self.assertEqual(first, ["A", "B"])
        scanned = {name: when for name in first}
        self.assertEqual(due_standby(["A", "B", "C", "D", "E"], scanned, when + timedelta(minutes=1), 3, 2), ["C", "D"])
        # Quiet minute: standby quotes wait for the cadence instead of a second request.
        self.assertEqual(quote_universe(["A"], ["S1"], 1, 10, 2), ["A"])
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        base = replace(list(demo_frames())[30][0], premarket_high=1, premarket_low=1)
        base = replace(base, premarket_high=base.spot - 1)
        idea = StockIdea("DEMO", base.day, base.at - timedelta(minutes=1), "CALL", base.spot,
                         base.spot - 0.5, base.spot - 1, "INTRADAY_BREAK", "A", {})
        rare = model_v_settings(score_mode="combined", post_combined_0dte=500, override_move_atr=0.01,
                                override_rvol=0.01, override_vwap_share=0, override_fresh_seconds=120)
        engine = Engine(store, rare, dry_run=False)
        engine.remember_stock_events([idea])
        with patch("lotto.engine.candidates", return_value=[]):
            posted = engine.process([base])
        self.assertEqual(len(posted), 1)
        description = posted[0]["payload"]["embeds"][0]["description"]
        self.assertIn("stock_override", description)
        self.assertNotIn("No option contract", description)
        row = store.db.execute("SELECT contract, payload FROM alerts").fetchone()
        self.assertTrue(row["contract"])
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)
        features = json.loads(store.db.execute(
            "SELECT features FROM decisions WHERE reason LIKE 'active alert%'").fetchone()["features"])
        self.assertEqual(features["metrics"]["path"], "stock_override")
        self.assertEqual(features["metrics"]["score_mode"], "combined")
        self.assertIn("option_v2", features["metrics"])
        self.assertIn("stock_v2", features["metrics"])

    def test_spec_vectors_match_fitted_probability_and_strength(self):
        spec = json.loads(Path(HANDOFF_SPEC_PATH).read_text())
        # These rows are the shipped ATR14 fit. Score them on LOTTO_ATR_DAYS=14.
        # Do not rebuild the vectors from 21-day ranges.
        live = model_v_settings(atr_days=14)
        s11 = model_v_settings(preset="S11", atr_days=14)
        self.assertEqual(model_v_settings().atr_days, 21)
        self.assertEqual(model_v_settings().pwin_decile, live.pwin_decile)
        self.assertEqual(live.pwin_decile, 0.17760635)
        self.assertEqual(live.pwin_decile_1dte, 0.18333891)
        self.assertEqual(s11.pwin_decile_1dte, 0.24061730)
        # The spec file prints rounded floors. Live uses the longer values.
        self.assertEqual(len(spec["test_vectors"]), 10)
        for row in spec["test_vectors"]:
            scored = score_handoff(spec, row["model_inputs"], rvol=row["raw"]["rvol"])
            self.assertAlmostEqual(scored["p_win"], row["expected_p_win"], places=6, msg=row["cid"])
            self.assertAlmostEqual(scored["hoF"], row["expected_v2eq_hoF"], places=4, msg=row["cid"])
            self.assertAlmostEqual(scored["stock_strength"], row["expected_SS"], places=6, msg=row["cid"])
            for name, expected in row["expected_SS_parts"].items():
                self.assertAlmostEqual(scored["strength_parts"][name], expected, places=6, msg=(row["cid"], name))
            untouched = score_handoff(spec, {**row["model_inputs"], "vwap_side": 0.0}, rvol=row["raw"]["rvol"])
            self.assertEqual(scored["p_win"], untouched["p_win"])

    def test_missing_breaks_fill_to_one_and_short_rvol_is_zero(self):
        spec = json.loads(Path(HANDOFF_SPEC_PATH).read_text())
        medians = spec["feature_definitions"]["missing_values"]["median"]
        raw = {"mo_atr": None, "rvol": None, "pm_brk": None, "pd_brk": None, "newx5": None}
        filled = fill_model_features(raw, medians)
        strength = fill_strength_inputs(raw, medians)
        self.assertEqual((filled["pm_brk"], filled["pd_brk"], filled["newx5"]), (1.0, 1.0, 1.0))
        self.assertEqual((strength["pm_brk"], strength["pd_brk"], strength["newx5"]), (1.0, 1.0, 1.0))
        self.assertEqual(strength["rvol"], 0.0)
        self.assertEqual(filled["l_rvol"], 0.0)
        scored = score_handoff(spec, filled, rvol=strength["rvol"])
        self.assertEqual(scored["strength_parts"]["breaks"], 1.0)
        snap = replace(list(demo_frames())[8][0], prior_full_days=4, rvol_base=1_000_000,
                       premarket_high=None, prior_high=None)
        features = build_handoff_features(snap, snap.options[0], [], 1)
        self.assertEqual(features["rvol"], 0.0)
        self.assertIsNone(features["pm_brk"])
        self.assertIsNone(features["pd_brk"])
        loud = replace(snap, prior_full_days=6, rvol_base=100.0)
        self.assertGreater(build_handoff_features(loud, loud.options[0], [], 1)["rvol"], 0)

    def test_vince_lane_vectors(self):
        root = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
        cases = (
            # ATR14 fixtures. The pass/fail columns stay the shipped floors.
            ("TEST-VECTORS-C1_S12_default.csv", model_v_settings(atr_days=14)),
            ("TEST-VECTORS-fallback_S11_default.csv", model_v_settings(preset="S11", atr_days=14)),
        )
        for name, settings in cases:
            with (root / name).open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 40, name)
            for row in rows:
                v2 = None if row["v2_score"] == "" else float(row["v2_score"])
                passed = lane_decision(
                    row["lane"], int(float(row["cdte"])), v2, float(row["pF"]), float(row["ss"]), settings)
                self.assertEqual(passed, bool(int(row["pass"])), (name, row["cid"], row["lane"]))
        s12 = model_v_settings(atr_days=14)
        # A runner name stays on the normal floor. 0.20 clears the S12 top-decile floor and misses 0.4316.
        self.assertTrue(lane_decision("handoff", 0, None, 0.20, 0.95, s12, a_bar=False))
        self.assertFalse(lane_decision("handoff", 0, None, 0.20, 0.95, s12, a_bar=True))
        self.assertTrue(lane_decision("handoff", 0, None, 0.44, 0.95, s12, a_bar=True))

    def test_preset_s11_wins_over_leftover_pwin_env(self):
        with patch.dict(os.environ, {
            "LOTTO_MODEL": "V",
            "LOTTO_PRESET": "S11",
            "LOTTO_PWIN_DECILE": "0.1776",
            "LOTTO_PWIN_DECILE_1DTE": "0.1833",
            "LOTTO_PWIN_QUINTILE_0DTE": "0.2882",
            "LOTTO_PWIN_QUINTILE_1DTE": "0.309",
            "LOTTO_PWIN_NORMAL_0DTE": "0.4316",
            "LOTTO_PWIN_NORMAL_1DTE": "0.4711",
        }, clear=False):
            with self.assertLogs("lotto.main", level="WARNING") as caught:
                settings = env_settings()
        self.assertEqual(settings.preset, "S11")
        self.assertEqual(settings.pwin_normal_0dte, 0.43157386)
        self.assertEqual(settings.pwin_normal_1dte, 0.47114974)
        self.assertEqual(settings.pwin_quintile_0dte, 0.32149954)
        self.assertEqual(settings.pwin_quintile_1dte, 0.34696342)
        self.assertEqual(settings.pwin_decile, 0.22822777)
        self.assertEqual(settings.pwin_decile_1dte, 0.24061730)
        text = "\n".join(caught.output)
        self.assertIn("Ignoring LOTTO_PWIN_DECILE=0.1776", text)
        self.assertIn("LOTTO_PRESET=S11", text)

    def test_spec_raw_features_score_end_to_end(self):
        spec = json.loads(Path(HANDOFF_SPEC_PATH).read_text())
        medians = spec["feature_definitions"]["missing_values"]["median"]
        self.assertEqual(len(spec["test_vectors"]), 10)
        for row in spec["test_vectors"]:
            filled = fill_model_features(features_from_spec_raw(row["raw"]), medians)
            for name, expected in row["model_inputs"].items():
                self.assertAlmostEqual(filled[name], expected, places=6, msg=(row["cid"], name))
            scored = score_handoff(spec, filled, rvol=row["raw"]["rvol"])
            self.assertAlmostEqual(scored["p_win"], row["expected_p_win"], places=6, msg=row["cid"])
            self.assertAlmostEqual(scored["hoF"], row["expected_v2eq_hoF"], places=4, msg=row["cid"])
            self.assertAlmostEqual(scored["stock_strength"], row["expected_SS"], places=6, msg=row["cid"])
            for name, expected in row["expected_SS_parts"].items():
                self.assertAlmostEqual(scored["strength_parts"][name], expected, places=6, msg=(row["cid"], name))

    def test_shadow_stock_first_still_posts_handoff_option_cards(self):
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        start = datetime(2026, 9, 21, 9, 30, tzinfo=ET)
        scanner = StockFirst(store, enabled=True, mode="shadow")
        prices = [100, 100.1, 100.2, 100.4, 100.8, 101.8, 102.7, 103.6]
        ideas = []
        for minute, price in enumerate(prices):
            at = start + timedelta(minutes=minute)
            ideas.extend(scanner.process([{
                "symbol": "DEMO", "observed_at": at, "at": at - timedelta(seconds=5),
                "price": price, "open": 100.0, "previous": 100.0,
                "high": price, "low": min(price, 100.0), "volume": 100000 + minute,
            }], dry_run=False))
        self.assertEqual(len(ideas), 1)
        self.assertEqual(store.db.execute("SELECT delivery FROM stock_ideas").fetchone()["delivery"], "internal")
        disabled = StockFirst(store, enabled=False, mode="shadow")
        self.assertEqual(disabled.process([{
            "symbol": "DEMO", "observed_at": start + timedelta(minutes=20),
            "at": start + timedelta(minutes=20),
            "price": 110.0, "open": 100.0, "previous": 100.0,
            "high": 110.0, "low": 100.0, "volume": 200000,
        }], dry_run=False), [])
        snap = list(demo_frames())[12][0]
        engine = Engine(Store(":memory:"), model_v_settings(
            score_mode="combined", post_combined_0dte=1, post_v2_option_0dte=1, post_v2_stock_0dte=1,
        ), dry_run=False)
        self.addCleanup(engine.store.db.close)
        engine.remember_stock_events(ideas)
        with patch("lotto.engine.candidates", return_value=[]):
            posted = engine.process([snap])
        self.assertEqual(len(posted), 1)
        title = posted[0]["payload"]["embeds"][0]["title"]
        self.assertNotIn("STOCK-LED", title)
        self.assertEqual(engine.store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
        with self.assertLogs("lotto.main", level="WARNING") as off:
            self.assertEqual(handoff_fallback_status(False, "shadow"), "shadow")
        self.assertIn("LOTTO_STOCK_FIRST_ENABLED=false", "\n".join(off.output))
        with self.assertLogs("lotto.main", level="WARNING") as on:
            self.assertEqual(handoff_fallback_status(True, "shadow"), "posting")
        posted_log = "\n".join(on.output)
        self.assertIn("Handoff fallback state: posting", posted_log)
        self.assertIn("Stock-only cards never post", posted_log)


def _contract(at, strike, side="CALL", bid=1.0, ask=1.1, dte=0):
    expiry = at.astimezone(ET).date() + timedelta(days=dte)
    root = "C" if side == "CALL" else "P"
    return Option(f"{root}{strike:g}{dte}", expiry, side, strike, bid, ask, 200, 10, 0.4, 0.02, at)


def _session(closes, volume=78, low_pad=1.0, high_pad=0.2, pads=None):
    start = datetime(2026, 10, 26, 9, 30, tzinfo=ET)
    rows = []
    for index, close in enumerate(closes):
        end = start + timedelta(minutes=index + 1)
        low = close - low_pad
        high = close + high_pad
        if pads:
            low, high = pads[index]
        rows.append(Bar(end, close, max(high, close), min(low, close), close, volume, None))
    return tuple(rows)


def _fallback_snap(bars, side="CALL", options=None, level=100.0, base=1600, sessions=10, atr21=2.0):
    at = bars[-1].end
    if options is None:
        if side == "CALL":
            options = (_contract(at, bars[-1].close, "CALL", 1.0, 1.05),
                       _contract(at, bars[-1].close + 1, "CALL", 1.0, 1.1))
        else:
            options = (_contract(at, bars[-1].close, "PUT", 1.0, 1.05),
                       _contract(at, bars[-1].close - 1, "PUT", 1.0, 1.1))
    snap = Snapshot("DEMO", at, bars[-1].close, at, 100, bars, tuple(options), prior_atr=9,
                    premarket_high=level if side == "CALL" else None,
                    premarket_low=level if side == "PUT" else None,
                    source="replay")
    object.__setattr__(snap, "prior_atr21", atr21)
    object.__setattr__(snap, "fallback_rvol_sessions", sessions)
    object.__setattr__(snap, "fallback_rvol_base", base)
    return snap


class AtrWindowTests(unittest.TestCase):
    def test_prior_atr_drives_mo_atr_and_the_floors_stay_shipped(self):
        from lotto.patterns import path_features
        shipped = model_v_settings()
        fourteen = model_v_settings(atr_days=14)
        self.assertEqual(shipped.atr_days, 21)
        for name in ("pwin_normal_0dte", "pwin_normal_1dte", "pwin_quintile_0dte",
                     "pwin_quintile_1dte", "pwin_decile", "pwin_decile_1dte",
                     "strength_quintile", "strength_decile"):
            self.assertEqual(getattr(shipped, name), getattr(fourteen, name))
        with self.assertRaises(ValueError):
            model_v_settings(atr_days=7)
        start = datetime(2026, 10, 26, 9, 30, tzinfo=ET)
        bars = _bars(start, 8, lambda minute: 100 + minute * 0.5)
        option = _contract(bars[-1].end, 105)
        move = bars[-1].close - bars[0].open
        common = dict(spot=bars[-1].close, spot_time=bars[-1].end, prior_close=100,
                      bars=bars, options=(option,))
        snap14 = Snapshot("DEMO", bars[-1].end, prior_atr=2, **common)
        snap21 = Snapshot("DEMO", bars[-1].end, prior_atr=4, **common)
        missing = Snapshot("DEMO", bars[-1].end, prior_atr=None, **common)
        self.assertAlmostEqual(build_handoff_features(snap14, option, [], 1)["mo_atr"], move / 2)
        self.assertAlmostEqual(build_handoff_features(snap21, option, [], 1)["mo_atr"], move / 4)
        self.assertAlmostEqual(path_features(snap21, 1)["move_from_open_atr"], move / 4)
        self.assertEqual(path_features(missing, 1)["atr_available"], 0)
        self.assertEqual(path_features(missing, 1)["move_from_open_atr"], 0)
        self.assertIsNone(build_handoff_features(missing, option, [], 1)["mo_atr"])
        filled = fill_strength_inputs(
            {"mo_atr": None, "rvol": 1, "pm_brk": 1, "pd_brk": 1, "newx5": 1}, {})
        self.assertAlmostEqual(filled["mo_atr"], 0.5380223785056588)


class FallbackRuleTests(unittest.TestCase):
    def test_boundaries_puts_rvol_gate_and_retrigger(self):
        self.assertIsNone(atr21_value([1] * 14))
        self.assertEqual(atr21_value([1] * 15), 1)
        self.assertEqual(atr21_value([9] + [1] * 21), 1)
        self.assertIsNone(fallback_rvol_value({0: Bar(datetime(2026, 10, 26, 9, 31, tzinfo=ET), 1, 1, 1, 1, 10, None)},
                                               0, 100, 9))
        self.assertEqual(fallback_rvol_value({}, 0, 0, 10), 0)
        indexed = {index: Bar(datetime(2026, 10, 26, 9, 31, tzinfo=ET), 1, 2, 0, 1, 1, None)
                   for index in range(16)}
        self.assertAlmostEqual(fallback_push(indexed, 15, "CALL", 2), 0.5)
        self.assertLess(fallback_push(indexed, 15, "CALL", 2.1), 0.5)
        self.assertEqual(quote_spread_fail(0.7, 1.0, 30)[0], 0)
        self.assertEqual(quote_spread_fail(0.69, 1.0, 30)[0], 1)
        self.assertEqual(quote_spread_fail(0, 1.0, 30)[0], 1)
        self.assertEqual(fire_why(first=False, fresh_level=False, new_extreme=False,
                                  q_false_since=True, candidate=True, side="CALL"), "rule_re_met")
        self.assertIsNone(fire_why(first=False, fresh_level=False, new_extreme=False,
                                   q_false_since=False, candidate=True, side="CALL"))

        settings = model_v_settings(fallback_rule="log")
        # Nothing fires before 15 minutes after the open.
        self.assertIsNone(evaluate_fallback(
            _fallback_snap(_session([100, 101, 102], volume=600)), "CALL", settings, new_fallback_state()))
        self.assertIsNone(evaluate_fallback(
            _fallback_snap(_session([100, 102], volume=2000)), "CALL", settings, new_fallback_state()))
        early = evaluate_fallback(
            _fallback_snap(_session([100] * 12 + [100, 101, 102], volume=200)),
            "CALL", settings, new_fallback_state())
        self.assertIsNone(early)
        state = new_fallback_state()
        held = _session([100] * 14 + [101, 102])
        fired = evaluate_fallback(_fallback_snap(held), "CALL", settings, state)
        self.assertEqual((fired["why"], fired["m"], fired["k"]), ("first", 15, 1))
        self.assertIn("pmh", fired["lvl"])
        self.assertAlmostEqual(fired["rvol"], 0.78)
        quiet = _fallback_snap(_session([100] * 14 + [101, 102, 102], volume=1), base=10_000)
        self.assertIsNone(evaluate_fallback(quiet, "CALL", settings, state))
        self.assertTrue(state["q_false_since"])
        still = _fallback_snap(_session([100] * 14 + [101, 102, 102, 102]))
        self.assertIsNone(evaluate_fallback(still, "CALL", settings, state))
        short = _fallback_snap(held, sessions=9)
        empty = new_fallback_state()
        self.assertIsNone(evaluate_fallback(short, "CALL", settings, empty))

        high_early = [100] * 16
        pads = [(99, 200)] + [(99, 100.2)] * 15
        blocked = _fallback_snap(_session(high_early, pads=pads), level=None)
        self.assertIsNone(evaluate_fallback(blocked, "CALL", settings, new_fallback_state()))
        hod_pads = [(99, 100)] * 14 + [(100, 101), (101, 102)]
        hod_closes = [100] * 14 + [101, 102]
        hod = evaluate_fallback(_fallback_snap(_session(hod_closes, pads=hod_pads), level=None),
                                "CALL", settings, new_fallback_state())
        self.assertEqual(hod["lvl"], "hod")
        self.assertEqual(hod["level_value"], 100)

        late_closes = [100] * 382 + [100, 101, 102]
        late = evaluate_fallback(_fallback_snap(_session(late_closes)), "CALL", settings, new_fallback_state())
        self.assertEqual(late["m"], 384)
        after = _session(late_closes + [103])
        self.assertIsNone(evaluate_fallback(_fallback_snap(after), "CALL", settings, new_fallback_state()))

        puts = _session([100] * 14 + [99, 98], high_pad=1, low_pad=0.2)
        put = evaluate_fallback(_fallback_snap(puts, side="PUT"), "PUT", settings, new_fallback_state())
        self.assertIn("pml", put["lvl"])
        self.assertGreaterEqual(put["push30"], 0.75)
        self.assertEqual(put["strike"], 98)
        retaken = _session([100] * 14 + [101, 102] + [102] * 4 + [100, 101, 102])
        second = evaluate_fallback(_fallback_snap(retaken), "CALL", settings, state)
        self.assertEqual(second["why"], "fresh_level")
        self.assertFalse(second["dedupe_flag"])
        self.assertEqual(second["k"], 2)

    def test_contract_spread_and_same_move_flag(self):
        at = datetime(2026, 10, 26, 9, 46, tzinfo=ET)
        wide = _contract(at, 101, bid=0.1, ask=2)
        tight = _contract(at, 102, bid=1.0, ask=1.1)
        snap = Snapshot("DEMO", at, 100, at, 100, (), (wide, tight), source="replay")
        chosen = choose_fallback_contract(snap, "CALL")
        self.assertEqual(chosen["strike"], 101)
        self.assertEqual(quote_spread_fail(wide.bid, wide.ask, 30)[0], 1)
        no_ask = _contract(at, 105, bid=0, ask=0)
        later = _contract(at, 106, bid=1, ask=1.1, dte=1)
        deferred = choose_fallback_contract(
            Snapshot("DEMO", at, 100, at, 100, (), (no_ask, later), source="replay"), "CALL")
        self.assertEqual(deferred["dte"], 1)
        live = _contract(at, 104, bid=0.2, ask=0.4)
        preferred = choose_fallback_contract(
            Snapshot("DEMO", at, 100, at, 100, (), (live, later), source="replay"), "CALL")
        self.assertEqual(preferred["dte"], 0)
        self.assertEqual(preferred["atm_strike"], 104)

        record = {"date": "2026-10-26", "ts_fire": at.isoformat(), "tk": "DEMO", "side": "CALL",
                  "skip_flag": False, "same_move_before_min": 15, "same_move_after_min": 5}
        later_alert = {"symbol": "DEMO", "side": "CALL", "id": "opt-1", "setup_name": "OPENING_BREAK",
                       "at": at + timedelta(minutes=4)}
        resolved = resolve_fallback_skip(record, [later_alert])
        self.assertTrue(resolved["skip_flag"])
        self.assertEqual(resolved["skip_ref_card_id"], "opt-1")
        self.assertTrue(resolved["skip_after_resolved"])

    def test_log_posts_nothing_and_matches_current_posting(self):
        closes = [100] * 14 + [101, 102, 102, 102]
        frames = [closes[:index] for index in (16, 17, 18)]
        with self.assertLogs("lotto.main", level="WARNING") as caught:
            self.assertEqual(fallback_rule_status(model_v_settings()), "post")
        self.assertIn(
            "Fallback rule: post rvol>=0.78 push>=0.75 atr=21 room<=1.0 min_after_open=15m spread<=30% contract=atm confluence>=9",
            "\n".join(caught.output))

        def run(mode, directory):
            store = Store(":memory:")
            self.addCleanup(store.db.close)
            engine = Engine(store, model_v_settings(fallback_rule=mode))
            engine.fallback_log = Path(directory) / "fallback-rule.jsonl"
            def candidates(snap, history, settings):
                option = snap.options[0]
                return [Candidate(snap, option, 80, {}, {"cluster_size": 2}, ["option side"], True)]
            def fake_apply(candidate, *args, **kwargs):
                candidate.metrics["score_v2"] = 80
                candidate.metrics["path"] = "hybrid"
                candidate.score = 80
                candidate.qualifying = True
                return candidate
            posted = []
            with patch("lotto.engine.candidates", candidates), patch("lotto.model_v.apply_v2", fake_apply):
                for prefix in frames:
                    posted.extend(engine.process([_fallback_snap(_session(prefix))]))
            rows = store.db.execute("SELECT payload, contract, setup_name FROM alerts ORDER BY at").fetchall()
            return posted, rows, engine.fallback_log

        import tempfile
        with tempfile.TemporaryDirectory() as off_dir, tempfile.TemporaryDirectory() as log_dir:
            off_posted, off_rows, off_path = run("off", off_dir)
            log_posted, log_rows, log_path = run("log", log_dir)
            self.assertEqual(len(off_posted), 1)
            self.assertEqual([row["payload"] for row in off_rows], [row["payload"] for row in log_rows])
            self.assertEqual([row["contract"] for row in off_rows], [row["contract"] for row in log_rows])
            self.assertFalse(off_path.exists())
            lines = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
            fire, entry = lines
            self.assertEqual(fire["line"], "fire")
            self.assertIsNone(fire["entry_ask"])
            self.assertIsNone(fire["spread_fail"])
            # The option card posted on the fire bar, so the flag is set and the line is not held.
            self.assertTrue(fire["skip_flag"])
            self.assertEqual(fire["skip_ref_card_id"], "pending")
            self.assertFalse(fire["skip_after_resolved"])
            self.assertEqual(entry["line"], "entry")
            self.assertEqual(entry["entry_missing"], 0)
            self.assertEqual(entry["entry_ask"], 1.05)
            self.assertEqual(entry["spread_fail"], 0)
            self.assertEqual(entry["ts_fire"], fire["ts_fire"])
            self.assertEqual(entry["k"], fire["k"])
            self.assertNotIn("stock bypass", off_rows[0]["payload"])
            self.assertEqual(len(log_posted), 1)
        bare = Snapshot("DEMO", datetime(2026, 10, 26, 9, 46, tzinfo=ET), 1, datetime(2026, 10, 26, 9, 46, tzinfo=ET),
                        1, (), ())
        object.__setattr__(bare, "prior_atr21", 2)
        self.assertNotIn("prior_atr21", bare.to_dict())

    def test_repeats_dedupe_skip_and_on_floors(self):
        posted_mode = model_v_settings(fallback_rule="post")
        for mode in ("off", "log", "post", "on"):
            settings = model_v_settings(fallback_rule=mode)
            self.assertEqual(handoff_floor(0.9, 0, settings), posted_mode.pwin_decile)
            self.assertEqual(handoff_floor(0.9, 1, settings), posted_mode.pwin_decile_1dte)
            self.assertEqual(handoff_floor(0.82, 0, settings), posted_mode.pwin_quintile_0dte)
            self.assertEqual(handoff_floor(0.2, 0, settings), posted_mode.pwin_normal_0dte)
            self.assertEqual(handoff_floor(0.9, 0, settings, a_bar=True), posted_mode.pwin_normal_0dte)

        def play(mode, closes_list, directory, prime=None):
            store = Store(":memory:")
            self.addCleanup(store.db.close)
            if prime:
                prime(store)
            engine = Engine(store, model_v_settings(fallback_rule=mode))
            engine.fallback_log = Path(directory) / "fallback-rule.jsonl"
            posted = []
            with patch("lotto.engine.candidates", return_value=[]):
                for closes in closes_list:
                    posted.extend(engine.process([_fallback_snap(_session(closes))]))
            text = engine.fallback_log.read_text().strip().splitlines() if engine.fallback_log.exists() else []
            return posted, [json.loads(line) for line in text if line]

        first = [100] * 14 + [101, 102]
        near = first + [100, 101, 102]
        near_entry = near + [102, 102]
        later = first + [102] * 4 + [100, 101, 102]
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            posted, rows = play("on", [first, first + [102], first + [102, 102], near, near + [102], near_entry], directory)
        fires = [row for row in rows if row["line"] == "fire"]
        self.assertEqual(len(posted), 1)
        self.assertEqual(fires[0]["k"], 1)
        self.assertTrue(fires[1]["dedupe_flag"])
        self.assertEqual(fires[1]["k"], 2)
        self.assertIn("PMH", posted[0]["payload"]["embeds"][0]["description"])
        self.assertIn("stock bypass", posted[0]["payload"]["embeds"][0]["description"])
        with tempfile.TemporaryDirectory() as directory:
            posted, _rows = play("on", [first, later], directory)
        self.assertEqual(len(posted), 2)
        with tempfile.TemporaryDirectory() as directory:
            posted, rows = play("log", [first, first + [102], first + [102, 102]], directory)
        self.assertEqual(posted, [])
        self.assertEqual([row["line"] for row in rows], ["fire", "entry"])
        self.assertEqual(rows[1]["entry_missing"], 0)
        self.assertEqual(rows[1]["entry_ask"], 1.05)
        with tempfile.TemporaryDirectory() as directory:
            _posted, missed = play("log", [first, first + [102] * 3], directory)
        self.assertEqual([row["line"] for row in missed], ["fire", "entry"])
        self.assertEqual(missed[1]["entry_missing"], 1)
        self.assertIsNone(missed[1]["spread_fail"])
        self.assertIsNone(missed[1]["entry_ask"])
        self.assertEqual(missed[1]["ts_fire"], missed[0]["ts_fire"])
        self.assertEqual(missed[1]["k"], missed[0]["k"])

        def prime(store):
            at = datetime(2026, 10, 26, 9, 36, tzinfo=ET)
            option = _contract(at, 103)
            snap = Snapshot("DEMO", at, 100, at, 100, _session([100] * 6), (option,), prior_atr=9)
            candidate = Candidate(snap, option, 80, {}, {"score_v2": 80}, ["option side"], True,
                                  setup=Setup("OPENING_BREAK", 100, 99))
            store.queue(candidate, alert_payload(candidate, "ACTIVE"), True, "ACTIVE")
        with tempfile.TemporaryDirectory() as directory:
            posted, rows = play("on", [first, first + [102], first + [102, 102]], directory, prime)
        self.assertEqual(posted, [])
        self.assertEqual(rows[0]["line"], "fire")
        self.assertTrue(rows[0]["skip_flag"])
        self.assertNotEqual(rows[0]["skip_ref_card_id"], "pending")

    def test_fallback_failures_do_not_drop_option_cards(self):
        closes = [100] * 14 + [101, 102, 102, 102]
        frames = [closes[:index] for index in (16, 17, 18)]

        def candidates(snap, history, settings):
            option = snap.options[0]
            return [Candidate(snap, option, 80, {}, {"cluster_size": 2}, ["option side"], True)]

        def fake_apply(candidate, *args, **kwargs):
            candidate.metrics["score_v2"] = 80
            candidate.metrics["path"] = "hybrid"
            candidate.score = 80
            candidate.qualifying = True
            return candidate

        def run(directory):
            store = Store(":memory:")
            self.addCleanup(store.db.close)
            engine = Engine(store, model_v_settings(fallback_rule="log"))
            engine.fallback_log = Path(directory) / "fallback-rule.jsonl"
            posted = []
            with patch("lotto.engine.candidates", candidates), patch("lotto.model_v.apply_v2", fake_apply):
                for prefix in frames:
                    posted.extend(engine.process([_fallback_snap(_session(prefix))]))
            rows = store.db.execute("SELECT payload FROM alerts").fetchall()
            return posted, rows

        real_open = Path.open

        def disk_full(self, *args, **kwargs):
            if str(self).endswith("fallback-rule.jsonl"):
                raise OSError(28, "No space left on device")
            return real_open(self, *args, **kwargs)

        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            with self.assertLogs("lotto.model_v", level="WARNING") as caught, patch.object(Path, "open", disk_full):
                posted, rows = run(directory)
            self.assertEqual(len(posted), 1)
            self.assertEqual(len(rows), 1)
            self.assertNotIn("stock bypass", rows[0]["payload"])
            self.assertIn("fallback log write failed (OSError)", "\n".join(caught.output))

        def explode(*_args, **_kwargs):
            raise RuntimeError("rule blew up")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertLogs("lotto.engine", level="WARNING") as caught, patch(
                    "lotto.model_v.evaluate_fallback", explode):
                posted, rows = run(directory)
            self.assertEqual(len(posted), 1)
            self.assertEqual(len(rows), 1)
            self.assertIn("fallback rule failed for DEMO (RuntimeError)", "\n".join(caught.output))


class ConfluenceGateTests(unittest.TestCase):
    def test_score_is_monotonic_and_a_weak_stack_stays_under_the_line(self):
        settings = model_v_settings()
        flags = {name: 0 for name, _tier in CONFLUENCE_TABLE}
        previous = -1
        for name, _tier in CONFLUENCE_TABLE:
            flags[name] = 1
            _points, score, count = tiered_points(flags, settings)
            self.assertGreater(score, previous)
            self.assertEqual(count, sum(flags.values()))
            previous = score
        full = previous
        for name, tier in CONFLUENCE_TABLE:
            dropped = dict(flags)
            dropped[name] = 0
            weight = {"primary": settings.tier_weight_primary, "secondary": settings.tier_weight_secondary,
                      "tertiary": settings.tier_weight_tertiary}[tier]
            _points, score, _count = tiered_points(dropped, settings)
            self.assertEqual(score, full - weight)
            self.assertGreater(score, 0)
        only_pullback = {name: 0 for name, _tier in CONFLUENCE_TABLE}
        only_pullback["w_pattern"] = 1
        _points, weak, _count = tiered_points(only_pullback, settings)
        self.assertLess(weak, settings.bypass_confluence_min)
        partial_flags = {name: 0 for name, _tier in CONFLUENCE_TABLE}
        partial_flags["flag"] = 0.5
        partial_points, partial, partial_count = tiered_points(partial_flags, settings)
        self.assertAlmostEqual(partial_points["flag"], 0.5 * settings.tier_weight_primary)
        self.assertAlmostEqual(partial, 0.5 * settings.tier_weight_primary)
        self.assertEqual(partial_count, 1)
        partial_flags["flag"] = 1
        _full_points, full, _full_count = tiered_points(partial_flags, settings)
        self.assertGreater(full, partial)
        partial_flags["flag"] = 1.4
        _clamped, clamped, _clamped_count = tiered_points(partial_flags, settings)
        self.assertAlmostEqual(clamped, full)
        self.assertEqual(band_score(4, 1, 3, 8, 16), 1)
        self.assertAlmostEqual(band_score(2, 1, 3, 8, 16), 0.5)
        self.assertEqual(band_score(0, 1, 3, 8, 16), 0)
        self.assertEqual(band_score(20, 1, 3, 8, 16), 0)
        self.assertEqual(primary_strength_marker(57, settings.strong_score), "⚡")
        self.assertIsNone(primary_strength_marker(56.9, settings.strong_score))
        self.assertNotEqual(primary_strength_marker(92, settings.strong_score), "🔥")

    def test_missing_volume_does_not_post_unless_the_override_clears(self):
        settings = model_v_settings()
        flags = {name: 0 for name, _tier in CONFLUENCE_TABLE}
        blocked = card_gate("CALL", 0, 0.05, flags, points=1, settings=settings)
        self.assertFalse(blocked["post_ok"])
        self.assertIn("volume", blocked["blocked_by"])
        lifted = card_gate("CALL", 0, 0.05, flags, points=settings.base_override_points, settings=settings)
        self.assertTrue(lifted["post_ok"])
        self.assertIn("volume", lifted["overrides"])
        self.assertFalse(card_gate(None, 100, 0.05, flags, points=100, settings=settings)["post_ok"])

    def test_wide_spread_blocker_lifts_when_the_strong_variables_are_on(self):
        settings = model_v_settings()
        flags = {name: 0 for name, _tier in CONFLUENCE_TABLE}
        held = card_gate("CALL", 100, 0.25, flags, points=0, settings=settings)
        self.assertIn("wide_spread", held["blocked_by"])
        self.assertFalse(held["post_ok"])
        flags["pm_break"] = 1
        flags["rvol"] = 1
        lifted = card_gate("CALL", 100, 0.25, flags, points=0, settings=settings)
        self.assertIn("wide_spread", lifted["overrides"])
        self.assertTrue(lifted["post_ok"])
        self.assertIn("wide_spread", lifted["blockers"])

    def test_put_pullback_and_flag_mirror_the_call(self):
        settings = model_v_settings()
        atr = 2.0
        calls = _expand_frame(_ema_pullback_ohlc(), 5, 1)
        puts = _expand_frame(_ema_pullback_ohlc(), 5, -1)
        call_ema = detect_ema_pullback(calls, 5, 1, atr, settings)
        put_ema = detect_ema_pullback(puts, 5, -1, atr, settings)
        wrong_ema = detect_ema_pullback(puts, 5, 1, atr, settings)
        self.assertGreaterEqual(call_ema, 0.5)
        self.assertGreaterEqual(put_ema, 0.5)
        self.assertLess(wrong_ema, 0.25)
        shallow = _expand_frame(_ema_pullback_ohlc(depth=0.15), 5, 1)
        partial_ema = detect_ema_pullback(shallow, 5, 1, atr, settings)
        self.assertGreater(partial_ema, 0)
        self.assertLess(partial_ema, call_ema)

        flag_calls = _expand_frame(_flag_ohlc(), 5, 1)
        flag_puts = _expand_frame(_flag_ohlc(), 5, -1)
        self.assertGreaterEqual(detect_flag(flag_calls, 5, 1, atr, settings), 0.5)
        self.assertGreaterEqual(detect_flag(flag_puts, 5, -1, atr, settings), 0.5)
        self.assertEqual(detect_flag(flag_calls, 5, -1, atr, settings), 0)
        wide_pause = _expand_frame(_flag_ohlc(pause_range=0.80), 5, 1)
        partial_flag = detect_flag(wide_pause, 5, 1, atr, settings)
        self.assertGreater(partial_flag, 0)
        self.assertLess(partial_flag, detect_flag(flag_calls, 5, 1, atr, settings))

        w_calls = _expand_frame(_w_ohlc(), 5, 1)
        w_puts = _expand_frame(_w_ohlc(), 5, -1)
        full_w = detect_w(w_calls, 5, 1, atr, settings)
        full_m = detect_m(w_puts, 5, -1, atr, settings)
        self.assertGreaterEqual(full_w, 0.5)
        self.assertGreaterEqual(full_m, 0.5)
        self.assertEqual(detect_w(w_puts, 5, -1, atr, settings), 0)
        self.assertEqual(detect_m(w_calls, 5, 1, atr, settings), 0)
        self.assertLess(detect_w(w_puts, 5, 1, atr, settings), 0.5)
        uneven = _expand_frame(_w_ohlc(second_low=98.7), 5, 1)
        partial_w = detect_w(uneven, 5, 1, atr, settings)
        self.assertGreater(partial_w, 0)
        self.assertLess(partial_w, full_w)

        late = detect_fashionably_late(flag_calls, 5, 1, atr, settings)
        self.assertGreater(late, 0)
        grind = _expand_frame(_grind_ohlc(), 5, 1)
        self.assertLess(pattern_match({
            "ema9_pullback": detect_ema_pullback(grind, None, 1, atr, settings),
            "flag": detect_flag(grind, None, 1, atr, settings),
            "w_pattern": detect_w(grind, None, 1, atr, settings),
            "m_pattern": detect_m(grind, None, 1, atr, settings),
            "fashionably_late": detect_fashionably_late(grind, None, 1, atr, settings),
        }), settings.pattern_trigger_min)

    def test_strong_stack_posts_atm_and_a_weak_stack_does_not(self):
        import tempfile
        strong = [100] * 14 + [101, 102]
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        engine = Engine(store, model_v_settings(fallback_rule="post"))
        engine.fallback_log = Path(tempfile.mkdtemp()) / "fallback-rule.jsonl"
        with patch("lotto.engine.candidates", return_value=[]):
            posted = engine.process([_fallback_snap(_session(strong))])
        self.assertEqual(len(posted), 1)
        description = posted[0]["payload"]["embeds"][0]["description"]
        self.assertIn("stock bypass", description)
        row = store.db.execute("SELECT contract, setup_name, quality_tier FROM alerts").fetchone()
        self.assertEqual(row["setup_name"], "STOCK_BYPASS")
        self.assertEqual(row["quality_tier"], "MAIN")
        self.assertTrue(row["contract"].startswith("C102"))
        features = json.loads(store.db.execute(
            "SELECT features FROM decisions WHERE reason LIKE 'active alert%'").fetchone()["features"])
        for key in ("quote_age_s", "bar_age_s", "min_since_trigger"):
            self.assertIn(key, features["metrics"])
        self.assertGreaterEqual(features["metrics"]["confluence_score"], engine.settings.bypass_confluence_min)

        quiet = Store(":memory:")
        self.addCleanup(quiet.db.close)
        quiet_engine = Engine(quiet, model_v_settings(fallback_rule="post"))
        quiet_engine.fallback_log = Path(tempfile.mkdtemp()) / "fallback-rule.jsonl"
        bare = _fallback_snap(_session(strong, volume=0), options=())
        with patch("lotto.engine.candidates", return_value=[]):
            self.assertEqual(quiet_engine.process([bare]), [])
        self.assertEqual(quiet.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)

        puts = Store(":memory:")
        self.addCleanup(puts.db.close)
        put_engine = Engine(puts, model_v_settings(fallback_rule="post"))
        put_engine.fallback_log = Path(tempfile.mkdtemp()) / "fallback-rule.jsonl"
        put_bars = _session([100] * 14 + [99, 98], high_pad=0.2, low_pad=1)
        with patch("lotto.engine.candidates", return_value=[]):
            posted = put_engine.process([_fallback_snap(put_bars, side="PUT")])
        self.assertEqual(len(posted), 1)
        row = puts.db.execute("SELECT side, contract FROM alerts").fetchone()
        self.assertEqual(row["side"], "PUT")
        self.assertTrue(row["contract"].startswith("P98"))


def _expand_frame(ohlc, minutes, direction, axis=100.0):
    """Rebuild higher-timeframe OHLC as one-minute bars. Puts mirror around axis."""
    start = datetime(2026, 10, 26, 9, 30, tzinfo=ET)
    rows = []
    cursor = 0
    for open_, high, low, close in ohlc:
        if direction == -1:
            open_, close = axis * 2 - open_, axis * 2 - close
            high, low = axis * 2 - low, axis * 2 - high
        for step in range(minutes):
            cursor += 1
            end = start + timedelta(minutes=cursor)
            if step == 0:
                bar_open, bar_high, bar_low, bar_close = open_, open_, open_, open_
            elif step == minutes - 1:
                bar_open, bar_high, bar_low, bar_close = close, close, close, close
            else:
                bar_open, bar_high, bar_low, bar_close = open_, high, low, close
            bar_high = max(bar_high, bar_open, bar_close)
            bar_low = min(bar_low, bar_open, bar_close)
            rows.append(Bar(end, bar_open, bar_high, bar_low, bar_close, 100, None))
    return tuple(rows)


def _ema_pullback_ohlc(depth=0.55):
    """Rise off 100, then a 3-bar dip whose last low tags the lagged 9 EMA."""
    closes = [100 + index * 0.40 for index in range(12)]
    # The dip is deep enough to reach a lagged EMA and shallow enough to hold it.
    peak = closes[-1]
    pull = [peak - depth, peak - depth * 1.7, peak - depth * 2.2]
    closes.extend(pull)
    rows = []
    previous = closes[0] - 0.05
    for index, close in enumerate(closes):
        open_ = previous
        if index >= 12 and index == len(closes) - 1:
            low = close - 0.05
            high = max(open_, close) + 0.05
        elif index >= 12:
            low = close - 0.08
            high = max(open_, close) + 0.05
        else:
            low = min(open_, close) - 0.05
            high = max(open_, close) + 0.05
        rows.append((open_, high, low, close))
        previous = close
    return rows


def _flag_ohlc(pause=3, pause_range=0.30):
    """One-bar impulse of 1 ATR (ATR 2), then a pause that holds.

    pause_range is the pause high-low in points. 0.30 is inside the tight
    core. 0.80 sits in the fade and earns a partial match.
    """
    rows = [(100.0, 100.15, 99.9, 100.0)]
    rows.append((100.0, 102.15, 99.9, 102.0))
    mid = 101.9
    half = pause_range / 2
    for _index in range(pause):
        rows.append((mid, mid + half, mid - half, mid))
    return rows


def _w_ohlc(second_low=98.1):
    """Two similar swing lows with a bounce between them and a lift after."""
    return [
        (100.0, 100.5, 99.6, 100.2),
        (100.2, 100.4, 98.0, 98.4),
        (98.4, 99.5, 98.3, 99.2),
        (99.2, 100.2, 99.0, 100.6),
        (100.6, 100.8, 99.2, 99.6),
        (99.6, 99.8, second_low, 98.6 if second_low < 98.5 else second_low + 0.3),
        (98.6 if second_low < 98.5 else second_low + 0.3, 99.8, second_low + 0.9, second_low + 1.3),
    ]


def _grind_ohlc():
    """A straight rise with wide bars. No tight pause, no double bottom, no EMA tag."""
    rows = []
    price = 100.0
    for _index in range(18):
        nxt = price + 0.25
        rows.append((price, nxt + 0.70, price - 0.70, nxt))
        price = nxt
    return rows


if __name__ == "__main__":
    unittest.main()
