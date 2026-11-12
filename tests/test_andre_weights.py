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
from lotto.andre_config import WEIGHTS
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

    def test_weight_table_caps_at_100_before_the_named_bonus(self):
        positive = (WEIGHTS["LEVEL_PM"] + WEIGHTS["LEVEL_PD"] + WEIGHTS["LEVEL_N3"] + WEIGHTS["LEVEL_N5"]
                    + WEIGHTS["SPY_TREND"] + WEIGHTS["SPY_LEVEL"] + WEIGHTS["QQQ_TREND"] + WEIGHTS["QQQ_LEVEL"]
                    + WEIGHTS["SECTOR_ETF"] + WEIGHTS["PEERS"] + WEIGHTS["LEADER"] + WEIGHTS["RVOL_3"]
                    + WEIGHTS["ENTRY"] + WEIGHTS["TIME_P1"])
        self.assertEqual(positive, 100)
        self.assertEqual(WEIGHTS["NAMED"], 5)
        self.assertEqual(WEIGHTS["PENALTY_CHASE"], 15)
        self.assertEqual(WEIGHTS["PENALTY_STRETCH"], 10)


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

    def test_time_windows_include_the_afternoon_and_are_configurable(self):
        self.assertEqual(time_points(20), 10)
        self.assertEqual(time_points(70), 6)
        self.assertEqual(time_points(120), 0)
        self.assertEqual(time_points(270), 6)
        with patch.dict(os.environ, {"LOTTO_ANDRE_W_TIME_P4": "9"}):
            self.assertEqual(time_points(270), 9)

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

    def test_level_break_room_and_each_named_path(self):
        # Fresh PMH cross with the next layer far away: the level break is allowed.
        closes = [99.5] * 10 + [100.4, 100.4]
        snap = _snap(_bars(closes), symbol="ZZZ", premarket_high=100, prior_high=110, prior_close=110)
        far = self._score(snap, rvol=2)
        self.assertIn("LEVEL_BREAK", far["metrics"]["andre_triggers"])
        self.assertNotIn("room to next layer", far["blockers"])

        # PDH sits 0.2 ATR overhead and RVOL is only 1.6: the break is held.
        tight = _snap(_bars(closes), symbol="ZZZ", premarket_high=100, prior_high=100.6, prior_atr=1, prior_close=110)
        held = self._score(tight, rvol=1.6)
        self.assertIn("room to next layer", held["blockers"])
        self.assertNotIn("LEVEL_BREAK", held["metrics"]["andre_triggers"].split(","))

        # Sector, SPY, and RVOL >= 3 clear the same tight break.
        TAPE.seed("SPY", price=101, open=100, at=tight.at)
        TAPE.seed("QQQ", price=101, open=100, at=tight.at)
        cleared = self._score(tight, rvol=3.2)
        # ZZZ has no sector ETF, so sector_working is false and the hold remains.
        self.assertIn("room to next layer", cleared["blockers"])
        mapped = _snap(_bars(closes), symbol="MU", premarket_high=100, prior_high=100.6, prior_atr=1, prior_close=110)
        TAPE.seed("SMH", price=110, open=100, vwap=105, at=mapped.at)
        released = self._score(mapped, rvol=3.2)
        self.assertNotIn("room to next layer", released["blockers"])
        self.assertIn("LEVEL_BREAK", released["metrics"]["andre_triggers"])

    def test_opening_range_break_is_inside_the_first_hour(self):
        closes = [100, 100, 100, 100, 100, 100, 101, 101]
        snap = _snap(_bars(closes), symbol="ZZZ", prior_close=90, premarket_high=120, prior_atr=5)
        result = self._score(snap, rvol=2)
        self.assertIn("OR5", result["metrics"]["andre_triggers"])

    def test_n_day_break_adds_the_range_points(self):
        closes = [99] * 10 + [101, 101]
        snap = _snap(_bars(closes), symbol="ZZZ", premarket_high=90, prior_high=90, prior_close=98, prior_atr=5)
        remember_levels("ZZZ", snap.at.astimezone(ET).date(), [(100, 90)] * 5)
        result = self._score(snap, rvol=2)
        self.assertIn("NDAY_BREAK", result["metrics"]["andre_triggers"])
        self.assertEqual(result["parts"]["level_n3"], WEIGHTS["LEVEL_N3"])
        self.assertEqual(result["parts"]["level_n5"], WEIGHTS["LEVEL_N5"])

    def test_s29_split_flush_and_fade(self):
        # S29-B does not fire on a bare level cross with no earlier reclaim.
        lone = _snap(_bars([99] * 10 + [101, 101]), symbol="ZZZ", premarket_high=100, prior_high=130, prior_atr=5)
        self.assertNotIn("S29B", self._score(lone, rvol=2.5)["metrics"]["andre_triggers"])

        # Inside-range VWAP cross after a flush: exempt from the outside-range gate.
        rows = [(220, 20)] * 2 + [(88, 9000)] * 6 + [(70, 100)] * 10
        rows += [(70 + (index + 1) * 1.3, 80) for index in range(30)]
        early = _snap(_varied(rows[:44]), symbol="ZZZ", premarket_high=300, prior_high=300, prior_atr=10, prior_close=100)
        reclaim = self._score(early, rvol=1.3)
        self.assertIn("S29A", reclaim["metrics"]["andre_triggers"])
        self.assertNotIn("outside the range", reclaim["blockers"])
        self.assertNotIn("S29B", reclaim["metrics"]["andre_triggers"])

        # Later PMH cross on a name that already reclaimed VWAP. RVOL >= 2.
        follow_closes = [100] * 8 + [94] * 6 + [100] * 6 + [101] * 10 + [103, 103]
        follow = _snap(_bars(follow_closes), symbol="ZZZ", premarket_high=102, prior_high=140, prior_atr=5, prior_close=99)
        self.assertIn("S29B", self._score(follow, rvol=2.5)["metrics"]["andre_triggers"])

        flushed = _snap(_bars([100] * 6 + [90] * 6 + [96] * 2), symbol="ZZZ", premarket_low=90, prior_atr=8, prior_close=100)
        self.assertIn("FLUSH_REVERSAL", self._score(flushed, rvol=2)["metrics"]["andre_triggers"])

        # Opening sell-off, then a VWAP rejection between 10:00 and 11:00.
        fade_closes = [100 - index * 0.3 for index in range(16)] + [95 + index * 0.4 for index in range(12)] + [98, 98, 94, 94]
        faded = _snap(_bars(fade_closes), symbol="ZZZ", premarket_low=93, prior_atr=8, prior_close=101)
        self.assertIn("FADE_REJECT", self._score(faded, direction=-1, rvol=2)["metrics"]["andre_triggers"])

    def test_trend_pullback_requires_the_drive(self):
        rise = [100 + index * 0.15 for index in range(36)]
        bars = list(_bars(rise))
        # Last bar tags back toward the EMA and closes up with the trend.
        last = bars[-1]
        bars[-1] = Bar(last.end, last.open, last.high, 103.5, last.close, last.volume)
        snap = _snap(tuple(bars), symbol="ZZZ", premarket_high=100.2, prior_high=104, prior_atr=2, prior_close=99)
        result = self._score(snap, rvol=2)
        self.assertIn("TREND_PULLBACK", result["metrics"]["andre_triggers"])
        inside = _snap(tuple(bars), symbol="ZZZ", premarket_high=200, prior_high=200, prior_atr=2)
        self.assertNotIn("TREND_PULLBACK", self._score(inside, rvol=2)["metrics"]["andre_triggers"])

    def test_s31_leader_breaks_while_the_etf_is_weak(self):
        closes = [99] * 10 + [101, 101]
        snap = _snap(_bars(closes), symbol="MU", premarket_high=100, prior_high=130, prior_atr=5, prior_close=98)
        TAPE.seed("SMH", price=50, open=80, high=80, low=49, vwap=70, at=snap.at)
        for peer in peers_for("MU"):
            TAPE.seed(peer, price=100, open=100, at=snap.at)
        result = self._score(snap, rvol=2)
        self.assertIn("S31", result["metrics"]["andre_triggers"])

    def test_mid_range_burst_scores_nothing_and_an_aligned_burst_does_not_wait(self):
        bars = _bars([100, 100.1], volume=5_000)
        quiet = _snap(bars, symbol="RVOL", premarket_high=200, prior_atr=5)
        day = quiet.at.astimezone(ET).date()
        remember_rvol("RVOL", day, {}, {0: (100.0, 20), 1: (100.0, 20)})
        quiet_result = evaluate_side_full(quiet, 1)
        self.assertEqual(quiet_result["metrics"]["andre_burst_aligned"], 0)
        self.assertEqual(quiet_result["parts"]["rvol"], 0.0)
        aligned = _snap(bars, symbol="RVOL", premarket_high=100, prior_high=140, prior_atr=5, prior_close=99)
        hit = evaluate_side_full(aligned, 1)
        self.assertEqual(hit["metrics"]["andre_burst_aligned"], 1)
        self.assertEqual(hit["parts"]["rvol"], WEIGHTS["RVOL_15"])

    def test_exhausted_approach_holds_unless_catalyst_or_pull_in(self):
        climb = [98 + index * (1.9 / 20) for index in range(22)] + [100.2, 100.2]
        snap = _snap(_bars(climb), symbol="ZZZ", premarket_high=100, prior_high=130, prior_atr=1, prior_close=98)
        TAPE.seed("SPY", price=102.0, open=100, atr=1, at=snap.at, samples=deque([
            (snap.at - timedelta(minutes=16), 101.4), (snap.at, 102.0)]))
        held = self._score(snap, rvol=2)
        self.assertIn("exhausted approach", held["blockers"])
        self.assertEqual(held["parts"]["chase"], -WEIGHTS["PENALTY_CHASE"])
        # Own catalyst: the stock's ATR move from the open outruns SPY.
        TAPE.seed("SPY", price=100.2, open=100, atr=1, at=snap.at, samples=deque([
            (snap.at - timedelta(minutes=16), 99.5), (snap.at, 100.2)]))
        exempt = self._score(snap, rvol=2)
        self.assertNotIn("exhausted approach", exempt["blockers"])
        self.assertEqual(exempt["metrics"]["andre_catalyst"], 1)

    def test_after_eleven_blocks_a_red_reclaim_and_the_afternoon_is_not_zero(self):
        start = datetime(2026, 11, 11, 13, 40, tzinfo=ET)
        closes = [110] + [99] * 10 + [100.2, 100.2]
        snap = _snap(_bars(closes, start=start), symbol="ZZZ", premarket_high=120, prior_high=100,
                     prior_atr=5, prior_close=110)
        result = self._score(snap, rvol=2)
        self.assertIn("in-range after 11:00", result["blockers"])
        self.assertEqual(time_points(250), WEIGHTS["TIME_P4"])

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
