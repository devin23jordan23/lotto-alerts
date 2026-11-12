"""Mode-1 score, gates, and setup triggers.

Active only when LOTTO_ANDRE_WEIGHTS_MODE=1. Flag 0 never calls this module
from the scoring path. Defaults and the weight table live in andre_config.
"""
from collections import deque
from datetime import timedelta
from statistics import median

from .andre_config import get, threshold, time_end, weight
from .groups import peers_for, primary_etf, symbols_to_quote
from .models import ET
from .patterns import Setup
from .resilience import flag


def andre_mode():
    return flag("LOTTO_ANDRE_WEIGHTS_MODE") == 1


# --- tape and history caches (filled by the Schwab poll when mode 1 is on) ---

class _Tape:
    def __init__(self):
        self.rows = {}
        self.universe = ()

    def clear(self):
        self.rows.clear()
        self.universe = ()

    def note_quote(self, symbol, quote):
        price, at = quote.get("price"), quote.get("at")
        if not price or not at:
            return
        row = self.rows.setdefault(symbol, {"samples": deque()})
        row["price"] = price
        if quote.get("open"):
            row["open"] = quote["open"]
        if quote.get("high"):
            row["high"] = quote["high"]
        if quote.get("low"):
            row["low"] = quote["low"]
        row["at"] = at
        volume = quote.get("volume")
        if volume is not None:
            previous = row.get("last_volume")
            if previous is not None and volume > previous:
                row["pv"] = row.get("pv", 0.0) + price * (volume - previous)
                row["dv"] = row.get("dv", 0.0) + (volume - previous)
            row["last_volume"] = volume
            if row.get("dv"):
                row["quote_vwap"] = row["pv"] / row["dv"]
        samples = row["samples"]
        if not samples or at > samples[-1][0]:
            samples.append((at, price))
        cutoff = at - timedelta(minutes=20)
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    def note_bars(self, symbol, bars, levels, atr, at):
        if not bars:
            return
        row = self.rows.setdefault(symbol, {"samples": deque()})
        volume = value = 0.0
        high = low = None
        for bar in bars:
            typical = (bar.high + bar.low + bar.close) / 3
            volume += bar.volume
            value += typical * bar.volume
            high = bar.high if high is None else max(high, bar.high)
            low = bar.low if low is None else min(low, bar.low)
        row.update({
            "bar_vwap": value / volume if volume else bars[-1].close,
            "bar_at": bars[-1].end, "open": bars[0].open, "high": high, "low": low,
            "price": bars[-1].close, "at": at or bars[-1].end,
        })
        if atr:
            row["atr"] = atr
        for source, dest in (("premarket_high", "pmh"), ("premarket_low", "pml"),
                             ("prior_high", "pdh"), ("prior_low", "pdl")):
            if levels and levels.get(source) is not None:
                row[dest] = levels[source]
        samples = row["samples"]
        if not samples or row["at"] > samples[-1][0]:
            samples.append((row["at"], row["price"]))

    def seed(self, symbol, **fields):
        row = self.rows.setdefault(symbol, {"samples": deque()})
        samples = fields.pop("samples", None)
        row.update(fields)
        if samples:
            row["samples"] = deque(samples)
        return row

    def bar_fresh(self, symbol, now):
        row = self.rows.get(symbol) or {}
        stamp = row.get("bar_at")
        return stamp is not None and 0 <= (now - stamp).total_seconds() <= threshold("TAPE_MAX_AGE")

    def view(self, symbol, now=None):
        row = self.rows.get(symbol)
        if not row or row.get("price") is None:
            return None
        if now is not None and row.get("at") is not None:
            if not 0 <= (now - row["at"]).total_seconds() <= threshold("TAPE_MAX_AGE"):
                return None
        vwap, source = row.get("vwap", row.get("quote_vwap")), "quote"
        if row.get("bar_vwap") is not None and (now is None or self.bar_fresh(symbol, now)):
            vwap, source = row["bar_vwap"], "bars"
        return {
            "price": row.get("price"), "open": row.get("open"), "high": row.get("high"),
            "low": row.get("low"), "vwap": vwap, "vwap_source": source,
            "pmh": row.get("pmh"), "pml": row.get("pml"), "pdh": row.get("pdh"),
            "pdl": row.get("pdl"), "atr": row.get("atr"), "samples": row.get("samples"),
        }


TAPE = _Tape()
_PROFILES = {}
_ROTATE = {"i": 0}


def reset_state():
    TAPE.clear()
    _PROFILES.clear()
    _ROTATE["i"] = 0


def remember_baseline(symbol, day, profiles, observations, daily):
    """Store the 20-day median RVOL profile and prior RTH daily highs/lows."""
    recent_days = sorted(profiles)[-int(threshold("RVOL_SESSIONS")):]
    per_minute = {}
    for minute in range(390):
        values = [profiles[day_key][minute] for day_key in recent_days if minute in profiles[day_key]]
        if values:
            per_minute[minute] = (float(median(values)), len(values))
    cumulative = {minute: (float(median(values)), len(values)) for minute, values in observations.items() if values}
    days = []
    for day_key in sorted(daily):
        rows = daily[day_key]
        days.append((day_key.isoformat(), max(row["high"] for row in rows), min(row["low"] for row in rows)))
    _PROFILES[(symbol, day.isoformat())] = {"minute": per_minute, "cum": cumulative, "days": days}


def copy_volume_profile(source, dest, day, daily):
    """SPX keeps its own daily highs and borrows SPY's share-volume profile."""
    borrowed = _PROFILES.get((source, day.isoformat()))
    if not borrowed:
        return
    days = []
    for day_key in sorted(daily):
        rows = daily[day_key]
        days.append((day_key.isoformat(), max(row["high"] for row in rows), min(row["low"] for row in rows)))
    _PROFILES[(dest, day.isoformat())] = {"minute": borrowed["minute"], "cum": borrowed["cum"], "days": days}


def remember_levels(symbol, day, days):
    """Test helper: days is a sequence of (high, low) before today, oldest first."""
    key = (symbol, day.isoformat() if hasattr(day, "isoformat") else day)
    row = _PROFILES.setdefault(key, {"minute": {}, "cum": {}, "days": []})
    row["days"] = [(str(index), high, low) for index, (high, low) in enumerate(days)]


def remember_rvol(symbol, day, cumulative, per_minute=None):
    """cumulative: {minute_offset: (median, count)}. Offsets match bar numbers (1 = 09:31)."""
    key = (symbol, day.isoformat() if hasattr(day, "isoformat") else day)
    row = _PROFILES.setdefault(key, {"minute": {}, "cum": {}, "days": []})
    row["cum"] = {int(minute): (float(stats[0]), int(stats[1])) for minute, stats in cumulative.items()}
    if per_minute:
        row["minute"] = {int(minute): (float(stats[0]), int(stats[1])) for minute, stats in per_minute.items()}


def extra_quote_symbols(universe):
    return symbols_to_quote(universe)


def refresh_symbols():
    needed = ["SPY", "QQQ"]
    for symbol in TAPE.universe:
        etf = primary_etf(symbol)
        if etf and etf not in needed:
            needed.append(etf)
    if not needed:
        return []
    start = _ROTATE["i"] % len(needed)
    _ROTATE["i"] += 1
    return needed[start:] + needed[:start]


# --- bars, EMA, time weights ---

def minutes_after_open(at):
    local = at.astimezone(ET)
    return local.hour * 60 + local.minute - 570


def ema_weights(minute):
    """2m / 5m / 10m weights. Plateaus with a linear fade across each boundary."""
    periods = (
        (0, (1.0, 0.0, 0.0)),
        (45, (0.5, 0.5, 0.0)),
        (60, (0.25, 0.5, 0.25)),
        (90, (0.0, 0.4, 0.6)),
    )
    fade = float(threshold("EMA_FADE_MINUTES"))
    current = periods[0][1]
    for boundary, dest in periods[1:]:
        if minute >= boundary + fade / 2:
            current = dest
            continue
        if minute > boundary - fade / 2:
            mix = (minute - (boundary - fade / 2)) / fade
            return tuple(old + (new - old) * mix for old, new in zip(current, dest))
        return current
    return current


def aggregate(bars, width):
    """Non-overlapping width-minute bars aligned to 09:30. A partial bucket is dropped."""
    if not bars:
        return []
    opening = bars[0].end.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    buckets = {}
    for bar in bars:
        offset = int(round((bar.end.astimezone(ET) - opening).total_seconds() / 60))
        if offset < 1:
            continue
        buckets.setdefault((offset - 1) // width, []).append(bar)
    merged = []
    for index in sorted(buckets):
        group = buckets[index]
        if len(group) < width:
            continue
        merged.append(type("Agg", (), {
            "end": group[-1].end, "open": group[0].open,
            "high": max(bar.high for bar in group), "low": min(bar.low for bar in group),
            "close": group[-1].close, "volume": sum(bar.volume for bar in group),
        })())
    return merged


def ema_series(closes, period):
    if not closes:
        return []
    value = closes[0]
    alpha = 2 / (period + 1)
    series = []
    for close in closes:
        value += alpha * (close - value)
        series.append(value)
    return series


def session_vwaps(bars):
    volume = value = 0.0
    series = []
    for bar in bars:
        typical = (bar.high + bar.low + bar.close) / 3
        volume += bar.volume
        value += typical * bar.volume
        series.append(value / volume if volume else bar.close)
    return series


def _vwap_on(bars, aggregated):
    stamps = {bar.end: vwap for bar, vwap in zip(bars, session_vwaps(bars))}
    return [stamps.get(bar.end, bar.close) for bar in aggregated]


def blended_ema(bars, minute):
    """Return blended 9/20 EMAs, the dominant timeframe, and its 9 EMA series."""
    weights = ema_weights(minute)
    frames = {}
    for width in (2, 5, 10):
        merged = aggregate(bars, width)
        if not merged:
            frames[width] = None
            continue
        closes = [bar.close for bar in merged]
        frames[width] = {
            "bars": merged, "ema9": ema_series(closes, 9), "ema20": ema_series(closes, 20),
            "vwap": _vwap_on(bars, merged),
        }
    values9 = [frames[w]["ema9"][-1] if frames[w] else None for w in (2, 5, 10)]
    values20 = [frames[w]["ema20"][-1] if frames[w] else None for w in (2, 5, 10)]
    slopes = []
    for frame in (frames[2], frames[5], frames[10]):
        if not frame:
            slopes.append(None)
            continue
        series = frame["ema9"]
        slopes.append(series[-1] - (series[-3] if len(series) >= 3 else series[0]))
    blend9 = _blend(weights, values9)
    blend20 = _blend(weights, values20)
    blend_slope = _blend(weights, slopes)
    order = sorted(range(3), key=lambda index: (weights[index], index), reverse=True)
    dominant = None
    for index in order:
        width = (2, 5, 10)[index]
        if frames[width] is not None and weights[index] > 0:
            dominant = frames[width]
            dominant["width"] = width
            dominant["weight"] = weights[index]
            break
    if dominant is None:
        for width in (2, 5, 10):
            if frames[width] is not None:
                dominant = frames[width]
                dominant["width"] = width
                break
    return {
        "weights": weights, "ema9": blend9, "ema20": blend20, "slope": blend_slope,
        "frames": frames, "dominant": dominant,
        "ema9_2": values9[0], "ema9_5": values9[1], "ema9_10": values9[2],
    }


def _blend(weights, values):
    pairs = [(weight_value, value) for weight_value, value in zip(weights, values)
             if value is not None and weight_value > 0]
    if not pairs:
        return None
    total = sum(weight_value for weight_value, _value in pairs)
    return sum(weight_value * value for weight_value, value in pairs) / total


def time_points(minute):
    ends = (("END_1", "TIME_P1"), ("END_2", "TIME_P2"), ("END_3", "TIME_P3"), ("END_4", "TIME_P4"))
    for end_key, weight_key in ends:
        if minute < time_end(end_key):
            return weight(weight_key)
    return weight("TIME_P5")


def rvol_now(symbol, day, offset, today_volume):
    row = _PROFILES.get((symbol, day.isoformat() if hasattr(day, "isoformat") else day))
    if not row:
        return {"status": "unknown", "rvol": None, "sessions": 0}
    stats = row["cum"].get(int(offset))
    if not stats or stats[1] < int(threshold("RVOL_MIN_SESSIONS")) or stats[0] <= 0:
        sessions = 0 if not stats else stats[1]
        return {"status": "unknown", "rvol": None, "sessions": sessions}
    return {"status": "ok", "rvol": today_volume / stats[0], "sessions": stats[1], "median": stats[0]}


def burst_multiple(symbol, day, aggregated_2m):
    """Latest completed 2-minute bar versus the same-minute 20-day median volume."""
    if not aggregated_2m:
        return None
    row = _PROFILES.get((symbol, day.isoformat() if hasattr(day, "isoformat") else day))
    if not row:
        return None
    bar = aggregated_2m[-1]
    opening = bar.end.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    end_offset = int(round((bar.end.astimezone(ET) - opening).total_seconds() / 60))
    start_offset = end_offset - 2
    expected = 0.0
    counted = 0
    for minute in range(start_offset, end_offset):
        # profiles store the minute index from 0 at 09:30; bar offset 1 is minute 0.
        stats = row["minute"].get(minute)
        if not stats or stats[1] < int(threshold("RVOL_MIN_SESSIONS")):
            expected = None
            break
        expected += stats[0]
        counted += 1
    if not expected:
        end_stats, start_stats = row["cum"].get(end_offset), row["cum"].get(start_offset)
        if end_stats and start_stats and min(end_stats[1], start_stats[1]) >= int(threshold("RVOL_MIN_SESSIONS")):
            expected = end_stats[0] - start_stats[0]
        elif end_stats and start_offset <= 0 and end_stats[1] >= int(threshold("RVOL_MIN_SESSIONS")):
            expected = end_stats[0]
        else:
            return None
    if expected <= 0:
        return None
    return bar.volume / expected


def nday_level(symbol, day, n, side):
    row = _PROFILES.get((symbol, day.isoformat() if hasattr(day, "isoformat") else day))
    if not row or len(row["days"]) < n:
        return None
    window = row["days"][-n:]
    highs = [high for _day, high, _low in window]
    lows = [low for _day, _high, low in window]
    return max(highs) if side == "high" else min(lows)


def _range_pos(spot, bars):
    high, low = max(bar.high for bar in bars), min(bar.low for bar in bars)
    if high <= low:
        return 0.5
    return (spot - low) / (high - low)


def _same_side(direction, price, open_):
    if price is None or not open_:
        return None
    return direction * (price / open_ - 1) > 0


def _beyond(direction, price, level):
    if price is None or level is None:
        return False
    return direction * (price - level) > 0


def _just_crossed(frame, level, direction, inside=None):
    inside = int(threshold("INSIDE_2M")) if inside is None else inside
    if level is None or frame is None or len(frame) < inside + 1:
        return False
    if direction * (frame[-1].close - level) <= 0:
        return False
    return all(direction * (bar.close - level) <= 0 for bar in frame[-(inside + 1):-1])


def _touch_pad(width, atr):
    key = {2: "TOUCH_ATR_2", 5: "TOUCH_ATR_5"}.get(width, "TOUCH_ATR_10")
    return float(threshold(key)) * atr


# --- context and triggers ---

def _build(snap):
    bars = snap.bars
    direction = None  # filled by the caller per side; this builds the shared tape
    minute = minutes_after_open(snap.at)
    blended = blended_ema(bars, minute)
    two = aggregate(bars, 2)
    opening = bars[0].end.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    offset = int(round((bars[-1].end.astimezone(ET) - opening).total_seconds() / 60))
    today_volume = sum(bar.volume for bar in bars)
    rvol = rvol_now(snap.symbol, snap.at.astimezone(ET).date(), offset, today_volume)
    burst = burst_multiple(snap.symbol, snap.at.astimezone(ET).date(), two)
    atr = snap.prior_atr if snap.prior_atr and snap.prior_atr > 0 else None
    return {
        "minute": minute, "blended": blended, "two": two, "rvol": rvol, "burst": burst,
        "atr": atr, "pos": _range_pos(snap.spot, bars), "open": bars[0].open,
        "high": max(bar.high for bar in bars), "low": min(bar.low for bar in bars),
    }


def _spy_move(direction, spy, minutes):
    if not spy or not spy.get("samples") or not spy.get("atr"):
        return None
    samples = list(spy["samples"])
    if len(samples) < 2:
        return None
    now_at, now_price = samples[-1]
    prior = [sample for sample in samples if (now_at - sample[0]).total_seconds() >= minutes * 60 - 30]
    if not prior:
        return None
    then = prior[-1][1]
    return direction * (now_price - then) / spy["atr"]


def _own_catalyst(direction, snap, shared, spy, etf):
    if not shared["atr"] or not shared["open"]:
        return False
    name = direction * (snap.spot - shared["open"]) / shared["atr"]
    if not spy or not spy.get("atr") or not spy.get("open") or not spy.get("price"):
        return False
    spy_move = direction * (spy["price"] - spy["open"]) / spy["atr"]
    if name - spy_move < float(threshold("CATALYST_VS_SPY")):
        return False
    if etf and etf.get("atr") and etf.get("open") and etf.get("price"):
        sector_move = direction * (etf["price"] - etf["open"]) / etf["atr"]
        if name - sector_move < float(threshold("CATALYST_VS_SECTOR")):
            return False
    return True


def _trending(direction, snap, shared):
    """Ace section 8. Pullback cards require this; it is not a global gate."""
    atr = shared["atr"]
    blended = shared["blended"]
    if atr is None or blended["slope"] is None or blended["dominant"] is None:
        return False
    pm = snap.premarket_high if direction == 1 else snap.premarket_low
    pd = snap.prior_high if direction == 1 else snap.prior_low
    if not _beyond(direction, snap.spot, pm):
        return False
    near_pd = pd is not None and direction * (snap.spot - pd) >= -float(threshold("PD_NEAR_ATR")) * atr
    if not near_pd:
        return False
    if direction * (snap.spot - shared["open"]) <= 0:
        return False
    if direction * blended["slope"] <= 0:
        return False
    dominant = blended["dominant"]
    flags = [direction * (bar.close - vwap) > 0 for bar, vwap in zip(dominant["bars"], dominant["vwap"])]
    return bool(flags) and sum(flags) / len(flags) >= float(threshold("VWAP_SHARE"))


def _flush(direction, bars, atr):
    if not atr:
        return 0.0, None
    if direction == 1:
        index = min(range(len(bars)), key=lambda i: bars[i].low)
        extreme = bars[index].low
        before = max(bar.high for bar in bars[:index + 1])
        size = (before - extreme) / atr
    else:
        index = max(range(len(bars)), key=lambda i: bars[i].high)
        extreme = bars[index].high
        before = min(bar.low for bar in bars[:index + 1])
        size = (extreme - before) / atr
    opening = bars[0].end.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    minute = int(round((bars[index].end.astimezone(ET) - opening).total_seconds() / 60))
    return size, minute


def _s29a(direction, snap, shared, spy):
    minute = shared["minute"]
    if not (float(threshold("S29_START_MINUTE")) <= minute <= float(threshold("S29_END_MINUTE"))):
        return False
    atr = shared["atr"]
    dominant = shared["blended"]["frames"].get(2)
    if atr is None or dominant is None or len(dominant["ema9"]) < int(threshold("S29_SLOPE_BARS")) + 1:
        return False
    pos = shared["pos"]
    bottom = pos <= float(threshold("S29_RANGE_POS")) if direction == 1 else pos >= 1 - float(threshold("S29_RANGE_POS"))
    if not bottom:
        return False
    flush, turn_minute = _flush(direction, snap.bars, atr)
    if flush < float(threshold("FLUSH_S29_ATR")) or turn_minute is None or turn_minute > float(threshold("S29_TURN_MINUTES")):
        return False
    look = int(threshold("S29_SLOPE_BARS"))
    slope = direction * (dominant["ema9"][-1] - dominant["ema9"][-1 - look])
    if slope <= float(threshold("S29_SLOPE_ATR")) * atr:
        return False
    vwap_delta = direction * (dominant["vwap"][-1] - dominant["vwap"][-1 - look])
    if vwap_delta > float(threshold("S29_VWAP_FLAT_ATR")) * atr:
        return False
    rvol = shared["rvol"]["rvol"]
    if rvol is None or rvol < float(threshold("S29A_RVOL")):
        return False
    ema, vwaps = dominant["ema9"], dominant["vwap"]
    crossed = direction * (ema[-2] - vwaps[-2]) <= 0 and direction * (ema[-1] - vwaps[-1]) > 0
    if not crossed:
        return False
    if spy and spy.get("low") is not None and spy.get("price") is not None and spy["price"] <= spy["low"]:
        return False
    return True


def _vwap_reclaim_before(direction, frame):
    if frame is None or len(frame["bars"]) < 3:
        return False
    for index in range(1, len(frame["bars"]) - 1):
        before = direction * (frame["bars"][index - 1].close - frame["vwap"][index - 1])
        after = direction * (frame["bars"][index].close - frame["vwap"][index])
        if before <= 0 and after > 0:
            return True
    return False


def _s29a_earlier(direction, snap, shared, spy):
    """True when an earlier 2-minute bar in this session already met S29-A."""
    frame = shared["blended"]["frames"].get(2)
    if frame is None or len(frame["bars"]) < 3:
        return _vwap_reclaim_before(direction, frame)
    # Replay the cross on each completed bar by slicing. Cheap: a session is < 200 2m bars.
    last = len(frame["bars"])  # the current bar is the signal; only earlier bars unlock S29-B
    for end in range(int(threshold("S29_SLOPE_BARS")) + 2, last):
        if _s29a_slice(direction, snap, shared, spy, end):
            return True
    return _vwap_reclaim_before(direction, frame)


def _s29a_slice(direction, snap, shared, spy, end):
    frame = shared["blended"]["frames"][2]
    look = int(threshold("S29_SLOPE_BARS"))
    if end < look + 1:
        return False
    bar = frame["bars"][end - 1]
    minute = minutes_after_open(bar.end)
    if not (float(threshold("S29_START_MINUTE")) <= minute <= float(threshold("S29_END_MINUTE"))):
        return False
    atr = shared["atr"]
    if atr is None:
        return False
    ema = frame["ema9"]
    slope = direction * (ema[end - 1] - ema[end - 1 - look])
    if slope <= float(threshold("S29_SLOPE_ATR")) * atr:
        return False
    if direction * (frame["vwap"][end - 1] - frame["vwap"][end - 1 - look]) > float(threshold("S29_VWAP_FLAT_ATR")) * atr:
        return False
    crossed = direction * (ema[end - 2] - frame["vwap"][end - 2]) <= 0 and direction * (ema[end - 1] - frame["vwap"][end - 1]) > 0
    return crossed


def _s29b(direction, snap, shared):
    rvol = shared["rvol"]["rvol"]
    if rvol is None or rvol < float(threshold("S29B_RVOL")):
        return False
    frame = shared["two"]
    pm = snap.premarket_high if direction == 1 else snap.premarket_low
    pd = snap.prior_high if direction == 1 else snap.prior_low
    return _just_crossed(frame, pm, direction) or _just_crossed(frame, pd, direction)


def _or5(direction, snap, shared, leader, spy):
    if shared["minute"] >= float(threshold("OR5_END_MINUTE")) or len(snap.bars) < 7:
        return False
    opening = snap.bars[:5]
    level = max(bar.high for bar in opening) if direction == 1 else min(bar.low for bar in opening)
    if not _just_crossed(shared["two"], level, direction, inside=1):
        return False
    gap_ok = snap.prior_close and direction * (shared["open"] - snap.prior_close) > 0
    if not gap_ok and not leader:
        return False
    if spy and spy.get("open") and spy.get("price") and direction * (spy["price"] - spy["open"]) < 0:
        return False
    return True


def _level_break(direction, snap, shared):
    frame = shared["two"]
    pm = snap.premarket_high if direction == 1 else snap.premarket_low
    pd = snap.prior_high if direction == 1 else snap.prior_low
    return _just_crossed(frame, pm, direction) or _just_crossed(frame, pd, direction)


def _nday_break(direction, snap, shared):
    side = "high" if direction == 1 else "low"
    day = snap.at.astimezone(ET).date()
    for n in (2, 3, 5, 6, 7, 14, 20):
        level = nday_level(snap.symbol, day, n, side)
        if _just_crossed(shared["two"], level, direction):
            return True
    return False


def _pullback(direction, snap, shared, trending):
    if not trending or not shared["atr"]:
        return False
    dominant = shared["blended"]["dominant"]
    if dominant is None or len(dominant["bars"]) < 2:
        return False
    pad = _touch_pad(dominant["width"], shared["atr"])
    ema_now = dominant["ema9"][-1]
    bar = dominant["bars"][-1]
    prev = dominant["bars"][-2]
    prev_ema = dominant["ema9"][-2]
    if direction == 1:
        touch = bar.low <= ema_now + pad or prev.low <= prev_ema + pad
    else:
        touch = bar.high >= ema_now - pad or prev.high >= prev_ema - pad
    reclaim = direction * (bar.close - ema_now) > 0
    return touch and reclaim


def _flush_reversal(direction, snap, shared):
    atr = shared["atr"]
    frame = shared["blended"]["frames"].get(2)
    if not atr or frame is None or len(frame["bars"]) < 2:
        return False
    flush, _minute = _flush(direction, snap.bars, atr)
    if flush < float(threshold("FLUSH_REV_ATR")):
        return False
    extreme = shared["low"] if direction == 1 else shared["high"]
    levels = [snap.premarket_low if direction == 1 else snap.premarket_high,
              snap.prior_low if direction == 1 else snap.prior_high]
    named = [level for level in levels if level is not None]
    if named:
        near = any(abs(extreme - level) <= float(threshold("FLUSH_LEVEL_ATR")) * atr for level in named)
    else:
        # No PML/PDL on the snapshot: the session extreme is the range low/high.
        near = True
    if not near:
        return False
    ema_cross = direction * (frame["bars"][-2].close - frame["vwap"][-2]) <= 0 and direction * (frame["bars"][-1].close - frame["vwap"][-1]) > 0
    return ema_cross


def _leader_rs(direction, snap, shared, etf, peer_returns):
    if not shared["open"]:
        return False
    name = snap.spot / shared["open"] - 1
    if etf and etf.get("open") and etf.get("price"):
        if direction * (name - (etf["price"] / etf["open"] - 1)) < float(threshold("LEADER_RS")):
            return False
    elif not peer_returns:
        return False
    if peer_returns:
        basket = sum(peer_returns) / len(peer_returns)
        if direction * (name - basket) < float(threshold("LEADER_RS")):
            return False
    return True


def _s31(direction, snap, shared, etf, peer_returns):
    if etf is None or etf.get("high") is None or etf.get("low") is None or etf.get("price") is None:
        return False
    span = etf["high"] - etf["low"]
    if span <= 0:
        return False
    etf_pos = (etf["price"] - etf["low"]) / span
    near_low = etf_pos <= float(threshold("ETF_NEAR_LOW")) if direction == 1 else etf_pos >= 1 - float(threshold("ETF_NEAR_LOW"))
    if not near_low:
        return False
    pm = snap.premarket_high if direction == 1 else snap.premarket_low
    if not (_just_crossed(shared["two"], pm, direction) or _beyond(direction, snap.spot, pm)):
        return False
    return _leader_rs(direction, snap, shared, etf, peer_returns)


def _fade(direction, snap, shared):
    """Two-entry fade. Entry 1 is the VWAP rejection; entry 2 is the PML/PMH break on burst."""
    atr = shared["atr"]
    frame = shared["blended"]["frames"].get(2)
    if not atr or frame is None or len(frame["bars"]) < 2:
        return False, False
    early = [bar for bar in snap.bars if minutes_after_open(bar.end) <= float(threshold("FADE_START_MINUTE"))]
    if len(early) < 3:
        return False, False
    if direction == 1:
        impulse = (max(bar.high for bar in early) - shared["open"]) / atr
    else:
        impulse = (shared["open"] - min(bar.low for bar in early)) / atr
    if impulse < float(threshold("FADE_IMPULSE_ATR")):
        return False, False
    minute = shared["minute"]
    in_window = float(threshold("FADE_START_MINUTE")) <= minute < float(threshold("FADE_END_MINUTE"))
    bar, prev = frame["bars"][-1], frame["bars"][-2]
    vwap, prev_vwap = frame["vwap"][-1], frame["vwap"][-2]
    if direction == 1:
        reject = in_window and bar.low <= vwap and bar.close > vwap and prev.close <= prev_vwap
    else:
        reject = in_window and bar.high >= vwap and bar.close < vwap and prev.close >= prev_vwap
    level = snap.premarket_low if direction == -1 else snap.premarket_high
    # Entry 2 is the mirror level: puts break PML, calls break PMH, on burst volume.
    rvol = shared["rvol"]["rvol"]
    burst_ok = rvol is not None and rvol >= float(threshold("S29B_RVOL"))
    level_break = burst_ok and _just_crossed(shared["two"], level, direction) and (reject or _rejection_earlier(direction, frame))
    return reject, level_break


def _rejection_earlier(direction, frame):
    for index in range(1, len(frame["bars"]) - 1):
        bar, vwap = frame["bars"][index], frame["vwap"][index]
        if direction == 1 and bar.low <= vwap and bar.close > vwap:
            return True
        if direction == -1 and bar.high >= vwap and bar.close < vwap:
            return True
    return False


def _peer_views(snap, now):
    returns = []
    for peer in peers_for(snap.symbol):
        view = TAPE.view(peer, now)
        if view and view.get("open") and view.get("price"):
            returns.append(view["price"] / view["open"] - 1)
    return returns


def _overhead_room(direction, snap, shared):
    atr = shared["atr"]
    if not atr:
        return None
    day = snap.at.astimezone(ET).date()
    side = "high" if direction == 1 else "low"
    levels = []
    pd = snap.prior_high if direction == 1 else snap.prior_low
    if pd is not None:
        levels.append(pd)
    for n in (3, 5, 20):
        level = nday_level(snap.symbol, day, n, side)
        if level is not None:
            levels.append(level)
    two = shared["two"]
    look = int(threshold("HOURLY_2M"))
    if len(two) > look + 1:
        window = two[-(look + 1):-1]
        levels.append(max(bar.high for bar in window) if direction == 1 else min(bar.low for bar in window))
    ahead = [direction * (level - snap.spot) / atr for level in levels if direction * (level - snap.spot) > 0]
    return min(ahead) if ahead else None


def _outside(direction, snap, shared):
    pm = snap.premarket_high if direction == 1 else snap.premarket_low
    green = direction * (snap.spot - shared["open"]) > 0
    upper = shared["pos"] >= float(threshold("UPPER_RANGE")) if direction == 1 else shared["pos"] <= 1 - float(threshold("UPPER_RANGE"))
    return _beyond(direction, snap.spot, pm) and green and upper


def _chase(direction, snap, shared, spy, catalyst):
    atr = shared["atr"]
    level = snap.premarket_high if direction == 1 else snap.premarket_low
    pd = snap.prior_high if direction == 1 else snap.prior_low
    if atr is None:
        return False
    pierce_level = None
    for candidate in (level, pd):
        if candidate is None:
            continue
        pierce = direction * (snap.spot - candidate) / atr
        if 0 < pierce < float(threshold("EXHAUSTED_PIERCE_ATR")):
            pierce_level = candidate
            break
    if pierce_level is None or catalyst:
        return False
    two = shared["two"]
    look = int(threshold("EXHAUSTED_BARS_2M"))
    window = two[-look:] if two else []
    if not window:
        return False
    extreme = min(bar.low for bar in window) if direction == 1 else max(bar.high for bar in window)
    if direction * (pierce_level - extreme) / atr < float(threshold("EXHAUSTED_MOVE_ATR")):
        return False
    spy_move = _spy_move(direction, spy, float(threshold("SPY_CHASE_MINUTES")))
    if spy_move is None or spy_move < float(threshold("SPY_CHASE_ATR")):
        return False
    if _pull_in_released(direction, two, pierce_level, shared, atr):
        return False
    return True


def _pull_in_released(direction, frame, level, shared, atr):
    first = None
    for index, bar in enumerate(frame):
        if direction * (bar.close - level) > 0:
            first = index
            break
    if first is None:
        return False
    adverse = 0
    touched = False
    pad = _touch_pad(2, atr)
    ema = ema_series([bar.close for bar in frame], 9)
    # Only the bars after the first break count. The approach itself always tags the EMA.
    for bar, value in zip(frame[first:-1], ema[first:-1]):
        if direction * (bar.close - bar.open) < 0:
            adverse += 1
        if direction == 1 and bar.low <= value + pad:
            touched = True
        if direction == -1 and bar.high >= value - pad:
            touched = True
    rvol = shared["rvol"]["rvol"]
    rebreak = direction * (frame[-1].close - level) > 0
    return (adverse >= int(threshold("PULLIN_BARS")) or touched) and rebreak and rvol is not None and rvol >= float(threshold("REBREAK_RVOL"))


INSIDE = {"S29A", "FLUSH_REVERSAL", "FADE_REJECT"}
LEVEL_TRIGGERS = {"LEVEL_BREAK", "NDAY_BREAK", "S29B", "FADE_LEVEL", "S31"}
ENTRY_TRIGGERS = LEVEL_TRIGGERS | {"TREND_PULLBACK", "OR5"}


def evaluate_side(snap, direction, legacy_blockers=()):
    shared = _build(snap)
    now = snap.at
    spy = TAPE.view("QQQ" if snap.symbol == "SPY" else "SPY", now)
    qqq = None if snap.symbol in {"SPY", "QQQ"} else TAPE.view("QQQ", now)
    etf_symbol = primary_etf(snap.symbol)
    etf = TAPE.view(etf_symbol, now) if etf_symbol and etf_symbol != snap.symbol else None
    peer_returns = _peer_views(snap, now)
    catalyst = _own_catalyst(direction, snap, shared, spy, etf)
    trending = _trending(direction, snap, shared)
    leader = _leader_rs(direction, snap, shared, etf, peer_returns)
    s29a = _s29a(direction, snap, shared, spy)
    prior_s29 = _s29a_earlier(direction, snap, shared, spy)
    triggers = set()
    if s29a:
        triggers.add("S29A")
    if prior_s29 and _s29b(direction, snap, shared):
        triggers.add("S29B")
    if _or5(direction, snap, shared, leader, spy):
        triggers.add("OR5")
    if _level_break(direction, snap, shared):
        triggers.add("LEVEL_BREAK")
    if _nday_break(direction, snap, shared):
        triggers.add("NDAY_BREAK")
    if _pullback(direction, snap, shared, trending):
        triggers.add("TREND_PULLBACK")
    if _flush_reversal(direction, snap, shared):
        triggers.add("FLUSH_REVERSAL")
    if _s31(direction, snap, shared, etf, peer_returns):
        triggers.add("S31")
    fade_reject, fade_level = _fade(direction, snap, shared)
    if fade_reject:
        triggers.add("FADE_REJECT")
    if fade_level:
        triggers.add("FADE_LEVEL")

    beyond_pm = _beyond(direction, snap.spot, snap.premarket_high if direction == 1 else snap.premarket_low)
    beyond_pd = _beyond(direction, snap.spot, snap.prior_high if direction == 1 else snap.prior_low)
    day = snap.at.astimezone(ET).date()
    side = "high" if direction == 1 else "low"
    beyond_n3 = False
    beyond_n5 = False
    for n in (3, 5, 6, 7, 14, 20):
        level = nday_level(snap.symbol, day, n, side)
        if _beyond(direction, snap.spot, level):
            if n >= 3:
                beyond_n3 = True
            if n >= 5:
                beyond_n5 = True
    two = shared["two"]
    look = int(threshold("HOURLY_2M"))
    hourly = False
    if len(two) > look + 1:
        window = two[-(look + 1):-1]
        hourly_level = max(bar.high for bar in window) if direction == 1 else min(bar.low for bar in window)
        hourly = _beyond(direction, snap.spot, hourly_level)
    daily_level = beyond_pd or beyond_n5 or hourly

    blockers = []
    room = _overhead_room(direction, snap, shared)
    tight = room is not None and room < float(threshold("ROOM_ATR"))
    sector_ok = _sector_ok(direction, etf)
    spy_ok = bool(spy and _same_side(direction, spy.get("price"), spy.get("open")))
    rvol_value = shared["rvol"]["rvol"]
    aligned = bool(triggers) or beyond_pm or beyond_pd or beyond_n3
    burst = shared["burst"]
    burst_hit = burst is not None and burst >= float(threshold("BURST_MULTIPLE")) and aligned
    if tight and triggers & LEVEL_TRIGGERS:
        room_ok = sector_ok and spy_ok and rvol_value is not None and rvol_value >= float(threshold("ROOM_RVOL"))
        if not room_ok:
            triggers -= LEVEL_TRIGGERS
            if not triggers:
                blockers.append("room to next layer")

    if "TREND_PULLBACK" in triggers and not trending:
        triggers.discard("TREND_PULLBACK")
    # Inside-range setups (S29-A, flush, VWAP rejection) and OR5 skip the global
    # outside-range gate. After 11:00 the remaining setups need to be green and
    # already beyond PMH or PDH; the morning gate still wants PMH, green, and
    # the upper part of the day's range.
    gated = triggers - INSIDE - {"OR5", "TREND_PULLBACK"}
    if gated and shared["minute"] >= float(threshold("AFTER_11_MINUTE")):
        green = direction * (snap.spot - shared["open"]) > 0
        if not (green and (beyond_pm or beyond_pd)):
            triggers -= gated
            if not triggers:
                blockers.append("in-range after 11:00")
    elif gated and not _outside(direction, snap, shared):
        triggers -= gated
        if not triggers:
            blockers.append("outside the range")

    if _chase(direction, snap, shared, spy, catalyst) and triggers & (LEVEL_TRIGGERS | {"OR5"}):
        blockers.append("exhausted approach")

    need_rvol = float(threshold("S29A_RVOL")) if triggers == {"S29A"} else float(threshold("RVOL_GATE"))
    rvol_ok = (rvol_value is not None and rvol_value >= need_rvol) or burst_hit
    if triggers and not rvol_ok:
        blockers.append("rvol below minimum")
    if not triggers and "room to next layer" not in blockers and "outside the range" not in blockers and "in-range after 11:00" not in blockers and "exhausted approach" not in blockers:
        blockers.append("no andre setup")

    parts = _parts(direction, snap, shared, spy, qqq, etf, peer_returns, catalyst, triggers,
                   beyond_pm, beyond_pd, beyond_n3, daily_level, leader, burst_hit)
    raw = sum(parts.values())
    score = round(max(0.0, min(100.0, raw)), 2)
    minimum = float(threshold("MIN_SCORE"))
    if score < minimum:
        blockers.append("score below andre minimum")
    for blocker in legacy_blockers:
        if blocker.startswith("dead zone") or blocker.startswith("volume burst on") or blocker.startswith("move already spent"):
            blockers.append(blocker)
    qualifying = not blockers and bool(triggers)
    top = float(threshold("TOP_SCORE"))
    fast = int(qualifying)
    setup = _setup(direction, snap, shared, triggers) if triggers else None
    if qualifying and setup is None:
        qualifying = False
        blockers.append("no andre setup")
    metrics = {
        "andre_triggers": ",".join(sorted(triggers)),
        "andre_tier": "A" if score >= top else "B",
        "andre_fast_path": fast,
        "andre_rvol": rvol_value if rvol_value is not None else -1,
        "andre_rvol_sessions": shared["rvol"]["sessions"],
        "andre_rvol_status": shared["rvol"]["status"],
        "andre_burst": burst if burst is not None else -1,
        "andre_burst_aligned": int(burst_hit),
        "andre_trending": int(trending),
        "andre_catalyst": int(catalyst),
        "andre_room_atr": room if room is not None else -1,
        "andre_stretch_atr": _stretch(direction, snap, shared),
        "andre_ema9": shared["blended"]["ema9"] if shared["blended"]["ema9"] is not None else -1,
        "andre_w2": shared["blended"]["weights"][0],
        "andre_w5": shared["blended"]["weights"][1],
        "andre_w10": shared["blended"]["weights"][2],
        "andre_outside": int(_outside(direction, snap, shared)),
        "andre_score_raw": round(raw, 2),
        "selected_spread_fraction": (legacy_blockers and 0) or 0,
    }
    # Keep the spread as an info field. The legacy metric already has the fraction;
    # apply_mode copies it back when present.
    reasons = [f"Andre {', '.join(sorted(triggers)) or 'no setup'}", f"Tier {metrics['andre_tier']}"]
    return {
        "parts": parts, "metrics": metrics, "score": score, "blockers": tuple(blockers),
        "qualifying": qualifying, "setup": setup, "reasons": reasons,
    }


def _stretch(direction, snap, shared):
    ema = shared["blended"]["ema9"]
    if ema is None or not shared["atr"]:
        return 0.0
    return direction * (snap.spot - ema) / shared["atr"]


def _sector_ok(direction, etf):
    if not etf or not etf.get("open") or not etf.get("price"):
        return False
    same = direction * (etf["price"] - etf["open"]) > 0
    past_vwap = etf.get("vwap") is not None and direction * (etf["price"] - etf["vwap"]) > 0
    past_level = _beyond(direction, etf.get("price"), etf.get("pmh") if direction == 1 else etf.get("pml"))
    past_level = past_level or _beyond(direction, etf.get("price"), etf.get("pdh") if direction == 1 else etf.get("pdl"))
    return same and (past_vwap or past_level)


def _parts(direction, snap, shared, spy, qqq, etf, peer_returns, catalyst, triggers,
           beyond_pm, beyond_pd, beyond_n3, daily_level, leader, burst_hit):
    rvol = shared["rvol"]["rvol"]
    if rvol is None:
        rvol_points = 0.0
    elif rvol >= float(threshold("RVOL_TIER3")):
        rvol_points = weight("RVOL_3")
    elif rvol >= float(threshold("RVOL_TIER2")):
        rvol_points = weight("RVOL_2")
    elif rvol >= float(threshold("RVOL_GATE")):
        rvol_points = weight("RVOL_15")
    else:
        rvol_points = 0.0
    # A mid-range burst adds nothing. An aligned burst does not raise the tier by
    # itself; it only lets the card fire without waiting (see andre_fast_path).
    if burst_hit and rvol_points == 0.0 and (rvol is None or rvol < float(threshold("RVOL_GATE"))):
        rvol_points = weight("RVOL_15")
    if catalyst:
        spy_trend = weight("SPY_TREND")
        spy_level = weight("SPY_LEVEL")
        qqq_points = weight("QQQ_TREND") + weight("QQQ_LEVEL")
    else:
        spy_trend = weight("SPY_TREND") if spy and _same_side(direction, spy.get("price"), spy.get("open")) else 0.0
        spy_level_flag = False
        if spy:
            spy_level_flag = _beyond(direction, spy.get("price"), spy.get("pmh") if direction == 1 else spy.get("pml"))
            spy_level_flag = spy_level_flag or _beyond(direction, spy.get("price"), spy.get("pdh") if direction == 1 else spy.get("pdl"))
        spy_level = weight("SPY_LEVEL") if spy_level_flag else 0.0
        qqq_points = 0.0
        if qqq and _same_side(direction, qqq.get("price"), qqq.get("open")):
            qqq_points += weight("QQQ_TREND")
        if qqq and (_beyond(direction, qqq.get("price"), qqq.get("pmh") if direction == 1 else qqq.get("pml"))
                    or _beyond(direction, qqq.get("price"), qqq.get("pdh") if direction == 1 else qqq.get("pdl"))):
            qqq_points += weight("QQQ_LEVEL")
    peer_hit = False
    if len(peer_returns) >= int(threshold("PEER_MIN")):
        share = sum(direction * value > 0 for value in peer_returns) / len(peer_returns)
        peer_hit = share >= float(threshold("PEER_BREADTH"))
    stretch = _stretch(direction, snap, shared)
    return {
        "level_pm": weight("LEVEL_PM") if beyond_pm else 0.0,
        "level_pd": weight("LEVEL_PD") if beyond_pd else 0.0,
        "level_n3": weight("LEVEL_N3") if beyond_n3 else 0.0,
        "level_n5": weight("LEVEL_N5") if daily_level else 0.0,
        "spy_trend": spy_trend,
        "spy_level": spy_level,
        "qqq": qqq_points,
        "sector_etf": weight("SECTOR_ETF") if _sector_ok(direction, etf) else 0.0,
        "peers": weight("PEERS") if peer_hit else 0.0,
        "leader": weight("LEADER") if leader else 0.0,
        "rvol": rvol_points,
        "entry": weight("ENTRY") if triggers & ENTRY_TRIGGERS else 0.0,
        "named_setup": weight("NAMED") if triggers else 0.0,
        "time_window": time_points(shared["minute"]),
        "stretch": -weight("PENALTY_STRETCH") if stretch > float(threshold("STRETCH_ATR")) else 0.0,
        "strike_breadth": 0.0,
        "liquidity": 0.0,
        "contract_response": 0.0,
        "option_velocity": 0.0,
    }


def _apply_chase_penalty(parts, blocked):
    parts = dict(parts)
    parts["chase"] = -weight("PENALTY_CHASE") if blocked else 0.0
    return parts


def _setup(direction, snap, shared, triggers):
    order = ("S29A", "S29B", "OR5", "LEVEL_BREAK", "NDAY_BREAK", "TREND_PULLBACK",
             "FLUSH_REVERSAL", "S31", "FADE_REJECT", "FADE_LEVEL")
    name = next(item for item in order if item in triggers)
    if name in {"LEVEL_BREAK", "S29B", "S31", "FADE_LEVEL"}:
        trigger = (snap.premarket_high if direction == 1 else snap.premarket_low) or snap.spot
    elif name == "NDAY_BREAK":
        trigger = snap.spot
    elif name == "TREND_PULLBACK":
        ema = shared["blended"]["ema9"]
        trigger = ema if ema is not None else snap.spot
    else:
        dominant = shared["blended"]["dominant"]
        trigger = dominant["vwap"][-1] if dominant else snap.spot
    invalidation = dominant_vwap(shared) 
    if direction == 1:
        invalidation = min(invalidation, trigger) if invalidation else trigger
    return Setup(name, float(trigger), float(invalidation or trigger), building=False)


def dominant_vwap(shared):
    dominant = shared["blended"]["dominant"]
    if dominant and dominant["vwap"]:
        return dominant["vwap"][-1]
    return None


def evaluate_side_full(snap, direction, legacy_blockers=()):
    """evaluate_side plus the chase penalty, which depends on the blocker list."""
    result = evaluate_side(snap, direction, legacy_blockers)
    chased = "exhausted approach" in result["blockers"]
    result["parts"] = _apply_chase_penalty(result["parts"], chased)
    raw = sum(result["parts"].values())
    result["score"] = round(max(0.0, min(100.0, raw)), 2)
    result["metrics"]["andre_score_raw"] = round(raw, 2)
    minimum = float(threshold("MIN_SCORE"))
    blockers = [blocker for blocker in result["blockers"] if blocker != "score below andre minimum"]
    if result["score"] < minimum:
        blockers.append("score below andre minimum")
    result["blockers"] = tuple(blockers)
    result["qualifying"] = not blockers and bool(result["metrics"]["andre_triggers"])
    result["metrics"]["andre_fast_path"] = int(result["qualifying"])
    result["metrics"]["andre_tier"] = "A" if result["score"] >= float(threshold("TOP_SCORE")) else "B"
    if result["qualifying"] and result["setup"] is None:
        result["qualifying"] = False
    return result


def apply_mode(results):
    """Replace the legacy score on each candidate. Flag 0 does not call this."""
    for candidate in results:
        direction = 1 if candidate.option.side == "CALL" else -1
        spread = candidate.metrics.get("selected_spread_fraction")
        result = evaluate_side_full(candidate.snapshot, direction, candidate.blockers)
        if spread is not None:
            result["metrics"]["selected_spread_fraction"] = spread
        candidate.parts = result["parts"]
        candidate.metrics.update(result["metrics"])
        blended = result["metrics"].get("andre_ema9")
        if isinstance(blended, (int, float)) and blended != -1:
            candidate.metrics["ema9"] = blended
        candidate.score = result["score"]
        candidate.blockers = result["blockers"]
        candidate.qualifying = result["qualifying"]
        if result["setup"] is not None:
            candidate.setup = result["setup"]
        candidate.reasons = result["reasons"] + [
            reason for reason in candidate.reasons if reason.startswith("Peers: ") or "supportive" in reason or reason.startswith("Outperforming ")
        ]
    return results
