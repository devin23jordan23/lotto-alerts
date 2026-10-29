import unittest
import os
from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch

from lotto.config import model_v_settings
from lotto.engine import Engine
from lotto.main import env_settings
from lotto.features import Candidate
from lotto.model_v import pick_stock_event_contract
from lotto.models import Bar, ET, Option, Snapshot
from lotto.patterns import Setup
from lotto.stock_first import StockIdea
from lotto.stock_thesis import stock_motion_gate, stock_thesis
from lotto.store import Store


class StockThesisTests(unittest.TestCase):
    def test_live_default_and_rollback_switch(self):
        with patch.dict(os.environ, {"LOTTO_MODEL": "V"}, clear=True):
            self.assertEqual(env_settings().stock_thesis_enabled, 1)
        with patch.dict(os.environ, {"LOTTO_MODEL": "V",
                                      "LOTTO_STOCK_THESIS_ENABLED": "0"}, clear=True):
            self.assertEqual(env_settings().stock_thesis_enabled, 0)

    def snapshot(self, prices, volumes, symbol="ORCL", expiry=date(2026, 10, 30)):
        start = datetime(2026, 10, 28, 9, 30, tzinfo=ET)
        bars = []
        for i, price in enumerate(prices):
            prior = prices[i - 1] if i else price
            bars.append(Bar(start + timedelta(minutes=i + 1), prior,
                            max(price, prior) + .02, min(price, prior) - .02,
                            price, volumes[i]))
        at = bars[-1].end + timedelta(seconds=15)
        option = Option(f"{symbol}  261030C00133000", expiry, "CALL", 133, 1.4, 1.6,
                        10, 0, None, None, at - timedelta(seconds=2))
        return Snapshot(symbol, at, prices[-1], at - timedelta(seconds=4), prices[0],
                        tuple(bars), (option,), prior_atr=6.37)

    def test_friday_only_contract_is_eligible_and_atm(self):
        snap = self.snapshot([131.32] * 7 + [131.6, 132.0, 133.0],
                             [10000] * 9 + [100000])
        self.assertEqual((snap.options[0].expiry - snap.at.date()).days, 2)
        chosen = pick_stock_event_contract(snap, "CALL")
        self.assertEqual(chosen.symbol, snap.options[0].symbol)
        evidence, reason = stock_thesis(snap, "CALL")
        self.assertEqual(reason, "")
        self.assertEqual(evidence["setup"], "HOD_BREAK")
        self.assertGreater(evidence["stock_volume_ratio"], 1.5)
        far = replace(snap.options[0], strike=120)
        self.assertIsNone(pick_stock_event_contract(replace(snap, options=(far,)), "CALL"))
        old = replace(snap.options[0], quote_time=snap.at - timedelta(minutes=2))
        self.assertIsNone(pick_stock_event_contract(replace(snap, options=(old,)), "CALL"))

    def test_flat_ema_touch_and_opposing_move_have_no_thesis(self):
        snap = self.snapshot([131.32 + .02 * (i % 2) for i in range(10)],
                             [10000] * 9 + [100000])
        self.assertEqual(stock_thesis(snap, "CALL")[0], {})
        running = self.snapshot([131.32] * 7 + [131.6, 132.0, 133.0],
                                [10000] * 9 + [100000])
        self.assertEqual(stock_thesis(running, "PUT")[0], {})
        self.assertFalse(stock_motion_gate(snap, "CALL")[0])
        self.assertFalse(stock_motion_gate(running, "PUT")[0])

    def test_friday_stock_event_posts_an_option_card(self):
        snap = self.snapshot([131.32] * 7 + [131.6, 132.0, 133.0],
                             [10000] * 9 + [100000])
        store = Store(":memory:")
        self.addCleanup(store.db.close)
        engine = Engine(store, model_v_settings(stock_thesis_enabled=1,
                                                fallback_rule="off"), dry_run=True)
        idea = StockIdea("ORCL", snap.day, snap.at - timedelta(seconds=10), "CALL",
                         snap.spot, 132.8, 131.5, "INTRADAY_BREAK", "A", {})
        engine.remember_stock_events([idea])
        with patch("lotto.engine.candidates", return_value=[]):
            cards = engine.process([snap])
        self.assertEqual(len(cards), 1)
        description = cards[0]["payload"]["embeds"][0]["description"]
        self.assertIn("2DTE", description)
        self.assertIn("Stock thesis", description)
        self.assertIn("133C", description)

    def test_high_option_score_does_not_rescue_flat_or_unanchored_stock(self):
        def pretend_high_score(candidate, *_args, **_kwargs):
            candidate.metrics.update({"score_v2": 130, "card_score": 130,
                                      "gate_post": 1, "path": "hybrid"})
            candidate.qualifying = True
            return candidate

        cases = [
            (self.snapshot([131.32 + .02 * (i % 2) for i in range(10)],
                           [10000] * 9 + [100000]),
             Setup("PULLBACK_RESUMPTION", 131.32, 131.0)),
            (self.snapshot([131.32] * 7 + [131.6, 132.0, 133.0],
                           [10000] * 9 + [100000]), None),
            (self.snapshot([131.32] * 7 + [131.6, 132.0, 133.0],
                           [10000] * 9 + [100000]),
             Setup("REVERSAL", 132.8, 131.5)),
        ]
        for snap, setup in cases:
            with self.subTest(setup=setup):
                store = Store(":memory:")
                engine = Engine(store, model_v_settings(stock_thesis_enabled=1,
                                                        fallback_rule="off"), dry_run=True)
                candidate = Candidate(snap, snap.options[0], 130, {}, {}, [], True, setup=setup)
                with patch("lotto.engine.candidates", return_value=[candidate]), \
                     patch("lotto.model_v.apply_v2", side_effect=pretend_high_score):
                    self.assertEqual(engine.process([snap]), [])
                store.db.close()


if __name__ == "__main__":
    unittest.main()
