"""Read-only Schwab REST adapter. No brokerage/order endpoints."""
import base64
import json
import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from collections import defaultdict, deque
from dataclasses import replace
from datetime import date, datetime, time as wall_time, timedelta, timezone
from math import isfinite
from pathlib import Path
from statistics import median
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Bar, ET, Option, Snapshot
from .discovery import Discovery
from .coverage import ChainCoverage

LOG = logging.getLogger(__name__)
BASE = "https://api.schwabapi.com/marketdata/v1"
SECTORS = {**dict.fromkeys(("TSM", "NVDA", "AMD", "ARM", "INTC", "MU", "AVGO", "SMCI", "QCOM", "MRVL", "AMAT", "LRCX", "KLAC", "ASML", "SNDK", "SKHY"), "SMH"),
           **dict.fromkeys(("COIN", "HOOD", "MSTR"), "IBIT"), "MRNA": "XBI"}


def _market_day_context(bars, spot, opening):
    """Benchmark return from the open and which side of session VWAP it is on."""
    if bars and spot and bars[0].open:
        price = spot
        day_open = bars[0].open
        volume = 0.0
        total = 0.0
        for bar in bars:
            typical = (bar.high + bar.low + bar.close) / 3
            volume += bar.volume
            total += typical * bar.volume
        vwap = total / volume if volume else bars[-1].close
        side = 1 if price > vwap else -1 if price < vwap else 0
        return price / day_open - 1, side
    if spot and opening:
        return spot / opening - 1, None
    return None, None


def benchmark_for(symbol):
    return "SPY" if symbol in {"SPX", "QQQ", "SMH", "SOXX", "XLK"} else SECTORS.get(symbol, "QQQ")


def api_symbol(symbol):
    return "$SPX" if symbol == "SPX" else symbol


def number(value):
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def epoch(value) -> datetime | None:
    value = number(value)
    if value is None or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


class Schwab:
    def __init__(self, data_dir: str):
        self.client_id = os.getenv("SCHWAB_CLIENT_ID", "")
        self.secret = os.getenv("SCHWAB_CLIENT_SECRET", "")
        self.seed = os.getenv("SCHWAB_REFRESH_TOKEN", "")
        self.broker_url = os.getenv("SCHWAB_TOKEN_BROKER_URL", "").strip()
        self.broker_key = os.getenv("SCHWAB_TOKEN_BROKER_KEY", "")
        self.broker_token = ""
        self.broker_deadline = 0.0
        self.token_path = Path(data_dir) / "schwab_tokens.json"
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        if bool(self.broker_url) != bool(self.broker_key):
            raise ValueError("Schwab token broker URL and key must be set together")
        if not self.broker_url and (not self.client_id or not self.secret or not (self.seed or self.token_path.exists())):
            raise ValueError("Schwab client ID, secret, and refresh token are required")
        self.last_request = 0
        self.request_lock = threading.Lock()
        self.base_request_interval = float(os.getenv("LOTTO_API_MIN_INTERVAL_SECONDS", "0.25"))
        self.min_request_interval = self.base_request_interval
        if not 0.2 <= self.min_request_interval <= 2:
            raise ValueError("LOTTO_API_MIN_INTERVAL_SECONDS must be between 0.2 and 2")
        self.request_starts = deque()
        self.rate_limit_hits = 0
        self.success_streak = 0
        self.baselines = {}
        self.calendar_cache = {}
        self.atrs = {}
        self.levels = {}
        self.bar_cache = {}
        self.quote_cycle = 0
        self.standby_chain_at = {}
        self.discovery = Discovery(int(os.getenv("LOTTO_CHAIN_CAPACITY", "60")),
            always_deep={s.strip().upper() for s in os.getenv(
                "LOTTO_CONTINUOUS_SYMBOLS",
                "SPY,QQQ,IWM,META,NVDA,AMD,TSLA,AAPL,MSFT,AMZN,GOOGL,AVGO,MU,MRNA,SMCI,COIN,PLTR,NFLX"
            ).split(",") if s.strip()} | {"SPX"})
        self.coverage = ChainCoverage(int(os.getenv("LOTTO_SWEEP_MINUTES", "3")))

    def token(self, force=False):
        if self.broker_url:
            if force:
                self.broker_token = ""
                self.broker_deadline = 0.0
            return self.token_from_broker()
        tokens = json.loads(self.token_path.read_text()) if self.token_path.exists() else {"refresh_token": self.seed}
        if not force and tokens.get("access_token") and time.time() < tokens.get("expires_at", 0) - 120:
            return tokens["access_token"]
        basic = base64.b64encode(f"{self.client_id}:{self.secret}".encode()).decode()
        request = Request("https://api.schwabapi.com/v1/oauth/token",
                          data=urlencode({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}).encode(),
                          headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen(request, timeout=15) as response:
                fresh = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Schwab token refresh rejected (HTTP {exc.code}); renew authorization if expired") from None
        fresh["refresh_token"] = fresh.get("refresh_token", tokens["refresh_token"])
        fresh["expires_at"] = time.time() + fresh.get("expires_in", 1800)
        temporary = self.token_path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(fresh, stream)
        temporary.replace(self.token_path)
        return fresh["access_token"]

    def token_from_broker(self):
        if self.broker_token and time.monotonic() < self.broker_deadline:
            return self.broker_token
        request = Request(self.broker_url, headers={
            "Authorization": f"Bearer {self.broker_key}",
            "Accept": "application/json",
        })
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Schwab token broker rejected request (HTTP {exc.code})") from None
        token = payload.get("access_token", "")
        if not token:
            raise RuntimeError("Schwab token broker returned no access token")
        ttl = max(30, min(int(payload.get("expires_in", 240)), 240) - 30)
        self.broker_token = token
        self.broker_deadline = time.monotonic() + ttl
        return token

    def get(self, path: str, params: dict, attempts: int = 3, timeout: int = 15) -> dict:
        retry_auth = False
        for attempt in range(attempts):
            with self.request_lock:
                access_token = self.token(force=retry_auth)
                retry_auth = False
                time.sleep(max(0, self.min_request_interval - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
                self.request_starts.append(self.last_request)
                self._prune_request_starts(self.last_request)
            request = Request(f"{BASE}{path}?{urlencode(params)}", headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
            try:
                with urlopen(request, timeout=timeout) as response:
                    payload = json.load(response)
                self._request_succeeded()
                return payload
            except HTTPError as exc:
                if exc.code != 429:
                    with self.request_lock:
                        self.success_streak = 0
                if exc.code in (401, 429) and attempt < attempts-1:
                    retry_auth = exc.code == 401
                    if exc.code == 429:
                        self._rate_limited(path)
                        time.sleep(min(5, 1 + attempt * 2))
                    continue
                raise RuntimeError(f"Schwab {path} failed (HTTP {exc.code})") from None
        raise RuntimeError("Schwab authorization retry failed")

    def send_quote(self, contract_symbol: str, underlying: str) -> dict | None:
        """One bounded market-data request; never a brokerage/order endpoint."""
        data = self.get("/quotes", {"symbols": f"{contract_symbol},{api_symbol(underlying)}"},
                        attempts=1, timeout=3)
        item = data.get(contract_symbol, {})
        base = data.get(api_symbol(underlying), data.get(underlying, {}))
        option_q, stock_q = item.get("quote", {}), base.get("quote", {})
        bid, ask = number(option_q.get("bidPrice")), number(option_q.get("askPrice"))
        quote_time = epoch(option_q.get("quoteTime"))
        spot = number(stock_q.get("lastPrice"))
        spot_time = epoch(stock_q.get("tradeTime") or stock_q.get("quoteTime"))
        if (item.get("realtime") is False or base.get("realtime") is False
                or bid is None or ask is None or not 0 < bid <= ask
                or quote_time is None or spot is None or spot_time is None):
            return None
        return {"bid": bid, "ask": ask, "quote_time": quote_time,
                "spot": spot, "spot_time": spot_time, "source": "Schwab /quotes", "realtime": True}

    def _prune_request_starts(self, now):
        while self.request_starts and now-self.request_starts[0] >= 60:
            self.request_starts.popleft()

    def _request_succeeded(self):
        with self.request_lock:
            self.success_streak += 1
            if self.success_streak >= 50 and self.min_request_interval > self.base_request_interval:
                self.min_request_interval = max(self.base_request_interval,
                                                round(self.min_request_interval * .8, 3))
                self.success_streak = 0
                LOG.info("Schwab request spacing recovered to %.3fs", self.min_request_interval)

    def _rate_limited(self, path):
        with self.request_lock:
            self.rate_limit_hits += 1
            self.success_streak = 0
            self.min_request_interval = min(2.0, max(0.5, self.min_request_interval * 2))
            spacing = self.min_request_interval
        LOG.warning("Schwab rate limit on %s; request spacing increased to %.2fs", path, spacing)

    def request_statistics(self):
        if not hasattr(self, "request_lock"):
            return 0, 0.0, 0
        with self.request_lock:
            self._prune_request_starts(time.monotonic())
            return len(self.request_starts), self.min_request_interval, self.rate_limit_hits

    def session(self, now: datetime) -> tuple[datetime, datetime] | None:
        day = now.astimezone(ET).date().isoformat()
        if day not in self.calendar_cache:
            data = self.get("/markets/equity", {"date": day})
            sessions = []
            def walk(value):
                if not isinstance(value, dict):
                    return
                if value.get("isOpen") is True:
                    sessions.extend(value.get("sessionHours", {}).get("regularMarket", []))
                for child in value.values():
                    walk(child)
            walk(data)
            self.calendar_cache[day] = next(((datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"]))
                                             for s in sessions if "start" in s and "end" in s), None)
        return self.calendar_cache[day]

    def candles(self, symbol: str, start: datetime, end: datetime, extended=False) -> dict:
        return self.get("/pricehistory", {"symbol": api_symbol(symbol), "periodType": "day", "frequencyType": "minute", "frequency": 1,
                                        "startDate": int(start.timestamp() * 1000), "endDate": int(end.timestamp() * 1000),
                                        "needExtendedHoursData": "true" if extended else "false", "needPreviousClose": "true"})

    def baseline(self, symbol: str, now: datetime):
        day = now.astimezone(ET).date()
        key = (symbol, day)
        if key in self.baselines:
            return self.baselines[key]
        data = self.candles(symbol, now - timedelta(days=45), now)
        daily = defaultdict(list)
        for raw in data.get("candles", []):
            at = epoch(raw.get("datetime"))
            if at and at.astimezone(ET).date() < day and wall_time(9,30) <= at.astimezone(ET).time() < wall_time(16):
                if all(number(raw.get(k)) is not None for k in ("high", "low", "close")):
                    daily[at.astimezone(ET).date()].append(raw)
        ranges, previous = [], None
        for d in sorted(daily):
            rows = sorted(daily[d], key=lambda r:r["datetime"])
            high, low = max(r["high"] for r in rows), min(r["low"] for r in rows)
            if previous:
                ranges.append(max(high-low, abs(high-previous), abs(low-previous)))
            previous = rows[-1]["close"]
        if not hasattr(self, "atrs"):
            self.atrs = {}
        # Both windows stay on the client. prior_atr_for picks one for prior_atr.
        # The fallback keeps atrs21 even when the live window is 14.
        self.atrs[key] = sum(ranges[-14:])/14 if len(ranges) >= 14 else None
        if not hasattr(self, "atrs21"):
            self.atrs21 = {}
        from .model_v import atr21_value
        self.atrs21[key] = atr21_value(ranges)
        if not hasattr(self,"levels"):
            self.levels = {}
        if daily:
            prior = daily[max(daily)]
            self.levels.setdefault(key,{}).update(prior_high=max(r["high"] for r in prior),
                                                  prior_low=min(r["low"] for r in prior))
        profiles = defaultdict(dict)
        for raw in data.get("candles", []):
            at = epoch(raw.get("datetime"))
            if not at:
                continue
            local = at.astimezone(ET)
            volume = number(raw.get("volume"))
            if local.date() < day and wall_time(9, 30) <= local.time() < wall_time(16) and volume is not None and volume >= 0:
                minute = (local.hour * 60 + local.minute) - 570
                profiles[local.date()][minute] = volume
        observations = defaultdict(list)
        for historic_day in sorted(profiles)[-20:]:
            minutes = profiles[historic_day]
            if 0 not in minutes:
                continue
            # Preserve the observed prefix and bridge at most two isolated
            # missing minutes. Three missing minutes end that day's profile.
            total = 0
            missing = 0
            for minute in range(390):
                if minute not in minutes:
                    missing += 1
                    if missing > 2:
                        break
                else:
                    missing = 0
                total += minutes.get(minute, 0)
                if minute in minutes:
                    observations[minute + 1].append(total)
        full_days = []
        for historic_day in sorted(profiles):
            minutes = profiles[historic_day]
            if sum(1 for minute in minutes if 0 <= minute < 390) < 380:
                continue
            total = 0.0
            curve = {}
            for minute in range(390):
                total += minutes.get(minute) or 0
                curve[minute] = total
            full_days.append(curve)
        handoff_base = {}
        if len(full_days) >= 10:
            used = full_days[-10:]
            for minute in range(390):
                handoff_base[minute] = sum(curve[minute] for curve in used) / 10
        if not hasattr(self, "handoff_rvol"):
            self.handoff_rvol = {}
        self.handoff_rvol[key] = {"days": len(full_days), "base": handoff_base}
        # Fallback RVOL is the same 10-full-day curve. Partial sessions do not build a second one.
        if not hasattr(self, "fallback_rvol"):
            self.fallback_rvol = {}
        self.fallback_rvol[key] = {"sessions": len(full_days), "base": handoff_base}
        baseline = {minute: median(values) for minute, values in observations.items() if len(values) >= 10}
        if symbol == "SPX":
            # Schwab index candles have zero volume; use traded SPY volume only
            # for volume-based signals while retaining SPX prices and ATR.
            baseline = self.baseline("SPY", now)
            spy_rvol = self.handoff_rvol.get(("SPY", day))
            if spy_rvol:
                self.handoff_rvol[key] = spy_rvol
            spy_fallback = getattr(self, "fallback_rvol", {}).get(("SPY", day))
            if spy_fallback is not None:
                self.fallback_rvol[key] = spy_fallback
        if not baseline:
            LOG.warning("%s has fewer than ten usable historical sessions; no alerts until baseline is available", symbol)
        self.baselines[key] = baseline
        return baseline

    def prior_atr_for(self, symbol: str, day, days=None):
        """Value written to prior_atr. 21 by default; 14 restores the old average.

        ATR21 is the mean of the prior daily true ranges, today already excluded,
        and it needs at least 15 ranges. Fewer than that returns None. The 14-day
        average is not substituted.
        """
        if days is None:
            raw = os.getenv("LOTTO_ATR_DAYS", "21").strip()
            days = 14 if raw == "14" else 21
        key = (symbol, day)
        if int(days) == 14:
            return self.atrs.get(key) if hasattr(self, "atrs") else None
        return getattr(self, "atrs21", {}).get(key)

    def bars(self, symbol: str, now: datetime, with_baseline=True) -> tuple[Bar, ...]:
        opening = now.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
        key = (symbol, opening.date())
        if not hasattr(self, "bar_cache"):
            self.bar_cache = {}
        cached = self.bar_cache.get(key, {})
        # Re-fetch only a short overlap after the first full load. A full
        # 4 a.m.-to-now request for every deep symbol every minute caused
        # request pressure and stale-bar vetoes on October 23.
        start = max(opening, max(cached) - timedelta(minutes=3)) if cached else opening.replace(hour=4)
        raw = self.candles(symbol, start, now, True)
        baseline = self.baseline(symbol, now) if with_baseline else {}
        proxy_volumes = None
        if symbol == "SPX":
            proxy = self.candles("SPY", start, now, True)
            proxy_volumes = {start + timedelta(minutes=1): number(c.get("volume"))
                             for c in proxy.get("candles", []) if (start := epoch(c.get("datetime")))}
        if not hasattr(self,"levels"):
            self.levels = {}
        premarket = [c for c in raw.get("candles",[]) if (at:=epoch(c.get("datetime")))
                     and opening.replace(hour=4)<=at<opening and at+timedelta(minutes=1)<=now
                     and number(c.get("high")) is not None and number(c.get("low")) is not None]
        if premarket:
            self.levels.setdefault((symbol,opening.date()),{}).update(
                premarket_high=max(c["high"] for c in premarket),premarket_low=min(c["low"] for c in premarket))
        bars = dict(cached)
        for candle in raw.get("candles", []):
            start = epoch(candle.get("datetime"))
            if not start or start < opening or start + timedelta(minutes=1) > now:
                continue
            end = start + timedelta(minutes=1)
            values = [number(candle.get(k)) for k in ("open", "high", "low", "close", "volume")]
            if proxy_volumes is not None:
                values[4] = proxy_volumes.get(end)
            if any(v is None for v in values):
                continue
            offset = int((end - opening).total_seconds() / 60)
            if not 1 <= offset <= 390:
                continue
            bars[end] = Bar(end, *values[:4], int(values[4]), baseline.get(offset))
        self.bar_cache = {k:v for k,v in self.bar_cache.items() if k[1] == opening.date()}
        self.bar_cache[key] = bars
        return tuple(bars[k] for k in sorted(bars))

    def warm_universe(self, symbols, now):
        session = self.session(now)
        if not session or now >= session[0]:
            return
        day=now.astimezone(ET).date()
        missing=[s for s in symbols if (s,day) not in self.baselines]
        for symbol in missing[:16]:
            try:
                self.baseline(symbol,now)
            except Exception as exc:
                LOG.warning("Baseline warmup %s unavailable (%s)",symbol,type(exc).__name__)
        if missing:
            LOG.info("Pre-open baselines loaded: %d/%d",sum((s,day) in self.baselines for s in symbols),len(symbols))

    @staticmethod
    def parse_chain(data: dict, allow_spx: bool = False) -> tuple[Option, ...]:
        if data.get("isDelayed") is True:
            return ()
        result = []
        for map_name, side in (("callExpDateMap", "CALL"), ("putExpDateMap", "PUT")):
            for expiry, strikes in data.get(map_name, {}).items():
                for contracts in strikes.values():
                    for raw in contracts:
                        quote_time = epoch(raw.get("quoteTimeInLong"))
                        fields = [number(raw.get(k)) for k in ("strikePrice", "bid", "ask", "totalVolume")]
                        contract_symbol = raw.get("symbol", "")
                        root = contract_symbol.split()[0] if contract_symbol else ""
                        if (not quote_time or not contract_symbol or raw.get("nonStandard")
                                or (allow_spx and root != "SPXW")
                                or (raw.get("isIndexOption") and not allow_spx)
                                or any(v is None for v in fields)):
                            continue
                        if number(raw.get("multiplier", 100)) != 100:
                            continue
                        result.append(Option(raw["symbol"], date.fromisoformat(expiry.split(":")[0]), side,
                                             *fields[:3], int(fields[3]), int(number(raw.get("openInterest")) or 0),
                                             number(raw.get("delta")), number(raw.get("gamma")), quote_time,
                                             number(raw.get("volatility")), number(raw.get("theta")),
                                             number(raw.get("vega"))))
        return tuple(result)

    def option_chain(self, symbol, now, max_dte):
        params = {"symbol": api_symbol(symbol), "contractType": "ALL", "strategy": "SINGLE",
                  "includeUnderlyingQuote": "true", "strikeCount": 80,
                  "fromDate": now.astimezone(ET).date().isoformat(),
                  "toDate": (now.astimezone(ET).date()+timedelta(days=max_dte)).isoformat()}
        data = self.get('/chains', params)
        options = self.parse_chain(data, allow_spx=symbol == 'SPX')
        def usable(o):
            return (o.delta is not None and 0 < abs(o.delta) <= 1
                    and (o.delta > 0) == (o.side == 'CALL')
                    and o.gamma is not None and o.gamma > 0)
        # Provider sentinel values (-999) are not Greeks. Retry once with a
        # smaller request, retaining the broad chain and only merging real,
        # newer provider quotes with usable Greeks. Never synthesize Greeks.
        if not options or any(usable(o) for o in options):
            return data
        LOG.warning('Chain Greeks unavailable for %s (%d contracts); retrying near-money chain', symbol, len(options))
        try:
            retry = self.get('/chains', {**params, 'strikeCount': 20})
            recovered = {o.symbol:o for o in self.parse_chain(retry, allow_spx=symbol == 'SPX') if usable(o)}
        except Exception as exc:
            LOG.warning('Chain Greek recovery %s failed (%s)', symbol, type(exc).__name__)
            return data
        merged = dict(data)
        count = 0
        for map_name in ('callExpDateMap', 'putExpDateMap'):
            replacements = {raw.get('symbol'):raw for strikes in retry.get(map_name, {}).values()
                            for contracts in strikes.values() for raw in contracts
                            if raw.get('symbol') in recovered}
            merged[map_name] = {}
            for expiry, strikes in data.get(map_name, {}).items():
                merged[map_name][expiry] = {}
                for strike, contracts in strikes.items():
                    rows = []
                    for raw in contracts:
                        newer = replacements.get(raw.get('symbol'))
                        if newer and (number(newer.get('quoteTimeInLong')) or 0) >= (number(raw.get('quoteTimeInLong')) or 0):
                            rows.append(newer); count += 1
                        else:
                            rows.append(raw)
                    merged[map_name][expiry][strike] = rows
        LOG.info('Chain Greek recovery %s: %d/%d contracts restored from provider', symbol, count, len(options))
        return merged

    def verify_greekless_opening_quotes(self, chains, spots, max_dte):
        """Check a separate quote endpoint when the entire opening chain is frozen.

        A refreshed timestamp in /chains did not imply a refreshed bid/ask on
        2026-10-19. Keep the chain price unless /quotes independently disagrees
        with it and supplies a current, crossed-free market. This does not
        estimate Greeks or turn a stock move into an option trade.
        """
        local = datetime.now(timezone.utc).astimezone(ET)
        if not ((local.hour == 9 and local.minute >= 35)
                or (local.hour == 10 and local.minute < 30)):
            return
        requested = {}
        for symbol, options in chains.items():
            spot = spots.get(symbol)
            if not spot or not options or any(
                0 <= (o.expiry-local.date()).days <= max_dte
                and -.01 <= (1 if o.side == 'CALL' else -1)*(o.strike/spot-1) <= .03
                and o.delta is not None and 0 < abs(o.delta) <= 1
                and (o.delta > 0) == (o.side == 'CALL')
                and o.gamma is not None and o.gamma > 0 for o in options):
                continue
            groups = defaultdict(list)
            for option in options:
                dte = (option.expiry-local.date()).days
                direction = 1 if option.side == 'CALL' else -1
                moneyness = direction*(option.strike/spot-1)
                # The chain premium itself may be frozen or nonsensical; do
                # not use it to decide which strikes receive a direct quote.
                if (0 <= dte <= max_dte and -.01 <= moneyness <= .03
                        and option.volume >= 100):
                    groups[(option.expiry,option.side)].append(option)
            for rows in groups.values():
                for option in sorted(rows, key=lambda o:(abs(o.strike-spot),-o.volume))[:8]:
                    requested[option.symbol] = (symbol,option)
        if not requested:
            return
        refreshed = {}
        ids = list(requested)
        for index in range(0,len(ids),60):
            batch = ids[index:index+60]
            try:
                direct = self.get('/quotes', {'symbols':','.join(batch)})
            except Exception as exc:
                LOG.warning('Direct opening option quote check failed (%s)', type(exc).__name__)
                continue
            observed_at = datetime.now(timezone.utc)
            for ident in batch:
                original = requested[ident][1]
                item = direct.get(ident,{})
                q = item.get('quote',{})
                qt, bid, ask = epoch(q.get('quoteTime')), number(q.get('bidPrice')), number(q.get('askPrice'))
                if (item.get('realtime') is False or qt is None or bid is None or ask is None
                        or not 0 <= (observed_at-qt).total_seconds() <= 90
                        or not 0 < bid < ask or ask-bid > .15
                        or (abs(bid-original.bid) < .01 and abs(ask-original.ask) < .01)):
                    continue
                volume = int(number(q.get('totalVolume')) or original.volume)
                refreshed[ident] = replace(original,bid=bid,ask=ask,quote_time=qt,
                                           volume=max(volume,original.volume),direct_quote_verified=True)
        for symbol, options in chains.items():
            chains[symbol] = [refreshed.get(option.symbol,option) for option in options]
        LOG.info('Opening Greekless chains: independently refreshed %d/%d near-money contracts',
                 len(refreshed),len(requested))

    def poll(self, symbols: list[str], tracked: dict, max_dte: int, on_discovery=None,
             standby=(), in_play_settings=None) -> list[Snapshot]:
        now = datetime.now(timezone.utc)
        if not hasattr(self, "coverage"):
            self.coverage = ChainCoverage()
        self.coverage.reset(now.astimezone(ET))
        self.discovery.observations = []
        session = self.session(now)
        if not session or not session[0] <= now < session[1]:
            return []
        benchmarks = {benchmark_for(s) for s in symbols} | {"SPY", "QQQ", "SMH"}
        standby = set(standby)
        atr_days = getattr(in_play_settings, "atr_days", None) if in_play_settings is not None else None
        if atr_days not in (14, 21):
            atr_days = None
        batch = getattr(in_play_settings, "quote_batch", 180) if in_play_settings is not None else 180
        quote_every = getattr(in_play_settings, "standby_quote_minutes", 1) if in_play_settings is not None else 1
        from .model_v import quote_universe
        cycle = getattr(self, "quote_cycle", 0)
        quoted = quote_universe([s for s in symbols if s not in standby],
                                [s for s in symbols if s in standby],
                                cycle, batch, quote_every)
        self.quote_cycle = cycle + 1
        if not hasattr(self, "standby_chain_at"):
            self.standby_chain_at = {}
        requested = sorted(set(quoted) | benchmarks)
        quotes = self.get("/quotes", {"symbols": ",".join(api_symbol(s) for s in requested)})
        parsed = {}
        for symbol in requested:
            item = quotes.get(api_symbol(symbol), quotes.get(symbol, {}))
            if item.get("realtime") is False:
                continue
            q = item.get("quote", {})
            parsed[symbol] = {
                "price":number(q.get("lastPrice")), "previous":number(q.get("closePrice")),
                "open":number(q.get("openPrice")), "volume":number(q.get("totalVolume")),
                "high":number(q.get("highPrice")), "low":number(q.get("lowPrice")),
                "at":epoch(q.get("tradeTime") or q.get("quoteTime")),
                "prior_atr": self.prior_atr_for(symbol, now.astimezone(ET).date(), atr_days),
            }
        if "SPX" in parsed:
            parsed["SPX"]["volume"] = parsed.get("SPY", {}).get("volume")
        core = set(symbols) - standby
        selected = self.discovery.update(parsed, datetime.now(timezone.utc), core or set(symbols), tracked,
                                         self.coverage.priorities(now),
                                         {s for s, available in self.coverage.has_near_expiry.items() if not available})
        if standby and in_play_settings is not None:
            from .model_v import due_standby, quote_in_play
            scan_minutes = max(1, int(getattr(in_play_settings, "standby_scan_minutes", 3)))
            scan_limit = max(1, int(getattr(in_play_settings, "standby_scans_per_cycle", 4)))
            in_play = []
            quotes_by_symbol = {}
            for symbol in standby:
                history = self.discovery.history.get(symbol) or []
                quote = dict(parsed.get(symbol) or {})
                if history:
                    quote.setdefault("volume_acceleration", history[-1].get("volume_acceleration"))
                    quote.setdefault("price", history[-1].get("price"))
                    quote.setdefault("open", history[-1].get("open"))
                    quote.setdefault("previous", history[-1].get("previous"))
                if not quote_in_play(quote, in_play_settings):
                    continue
                in_play.append(symbol)
                quotes_by_symbol[symbol] = history
            for symbol in due_standby(in_play, self.standby_chain_at, now, scan_minutes, scan_limit):
                self.standby_chain_at[symbol] = now
                history = quotes_by_symbol.get(symbol) or []
                if history and all(row["symbol"] != symbol for row in self.discovery.observations):
                    self.discovery.observations.append(history[-1])
                if symbol not in selected:
                    selected.insert(0, symbol)
        # The whole-universe stock path runs now. It must not wait for the
        # slower option-chain sweep or deep historical-bar downloads.
        handoff = []
        if on_discovery is not None:
            returned = on_discovery(self.discovery.observations) or []
            handoff = [s for s in returned if s in parsed]
        for symbol in reversed(handoff):
            if symbol in selected:
                selected.remove(symbol)
            selected.insert(0, symbol)
        result = []
        began = time.monotonic()
        _, _, rate_limits_before = self.request_statistics()
        sweep_limit = max(1, int(os.getenv("LOTTO_SWEEP_PER_CYCLE", "26")))
        sweep_universe = [s for s in symbols if s not in standby or s in selected]
        sweep_targets = self.coverage.due(sweep_universe, now, selected, sweep_limit)
        def chain(symbol):
            return self.option_chain(symbol, now, max_dte)
        def deep(symbol):
            bars = self.bars(symbol, datetime.now(timezone.utc))
            return bars, chain(symbol)
        with ThreadPoolExecutor(max_workers=6) as pool:
            sweep_jobs = {pool.submit(chain, symbol): symbol for symbol in sweep_targets}
            sweep_done, sweep_pending = wait(sweep_jobs, timeout=27)
            for job in sweep_pending:
                job.cancel()
            if sweep_pending:
                LOG.warning("Universe sweep budget reached: %d/%d chains completed", len(sweep_done), len(sweep_jobs))
            for job in sweep_done:
                symbol = sweep_jobs[job]
                self.coverage.attempt(symbol, datetime.now(timezone.utc))
                try:
                    data = job.result()
                    if data.get("isDelayed") is True:
                        raise ValueError("delayed option chain")
                    self.coverage.observe(symbol, datetime.now(timezone.utc), self.parse_chain(data, allow_spx=symbol == "SPX"),
                                          parsed.get(symbol, {}).get("price"))
                except Exception as exc:
                    LOG.warning("Universe chain sweep %s unavailable (%s)", symbol, type(exc).__name__)
            # Continue unfinished cold-start work first; a slow baseline must not starve later names.
            selected.sort(key=lambda s: (s not in tracked, s not in self.discovery.always_deep,
                                         getattr(self, "last_polled", {}).get(s, 0)))
            if not hasattr(self, "last_polled"):
                self.last_polled = {}
            deep_jobs = {pool.submit(deep, symbol): symbol for symbol in selected}
            completed = {}
            deep_done, deep_pending = wait(deep_jobs, timeout=max(0, 54-(time.monotonic()-began)))
            for job in deep_pending:
                job.cancel()
            if deep_pending:
                LOG.warning("Deep collection budget reached: %d/%d names completed", len(deep_done), len(deep_jobs))
            for job in deep_done:
                symbol = deep_jobs[job]
                self.last_polled[symbol] = time.monotonic()
                try:
                    completed[symbol] = job.result()
                except Exception as exc:
                    LOG.warning("%s skipped (%s)", symbol, type(exc).__name__)
        chain_options = {symbol:list(self.parse_chain(data, allow_spx=symbol == 'SPX'))
                         for symbol,(_,data) in completed.items()}
        self.verify_greekless_opening_quotes(chain_options,
            {symbol:item.get('price') for symbol,item in parsed.items()},max_dte)
        for symbol in selected:
            if symbol not in completed:
                continue
            try:
                bars, data = completed[symbol]
                options = chain_options[symbol]
                if data.get("isDelayed") is not True:
                    self.coverage.observe(symbol, datetime.now(timezone.utc), options,
                                          parsed.get(symbol, {}).get("price"), promoted=True)
                q = parsed.get(symbol, {})
                spot, previous, spot_time = q.get("price"), q.get("previous"), q.get("at")
                if data.get("isDelayed") is True or not spot_time or not spot or not previous:
                    raise ValueError("missing fresh underlying or delayed entitlement")
                existing = {o.symbol for o in options}
                missing = [o for o in tracked.get(symbol, []) if o.symbol not in existing]
                if missing:
                    extra = self.get("/quotes", {"symbols": ",".join(o.symbol for o in missing)})
                    for option in missing:
                        item = extra.get(option.symbol, {})
                        q = item.get("quote", {})
                        qt, bid, ask = epoch(q.get("quoteTime")), number(q.get("bidPrice")), number(q.get("askPrice"))
                        if qt and bid is not None and ask is not None and item.get("realtime") is not False:
                            options.append(replace(option, quote_time=qt, bid=bid, ask=ask,
                                                   volume=int(number(q.get("totalVolume")) or option.volume),
                                                   direct_quote_verified=False))
                at = datetime.now(timezone.utc)
                benchmark = benchmark_for(symbol)
                context, context_time = self.discovery.return_5m(benchmark, at)
                market = "QQQ" if symbol == "SPY" else "SPY"
                market_return, market_time = self.discovery.return_5m(market, at)
                market_from_open, market_vwap_side = _market_day_context(
                    completed.get(market, (None, None))[0] if market in completed else None,
                    parsed.get(market, {}).get("price"), parsed.get(market, {}).get("open"))
                peers = []
                # Genuine peers only; exclude the target and sector ETF itself.
                for peer in symbols:
                    if peer != symbol and peer in SECTORS and SECTORS.get(peer) == SECTORS.get(symbol) and symbol in SECTORS:
                        value, timestamp = self.discovery.return_5m(peer, at)
                        if value is not None:
                            peers.append((peer, value, timestamp))
                peer_return = median(p[1] for p in peers) if len(peers) >= 2 else None
                leaders = tuple(p[0] for p in sorted(peers, key=lambda p:abs(p[1]), reverse=True)[:3]
                                if peer_return is not None and p[1]*peer_return > 0)
                rvol_info = getattr(self, "handoff_rvol", {}).get((symbol, now.astimezone(ET).date()), {})
                rvol_minute = None
                if bars:
                    end = bars[-1].end.astimezone(ET)
                    rvol_minute = (end.hour * 60 + end.minute) - (9 * 60 + 31)
                rvol_base = (rvol_info.get("base") or {}).get(rvol_minute)
                snap = Snapshot(symbol, at, spot, spot_time, previous, bars, tuple(options),
                                       context, context_time, benchmark, "schwab", session[1],
                                       volume_source="SPY" if symbol == "SPX" else symbol,
                                       prior_atr=self.prior_atr_for(symbol, now.astimezone(ET).date(), atr_days),
                                       market_return_5m=market_return, market_time=market_time, market_label=market,
                                       market_return_from_open=market_from_open, market_vwap_side=market_vwap_side,
                                       peer_return_5m=peer_return, peer_time=min((p[2] for p in peers), default=None),
                                       peer_leaders=leaders,
                                       prior_full_days=rvol_info.get("days"),
                                       rvol_base=rvol_base,
                                       **getattr(self,"levels",{}).get((symbol,now.astimezone(ET).date()),{}))
                # Kept off the dataclass so stored option cards stay the same bytes.
                day_key = (symbol, now.astimezone(ET).date())
                fb = getattr(self, "fallback_rvol", {}).get(day_key, {})
                object.__setattr__(snap, "prior_atr21", getattr(self, "atrs21", {}).get(day_key))
                object.__setattr__(snap, "fallback_rvol_sessions", fb.get("sessions"))
                object.__setattr__(snap, "fallback_rvol_base", (fb.get("base") or {}).get(rvol_minute))
                result.append(snap)
            except Exception as exc:
                LOG.warning("%s skipped (%s)", symbol, type(exc).__name__)
        recent_requests, spacing, rate_limits_total = self.request_statistics()
        LOG.info("Discovery: %d/%d fresh quotes, %d promoted, %d deep chains; universe chains %d/%d fresh within %d min (%d this cycle); collection %.1fs; Schwab requests/60s=%d spacing=%.3fs 429s/cycle=%d total=%d",
                 len(self.discovery.observations), len(symbols), len(selected), len(result),
                 self.coverage.count_fresh(symbols, datetime.now(timezone.utc)), len(symbols),
                 self.coverage.interval.seconds//60, len(self.coverage.rows), time.monotonic()-began,
                 recent_requests, spacing, rate_limits_total-rate_limits_before, rate_limits_total)
        return result
