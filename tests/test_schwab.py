import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from lotto.models import ET
from lotto.schwab import Schwab
from lotto.discovery import Discovery
from lotto.main import DEFAULT_UNIVERSE


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = Schwab.__new__(Schwab)
        self.client.baselines = {}
        self.client.calendar_cache = {}

    def test_chain_preserves_iv_and_greeks_for_future_feedback(self):
        at = datetime(2026, 10, 1, 13, 27, tzinfo=ET)
        raw = {"callExpDateMap": {"2026-10-01:0": {"280.0": [{
            "symbol": "IWM   261001C00280000", "strikePrice": 280,
            "bid": .44, "ask": .45, "totalVolume": 79946,
            "openInterest": 1200, "delta": .468, "gamma": .14,
            "volatility": 27.5, "theta": -.08, "vega": .02,
            "quoteTimeInLong": int(at.timestamp() * 1000),
        }]}}}
        option = Schwab.parse_chain(raw)[0]
        self.assertEqual((option.implied_volatility, option.theta, option.vega),
                         (27.5, -.08, .02))

    def test_broker_mode_needs_no_local_refresh_credentials(self):
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(json.dumps({
            "access_token": "broker-token",
            "expires_in": 240,
        }).encode()))
        response.__exit__ = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "SCHWAB_TOKEN_BROKER_URL": "https://broker.test/schwab-token",
            "SCHWAB_TOKEN_BROKER_KEY": "shared-secret",
        }, clear=True), patch("lotto.schwab.urlopen", return_value=response) as open_url:
            client = Schwab(directory)
            self.assertEqual(client.token(), "broker-token")
            self.assertEqual(client.token(), "broker-token")
            self.assertEqual(open_url.call_count, 1)

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
        self.assertEqual(self.client.levels[("TEST",now.date())]["premarket_high"],102)
        self.assertEqual(self.client.candles.call_args.args[-1],True)

    def test_premarket_levels_cannot_include_regular_or_future_extremes(self):
        now=datetime(2026,9,21,9,32,30,tzinfo=ET)
        candles=[]
        for minute,high in ((29,101),(30,110),(31,111),(32,120),(33,130)):
            candles.append({"datetime":now.replace(minute=minute,second=0).timestamp()*1000,
                            "open":100,"high":high,"low":99,"close":100,"volume":100})
        self.client.candles=Mock(return_value={"candles":candles})
        bars=self.client.bars("TEST",now,with_baseline=False)
        self.assertEqual(self.client.levels[("TEST",now.date())]["premarket_high"],101)
        self.assertEqual(max(b.high for b in bars),111)

    def test_calendar_closed_and_early_close(self):
        now = datetime(2026, 11, 27, 12, 0, tzinfo=ET)
        self.client.get = Mock(return_value={"equity": {"EQ": {"isOpen": False}}})
        self.assertIsNone(self.client.session(now))
        self.client.calendar_cache.clear()
        self.client.get.return_value = {"equity": {"EQ": {"isOpen": True, "sessionHours": {
            "regularMarket": [{"start": "2026-11-27T09:30:00-05:00", "end": "2026-11-27T13:00:00-05:00"}]}}}}
        self.assertEqual(self.client.session(now)[1].hour, 13)

    def test_full_universe_chain_sweep_reaches_every_name(self):
        now=datetime.now(timezone.utc)
        client=self.client
        client.discovery=Discovery(18)
        client.atrs={}
        client.session=Mock(return_value=(now-timedelta(hours=1),now+timedelta(hours=1)))
        client.bars=Mock(return_value=())
        symbols=DEFAULT_UNIVERSE.split(',')
        calls=[]
        def get(path,params):
            calls.append((path,params))
            if path=='/quotes':
                return {s:{'realtime':True,'quote':{'lastPrice':101,'openPrice':100,'closePrice':100,
                           'totalVolume':100000,'tradeTime':now.timestamp()*1000,'highPrice':102,'lowPrice':99}}
                        for s in params['symbols'].split(',')}
            return {'isDelayed':False}
        client.get=get
        frame=client.poll(symbols,{},7)
        self.assertEqual(len(frame),18)
        self.assertEqual(sum(path=='/quotes' for path,_ in calls),1)
        self.assertEqual(sum(path=='/chains' for path,_ in calls),44)
        for _ in range(3):
            client.poll(symbols,{},7)
        self.assertEqual({params['symbol'] for path,params in calls if path=='/chains'},set(symbols))
        self.assertEqual(client.coverage.count_fresh(symbols,datetime.now(timezone.utc)),104)
        self.assertEqual(len(client.discovery.observations),104)
