"""Volume-trend profiles, Vince V2 sector/SPY rows, Ace volume rows, and stacks."""
from datetime import date, datetime, timedelta
import unittest

from lotto.models import ET, Bar, Option
from lotto.volume_trend import (
    PRIMARY_WEIGHTS, PROFILES, VOL_ALIASES, VOLUME_TREND_RULES,
    alignment_features, evaluate_stacks, is_volume_trend, overlay_book,
    rally_breadth, rally_points, reference_close, score_volume_trend,
    sector_map_names, spy_turn, spy_window_return, stacks_from_rules,
    volume_trend_lines, volume_trend_symbols,
)
from lotto.vt_measure import (
    adjacent_strike_count, band_score, close_loc_score, hold_score,
    measure_volume, option_rows, range_atr_score, room_score, rvol_cum_chain,
    sust_score, sustained_share, volume_steady,
)
from lotto.weights import active_profile, load_weights


def _bar(minute, close, volume, span=0.4):
    end = datetime(2026, 6, 2, 9, 31, tzinfo=ET) + timedelta(minutes=minute)
    return Bar(end, close, close + span / 2, close - span / 2, close, int(volume))


def _flat_tod(level=100.0):
    curve = {minute: level for minute in range(390)}
    return {10: curve, 5: curve, 3: curve, "any": curve}


class ProfileTests(unittest.TestCase):
    def test_lines_and_live_book_stay_put(self):
        self.assertEqual(volume_trend_lines("volume_trend")["post_threshold"], 108)
        self.assertEqual(volume_trend_lines("volume_trend_aggressive")["post_threshold"], 96)
        self.assertEqual(volume_trend_lines("volume_trend_conservative")["post_threshold"], 124)
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
        self.assertEqual(over["weights"]["SECTOR_PEER_COUNT"]["call"], 0)
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
        self.assertEqual(rally_points(rally_breadth(1, table(0.80, 1))), 41)
        self.assertEqual(rally_points(rally_breadth(1, table(0.95, 1))), 0)
        self.assertEqual(rally_points(rally_breadth(1, table(0.50, 1))), 0)
        self.assertEqual(rally_points(rally_breadth(-1, table(0.70, -1))), 18)
        self.assertEqual(rally_points(rally_breadth(-1, table(0.80, -1))), 41)
        self.assertEqual(rally_points(rally_breadth(-1, table(0.95, -1))), 0)
        self.assertEqual(rally_points(rally_breadth(1, table(0.70, -1))), 0)
        self.assertEqual(rally_points(0.75), 41)
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
        for alias, primary in VOL_ALIASES.items():
            self.assertIn(primary, PRIMARY_WEIGHTS)
            self.assertEqual(PRIMARY_WEIGHTS[alias], 0)
            self.assertIn(primary, PRIMARY_WEIGHTS)


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


if __name__ == "__main__":
    unittest.main()
