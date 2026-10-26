import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from lotto.discord import deliver_stock_pending
from lotto.discovery import Discovery
from lotto.models import ET
from lotto.stock_first import StockFirst
from lotto.store import Store


class StockFirstTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.start = datetime(2026, 10, 23, 9, 30, tzinfo=ET)

    def tearDown(self):
        self.store.db.close()

    def row(self, minute, price, symbol="PANW", opening=370.0, high=None, low=None):
        at = self.start + timedelta(minutes=minute)
        return {"symbol": symbol, "observed_at": at, "at": at-timedelta(seconds=7),
                "price": price, "open": opening, "previous": opening,
                "high": high if high is not None else price,
                "low": low if low is not None else min(price, opening),
                "volume": 100000+minute*1000, "promoted": False}

    def test_optionless_unpromoted_runner_sends_once(self):
        scanner = StockFirst(self.store, enabled=True, mode="send")
        prices = [370, 370.1, 370.2, 370.4, 370.8, 371.8, 372.7, 373.6]
        ideas = [idea for minute, price in enumerate(prices)
                 for idea in scanner.process([self.row(minute, price)], dry_run=False)]
        self.assertEqual(len(ideas), 1)
        self.assertEqual(ideas[0].symbol, "PANW")
        self.assertEqual(ideas[0].side, "CALL")
        row = self.store.db.execute("SELECT * FROM stock_ideas").fetchone()
        self.assertEqual(row["delivery"], "internal")
        self.assertIn("No option contract or entry price validated", row["payload"])
        with patch("lotto.discord.urlopen") as send:
            deliver_stock_pending(self.store, "https://discord.com/api/webhooks/test/test")
        send.assert_not_called()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)

    def test_flat_stock_and_stale_quote_cannot_fire(self):
        scanner = StockFirst(self.store, enabled=True, mode="send")
        for minute in range(12):
            scanner.process([self.row(minute, 370+(minute%2)*.02)], dry_run=False)
        stale = self.row(12, 375)
        stale["at"] -= timedelta(minutes=3)
        self.assertEqual(scanner.process([stale], dry_run=False), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM stock_ideas").fetchone()[0], 0)

    def test_shadow_is_retained_but_not_delivered(self):
        scanner = StockFirst(self.store, enabled=True, mode="shadow")
        for minute, price in enumerate([370, 370.1, 370.2, 370.4, 370.8, 371.8, 372.7]):
            scanner.process([self.row(minute, price)], dry_run=False)
        row = self.store.db.execute("SELECT delivery FROM stock_ideas").fetchone()
        self.assertEqual(row["delivery"], "internal")
        with patch("lotto.discord.urlopen") as send:
            deliver_stock_pending(self.store, "https://discord.com/api/webhooks/test/test")
        send.assert_not_called()

    def test_put_break_and_held_alert_delivery(self):
        scanner = StockFirst(self.store, enabled=True, mode="send")
        for minute, price in enumerate([370, 369.9, 369.8, 369.6, 369.2, 368.2, 367.3]):
            scanner.process([self.row(minute, price, high=370, low=price)], dry_run=False)
        idea = self.store.db.execute("SELECT * FROM stock_ideas").fetchone()
        self.assertEqual(idea["side"], "PUT")
        payload = json.loads(idea["payload"])
        self.assertIn("PANW PUTS WATCH", payload["embeds"][0]["title"])

    def test_atr_scaled_motion_rejects_small_range_pop(self):
        scanner = StockFirst(self.store, enabled=True, mode="send")
        for minute, price in enumerate([370, 370.1, 370.2, 370.3, 370.35, 370.4, 370.45]):
            row = self.row(minute, price)
            row["prior_atr"] = 10.0
            scanner.process([row], dry_run=False)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM stock_ideas").fetchone()[0], 0)

    def test_a_wider_atr_drops_the_handoff_trigger(self):
        # Same prices clear 0.25 ATR at 10 and miss it at 20. The live quote's
        # prior_atr is whichever window LOTTO_ATR_DAYS selected.
        scanner = StockFirst(self.store, enabled=True, mode="send")
        ideas = []
        for minute, price in enumerate([370, 370.1, 370.2, 370.4, 370.8, 371.8, 372.7, 373.6]):
            row = self.row(minute, price)
            row["prior_atr"] = 20.0
            ideas.extend(scanner.process([row], dry_run=False))
        self.assertEqual(ideas, [])

    def test_atr_scaled_opening_runner_alerts(self):
        scanner = StockFirst(self.store, enabled=True, mode="send")
        ideas = []
        for minute, price in enumerate([370, 370.1, 370.2, 370.4, 370.8, 371.8, 372.7, 373.6]):
            row = self.row(minute, price)
            row["prior_atr"] = 10.0
            ideas.extend(scanner.process([row], dry_run=False))
        self.assertEqual(len(ideas), 1)
        self.assertEqual(ideas[0].setup, "OPENING_DRIVE")

    def test_missing_stock_volume_does_not_hide_price_observation(self):
        now = self.start + timedelta(minutes=6)
        discovery = Discovery(4)
        selected = discovery.update({"PANW": {"price": 374, "open": 370,
                            "previous": 369, "high": 374, "low": 370,
                            "volume": None, "at": now}}, now, {"PANW"})
        self.assertIn("PANW", selected)
        self.assertEqual(discovery.observations[0]["volume_unknown"], 1)


if __name__ == "__main__":
    unittest.main()
