"""Ace volume rows and the option-volume count. Scored, never a gate.

Primary keys are Ace's names. VOL_* aliases are applied by volume_trend.
Every ratio stays a float. A missing curve falls through the chain and is logged.
Only bars at or before the current bar are read. Prior-day TOD smoothing
uses yesterday's later minutes, which are already known. It does not read
today's future bars.

The 3.5-minute SPY window is not computed here. See volume_trend.reference_close.
"""
from __future__ import annotations

import logging
from math import isfinite

from .models import ET

LOG = logging.getLogger(__name__)

CALL_LEVELS = (
    ("premarket_high", "PMH"),
    ("prior_high", "PDH"),
    ("high_5d", "R5H"),
    ("high_md", "MDH"),
    ("week_high", "WKH"),
)
PUT_LEVELS = (
    ("premarket_low", "PML"),
    ("prior_low", "PDL"),
    ("low_5d", "R5L"),
    ("low_md", "MDL"),
    ("week_low", "WKL"),
)
STACK_LEVELS = {"PMH", "PDH", "R5H", "WKH", "PML", "PDL", "R5L", "WKL"}
RANGE_LEVELS = {"PDH", "R5H", "PDL", "R5L"}


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def minute_of(bar):
    end = getattr(bar, "end", None)
    if end is None:
        return None
    local = end.astimezone(ET)
    minute = (local.hour * 60 + local.minute) - (9 * 60 + 31)
    if 0 <= minute <= 389:
        return minute
    return None


def day_average(bars, index: int, premarket, widen_under: int):
    """Mean volume before `index`. Early bars pull in 04:00 premarket volume."""
    vols = [float(bar.volume) for bar in bars[:index]]
    if index < int(widen_under):
        vols = [float(value) for value in (premarket or []) if _num(value) is not None] + vols
    if not vols:
        return None
    return sum(vols) / len(vols)


def ratio_score(ratio, full: float) -> float:
    number = _num(ratio)
    if number is None or full <= 0:
        return 0.0
    return _clamp(number / full)


def band_score(ratio, lo: float, hi: float, climax: float, zero_at: float) -> float:
    """Full credit inside the band. Partial below it. Taper from climax to zero_at."""
    number = _num(ratio)
    if number is None or number <= 0:
        return 0.0
    if number < lo:
        return _clamp(number / lo) if lo > 0 else 0.0
    if number <= climax:
        return 1.0
    if zero_at <= climax or number >= zero_at:
        return 0.0
    return _clamp(1.0 - (number - climax) / (zero_at - climax))


def range_atr_score(bar_range, atr, rules) -> float:
    atr = _num(atr)
    span = _num(bar_range)
    if atr is None or atr <= 0 or span is None or span < 0:
        return 0.0
    return band_score(
        span / atr,
        float(rules["RANGE_ATR_LO"]),
        float(rules["RANGE_ATR_HI"]),
        float(rules["RANGE_ATR_HI"]),
        float(rules["RANGE_ATR_ZERO"]),
    )


def close_loc_score(bar, direction: int, rules) -> float:
    span = bar.high - bar.low
    if span <= 0:
        return 0.0
    loc = (bar.close - bar.low) / span
    top = float(rules["CLOSE_LOC_TOP"])
    if direction >= 0:
        if loc >= top:
            return 1.0
        floor = top - 0.30
        if loc <= floor:
            return 0.0
        return _clamp((loc - floor) / (top - floor))
    bottom = 1.0 - top
    if loc <= bottom:
        return 1.0
    ceiling = bottom + 0.30
    if loc >= ceiling:
        return 0.0
    return _clamp((ceiling - loc) / (ceiling - bottom))


def room_score(used, rules) -> float:
    number = _num(used)
    if number is None:
        return 0.0
    full = float(rules["ROOM_ATR_MAX"])
    zero_at = float(rules["ROOM_ATR_ZERO"])
    if number <= full:
        return 1.0
    if number >= zero_at or zero_at <= full:
        return 0.0
    return _clamp((zero_at - number) / (zero_at - full))


def sust_score(share, rules) -> float:
    number = _num(share)
    if number is None:
        return 0.0
    start = float(rules["SUST_SCORE_FROM"])
    if number <= start:
        return 0.0
    span = 1.0 - start
    if span <= 0:
        return 1.0
    return _clamp((number - start) / span)


def _levels(levels, direction: int):
    table = CALL_LEVELS if direction >= 0 else PUT_LEVELS
    out = []
    for key, name in table:
        value = _num((levels or {}).get(key) if isinstance(levels, dict) else getattr(levels, key, None))
        if value is not None:
            out.append((name, value))
    return out


def _beyond(close, level, direction: int) -> bool:
    return direction * (close - level) > 0


def fresh_break(bars, levels, direction: int, window: int):
    """Latest fresh close-cross inside the last `window` bars that is still held.

    The scan stops at the last bar in `bars`. Later bars are not visible.
    """
    rows = list(bars or [])
    usable = _levels(levels, direction)
    if len(rows) < 2 or not usable:
        return None
    start = max(1, len(rows) - int(window))
    found = None
    for index in range(start, len(rows)):
        names = []
        chosen = None
        for name, level in usable:
            prev = rows[index - 1].close
            close = rows[index].close
            crossed = _beyond(close, level, direction) and not _beyond(prev, level, direction)
            held = _beyond(rows[-1].close, level, direction)
            if crossed and held:
                names.append((name, level))
        if names:
            name_only = tuple(name for name, _level in names)
            nearest = min(names, key=lambda item: abs(rows[-1].close - item[1]))
            found = {"index": index, "level": nearest[1], "names": name_only}
    return found


def held_level(bars, levels, direction: int):
    """1 when the last close is through PMH/PDH/R5H/WKH. Puts use the lows."""
    rows = list(bars or [])
    if not rows:
        return 0.0
    close = rows[-1].close
    for name, level in _levels(levels, direction):
        if name not in STACK_LEVELS:
            continue
        if _beyond(close, level, direction):
            return 1.0
    return 0.0


def tod_ratio(volume, minute, tod, day_ratio):
    """Volume vs a K-day same-minute norm. Falls through 10, 5, 3, any, then the day."""
    for key, source in ((10, "TOD_10"), (5, "TOD_5"), (3, "TOD_3"), ("any", "TOD_ANY")):
        curve = (tod or {}).get(key) or {}
        norm = _num(curve.get(minute)) if minute is not None else None
        if norm is not None and norm > 0:
            return float(volume) / norm, source
    if day_ratio is not None and _num(day_ratio) is not None:
        return float(day_ratio), "DAY"
    return 1.0, "NEUTRAL"


def specific_tod(volume, minute, tod, key, day_ratio):
    """One K first, then the shorter curves. Never blank."""
    order = {
        10: ((10, "TOD_10"), (5, "TOD_5"), (3, "TOD_3"), ("any", "TOD_ANY")),
        5: ((5, "TOD_5"), (3, "TOD_3"), ("any", "TOD_ANY")),
        3: ((3, "TOD_3"), ("any", "TOD_ANY")),
    }[key]
    for which, source in order:
        curve = (tod or {}).get(which) or {}
        norm = _num(curve.get(minute)) if minute is not None else None
        if norm is not None and norm > 0:
            return float(volume) / norm, source
    if day_ratio is not None and _num(day_ratio) is not None:
        return float(day_ratio), "DAY"
    return 1.0, "NEUTRAL"


def rvol_cum_chain(cum_now, curves, bar_vs_day, full_day, minute):
    """10d → 5d → 3d → any same-time → bar vs day → linear time-scaled → 1.0."""
    for key, source in ((10, "RVOL_CUM_10"), (5, "RVOL_CUM_5"), (3, "RVOL_CUM_3"), ("any", "RVOL_ANY")):
        base = _num((curves or {}).get(key))
        if base is not None and base > 0:
            return float(cum_now) / base, source
    if bar_vs_day is not None and _num(bar_vs_day) is not None:
        return float(bar_vs_day), "BAR_VS_DAY"
    full = _num(full_day)
    if full is not None and full > 0 and minute is not None:
        expected = full * ((int(minute) + 1) / 390.0)
        if expected > 0:
            return float(cum_now) / expected, "RVOL_CUM_TIMESCALE"
    return 1.0, "NEUTRAL"


def _norm_at(minute, tod, fallback):
    for key in (10, 5, 3, "any"):
        curve = (tod or {}).get(key) or {}
        norm = _num(curve.get(minute)) if minute is not None else None
        if norm is not None and norm > 0:
            return norm
    return fallback


def _bar_ratio(bar, tod, fallback):
    norm = _norm_at(minute_of(bar), tod, fallback)
    if norm is None or norm <= 0:
        return None
    return bar.volume / norm


def sustained_share(bars, tod, width: int, threshold: float, consecutive: bool, fallback):
    rows = list(bars or [])
    if width <= 0 or len(rows) < width:
        return 0.0
    window = rows[-width:]
    ratios = []
    for bar in window:
        ratio = _bar_ratio(bar, tod, fallback)
        ratios.append(0.0 if ratio is None else ratio)
    if consecutive:
        run = 0
        for ratio in reversed(ratios):
            if ratio + 1e-12 >= threshold:
                run += 1
            else:
                break
        return run / float(width)
    hits = sum(1 for ratio in ratios if ratio + 1e-12 >= threshold)
    return hits / float(width)


def five_minute_share(bars, tod, width: int, threshold: float, fallback):
    """Share of the last `width` completed 5-minute buckets. No partial, no future bar."""
    rows = list(bars or [])
    buckets = []
    for start in range(0, len(rows) - 4, 5):
        chunk = rows[start:start + 5]
        if len(chunk) < 5:
            break
        buckets.append(chunk)
    if len(buckets) < width or width <= 0:
        return 0.0
    hits = 0
    for chunk in buckets[-width:]:
        volume = sum(bar.volume for bar in chunk)
        norms = []
        for bar in chunk:
            norm = _norm_at(minute_of(bar), tod, fallback)
            if norm is not None and norm > 0:
                norms.append(norm)
        if not norms:
            continue
        if volume / sum(norms) + 1e-12 >= threshold:
            hits += 1
    return hits / float(width)


def slope_score(bars, index, width: int, full: float) -> float:
    if index is None or width <= 1 or index < width:
        return 0.0
    chunk = list(bars[index - width:index])
    first = float(chunk[0].volume)
    last = float(chunk[-1].volume)
    if first <= 0 or last <= first:
        return 0.0
    if full <= 0:
        return 1.0
    return _clamp(((last / first) - 1.0) / full)


def hold_score(bars, found, direction: int, atr, tod, rules, fallback) -> float:
    """0 until W bars after the break exist. Those bars are already in `bars`."""
    if not found:
        return 0.0
    width = int(rules["HOLD_W"])
    index = found["index"]
    after = list(bars)[index + 1:index + 1 + width]
    if len(after) < width:
        return 0.0
    level = found["level"]
    atr = _num(atr)
    buffer = 0.0 if atr is None or atr <= 0 else float(rules["HOLD_ATR_BUFFER"]) * atr
    if direction >= 0:
        if any(bar.low < level - buffer for bar in after):
            return 0.0
        held = sum(1 for bar in after if bar.close > level) / float(width)
    else:
        if any(bar.high > level + buffer for bar in after):
            return 0.0
        held = sum(1 for bar in after if bar.close < level) / float(width)
    ratios = []
    for bar in after:
        ratio = _bar_ratio(bar, tod, fallback)
        if ratio is not None:
            ratios.append(ratio)
    mean_ratio = sum(ratios) / len(ratios) if ratios else 1.0
    thresh = float(rules["HOLD_VOL_MIN"])
    volume_factor = 1.0 if thresh <= 0 else min(1.0, mean_ratio / thresh)
    close_line = float(rules["HOLD_CLOSE_FRAC"])
    close_factor = 1.0 if close_line <= 0 else min(1.0, held / close_line)
    return _clamp(close_factor * volume_factor)


def pb_resume_score(bars, found, direction: int, tod, rules, fallback) -> float:
    if not found:
        return 0.0
    level = found["level"]
    dry_max = float(rules["PB_DRY_MAX"])
    resume_min = float(rules["PB_RESUME_MIN"])
    saw_dry = False
    best = 0.0
    for bar in list(bars)[found["index"] + 1:]:
        ratio = _bar_ratio(bar, tod, fallback)
        if ratio is None:
            continue
        retrace = direction * (bar.close - level) <= 0
        if retrace and ratio < dry_max:
            saw_dry = True
            continue
        if saw_dry and _beyond(bar.close, level, direction):
            if ratio + 1e-12 >= resume_min:
                return 1.0
            if ratio >= 1.0 and resume_min > 1:
                best = max(best, _clamp((ratio - 1.0) / (resume_min - 1.0)))
    return best


def _cum_curves(snap, minute):
    out = {}
    mapping = (
        (10, "rvol_base", "prior_full_days", 10),
        (5, "vt_rvol_base_5", "vt_rvol_sessions_5", 5),
        (3, "vt_rvol_base_3", "vt_rvol_sessions_3", 3),
        ("any", "vt_rvol_base_any", "vt_rvol_sessions_any", 1),
    )
    for key, base_attr, sessions_attr, need in mapping:
        sessions = getattr(snap, sessions_attr, None)
        base = getattr(snap, base_attr, None)
        if key == 10 and (sessions is None or _num(base) is None):
            base = getattr(snap, "fallback_rvol_base", None)
            sessions = getattr(snap, "fallback_rvol_sessions", None)
        try:
            sessions = int(sessions) if sessions is not None else 0
        except (TypeError, ValueError):
            sessions = 0
        number = _num(base)
        if sessions >= need and number is not None:
            out[key] = number
    tod = getattr(snap, "vt_tod", None) or {}
    if not out and tod:
        # A test can pass cumulative norms on vt_tod under "cum".
        cum = tod.get("cum") or {}
        for key in (10, 5, 3, "any"):
            number = _num((cum.get(key) or {}).get(minute))
            if number is not None:
                out[key] = number
    return out


def counted_volume(bar, direction: int, signed: bool) -> float:
    """Share volume. Signed volume keeps only bars that close with the card."""
    try:
        vol = float(bar.volume)
    except (TypeError, ValueError, AttributeError):
        return 0.0
    if vol < 0 or vol != vol:
        return 0.0
    if not signed:
        return vol
    try:
        opened = float(bar.open)
        close = float(bar.close)
    except (TypeError, ValueError, AttributeError):
        return 0.0
    if direction > 0 and close > opened:
        return vol
    if direction < 0 and close < opened:
        return vol
    return 0.0


def measure_volume(bars, direction: int, levels, atr, tod=None, premarket=None,
                   cum_curves=None, full_day=None, rules=None, symbol: str = "",
                   signed: bool = False) -> dict:
    """Raw ratios plus 0-1 scores. `bars` is the causal list ending at the card."""
    from .volume_trend import VOLUME_TREND_RULES
    rules = rules or VOLUME_TREND_RULES
    direction = 1 if int(direction) >= 0 else -1

    def _vol(bar) -> float:
        return counted_volume(bar, direction, signed)
    rows = list(bars or [])
    tod = tod or {}
    widen = int(rules["DAY_EXTEND_UNDER"])
    full = float(rules["ratio_score_full"])
    found = fresh_break(rows, levels, direction, int(rules["brk_window_bars"]))
    index = len(rows) - 1 if rows else None
    use = found["index"] if found else index
    raw = {}
    flags = {}
    sources = {}
    if use is None or not rows:
        flags["LEVEL_BREAK"] = 0.0
        flags["BRK_VOL_PDH_R5"] = 0.0
        return {"flags": flags, "raw": raw, "sources": sources}

    day_ratio_value = None
    average = day_average(rows, use, premarket, widen)
    if average and average > 0:
        day_ratio_value = _vol(rows[use]) / average
    else:
        day_ratio_value = 1.0
    raw["BRK_VS_DAY"] = float(day_ratio_value)
    flags["BRK_VS_DAY"] = ratio_score(day_ratio_value, full)

    for width, key in ((5, "BU_5_DAY"), (3, "BU_3_DAY")):
        if use >= width:
            pre = sum(_vol(rows[j]) for j in range(use - width, use)) / width
            base_index = use - width
            base = day_average(rows, base_index, premarket, widen)
            if base is None or base <= 0:
                base = day_average(rows, use, premarket, widen)
            ratio = pre / base if base and base > 0 else 1.0
        else:
            ratio = 1.0
        raw[key] = float(ratio)
        flags[key] = ratio_score(ratio, full)

    prior10 = day_average(rows, use, premarket, widen)
    if use >= int(rules["brk_prior_bars"]):
        window = [rows[j].volume for j in range(use - int(rules["brk_prior_bars"]), use)]
        prior10 = sum(window) / len(window)
    vs10 = _vol(rows[use]) / prior10 if prior10 and prior10 > 0 else 1.0
    raw["BRK_VS_10"] = float(vs10)
    flags["BRK_VS_10_ge1.5"] = 1.0 if vs10 >= float(rules["brk_min_ratio"]) else 0.0
    flags["BU_5_DAY_ge1"] = 1.0 if raw["BU_5_DAY"] >= float(rules["bu_min_ratio"]) else 0.0

    minute = minute_of(rows[use])
    for key in (10, 5, 3):
        ratio, source = specific_tod(_vol(rows[use]), minute, tod, key, day_ratio_value)
        name = f"BRK_TOD_{key}"
        raw[name] = float(ratio)
        flags[name] = ratio_score(ratio, full)
        sources[name] = source
        LOG.info("BRK_TOD symbol=%s K=%s source=%s ratio=%.4f", symbol, key, source, ratio)
    flags["BRK_BAND"] = band_score(
        raw["BRK_TOD_10"],
        float(rules["BRK_BAND_LO"]),
        float(rules["BRK_BAND_HI"]),
        float(rules["BRK_CLIMAX"]),
        float(rules["BRK_CLIMAX_ZERO"]),
    )
    bar = rows[use]
    flags["RANGE_ATR"] = range_atr_score(bar.high - bar.low, atr, rules)
    flags["CLOSE_LOC"] = close_loc_score(bar, direction, rules)
    atr_value = _num(atr)
    if rows and atr_value is not None and atr_value > 0:
        used = direction * (rows[-1].close - rows[0].open) / atr_value
    else:
        used = None
    raw["ROOM"] = used
    flags["ROOM"] = room_score(used, rules)

    fallback = day_ratio_value
    for n in (5, 3):
        flags[f"BU_SLOPE_{n}"] = slope_score(rows, use if found else None, n, float(rules["BU_SLOPE_FULL"]))
        for k in (10, 5, 3):
            if use < n:
                ratio = 1.0
                source = "NEUTRAL"
            else:
                chunk = rows[use - n:use]
                norms = []
                for item in chunk:
                    norm = _num(((tod or {}).get(k) or {}).get(minute_of(item)))
                    if norm is None or norm <= 0:
                        norm = fallback if fallback and fallback > 0 else None
                    if norm is not None and norm > 0:
                        norms.append(_vol(item) / norm)
                ratio = sum(norms) / len(norms) if norms else 1.0
                source = f"TOD_{k}" if norms else "NEUTRAL"
            name = f"BU_{n}_TOD_{k}"
            raw[name] = float(ratio)
            flags[name] = ratio_score(ratio, full)
            sources[name] = source

    cum = sum(_vol(bar) for bar in rows)
    minute_now = minute_of(rows[-1])
    curves = cum_curves if cum_curves is not None else {}
    chain, chain_source = rvol_cum_chain(cum, curves, fallback, full_day, minute_now)
    raw["RVOL_CUM"] = float(chain)
    flags["RVOL_CUM"] = ratio_score(chain, full)
    sources["RVOL_CUM"] = chain_source
    LOG.info("RVOL_CUM symbol=%s source=%s ratio=%.4f", symbol, chain_source, chain)
    for key, label in ((10, "RVOL_CUM_10"), (5, "RVOL_CUM_5"), (3, "RVOL_CUM_3")):
        base = _num(curves.get(key))
        if base is not None and base > 0:
            ratio = cum / base
            source = label
        else:
            ratio, source = chain, chain_source
        raw[label] = float(ratio)
        flags[label] = ratio_score(ratio, full)
        sources[label] = source
    scaled, scaled_source = rvol_cum_chain(cum, {}, None, full_day, minute_now)
    if full_day and minute_now is not None and _num(full_day) and full_day > 0:
        expected = float(full_day) * ((int(minute_now) + 1) / 390.0)
        scaled = cum / expected if expected > 0 else 1.0
        scaled_source = "RVOL_CUM_TIMESCALE"
    raw["RVOL_CUM_TIMESCALE"] = float(scaled)
    flags["RVOL_CUM_TIMESCALE"] = ratio_score(scaled, full)
    sources["RVOL_CUM_TIMESCALE"] = scaled_source

    hot = float(rules["rvol_hot"])
    dead_lo = float(rules["rvol_dead_lo"])
    dead_hi = float(rules["rvol_dead_hi"])
    if chain_source == "NEUTRAL":
        flags["RVOL_CHAIN_ge1.5"] = 0.0
        flags["RVOL_CHAIN_1_15"] = 0.0
    else:
        flags["RVOL_CHAIN_ge1.5"] = 1.0 if chain >= hot else 0.0
        flags["RVOL_CHAIN_1_15"] = 1.0 if dead_lo <= chain < dead_hi else 0.0

    thresh = float(rules["SUST_THRESH"])
    width = int(rules["SUST_W_1M"])
    share = sustained_share(
        rows, tod, width, thresh, bool(rules.get("SUST_CONSECUTIVE")), fallback)
    raw["SUST_VOL_TOD_1M"] = share
    flags["SUST_VOL_TOD_1M"] = sust_score(share, rules)
    share5 = five_minute_share(rows, tod, int(rules["SUST_W_5M"]), thresh, fallback)
    raw["SUST_VOL_TOD_5M"] = share5
    flags["SUST_VOL_TOD_5M"] = sust_score(share5, rules)

    flags["HOLD_ON_VOL"] = hold_score(rows, found, direction, atr, tod, rules, fallback)
    flags["PB_RESUME"] = pb_resume_score(rows, found, direction, tod, rules, fallback)
    flags["LEVEL_BREAK"] = held_level(rows, levels, direction)
    names = set(found["names"]) if found else set()
    volume_hot = (
        raw["BRK_TOD_10"] >= float(rules["STACK_BRK_TOD_MIN"])
        or raw["BRK_VS_10"] >= float(rules["STACK_BRK_VS_MIN"])
    )
    flags["BRK_VOL_PDH_R5"] = 1.0 if volume_hot and names & RANGE_LEVELS else 0.0
    return {"flags": flags, "raw": raw, "sources": sources, "break_index": None if not found else found["index"]}


def adjacent_strike_count(options, picked, minimum: float, radius: int = 2) -> float:
    """How many strikes within ±radius of the pick have enough contract volume.

    Missing chain, missing pick, or a missing volume is not present. It is not an error.
    """
    if picked is None or options is None:
        return 0.0
    try:
        side = picked.side
        expiry = picked.expiry
        strike = float(picked.strike)
    except (AttributeError, TypeError, ValueError):
        return 0.0
    same = []
    for option in options:
        try:
            if option.side != side or option.expiry != expiry:
                continue
            same.append(option)
        except AttributeError:
            continue
    strikes = sorted({float(option.strike) for option in same} | {strike})
    if strike not in strikes:
        return 0.0
    center = strikes.index(strike)
    window = {price for offset, price in enumerate(strikes) if abs(offset - center) <= int(radius)}
    counted = set()
    for option in same:
        price = float(option.strike)
        if price not in window:
            continue
        number = _num(getattr(option, "volume", None))
        if number is None:
            continue
        if number + 1e-9 >= float(minimum):
            counted.add(price)
    return float(len(counted))


def volume_steady(history, cycles: int) -> float:
    """1 when the last `cycles` volumes are not falling. Missing history is 0."""
    if not history or cycles <= 1:
        return 0.0
    recent = list(history)[-int(cycles):]
    if len(recent) < int(cycles):
        return 0.0
    numbers = [_num(value) for value in recent]
    if any(value is None for value in numbers):
        return 0.0
    if all(numbers[i] >= numbers[i - 1] for i in range(1, len(numbers))):
        return 1.0
    return 0.0


def option_rows(options, picked, history, rules) -> dict:
    try:
        count = adjacent_strike_count(
            options, picked, float(rules["OPT_ADJ_VOL_MIN"]), int(rules["OPT_ADJ_RADIUS"]))
    except (TypeError, ValueError):
        LOG.info("option adjacent volume unreadable; counting it as not present")
        count = 0.0
    steady = volume_steady(history, int(rules["OPT_STEADY_CYCLES"]))
    full = max(1.0, float(rules["STACK_OPT_MIN_STRIKES"]))
    return {
        "OPT_VOL_ADJ_STRIKES": float(count),
        "OPT_VOL_ADJ": _clamp(count / full),
        "OPT_VOL_STEADY": steady,
    }
