"""LOTTO_WEEKLY_FALLBACK_DTE. Flag 0 matches today's 0–1 DTE window."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from lotto.config import Settings
from lotto.discovery import Discovery
from lotto.features import contract_ok
from lotto.models import ET, Option, Snapshot
from lotto.schwab import Schwab
from lotto.weekly import chain_to_date, weekly_fallback_dte


def _option(at, dte, symbol):
    return Option(symbol, at.astimezone(ET).date() + timedelta(days=dte), "CALL",
                  100, 0.40, 0.45, 200, 100, 0.30, 0.05, at)


def _snap(symbol, at, dtes):
    options = tuple(_option(at, dte, f"{symbol}{dte}") for dte in dtes)
    return Snapshot(symbol, at, 100, at, 99, (), options, prior_atr=5)


class WeeklyFallbackTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 11, 11, 15, 0, tzinfo=ET)  # Wednesday

    def test_flag_zero_rejects_weeklies_and_keeps_the_chain_window(self):
        self.assertEqual(weekly_fallback_dte(), 0)
        day = self.at.astimezone(ET).date()
        self.assertEqual(chain_to_date(day, 1, 0), day + timedelta(days=1))
        weekly = _snap("ARM", self.at, (2, 3, 4))
        self.assertTrue(all(not contract_ok(option, weekly, Settings()) for option in weekly.options))
        with self.assertRaises(ValueError):
            Settings(max_dte=2)

    def test_flag_four_accepts_a_weekly_only_name_at_two_three_and_four_dte(self):
        weekly = _snap("ARM", self.at, (2, 3, 4))
        with patch.dict(os.environ, {"LOTTO_WEEKLY_FALLBACK_DTE": "4"}):
            self.assertEqual(weekly_fallback_dte(), 4)
            self.assertEqual(chain_to_date(self.at.date(), 1), self.at.date() + timedelta(days=4))
            self.assertTrue(all(contract_ok(option, weekly, Settings()) for option in weekly.options))

    def test_daily_expiry_name_stays_at_zero_or_one_dte(self):
        at = self.at
        spy = _snap("SPY", at, (0, 4))
        with patch.dict(os.environ, {"LOTTO_WEEKLY_FALLBACK_DTE": "4"}):
            near, far = spy.options
            self.assertTrue(contract_ok(near, spy, Settings()))
            self.assertFalse(contract_ok(far, spy, Settings()))

    def test_values_above_seven_are_capped_and_do_not_add_chain_calls(self):
        day = self.at.date()
        with patch.dict(os.environ, {"LOTTO_WEEKLY_FALLBACK_DTE": "9"}):
            self.assertEqual(weekly_fallback_dte(), 7)
            self.assertEqual(chain_to_date(day, 1), day + timedelta(days=7))
        off = self._chain_calls("0")
        on = self._chain_calls("4")
        self.assertEqual(len(off), len(on))
        self.assertTrue(off and on)
        self.assertEqual(on[0]["toDate"], (datetime.now(timezone.utc).astimezone(ET).date() + timedelta(days=4)).isoformat())
        self.assertEqual(off[0]["toDate"], (datetime.now(timezone.utc).astimezone(ET).date() + timedelta(days=1)).isoformat())

    def _chain_calls(self, flag):
        now = datetime.now(timezone.utc)
        client = Schwab.__new__(Schwab)
        client.discovery = Discovery(4, always_deep=set())
        client.atrs = {}
        client.baselines = {}
        client.levels = {}
        client.coverage = Mock()
        client.coverage.due.return_value = ["ARM"]
        client.coverage.priorities.return_value = {}
        client.coverage.interval = timedelta(minutes=3)
        client.coverage.rows = []
        client.coverage.count_fresh.return_value = 0
        client.session = Mock(return_value=(now - timedelta(hours=1), now + timedelta(hours=1)))
        client.bars = Mock(return_value=())
        calls = []

        def get(path, params):
            calls.append((path, params))
            if path == "/quotes":
                return {symbol: {"realtime": True, "quote": {
                    "lastPrice": 100, "openPrice": 99, "closePrice": 98, "totalVolume": 1000,
                    "tradeTime": int(now.timestamp() * 1000)}}
                    for symbol in params["symbols"].split(",")}
            return {"isDelayed": False, "callExpDateMap": {}, "putExpDateMap": {}}

        client.get = get
        with patch.dict(os.environ, {"LOTTO_WEEKLY_FALLBACK_DTE": flag, "LOTTO_ANDRE_WEIGHTS_MODE": "0"}):
            client.poll(["ARM"], {}, 1)
        return [params for path, params in calls if path == "/chains"]
