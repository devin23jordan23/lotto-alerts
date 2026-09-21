"""Read-only Schwab REST adapter. No brokerage/order endpoints."""
import base64
import json
import logging
import os
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

LOG = logging.getLogger(__name__)
BASE = "https://api.schwabapi.com/marketdata/v1"
SECTORS = {**dict.fromkeys(("NVDA", "AMD", "ARM", "INTC", "MU", "AVGO", "SMCI"), "SMH"),
           **dict.fromkeys(("COIN", "HOOD", "MSTR"), "IBIT"), "MRNA": "XBI"}


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
        self.token_path = Path(data_dir) / "schwab_tokens.json"
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.client_id or not self.secret or not (self.seed or self.token_path.exists()):
            raise ValueError("Schwab client ID, secret, and refresh token are required")
        self.last_request = 0
        self.baselines = {}
        self.calendar_cache = {}

    def token(self, force=False):
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

    def get(self, path: str, params: dict) -> dict:
        for attempt in range(2):
            time.sleep(max(0, .55 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
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

    def candles(self, symbol: str, start: datetime, end: datetime) -> dict:
        return self.get("/pricehistory", {"symbol": symbol, "periodType": "day", "frequencyType": "minute", "frequency": 1,
                                        "startDate": int(start.timestamp() * 1000), "endDate": int(end.timestamp() * 1000),
                                        "needExtendedHoursData": "false", "needPreviousClose": "true"})

    def baseline(self, symbol: str, now: datetime):
        day = now.astimezone(ET).date()
        key = (symbol, day)
        if key in self.baselines:
            return self.baselines[key]
        data = self.candles(symbol, now - timedelta(days=45), now)
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
        if not baseline:
            LOG.warning("%s has fewer than ten usable historical sessions; no alerts until baseline is available", symbol)
        self.baselines[key] = baseline
        return baseline

    def bars(self, symbol: str, now: datetime, with_baseline=True) -> tuple[Bar, ...]:
        opening = now.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
        raw = self.candles(symbol, opening, now)
        baseline = self.baseline(symbol, now) if with_baseline else {}
        bars = {}
        for candle in raw.get("candles", []):
            start = epoch(candle.get("datetime"))
            if not start or start < opening or start + timedelta(minutes=1) > now:
                continue
            end = start + timedelta(minutes=1)
            values = [number(candle.get(k)) for k in ("open", "high", "low", "close", "volume")]
            if any(v is None for v in values):
                continue
            offset = int((end - opening).total_seconds() / 60)
            if not 1 <= offset <= 390:
                continue
            bars[end] = Bar(end, *values[:4], int(values[4]), baseline.get(offset))
        return tuple(bars[k] for k in sorted(bars))

    @staticmethod
    def parse_chain(data: dict) -> tuple[Option, ...]:
        if data.get("isDelayed") is True:
            return ()
        result = []
        for map_name, side in (("callExpDateMap", "CALL"), ("putExpDateMap", "PUT")):
            for expiry, strikes in data.get(map_name, {}).items():
                for contracts in strikes.values():
                    for raw in contracts:
                        quote_time = epoch(raw.get("quoteTimeInLong"))
                        fields = [number(raw.get(k)) for k in ("strikePrice", "bid", "ask", "totalVolume")]
                        if (not quote_time or not raw.get("symbol") or raw.get("nonStandard")
                                or raw.get("isIndexOption") or any(v is None for v in fields)):
                            continue
                        if number(raw.get("multiplier", 100)) != 100:
                            continue
                        result.append(Option(raw["symbol"], date.fromisoformat(expiry.split(":")[0]), side,
                                             *fields[:3], int(fields[3]), int(number(raw.get("openInterest")) or 0),
                                             number(raw.get("delta")), number(raw.get("gamma")), quote_time))
        return tuple(result)

    def poll(self, symbols: list[str], tracked: dict, max_dte: int) -> list[Snapshot]:
        now = datetime.now(timezone.utc)
        session = self.session(now)
        if not session or not session[0] <= now < session[1]:
            return []
        contexts = {}
        for benchmark in sorted({SECTORS.get(s, "QQQ") for s in symbols}):
            try:
                bars = self.bars(benchmark, now, with_baseline=False)
                if len(bars) >= 6 and (bars[-1].end - bars[-6].end).total_seconds() == 300:
                    contexts[benchmark] = (bars[-1].close / bars[-6].close - 1, bars[-1].end)
            except Exception as exc:
                LOG.warning("Context %s unavailable (%s)", benchmark, type(exc).__name__)
        result = []
        for symbol in symbols:
            try:
                # Fetch bars/baselines first so their startup latency cannot age the quote.
                bars = self.bars(symbol, datetime.now(timezone.utc))
                data = self.get("/chains", {"symbol": symbol, "contractType": "ALL", "strategy": "SINGLE",
                                            "includeUnderlyingQuote": "true", "strikeCount": 80,
                                            "fromDate": now.astimezone(ET).date().isoformat(),
                                            "toDate": (now.astimezone(ET).date() + timedelta(days=max_dte)).isoformat()})
                options = list(self.parse_chain(data))
                quote_response = self.get("/quotes", {"symbols": symbol})
                item = quote_response.get(symbol, {})
                if item.get("realtime") is False or data.get("isDelayed") is True:
                    raise ValueError("delayed entitlement")
                quote = item.get("quote", {})
                spot, previous = number(quote.get("lastPrice")), number(quote.get("closePrice"))
                spot_time = epoch(quote.get("tradeTime"))
                if not spot_time or not spot or not previous:
                    raise ValueError("missing underlying source timestamp or price")
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
                                                   volume=int(number(q.get("totalVolume")) or option.volume)))
                benchmark = SECTORS.get(symbol, "QQQ")
                context, context_time = contexts.get(benchmark, (None, None))
                result.append(Snapshot(symbol, datetime.now(timezone.utc), spot, spot_time, previous,
                                       bars, tuple(options), context, context_time, benchmark, "schwab", session[1]))
            except Exception as exc:
                # Exception type only: network errors may embed token-bearing URLs.
                LOG.warning("%s skipped (%s)", symbol, type(exc).__name__)
        return result
