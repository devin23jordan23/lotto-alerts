from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from math import isfinite
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamps must include a UTC offset")
    return result


def positive(*values: float) -> bool:
    return all(isfinite(v) and v > 0 for v in values)


@dataclass(frozen=True)
class Bar:
    end: datetime  # End of a completed one-minute regular-session bar.
    open: float
    high: float
    low: float
    close: float
    volume: int
    expected_cumulative_volume: float | None = None

    def valid(self) -> bool:
        return (self.end.tzinfo is not None and positive(self.open, self.high, self.low, self.close)
                and self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high
                and self.volume >= 0 and isfinite(self.volume))


@dataclass(frozen=True)
class Option:
    symbol: str  # Unique provider contract identifier.
    expiry: date
    side: str  # CALL or PUT
    strike: float
    bid: float
    ask: float
    volume: int  # Cumulative session contracts; not signed flow.
    open_interest: int
    delta: float | None
    gamma: float | None
    quote_time: datetime

    def valid(self) -> bool:
        return (self.side in {"CALL", "PUT"} and positive(self.strike, self.ask)
                and isfinite(self.bid) and 0 <= self.bid <= self.ask
                and self.volume >= 0 and isfinite(self.volume)
                and self.quote_time.tzinfo is not None)


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    at: datetime
    spot: float
    spot_time: datetime
    prior_close: float
    bars: tuple[Bar, ...]  # Complete session-to-date history, no extended hours.
    options: tuple[Option, ...]
    context_return_5m: float | None = None
    context_time: datetime | None = None
    context_label: str = ""
    source: str = "replay"
    session_end: datetime | None = None  # Required for live; respects holidays/early closes.
    prior_atr: float | None = None  # Completed prior daily sessions only.
    market_return_5m: float | None = None
    market_time: datetime | None = None
    market_label: str = "SPY"
    peer_return_5m: float | None = None
    peer_time: datetime | None = None
    peer_leaders: tuple[str, ...] = ()

    @property
    def day(self) -> str:
        return self.at.astimezone(ET).date().isoformat()

    def to_dict(self) -> dict:
        def convert(value):
            if isinstance(value, (datetime, date)):
                return value.isoformat()
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, (tuple, list)):
                return [convert(v) for v in value]
            return value
        return convert(asdict(self))

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        data = dict(data)
        for key in ("at", "spot_time", "context_time", "session_end", "market_time", "peer_time"):
            if data.get(key) is not None:
                data[key] = timestamp(data[key])
        data["bars"] = tuple(Bar(**{**b, "end": timestamp(b["end"])}) for b in data["bars"])
        data["options"] = tuple(Option(**{**o, "expiry": date.fromisoformat(o["expiry"]),
                                           "quote_time": timestamp(o["quote_time"])}) for o in data["options"])
        data["peer_leaders"] = tuple(data.get("peer_leaders", ()))
        return cls(**data)

    def problems(self, max_age: int = 90, min_bars: int = 11) -> list[str]:
        if self.at.tzinfo is None or self.spot_time.tzinfo is None:
            return ["timezone missing"]
        local = self.at.astimezone(ET)
        if local.weekday() >= 5 or not time(9, 30) <= local.time() < time(16):
            return ["outside regular session"]
        if self.session_end and self.at >= self.session_end:
            return ["session closed"]
        if not positive(self.spot, self.prior_close):
            return ["invalid underlying price"]
        if not 0 <= (self.at - self.spot_time).total_seconds() <= max_age:
            return ["stale or future underlying quote"]
        if len(self.bars) < min_bars:
            return ["warming up: opening range plus one completed bar required"]
        if not 0 <= (self.at - self.bars[-1].end).total_seconds() <= max_age:
            return ["stale or future underlying bars"]
        if len({o.symbol for o in self.options}) != len(self.options):
            return ["duplicate option identifiers"]
        for i, bar in enumerate(self.bars):
            local_bar = bar.end.astimezone(ET)
            if (not bar.valid() or local_bar.date() != local.date()
                    or not time(9, 31) <= local_bar.time() <= time(16) or bar.end > self.at):
                return ["invalid, future, or mixed-session bar"]
            if i and (bar.end - self.bars[i - 1].end).total_seconds() != 60:
                return ["incomplete one-minute bar history"]
        if self.bars[0].end.astimezone(ET).time() != time(9, 31):
            return ["session open bar missing"]
        baseline = self.bars[-1].expected_cumulative_volume
        if baseline is None or not positive(baseline):
            return ["same-time historical volume baseline unavailable"]
        return []
