import csv
import json
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from lotto.config import HANDOFF_SPEC_PATH, Settings, model_v_settings
from lotto.demo import demo_frames
from lotto.discord import deliver_stock_pending
from lotto.engine import Engine
from lotto.features import Candidate
from lotto.main import env_settings, handoff_fallback_status, standby_symbols
from lotto.model_v import (apply_v2, build_handoff_features, cap_stock, classify_runner,
                           due_standby, exceptional_stock, features_from_spec_raw,
                           fill_model_features, fill_strength_inputs, invert_live_blend,
                           lane_decision, level_retest, pick_one_otm, post_bar,
                           quote_in_play, quote_universe, score_handoff)
from lotto.stock_first import StockFirst
from lotto.models import Bar, ET, Option
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
                         "LOTTO_STRENGTH_DISTRIBUTION_PATH"):
                os.environ.pop(name, None)
            live = env_settings()
        self.assertEqual(live.model, "V")
        self.assertEqual(live.min_score, 60)
        self.assertEqual(live.score_mode, "s12")
        self.assertEqual(live.preset, "S12")
        self.assertEqual(live.standby_names, 0)
        self.assertEqual(live.pwin_normal_0dte, 0.43157386)
        self.assertEqual(live.pwin_normal_1dte, 0.47114974)
        self.assertEqual(live.pwin_quintile_0dte, 0.28822075)
        self.assertEqual(live.pwin_decile, 0.17760635)
        self.assertEqual(live.pwin_decile_1dte, 0.18333891)
        self.assertTrue(live.handoff_model_path.endswith("handoff_model_v1026.json"))
        self.assertEqual(live.handoff_model_path, live.strength_distribution_path)
        self.assertEqual(live.handoff_model_path, HANDOFF_SPEC_PATH)
        self.assertNotIn("PLACEHOLDER", live.preset_note)
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
        live = model_v_settings()
        s11 = model_v_settings(preset="S11")
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
            ("TEST-VECTORS-C1_S12_default.csv", model_v_settings()),
            ("TEST-VECTORS-fallback_S11_default.csv", model_v_settings(preset="S11")),
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
        s12 = model_v_settings()
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


if __name__ == "__main__":
    unittest.main()
