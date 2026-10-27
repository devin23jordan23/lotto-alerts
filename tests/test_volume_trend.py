"""Volume-trend profiles, Vince V2 sector/SPY rows, Ace volume rows, and stacks."""
from datetime import date, datetime, timedelta
import unittest

from lotto.models import ET, Bar, Option
from lotto.volume_trend import (
    DEEP_SLOTS, DIVERGENCE_FLAGS, EXTRA_UNIVERSE, EXTRA_UNIVERSE_MAX, NO_SECTOR_ETF,
    PAID_MARKET_DATA_PER_MINUTE,
    PRIMARY_WEIGHTS, PROFILES, SECTOR_BASKETS, VOL_ALIASES, VOLUME_TREND_RULES,
    alias_rows, alignment_features, baskets_for, call_budget, candidate_etfs, choose_sector_etf,
    _divergence_tier_points, divergence_sector, divergence_weight_spec, ensure_etf_typicals,
    etf_for, etf_multiplier, etf_typical_score, etf_typical_table,
    evaluate_stacks, is_volume_trend,
    overlay_book, penalty_opt_vol_points, penalty_spread_points, poll_cycle_calls, rally_breadth,
    rally_points, reference_close, score_volume_trend, sector_feature_choice, sector_map_names,
    sector_quote_symbols,
    select_extra_names, spy_turn, spy_window_return, stacks_from_rules, vol_bin_flags,
    volume_trend_lines, volume_trend_symbols,
)
from lotto.vt_measure import (
    adjacent_strike_count, band_score, close_loc_score, hold_score,
    measure_volume, option_rows, range_atr_score, room_score, rvol_cum_chain,
    sust_score, sustained_share, volume_steady,
)
from lotto.config import model_v_settings
from lotto.confluence import apply_card_layers
from lotto.features import Candidate
from lotto.model_v import option_extras
from lotto.models import Snapshot
from lotto.weights import active_profile, load_weights, score_path


def _bar(minute, close, volume, span=0.4):
    end = datetime(2026, 6, 2, 9, 31, tzinfo=ET) + timedelta(minutes=minute)
    return Bar(end, close, close + span / 2, close - span / 2, close, int(volume))


def _flat_tod(level=100.0):
    curve = {minute: level for minute in range(390)}
    return {10: curve, 5: curve, 3: curve, "any": curve}


class ProfileTests(unittest.TestCase):
    def test_lines_and_live_book_stay_put(self):
        self.assertEqual(volume_trend_lines("volume_trend")["post_threshold"], 100)
        self.assertEqual(volume_trend_lines("volume_trend_aggressive")["post_threshold"], 89)
        self.assertEqual(volume_trend_lines("volume_trend_conservative")["post_threshold"], 117)
        self.assertEqual(active_profile(load_weights(), "balanced")["post_threshold"], 84)
        self.assertEqual(active_profile(load_weights(), "conservative")["post_threshold"], 92)
        book = load_weights()
        self.assertEqual(set(book["profiles"]), {"aggressive", "middle", "balanced", "conservative"})
        before = book["weights"]["sp5"]["call"]
        over = overlay_book(book, "volume_trend_balanced")
        self.assertEqual(book["weights"]["sp5"]["call"], before)
        self.assertEqual(over["weights"]["sp5"]["call"], 20)
        self.assertEqual(over["weights"]["SPY_ALIGN_TURN_3M"]["call"], 15)
        self.assertEqual(over["weights"]["SECTOR_ETF_PCT_TS"]["call"], 16)
        self.assertEqual(over["weights"]["SECTOR_L2_PMH"]["call"], 0)
        self.assertEqual(over["weights"]["SECTOR_ETF_PCT"]["call"], 0)
        self.assertEqual(over["weights"]["SECTOR_PEER_COUNT"]["call"], 33)
        self.assertEqual(over["weights"]["SPY_ALIGN_OPEN"]["call"], 1)
        self.assertNotIn("SPY_ALIGN_TURN_3M", book["bypass_weights"])
        for name in ("volume_trend_aggressive", "volume_trend_balanced", "volume_trend_conservative"):
            self.assertEqual(PROFILES[name]["primary"], PRIMARY_WEIGHTS)
            self.assertEqual(PROFILES[name]["bypass_threshold"], 40)
        self.assertTrue(is_volume_trend("volume_trend"))
        self.assertFalse(is_volume_trend("balanced"))

    def test_universe_keeps_iwm_and_moves_standby(self):
        names = volume_trend_symbols(["DIA", "IWM", "SMH", "MCD", "JPM", "SPY"])
        self.assertIn("IWM", names)
        self.assertIn("JPM", names)
        self.assertIn("SPY", names)
        self.assertIn("IREN", names)
        self.assertNotIn("DIA", names)
        self.assertNotIn("SMH", names)
        self.assertNotIn("MCD", names)


class SpySectorTests(unittest.TestCase):
    def test_turn_window_is_three_and_a_half_minutes(self):
        self.assertEqual(VOLUME_TREND_RULES["SPY_ALIGN_TURN_WINDOW_MIN"], 3.5)
        # Last bar at minute 10. 3.5 minutes back is halfway between minute 6 and 7.
        closes = [100.0] * 11
        closes[6] = 100.0
        closes[7] = 102.0
        closes[10] = 101.0 * 1.0008
        bars = [_bar(i, closes[i], 100) for i in range(11)]
        ref = reference_close(bars, 3.5)
        self.assertAlmostEqual(ref, 101.0, places=6)
        self.assertAlmostEqual(spy_window_return(bars, 1, 3.5), 0.08, places=4)
        self.assertEqual(spy_turn(bars, 1, 3.5), 1.0)
        self.assertEqual(spy_turn(bars, 1, 3.0), 0.0)
        put_closes = [100.0] * 11
        put_closes[6] = 100.0
        put_closes[7] = 98.0
        put_closes[10] = 99.0 * 0.9992
        put_bars = [_bar(i, put_closes[i], 100) for i in range(11)]
        self.assertAlmostEqual(reference_close(put_bars, 3.5), 99.0, places=6)
        self.assertEqual(spy_turn(put_bars, -1, 3.5), 1.0)
        self.assertEqual(spy_turn(put_bars, 1, 3.5), 0.0)
        self.assertEqual(spy_turn(bars, -1, 3.5), 0.0)

    def test_turn_band_edges(self):
        bars = [_bar(i, 100.0, 100) for i in range(6)]
        bars[-1] = _bar(5, 100 * 1.0006, 100)
        self.assertEqual(spy_turn(bars, 1, 3.0), 1.0)
        bars[-1] = _bar(5, 100.20, 100)
        self.assertEqual(spy_turn(bars, 1, 3.0), 0.0)
        bars[-1] = _bar(5, 99.92, 100)
        self.assertEqual(spy_turn(bars, -1, 3.0), 1.0)

    def test_rally_tiers_and_put_mirror(self):
        names = sector_map_names()
        self.assertGreater(len(names), 50)

        def table(fraction, side):
            out = {}
            hot = int(round(fraction * len(names)))
            for index, name in enumerate(names):
                if index < hot:
                    out[name] = 0.4 if side > 0 else -0.4
                else:
                    out[name] = -0.1 if side > 0 else 0.1
            return out

        self.assertEqual(rally_points(rally_breadth(1, table(0.70, 1))), 18)
        self.assertEqual(rally_points(rally_breadth(1, table(0.80, 1))), 18)
        self.assertEqual(rally_points(rally_breadth(1, table(0.95, 1))), 0)
        self.assertEqual(rally_points(rally_breadth(1, table(0.50, 1))), 0)
        self.assertEqual(rally_points(rally_breadth(-1, table(0.70, -1))), 18)
        self.assertEqual(rally_points(rally_breadth(-1, table(0.80, -1))), 18)
        self.assertEqual(rally_points(rally_breadth(-1, table(0.95, -1))), 0)
        self.assertEqual(rally_points(rally_breadth(1, table(0.70, -1))), 0)
        self.assertEqual(rally_points(0.75), 18)
        self.assertEqual(rally_points(0.90), 0)

    def test_etf_typical_and_peer_count_both_sides(self):
        calls = alignment_features(direction=1, etf_pct=2.0, etf_typical=1.0, peers=[
            {"symbol": "A", "open": 10, "last": 11},
            {"symbol": "B", "open": 10, "last": 11},
            {"symbol": "C", "open": 10, "last": 11},
        ])
        puts = alignment_features(direction=-1, etf_pct=-2.0, etf_typical=1.0, peers=[
            {"symbol": "A", "open": 10, "last": 9},
            {"symbol": "B", "open": 10, "last": 9},
            {"symbol": "C", "open": 10, "last": 9},
        ])
        self.assertEqual(calls["SECTOR_ETF_PCT_TS"], 1.0)
        self.assertEqual(puts["SECTOR_ETF_PCT_TS"], 1.0)
        self.assertEqual(alignment_features(direction=1, etf_pct=-2.0, etf_typical=1.0)["SECTOR_ETF_PCT_TS"], 0.0)
        self.assertEqual(alignment_features(direction=1, etf_pct=1.0, etf_typical=1.0)["SECTOR_ETF_PCT_TS"], 0.0)
        self.assertEqual(calls["SECTOR_PEER_COUNT"], 1.0)
        self.assertEqual(puts["SECTOR_PEER_COUNT"], 1.0)
        two = alignment_features(direction=1, peers=[
            {"symbol": "A", "open": 10, "last": 11},
            {"symbol": "B", "open": 10, "last": 11},
        ])
        self.assertEqual(two["SECTOR_PEER_COUNT"], 0.0)
        missing = alignment_features(direction=1, peers=[{"symbol": "DASH"}])
        self.assertEqual(missing["SECTOR_PEER_COUNT"], 0.0)


class VolumeRowTests(unittest.TestCase):
    def _levels(self, direction):
        if direction > 0:
            return {"prior_high": 100.0, "high_5d": 100.0, "premarket_high": 90.0}
        return {"prior_low": 100.0, "low_5d": 100.0, "premarket_low": 110.0}

    def _series(self, direction):
        # Eleven quiet bars, then a break bar, then five hold bars.
        closes = []
        volumes = []
        for i in range(11):
            closes.append(99.0 if direction > 0 else 101.0)
            volumes.append(100)
        closes.append(101.0 if direction > 0 else 99.0)
        volumes.append(250)
        for _ in range(5):
            closes.append(102.0 if direction > 0 else 98.0)
            volumes.append(150)
        return [_bar(i, closes[i], volumes[i], span=0.5) for i in range(len(closes))]

    def test_each_volume_row_and_put_mirror(self):
        rules = VOLUME_TREND_RULES
        tod = _flat_tod(100)
        for direction in (1, -1):
            bars = self._series(direction)
            measured = measure_volume(
                bars, direction, self._levels(direction), atr=2.0, tod=tod,
                cum_curves={10: 1000}, full_day=39000, rules=rules, symbol="TEST")
            raw, flags = measured["raw"], measured["flags"]
            self.assertGreater(raw["BRK_VS_DAY"], 1.5)
            self.assertGreater(flags["BRK_VS_DAY"], 0)
            self.assertGreater(raw["BRK_TOD_10"], 1.5)
            self.assertEqual(flags["BRK_BAND"], 1.0)
            self.assertGreater(flags["BU_5_DAY"], 0)
            self.assertGreater(flags["BU_3_DAY"], 0)
            self.assertIn(measured["sources"]["RVOL_CUM"], {"RVOL_CUM_10"})
            self.assertGreater(flags["RVOL_CUM"], 0)
            self.assertGreater(flags["RVOL_CUM_TIMESCALE"], 0)
            self.assertGreater(flags["CLOSE_LOC"], 0)
            self.assertGreater(flags["RANGE_ATR"], 0)
            self.assertEqual(flags["LEVEL_BREAK"], 1.0)
            self.assertEqual(flags["BRK_VOL_PDH_R5"], 1.0)
            self.assertGreater(flags["HOLD_ON_VOL"], 0)
            self.assertEqual(flags["ROOM"], 0.0)
            early = measure_volume(
                bars[:12], direction, self._levels(direction), atr=2.0, tod=tod,
                rules=rules, symbol="TEST")
            self.assertEqual(early["flags"]["HOLD_ON_VOL"], 0.0)

    def test_fallback_chain_and_premarket_extension(self):
        ratio, source = rvol_cum_chain(500, {3: 250}, None, None, 10)
        self.assertEqual(source, "RVOL_CUM_3")
        self.assertEqual(ratio, 2.0)
        ratio, source = rvol_cum_chain(500, {}, 1.4, None, 10)
        self.assertEqual(source, "BAR_VS_DAY")
        ratio, source = rvol_cum_chain(100, {}, None, 39000, 38)
        self.assertEqual(source, "RVOL_CUM_TIMESCALE")
        self.assertAlmostEqual(ratio, 100 / (39000 * 39 / 390), places=6)
        ratio, source = rvol_cum_chain(100, {}, None, None, 10)
        self.assertEqual((ratio, source), (1.0, "NEUTRAL"))
        bars = [_bar(i, 101 if i == 2 else 99, 100 if i < 2 else 200) for i in range(3)]
        plain = measure_volume(bars, 1, {"prior_high": 100}, 2.0, premarket=None, rules=VOLUME_TREND_RULES)
        wide = measure_volume(
            bars, 1, {"prior_high": 100}, 2.0, premarket=[1000, 1000], rules=VOLUME_TREND_RULES)
        self.assertGreater(plain["raw"]["BRK_VS_DAY"], wide["raw"]["BRK_VS_DAY"])

    def test_sustained_band_room_and_aliases(self):
        rules = VOLUME_TREND_RULES
        tod = _flat_tod(100)
        hot = [_bar(i, 100, 200) for i in range(5)]
        mixed = [_bar(i, 100, 200 if i >= 3 else 50) for i in range(5)]
        self.assertEqual(sustained_share(hot, tod, 5, 1.2, False, 1), 1.0)
        self.assertEqual(sust_score(1.0, rules), 1.0)
        self.assertEqual(sust_score(0.6, rules), 0.0)
        self.assertAlmostEqual(sust_score(0.8, rules), 0.5)
        self.assertEqual(band_score(2.0, 1.2, 3.0, 4.0, 8.0), 1.0)
        self.assertEqual(band_score(9.0, 1.2, 3.0, 4.0, 8.0), 0.0)
        self.assertGreater(band_score(5.0, 1.2, 3.0, 4.0, 8.0), 0)
        self.assertLess(band_score(5.0, 1.2, 3.0, 4.0, 8.0), 1)
        self.assertEqual(room_score(0.5, rules), 1.0)
        self.assertEqual(room_score(1.5, rules), 0.0)
        up = _bar(0, 10, 100, span=1.0)
        # Rebuild so the close sits in the top 30% and the bottom 30%.
        top = Bar(up.end, 9.8, 10.0, 9.0, 9.8, 100)
        bottom = Bar(up.end, 9.2, 10.0, 9.0, 9.2, 100)
        self.assertEqual(close_loc_score(top, 1, rules), 1.0)
        self.assertEqual(close_loc_score(bottom, -1, rules), 1.0)
        self.assertEqual(close_loc_score(bottom, 1, rules), 0.0)
        self.assertEqual(close_loc_score(top, -1, rules), 0.0)
        self.assertGreater(range_atr_score(0.4, 2.0, rules), 0)
        for alias, spec in VOL_ALIASES.items():
            self.assertEqual(PRIMARY_WEIGHTS[alias], 0)
            for primary in alias_rows(spec):
                self.assertIn(primary, PRIMARY_WEIGHTS)

    def test_remapped_aliases_name_live_rows_and_add_no_points(self):
        expected = {
            "VOL_RVOL_TOD": ("RVOL_CUM_15_25", "RVOL_CUM_ge25"),
            "VOL_PRE_BREAK": ("BU_5_TOD_10",),
            "VOL_BREAK_BAR": ("BRK_TOD_BRK_08_12", "BRK_TOD_BRK_12_3", "BRK_TOD_BRK_ge3"),
        }
        book = overlay_book(load_weights(), "volume_trend_balanced")
        self.assertEqual(PRIMARY_WEIGHTS["BU_5_DAY_ge1"], 0)
        for name in ("volume_trend_aggressive", "volume_trend_balanced", "volume_trend_conservative"):
            self.assertEqual(PROFILES[name]["primary"]["BU_5_DAY_ge1"], 0)
        live = load_weights()
        self.assertNotIn("BU_5_DAY_ge1", live["weights"])
        self.assertNotIn("BU_5_DAY_ge1", live.get("penalties") or {})
        over = overlay_book(live, "volume_trend_balanced")
        self.assertEqual(over["weights"]["BU_5_DAY_ge1"], {"call": 0, "put": 0})
        self.assertNotIn("BU_5_DAY_ge1", live["weights"])
        for alias, rows in expected.items():
            self.assertEqual(alias_rows(VOL_ALIASES[alias]), rows)
            self.assertEqual(PRIMARY_WEIGHTS[alias], 0)
            for primary in rows:
                self.assertGreater(PRIMARY_WEIGHTS[primary], 0)
                live = {primary: 1}
                _points, score, _positive = score_path(live, "CALL", book)
                aliased = {primary: 1, alias: 1}
                _again, with_alias, _pos = score_path(aliased, "CALL", book)
                self.assertEqual(with_alias, score)
                self.assertEqual(_again.get(alias, 0), 0)


class StackTests(unittest.TestCase):
    def _features(self, **overrides):
        base = {
            "OPT_VOL_ADJ_STRIKES": 3,
            "LEVEL_BREAK": 1,
            "SECTOR_HIT": 1,
            "BRK_VOL_PDH_R5": 1,
            "BRK_TOD_10": 1.8,
            "TB_HOLD": 1,
        }
        base.update(overrides)
        return base

    def test_all_four_three_of_four_and_missing_option_data(self):
        full, names = evaluate_stacks(self._features())
        self.assertEqual(full, 0)
        self.assertEqual(names, ["STACK_APLUS"])
        partial, names = evaluate_stacks(self._features(OPT_VOL_ADJ_STRIKES=0))
        self.assertEqual(partial, 0)
        self.assertEqual(names, ["STACK_BONUS_3OF4"])
        missing, names = evaluate_stacks(self._features(OPT_VOL_ADJ_STRIKES=None))
        self.assertEqual(missing, 0)
        self.assertEqual(names, ["STACK_BONUS_3OF4"])
        absent = dict(self._features())
        del absent["OPT_VOL_ADJ_STRIKES"]
        points, names = evaluate_stacks(absent)
        self.assertEqual((points, names), (0, ["STACK_BONUS_3OF4"]))
        none, names = evaluate_stacks({})
        self.assertEqual((none, names), (0, []))

    def test_generic_stack_and_put_features(self):
        custom = [{
            "name": "VOL_X_HOLD",
            "points": 7,
            "conditions": [
                {"feature": "BRK_TOD_10", "op": ">=", "threshold": 1.5},
                {"feature": "TB_HOLD", "op": "==", "threshold": 1},
            ],
        }]
        points, names = evaluate_stacks(self._features(), custom)
        self.assertEqual((points, names), (7, ["VOL_X_HOLD"]))
        points, names = evaluate_stacks({"BRK_TOD_10": 1.8}, custom)
        self.assertEqual(points, 0)
        rules = VOLUME_TREND_RULES
        puts = measure_volume(
            [_bar(i, 101 if i < 11 else 99, 100 if i < 11 else 250) for i in range(12)],
            -1, {"prior_low": 100, "low_5d": 100}, 2.0, tod=_flat_tod(100), rules=rules)
        self.assertEqual(puts["flags"]["LEVEL_BREAK"], 1.0)
        self.assertEqual(puts["flags"]["BRK_VOL_PDH_R5"], 1.0)
        calls_on_put_tape = measure_volume(
            [_bar(i, 101 if i < 11 else 99, 100 if i < 11 else 250) for i in range(12)],
            1, {"prior_high": 110}, 2.0, tod=_flat_tod(100), rules=rules)
        self.assertEqual(calls_on_put_tape["flags"]["LEVEL_BREAK"], 0.0)

    def test_option_rows_do_not_raise_when_data_is_missing(self):
        self.assertEqual(adjacent_strike_count(None, None, 100), 0)
        self.assertEqual(option_rows(None, None, None, VOLUME_TREND_RULES)["OPT_VOL_ADJ_STRIKES"], 0)
        self.assertEqual(volume_steady(None, 3), 0)
        self.assertEqual(volume_steady([10, None, 12], 3), 0)
        expiry = date(2026, 6, 2)
        when = datetime(2026, 6, 2, 10, 0, tzinfo=ET)
        def opt(strike, volume, side="CALL"):
            vol = 0 if volume is None else volume
            return Option(f"{side}{strike}", expiry, side, strike, 0.4, 0.5, vol, 10, 0.4, 0.01, when)
        picked = opt(100, 200)
        chain = [opt(90, 10), opt(95, 150), opt(100, 200), opt(105, 180), opt(110, 50), opt(100, 10, "PUT")]
        # 95, 100, 105 clear 100. 90 and 110 do not. The put is a different side.
        self.assertEqual(adjacent_strike_count(chain, picked, 100, 2), 3)
        broken = [opt(100, 0)]
        broken[0] = type("Bad", (), {})()
        self.assertEqual(adjacent_strike_count([broken[0]], picked, 100), 0)
        self.assertEqual(volume_steady([100, 100, 120], 3), 1)
        self.assertEqual(volume_steady([100, 90, 120], 3), 0)

    def test_primary_score_adds_rally_and_stack_points(self):
        features = {
            "SPY_ALIGN_TURN_3M": 1,
            "SECTOR_ETF_PCT_TS": 1,
            "_rally_points": 19,
            "_vt_raw": {"BRK_TOD_10": 2.0, "BRK_VS_10": 2.0},
            "OPT_VOL_ADJ_STRIKES": 4,
            "LEVEL_BREAK": 1,
            "SECTOR_HIT": 1,
            "BRK_VOL_PDH_R5": 1,
        }
        points, score, _positive, fired = score_volume_trend(
            features, "CALL", load_weights(), "primary", "volume_trend_balanced")
        self.assertEqual(points["SPY_ALIGN_TURN_3M"], 15)
        self.assertEqual(points["SECTOR_ETF_PCT_TS"], 16)
        self.assertEqual(points["SPY_ALIGN_RALLY_DAY"], 19)
        self.assertNotIn("STACKS", points)
        self.assertEqual(points["PENALTY_SPREAD"], 0)
        self.assertEqual(points["PENALTY_OPT_VOL"], 0)
        self.assertIn("STACK_APLUS", fired)
        self.assertGreaterEqual(score, 15 + 16 + 19)
        _points, bypass, _pos, fired = score_volume_trend(
            features, "PUT", load_weights(), "bypass", "volume_trend_balanced")
        self.assertNotIn("STACKS", _points)
        self.assertNotIn("SPY_ALIGN_RALLY_DAY", _points)
        self.assertEqual(fired, [])
        self.assertLess(bypass, score)


class PenaltyTests(unittest.TestCase):
    def test_bands_halve_on_the_stock_bar_and_apply_once(self):
        settings = model_v_settings()
        strong = {"rvol": 2.0, "push": 2.0, "setups": 0}
        weak = {"rvol": 0.1, "push": 0.1, "setups": 0}
        self.assertEqual(penalty_spread_points(0.25), 0)
        self.assertEqual(penalty_spread_points(0.30), -10)
        self.assertEqual(penalty_spread_points(0.35), -10)
        self.assertEqual(penalty_spread_points(0.37), -12)
        self.assertEqual(penalty_spread_points(0.40), -12)
        self.assertEqual(penalty_spread_points(0.41), -15)
        self.assertEqual(penalty_spread_points(None), 0)
        self.assertEqual(penalty_spread_points(0.30, parts=strong, settings=settings), -5)
        self.assertEqual(penalty_spread_points(0.37, parts=strong, settings=settings), -6)
        self.assertEqual(penalty_spread_points(0.50, parts=strong, settings=settings), -7.5)
        self.assertEqual(penalty_spread_points(0.30, parts=weak, settings=settings), -10)
        self.assertEqual(penalty_opt_vol_points(49), -8)
        self.assertEqual(penalty_opt_vol_points(50), 0)
        self.assertEqual(penalty_opt_vol_points(None), 0)
        self.assertEqual(
            penalty_opt_vol_points(10, parts={"rvol": 1.0, "push": 1.0, "setups": 0},
                                   settings=settings, spread=0.10),
            0,
        )
        self.assertEqual(
            penalty_opt_vol_points(10, parts=weak, settings=settings, spread=0.10),
            -8,
        )
        features = {"entry_spread": 0.30, "opt_volume_5m": 10}
        points, score, _positive, _fired = score_volume_trend(
            features, "CALL", load_weights(), "primary", "volume_trend_balanced",
            parts=weak, settings=settings)
        self.assertEqual(points["PENALTY_SPREAD"], -10)
        self.assertEqual(points["PENALTY_OPT_VOL"], -8)
        book_points, book_score, _book_positive = score_path(
            features, "CALL", overlay_book(load_weights(), "volume_trend_balanced"))
        self.assertEqual(book_points.get("PENALTY_SPREAD", 0), 0)
        self.assertEqual(book_points.get("PENALTY_OPT_VOL", 0), 0)
        self.assertEqual(score, book_score - 10 - 8)

        at = datetime(2026, 6, 2, 10, 0, tzinfo=ET)
        prior_at = at - timedelta(minutes=6)
        bid, ask = 1.0, 2.3 / 1.7
        option = Option("C100", at.date(), "CALL", 100, bid, ask, 40, 10, 0.4, 0.02, at)
        prior_option = Option("C100", at.date(), "CALL", 100, bid, ask, 30, 10, 0.4, 0.02, prior_at)
        bars = tuple(_bar(i, 100.0, 1000) for i in range(12))
        snap = Snapshot("TEST", at, 100.0, at, 99.0, bars, (option,), prior_atr=1.0)
        prior = Snapshot("TEST", prior_at, 100.0, prior_at, 99.0, bars[:6], (prior_option,), prior_atr=1.0)
        candidate = Candidate(snap, option, 30, {}, {}, [], False)
        profile = model_v_settings(profile="volume_trend_balanced", post_threshold=100)
        extras = option_extras(candidate, profile, [prior])
        self.assertLess(extras["spread_penalty"], 0)
        apply_card_layers(candidate, profile, False, 30, [prior])
        posted = candidate.metrics["confluence_points"]
        self.assertEqual(posted["PENALTY_SPREAD"], -10)
        self.assertEqual(posted["PENALTY_OPT_VOL"], -8)
        self.assertEqual(candidate.score, candidate.metrics["confluence_score"])
        self.assertNotEqual(
            candidate.score,
            candidate.metrics["confluence_score"] + extras["spread_penalty"],
        )


class ExtraUniverseTests(unittest.TestCase):
    def test_cap_loads_first_twenty_net_new(self):
        import os
        from lotto.main import DEFAULT_UNIVERSE, universe_symbols

        previous = os.environ.pop("EXTRA_UNIVERSE_MAX", None)
        try:
            self.assertEqual(EXTRA_UNIVERSE_MAX, 20)
            self.assertEqual(DEEP_SLOTS, 60)
            self.assertEqual(PAID_MARKET_DATA_PER_MINUTE, 120)
            self.assertEqual(poll_cycle_calls(), 148)
            core = volume_trend_symbols(universe_symbols(DEFAULT_UNIVERSE))
            budget = call_budget(len(core))
            self.assertEqual(budget["configured_deep_slots"], 60)
            self.assertEqual(budget["extra_deep_slots"], 0)
            self.assertEqual(budget["extra_universe_max"], 20)
            self.assertGreater(budget["extra_names_budget"], 20)
            self.assertEqual(budget["extra_names"], 20)
            self.assertEqual(budget["calls_per_minute_40"], 148)
            loaded = select_extra_names(core, lambda _name: True, budget["extra_names"])
            self.assertEqual(loaded, [
                "OXY", "EOSE", "RDDT", "APP", "APLD", "DAL", "CVS", "GILD", "TXN", "WULF",
                "AAL", "ONDS", "WFC", "NVTS", "USAR", "MP", "SOUN", "CLF", "CCJ", "TWLO",
            ])
            self.assertGreater(len(EXTRA_UNIVERSE), 20)
            self.assertEqual(EXTRA_UNIVERSE[0], "MARA")
        finally:
            if previous is not None:
                os.environ["EXTRA_UNIVERSE_MAX"] = previous


class ParityTests(unittest.TestCase):
    def test_volume_bins_are_flags(self):
        blank = vol_bin_flags({})
        for key in (
            "RVOL_CUM_15_25", "RVOL_CUM_ge25", "BU_5_TOD_10",
            "BRK_TOD_BRK_08_12", "BRK_TOD_BRK_12_3", "BRK_TOD_BRK_ge3",
        ):
            self.assertEqual(blank[key], 0.0)
        mid = vol_bin_flags({"RVOL_CUM": 1.5, "BU_5_TOD_10": 1.6, "BRK_TOD_10": 0.8})
        self.assertEqual(mid["RVOL_CUM_15_25"], 1.0)
        self.assertEqual(mid["RVOL_CUM_ge25"], 0.0)
        self.assertEqual(mid["BU_5_TOD_10"], 1.0)
        self.assertEqual(mid["BRK_TOD_BRK_08_12"], 1.0)
        high = vol_bin_flags({"RVOL_CUM": 2.5, "BU_5_TOD_10": 1.59, "BRK_TOD_10": 1.2})
        self.assertEqual(high["RVOL_CUM_ge25"], 1.0)
        self.assertEqual(high["RVOL_CUM_15_25"], 0.0)
        self.assertEqual(high["BU_5_TOD_10"], 0.0)
        self.assertEqual(high["BRK_TOD_BRK_12_3"], 1.0)
        self.assertEqual(high["BRK_TOD_BRK_08_12"], 0.0)
        climax = vol_bin_flags({"BRK_TOD_10": 3.0})
        self.assertEqual(climax["BRK_TOD_BRK_ge3"], 1.0)
        self.assertEqual(climax["BRK_TOD_BRK_12_3"], 0.0)

    def test_typical_table_fires_on_a_two_times_move(self):
        curve, source = etf_typical_table([{30: 1.0} for _ in range(20)])
        self.assertEqual(source, "20-day")
        self.assertAlmostEqual(curve[30], 1.0)
        short, short_source = etf_typical_table([{30: 1.0}] * 4)
        self.assertEqual(short_source, "available-days")
        scaled, scaled_source = etf_typical_table([], 2.0)
        self.assertEqual(scaled_source, "atr-scaled")
        self.assertGreater(scaled[30], 0)

        class Snap:
            symbol = "AAPL"
            bars = []
            vt_pct_by_symbol = {}
            vt_peer_sets = {"XLK": [], "MTUM": []}
            vt_etf_typical_by_etf = {"XLK": curve[30], "MTUM": curve[30]}
            vt_etf_stats = {
                "XLK": {"pct": 2.0, "last": 102.0, "pmh": None, "pml": None},
                "MTUM": {"pct": 0.2, "last": 100.2, "pmh": None, "pml": None},
            }

        feats = sector_feature_choice(Snap(), 1, None)
        self.assertEqual(etf_typical_score(1, 2.0, curve[30]), 1.0)
        self.assertEqual(feats["SECTOR_ETF_PCT_TS"], 1.0)
        self.assertEqual(feats["_sector_etf"], "XLK")
        self.assertEqual(feats["_sector_etf_weight"], 1.0)

    def test_etf_typicals_reload_once_per_session_day(self):
        class Client:
            def __init__(self):
                self.baselines = {}
                self.calls = []

            def baseline(self, symbol, now):
                day = now.astimezone(ET).date()
                self.calls.append((symbol, day))
                self.baselines[(symbol, day)] = {}

        client = Client()
        monday = datetime(2026, 10, 26, 14, 0, tzinfo=ET)
        tuesday = datetime(2026, 10, 27, 14, 0, tzinfo=ET)
        ensure_etf_typicals(client, monday)
        ensure_etf_typicals(client, monday)
        count = len(sector_quote_symbols())
        self.assertEqual(len(client.calls), count)
        ensure_etf_typicals(client, tuesday)
        self.assertEqual(len(client.calls), count * 2)

    def test_sector_weight_picks_smh_soxx_xop_and_scales_the_low_group(self):
        for etf in ("XLI", "XLU", "XLB", "XLP", "XLRE"):
            self.assertEqual(etf_multiplier(etf), 0.5)
        for etf in ("XLK", "SMH", "SOXX", "XOP", "XLE"):
            self.assertEqual(etf_multiplier(etf), 1.0)
        smh = choose_sector_etf([
            ("XLK", {"SECTOR_PEER_COUNT": 0.0}),
            ("SMH", {"SECTOR_PEER_COUNT": 1.0}),
            ("SOXX", {"SECTOR_PEER_COUNT": 0.0}),
        ], primary="XLK")
        self.assertEqual(smh["etf"], "SMH")
        self.assertEqual(smh["total"], 33)
        tied = choose_sector_etf([
            ("XLK", {"SECTOR_PEER_COUNT": 1.0}),
            ("SMH", {"SECTOR_PEER_COUNT": 1.0}),
        ], primary="XLK")
        self.assertEqual(tied["etf"], "XLK")
        xop = choose_sector_etf([
            ("XLE", {"SECTOR_ETF_PCT_TS": 0.0}),
            ("XOP", {"SECTOR_ETF_PCT_TS": 1.0}),
        ], primary="XLE")
        self.assertEqual(xop["etf"], "XOP")
        soxx = choose_sector_etf([
            ("XLK", {"SECTOR_ETF_PCT_TS": 0.0}),
            ("SOXX", {"SECTOR_ETF_PCT_TS": 1.0}),
        ], primary="XLK")
        self.assertEqual(soxx["etf"], "SOXX")
        points, _score, _positive, _fired = score_volume_trend(
            {"SECTOR_ETF_PCT_TS": 1, "_sector_etf_weight": 0.5},
            "CALL", load_weights(), "primary", "volume_trend_balanced",
        )
        self.assertEqual(points["SECTOR_ETF_PCT_TS"], 8)

    def test_indexes_have_no_sector_etf(self):
        closes = [100.0] * 11
        closes[7] = 102.0
        closes[10] = 101.0 * 1.0008
        bars = [_bar(i, closes[i], 100) for i in range(11)]
        for symbol in ("QQQ", "SPY", "IWM"):
            self.assertIn(symbol, NO_SECTOR_ETF)
            self.assertIsNone(etf_for(symbol))
            self.assertEqual(candidate_etfs(symbol), ())
            feats = sector_feature_choice(type("S", (), {"symbol": symbol, "bars": []})(), 1, bars)
            self.assertEqual(feats["SECTOR_ETF_PCT_TS"], 0.0)
            self.assertEqual(feats["SECTOR_PEER_COUNT"], 0.0)
            self.assertEqual(feats["SPY_ALIGN_TURN_3M"], 1.0)

    def test_baskets_and_gics_map(self):
        by_name = {row["name"]: row for row in SECTOR_BASKETS}
        self.assertIn("GLW", by_name["OPTICAL"]["tickers"])
        self.assertNotIn("CIEN", by_name["OPTICAL"]["tickers"])
        self.assertIn("S", by_name["CYBER"]["tickers"])
        self.assertNotIn("FTNT", by_name["CYBER"]["tickers"])
        self.assertEqual(by_name["DC_AI_INFRA"]["tickers"][0], "NBIS")
        self.assertEqual(by_name["FINANCIALS_SLOW"]["tickers"], ())
        for key in (
            "BASKET_PCT_TS", "BASKET_SHARE_GREEN", "BASKET_SHARE_ABOVE_PMH",
            "BASKET_RANK", "BASKET_REL", "BASKET_LEADER_RANK",
        ):
            self.assertEqual(PRIMARY_WEIGHTS[key], 0)
        self.assertEqual({row["name"] for row in baskets_for("ANET")}, {"OPTICAL", "DC_AI_INFRA"})
        self.assertEqual(etf_for("UBER"), "XLI")
        self.assertEqual(etf_for("NBIS"), "XLK")
        self.assertEqual(etf_for("TGT"), "XLY")
        self.assertEqual(etf_for("RKLB"), "XLI")
        self.assertEqual(etf_for("BABA"), "XLY")
        self.assertEqual(etf_for("BMNR"), "XLK")
        self.assertEqual(etf_for("WULF"), "XLK")
        for name in EXTRA_UNIVERSE:
            self.assertIsNotNone(etf_for(name), name)


def _px(minute, open_, high, low, close):
    end = datetime(2026, 6, 2, 9, 31, tzinfo=ET) + timedelta(minutes=minute)
    return Bar(end, open_, high, low, close, 1000)


def _flat(count, high, low, close, open_=100.0):
    return [_px(i, open_ if i == 0 else close, high, low, close) for i in range(count)]


class DivergenceTests(unittest.TestCase):
    def test_flags_fire_and_mirror(self):
        below = _flat(10, 99.5, 98.5, 99.0)
        calls = divergence_sector(
            symbol="AAPL", stock_bars=below, etf_bars=below, direction=1,
            spy_pct=0.2, spy_above_vwap=1, card_time=below[-1].end,
        )
        self.assertEqual(calls["_div_etf"], "XLK")
        self.assertEqual(calls["DIV_ETF_WRONG_SIDE_10"], 1.0)
        short = divergence_sector(
            symbol="AAPL", stock_bars=below[:9], etf_bars=below[:9], direction=1,
            spy_pct=0.2, spy_above_vwap=1, card_time=below[8].end,
        )
        self.assertEqual(short["DIV_ETF_WRONG_SIDE_10"], 0.0)
        dark = divergence_sector(
            symbol="AAPL", stock_bars=below, etf_bars=below, direction=1,
            spy_pct=-0.2, spy_above_vwap=-1, card_time=below[-1].end,
        )
        self.assertEqual(dark["DIV_ETF_WRONG_SIDE_10"], 0.0)

        above = _flat(10, 101.5, 100.5, 101.0)
        puts = divergence_sector(
            symbol="AAPL", stock_bars=above, etf_bars=above, direction=-1,
            spy_pct=-0.2, spy_above_vwap=-1, card_time=above[-1].end,
        )
        self.assertEqual(puts["DIV_ETF_WRONG_SIDE_10"], 1.0)
        puts_off = divergence_sector(
            symbol="AAPL", stock_bars=above, etf_bars=above, direction=-1,
            spy_pct=0.2, spy_above_vwap=1, card_time=above[-1].end,
        )
        self.assertEqual(puts_off["DIV_ETF_WRONG_SIDE_10"], 0.0)

        stock = _flat(15, 100.0, 99.0, 100.0) + [_px(15, 100.0, 101.0, 99.5, 100.8)]
        etf = _flat(16, 100.0, 99.0, 100.0)
        orb = divergence_sector(
            symbol="NVDA", stock_bars=stock, etf_bars=etf, direction=1,
            spy_pct=0.1, spy_above_vwap=1, card_time=stock[-1].end,
        )
        self.assertEqual(orb["DIV_STOCK_ORB_ETF_NOT"], 1.0)
        both = divergence_sector(
            symbol="NVDA", stock_bars=stock, etf_bars=stock, direction=1,
            spy_pct=0.1, spy_above_vwap=1, card_time=stock[-1].end,
        )
        self.assertEqual(both["DIV_STOCK_ORB_ETF_NOT"], 0.0)
        put_stock = _flat(15, 101.0, 100.0, 100.0) + [_px(15, 100.0, 100.5, 99.0, 99.2)]
        put_etf = _flat(16, 101.0, 100.0, 100.0)
        put_orb = divergence_sector(
            symbol="NVDA", stock_bars=put_stock, etf_bars=put_etf, direction=-1,
            spy_pct=-0.1, spy_above_vwap=-1, card_time=put_stock[-1].end,
        )
        self.assertEqual(put_orb["DIV_STOCK_ORB_ETF_NOT"], 1.0)

        against = _flat(30, 101.0, 99.0, 100.0) + [_px(30, 100.0, 100.2, 98.0, 98.5)]
        broke = divergence_sector(
            symbol="AAPL", stock_bars=against, etf_bars=against, direction=1,
            spy_pct=0.2, spy_above_vwap=1, card_time=against[-1].end,
        )
        self.assertEqual(broke["DIV_ETF_OR30_AGAINST"], 1.0)
        held = divergence_sector(
            symbol="AAPL", stock_bars=against, etf_bars=against, direction=1,
            spy_pct=0.2, spy_above_vwap=1, card_time=against[29].end,
        )
        self.assertEqual(held["DIV_ETF_OR30_AGAINST"], 0.0)
        put_against = _flat(30, 101.0, 99.0, 100.0) + [_px(30, 100.0, 102.0, 99.8, 101.5)]
        put_broke = divergence_sector(
            symbol="AAPL", stock_bars=put_against, etf_bars=put_against, direction=-1,
            spy_pct=-0.2, spy_above_vwap=-1, card_time=put_against[-1].end,
        )
        self.assertEqual(put_broke["DIV_ETF_OR30_AGAINST"], 1.0)

    def test_no_sector_etf_is_false_and_weight_zero_keeps_the_score(self):
        bars = _flat(12, 99.5, 98.5, 99.0)
        for symbol in ("QQQ", "SPY", "IWM", "SPX"):
            flags = divergence_sector(
                symbol=symbol, stock_bars=bars, etf_bars=bars, direction=1,
                spy_pct=0.2, spy_above_vwap=1, card_time=bars[-1].end,
            )
            self.assertEqual(flags["_div_etf"], "")
            for name in DIVERGENCE_FLAGS:
                self.assertFalse(flags[name])
        book = load_weights()
        self.assertEqual(book["profiles"]["balanced"]["post_threshold"], 84)
        for name in DIVERGENCE_FLAGS:
            self.assertNotIn(name, book["weights"])
            self.assertEqual(PRIMARY_WEIGHTS[name], 0)
            self.assertEqual(PRIMARY_WEIGHTS[f"{name}_PUT"], 0)
        over = overlay_book(book, "volume_trend_balanced")
        self.assertEqual(over["weights"]["DIV_ETF_WRONG_SIDE_10"]["call"], 0)
        self.assertEqual(over["weights"]["DIV_ETF_WRONG_SIDE_10"]["put"], 0)
        self.assertNotIn("DIV_ETF_WRONG_SIDE_10_PUT", over["weights"])
        self.assertNotIn("DIV_ETF_WRONG_SIDE_10", book["weights"])
        self.assertEqual(
            divergence_weight_spec("DIV_ETF_WRONG_SIDE_10", {"DIV_ETF_WRONG_SIDE_10": 4}),
            {"call": 4.0, "put": 4.0},
        )
        self.assertEqual(
            divergence_weight_spec(
                "DIV_ETF_WRONG_SIDE_10",
                {"DIV_ETF_WRONG_SIDE_10": 4, "DIV_ETF_WRONG_SIDE_10_PUT": 1},
            ),
            {"call": 4.0, "put": 1.0},
        )
        self.assertIsNone(_divergence_tier_points("DIV_ETF_WRONG_SIDE_10", {"DIV_ETF_WRONG_SIDE_10": 1}))
        base = {"SPY_ALIGN_TURN_3M": 1.0, "SECTOR_ETF_PCT_TS": 1.0}
        quiet = score_volume_trend(base, "CALL", book, "primary", "volume_trend_balanced")
        loud = score_volume_trend(
            {**base, **{name: 1.0 for name in DIVERGENCE_FLAGS}},
            "CALL", book, "primary", "volume_trend_balanced",
        )
        self.assertEqual(quiet[1], loud[1])
        self.assertEqual(quiet[2], loud[2])
        for name in DIVERGENCE_FLAGS:
            self.assertEqual(loud[0][name], 0)


if __name__ == "__main__":
    unittest.main()
