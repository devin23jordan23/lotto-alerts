from dataclasses import replace
from datetime import timedelta
import unittest

from lotto.demo import demo_frames
from lotto.models import ET, Option
from lotto.patterns import Setup
from lotto.quality import QualityConfig, evaluate


class S2Tests(unittest.TestCase):
    def setUp(self):
        self.snap = list(demo_frames())[-1][0]
        local = self.snap.at.astimezone(ET)
        self.snap = replace(self.snap, at=local.replace(hour=11, minute=0))
        self.today = self.snap.at.astimezone(ET).date()

    def cand(self, setup="CONTINUATION", dte=0, mfo=.6, rvol=1.5, flow=1):
        opt = Option("X C100", self.today + timedelta(days=dte), "CALL", 100, 1.0, 1.05, 500, 100, .3, .02, self.snap.at)
        c = type("C", (), {})()
        c.snapshot, c.option, c.setup = self.snap, opt, Setup(setup, 100, 99)
        # response/return high enough that WF never fires; extension < 1 so LATE never fires
        c.metrics = {"move_from_open_atr": mfo, "local_rvol_5m": rvol, "options_persistent_20m": flow,
                     "response_proxy": 1.0, "return_5m_directional": .01, "price_acceleration": .001}
        return c

    def test_clean_directional_idea_posts(self):
        self.assertNotEqual(evaluate(self.cand(), "S2").tier, "RESEARCH")

    def test_each_s2_rule_demotes(self):
        for kw, rule in ((dict(setup="INTRADAY_REVERSAL"), "REV"), (dict(setup="REVERSAL"), "REV"),
                         (dict(dte=1), "DTE1"), (dict(mfo=-.2), "AGAINST_OPEN"), (dict(rvol=.5), "LOWRVOL")):
            d = evaluate(self.cand(**kw), "S2")
            self.assertEqual(d.tier, "RESEARCH", kw)
            self.assertIn(rule, d.reasons, kw)

    def test_missing_rvol_is_not_a_demotion(self):
        self.assertNotIn("LOWRVOL", evaluate(self.cand(rvol=None), "S2").reasons)

    def test_reversal_is_not_double_counted_as_against_open(self):
        self.assertNotIn("AGAINST_OPEN", evaluate(self.cand(setup="REVERSAL", mfo=-.5), "S2").reasons)

    def test_policy_s_is_unchanged(self):
        # Under S these traits are not research reasons (S only routes WF / LATE to research).
        for kw in (dict(setup="INTRADAY_REVERSAL"), dict(mfo=-.2), dict(rvol=.5)):
            d = evaluate(self.cand(**kw), "S")
            self.assertNotEqual(d.tier, "RESEARCH", kw)
            self.assertEqual(d.policy_id, "S-2026-10-19-open0935")

    def test_config_accepts_s2(self):
        self.assertEqual(QualityConfig("enforce", "S2").policy, "S2")
        with self.assertRaises(ValueError):
            QualityConfig("enforce", "S9")
