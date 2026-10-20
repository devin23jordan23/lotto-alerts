import unittest
from dataclasses import replace
from datetime import timedelta

from lotto.demo import demo_frames
from lotto.engine import distinct_price_leg
from lotto.features import Candidate, index_price_flow_confirmation
from lotto.patterns import Setup
from lotto.store import Store


class EtfPaceAndRearmTests(unittest.TestCase):
    def test_index_flow_requires_price_and_option_confirmation(self):
        snap = replace(list(demo_frames())[8][0], symbol="SPY")
        setup = Setup("OPENING_BREAK", snap.spot - .2, snap.spot - 1)
        metrics = {"return_5m_directional": .002, "efficiency": .65}
        self.assertTrue(index_price_flow_confirmation(snap, setup, metrics, 84, .85, 4, 1.6, False))
        self.assertFalse(index_price_flow_confirmation(snap, setup, metrics, 84, .5, 4, 1.6, False))
        self.assertFalse(index_price_flow_confirmation(snap, setup, metrics, 84, .85, 1, 1.6, False))
        self.assertFalse(index_price_flow_confirmation(snap, setup, metrics, 84, .85, 4, 1.0, False))
        self.assertFalse(index_price_flow_confirmation(snap, replace(setup, building=True), metrics, 84, .85, 4, 1.6, False))
        self.assertFalse(index_price_flow_confirmation(replace(snap, symbol="AAPL"), setup, metrics, 84, .85, 4, 1.6, False))

    def test_new_level_rearms_without_a_fixed_wait_and_same_leg_does_not(self):
        snap = list(demo_frames())[8][0]
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        option = snap.options[0]
        prior = Candidate(snap, option, 90, {}, {}, [], True,
                          setup=Setup("LEVEL_RECLAIM", snap.spot - .1, snap.spot - 1))
        store.queue(prior, {"embeds": [{}]}, True, "ACTIVE")
        row = store.last_alert(snap.day, snap.symbol, "ACTIVE")
        atr = snap.prior_atr
        later = replace(snap, at=snap.at + timedelta(minutes=1), spot=snap.spot + .3 * atr)
        same = Candidate(later, option, 90, {}, {}, [], True, setup=prior.setup)
        next_level = replace(same, setup=replace(prior.setup, trigger=prior.setup.trigger + .3 * atr))
        self.assertFalse(distinct_price_leg(same, row))
        self.assertTrue(distinct_price_leg(next_level, row))
        self.assertFalse(distinct_price_leg(replace(next_level, setup=replace(next_level.setup, building=True)), row))
        rolling = replace(next_level, setup=replace(next_level.setup, name="CONTINUATION"))
        store.queue(rolling, {"embeds": [{}]}, True, "ACTIVE")
        last = store.last_alert(snap.day, snap.symbol, "ACTIVE")
        self.assertFalse(distinct_price_leg(replace(rolling, snapshot=replace(later, spot=later.spot + atr)), last))


if __name__ == "__main__":
    unittest.main()
