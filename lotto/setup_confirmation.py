"""Causal, strategy-specific confirmation for Model V option cards.

This module does not choose a contract or assign points.  It reports whether
the stock setup is confirmed at the observation timestamp.  The live gate is
separately controlled by LOTTO_SETUP_CONFIRMATION_ENABLED; until it is evaluated on
current sessions the result is shadow-logged only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from statistics import median


@dataclass(frozen=True)
class Confirmation:
    state: str  # detected, confirmed, or expired
    family: str
    reason: str
    event_at: datetime | None
    volume_ratio: float | None

    @property
    def ready(self) -> bool:
        return self.state == "confirmed"


def confirmation_metrics(confirmation: Confirmation, required: bool) -> dict:
    """Store the same audit fields for ordinary and stock-event candidates."""
    return {
        "setup_confirmation_state": confirmation.state,
        "setup_confirmation_family": confirmation.family,
        "setup_confirmation_reason": confirmation.reason,
        "setup_confirmation_event_at": (confirmation.event_at.isoformat()
                                        if confirmation.event_at else None),
        "setup_confirmation_volume_ratio": confirmation.volume_ratio,
        "setup_confirmation_required": int(required),
    }


def _direction(side: str) -> int:
    return 1 if side == "CALL" else -1 if side == "PUT" else 0


def _crossed(previous: float, current: float, level: float, direction: int) -> bool:
    return direction * (previous - level) <= 0 < direction * (current - level)


def fresh_cross(snap, side: str, levels, max_bars: int = 3):
    """Latest crossing of a known level, using only bars available at `snap.at`.

    A live stock quote can confirm a cross after the last completed bar.  Its
    timestamp must be newer than that bar, so a stale last trade cannot do it.
    """
    direction = _direction(side)
    bars = snap.bars
    if not direction or not bars:
        return None
    known = [(name, float(value)) for name, value in levels
             if isinstance(value, (int, float)) and isfinite(value) and value > 0]
    if not known:
        return None
    latest = None
    for index in range(max(1, len(bars) - max_bars), len(bars)):
        for name, level in known:
            if _crossed(bars[index - 1].close, bars[index].close, level, direction):
                latest = (name, level, bars[index].end)
    if (snap.spot_time >= bars[-1].end and snap.spot_time <= snap.at
            and (snap.at - snap.spot_time).total_seconds() <= 90):
        for name, level in known:
            if _crossed(bars[-1].close, snap.spot, level, direction):
                latest = (name, level, snap.spot_time)
    return latest


def _ema(values, period: int) -> float:
    value = float(values[0])
    alpha = 2 / (period + 1)
    for price in values[1:]:
        value += alpha * (price - value)
    return value


def five_bar_tod_volume(snap) -> float | None:
    """Five completed bars divided by their same-time historical expectation."""
    bars = snap.bars
    if len(bars) < 6:
        return None
    newest = bars[-1].expected_cumulative_volume
    before = bars[-6].expected_cumulative_volume
    if (newest is None or before is None or not isfinite(newest)
            or not isfinite(before) or newest <= before):
        return None
    return sum(bar.volume for bar in bars[-5:]) / (newest - before)


def _local_participation(bars) -> float | None:
    earlier = [bar.volume for bar in bars[-6:-1] if bar.volume > 0]
    if len(earlier) < 3:
        return None
    return bars[-1].volume / median(earlier)


def _level_context(snap, side: str) -> tuple:
    direction = _direction(side)
    levels = (("PMH", snap.premarket_high), ("PDH", snap.prior_high)) if direction == 1 else (
        ("PML", snap.premarket_low), ("PDL", snap.prior_low))
    return tuple((name, value) for name, value in levels
                 if isinstance(value, (int, float)) and isfinite(value) and value > 0
                 and direction * (snap.spot - value) > 0)


def ema_level_continuation(snap, side: str, min_tod_volume: float = 1.5) -> Confirmation:
    """PD/PM-level EMA continuation: context AND reclaim AND volume AND push."""
    bars = snap.bars
    direction = _direction(side)
    if not direction or len(bars) < 10:
        return Confirmation("detected", "EMA_LEVEL_CONTINUATION", "EMA warmup", None, None)
    context = tuple((name, value) for name, value in _level_context(snap, side)
                    if all(direction * (bar.close - value) > 0 for bar in bars[-2:]))
    if not context:
        return Confirmation("detected", "EMA_LEVEL_CONTINUATION", "directional PD/PM level absent", None, None)
    closes = [bar.close for bar in bars]
    ema_before = _ema(closes[:-1], 9)
    ema_now = _ema(closes, 9)
    touched = bars[-2].low <= ema_before <= bars[-2].high
    reclaimed = (direction * (bars[-1].close - ema_now) > 0
                 and direction * (bars[-1].close - bars[-2].close) > 0)
    renewed = direction * (snap.spot - (bars[-2].high if direction == 1 else bars[-2].low)) > 0
    if not (touched and reclaimed and renewed and direction * (ema_now - ema_before) > 0):
        return Confirmation("detected", "EMA_LEVEL_CONTINUATION", "touch without reclaim and renewed move", None, None)
    volume = five_bar_tod_volume(snap)
    if volume is None or volume < min_tod_volume:
        return Confirmation("detected", "EMA_LEVEL_CONTINUATION", "same-time volume unconfirmed", bars[-1].end, volume)
    return Confirmation("confirmed", "EMA_LEVEL_CONTINUATION", "+".join(name for name, _ in context),
                        bars[-1].end, volume)


def confirm_setup(snap, side: str, setup) -> Confirmation:
    """Evaluate a named stock setup; broad score and option flow never vote."""
    if setup is None:
        return Confirmation("detected", "NONE", "no stock setup", None, None)
    family = setup.name
    if setup.building:
        return Confirmation("detected", family, "setup still building", None, None)
    if family in {"REVERSAL", "INTRADAY_REVERSAL", "STOCK_OVERRIDE"}:
        return Confirmation("expired", family, "directional model excludes reversal/override", None, None)
    bars = snap.bars
    direction = _direction(side)
    if not direction or len(bars) < 3:
        return Confirmation("detected", family, "stock bars unavailable", None, None)
    tod = five_bar_tod_volume(snap)
    local = _local_participation(bars)
    participating = (tod is not None and tod >= 1.0) or (tod is None and local is not None and local >= 1.0)
    recent_move = direction * (snap.spot - bars[-3].close) > 0
    if family == "EMA_LEVEL_CONTINUATION":
        return ema_level_continuation(snap, side)
    if family in {"OPENING_BREAK", "LEVEL_RECLAIM", "COILED_CONTINUATION", "BASE_BREAK",
                  "LOCAL_RANGE_BREAK", "INTRADAY_BREAK", "PM_BREAK", "PD_BREAK", "HOD_BREAK", "LOD_BREAK"}:
        crossed = fresh_cross(snap, side, ((family, setup.trigger),))
        if not crossed:
            return Confirmation("expired", family, "level break older than three bars or absent", None, tod)
        if not participating or not recent_move:
            return Confirmation("detected", family, "break lacks participation or renewed movement", crossed[2], tod)
        return Confirmation("confirmed", family, crossed[0], crossed[2], tod)
    if family == "OPENING_DRIVE":
        if len(bars) > 15:
            return Confirmation("expired", family, "opening drive window passed", None, tod)
        if not participating or not recent_move or direction * (snap.spot - bars[0].open) <= 0:
            return Confirmation("detected", family, "drive lacks direction or participation", None, tod)
        return Confirmation("confirmed", family, "opening push", bars[-1].end, tod)
    if family in {"PULLBACK_RESUMPTION", "RECOVERY_CONTINUATION", "CONTINUATION", "TREND_PULLBACK"}:
        if not participating or not recent_move:
            return Confirmation("detected", family, "continuation lacks participation or renewed movement", None, tod)
        if direction * (snap.spot - bars[0].open) <= 0:
            return Confirmation("detected", family, "continuation opposes session direction", None, tod)
        if family in {"PULLBACK_RESUMPTION", "TREND_PULLBACK"}:
            # An EMA tag alone is not a trigger: the current bar must resume
            # through the preceding bar's directional edge.
            edge = bars[-2].high if direction == 1 else bars[-2].low
            if direction * (snap.spot - edge) <= 0:
                return Confirmation("detected", family, "pullback has not resumed", None, tod)
        return Confirmation("confirmed", family, "directional continuation", bars[-1].end, tod)
    # Preserve other named setups while making the unknown family explicit.
    return Confirmation("detected", family, "setup family has no confirmation rule", None, tod)
