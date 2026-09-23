import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from lotto.config import Settings
from lotto.demo import demo_frames
from lotto.discovery import Discovery
from lotto.engine import Engine
from lotto.features import candidates, persistent_activity
from lotto.main import DEFAULT_UNIVERSE
from lotto.models import Bar, ET
from lotto.nightly import run_nightly
from lotto.patterns import detect_setup, path_features
from lotto.schwab import benchmark_for
from lotto.store import Store
from unittest.mock import Mock, patch
from lotto.nightly import maybe_nightly


class DevelopingTests(unittest.TestCase):
    def setUp(self):
        self.frames = [f[0] for f in demo_frames()]

    def path(self, prices, prior=100):
        opening = datetime(2026,9,21,9,30,tzinfo=ET)
        bars = tuple(Bar(opening+timedelta(minutes=i+1), prices[max(0,i-1)], max(prices[max(0,i-1)],p)+.01,
                         min(prices[max(0,i-1)],p)-.01,p,1000,(i+1)*400) for i,p in enumerate(prices))
        return replace(self.frames[-1],at=bars[-1].end,spot_time=bars[-1].end,spot=prices[-1],
                       bars=bars,prior_close=prior,prior_atr=3)

    def test_opening_break_after_ten_minutes(self):
        snap = self.path([100+i*.15 for i in range(12)])
        setup = detect_setup(snap,1,path_features(snap,1))
        self.assertEqual(setup.name,"OPENING_BREAK")
        self.assertLess(setup.trigger,snap.spot)

    def test_reversal_does_not_require_green_day(self):
        snap = self.path([100-i*.2 for i in range(16)]+[97+i*.25 for i in range(12)],prior=104)
        setup = detect_setup(snap,1,path_features(snap,1))
        self.assertEqual(setup.name,"REVERSAL")
        self.assertLess(snap.spot,snap.prior_close)

    def test_hour_coil_can_build_before_breakout(self):
        prices = [100+i*.15 for i in range(20)]+[103+(.08 if i%2 else -.08) for i in range(60)]+[103.09]*5
        snap = self.path(prices)
        metrics = path_features(snap,1)
        setup = detect_setup(snap,1,metrics)
        self.assertEqual(setup.name,"COILED_CONTINUATION")
        self.assertTrue(setup.building)
        self.assertEqual(metrics["consolidation_minutes"],60)
        self.assertLess(snap.spot,setup.trigger)
        self.assertLess(setup.invalidation,snap.spot)

    def test_chop_without_prior_impulse_is_not_a_coil(self):
        snap = self.path([100+(.05 if i%2 else -.05) for i in range(85)])
        setup = detect_setup(snap,1,path_features(snap,1))
        self.assertTrue(setup is None or setup.name!="COILED_CONTINUATION")

    def test_twenty_minute_flow_rejects_resets_and_gaps(self):
        snap=self.frames[30]; history=self.frames[9:30]; ids={o.symbol for o in snap.options}
        self.assertTrue(persistent_activity(snap,history,ids,Settings()))
        self.assertFalse(persistent_activity(snap,history[:5]+history[10:],ids,Settings()))
        history[12]=replace(history[12],options=tuple(replace(o,volume=0) for o in history[12].options))
        self.assertFalse(persistent_activity(snap,history,ids,Settings()))

    def test_all_102_names_get_discovery_even_with_twelve_chains(self):
        symbols=DEFAULT_UNIVERSE.split(','); self.assertEqual(len(symbols),102)
        now=self.frames[20].at
        quotes={s:{"price":101,"open":100,"previous":100,"volume":10000,"at":now,"high":102,"low":99} for s in symbols}
        discovery=Discovery(12)
        selected=discovery.update(quotes,now,set(symbols))
        self.assertEqual(len(selected),12)
        self.assertEqual(len(discovery.observations),102)
        self.assertEqual({r['symbol'] for r in discovery.observations},set(symbols))
        self.assertEqual(sum(r['promoted'] for r in discovery.observations),12)

    def test_lease_stability_and_strong_newcomer_promotion(self):
        now=self.frames[20].at; d=Discovery(4)
        q={s:{"price":100,"open":100,"previous":100,"volume":100,"at":now} for s in 'ABCDE'}
        initial=d.update(q,now,set(q))
        self.assertNotIn('E',initial)
        later=now+timedelta(minutes=1)
        q={s:{**v,'at':later,'price':110 if s=='E' else 100} for s,v in q.items()}
        selected=d.update(q,later,set(q))
        self.assertIn('E',selected)
        self.assertEqual(len(set(initial)&set(selected)),3)

    def test_no_self_confirming_benchmark(self):
        for s in DEFAULT_UNIVERSE.split(','):
            self.assertNotEqual(benchmark_for(s),s)
        self.assertEqual(benchmark_for('TSM'),'SMH')
        snap=replace(self.frames[30],context_label='DEMO')
        self.assertFalse(any(c.qualifying for c in candidates(snap,self.frames[10:30],Settings())))

    def test_qqq_relative_strength_can_confirm_against_flat_spy(self):
        snap=replace(self.frames[30],symbol='QQQ',context_label='SPY',context_return_5m=0)
        results=candidates(snap,self.frames[10:30],Settings())
        self.assertTrue(results)
        self.assertTrue(all('independent sector/peer confirmation missing' not in c.blockers for c in results))

    def test_nightly_waits_for_early_close_and_runs_once(self):
        store=Store(':memory:'); self.addCleanup(store.db.close)
        close=datetime(2026,9,21,13,tzinfo=ET)
        client=Mock();client.session.return_value=(close.replace(hour=9,minute=30),close)
        with patch('lotto.nightly.run_nightly') as run:
            maybe_nightly(store,client,['AAA'],'data',close+timedelta(minutes=4))
            run.assert_not_called()
            maybe_nightly(store,client,['AAA'],'data',close+timedelta(minutes=5))
            run.assert_called_once()
            store.db.execute('INSERT INTO nightly_runs VALUES (?,?,?)',('2026-09-21',close.isoformat(),'report.json'))
            maybe_nightly(store,client,['AAA'],'data',close+timedelta(minutes=6))
            run.assert_called_once()

    def test_fresh_zero_bid_is_minus_100_but_morning_is_not_close(self):
        store=Store(':memory:'); self.addCleanup(store.db.close)
        first=self.frames[20]; second=replace(self.frames[21],options=tuple(replace(o,bid=0) for o in self.frames[21].options))
        store.record(first);store.record(second);store.record(replace(second,at=second.at+timedelta(seconds=20)))
        row=store.end_of_day_options(first.day)[0]
        self.assertEqual(row['quote_count'],2)
        self.assertEqual(row['observed_return'],-1)
        self.assertEqual(row['min_bid'],0)
        self.assertIsNone(row['open_to_close_return'])

    def test_early_close_last_minute_included(self):
        store=Store(':memory:');self.addCleanup(store.db.close)
        close=self.frames[20].at.replace(hour=13,minute=0)
        at=close-timedelta(seconds=20)
        snap=replace(self.frames[20],at=at,spot_time=at,session_end=close,
                     options=tuple(replace(o,quote_time=at) for o in self.frames[20].options))
        store.record(snap)
        row=store.end_of_day_options(snap.day)[0]
        self.assertTrue(row['close_covered'])
        self.assertFalse(row['open_covered'])
        self.assertIsNone(row['open_to_close_return'])

    def test_nightly_records_failures_and_does_not_mutate_live_rules(self):
        store=Store(':memory:');self.addCleanup(store.db.close)
        settings=Settings();engine=Engine(store,settings)
        for s in self.frames: engine.process([s])
        with tempfile.TemporaryDirectory() as directory:
            report=json.loads(run_nightly(store,self.frames[0].day,directory).read_text())
        self.assertTrue(report['candidate_evaluations'])
        self.assertTrue(report['decision_reasons'])
        self.assertFalse(report['full_universe_price_research'])
        self.assertEqual(engine.settings,settings)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM nightly_runs').fetchone()[0],0)
