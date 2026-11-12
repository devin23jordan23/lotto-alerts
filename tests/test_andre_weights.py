"""Mode 1 is off unless LOTTO_ANDRE_WEIGHTS_MODE=1. Flag 0 keeps today's score."""
import os
import unittest
from collections import deque
from datetime import datetime, timedelta
from unittest.mock import patch

from lotto.andre import (
    TAPE, aggregate, apply_mode, blended_ema, burst_multiple, ema_weights,
    evaluate_side_full, remember_levels, remember_rvol, reset_state, rvol_now, time_points,
)
from lotto.andre_config import WEIGHTS, threshold
from lotto.config import Settings
from lotto.demo import demo_frames
from lotto.engine import Engine
from lotto.features import Candidate, candidates
from lotto.groups import peers_for, primary_etf
from lotto.models import Bar, ET
from lotto.patterns import Setup
from lotto.store import Store


def _varied(rows, start=None):
    start = start or datetime(2026, 11, 11, 9, 31, tzinfo=ET)
    built, prev = [], rows[0][0]
    for index, (close, volume) in enumerate(rows):
        high, low = max(prev, close) + 0.02, min(prev, close) - 0.02
        built.append(Bar(start + timedelta(minutes=index), prev, high, low, close, int(volume)))
        prev = close
    return tuple(built)


def _bars(closes, start=None, volume=1_000):
    start = start or datetime(2026, 11, 11, 9, 31, tzinfo=ET)
    built, prev = [], closes[0]
    for index, close in enumerate(closes):
        high, low = max(prev, close) + 0.02, min(prev, close) - 0.02
        built.append(Bar(start + timedelta(minutes=index), prev, high, low, close, volume))
        prev = close
    return tuple(built)


def _snap(bars, **fields):
    from dataclasses import replace
    base = list(demo_frames())[20][0]
    end = bars[-1].end
    return replace(base, at=end, spot=bars[-1].close, spot_time=end, bars=bars,
                   prior_atr=fields.pop("prior_atr", 2), **fields)


class FlagOffTests(unittest.TestCase):
    def test_demo_score_is_unchanged(self):
        frames = list(demo_frames())
        snap, history = frames[15][0], [frame[0] for frame in frames[:15]]
        found = candidates(snap, history, Settings())
        self.assertTrue(found)
        card = found[0]
        self.assertEqual(card.score, 91.79)
        self.assertAlmostEqual(card.parts["price_structure"], 25.0)
        self.assertAlmostEqual(card.parts["option_velocity"], 15.0)
        self.assertAlmostEqual(card.parts["sector_context"], 10.0)
        self.assertAlmostEqual(card.parts["volume_persistence"], 19.29303870692332)
        self.assertAlmostEqual(card.metrics["ema9"], 102.42814749767106)
        self.assertNotIn("andre_triggers", card.metrics)
        with patch.dict(os.environ, {"LOTTO_ANDRE_WEIGHTS_MODE": "1"}):
            rescored = candidates(snap, history, Settings())
        self.assertNotEqual(rescored[0].score, 91.79)
        self.assertIn("rvol", rescored[0].parts)
        self.assertEqual(rescored[0].parts["option_velocity"], 0.0)
        self.assertEqual(rescored[0].parts["strike_breadth"], 0.0)
        self.assertEqual(rescored[0].parts["liquidity"], 0.0)
        self.assertEqual(rescored[0].parts["contract_response"], 0.0)
        again = candidates(snap, history, Settings())
        self.assertEqual(again[0].score, 91.79)

    def test_verified_weights_are_additive_and_the_cut_is_six(self):
        # The spec's 38 counts both morning buckets. They do not co-fire; the
        # highest single card is TIME_P1 plus the other seven terms.
        listed = (WEIGHTS["TIME_P1"] + WEIGHTS["TIME_P2"] + WEIGHTS["SPY_EFF"] + WEIGHTS["SPY_D3"]
                  + WEIGHTS["PACE_2"] + WEIGHTS["RVOL_3"] + WEIGHTS["BURST_PD"] + WEIGHTS["GAP_1ATR"]
                  + WEIGHTS["EARN_D1"])
        self.assertEqual(listed, 38)
        self.assertEqual(listed - WEIGHTS["TIME_P2"], 33)
        self.assertEqual(threshold("MIN_SCORE"), 6)
        self.assertEqual(threshold("TOP_SCORE"), 8)
        self.assertEqual(WEIGHTS["TIME_P4"], -2)
        self.assertEqual(WEIGHTS["LEVEL_PM"], 0)
        self.assertEqual(WEIGHTS["SPY_LEVEL"], 0)
        self.assertEqual(WEIGHTS["SECTOR_ETF"], 0)
        with patch.dict(os.environ, {"LOTTO_ANDRE_MIN_SCORE": "0"}):
            self.assertEqual(threshold("MIN_SCORE"), 0)


class StructureTests(unittest.TestCase):
    def test_aggregation_drops_a_partial_bucket(self):
        bars = _bars([10, 11, 12, 13, 14, 15, 16])
        two = aggregate(bars, 2)
        self.assertEqual(len(two), 3)
        self.assertEqual((two[0].open, two[0].close, two[0].high, two[0].low), (10, 11, 11.02, 9.98))
        self.assertEqual(two[0].volume, 2_000)
        self.assertEqual(len(aggregate(bars, 5)), 1)
        self.assertEqual(aggregate(bars, 5)[0].close, 14)
        self.assertEqual(aggregate(bars, 10), [])

    def test_ema_weights_follow_the_session_clock(self):
        self.assertEqual(ema_weights(20), (1.0, 0.0, 0.0))
        self.assertEqual(ema_weights(50), (0.5, 0.5, 0.0))
        self.assertEqual(tuple(round(value, 2) for value in ema_weights(75)), (0.25, 0.5, 0.25))
        self.assertEqual(ema_weights(120), (0.0, 0.4, 0.6))
        blended = blended_ema(_bars([10 + index * 0.1 for index in range(40)]), 20)
        self.assertAlmostEqual(blended["ema9"], blended["ema9_2"])
        self.assertGreater(blended["weights"][0], 0)

    def test_time_windows_follow_the_verified_buckets(self):
        self.assertEqual(time_points(20), 8)     # 09:50
        self.assertEqual(time_points(37), 5)     # 10:07
        self.assertEqual(time_points(120), 0)    # 11:30
        self.assertEqual(time_points(240), -2)   # 13:30, not a bonus
        self.assertEqual(time_points(400), -2)
        with patch.dict(os.environ, {"LOTTO_ANDRE_W_TIME_P4": "0"}):
            self.assertEqual(time_points(240), 0)

    def test_rvol_uses_twenty_sessions_then_fourteen_and_is_unknown_below_that(self):
        day = datetime(2026, 11, 11, tzinfo=ET).date()
        remember_rvol("RVOL", day, {30: (100.0, 20)}, {28: (40.0, 20), 29: (40.0, 20)})
        self.assertEqual(rvol_now("RVOL", day, 30, 250)["rvol"], 2.5)
        remember_rvol("RVOL", day, {30: (100.0, 14)})
        self.assertEqual(rvol_now("RVOL", day, 30, 160)["status"], "ok")
        remember_rvol("RVOL", day, {30: (100.0, 13)}, {0: (20.0, 20), 1: (20.0, 20)})
        self.assertEqual(rvol_now("RVOL", day, 30, 500)["status"], "unknown")
        bars = _bars([10, 11], volume=300)
        self.assertGreaterEqual(burst_multiple("RVOL", day, aggregate(bars, 2)), 3)

    def test_sector_map_and_peer_score(self):
        self.assertEqual(primary_etf("MU"), "SMH")
        self.assertEqual(peers_for("MU"), ("SNDK", "WDC", "STX"))
        self.assertNotIn("MU", peers_for("MU"))
        self.assertEqual(primary_etf("NOPE"), None)
        bars = _bars([100 + index * 0.2 for index in range(16)])
        snap = _snap(bars, symbol="MU", premarket_high=90, prior_high=120)
        at = snap.at
        TAPE.seed("SMH", price=110, open=100, vwap=105, high=112, low=99, at=at)
        for peer, price in (("SNDK", 101), ("WDC", 102), ("STX", 99)):
            TAPE.seed(peer, price=price, open=100, at=at)
        result = evaluate_side_full(snap, 1)
        self.assertEqual(result["parts"]["sector_etf"], WEIGHTS["SECTOR_ETF"])
        self.assertEqual(result["parts"]["peers"], WEIGHTS["PEERS"])
        self.assertEqual(result["parts"]["leader"], 0.0)

    def tearDown(self):
        reset_state()


class TriggerTests(unittest.TestCase):
    def tearDown(self):
        reset_state()

    def _score(self, snap, direction=1, rvol=2.0, sessions=20):
        volume = sum(bar.volume for bar in snap.bars)
        remember_rvol(snap.symbol, snap.at.astimezone(ET).date(),
                      {len(snap.bars): (volume / rvol, sessions)})
        return evaluate_side_full(snap, direction)

    def test_score_matches_the_verified_table(self):
        # 10:07 is the +5 bucket. A prior-day burst adds +1. Nothing else verified.
        # Own-catalyst must not fill in SPY points.
        start = datetime(2026, 11, 11, 9, 31, tzinfo=ET)
        closes = [100] * 28 + [101.2, 101.2]
        snap = _snap(_bars(closes, start=start), symbol="MSFT", premarket_high=90, prior_high=101,
                     prior_atr=5, prior_close=100.2)
        TAPE.seed("SPY", price=101, open=100, pdh=90, at=snap.at)
        remember_rvol("MSFT", snap.at.astimezone(ET).date(), {}, {minute: (200.0, 20) for minute in range(40)})
        result = self._score(snap, rvol=1.2)
        self.assertEqual(result["parts"]["time_window"], 5)
        self.assertEqual(result["parts"]["burst_pd"], 1)
        self.assertEqual(result["parts"]["spy_eff"], 0)
        self.assertEqual(result["parts"]["spy_d3"], 0)
        self.assertEqual(result["parts"]["pace"], 0)
        self.assertEqual(result["parts"]["spy_trend"], 0)
        self.assertEqual(result["score"], 6)
        self.assertEqual(threshold("MIN_SCORE"), 6)

        afternoon = datetime(2026, 11, 11, 13, 30, tzinfo=ET)
        late = _snap(_bars([100, 100.2], start=afternoon), symbol="ZZZ", premarket_high=90,
                     prior_atr=5, prior_close=100)
        late_score = self._score(late, rvol=1.0)
        self.assertEqual(late_score["parts"]["time_window"], -2)

        watched = _snap(_bars([99.5] * 10 + [100.4, 100.4]), symbol="MU",
                        premarket_high=100, prior_high=110, prior_atr=5, prior_close=100)
        TAPE.seed("SMH", price=110, open=100, vwap=105, at=watched.at)
        TAPE.seed("SPY", price=110, open=100, pdh=90, at=watched.at)
        watched_score = self._score(watched, rvol=1.1)
        self.assertEqual(watched_score["parts"]["spy_level"], 0)
        self.assertEqual(watched_score["parts"]["sector_etf"], 0)
        self.assertEqual(watched_score["parts"]["level_pm"], 0)
        self.assertEqual(watched_score["parts"]["level_pd"], 0)
        self.assertIn("LEVEL_BREAK", watched_score["metrics"]["andre_watch"])
        self.assertEqual(watched_score["metrics"]["andre_fast_path"], 0)

    def test_only_the_open_flush_and_a_deep_trend_reclaim_can_fire(self):
        # Tight flush into PDL, reclaim inside three 2-minute bars, depth >= 5, 09:32-10:00.
        closes = [100, 100, 100, 100, 97.8, 97.8, 99, 99, 102, 102]
        snap = _snap(_bars(closes), symbol="ZZZ", premarket_high=90, prior_low=98, prior_high=101,
                     prior_atr=2, prior_close=100.4)
        TAPE.seed("SPY", price=100, open=100, at=snap.at)
        fired = self._score(snap, rvol=2)
        self.assertIn("FLUSH_V", fired["metrics"]["andre_triggers"])
        self.assertEqual(fired["metrics"]["andre_fast_path"], 1)
        self.assertGreaterEqual(fired["score"], 6)

        reset_state()
        late_start = datetime(2026, 11, 11, 11, 0, tzinfo=ET)
        midday = _snap(_bars(closes, start=late_start), symbol="ZZZ", premarket_high=90, prior_low=98,
                       prior_high=101, prior_atr=2, prior_close=100.4)
        TAPE.seed("SPY", price=100, open=100, at=midday.at)
        quiet = self._score(midday, rvol=2)
        self.assertNotIn("FLUSH_V", quiet["metrics"]["andre_triggers"])
        self.assertEqual(quiet["metrics"]["andre_fast_path"], 0)

    def test_watchlist_setups_do_not_fast_path_and_dropped_setups_do_not_fire(self):
        level = _snap(_bars([99.5] * 10 + [100.4, 100.4]), symbol="ZZZ", premarket_high=100,
                      prior_high=110, prior_atr=5, prior_close=110)
        level_result = self._score(level, rvol=2)
        self.assertIn("LEVEL_BREAK", level_result["metrics"]["andre_watch"])
        self.assertNotIn("LEVEL_BREAK", level_result["metrics"]["andre_triggers"])
        self.assertEqual(level_result["metrics"]["andre_fast_path"], 0)

        opening = _snap(_bars([100] * 6 + [101, 101]), symbol="ZZZ", prior_close=90,
                        premarket_high=120, prior_atr=5)
        opening_result = self._score(opening, rvol=2)
        self.assertIn("OR5", opening_result["metrics"]["andre_watch"])
        self.assertEqual(opening_result["metrics"]["andre_fast_path"], 0)

        nday = _snap(_bars([99] * 10 + [101, 101]), symbol="ZZZ", premarket_high=90, prior_high=90,
                     prior_close=98, prior_atr=5)
        from lotto.andre import remember_levels
        remember_levels("ZZZ", nday.at.astimezone(ET).date(), [(100, 90)] * 5)
        nday_result = self._score(nday, rvol=2)
        self.assertNotIn("NDAY_BREAK", nday_result["metrics"]["andre_triggers"])
        self.assertNotIn("NDAY_BREAK", nday_result["metrics"]["andre_watch"])
        self.assertEqual(nday_result["metrics"]["andre_fast_path"], 0)

        follow = _snap(_bars([100] * 8 + [94] * 6 + [100] * 6 + [101] * 10 + [103, 103]),
                       symbol="ZZZ", premarket_high=102, prior_high=140, prior_atr=5, prior_close=99)
        follow_result = self._score(follow, rvol=2.5)
        self.assertIn("S29B", follow_result["metrics"]["andre_watch"])
        self.assertEqual(follow_result["metrics"]["andre_fast_path"], 0)

        leader = _snap(_bars([99] * 10 + [101, 101]), symbol="MU", premarket_high=100, prior_high=130,
                       prior_atr=5, prior_close=98)
        TAPE.seed("SMH", price=50, open=80, high=80, low=49, vwap=70, at=leader.at)
        for peer in peers_for("MU"):
            TAPE.seed(peer, price=100, open=100, at=leader.at)
        leader_result = self._score(leader, rvol=2)
        self.assertIn("S31", leader_result["metrics"]["andre_watch"])
        self.assertEqual(leader_result["metrics"]["andre_fast_path"], 0)

        early = _snap(_bars([100] * 12 + [101, 101]), symbol="ZZZ", premarket_high=300, prior_atr=5, prior_close=100)
        early_result = self._score(early, rvol=2)
        self.assertNotIn("S29A", early_result["metrics"]["andre_triggers"])
        self.assertEqual(early_result["metrics"]["andre_fast_path"], 0)

        fade_closes = [100 - index * 0.3 for index in range(16)] + [95 + index * 0.4 for index in range(12)] + [98, 98, 94, 94]
        faded = _snap(_bars(fade_closes), symbol="ZZZ", premarket_low=93, prior_atr=8, prior_close=101)
        faded_result = self._score(faded, direction=-1, rvol=2)
        self.assertNotIn("FADE_REJECT", faded_result["metrics"]["andre_triggers"])
        self.assertNotIn("FADE_LEVEL", faded_result["metrics"]["andre_triggers"])
        self.assertEqual(faded_result["metrics"]["andre_fast_path"], 0)

    def test_old_holds_and_the_catalyst_fill_are_gone(self):
        climb = [98 + index * (1.9 / 20) for index in range(22)] + [100.2, 100.2]
        snap = _snap(_bars(climb), symbol="ZZZ", premarket_high=100, prior_high=130, prior_atr=1, prior_close=98)
        TAPE.seed("SPY", price=102.0, open=100, atr=1, pdh=90, at=snap.at, samples=deque([
            (snap.at - timedelta(minutes=16), 101.4), (snap.at, 102.0)]))
        held = self._score(snap, rvol=2)
        self.assertNotIn("exhausted approach", held["blockers"])
        self.assertNotIn("room to next layer", held["blockers"])
        self.assertEqual(held["parts"]["chase"], 0)
        self.assertEqual(held["parts"]["spy_eff"], 0)
        self.assertEqual(held["parts"]["spy_d3"], 0)

        tight = _snap(_bars([99.5] * 10 + [100.4, 100.4]), symbol="MU", premarket_high=100,
                      prior_high=100.6, prior_atr=1, prior_close=110)
        TAPE.seed("SMH", price=110, open=100, vwap=105, at=tight.at)
        TAPE.seed("SPY", price=101, open=100, at=tight.at)
        self.assertNotIn("room to next layer", self._score(tight, rvol=1.6)["blockers"])

    def test_after_eleven_inside_range_is_blocked_and_costs_four(self):
        start = datetime(2026, 11, 11, 11, 20, tzinfo=ET)
        snap = _snap(_bars([100] * 8, start=start), symbol="ZZZ", premarket_high=120, prior_high=120,
                     prior_atr=5, prior_close=100)
        result = self._score(snap, rvol=1)
        self.assertEqual(result["parts"]["inside_after_11"], -4)
        self.assertEqual(result["metrics"]["andre_fast_path"], 0)
        self.assertIn("no andre setup", result["blockers"])

    def test_midday_in_range_ema_touch_does_not_fire(self):
        start = datetime(2026, 11, 11, 11, 40, tzinfo=ET)
        closes = [100 + index * 0.02 for index in range(20)]
        snap = _snap(_bars(closes, start=start), symbol="ZZZ", premarket_high=130, prior_high=140, prior_atr=5)
        result = self._score(snap, rvol=0.8)
        self.assertNotIn("TREND_PULLBACK", result["metrics"]["andre_triggers"])
        self.assertEqual(result["metrics"]["andre_fast_path"], 0)

    def test_fast_path_does_not_wait_for_a_second_bar(self):
        frames = list(demo_frames())
        snap = frames[12][0]
        store = Store(":memory:")
        self.addCleanup(store.db.close)

        def qualify(current, history, settings, fast):
            option = current.options[2]
            setup = Setup("LEVEL_BREAK", current.spot, current.spot - 1)
            metrics = {"andre_fast_path": fast}
            return [Candidate(current, option, 80, {"rvol": 10}, metrics, [], True, setup=setup)]

        with patch("lotto.engine.candidates", side_effect=lambda *args: qualify(*args, 0)):
            self.assertFalse(Engine(store).process([snap]))
        store2 = Store(":memory:")
        self.addCleanup(store2.db.close)
        with patch("lotto.engine.candidates", side_effect=lambda *args: qualify(*args, 1)):
            self.assertEqual(len(Engine(store2).process([snap])), 1)


class ApplyModeTests(unittest.TestCase):
    def test_apply_mode_keeps_dead_market_blockers(self):
        frames = list(demo_frames())
        card = candidates(frames[15][0], [frame[0] for frame in frames[:15]], Settings())[0]
        card.blockers = ("dead zone: flat 30m hugging VWAP (11:00-14:30)",)
        with patch.dict(os.environ, {"LOTTO_ANDRE_WEIGHTS_MODE": "1"}):
            apply_mode([card])
        self.assertIn("dead zone: flat 30m hugging VWAP (11:00-14:30)", card.blockers)


if __name__ == "__main__":
    unittest.main()
