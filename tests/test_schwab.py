import io
import json
import tempfile
import threading
import time
from collections import deque
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from lotto.models import ET, Snapshot
from lotto.features import Candidate
from lotto.discord import alert_payload
from lotto.schwab import Schwab, api_symbol, benchmark_for
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

    def test_spx_weekly_chain_is_opted_in_without_admitting_other_indexes(self):
        at = datetime(2026, 10, 8, 12, 30, tzinfo=ET)
        def contract(symbol):
            return {"symbol": symbol, "strikePrice": 7800, "bid": .85, "ask": .95,
                    "totalVolume": 500, "openInterest": 50, "isIndexOption": True,
                    "quoteTimeInLong": int(at.timestamp() * 1000)}
        chain = {"putExpDateMap": {"2026-10-08:0": {"7800.0": [
            contract("SPXW  261008P07800000"), contract("SPX   261008P07800000"),
            contract("OTHER 261008P07800000")
        ]}}}
        self.assertEqual(Schwab.parse_chain(chain), ())
        self.assertEqual([o.symbol for o in Schwab.parse_chain(chain, allow_spx=True)],
                         ["SPXW  261008P07800000"])
        option = Schwab.parse_chain(chain, allow_spx=True)[0]
        snapshot = Snapshot("SPX", at, 7800, at, 7795, (), (option,))
        message = alert_payload(Candidate(snapshot, option, 92, {}, {}, [], True), "ACTIVE")
        self.assertIn("SPXW 7800P", message["embeds"][0]["description"])
        self.assertIn("Index $7800.00", message["embeds"][0]["description"])
        self.assertEqual(api_symbol("SPX"), "$SPX")
        self.assertEqual(benchmark_for("SPX"), "SPY")

    def test_spx_bars_use_aligned_spy_volume_without_changing_index_prices(self):
        now = datetime(2026, 10, 8, 9, 33, 30, tzinfo=ET)
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        def candle(minute, price, volume):
            return {"datetime": int((start+timedelta(minutes=minute)).timestamp()*1000),
                    "open": price, "high": price+1, "low": price-1, "close": price+.5,
                    "volume": volume}
        def get_candles(symbol, *_args):
            if symbol == "SPX":
                return {"candles": [candle(i, 7800+i, 0) for i in range(3)]}
            if symbol == "SPY":
                return {"candles": [candle(i, 780+i, 100+i*10) for i in range(3)]}
            raise AssertionError(symbol)
        self.client.candles = Mock(side_effect=get_candles)
        bars = self.client.bars("SPX", now, with_baseline=False)
        self.assertEqual([b.close for b in bars], [7800.5, 7801.5, 7802.5])
        self.assertEqual([b.volume for b in bars], [100, 110, 120])

    def test_spx_quote_maps_from_provider_symbol_and_uses_spy_volume(self):
        now = datetime.now(timezone.utc)
        client = self.client
        client.discovery = Discovery(4, always_deep={"SPX"})
        client.atrs = {}
        client.session = Mock(return_value=(now-timedelta(hours=1), now+timedelta(hours=1)))
        client.bars = Mock(return_value=())
        calls = []
        def get(path, params):
            calls.append((path, params))
            if path == "/quotes":
                return {symbol: {"realtime": True, "quote": {
                    "lastPrice": 7800 if symbol == "$SPX" else 780,
                    "openPrice": 7790 if symbol == "$SPX" else 779,
                    "closePrice": 7795 if symbol == "$SPX" else 780,
                    "totalVolume": 0 if symbol == "$SPX" else 1_000_000,
                    "tradeTime": int(now.timestamp()*1000)}}
                    for symbol in params["symbols"].split(",")}
            return {"isDelayed": False}
        client.get = get
        frames = client.poll(["SPX", "SPY"], {}, 1)
        self.assertIn("$SPX", calls[0][1]["symbols"].split(","))
        self.assertTrue(any(path == "/chains" and params["symbol"] == "$SPX"
                            for path, params in calls))
        self.assertEqual(client.discovery.history["SPX"][-1]["volume"], 1_000_000)
        spx = next(frame for frame in frames if frame.symbol == "SPX")
        self.assertEqual((spx.spot, spx.volume_source), (7800, "SPY"))

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

    def test_rate_limit_spacing_recovers_after_successful_requests(self):
        client = self.client
        client.request_lock = threading.Lock()
        client.base_request_interval = .25
        client.min_request_interval = .25
        client.rate_limit_hits = 0
        client.success_streak = 0
        for _ in range(6):
            client._rate_limited('/chains')
        self.assertEqual(client.min_request_interval, 2.0)
        for _ in range(500):
            client._request_succeeded()
        self.assertEqual(client.min_request_interval, .25)
        self.assertEqual(client.rate_limit_hits, 6)

    def test_request_statistics_uses_a_rolling_minute(self):
        client = self.client
        client.request_lock = threading.Lock()
        client.request_starts = deque([time.monotonic()-65, time.monotonic()-5])
        client.min_request_interval = .5
        client.rate_limit_hits = 2
        self.assertEqual(client.request_statistics(), (1, .5, 2))

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
        self.assertEqual({params['symbol'] for path,params in calls if path=='/chains'},
                         {api_symbol(symbol) for symbol in symbols})
        self.assertEqual(client.coverage.count_fresh(symbols,datetime.now(timezone.utc)),106)
        self.assertEqual(len(client.discovery.observations),106)

    def test_sixty_deep_names_are_fetched_while_universe_quotes_remain_visible(self):
        now = datetime.now(timezone.utc)
        client = self.client
        client.discovery = Discovery(60)
        client.atrs = {}
        client.session = Mock(return_value=(now-timedelta(hours=1), now+timedelta(hours=1)))
        client.bars = Mock(return_value=())
        symbols = DEFAULT_UNIVERSE.split(',')
        def get(path, params):
            if path == '/quotes':
                return {s:{'realtime':True,'quote':{'lastPrice':101,'openPrice':100,'closePrice':100,
                           'totalVolume':100000,'tradeTime':now.timestamp()*1000,'highPrice':102,'lowPrice':99}}
                        for s in params['symbols'].split(',')}
            return {'isDelayed':False}
        client.get = get
        frames = client.poll(symbols, {}, 1)
        self.assertEqual(len(frames), 60)
        self.assertEqual(len(client.discovery.observations), 106)

    def test_empty_near_expiry_cannot_evict_continuous_name(self):
        now = datetime.now(timezone.utc)
        discovery = Discovery(4, always_deep={'A'})
        quotes = {s:{'price':100,'open':100,'previous':100,'volume':100,'at':now}
                  for s in 'ABCDE'}
        selected = discovery.update(quotes, now, set(quotes), unavailable={'A','B'})
        self.assertIn('A', selected)
        self.assertNotIn('B', selected)
        self.assertEqual(len(discovery.observations), 5)

    def test_greek_recovery_preserves_broad_chain_and_fresh_provider_quotes(self):
        now=datetime.now(timezone.utc)
        stamp=int(now.timestamp()*1000)
        def raw(symbol, delta, gamma, stamp=stamp):
            return dict(symbol=symbol,strikePrice=100,bid=.5,ask=.51,totalVolume=1000,
                        quoteTimeInLong=stamp,delta=delta,gamma=gamma)
        a=raw('A',-999,-999); b=raw('B',-999,-999)
        recovered=raw('A',.3,.05,stamp+1)
        broad={'callExpDateMap':{str(now.date())+':0':{'100':[a],'105':[b]}}}
        narrow={'callExpDateMap':{str(now.date())+':0':{'100':[recovered]}}}
        self.client.get=Mock(side_effect=[broad,narrow])
        result=self.client.option_chain('AMZN',now,1)
        parsed={o.symbol:o for o in Schwab.parse_chain(result)}
        self.assertEqual(set(parsed),{'A','B'})
        self.assertEqual((parsed['A'].delta,parsed['A'].gamma),(.3,.05))
        self.assertEqual(parsed['B'].delta,-999)
        self.assertEqual(a['delta'],-999)  # Do not mutate the saved provider response.
        self.assertEqual(self.client.get.call_args_list[1].args[1]['strikeCount'],20)
        self.client.get=Mock(side_effect=[broad,{**narrow,'isDelayed':True}])
        self.assertEqual(Schwab.parse_chain(self.client.option_chain('AMZN',now,1))[0].delta,-999)
        stale={'callExpDateMap':{str(now.date())+':0':{'100':[raw('A',.3,.05,stamp-1)]}}}
        self.client.get=Mock(side_effect=[broad,stale])
        self.assertEqual(Schwab.parse_chain(self.client.option_chain('AMZN',now,1))[0].delta,-999)

    def test_healthy_or_empty_chain_needs_no_greek_retry(self):
        now=datetime.now(timezone.utc)
        for data in ({'callExpDateMap':{}}, {'callExpDateMap':{str(now.date())+':0':{'100':[dict(
                symbol='A',strikePrice=100,bid=.5,ask=.51,totalVolume=1000,
                quoteTimeInLong=int(now.timestamp()*1000),delta=.3,gamma=.05)]}}}):
            self.client.get=Mock(return_value=data)
            self.assertIs(self.client.option_chain('AMZN',now,1),data)
            self.client.get.assert_called_once()

    def test_greek_retry_failure_preserves_original_quotes(self):
        now=datetime.now(timezone.utc)
        data={'callExpDateMap':{str(now.date())+':0':{'100':[dict(
            symbol='A',strikePrice=100,bid=.5,ask=.51,totalVolume=1000,
            quoteTimeInLong=int(now.timestamp()*1000),delta=-999,gamma=-999)]}}}
        self.client.get=Mock(side_effect=[data,RuntimeError('provider unavailable')])
        self.assertIs(self.client.option_chain('AMZN',now,1),data)
        self.assertEqual(self.client.get.call_count,2)
