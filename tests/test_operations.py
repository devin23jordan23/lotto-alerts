import unittest
from datetime import datetime, timedelta

from lotto.models import ET
from lotto.operations import HealthMonitor
from lotto.store import Store


class HealthMonitorTests(unittest.TestCase):
    def test_sustained_failure_notifies_once_and_recovery_requires_data(self):
        store = Store(":memory:")
        sent = []
        now = datetime(2026, 9, 29, 11, 38, tzinfo=ET)
        monitor = HealthMonitor(store, "https://discord.com/api/webhooks/test/test",
                                sender=lambda _url, payload: sent.append(payload))
        for minute in range(3):
            monitor.failed_cycle(now + timedelta(minutes=minute), "Schwab token broker unavailable")
        self.assertEqual(len(sent), 1)
        self.assertIn("data unavailable", sent[0]["embeds"][0]["title"])
        # A worker restart must not resend the same outage notice.
        restarted = HealthMonitor(store, monitor.webhook, sender=monitor.sender)
        for minute in range(3, 6):
            restarted.failed_cycle(now + timedelta(minutes=minute), "Schwab token broker unavailable")
        self.assertEqual(len(sent), 1)
        restarted.successful_cycle(now + timedelta(minutes=6), 0)
        self.assertEqual(len(sent), 1)
        restarted.successful_cycle(now + timedelta(minutes=7), 18)
        self.assertEqual(len(sent), 2)
        self.assertIn("data restored", sent[1]["embeds"][0]["title"])

    def test_after_hours_failures_do_not_notify(self):
        store = Store(":memory:")
        sent = []
        monitor = HealthMonitor(store, "https://discord.com/api/webhooks/test/test",
                                sender=lambda _url, payload: sent.append(payload))
        at = datetime(2026, 9, 29, 17, 0, tzinfo=ET)
        for minute in range(5):
            monitor.failed_cycle(at + timedelta(minutes=minute), "market-data cycle unavailable")
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
