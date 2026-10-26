"""Direction-aware confluence features, tiers, and the card gate.

Ace's playbook is not in this repo. Pattern detectors are loose on purpose.
Each one returns a match from 0 to 1, the best score across the configured
timeframes and across the bar-count, depth, and ATR bands in Settings.
Inside a core band the match is 1. Between the core and the outer edge it
fades in a straight line to 0. Outside the outer edge it is 0. There is no
single bar count, depth, or ATR that is the only template.

9 EMA pullback (calls): on each timeframe, price has extended off that
frame's open by an ATR amount inside the EMA-move band, and a prior close
has been beyond the 9 EMA. The latest bar then pulls back toward the EMA.
The pullback's length and depth have to sit in the shared bar and depth
bands, the low has to come within the tag band of the EMA, and the close
has to hold the EMA (a small pierce fades rather than failing). Puts mirror:
extension down, tag with the high, close holds on or below the EMA.

Flag (calls): an impulse of one or more bars whose net ATR size sits in the
impulse band, then a pause whose length sits in the pause band and whose
high-low range sits in the range band, holding above the impulse open.
A one-bar pause is the outer edge and scores 0. Puts mirror.

W (calls only): two swing lows separated by a bar count in the shared band,
within the twin band of each other, with a bounce between them in the depth
band, and the close lifting off the second low. A flat series has no swing
and scores 0. M is the mirror and scores for puts only. The other direction
scores 0, so a call does not earn the bearish pattern.

Fashionably Late: the extension off the session open sits in the late ATR
band, and a pullback or a flag is already matching. The score is the
extension match times the better of those two. A straight grind has no
pullback or flag, so it scores 0 and does not re-trigger a held move.

Consolidation and bounce use the same bar and ATR bands. A tight window
that breaks in the trade direction, or a tag of VWAP that holds, earns a
partial match when the tag or the window is only partway into the band.

Points are tier weight times the match. These detectors are not a separate
bypass rule. The bypass posts when the tiered sum clears bypass_confluence_min,
the data-derived entry filters pass, and the base/blocker table allows it.
A pattern is a stock trigger only on the bar its match first reaches
pattern_trigger_min.
"""
from math import floor, isfinite

from .config import CONFLUENCE_TABLE, Settings
from .models import ET


# Chart patterns that can open a bypass on the bar they first clear the trigger.
PATTERN_FEATURES = (
    "ema9_pullback", "flag", "w_pattern", "m_pattern", "fashionably_late",
)


def tier_weight(settings: Settings, tier: str) -> float:
    if tier == "primary":
        return float(settings.tier_weight_primary)
    if tier == "secondary":
        return float(settings.tier_weight_secondary)
    return float(settings.tier_weight_tertiary)


def match_value(value) -> float:
    """Clamp a feature to the 0-1 match that multiplies its tier weight."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


def tiered_points(flags: dict, settings: Settings) -> tuple[dict, float, int]:
    """Points per feature, the tiered sum, and how many features are on.

    A full match earns the whole tier weight. A partial match earns that
    fraction. A missing feature earns nothing and does not zero the rest.
    """
    points = {}
    for name, tier in CONFLUENCE_TABLE:
        weight = tier_weight(settings, tier)
        points[name] = weight * match_value(flags.get(name))
    score = sum(points.values())
    count = sum(1 for name, _tier in CONFLUENCE_TABLE if match_value(flags.get(name)) > 0)
    return points, score, count


def pattern_match(flags: dict) -> float:
    """Best chart-pattern match. Held level breaks are not in this number."""
    return max((match_value(flags.get(name)) for name in PATTERN_FEATURES), default=0.0)


def card_gate(side, volume, spread, flags: dict, points: float, settings: Settings) -> dict:
    """Base items, blockers, and overrides. Direction is never overridden.

    Missing volume is lifted only when the tiered score clears
    base_override_points. A slightly wide spread is lifted by that score
    or by PM break + RVOL + volume together.
    """
    base = {
        "direction": int(side in {"CALL", "PUT"}),
        "volume": int(volume is not None and volume >= settings.base_volume_min),
    }
    overrides = []
    missing = [name for name, ok in base.items() if not ok]
    if "volume" in missing and points >= settings.base_override_points:
        missing.remove("volume")
        overrides.append("volume")
    blockers = []
    if spread is not None and spread > settings.blocker_spread:
        blockers.append("wide_spread")
        strong = bool(flags.get("pm_break") and flags.get("rvol") and base["volume"])
        if points >= settings.blocker_spread_override_points or strong:
            overrides.append("wide_spread")
    blocking = [name for name in blockers if name not in overrides]
    blocked_by = list(missing) + blocking
    return {
        "base": base,
        "blockers": blockers,
        "overrides": overrides,
        "blocked_by": blocked_by,
        "post_ok": not blocked_by,
    }


def primary_strength_marker(score, minimum: float) -> str | None:
    """⚡ when the model score clears LOTTO_STRONG_SCORE.

    `minimum` is the hook. A later fitted cut replaces it. 🔥 stays the
    extreme title and is not this marker.
    """
    if score is None or not isinstance(score, (int, float)) or not isfinite(score):
        return None
    if score >= minimum:
        return "⚡"
    return None


def card_freshness(snap, option, trigger=None) -> dict:
    """quote_age_s, bar_age_s, and min_since_trigger. None of these block."""
    quote_age = None
    if option is not None and getattr(option, "quote_time", None) is not None:
        quote_age = round((snap.at - option.quote_time).total_seconds(), 3)
    bar_age = None
    if snap.bars:
        bar_age = round((snap.at - snap.bars[-1].end).total_seconds(), 3)
    side = option.side if option is not None else "CALL"
    return {
        "quote_age_s": quote_age,
        "bar_age_s": bar_age,
        "min_since_trigger": minutes_since_trigger(snap, trigger, side),
    }


def minutes_since_trigger(snap, trigger, side: str):
    if trigger is None or not isinstance(trigger, (int, float)) or not isfinite(trigger) or not snap.bars:
        return None
    direction = 1 if side == "CALL" else -1
    for bar in snap.bars:
        touched = bar.high >= trigger if direction == 1 else bar.low <= trigger
        if touched:
            return round((snap.at - bar.end).total_seconds() / 60.0, 3)
    return None


def timeframe_bars(bars, minutes: int) -> list[dict]:
    """Completed clock-aligned bars from 9:30. A partial bucket is dropped."""
    buckets: dict[int, list] = {}
    for bar in bars:
        local = bar.end.astimezone(ET)
        minute = local.hour * 60 + local.minute
        open_min = 9 * 60 + 30
        if minute <= open_min:
            continue
        index = (minute - open_min - 1) // minutes
        buckets.setdefault(index, []).append(bar)
    if not buckets:
        return []
    last_index = max(buckets)
    built = []
    for index in sorted(buckets):
        group = buckets[index]
        end_min = open_min + (index + 1) * minutes
        last = group[-1].end.astimezone(ET)
        if index == last_index and (last.hour * 60 + last.minute) < end_min:
            continue
        built.append({
            "open": group[0].open,
            "high": max(bar.high for bar in group),
            "low": min(bar.low for bar in group),
            "close": group[-1].close,
        })
    return built


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    ema = values[0]
    series = []
    for value in values:
        ema = value if not series else (alpha * value + (1 - alpha) * ema)
        series.append(ema)
    return series


def _atr(snap) -> float | None:
    atr = getattr(snap, "prior_atr21", None)
    if isinstance(atr, (int, float)) and isfinite(atr) and atr > 0:
        return float(atr)
    atr = getattr(snap, "prior_atr", None)
    if isinstance(atr, (int, float)) and isfinite(atr) and atr > 0:
        return float(atr)
    return None


def band_score(value, outer_min, core_min, core_max, outer_max) -> float:
    """1 inside the core, linear fade to 0 at the outer edges, 0 outside."""
    if value is None or not isinstance(value, (int, float)) or not isfinite(value):
        return 0.0
    value = float(value)
    if core_min <= value <= core_max:
        return 1.0
    if outer_min <= value < core_min:
        span = core_min - outer_min
        return (value - outer_min) / span if span else 0.0
    if core_max < value <= outer_max:
        span = outer_max - core_max
        return (outer_max - value) / span if span else 0.0
    return 0.0


def fade_below(distance, core, outer) -> float:
    """1 at or under core, 0 at or beyond outer, linear between."""
    if distance is None or not isinstance(distance, (int, float)) or not isfinite(distance):
        return 0.0
    if distance <= core:
        return 1.0
    if distance >= outer or outer <= core:
        return 0.0
    return (outer - distance) / (outer - core)


def fade_in(value, zero_at, full_at) -> float:
    """0 at or under zero_at, 1 at or above full_at, linear between."""
    if value is None or not isinstance(value, (int, float)) or not isfinite(value):
        return 0.0
    if value <= zero_at or full_at <= zero_at:
        return 0.0
    if value >= full_at:
        return 1.0
    return (value - zero_at) / (full_at - zero_at)


def pattern_timeframes(settings: Settings) -> list[int]:
    frames = []
    for part in str(settings.pattern_timeframes).split(","):
        part = part.strip()
        if part:
            frames.append(int(part))
    return frames or [2, 5, 10]


def _frames(minutes, settings: Settings) -> list[int]:
    if minutes:
        return [int(minutes)]
    return pattern_timeframes(settings)


def _clip(score: float) -> float:
    if not isfinite(score):
        return 0.0
    return round(max(0.0, min(1.0, score)), 4)


def detect_ema_pullback(bars, minutes, direction: int, atr: float | None, settings: Settings) -> float:
    """Best 0-1 pullback to the 9 EMA across the configured ranges."""
    if atr is None or atr <= 0 or direction not in (1, -1):
        return 0.0
    best = 0.0
    period = int(settings.ema_pullback_period)
    window = max(int(settings.pattern_bars_outer_max), 1)
    for frame in _frames(minutes, settings):
        framed = timeframe_bars(bars, frame)
        if len(framed) < period + 1:
            continue
        closes = [bar["close"] for bar in framed]
        emas = _ema(closes, period)
        prior = framed[:-1]
        recent = prior[-window:]
        offset = len(prior) - len(recent)
        if direction == 1:
            local = max(range(len(recent)), key=lambda i: recent[i]["close"])
            favorable = recent[local]["close"] - framed[0]["open"]
            depth = (recent[local]["close"] - framed[-1]["low"]) / atr
            near = abs(framed[-1]["low"] - emas[-1]) / atr
            hold_gap = (framed[-1]["close"] - emas[-1]) / atr
        else:
            local = min(range(len(recent)), key=lambda i: recent[i]["close"])
            favorable = framed[0]["open"] - recent[local]["close"]
            depth = (framed[-1]["high"] - recent[local]["close"]) / atr
            near = abs(emas[-1] - framed[-1]["high"]) / atr
            hold_gap = (emas[-1] - framed[-1]["close"]) / atr
        pull_bars = len(framed) - 1 - (offset + local)
        move = band_score(
            favorable / atr, settings.ema_move_outer_min, settings.ema_move_core_min,
            settings.ema_move_core_max, settings.ema_move_outer_max)
        bars_s = band_score(
            pull_bars, settings.pattern_bars_outer_min, settings.pattern_bars_core_min,
            settings.pattern_bars_core_max, settings.pattern_bars_outer_max)
        depth_s = band_score(
            depth, settings.pattern_depth_outer_min, settings.pattern_depth_core_min,
            settings.pattern_depth_core_max, settings.pattern_depth_outer_max)
        tag = fade_below(near, settings.ema_tag_core, settings.ema_tag_outer)
        hold = 1.0 if hold_gap >= 0 else fade_below(-hold_gap, 0.0, settings.ema_tag_outer)
        best_gap = 0.0
        for index in range(max(period - 1, offset), len(prior)):
            best_gap = max(best_gap, direction * (framed[index]["close"] - emas[index]) / atr)
        if best_gap >= settings.ema_tag_core:
            extended = 1.0
        elif best_gap <= 0:
            extended = 0.0
        else:
            extended = best_gap / settings.ema_tag_core
        best = max(best, min(move, bars_s, depth_s, tag, hold, extended))
    return _clip(best)


def detect_flag(bars, minutes, direction: int, atr: float | None, settings: Settings) -> float:
    """Best 0-1 impulse-and-pause across bar counts, depth, and ATR."""
    if atr is None or atr <= 0 or direction not in (1, -1):
        return 0.0
    best = 0.0
    pause_outer = max(int(settings.flag_pause_outer_max), 1)
    impulse_outer = max(int(settings.pattern_bars_outer_max), 1)
    for frame in _frames(minutes, settings):
        framed = timeframe_bars(bars, frame)
        count = len(framed)
        for pause_len in range(1, min(pause_outer, count - 1) + 1):
            bars_s = band_score(
                pause_len, settings.flag_pause_outer_min, settings.flag_pause_core_min,
                settings.flag_pause_core_max, settings.flag_pause_outer_max)
            if bars_s <= 0:
                continue
            pause = framed[-pause_len:]
            width = (max(bar["high"] for bar in pause) - min(bar["low"] for bar in pause)) / atr
            range_s = fade_below(width, settings.flag_range_core, settings.flag_range_outer)
            if range_s <= 0:
                continue
            for impulse_len in range(1, impulse_outer + 1):
                start = count - pause_len - impulse_len
                if start < 0:
                    break
                impulse = framed[start:count - pause_len]
                net = direction * (impulse[-1]["close"] - impulse[0]["open"]) / atr
                impulse_s = band_score(
                    net, settings.flag_impulse_outer_min, settings.flag_impulse_core_min,
                    settings.flag_impulse_core_max, settings.flag_impulse_outer_max)
                if impulse_s <= 0:
                    continue
                origin = impulse[0]["open"]
                if direction == 1:
                    worst = min(bar["close"] - origin for bar in pause) / atr
                else:
                    worst = min(origin - bar["close"] for bar in pause) / atr
                hold = 1.0 if worst >= 0 else fade_below(-worst, 0.0, settings.pattern_twin_outer)
                length_s = 1.0 if impulse_len <= settings.pattern_bars_core_max else band_score(
                    impulse_len, 1, 1, settings.pattern_bars_core_max, settings.pattern_bars_outer_max)
                best = max(best, min(bars_s, range_s, impulse_s, hold, length_s))
    return _clip(best)


def _pivot_beat(framed, index: int, radius: int, direction: int):
    """How far a swing sticks out from its neighbors, in price. None if it cannot."""
    left = index - radius
    if left < 0 or radius < 1:
        return None
    end = min(len(framed) - 1, index + radius)
    if end == index and index < len(framed) - 1:
        return None
    others = [framed[j] for j in range(left, end + 1) if j != index]
    if not others:
        return None
    if direction == 1:
        return min(bar["low"] for bar in others) - framed[index]["low"]
    return framed[index]["high"] - max(bar["high"] for bar in others)


def _double_score(bars, minutes, direction: int, atr: float, settings: Settings) -> float:
    """Loose double bottom (direction 1) or double top (direction -1)."""
    best = 0.0
    outer = max(int(settings.pattern_bars_outer_max), 2)
    for frame in _frames(minutes, settings):
        framed = timeframe_bars(bars, frame)
        count = len(framed)
        if count < 5:
            continue
        first_right = max(2, count - outer - 1)
        for right in range(first_right, count):
            for radius in (1, 2):
                right_beat = _pivot_beat(framed, right, radius, direction)
                if right_beat is None or right_beat <= 0:
                    continue
                right_pivot = fade_in(right_beat / atr, 0.0, settings.pattern_depth_outer_min)
                if right + radius >= count:
                    right_pivot *= 0.75
                span = range(max(0, right - outer), right)
                for left in span:
                    separation = right - left
                    bars_s = band_score(
                        separation, settings.pattern_bars_outer_min, settings.pattern_bars_core_min,
                        settings.pattern_bars_core_max, settings.pattern_bars_outer_max)
                    if bars_s <= 0 or left < radius:
                        continue
                    left_beat = _pivot_beat(framed, left, radius, direction)
                    if left_beat is None or left_beat <= 0:
                        continue
                    left_pivot = fade_in(left_beat / atr, 0.0, settings.pattern_depth_outer_min)
                    middle = framed[left + 1:right]
                    if not middle:
                        continue
                    if direction == 1:
                        neck = max(bar["high"] for bar in middle)
                        base = max(framed[left]["low"], framed[right]["low"])
                        depth = (neck - base) / atr
                        twin = abs(framed[left]["low"] - framed[right]["low"]) / atr
                        lift = (framed[-1]["close"] - framed[right]["low"]) / atr
                    else:
                        neck = min(bar["low"] for bar in middle)
                        base = min(framed[left]["high"], framed[right]["high"])
                        depth = (base - neck) / atr
                        twin = abs(framed[left]["high"] - framed[right]["high"]) / atr
                        lift = (framed[right]["high"] - framed[-1]["close"]) / atr
                    depth_s = band_score(
                        depth, settings.pattern_depth_outer_min, settings.pattern_depth_core_min,
                        settings.pattern_depth_core_max, settings.pattern_depth_outer_max)
                    twin_s = fade_below(twin, settings.pattern_twin_core, settings.pattern_twin_outer)
                    lift_s = fade_in(lift, 0.0, settings.pattern_depth_outer_min)
                    best = max(best, min(right_pivot, left_pivot, bars_s, depth_s, twin_s, lift_s))
    return _clip(best)


def detect_w(bars, minutes, direction: int, atr: float | None, settings: Settings) -> float:
    """Bullish double bottom. Puts do not earn this row; they earn M."""
    if atr is None or atr <= 0 or direction != 1:
        return 0.0
    return _double_score(bars, minutes, 1, atr, settings)


def detect_m(bars, minutes, direction: int, atr: float | None, settings: Settings) -> float:
    """Bearish double top. Calls do not earn this row; they earn W."""
    if atr is None or atr <= 0 or direction != -1:
        return 0.0
    return _double_score(bars, minutes, -1, atr, settings)


def detect_fashionably_late(bars, minutes, direction: int, atr: float | None, settings: Settings,
                            ema: float | None = None, flag: float | None = None) -> float:
    """Extension in the late band times the better pullback or flag. A grind is 0."""
    if atr is None or atr <= 0 or direction not in (1, -1) or not bars:
        return 0.0
    extension = direction * (bars[-1].close - bars[0].open) / atr
    ext = band_score(
        extension, settings.late_outer_min, settings.late_core_min,
        settings.late_core_max, settings.late_outer_max)
    if ext <= 0:
        return 0.0
    if ema is None:
        ema = detect_ema_pullback(bars, minutes, direction, atr, settings)
    if flag is None:
        flag = detect_flag(bars, minutes, direction, atr, settings)
    return _clip(ext * max(ema, flag))


def loose_pattern_scores(bars, direction: int, atr: float | None, settings: Settings) -> dict:
    """One 0-1 score per pattern family. The best timeframe is already inside."""
    ema = detect_ema_pullback(bars, None, direction, atr, settings)
    flag = detect_flag(bars, None, direction, atr, settings)
    return {
        "ema9_pullback": ema,
        "flag": flag,
        "w_pattern": detect_w(bars, None, direction, atr, settings),
        "m_pattern": detect_m(bars, None, direction, atr, settings),
        "fashionably_late": detect_fashionably_late(bars, None, direction, atr, settings, ema, flag),
    }


def _consolidation_score(bars, direction: int, atr: float, settings: Settings) -> float:
    """Tight window, then a break in the trade direction. Partial when either is partial."""
    best = 0.0
    outer = max(int(settings.pattern_bars_outer_max), 1)
    longest = min(outer, len(bars) - 1)
    for length in range(1, longest + 1):
        bars_s = band_score(
            length, settings.pattern_bars_outer_min, settings.pattern_bars_core_min,
            settings.pattern_bars_core_max, settings.pattern_bars_outer_max)
        if bars_s <= 0:
            continue
        window = bars[-(length + 1):-1]
        if not window:
            continue
        width = (max(bar.high for bar in window) - min(bar.low for bar in window)) / atr
        tight = fade_below(width, settings.flag_range_core, settings.flag_range_outer)
        if direction == 1:
            broke = (bars[-1].close - max(bar.high for bar in window)) / atr
        else:
            broke = (min(bar.low for bar in window) - bars[-1].close) / atr
        best = max(best, min(bars_s, tight, fade_in(broke, 0.0, settings.pattern_depth_outer_min)))
    return _clip(best)


def _bounce_score(bars, direction: int, vwap: float, atr: float, settings: Settings) -> float:
    """Tag of VWAP inside the lookback band, with the close holding the far side."""
    best = 0.0
    outer = max(int(settings.pattern_bars_outer_max), 1)
    look = bars[-outer:]
    for length in range(1, len(look) + 1):
        bars_s = band_score(
            length, settings.pattern_bars_outer_min, settings.pattern_bars_core_min,
            settings.pattern_bars_core_max, settings.pattern_bars_outer_max)
        if bars_s <= 0:
            continue
        window = look[-length:]
        if direction == 1:
            tag = abs(min(bar.low for bar in window) - vwap) / atr
            hold_gap = (bars[-1].close - vwap) / atr
        else:
            tag = abs(max(bar.high for bar in window) - vwap) / atr
            hold_gap = (vwap - bars[-1].close) / atr
        hold = 1.0 if hold_gap >= 0 else fade_below(-hold_gap, 0.0, settings.ema_tag_outer)
        tag_s = fade_below(tag, settings.ema_tag_core, settings.ema_tag_outer)
        best = max(best, min(bars_s, tag_s, hold))
    return _clip(best)


def _held(bars, level, direction: int) -> bool:
    if level is None or not isinstance(level, (int, float)) or not isfinite(level) or len(bars) < 2:
        return False
    return all(direction * (bar.close - level) > 0 for bar in bars[-2:])


def _own_return(bars, direction: int):
    if len(bars) < 6:
        return None
    reference = bars[-6].close
    if reference <= 0:
        return None
    return direction * (bars[-1].close / reference - 1)


def confluence_flags(snap, option, metrics: dict | None = None) -> dict:
    """On/off for every table row. Missing inputs stay off. Puts mirror calls."""
    metrics = metrics or {}
    direction = -1 if option is not None and option.side == "PUT" else 1
    bars = list(snap.bars)
    flags = {name: 0 for name, _tier in CONFLUENCE_TABLE}
    if not bars:
        return flags
    close = bars[-1].close
    pm = snap.premarket_high if direction == 1 else snap.premarket_low
    pd = snap.prior_high if direction == 1 else snap.prior_low
    flags["pm_break"] = int(_held(bars, pm, direction))
    flags["pd_break"] = int(_held(bars, pd, direction))
    if len(bars) >= 3:
        prior_extreme = max(bar.high for bar in bars[:-1]) if direction == 1 else min(bar.low for bar in bars[:-1])
        beyond = close > prior_extreme if direction == 1 else close < prior_extreme
        minute = _session_minute(bars[-1])
        flags["hod_break"] = int(bool(beyond and minute is not None and minute >= 15))
    rvol = max(metrics.get("pace_rvol") or 0, metrics.get("local_rvol_5m") or 0)
    fallback_base = getattr(snap, "fallback_rvol_base", None)
    sessions = getattr(snap, "fallback_rvol_sessions", None)
    if sessions is not None and sessions >= 10 and fallback_base not in (None, 0):
        rvol = max(rvol, sum(bar.volume for bar in bars) / fallback_base)
    moved = direction * (close - bars[0].open) > 0
    floor = metrics.get("_conf_rvol_min", 1)
    flags["rvol"] = int(bool(moved and rvol >= floor))
    flags["volume"] = int(bars[-1].volume > 0)
    if option is not None:
        same = [item.volume for item in snap.options if item.side == option.side]
        lead = option.volume > 0 and same and option.volume >= max(same)
        rate = metrics.get("contract_volume_rate_5m") or 0
        cluster = metrics.get("cluster_size") or 0
        flags["option_flow"] = int(bool(lead or rate > 0 or cluster >= 2))
    if len(bars) >= 6:
        prior_high = max(bar.high for bar in bars[:-1])
        prior_low = min(bar.low for bar in bars[:-1])
        flags["range_break"] = int(close > prior_high if direction == 1 else close < prior_low)
    own = _own_return(bars, direction)
    spy = snap.market_return_5m
    sector = snap.peer_return_5m
    rs_spy = own is not None and spy is not None and own - direction * spy > 0
    rs_sector = own is not None and sector is not None and own - direction * sector > 0
    flags["rs"] = int(bool(rs_spy or rs_sector))
    flags["sector"] = int(bool(sector is not None and direction * sector > 0))
    spy_move = snap.market_return_from_open if snap.market_return_from_open is not None else spy
    flags["spy"] = int(bool(spy_move is not None and direction * spy_move > 0))
    vwap = metrics.get("vwap")
    if vwap is not None and direction * (close - vwap) > 0:
        flags["vwap"] = 1
    atr = _atr(snap)
    move = metrics.get("move_from_open_atr")
    if move is None and atr:
        move = direction * (close - bars[0].open) / atr
    flags["atr_move"] = int(bool(isinstance(move, (int, float)) and move >= 0.25))
    box_high = getattr(snap, "box5_high", None)
    box_low = getattr(snap, "box5_low", None)
    box = box_high if direction == 1 else box_low
    if box is not None and direction * (close - box) > 0:
        flags["box5"] = 1
    view = _settings_view(metrics)
    if atr and len(bars) >= 4:
        flags["consolidation"] = _consolidation_score(bars, direction, atr, view)
    if vwap is not None and atr:
        flags["bounce"] = _bounce_score(bars, direction, vwap, atr, view)
    elif vwap is not None:
        recent = bars[-5:]
        if direction == 1:
            flags["bounce"] = int(any(bar.low <= vwap for bar in recent) and close > vwap)
        else:
            flags["bounce"] = int(any(bar.high >= vwap for bar in recent) and close < vwap)
    if atr:
        flags.update(loose_pattern_scores(bars, direction, atr, view))
        flags["mtf_flag"] = int(_mtf(bars, direction, pm, pd))
    return flags


def _settings_view(metrics):
    settings = metrics.get("_settings")
    if settings is not None:
        return settings
    return _FALLBACK_SETTINGS


_FALLBACK_SETTINGS = Settings()


def _session_minute(bar):
    local = bar.end.astimezone(ET)
    minute = local.hour * 60 + local.minute
    open_min = 9 * 60 + 30
    if minute <= open_min:
        return None
    return minute - open_min - 1


def _mtf(bars, direction: int, pm, pd) -> bool:
    framed = timeframe_bars(bars, 5)
    if not framed:
        return False
    five = framed[-1]["close"]
    one = bars[-1].close
    for level in (pm, pd):
        if level is None or not isinstance(level, (int, float)) or not isfinite(level):
            continue
        if direction * (one - level) > 0 and direction * (five - level) > 0:
            return True
    return False


def confluence_flags_for(snap, option, metrics, settings: Settings) -> dict:
    """Same flags, with this card's settings on the detectors and the RVOL floor."""
    metrics = dict(metrics or {})
    metrics["_settings"] = settings
    metrics["_conf_rvol_min"] = settings.conf_rvol_min
    flags = confluence_flags(snap, option, metrics)
    # RVOL feature uses the config floor, not the literal 1 baked above.
    direction = -1 if option is not None and option.side == "PUT" else 1
    bars = list(snap.bars)
    if bars:
        rvol = max(metrics.get("pace_rvol") or 0, metrics.get("local_rvol_5m") or 0)
        sessions = getattr(snap, "fallback_rvol_sessions", None)
        base = getattr(snap, "fallback_rvol_base", None)
        if sessions is not None and sessions >= 10 and base not in (None, 0):
            rvol = max(rvol, sum(bar.volume for bar in bars) / base)
        moved = direction * (bars[-1].close - bars[0].open) > 0
        flags["rvol"] = int(bool(moved and rvol >= settings.fb_rvol_min))
        flags["volume"] = int(bars[-1].volume >= settings.base_volume_min)
        move = metrics.get("move_from_open_atr")
        atr = _atr(snap)
        if move is None and atr:
            move = direction * (bars[-1].close - bars[0].open) / atr
        flags["atr_move"] = int(bool(isinstance(move, (int, float)) and move >= settings.conf_atr_min))
    return flags


def spread_fraction(option) -> float | None:
    if option is None or not option.ask:
        return None
    return (option.ask - option.bid) / option.ask


def apply_card_layers(candidate, settings: Settings, s12_ok: bool, model_score: float) -> bool:
    """Log tiers, base, blockers, freshness, and the ⚡ marker.

    S12 already decided `s12_ok` from its own score. Confluence does not
    change that decision. The base/blocker table can still hold the post.
    The returned qualifying flag is what the scanner posts.
    """
    snap, option = candidate.snapshot, candidate.option
    flags = confluence_flags_for(snap, option, candidate.metrics, settings)
    points, score, count = tiered_points(flags, settings)
    volume = snap.bars[-1].volume if snap.bars else 0
    gate = card_gate(option.side, volume, spread_fraction(option), flags, score, settings)
    trigger = None
    if candidate.setup is not None:
        trigger = candidate.setup.trigger
    elif candidate.metrics.get("handoff_trigger") is not None:
        trigger = candidate.metrics.get("handoff_trigger")
    ages = card_freshness(snap, option, trigger)
    stale = ages["quote_age_s"] is not None and ages["quote_age_s"] > settings.stale_quote_seconds
    penalty = float(settings.stale_quote_penalty) if stale else 0.0
    marked = model_score - penalty
    card_score = round(marked + score, 2)
    post_ok = bool(s12_ok) and gate["post_ok"]
    metrics = candidate.metrics
    metrics.update(ages)
    metrics["confluence_flags"] = flags
    metrics["confluence_points"] = points
    metrics["confluence_score"] = score
    metrics["confluence_count"] = count
    metrics["confluence_total"] = len(CONFLUENCE_TABLE)
    metrics["base"] = gate["base"]
    metrics["blockers"] = gate["blockers"]
    metrics["overrides"] = gate["overrides"]
    metrics["gate_blocked_by"] = gate["blocked_by"]
    metrics["gate_post"] = int(gate["post_ok"])
    metrics["s12_qualifying"] = int(bool(s12_ok))
    metrics["model_score"] = model_score
    metrics["card_score"] = card_score
    if stale:
        metrics["stale_quote"] = 1
        if "stale quote" not in candidate.reasons:
            candidate.reasons = list(candidate.reasons) + ["stale quote"]
        candidate.parts["stale_quote"] = round(-penalty, 2)
    if post_ok:
        marker = primary_strength_marker(marked, settings.strong_score)
        if marker:
            metrics["strength_marker"] = marker
    for name, value in points.items():
        candidate.parts[f"conf_{name}"] = value
    candidate.parts["confluence"] = score
    candidate.metrics = metrics
    candidate.score = card_score
    candidate.qualifying = post_ok
    return post_ok


def whole_number_crossed(previous: float, price: float, direction: int) -> bool:
    if direction == 1:
        return floor(price) > floor(previous)
    return floor(previous) > floor(price)
