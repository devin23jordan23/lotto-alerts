"""Directional stock evidence required before an option idea can post.

Uses only completed bars and frozen premarket/prior-day levels. Option flow
can improve contract choice, but cannot manufacture a stock setup.
"""
from math import isfinite
from statistics import median


def _ema(values, period):
    value = values[0]
    weight = 2 / (period + 1)
    for price in values[1:]:
        value += weight * (price - value)
    return value


def stock_motion_gate(snap, side):
    """Suppress only the nearly stationary cards identified in path replay.

    Historical 15-minute bid replay: from-open <0.10 ATR AND recent-three-bar
    <0.10 ATR held 84/463 cards, including 13 of 113 +40% winners and 4 of
    35 +100% winners. Missing ATR is unknown, so it cannot veto a card.
    """
    bars = snap.bars
    atr = getattr(snap, "prior_atr21", None) or snap.prior_atr
    if side not in {"CALL", "PUT"} or len(bars) < 4 or not atr or not isfinite(atr) or atr <= 0:
        return True, None
    direction = 1 if side == "CALL" else -1
    from_open = direction * (bars[-1].close - bars[0].open) / atr
    recent = direction * (bars[-1].close - bars[-4].close) / atr
    if from_open < .10 and recent < .10:
        return False, {"move_from_open_atr": round(from_open, 3),
                       "recent_move_atr": round(recent, 3)}
    return True, {"move_from_open_atr": round(from_open, 3),
                  "recent_move_atr": round(recent, 3)}


def stock_thesis(snap, side):
    """Return (evidence, reason). Evidence is empty when the stock is not ready."""
    bars = snap.bars
    if side not in {"CALL", "PUT"} or len(bars) < 4:
        return {}, "stock thesis: incomplete bars"
    direction = 1 if side == "CALL" else -1
    atr = getattr(snap, "prior_atr21", None) or snap.prior_atr
    if not atr or not isfinite(atr) or atr <= 0:
        return {}, "stock thesis: prior ATR unavailable"
    current = bars[-1]
    opened = bars[0].open
    from_open = direction * (current.close - opened) / atr
    recent = direction * (current.close - bars[-4].close) / atr
    body = direction * (current.close - current.open)
    if from_open < .20 or recent < .10 or body <= 0:
        return {}, "stock thesis: no directional stock push"

    # Compare each of the last three completed bars with levels known before
    # that bar. A touch or a break that happened much earlier does not count.
    frozen = ((snap.premarket_high, snap.prior_high) if direction == 1
              else (snap.premarket_low, snap.prior_low))
    level = None
    level_value = None
    for index in range(max(1, len(bars) - 3), len(bars)):
        bar, previous = bars[index], bars[index - 1]
        candidates = [("PM", frozen[0]), ("PD", frozen[1]),
                      ("HOD" if direction == 1 else "LOD",
                       max(item.high for item in bars[:index]) if direction == 1
                       else min(item.low for item in bars[:index]))]
        for name, value in candidates:
            if value and isfinite(value) and direction * (previous.close - value) <= 0 < direction * (bar.close - value):
                level = name
                level_value = value
                break
        if level:
            break
    local_before = bars[-6:-1] if len(bars) >= 6 else bars[:-1]
    local_level = max(b.high for b in local_before) if direction == 1 else min(b.low for b in local_before)
    local_break = direction * (bars[-2].close - local_level) <= 0 < direction * (current.close - local_level)

    closes = [bar.close for bar in bars]
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    prior_ema9 = _ema(closes[:-1], 9)
    curl = (len(bars) >= 9 and from_open >= .40 and
            direction * (ema9 - ema21) > 0 and
            direction * (ema9 - prior_ema9) > 0 and
            bars[-2].low <= prior_ema9 <= bars[-2].high and
            direction * (current.close - ema9) > 0 and
            direction * (current.close - bars[-2].close) > 0)

    prior_volumes = [bar.volume for bar in bars[-11:-1] if bar.volume > 0]
    volume_ratio = current.volume / median(prior_volumes) if len(prior_volumes) >= 3 else None
    volume_burst = volume_ratio is not None and volume_ratio >= 1.5
    strong_break = from_open >= .50 and bool(level)
    if not (level or local_break or curl):
        return {}, "stock thesis: no fresh break or trend pullback"
    if not (volume_burst or strong_break):
        return {}, "stock thesis: breakout volume absent"
    setup = (f"{level}_BREAK" if level else "LOCAL_RANGE_BREAK" if local_break else "TREND_PULLBACK")
    trigger = level_value if level else local_level if local_break else ema9
    invalidation = min(b.low for b in bars[-3:]) if direction == 1 else max(b.high for b in bars[-3:])
    return {"setup": setup, "move_from_open_atr": round(from_open, 3),
            "recent_move_atr": round(recent, 3),
            "stock_volume_ratio": round(volume_ratio, 3) if volume_ratio is not None else None,
            "fresh_level": level or "", "volume_burst": int(volume_burst),
            "trigger": round(trigger, 4), "invalidation": round(invalidation, 4)}, ""
