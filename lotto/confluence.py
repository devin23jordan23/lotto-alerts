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

Points come from the weights book: weight for the side times the 0-1 match.
A row with requires_min_other_positive stays at 0 until that many other
positive features are already on the card. A row with requires_min_conf stays
at 0 until that many of {level break, RVOL >= 1.5, SPY with the trade,
option volume >= 200} are on. A pattern is a stock trigger only on the bar
its match first reaches pattern_trigger_min.

v4c EMA rows are always measured and logged. The book sets most of them to
weight 0 so they stay available for shadow grading. The 2-minute 21 EMA
dip-and-reclaim and the 5/10-minute put dip-and-reclaim are the rows with
a shipped weight, and only after their gates.
"""
import logging
from math import floor, isfinite

from .config import CONFLUENCE_TABLE, Settings
from .models import ET

LOG = logging.getLogger(__name__)

# Logged on each scored card so a later fit can use the live detector outputs.
ACE_SPY_FLAGS = (
    "a_drive", "a_flushpop", "a_open_reclaim", "a_pmx_reclaim",
    "a_vwap_slap", "a_wm", "a_spike_rev", "spy10",
)


# Chart patterns that can open a bypass on the bar they first clear the trigger.
PATTERN_FEATURES = (
    "ema9_pullback", "flag", "w_pattern", "m_pattern", "fashionably_late",
)

# v4c feature names. Weights stay in the book; these are only the keys.
DRC_EMA21_2M = "drc_ema21_2m_dip_reclaim"
DRP_510 = "drp_510_dip_reclaim"
E1_EMA9_2M_PRE10 = "e1_ema9_2m_pullin_pre10"
EMA21_PULLIN_2M = "ema21_pullin_2m"
EMA21_PULLIN_5M = "ema21_pullin_5m"
EMA21_PULLIN_10M = "ema21_pullin_10m"
EMA9_2M_DIP = "ema9_2m_dip_reclaim"
DIP_RECLAIM_5_10_CALLS = "dip_reclaim_5_10m_calls"
SHADOW_EMA_FEATURES = (
    E1_EMA9_2M_PRE10, EMA21_PULLIN_2M, EMA21_PULLIN_5M, EMA21_PULLIN_10M,
    EMA9_2M_DIP, DIP_RECLAIM_5_10_CALLS,
)
# Freshness of a dip-and-reclaim, in minutes, by chart frame.
_DIP_FRESH_MINUTES = {2: 10, 5: 15, 10: 20}


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


def card_gate(side, volume, spread, settings: Settings, bid=None, option_volume_5m=None) -> dict:
    """Direction, stock volume, spread, bid, and 5-minute option volume.

    Nothing in the score lifts a miss. Spread is (ask-bid)/mid. Exactly the
    spread cap passes. A bid at the floor blocks. Exactly the volume floor passes.
    """
    base = {
        "direction": int(side in {"CALL", "PUT"}),
        "volume": int(volume is not None and volume >= settings.base_volume_min),
    }
    missing = [name for name, ok in base.items() if not ok]
    blockers = []
    if spread is None or not isinstance(spread, (int, float)) or not isfinite(spread) or spread > settings.blocker_spread:
        blockers.append("wide_spread")
    if bid is None or not isinstance(bid, (int, float)) or not isfinite(bid) or bid <= settings.blocker_bid:
        blockers.append("low_bid")
    if (option_volume_5m is None or not isinstance(option_volume_5m, (int, float))
            or not isfinite(option_volume_5m) or option_volume_5m < settings.blocker_option_volume_5m):
        blockers.append("option_volume")
    blocked_by = list(missing) + blockers
    return {
        "base": base,
        "blockers": blockers,
        "overrides": [],
        "blocked_by": blocked_by,
        "post_ok": not blocked_by,
    }


def primary_strength_marker(positive_features, minimum: float, score=None, min_score: float = 20) -> str | None:
    """⚡ when the score clears min_score and enough features are positive.

    Pass score=None to check the count only. 🔥 stays the extreme title and
    is not this marker.
    """
    if score is not None:
        try:
            scored = float(score)
        except (TypeError, ValueError):
            return None
        if not isfinite(scored) or scored < float(min_score):
            return None
    try:
        count = float(positive_features)
    except (TypeError, ValueError):
        return None
    if not isfinite(count):
        return None
    if count >= minimum:
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


def detect_ema_pullback(bars, minutes, direction: int, atr: float | None, settings: Settings,
                        period: int | None = None, frames: list[int] | None = None) -> float:
    """Best 0-1 pullback to an EMA across the configured ranges.

    `period` defaults to the 9 EMA. `frames` defaults to the minutes argument
    or the configured timeframes.
    """
    if atr is None or atr <= 0 or direction not in (1, -1):
        return 0.0
    best = 0.0
    period = int(period or settings.ema_pullback_period)
    window = max(int(settings.pattern_bars_outer_max), 1)
    chosen = list(frames) if frames is not None else _frames(minutes, settings)
    for frame in chosen:
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


def _before_ten(bars) -> bool:
    """The latest bar ends before 10:00 ET."""
    if not bars:
        return False
    local = bars[-1].end.astimezone(ET)
    return (local.hour, local.minute, local.second, local.microsecond) < (10, 0, 0, 0)


def detect_ema9_2m_before_10(bars, direction: int, atr: float | None, settings: Settings) -> float:
    """2-minute 9 EMA pull-in. 0 once the bar is 10:00 ET or later."""
    if not _before_ten(bars):
        return 0.0
    return detect_ema_pullback(bars, 2, direction, atr, settings, period=9, frames=[2])


def detect_ema21_pullin(bars, direction: int, atr: float | None, settings: Settings) -> float:
    """21 EMA pull-in. Best 0-1 match on the 2-, 5-, and 10-minute frames."""
    return detect_ema_pullback(bars, None, direction, atr, settings, period=21, frames=[2, 5, 10])


def _nia_trend(framed, emas, index: int, direction: int) -> bool:
    """EMA with the trade versus 3 bars ago, and 60% of the prior 8 closes with it."""
    if index < 8:
        return False
    if direction * (emas[index] - emas[index - 3]) <= 0:
        return False
    held = 0
    for prior in range(index - 8, index):
        if direction * (framed[prior]["close"] - emas[prior]) > 0:
            held += 1
    return held / 8 >= 0.6


def _nia_beyond(bar, ema: float, direction: int, atr: float) -> bool:
    """Low (calls) or high (puts) sits 0.05-0.5 ATR past the EMA."""
    if direction == 1:
        depth = (ema - bar["low"]) / atr
    else:
        depth = (bar["high"] - ema) / atr
    return 0.05 <= depth <= 0.5


def _nia_reclaim(framed, emas, index: int, direction: int) -> bool:
    """Close back across the EMA and beyond the prior bar's close."""
    if index < 1:
        return False
    close = framed[index]["close"]
    if direction * (close - emas[index]) <= 0:
        return False
    return direction * (close - framed[index - 1]["close"]) > 0


def nia_dip_reclaim(bars, frame: int, direction: int, atr: float | None, period: int) -> float:
    """1 inside Nia's dip-and-reclaim spec on this frame, else 0.

    The dip is 0.05-0.5 ATR past the EMA. The close comes back across the EMA,
    and past the prior close, within 1-4 bars. A same-bar wick counts as 1.
    The reclaim is fresh for 10 minutes on a 2-minute frame, 15 on 5-minute,
    and 20 on 10-minute. Only completed bars are read. The latest bar can be
    the reclaim.
    """
    if atr is None or atr <= 0 or direction not in (1, -1) or not bars:
        return 0.0
    framed = timeframe_bars(bars, int(frame))
    if len(framed) < 9:
        return 0.0
    closes = [bar["close"] for bar in framed]
    emas = _ema(closes, int(period))
    last = len(framed) - 1
    fresh = _DIP_FRESH_MINUTES.get(int(frame), int(frame) * 5)
    for reclaim_i in range(last, 7, -1):
        if (last - reclaim_i) * int(frame) > fresh:
            break
        if not _nia_trend(framed, emas, reclaim_i, direction):
            continue
        if not _nia_reclaim(framed, emas, reclaim_i, direction):
            continue
        for dip_i in range(reclaim_i, reclaim_i - 4, -1):
            if dip_i < 0:
                break
            if _nia_beyond(framed[dip_i], emas[dip_i], direction, float(atr)):
                return 1.0
    return 0.0


def detect_drc_ema21_2m(bars, direction: int, atr: float | None) -> float:
    """2-minute 21 EMA dip-and-reclaim. Calls only."""
    if direction != 1:
        return 0.0
    return nia_dip_reclaim(bars, 2, 1, atr, 21)


def detect_drp_510(bars, direction: int, atr: float | None) -> float:
    """5- or 10-minute dip-and-reclaim on the 9 or 21 EMA. Puts only."""
    if direction != -1:
        return 0.0
    best = 0.0
    for frame in (5, 10):
        for period in (9, 21):
            best = max(best, nia_dip_reclaim(bars, frame, -1, atr, period))
    return best


def detect_ema9_2m_dip(bars, direction: int, atr: float | None) -> float:
    """2-minute 9 EMA dip-and-reclaim. Both sides, for the shadow row."""
    if direction not in (1, -1):
        return 0.0
    return nia_dip_reclaim(bars, 2, direction, atr, 9)


def detect_dip_reclaim_5_10_calls(bars, direction: int, atr: float | None) -> float:
    """5- or 10-minute dip-and-reclaim on the 9 or 21 EMA. Calls only."""
    if direction != 1:
        return 0.0
    best = 0.0
    for frame in (5, 10):
        for period in (9, 21):
            best = max(best, nia_dip_reclaim(bars, frame, 1, atr, period))
    return best


def detect_ema_dip_reclaim(bars, direction: int, atr: float | None, settings: Settings) -> float:
    """Best Nia dip-and-reclaim on the 9 and 21 EMA, 2-, 5-, and 10-minute.

    Calls dip below the EMA and close back above it. Puts pop above and close
    back below. `settings` is unused; the spec is fixed.
    """
    del settings
    if atr is None or atr <= 0 or direction not in (1, -1) or not bars:
        return 0.0
    best = 0.0
    for period in (9, 21):
        for frame in (2, 5, 10):
            best = max(best, nia_dip_reclaim(bars, frame, direction, atr, period))
    return best


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
    rvol = _curve_rvol(snap, bars) or 0
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
    flags["atr_move"] = 0
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
        rvol = _curve_rvol(snap, bars) or 0
        moved = direction * (bars[-1].close - bars[0].open) > 0
        flags["rvol"] = int(bool(moved and rvol >= settings.fb_rvol_min))
        flags["volume"] = int(bars[-1].volume >= settings.base_volume_min)
        flags["atr_move"] = 0
    return flags


def _curve_rvol(snap, bars) -> float | None:
    """Cumulative volume over the mean curve of the prior 10 full days.

    The handoff curve and the fallback curve are the same series. Fewer than
    10 full days leaves this empty. A zero mean is RVOL 0.
    """
    if not bars:
        return None
    days = getattr(snap, "prior_full_days", None)
    base = getattr(snap, "rvol_base", None)
    if days is not None and days >= 10 and base and base > 0:
        return sum(bar.volume for bar in bars) / float(base)
    sessions = getattr(snap, "fallback_rvol_sessions", None)
    fallback = getattr(snap, "fallback_rvol_base", None)
    if sessions is None or sessions < 10:
        return None
    if fallback in (None, 0):
        return 0.0 if fallback == 0 else None
    return sum(bar.volume for bar in bars) / float(fallback)


def _on(value) -> float:
    return 1.0 if value else 0.0


def _causal_bars(bars, trigger_end=None) -> list:
    """Completed bars only, and nothing after the trigger minute."""
    rows = [bar for bar in (bars or []) if getattr(bar, "end", None) is not None]
    if trigger_end is not None:
        rows = [bar for bar in rows if bar.end <= trigger_end]
    return rows


def _session_vwap(bars) -> float | None:
    volume = 0.0
    total = 0.0
    for bar in bars:
        if bar.volume <= 0:
            continue
        volume += bar.volume
        total += (bar.high + bar.low + bar.close) / 3 * bar.volume
    if volume <= 0:
        return None
    return total / volume


# Research join (fq_s.py and andre_core.py): the feature is the match of the
# latest setup event whose trig_m is <= the card minute minus 1. That match
# persists. The last causal bar is the alert minute and is not searched.
# The attached research files store precomputed match columns. These cuts are
# those same ATR21 depths on completed 5-minute bars, which is the timeframe
# a 0.30/0.50 ATR event can actually print on. spy10 is not in that event
# table. It is the 10:00 pause/turn and scores only through the 10:15 bar.
DRIVE_NET_ATR = 0.35
DRIVE_EFFICIENCY = 0.55
FLUSH_DEPTH_ATR = 0.30
SPIKE_DEPTH_ATR = 0.50
SPIKE_REVERSAL_BARS = 3
OPEN_LOSS_ATR = 0.05
OPEN_RECLAIM_BARS = 3
SPY10_FIRST_MINUTE = 29  # bar ending 10:00 ET
SPY10_LAST_MINUTE = 44   # bar ending 10:15 ET


def _research_rows(bars, trigger_end=None) -> list:
    """Causal bars through the alert minute, with the alert minute removed."""
    rows = _causal_bars(bars, trigger_end)
    if len(rows) < 2:
        return []
    return rows[:-1]


def _clock_bars(bars, minutes: int) -> list[dict]:
    """Completed clock bars from 9:30, with the bucket's end timestamp."""
    buckets: dict[int, list] = {}
    open_min = 9 * 60 + 30
    for bar in bars:
        local = bar.end.astimezone(ET)
        minute = local.hour * 60 + local.minute
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
            "high": max(item.high for item in group),
            "low": min(item.low for item in group),
            "close": group[-1].close,
            "end": group[-1].end,
        })
    return built


def _vwap_through(bars, end) -> float | None:
    volume = 0.0
    total = 0.0
    for bar in bars:
        if bar.end > end:
            break
        if bar.volume <= 0:
            continue
        volume += bar.volume
        total += (bar.high + bar.low + bar.close) / 3 * bar.volume
    if volume <= 0:
        return None
    return total / volume


def _ready(direction: int, atr: float | None, rows) -> bool:
    return (atr is not None and atr > 0 and direction in (1, -1) and bool(rows))


def detect_ace_drive(bars, direction: int, atr: float | None, trigger_end=None) -> float:
    """Opening drive on 5-minute bars through 10:00. The match persists after.

    Net move at least 0.35 ATR21 and path efficiency at least 0.55. A later
    bar does not turn the flag off, and a bar after the alert does not count.
    """
    rows = _research_rows(bars, trigger_end)
    if not _ready(direction, atr, rows):
        return 0.0
    opening = [bar for bar in _clock_bars(rows, 5) if _session_minute_of(bar["end"]) is not None
               and _session_minute_of(bar["end"]) <= 29]
    if len(opening) < 2:
        return 0.0
    net = direction * (opening[-1]["close"] - opening[0]["open"])
    if net < DRIVE_NET_ATR * atr:
        return 0.0
    path = abs(opening[0]["close"] - opening[0]["open"])
    path += sum(abs(opening[index]["close"] - opening[index - 1]["close"]) for index in range(1, len(opening)))
    if path <= 0 or net / path < DRIVE_EFFICIENCY:
        return 0.0
    return 1.0


def _session_minute_of(moment):
    local = moment.astimezone(ET)
    minute = local.hour * 60 + local.minute
    open_min = 9 * 60 + 30
    if minute <= open_min:
        return None
    return minute - open_min - 1


def detect_open_reclaim(bars, direction: int, atr: float | None, trigger_end=None) -> float:
    """A 5-minute close loses the open, then a close reclaims it within 15 minutes.

    The reclaim is an event. It persists. A wick through the open, or a close
    that is merely back on the trade side with no paired loss, does not fire.
    """
    rows = _research_rows(bars, trigger_end)
    framed = _clock_bars(rows, 5)
    if not _ready(direction, atr, framed) or len(framed) < 2:
        return 0.0
    open_ = rows[0].open
    episode = False
    for index, bar in enumerate(framed):
        lost = direction * (open_ - bar["close"]) >= OPEN_LOSS_ATR * atr
        back = direction * (bar["close"] - open_) > 0
        if lost and not episode:
            window = framed[index + 1:index + 1 + OPEN_RECLAIM_BARS]
            if any(direction * (later["close"] - open_) > 0 for later in window):
                return 1.0
            episode = True
        elif back:
            episode = False
    return 0.0


def detect_pmx_reclaim(bars, direction: int, level, trigger_end=None) -> float:
    """A 5-minute close through the premarket extreme, after price was on the other side.

    The cross can be any bar before the alert. It persists. Calls use the
    premarket high. Puts use the premarket low.
    """
    rows = _research_rows(bars, trigger_end)
    if level is None or not isinstance(level, (int, float)) or not isfinite(level):
        return 0.0
    if direction not in (1, -1) or not rows:
        return 0.0
    framed = _clock_bars(rows, 5)
    if not framed:
        return 0.0
    armed = direction * (rows[0].open - level) <= 0
    for bar in framed:
        through = direction * (bar["close"] - level) > 0
        if through and armed:
            return 1.0
        if not through:
            armed = True
    return 0.0


def detect_flushpop(bars, direction: int, atr: float | None, trigger_end=None) -> float:
    """A 5-minute flush of at least 0.30 ATR21, then a close back through it.

    The pop can be that same 5-minute bar or a later one. The match persists.
    """
    rows = _research_rows(bars, trigger_end)
    framed = _clock_bars(rows, 5)
    if not _ready(direction, atr, framed) or len(framed) < 2:
        return 0.0
    for index in range(1, len(framed)):
        origin = framed[index - 1]["close"]
        bar = framed[index]
        if direction == 1:
            depth = (origin - bar["low"]) / atr
            popped = bar["close"] > origin
        else:
            depth = (bar["high"] - origin) / atr
            popped = bar["close"] < origin
        if depth < FLUSH_DEPTH_ATR:
            continue
        if popped:
            return 1.0
        for later in framed[index + 1:]:
            if direction == 1 and later["close"] > origin and later["close"] > bar["close"]:
                return 1.0
            if direction == -1 and later["close"] < origin and later["close"] < bar["close"]:
                return 1.0
    return 0.0


def detect_vwap_slap(bars, direction: int, atr: float | None, vwap=None, trigger_end=None) -> float:
    """Any earlier 5-minute bar tags VWAP and closes on the trade side.

    The tag does not have to be the alert bar. The match persists. Each bar
    uses the session VWAP known at that bar. A single current VWAP is not
    applied to earlier bars. `vwap` stays in the signature for the harness.
    """
    del vwap
    rows = _research_rows(bars, trigger_end)
    framed = _clock_bars(rows, 5)
    if not _ready(direction, atr, framed):
        return 0.0
    for bar in framed:
        level = _vwap_through(rows, bar["end"])
        if level is None:
            continue
        tagged = bar["low"] <= level <= bar["high"]
        if tagged and direction * (bar["close"] - level) > 0:
            return 1.0
    return 0.0


def detect_spike_rev(bars, direction: int, atr: float | None, trigger_end=None) -> float:
    """A 5-minute spike of at least 0.50 ATR21, then a reversal within three bars.

    The reversal is a later close back through the spike open and beyond the
    spike close. The match persists.
    """
    rows = _research_rows(bars, trigger_end)
    framed = _clock_bars(rows, 5)
    if not _ready(direction, atr, framed) or len(framed) < 2:
        return 0.0
    for index, spike in enumerate(framed[:-1]):
        if direction == 1:
            depth = (spike["open"] - spike["low"]) / atr
        else:
            depth = (spike["high"] - spike["open"]) / atr
        if depth < SPIKE_DEPTH_ATR:
            continue
        for later in framed[index + 1:index + 1 + SPIKE_REVERSAL_BARS]:
            if direction == 1 and later["close"] > spike["open"] and later["close"] > spike["close"]:
                return 1.0
            if direction == -1 and later["close"] < spike["open"] and later["close"] < spike["close"]:
                return 1.0
    return 0.0


def detect_spy10(spy_bars, direction: int, atr: float | None, trigger_end=None) -> float:
    """SPY 10:00 pause or turn, in the trade direction. Bars after the trigger are ignored.

    The cut is ATR21. The flag is on only while the alert bar ends from 10:00
    through 10:15 ET. It does not stay on for the rest of the day.
    """
    rows = _causal_bars(spy_bars, trigger_end)
    if atr is None or atr <= 0 or direction not in (1, -1) or len(rows) < 5:
        return 0.0
    minute = _session_minute(rows[-1])
    if minute is None or minute < SPY10_FIRST_MINUTE or minute > SPY10_LAST_MINUTE:
        return 0.0
    ten = None
    for bar in rows:
        local = bar.end.astimezone(ET)
        if (local.hour, local.minute) <= (10, 0):
            ten = bar
        else:
            break
    if ten is None:
        return 0.0
    if direction * (ten.close - rows[0].open) < 0.20 * atr:
        return 0.0
    after = [bar for bar in rows if bar.end >= ten.end]
    if direction == 1:
        adverse = ten.close - min(bar.low for bar in after)
        turned = rows[-1].close >= ten.close and any(bar.low <= ten.close - 0.10 * atr for bar in after)
        paused = adverse <= 0.35 * atr and rows[-1].close >= rows[0].open
    else:
        adverse = max(bar.high for bar in after) - ten.close
        turned = rows[-1].close <= ten.close and any(bar.high >= ten.close + 0.10 * atr for bar in after)
        paused = adverse <= 0.35 * atr and rows[-1].close <= rows[0].open
    return 1.0 if paused or turned else 0.0


def _level_break(snap, bars, direction: int) -> float:
    """1 when price has traded through PM or prior-day level, including a gap."""
    if not bars or direction not in (1, -1):
        return 0.0
    if direction == 1:
        levels = (getattr(snap, "premarket_high", None), getattr(snap, "prior_high", None))
    else:
        levels = (getattr(snap, "premarket_low", None), getattr(snap, "prior_low", None))
    for level in levels:
        if level is None or not isinstance(level, (int, float)) or not isfinite(level):
            continue
        if direction * (bars[0].open - level) > 0:
            return 1.0
        if any(direction * (bar.close - level) > 0 for bar in bars):
            return 1.0
    return 0.0


def additive_features(snap, option, settings: Settings, metrics=None, history=None, setup=None,
                      spy_bars=None) -> dict:
    """0-1 values for the weights book. Unlisted names still get measured.

    The book decides the points. EMA rows are logged even at weight 0.
    """
    metrics = dict(metrics or {})
    bars = list(snap.bars)
    side = option.side if option is not None and option.side == "PUT" else "CALL"
    direction = -1 if side == "PUT" else 1
    atr = _atr(snap)
    minute = _session_minute(bars[-1]) if bars else None
    room = push = None
    indexed = {}
    if bars:
        from .model_v import _minute_index, fallback_push, room_from_open
        for bar in bars:
            index = _minute_index(bar)
            if index is not None:
                indexed[index] = bar
        if indexed:
            minute = max(indexed)
            room = room_from_open(indexed, minute, side, atr)
            push = fallback_push(indexed, minute, side, atr)
    rvol = _curve_rvol(snap, bars)
    vol5 = None
    if option is not None:
        from .model_v import option_volume_5m
        vol5 = option_volume_5m(option, history, snap)
    ask = option.ask if option is not None else None
    bid = option.bid if option is not None else None
    sp5 = 0.0
    if ask and bid is not None and ask > 0:
        mid = (ask + bid) / 2
        if mid > 0:
            sp5 = _on((ask - bid) / mid <= 0.05)
    setup_name = setup.name if setup is not None else ""
    vwap_pb = 0.0
    vwap = metrics.get("vwap")
    if vwap is not None and atr and bars:
        vwap_pb = _bounce_score(bars, direction, vwap, atr, settings)
    # bounce is the reversal setup only. A VWAP tag stays on vwap_pb.
    bounce = 1.0 if setup_name in {"REVERSAL", "INTRADAY_REVERSAL"} else 0.0
    spy_open = snap.market_return_from_open
    spy_5 = snap.market_return_5m
    spy_with = spy_vs = 0.0
    if spy_open is not None and spy_5 is not None:
        spy_with = _on(direction * spy_open > 0 and direction * spy_5 > 0)
        spy_vs = _on(direction * spy_open < 0 and direction * spy_5 < 0)
    pull2 = detect_ema_pullback(bars, 2, direction, atr, settings, period=9, frames=[2]) if bars else 0.0
    pull5 = detect_ema_pullback(bars, 5, direction, atr, settings, period=9, frames=[5]) if bars else 0.0
    pull2 = 1.0 if pull2 >= 0.5 else 0.0
    pull5 = 1.0 if pull5 >= 0.5 else 0.0
    trigger_end = bars[-1].end if bars else None
    level = snap.premarket_high if direction == 1 else snap.premarket_low
    if spy_bars is None and getattr(snap, "symbol", "") == "SPY":
        spy_bars = bars
    w_score = detect_w(bars, None, direction, atr, settings) if bars else 0.0
    m_score = detect_m(bars, None, direction, atr, settings) if bars else 0.0
    gap = 0.0
    if bars and atr and snap.prior_close:
        gap = _on(direction * (bars[0].open - snap.prior_close) / atr >= 0.5)
    dte = None
    if option is not None:
        dte = (option.expiry - snap.at.astimezone(ET).date()).days
    return {
        "flow85": _on(vol5 is not None and vol5 >= 85),
        "flow200": _on(vol5 is not None and vol5 >= 200),
        "sp5": sp5,
        "ask_mid": _on(ask is not None and 0.50 <= ask <= 1.75),
        "bounce": bounce,
        "spy10": detect_spy10(spy_bars, direction, atr, trigger_end) if spy_bars else 0.0,
        "spy_with": spy_with,
        "vwap_pb": vwap_pb,
        "reclaim": _on(setup_name == "LEVEL_RECLAIM"),
        "a_drive": detect_ace_drive(bars, direction, atr, trigger_end) if bars else 0.0,
        "a_flushpop": detect_flushpop(bars, direction, atr, trigger_end) if bars else 0.0,
        "a_open_reclaim": detect_open_reclaim(bars, direction, atr, trigger_end) if bars else 0.0,
        "a_pmx_reclaim": detect_pmx_reclaim(bars, direction, level, trigger_end) if bars else 0.0,
        "a_vwap_slap": detect_vwap_slap(bars, direction, atr, None, trigger_end) if bars else 0.0,
        "a_wm": w_score if direction == 1 else m_score,
        "a_spike_rev": detect_spike_rev(bars, direction, atr, trigger_end) if bars else 0.0,
        "e1_ema9_2m_pullin_x_rvol15": pull2 if rvol is not None and rvol >= 1.5 else 0.0,
        "e2_ema9_5m_pullin": pull5,
        "ema50_touch": 0.0,
        "rvol_06_08": _on(rvol is not None and 0.6 <= rvol < 0.8),
        "late": _on(minute is not None and minute >= 300),
        "gap": gap,
        "room_hi": _on(room is not None and room > settings.fb_room_atr_max),
        "spy_vs": spy_vs,
        "room_used_le_1atr": _on(room is not None and room <= settings.fb_room_atr_max),
        "push30_ge_0.75atr": _on(push is not None and push >= settings.fb_push_atr_min),
        "w_bottom_ace": w_score if direction == 1 else 0.0,
        "dte1": _on(dte == 1),
        "rvol15": _on(rvol is not None and rvol >= 1.5),
        "level_break": _level_break(snap, bars, direction),
        DRC_EMA21_2M: detect_drc_ema21_2m(bars, direction, atr) if bars else 0.0,
        DRP_510: detect_drp_510(bars, direction, atr) if bars else 0.0,
        E1_EMA9_2M_PRE10: detect_ema9_2m_before_10(bars, direction, atr, settings) if bars else 0.0,
        EMA21_PULLIN_2M: detect_ema_pullback(bars, 2, direction, atr, settings, period=21, frames=[2]) if bars else 0.0,
        EMA21_PULLIN_5M: detect_ema_pullback(bars, 5, direction, atr, settings, period=21, frames=[5]) if bars else 0.0,
        EMA21_PULLIN_10M: detect_ema_pullback(bars, 10, direction, atr, settings, period=21, frames=[10]) if bars else 0.0,
        EMA9_2M_DIP: detect_ema9_2m_dip(bars, direction, atr) if bars else 0.0,
        DIP_RECLAIM_5_10_CALLS: detect_dip_reclaim_5_10_calls(bars, direction, atr) if bars else 0.0,
        "_vol5": vol5,
    }


def _log_ace_flags(snap, option, features) -> None:
    """One debug line per scored card: the live Ace and spy10 values."""
    parts = " ".join(
        f"{name}={float((features or {}).get(name) or 0):.4f}" for name in ACE_SPY_FLAGS
    )
    LOG.debug(
        "Ace flags symbol=%s side=%s contract=%s at=%s %s",
        getattr(snap, "symbol", ""),
        getattr(option, "side", ""),
        getattr(option, "symbol", ""),
        snap.at.isoformat(),
        parts,
    )


def spread_fraction(option) -> float | None:
    """Blocker spread. Research and the book use (ask-bid)/mid."""
    if option is None or not option.ask or option.bid is None:
        return None
    mid = (option.ask + option.bid) / 2
    if not mid:
        return None
    return (option.ask - option.bid) / mid


def apply_card_layers(candidate, settings: Settings, s12_ok: bool, model_score: float, history=None,
                      spy_bars=None) -> bool:
    """Score the weights book, roll a failing contract, and set the ⚡ marker.

    The additive score is the post decision. It has to clear the profile
    post_threshold, at least min_positive_features rows have to be at least
    0.5 and contribute points, and the contract that would post has to pass
    the blockers,
    including a real 5-minute volume reading. s12_ok is stored as a label
    and does not vote. A stale quote subtracts points and does not block
    by itself. The post line is not capped.
    """
    from .model_v import choose_fallback_contract
    from .weights import load_weights, score_path

    snap, option = candidate.snapshot, candidate.option
    volume = snap.bars[-1].volume if snap.bars else 0
    features = additive_features(
        snap, option, settings, candidate.metrics, history, candidate.setup, spy_bars=spy_bars)
    vol5 = features.get("_vol5")
    gate = card_gate(
        option.side, volume, spread_fraction(option), settings,
        bid=getattr(option, "bid", None), option_volume_5m=vol5,
    )
    if any(name in gate["blocked_by"] for name in ("wide_spread", "low_bid", "option_volume")):
        rolled = choose_fallback_contract(snap, option.side, settings, history)
        rolled_option = rolled.get("option")
        if rolled_option is not None and rolled_option is not option:
            option = rolled_option
            candidate.option = option
            features = additive_features(
                snap, option, settings, candidate.metrics, history, candidate.setup, spy_bars=spy_bars)
            vol5 = features.get("_vol5")
            gate = card_gate(
                option.side, volume, spread_fraction(option), settings,
                bid=option.bid, option_volume_5m=vol5,
            )
    features.pop("_vol5", None)
    _log_ace_flags(snap, option, features)
    book = load_weights(settings.weights_path)
    points, score, positive = score_path(features, option.side, book, "primary")
    trigger = None
    if candidate.setup is not None:
        trigger = candidate.setup.trigger
    elif candidate.metrics.get("handoff_trigger") is not None:
        trigger = candidate.metrics.get("handoff_trigger")
    ages = card_freshness(snap, option, trigger)
    stale = ages["quote_age_s"] is not None and ages["quote_age_s"] > settings.stale_quote_seconds
    penalty = float(settings.stale_quote_penalty) if stale else 0.0
    marked = score - penalty
    card_score = round(marked, 2)
    enough = positive >= settings.min_positive_features
    post_ok = gate["post_ok"] and marked >= settings.post_threshold and enough
    metrics = candidate.metrics
    metrics.update(ages)
    metrics["confluence_flags"] = features
    metrics["confluence_points"] = points
    metrics["confluence_score"] = score
    metrics["confluence_count"] = positive
    metrics["positive_features"] = positive
    metrics["min_positive_features"] = settings.min_positive_features
    metrics["positive_features_met"] = int(enough)
    metrics["confluence_total"] = len(points)
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
    marker = primary_strength_marker(
        positive, settings.strong_feature_min, marked, settings.post_threshold)
    if post_ok and marker:
        metrics["strength_marker"] = marker
    else:
        metrics.pop("strength_marker", None)
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
