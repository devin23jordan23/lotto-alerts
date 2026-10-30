import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

from lotto.demo import demo_frames
from lotto.engine import Engine
from lotto.features import Candidate
from lotto.models import ET
from lotto.patterns import Setup
from lotto.quality import QualityConfig, evaluate, scalar
from lotto.quality_report import scorecard
from lotto.store import Store
from lotto.discord import deliver_pending


class QualityTests(unittest.TestCase):
    def candidate(self, metrics=None, at=None, ask=.5, setup='CONTINUATION', dte=0):
        snap=list(demo_frames())[8][0]
        at=at or datetime(2026,10,16,11,10,tzinfo=ET)
        option=replace(snap.options[0],ask=ask,bid=ask*.95,expiry=at.date()+timedelta(days=dte),quote_time=at)
        snap=replace(snap,at=at,spot_time=at,options=(option,),market_time=at,market_return_5m=.001)
        return Candidate(snap,option,90,{},metrics or {},[],True,setup=Setup(setup,snap.spot,snap.spot-1))

    def test_weak_boundary_and_expansion(self):
        self.assertEqual(evaluate(self.candidate({'response_proxy':.4,'return_5m_directional':.0039})).tier,'RESEARCH')
        self.assertEqual(evaluate(self.candidate({'response_proxy':.4,'return_5m_directional':.004})).tier,'MAIN')
        self.assertEqual(evaluate(self.candidate({'response_proxy':.4,'return_5m_directional':.0039,'opening_expansion':1})).tier,'MAIN')

    def test_premium_boundary_and_escape(self):
        self.assertEqual(evaluate(self.candidate(ask=1.5)).tier,'MAIN')
        self.assertEqual(evaluate(self.candidate(ask=1.51)).tier,'SECONDARY')
        m=dict(return_5m_directional=.001,move_from_open_atr=.25,efficiency=.45,relative_market_5m=0,local_rvol_5m=.8)
        self.assertEqual(evaluate(self.candidate(m,ask=1.51)).tier,'MAIN')
        m['response_proxy']=.4
        self.assertEqual(evaluate(self.candidate(m,ask=1.51)).tier,'RESEARCH')

    def test_opening_time_boundary(self):
        at=datetime(2026,10,16,9,34,59,tzinfo=ET)
        self.assertIn('OPEN',evaluate(self.candidate(at=at)).reasons)
        self.assertNotIn('OPEN',evaluate(self.candidate(at=at+timedelta(seconds=1))).reasons)
        for minute in (35,39,45,59):
            c=self.candidate(at=at.replace(minute=minute,second=0))
            self.assertEqual(evaluate(c).tier,'MAIN')
            self.assertEqual(evaluate(c,'stack8').tier,'SECONDARY')
        self.assertNotIn('OPEN',evaluate(self.candidate(at=at.replace(hour=10,minute=0)),'stack8').reasons)

    def test_opening_relief_preserves_other_quality_rules(self):
        at=datetime(2026,10,16,9,35,tzinfo=ET)
        self.assertEqual(evaluate(self.candidate(at=at,ask=2)).reasons,('PREM',))
        self.assertEqual(evaluate(self.candidate({'response_proxy':.4,'return_5m_directional':.001},at=at)).tier,'RESEARCH')

    def test_0935_opening_drive_delivers_to_main_without_secondary_hook(self):
        store=Store(':memory:');self.addCleanup(store.db.close)
        engine=Engine(store,dry_run=False,quality=QualityConfig(mode='enforce'))
        snap=list(demo_frames())[4][0]  # Five completed bars, 09:35 ET.
        c=Candidate(snap,snap.options[0],90,{},
                    {'response_proxy':.8,'return_5m_directional':.01},[],True,
                    setup=Setup('OPENING_DRIVE',snap.spot,snap.spot-1))
        with patch('lotto.engine.candidates',return_value=[c]):
            alerts=engine.process([snap])
        self.assertEqual(len(alerts),1)
        self.assertEqual(store.db.execute('SELECT quality_tier FROM alerts').fetchone()[0],'MAIN')
        with patch('lotto.discord.datetime') as clock,patch('lotto.discord.urlopen') as send:
            clock.now.return_value=snap.at
            clock.fromisoformat.side_effect=datetime.fromisoformat
            deliver_pending(store,'https://discord.com/api/webhooks/main/test',secondary_webhook='')
        self.assertEqual(send.call_args.args[0].full_url,'https://discord.com/api/webhooks/main/test')
        self.assertEqual(store.db.execute('SELECT delivery FROM alerts').fetchone()[0],'sent')

    def test_exhaustion_exact_constants(self):
        self.assertNotIn('EXHQ',evaluate(self.candidate({'local_rvol_5m':2.16036,'price_acceleration':0})).reasons)
        self.assertNotIn('EXHQ',evaluate(self.candidate({'local_rvol_5m':3,'price_acceleration':.00039389})).reasons)
        self.assertIn('EXHQ',evaluate(self.candidate({'local_rvol_5m':2.1603607104117084,'price_acceleration':.00034480323590035944})).reasons)

    def test_one_day_reclaim_exception(self):
        self.assertIn('W1DTE',evaluate(self.candidate(dte=1)).reasons)
        self.assertNotIn('W1DTE',evaluate(self.candidate(dte=1,setup='LEVEL_RECLAIM')).reasons)

    def test_missing_nonfinite_and_bool_metrics_do_not_fire(self):
        for value in (None,True,False,float('nan'),float('inf'),'0.1'):
            self.assertIsNone(scalar({'a':value},'a'))
            self.assertNotIn('WF',evaluate(self.candidate({'response_proxy':value,'return_5m_directional':.001})).reasons)
        self.assertEqual(evaluate(self.candidate()).tier,'MAIN')

    def test_stale_future_benchmark_not_labeled(self):
        c=self.candidate(setup='LEVEL_RECLAIM')
        c.snapshot=replace(c.snapshot,market_return_5m=-.01,market_time=c.snapshot.at-timedelta(seconds=91))
        self.assertNotIn('LRSPY',evaluate(c,'stack8').reasons)
        self.assertFalse(evaluate(c).labels)
        c.snapshot=replace(c.snapshot,market_time=c.snapshot.at+timedelta(seconds=1))
        self.assertFalse(evaluate(c).labels)
        c.snapshot=replace(c.snapshot,market_time=c.snapshot.at,market_label='QQQ')
        self.assertIn('QQQ opposing',evaluate(c).labels)
        self.assertIn('LRSPY',evaluate(c,'stack8').reasons)

    def test_disable_rule_and_label_only(self):
        c=self.candidate({'move_from_open_atr':-1},setup='REVERSAL')
        self.assertEqual(evaluate(c).tier,'MAIN')
        self.assertEqual(evaluate(c,'stack8').tier,'SECONDARY')
        self.assertEqual(evaluate(self.candidate(ask=2),disabled={'PREM'}).tier,'MAIN')

    def test_mode_validation(self):
        for kwargs in ({'mode':'bogus'},{'policy':'stack8'},{'disabled_rules':frozenset({'NOPE'})}):
            with self.assertRaises(ValueError):QualityConfig(**kwargs)

    def engine_run(self, mode, metrics, ask=.5):
        store=Store(':memory:');self.addCleanup(store.db.close)
        engine=Engine(store,dry_run=False,quality=QualityConfig(mode=mode))
        frames=[f[0] for f in demo_frames()][:10]
        def candidates(snap,*_):
            option=replace(snap.options[0],ask=ask,bid=ask*.95)
            return [Candidate(snap,option,90,{},dict(metrics),[],True,
                              setup=Setup('LEVEL_RECLAIM',snap.spot,snap.spot-1))]
        with patch('lotto.engine.candidates',side_effect=candidates):
            alerts=[a for snap in frames for a in engine.process([snap])]
        return store,alerts,frames

    def test_enforce_replaces_older_weak_gate(self):
        metrics={'response_proxy':.4,'return_5m_directional':.0045}
        off,old,_=self.engine_run('off',metrics)
        live,new,_=self.engine_run('enforce',metrics)
        self.assertEqual(len(old),0)
        self.assertGreater(len(new),0)
        self.assertEqual(live.db.execute('SELECT COUNT(*) FROM research_ideas').fetchone()[0],0)

    def test_shadow_delivery_and_metrics_unchanged(self):
        m={'response_proxy':.8,'return_5m_directional':.01}
        off,a,_=self.engine_run('off',m)
        shadow,b,_=self.engine_run('shadow',m)
        self.assertEqual(a,b)
        self.assertEqual([tuple(r) for r in off.db.execute('SELECT * FROM decisions')],
                         [tuple(r) for r in shadow.db.execute('SELECT * FROM decisions')])
        self.assertEqual(off.db.execute('SELECT COUNT(*) FROM quality_shadow').fetchone()[0],0)
        self.assertGreater(shadow.db.execute('SELECT COUNT(*) FROM quality_shadow').fetchone()[0],0)
        self.assertTrue(shadow.db.execute("SELECT COUNT(*) FROM quality_shadow WHERE live_outcome LIKE '%cooldown%'").fetchone()[0])

    def test_research_has_no_alert_or_cooldown(self):
        store,alerts,_=self.engine_run('enforce',{'response_proxy':.4,'return_5m_directional':.001})
        self.assertFalse(alerts)
        self.assertGreater(store.db.execute('SELECT COUNT(*) FROM research_ideas').fetchone()[0],0)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0],0)

    def test_secondary_internal_without_webhook_main_sends_and_records_delivery(self):
        store,alerts,frames=self.engine_run('enforce',{'response_proxy':.8,'return_5m_directional':.01},ask=2)
        self.assertTrue(alerts)
        with patch('lotto.discord.datetime') as clock,patch('lotto.discord.urlopen') as send:
            clock.now.return_value=frames[-1].at
            clock.fromisoformat.side_effect=datetime.fromisoformat
            deliver_pending(store,'https://discord.com/api/webhooks/main/test',secondary_webhook='')
        send.assert_not_called()
        self.assertEqual(store.db.execute('SELECT delivery FROM alerts').fetchone()[0],'internal')
        self.assertTrue(store.active_options(frames[-1].day))

    def test_secondary_uses_only_secondary_destination(self):
        store,alerts,frames=self.engine_run('enforce',{'response_proxy':.8,'return_5m_directional':.01},ask=2)
        at=datetime.fromisoformat(store.db.execute('SELECT at FROM alerts').fetchone()[0])
        with patch('lotto.discord.datetime') as clock,patch('lotto.discord.urlopen') as send:
            clock.now.return_value=at
            clock.fromisoformat.side_effect=datetime.fromisoformat
            deliver_pending(store,'https://discord.com/api/webhooks/main/test','https://discord.com/api/webhooks/secondary/test')
        self.assertEqual(send.call_args.args[0].full_url,'https://discord.com/api/webhooks/secondary/test')
        self.assertIsNotNone(store.db.execute('SELECT delivered_at FROM alerts').fetchone()[0])

    def test_shadow_paths_preserve_order_and_censor_incomplete_losses(self):
        store=Store(':memory:');self.addCleanup(store.db.close)
        c=self.candidate()
        store.quality_shadow(c,evaluate(c),'ACTIVE')
        for minute,bid in ((1,.3),(2,.8)):
            at=c.snapshot.at+timedelta(minutes=minute)
            option=replace(c.option,bid=bid,ask=bid+.02,quote_time=at)
            store.track(replace(c.snapshot,at=at,spot_time=at,options=(option,)),__import__('lotto.config',fromlist=['Settings']).Settings())
        result=scorecard(store,c.snapshot.day)['observations'][0]
        self.assertEqual(result['WIN_BADENTRY'],1)
        self.assertEqual(result['WIN_CLEAN'],0)
        self.assertEqual(result['incomplete_or_gapped'],1)
        self.assertEqual(result['LOSS_STOPPED'],0)
