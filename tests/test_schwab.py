import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock

from lotto.models import ET
from lotto.schwab import Schwab


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = Schwab.__new__(Schwab)
        self.client.baselines = {}
        self.client.calendar_cache = {}

    def test_baseline_excludes_today_and_uses_same_elapsed_time(self):
        today = datetime(2026, 9, 21, 10, 0, tzinfo=ET)
        candles = []
        for days in range(11):
            start = (today-timedelta(days=days)).replace(hour=9, minute=30)
            for minute in range(2):
                candles.append({"datetime": (start+timedelta(minutes=minute)).timestamp()*1000,
                                "volume": 1_000_000 if days == 0 else 100})
        self.client.candles = Mock(return_value={"candles": candles})
        self.assertEqual(self.client.baseline("TEST", today), {1: 100, 2: 200})

    def test_incomplete_baseline_cannot_be_treated_as_zero_volume(self):
        today = datetime(2026, 9, 21, 10, 0, tzinfo=ET)
        candles = [{"datetime": (today-timedelta(days=i)).replace(hour=9, minute=31).timestamp()*1000, "volume": 100}
                   for i in range(1, 21)]
        self.client.candles = Mock(return_value={"candles": candles})
        self.assertEqual(self.client.baseline("TEST", today), {})

    def test_current_unfinished_minute_is_excluded(self):
        now = datetime(2026, 9, 21, 9, 32, 30, tzinfo=ET)
        candles = [{"datetime": now.replace(minute=minute, second=0).timestamp()*1000,
                    "open": 100, "high": 102, "low": 99, "close": 101, "volume": 500}
                   for minute in (29, 30, 31, 32, 33)]
        self.client.candles = Mock(return_value={"candles": candles})
        bars = self.client.bars("TEST", now, with_baseline=False)
        self.assertEqual([b.end.minute for b in bars], [31, 32])

    def test_calendar_closed_and_early_close(self):
        now = datetime(2026, 11, 27, 12, 0, tzinfo=ET)
        self.client.get = Mock(return_value={"equity": {"EQ": {"isOpen": False}}})
        self.assertIsNone(self.client.session(now))
        self.client.calendar_cache.clear()
        self.client.get.return_value = {"equity": {"EQ": {"isOpen": True, "sessionHours": {
            "regularMarket": [{"start": "2026-11-27T09:30:00-05:00", "end": "2026-11-27T13:00:00-05:00"}]}}}}
        self.assertEqual(self.client.session(now)[1].hour, 13)
