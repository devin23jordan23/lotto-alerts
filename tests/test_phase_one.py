import json
from dataclasses import replace
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from lotto.config import Settings, STRATEGY_VERSION
from lotto.demo import demo_frames
from lotto.discovery import Discovery
from lotto.engine import Engine
from lotto.features import candidates, contract_ok, early_broad_flow
from lotto.models import Bar, ET
from lotto.nightly import alert_feedback
from lotto.patterns import Setup, detect_setup, path_features
from lotto.store import Store


class PhaseOneTests(unittest.TestCase):
    def setUp(self):
        self.frames=[f[0] for f in demo_frames()]
        self.store=Store(":memory:")
        self.addCleanup(self.store.db.close)

    def path(self, prices, atr=5):
        opening=datetime(2026,9,21,9,30,tzinfo=ET)
        bars=tuple(Bar(opening+timedelta(minutes=i+1),prices[max(0,i-1)],
            max(prices[max(0,i-1)],p)+.01,min(prices[max(0,i-1)],p)-.01,p,1000,(i+1)*600)
            for i,p in enumerate(prices))
        return replace(self.frames[-1],at=bars[-1].end,spot_time=bars[-1].end,spot=prices[-1],
                       bars=bars,prior_close=100,prior_atr=atr)

    def test_opening_drive_has_short_valid_window_and_no_future_bar(self):
        engine=Engine(self.store)
        for snap in self.frames[:5]:
            self.assertFalse(engine.process([snap]))
        alerts=engine.process([self.frames[5]])
        self.assertEqual(len(alerts),1)
        setup = detect_setup(self.frames[5], 1, path_features(self.frames[5], 1))
        self.assertEqual(setup.name, "OPENING_DRIVE")
        self.assertIn("Trigger", alerts[0]["payload"]["embeds"][0]["description"])
        invalid=replace(self.frames[5],at=self.frames[4].at,spot_time=self.frames[4].at)
        self.assertTrue(invalid.problems(min_bars=5))

    def test_poll_jitter_does_not_add_an_extra_confirmation_minute(self):
        engine=Engine(self.store)
        for snap in self.frames[:5]:
            engine.process([replace(snap,at=snap.at+timedelta(seconds=40))])
        self.assertEqual(len(engine.process([replace(self.frames[5],at=self.frames[5].at+timedelta(seconds=5))])),1)

    def test_broad_early_flow_accepts_steady_options_volume_without_weakening_later_scans(self):
        snap = self.frames[15]
        setup = Setup("OPENING_DRIVE", snap.spot-.1, snap.spot-1)
        metrics = {"impulse_atr":.26, "move_from_open_atr":.26,
                   "above_vwap_share":1.0, "return_5m_directional":.008,
                   "local_rvol_5m":1.18, "volume_acceleration":1.31,
                   "cluster_size":15, "option_volume_5m":7221,
                   "contract_volume_rate_5m":1314}
        option = snap.options[0]
        self.assertTrue(early_broad_flow(snap, setup, metrics, 81.8, option, Settings()))
        self.assertFalse(early_broad_flow(snap, replace(setup, building=True), metrics, 81.8, option, Settings()))
        self.assertFalse(early_broad_flow(snap, setup, {**metrics,"option_volume_5m":500}, 81.8, option, Settings()))
        self.assertFalse(early_broad_flow(snap, setup, {**metrics,"volume_acceleration":1.0}, 81.8, option, Settings()))
        self.assertFalse(early_broad_flow(replace(snap,bars=snap.bars*4), setup, metrics, 81.8, option, Settings()))
        self.assertFalse(early_broad_flow(replace(snap,at=snap.at+timedelta(hours=2)), setup, metrics, 81.8, option, Settings()))

    def test_contract_cap_scales_with_volatility_but_remains_bounded(self):
        snap=replace(self.frames[20],spot=1000,prior_atr=30)
        option=replace(snap.options[0],strike=1010,ask=5.45,bid=5.35,delta=.25)
        self.assertTrue(contract_ok(option,snap,Settings()))
        self.assertFalse(contract_ok(option,replace(snap,prior_atr=5),Settings()))
        self.assertFalse(contract_ok(replace(option,ask=8,bid=7.90),snap,Settings()))
        self.assertFalse(contract_ok(replace(option,strike=1040),snap,Settings()))
        self.assertFalse(contract_ok(replace(option,bid=5.0),snap,Settings()))

    def test_entry_location_and_remaining_room_can_veto_a_strong_score(self):
        snap=self.frames[15]
        self.assertTrue(any(c.qualifying for c in candidates(snap,self.frames[:15],Settings())))
        for setup,blocker in [
            (Setup("CONTINUATION",snap.spot+1,snap.spot-1,True),"too far from price trigger"),
            (Setup("OPENING_BREAK",snap.spot-2,snap.spot-3),"too far from price trigger"),
            (Setup("PULLBACK_RESUMPTION",snap.spot-.05,snap.spot-1,target=snap.spot+.20),
             "insufficient room to retest prior extreme")]:
            with self.subTest(setup=setup),patch("lotto.features.detect_setup",return_value=setup):
                evaluated=candidates(snap,self.frames[:15],Settings())
                self.assertTrue(evaluated)
                self.assertTrue(all(not c.qualifying and blocker in c.blockers for c in evaluated))

    def test_minimum_two_adjacent_strikes_and_counter_integrity(self):
        snaps=[replace(s,options=s.options[-2:]) for s in self.frames]
        self.assertTrue(candidates(snaps[15],snaps[:15],Settings()))
        self.assertFalse(candidates(replace(snaps[15],options=snaps[15].options[:1]),
                                    [replace(s,options=s.options[:1]) for s in snaps[:15]],Settings()))
        reset=snaps[:15]
        reset[13]=replace(reset[13],options=tuple(replace(o,volume=0) for o in reset[13].options))
        self.assertFalse(candidates(snaps[15],reset,Settings()))

    def test_pullback_resumption_keeps_original_high_as_target(self):
        snap=self.path([100+i*.25 for i in range(20)]+[104.75-i*.15 for i in range(10)]
                       +[103.4+i*.08 for i in range(10)])
        setup=detect_setup(snap,1,path_features(snap,1))
        self.assertEqual(setup.name,"PULLBACK_RESUMPTION")
        self.assertGreater(setup.target,snap.spot)
        self.assertLess(setup.invalidation,snap.spot)
        # Put symmetry uses the same known path, with prices reflected around 100.
        bearish=replace(snap,spot=200-snap.spot,bars=tuple(replace(b,open=200-b.open,
            high=200-b.low,low=200-b.high,close=200-b.close) for b in snap.bars))
        put=detect_setup(bearish,-1,path_features(bearish,-1))
        self.assertEqual(put.name,setup.name)
        self.assertAlmostEqual(put.target,200-setup.target)

    def test_continuous_indexes_cannot_be_displaced_by_large_stock_moves(self):
        discovery=Discovery(8,always_deep={"SPY","QQQ","IWM"})
        now=self.frames[10].at
        symbols={"SPY","QQQ","IWM","A","B","C","D","E","F","G"}
        for minute in range(35):
            at=now+timedelta(minutes=minute)
            quotes={s:{"price":100 if s in discovery.always_deep else 110+minute,
                       "open":100,"previous":100,"volume":10000+minute*100,"at":at,"high":150,"low":99}
                    for s in symbols}
            selected=discovery.update(quotes,at,symbols)
            self.assertTrue(discovery.always_deep.issubset(selected))
            self.assertEqual(len(selected),8)
            self.assertEqual(len(discovery.observations),len(symbols))

    def test_feedback_and_candidate_version_are_persisted(self):
        engine=Engine(self.store)
        for snap in self.frames:
            engine.process([snap])
        alert=self.store.summary()[0]
        self.store.add_feedback(alert["id"],"mixed","First partial worked; remainder faded")
        feedback=alert_feedback(self.store,self.frames[0].day)[0]
        self.assertEqual(feedback["strategy"],STRATEGY_VERSION)
        self.assertEqual(feedback["user_feedback"]["outcome"],"mixed")
        self.assertEqual(feedback["delivery"],"dry_run")
        self.assertTrue(feedback["first_100pct_at"])
        row=self.store.db.execute("SELECT strategy,features FROM candidate_evaluations LIMIT 1").fetchone()
        self.assertEqual(row["strategy"],STRATEGY_VERSION)
        self.assertIn("settings",json.loads(row["features"]))
        with self.assertRaises(ValueError):
            self.store.add_feedback("unknown","worked")

    def test_nonfinite_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            Settings(min_score=float("nan"))
        with self.assertRaises(ValueError):
            Settings(max_dte=2)
