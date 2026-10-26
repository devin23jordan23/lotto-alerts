from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from lotto.config import Settings
from lotto.demo import demo_frames
from lotto.engine import Engine
from lotto.discord import mover_send_quote_ok, deliver_pending
from lotto.features import Candidate
from lotto.models import Bar, ET, Option
from lotto.mover import keep_reason, mover_candidates, pick_contract
from lotto.patterns import Setup
from lotto.store import Store


def bars_from(prices, start=datetime(2026, 10, 23, 9, 30, tzinfo=ET)):
    return tuple(Bar(start + timedelta(minutes=i + 1), prices[max(0, i - 1)],
                     max(prices[max(0, i - 1)], p) + .01, min(prices[max(0, i - 1)], p) - .01, p, 1000, (i + 1) * 600)
                 for i, p in enumerate(prices))


class MoverTests(unittest.TestCase):
    def setUp(self):
        self.base = list(demo_frames())[-1][0]
        self.store = Store(":memory:")
        self.addCleanup(self.store.db.close)

    def snap(self, symbol, prices, atr, options=()):
        bars = bars_from(prices)
        at = bars[-1].end + timedelta(seconds=20)
        return replace(self.base, symbol=symbol, at=at, spot_time=at, spot=prices[-1], bars=bars,
                       prior_close=prices[0], prior_atr=atr, options=tuple(options), session_end=None)

    def option(self, at, strike, bid, ask, side="CALL", delta=.30):
        return Option(f"X 261023{side[0]}{strike}", at.astimezone(ET).date(), side, strike, bid, ask, 50, 10,
                      delta if side == "CALL" else -delta, .02, at)

    def _mover_setup(self):
        settings = replace(Settings(), mover_enabled=1)
        engine = Engine(self.store, settings)
        prices = [100 + .02 * i for i in range(30)] + [100.6 + .12 * i for i in range(20)]  # quiet, then +2.3 in 20 min
        spy = self.snap("SPY", [500 + .05 * i for i in range(50)], 6)
        engine.spy_bars[spy.day] = spy.bars
        return settings, engine, prices, self.snap("PANW", prices, 5)

    def test_trending_stock_alerts_with_a_tradable_thin_contract(self):
        settings, engine, prices, snap = self._mover_setup()
        thin = self.option(snap.at, 103, .80, 1.10)  # 27% spread, volume 50: fails the legacy contract rules
        snap = replace(snap, options=(thin,))
        out = mover_candidates(engine, snap, settings)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].option.symbol, thin.symbol)
        self.assertEqual(out[0].setup.name, "MOVER")
        engine.mover_sent[(snap.day, snap.symbol, 1)] = True  # set only after the engine queues it
        # same move one minute later: no second alert (one alert per move, not a cooldown)
        later = replace(snap, at=snap.at + timedelta(minutes=1))
        later = replace(later, bars=later.bars + (Bar(snap.bars[-1].end + timedelta(minutes=1), prices[-1], prices[-1] + .2,
                                                      prices[-1], prices[-1] + .15, 1000, 31000),), spot=prices[-1] + .15, spot_time=later.at)
        self.assertEqual(mover_candidates(engine, later, settings), [])

    def test_untradable_contract_is_not_posted_but_the_move_can_still_alert_later(self):
        settings, engine, prices, snap = self._mover_setup()
        wide = self.option(snap.at, 103, .60, 1.10)  # 45% spread: not tradable
        self.assertEqual(mover_candidates(engine, replace(snap, options=(wide,)), settings), [])
        # one minute later the same move has a tradable quote: it alerts (the move was never consumed)
        later = replace(snap, at=snap.at + timedelta(minutes=1))
        later = replace(later, bars=later.bars + (Bar(snap.bars[-1].end + timedelta(minutes=1), prices[-1], prices[-1] + .2,
                                                      prices[-1], prices[-1] + .15, 1000, 31000),), spot=prices[-1] + .15, spot_time=later.at)
        ok = self.option(later.at, 103, .90, 1.05)
        self.assertEqual(len(mover_candidates(engine, replace(later, options=(ok,)), settings)), 1)

    def test_flat_stock_never_alerts_and_keep_gate_holds_it(self):
        settings = replace(Settings(), mover_enabled=1, keep_gate=1)
        engine = Engine(self.store, settings)
        spy = self.snap("SPY", [500 + .05 * i for i in range(50)], 6)
        engine.spy_bars[spy.day] = spy.bars
        flat = self.snap("MU", [100 + (.03 if i % 2 else 0) for i in range(50)], 5)
        flat = replace(flat, options=(self.option(flat.at, 101, .50, .52),))
        self.assertEqual(mover_candidates(engine, flat, settings), [])
        cand = type("C", (), {})()
        cand.snapshot, cand.option, cand.metrics = flat, flat.options[0], {}
        self.assertIn("stock not moving", keep_reason(engine, cand, settings))

    def test_spy_against_blocks_the_mover(self):
        settings = replace(Settings(), mover_enabled=1)
        engine = Engine(self.store, settings)
        spy = self.snap("SPY", [500 - .08 * i for i in range(50)], 6)
        engine.spy_bars[spy.day] = spy.bars
        snap = self.snap("CRWD", [100 + .06 * i for i in range(50)], 5)
        snap = replace(snap, options=(self.option(snap.at, 103, .40, .44),))
        self.assertEqual(mover_candidates(engine, snap, settings), [])

    def test_default_settings_change_nothing(self):
        self.assertEqual(Settings().mover_enabled, 0)
        self.assertEqual(Settings().keep_gate, 0)

    def test_picker_prefers_tradable_near_030_delta(self):
        snap = self.snap("NET", [100 + .05 * i for i in range(50)], 5)
        a = self.option(snap.at, 103, .10, .40, delta=.30)   # 75% spread: excluded
        b = self.option(snap.at, 104, .30, .34, delta=.22)   # tradable
        c = self.option(snap.at, 105, .12, .14, delta=.15)   # ask below $0.15: excluded
        self.assertEqual(pick_contract(replace(snap, options=(a, b, c)), 1, Settings()).symbol, b.symbol)
        self.assertIsNone(pick_contract(replace(snap, options=(a, c)), 1, Settings()))

    def test_picker_falls_back_to_listed_next_day_when_same_day_is_untradable(self):
        snap = self.snap("NET", [100 + .05 * i for i in range(50)], 5)
        wide = self.option(snap.at, 103, .50, 1.0)
        later = replace(self.option(snap.at, 104, .80, 1.0),
                        expiry=snap.at.astimezone(ET).date() + timedelta(days=1), symbol="NEXT")
        self.assertEqual(pick_contract(replace(snap, options=(wide, later)), 1, Settings()).symbol, "NEXT")

    def test_missing_spy_is_unknown_and_does_not_erase_stock_move(self):
        settings, engine, _, snap = self._mover_setup()
        engine.spy_bars.clear()
        opt = self.option(snap.at, 103, .90, 1.05)
        out = mover_candidates(engine, replace(snap, options=(opt,)), settings)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0].metrics["mover_spy15_pct"])

    def test_future_bar_cannot_trigger_mover(self):
        settings, engine, _, snap = self._mover_setup()
        opt = self.option(snap.at, 103, .90, 1.05)
        future = replace(snap.bars[-1], end=snap.at + timedelta(minutes=1))
        self.assertEqual(mover_candidates(engine, replace(snap, bars=snap.bars[:-1] + (future,),
                                                        options=(opt,)), settings), [])


class BaseBreakTests(unittest.TestCase):
    def test_opening_new_extreme_is_not_a_fifteen_minute_base(self):
        from lotto.mover import base_break
        bars = bars_from([100, 101, 102])
        self.assertFalse(base_break(bars, 1, 15))

    def test_grind_day_realerts_on_break_of_a_base(self):
        from lotto.mover import base_break
        # +1 ATR stair-step: push, 20-min flat base, then a new high. 15-min move stays under .25 ATR.
        prices = [100 + .04 * i for i in range(40)] + [101.3] * 20 + [101.5, 101.7]
        bars = bars_from(prices)
        self.assertTrue(base_break(bars, 1, 15))
        self.assertFalse(base_break(bars[:-1] + (bars[-1].__class__(bars[-1].end, 101.3, 101.35, 101.25, 101.3, 1000, 0),), 1, 15))
        self.assertFalse(base_break(bars_from([100 + .04 * i for i in range(40)]), 1, 15))  # every bar a new high: no base

    def test_default_base_break_settings(self):
        s = Settings()
        self.assertEqual((s.mover_base_break, s.mover_base_minutes, s.mover_base_open_atr), (1, 15, .50))


class BypassTests(unittest.TestCase):
    def setUp(self):
        self.m = MoverTests('test_default_settings_change_nothing'); self.m.setUp()
        self.store = self.m.store

    def _snap(self):
        prices = [100 + .02 * i for i in range(30)] + [100.6 + .12 * i for i in range(20)]
        spy = self.m.snap("SPY", [500 + .05 * i for i in range(50)], 6)
        snap = self.m.snap("PANW", prices, 5)
        return spy, replace(snap, options=(self.m.option(snap.at, 103, .90, 1.05),))

    def test_mover_sends_under_hybrid_ab(self):
        settings = replace(Settings(), mover_enabled=1, hybrid=1, soft_blocks=1, min_score=60)
        engine = Engine(self.store, settings)
        spy, snap = self._snap()
        engine.spy_bars[spy.day] = spy.bars
        alerts = engine.process([snap])
        self.assertEqual(len(alerts), 1)
        row = self.store.db.execute("select delivery from alerts").fetchone()
        self.assertNotEqual(row[0], "internal")

    def test_mover_silent_when_option_lane_already_alerted_this_move(self):
        settings = replace(Settings(), mover_enabled=1)
        spy, snap = self._snap()
        first = Engine(self.store, settings); first.spy_bars[spy.day] = spy.bars
        self.assertEqual(len(first.process([snap])), 1)       # an ACTIVE alert now exists for PANW calls
        second = Engine(self.store, settings); second.spy_bars[spy.day] = spy.bars
        self.assertEqual(mover_candidates(second, snap, settings), [])

    def test_hybrid_reversal_goes_to_tier_c(self):
        from lotto.engine import hybrid_tier
        from lotto.patterns import Setup
        policy = type("P", (), {"reasons": ()})()
        for name, want in (("REVERSAL", "C"), ("INTRADAY_REVERSAL", "C"), ("CONTINUATION", "A")):
            c = type("C", (), {})()
            c.score, c.metrics, c.setup = 99, {}, Setup(name, 1, 0)
            self.assertEqual(hybrid_tier(c, policy, Settings()), want, name)

    def test_internal_c_tier_does_not_silence_same_cycle_mover(self):
        settings = replace(Settings(), mover_enabled=1, hybrid=1, soft_blocks=1, min_score=60)
        engine = Engine(self.store, settings)
        spy, snap = self._snap()
        engine.spy_bars[spy.day] = spy.bars
        option = snap.options[0]
        legacy = Candidate(snap, option, 75, {}, {"opening_expansion": 1}, [], True,
                           setup=Setup("CONTINUATION", snap.spot, snap.spot - 1))
        with patch("lotto.engine.candidates", return_value=[legacy]):
            engine.process([snap])
        rows = self.store.db.execute("SELECT setup_name,quality_tier FROM alerts ORDER BY setup_name").fetchall()
        self.assertEqual([(r["setup_name"], r["quality_tier"]) for r in rows],
                         [("CONTINUATION", "C"), ("MOVER", "MAIN")])

    def test_sendable_ab_idea_covers_same_cycle_mover(self):
        settings = replace(Settings(), mover_enabled=1, hybrid=1, soft_blocks=1, min_score=60)
        engine = Engine(self.store, settings)
        spy, snap = self._snap()
        engine.spy_bars[spy.day] = spy.bars
        option = snap.options[0]
        legacy = Candidate(snap, option, 96, {}, {"opening_expansion": 1}, [], True,
                           setup=Setup("CONTINUATION", snap.spot, snap.spot - 1))
        with patch("lotto.engine.candidates", return_value=[legacy]):
            engine.process([snap])
        rows = self.store.db.execute("SELECT setup_name,quality_tier FROM alerts").fetchall()
        self.assertEqual([(r["setup_name"], r["quality_tier"]) for r in rows],
                         [("CONTINUATION", "A")])

    def test_failed_ab_delivery_releases_same_cycle_mover(self):
        settings = replace(Settings(), mover_enabled=1, hybrid=1, soft_blocks=1, min_score=60)
        engine = Engine(self.store, settings)
        spy, snap = self._snap()
        engine.spy_bars[spy.day] = spy.bars
        legacy = Candidate(snap, snap.options[0], 96, {}, {"opening_expansion": 1}, [], True,
                           setup=Setup("CONTINUATION", snap.spot, snap.spot - 1))
        with patch("lotto.engine.candidates", return_value=[legacy]):
            engine.process([snap])
        with self.store.db:
            self.store.db.execute("UPDATE alerts SET delivery='failed'")
        self.assertEqual(len(mover_candidates(engine, snap, settings)), 1)


class MoverDeliveryTests(unittest.TestCase):
    def test_send_quote_requires_fresh_tradable_two_sided_market(self):
        now = datetime.now(timezone.utc)
        valid = {"realtime": True, "quote_time": now, "spot_time": now,
                 "bid": .80, "ask": 1.0, "spot": 100.0}
        self.assertTrue(mover_send_quote_ok(valid, now))
        for change in ({"ask": 1.5}, {"bid": 0}, {"spot": None},
                       {"quote_time": now - timedelta(seconds=91)},
                       {"spot_time": now - timedelta(seconds=91)}, {"realtime": False}):
            self.assertFalse(mover_send_quote_ok({**valid, **change}, now), change)

    def test_failed_mover_requote_stays_internal_without_discord_post(self):
        helper = MoverTests("test_default_settings_change_nothing")
        helper.setUp()
        self.addCleanup(helper.store.db.close)
        settings, engine, _, snap = helper._mover_setup()
        snap = replace(snap, options=(helper.option(snap.at, 103, .90, 1.05),))
        candidate = mover_candidates(engine, snap, settings)[0]
        from lotto.discord import alert_payload
        helper.store.queue(candidate, alert_payload(candidate, "ACTIVE"), dry_run=False,
                           phase="ACTIVE", tier="MAIN")
        with helper.store.db:
            helper.store.db.execute("UPDATE alerts SET at=?", (datetime.now(timezone.utc).isoformat(),))
        with patch("lotto.discord.urlopen") as send:
            deliver_pending(helper.store, "https://discord.com/api/webhooks/test/test",
                            quoter=lambda contract, symbol: None)
            send.assert_not_called()
        self.assertEqual(helper.store.db.execute("SELECT delivery FROM alerts").fetchone()[0], "internal")
