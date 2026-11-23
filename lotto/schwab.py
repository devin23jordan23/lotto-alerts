"""Read-only Schwab REST adapter. No brokerage/order endpoints."""
import base64
import json
import logging
import os
import threading
import time
from collections import defaultdict
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
from .latency import fetch_plan, request_gap
from .resilience import flag

LOG = logging.getLogger(__name__)
BASE = "https://api.schwabapi.com/marketdata/v1"
SECTORS = {**dict.fromkeys(("TSM", "NVDA", "AMD", "ARM", "INTC", "MU", "AVGO", "SMCI", "QCOM", "MRVL", "AMAT", "LRCX", "KLAC", "ASML", "SNDK", "SKHY"), "SMH"),
           **dict.fromkeys(("COIN", "HOOD", "MSTR"), "IBIT"), "MRNA": "XBI"}


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
        self.broker_expiry = 0.0  # real expiry (monotonic); used only by LOTTO_TOKEN_RESILIENCE_MODE=1
        self._fallback_token = ""
        self._fallback_until = 0.0
        self._gap_lock = threading.Lock()
        self._request_gap = 0.55
        self.request_count = 0
        self.fetch_trace = []
        self.token_path = Path(data_dir) / "schwab_tokens.json"
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        if bool(self.broker_url) != bool(self.broker_key):
            raise ValueError("Schwab token broker URL and key must be set together")
        if not self.broker_url and (not self.client_id or not self.secret or not (self.seed or self.token_path.exists())):
            raise ValueError("Schwab client ID, secret, and refresh token are required")
        self.last_request = 0
        self.baselines = {}
        self.calendar_cache = {}
        self.atrs = {}
        self.levels = {}
        self.discovery = Discovery(int(os.getenv("LOTTO_CHAIN_CAPACITY", "30")),
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
                self._fallback_token = ""
            return self.token_from_broker()
        return self._refresh_access_token(force)

    def _direct_refresh_ready(self) -> bool:
        if not self.client_id or not self.secret or not self.token_path.exists():
            return False
        try:
            payload = json.loads(self.token_path.read_text())
        except (OSError, ValueError):
            return False
        return bool(payload.get("refresh_token"))

    def _refresh_access_token(self, force=False):
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

    def _remember_fallback(self, token):
        self._fallback_token = token
        try:
            fresh = json.loads(self.token_path.read_text())
            self._fallback_until = float(fresh.get("expires_at", time.time() + 180))
        except (OSError, ValueError, TypeError):
            self._fallback_until = time.time() + 180

    def token_from_broker(self):
        # A direct-login token already in hand: one broker attempt, then reuse it.
        # This avoids a retry storm on every quote while the broker stays down.
        if (flag("LOTTO_TOKEN_FALLBACK_MODE") in (1, 2) and self._fallback_token
                and time.time() < self._fallback_until - 120):
            try:
                token = self._broker_fetch_once()
            except Exception:
                return self._fallback_token
            self._fallback_token = ""
            return token
        try:
            if flag("LOTTO_TOKEN_RESILIENCE_MODE") == 1:
                token = self.resilient_broker_token()
            else:
                token = self._broker_fetch_once()
        except Exception:
            if flag("LOTTO_TOKEN_FALLBACK_MODE") not in (1, 2) or not self._direct_refresh_ready():
                raise
            LOG.warning("token fallback used")
            token = self._refresh_access_token(force=False)
            self._remember_fallback(token)
            return token
        self._fallback_token = ""
        return token

    def _broker_fetch_once(self):
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

    def resilient_broker_token(self, sleep=None):
        """Refresh early, retry briefly, and keep a still-valid token through a broker blip."""
        if sleep is None:
            sleep = time.sleep
        now = time.monotonic()
        if self.broker_token and now < self.broker_deadline:
            return self.broker_token
        last_error = None
        for delay in (0, 1, 2, 4):
            if delay:
                sleep(delay)
            request = Request(self.broker_url, headers={
                "Authorization": f"Bearer {self.broker_key}",
                "Accept": "application/json",
            })
            try:
                with urlopen(request, timeout=5) as response:
                    payload = json.load(response)
                token = payload.get("access_token", "")
                if not token:
                    raise RuntimeError("Schwab token broker returned no access token")
                lifetime = max(30, int(payload.get("expires_in", 240)))
                lead = max(10, flag("LOTTO_TOKEN_REFRESH_LEAD_SECONDS", 60))
                fetched = time.monotonic()
                self.broker_token = token
                self.broker_expiry = fetched + lifetime - 5
                self.broker_deadline = fetched + max(15, lifetime - lead)
                return token
            except HTTPError as exc:
                last_error = RuntimeError(f"Schwab token broker rejected request (HTTP {exc.code})")
                if exc.code in (401, 403):
                    break  # a bad key will not heal by retrying
            except Exception as exc:  # timeout, DNS, connection reset, bad JSON
                last_error = exc if isinstance(exc, RuntimeError) else RuntimeError(
                    f"Schwab token broker unavailable ({type(exc).__name__})")
        if self.broker_token and time.monotonic() < self.broker_expiry:
            LOG.warning("Schwab token broker refresh failed (%s); reusing the cached token until it expires", last_error)
            return self.broker_token
        raise last_error or RuntimeError("Schwab token broker unavailable")

    def get(self, path: str, params: dict) -> dict:
        for attempt in range(2):
            with self._gap_lock:
                time.sleep(max(0, self._request_gap - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
                self.request_count += 1
            request = Request(f"{BASE}{path}?{urlencode(params)}", headers={"Authorization": f"Bearer {self.token()}", "Accept": "application/json"})
            try:
                with urlopen(request, timeout=15) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    self.token(force=True)
                    continue
                raise RuntimeError(f"Schwab {path} failed (HTTP {exc.code})") from None
        raise RuntimeError("Schwab authorization retry failed")

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
        self.atrs[key] = sum(ranges[-14:])/14 if len(ranges) >= 14 else None
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
            total = 0
            for minute in range(390):
                if minute not in profiles[historic_day]:
                    break  # Missing bars never lower the cumulative baseline artificially.
                total += profiles[historic_day][minute]
                observations[minute + 1].append(total)
        baseline = {minute: median(values) for minute, values in observations.items() if len(values) >= 10}
        if symbol == "SPX":
            # Schwab index candles have zero volume; use traded SPY volume only
            # for volume-based signals while retaining SPX prices and ATR.
            baseline = self.baseline("SPY", now)
        if not baseline:
            LOG.warning("%s has fewer than ten usable historical sessions; no alerts until baseline is available", symbol)
        self.baselines[key] = baseline
        return baseline

    def bars(self, symbol: str, now: datetime, with_baseline=True) -> tuple[Bar, ...]:
        opening = now.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
        raw = self.candles(symbol, opening.replace(hour=4), now, True)
        baseline = self.baseline(symbol, now) if with_baseline else {}
        proxy_volumes = None
        if symbol == "SPX":
            proxy = self.candles("SPY", opening.replace(hour=4), now, True)
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
        bars = {}
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

    def poll(self, symbols: list[str], tracked: dict, max_dte: int, settings=None,
             hot=None, on_snapshot=None, on_seed=None) -> list[Snapshot]:
        now = datetime.now(timezone.utc)
        if not hasattr(self, "coverage"):
            self.coverage = ChainCoverage()
        self.coverage.reset(now.astimezone(ET))
        self.discovery.observations = []
        session = self.session(now)
        if not session or not session[0] <= now < session[1]:
            return []
        benchmarks = {benchmark_for(s) for s in symbols} | {"SPY", "QQQ", "SMH"}
        requested = sorted(set(symbols) | benchmarks)
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
            }
        if "SPX" in parsed:
            parsed["SPX"]["volume"] = parsed.get("SPY", {}).get("volume")
        priority_mode = settings.discovery_priority_mode if settings is not None else 0
        selected = self.discovery.update(parsed, datetime.now(timezone.utc), set(symbols), tracked,
                                         self.coverage.priorities(now), priority_mode)
        mode = settings.bar_close_eval_mode if settings is not None else 0
        cap = settings.hot_max if settings is not None else 12
        self._request_gap = request_gap(mode, settings.schwab_rps if settings is not None else 1.8)
        self.fetch_trace = ["quotes"]
        plan = fetch_plan(selected, hot or [], mode, cap)
        hot_first = plan[1:plan.index("sweep")] if mode == 2 else []
        result = []
        began = time.monotonic()

        def consume(symbol):
            if not hasattr(self, "last_polled"):
                self.last_polled = {}
            self.last_polled[symbol] = time.monotonic()
            self.fetch_trace.append(f"deep:{symbol}")
            try:
                bars = self.bars(symbol, datetime.now(timezone.utc))
                data = self.get("/chains", {"symbol": api_symbol(symbol), "contractType": "ALL", "strategy": "SINGLE",
                                            "includeUnderlyingQuote": "true", "strikeCount": 80,
                                            "fromDate": now.astimezone(ET).date().isoformat(),
                                            "toDate": (now.astimezone(ET).date() + timedelta(days=max_dte)).isoformat()})
                options = list(self.parse_chain(data, allow_spx=symbol == "SPX"))
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
                        quote = item.get("quote", {})
                        qt, bid, ask = epoch(quote.get("quoteTime")), number(quote.get("bidPrice")), number(quote.get("askPrice"))
                        if qt and bid is not None and ask is not None and item.get("realtime") is not False:
                            options.append(replace(option, quote_time=qt, bid=bid, ask=ask,
                                                   volume=int(number(quote.get("totalVolume")) or option.volume)))
                at = datetime.now(timezone.utc)
                benchmark = benchmark_for(symbol)
                context, context_time = self.discovery.return_5m(benchmark, at)
                tape = {}
                if settings is not None and settings.after14_mode >= 1:
                    spy_return, spy_time = self.discovery.return_5m("SPY", at)
                    qqq_return, qqq_time = self.discovery.return_5m("QQQ", at)
                    tape = {"spy_return_5m": spy_return, "spy_return_time": spy_time,
                            "qqq_return_5m": qqq_return, "qqq_return_time": qqq_time}
                market = "QQQ" if symbol == "SPY" else "SPY"
                market_return, market_time = self.discovery.return_5m(market, at)
                peers = []
                for peer in symbols:
                    if peer != symbol and peer in SECTORS and SECTORS.get(peer) == SECTORS.get(symbol) and symbol in SECTORS:
                        value, timestamp = self.discovery.return_5m(peer, at)
                        if value is not None:
                            peers.append((peer, value, timestamp))
                peer_return = median(p[1] for p in peers) if len(peers) >= 2 else None
                leaders = tuple(p[0] for p in sorted(peers, key=lambda p: abs(p[1]), reverse=True)[:3]
                                if peer_return is not None and p[1] * peer_return > 0)
                result.append(Snapshot(symbol, at, spot, spot_time, previous, bars, tuple(options),
                                       context, context_time, benchmark, "schwab", session[1],
                                       volume_source="SPY" if symbol == "SPX" else symbol,
                                       prior_atr=self.atrs.get((symbol, now.astimezone(ET).date())),
                                       market_return_5m=market_return, market_time=market_time, market_label=market,
                                       peer_return_5m=peer_return, peer_time=min((p[2] for p in peers), default=None),
                                       peer_leaders=leaders,
                                       **tape,
                                       **getattr(self, "levels", {}).get((symbol, now.astimezone(ET).date()), {})))
                return True
            except Exception as exc:
                LOG.warning("%s skipped (%s)", symbol, type(exc).__name__)
                return False

        hot_done = set()
        for symbol in hot_first:
            if time.monotonic() - began >= 54:
                LOG.warning("Deep chain time budget reached; remaining candidates resume next cycle")
                break
            if consume(symbol) and on_snapshot is not None:
                on_snapshot(result[-1])
            hot_done.add(symbol)
        self.fetch_trace.append("sweep")
        sweep_limit = max(1, int(os.getenv("LOTTO_SWEEP_PER_CYCLE", "26")))
        sweep_targets = self.coverage.due(symbols, now, selected, sweep_limit)
        for symbol in sweep_targets:
            if time.monotonic()-began >= 27:
                LOG.warning("Universe chain sweep time budget reached; remaining names resume next cycle")
                break
            self.coverage.attempt(symbol, datetime.now(timezone.utc))
            try:
                data = self.get("/chains", {"symbol":api_symbol(symbol), "contractType":"ALL", "strategy":"SINGLE",
                                            "includeUnderlyingQuote":"true", "strikeCount":80,
                                            "fromDate":now.astimezone(ET).date().isoformat(),
                                            "toDate":(now.astimezone(ET).date()+timedelta(days=max_dte)).isoformat()})
                if data.get("isDelayed") is True:
                    raise ValueError("delayed option chain")
                chain = self.parse_chain(data, allow_spx=symbol == "SPX")
                price = parsed.get(symbol, {}).get("price")
                self.coverage.observe(symbol, datetime.now(timezone.utc), chain, price)
                if on_seed is not None and settings is not None and settings.oh_seed_mode >= 1:
                    on_seed(symbol, datetime.now(timezone.utc), chain, price)
            except Exception as exc:
                LOG.warning("Universe chain sweep %s unavailable (%s)", symbol, type(exc).__name__)
        # Continue unfinished cold-start work first; a slow baseline must not starve later names.
        selected.sort(key=lambda s: (s not in tracked, s not in self.discovery.always_deep,
                                     getattr(self, "last_polled", {}).get(s, 0)))
        for symbol in selected:
            if symbol in hot_done:
                continue
            if time.monotonic()-began >= 54:
                LOG.warning("Deep chain time budget reached; remaining candidates resume next cycle")
                break
            consume(symbol)
        LOG.info("Discovery: %d/%d fresh quotes, %d promoted, %d deep chains; universe chains %d/%d fresh within %d min (%d this cycle)",
                 len(self.discovery.observations), len(symbols), len(selected), len(result),
                 self.coverage.count_fresh(symbols, datetime.now(timezone.utc)), len(symbols),
                 self.coverage.interval.seconds//60, len(self.coverage.rows))
        return result
