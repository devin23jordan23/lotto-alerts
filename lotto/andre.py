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
        cutoff = at - timedelta(minutes=35)
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
_FLUSH_SEEN = set()
_EARNINGS = set()


def reset_state():
    TAPE.clear()
    _PROFILES.clear()
    _ROTATE["i"] = 0
    _FLUSH_SEEN.clear()
    _EARNINGS.clear()


def note_earnings(symbol, day):
    """Mark an earnings day-1. There is no live calendar in this repo, so this stays empty in production."""
    _EARNINGS.add((symbol, day.isoformat() if hasattr(day, "isoformat") else day))


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
        rows = sorted(daily[day_key], key=lambda row: row.get("datetime") or 0)
        close = rows[-1].get("close")
        days.append((day_key.isoformat(), max(row["high"] for row in rows), min(row["low"] for row in rows), close))
    _PROFILES[(symbol, day.isoformat())] = {"minute": per_minute, "cum": cumulative, "days": days}


def copy_volume_profile(source, dest, day, daily):
    """SPX keeps its own daily highs and borrows SPY's share-volume profile."""
    borrowed = _PROFILES.get((source, day.isoformat()))
    if not borrowed:
        return
    days = []
    for day_key in sorted(daily):
        rows = sorted(daily[day_key], key=lambda row: row.get("datetime") or 0)
        close = rows[-1].get("close")
        days.append((day_key.isoformat(), max(row["high"] for row in rows), min(row["low"] for row in rows), close))
    _PROFILES[(dest, day.isoformat())] = {"minute": borrowed["minute"], "cum": borrowed["cum"], "days": days}


def remember_levels(symbol, day, days):
    """Test helper: days is a sequence of (high, low) or (high, low, close), oldest first."""
    key = (symbol, day.isoformat() if hasattr(day, "isoformat") else day)
    row = _PROFILES.setdefault(key, {"minute": {}, "cum": {}, "days": []})
    stored = []
    for index, item in enumerate(days):
        high, low = item[0], item[1]
        close = item[2] if len(item) > 2 else None
        stored.append((str(index), high, low, close))
    row["days"] = stored


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
    highs = [item[1] for item in window]
    lows = [item[2] for item in window]
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


WATCHLIST = {"LEVEL_BREAK", "OR5", "S29A", "S29B", "S31"}
SIDE_TRIGGERS = {"FLUSH_V", "TREND_PULLBACK"}


def _day_closes(symbol, day):
    row = _PROFILES.get((symbol, day.isoformat() if hasattr(day, "isoformat") else day))
    if not row:
        return []
    return [item[3] for item in row["days"] if len(item) >= 4 and item[3] is not None]


def _days_down(symbol, day, direction):
    closes = _day_closes(symbol, day)
    run = 0
    for older, newer in zip(closes, closes[1:]):
        step = direction * (older - newer)
        run = run + 1 if step > 0 else 0
    return run


def _spy_efficiency(spy, direction):
    samples = list((spy or {}).get("samples") or [])
    if len(samples) < 3:
        return False
    now = samples[-1][0]
    span = float(threshold("SPY_EFF_MINUTES")) * 60
    window = [sample for sample in samples if 0 <= (now - sample[0]).total_seconds() <= span + 30]
    if len(window) < 3 or (now - window[0][0]).total_seconds() < span - 90:
        return False
    prices = [price for _at, price in window]
    net = prices[-1] - prices[0]
    path = sum(abs(b - a) for a, b in zip(prices, prices[1:]))
    if path <= 0:
        return False
    return abs(net) / path >= float(threshold("SPY_EFF_MIN")) and direction * net > 0


def _spy_d3(day, direction):
    closes = _day_closes("SPY", day)
    if len(closes) < 4:
        return False
    return direction * (closes[-1] - closes[-4]) > 0


def _pace(snap, legacy):
    if legacy and legacy.get("pace_rvol"):
        return float(legacy["pace_rvol"])
    baseline = snap.bars[-1].expected_cumulative_volume if snap.bars else None
    if baseline and baseline > 0:
        return sum(bar.volume for bar in snap.bars) / baseline
    return 0.0


def _ema_reclaims(direction, shared):
    frame = (shared["blended"]["frames"] or {}).get(2)
    if not frame:
        return 0
    count = 0
    for bar, ema in zip(frame["bars"], frame["ema9"]):
        tagged = bar.low <= ema if direction == 1 else bar.high >= ema
        reclaimed = direction * (bar.close - ema) > 0
        if tagged and reclaimed:
            count += 1
    return count


def _stack_depth(direction, snap, shared, spy):
    atr = shared["atr"]
    flags = []
    flags.append(bool(atr) and direction * (snap.spot - shared["open"]) / atr >= float(threshold("STACK_MOVE_ATR")))
    prior = snap.prior_high if direction == 1 else snap.prior_low
    flags.append(_beyond(direction, snap.spot, prior))
    flags.append(bool(atr) and (shared["high"] - shared["low"]) / atr >= float(threshold("STACK_BIG_RANGE_ATR")))
    relative = False
    if spy and spy.get("open") and spy.get("price") and shared["open"]:
        name = snap.spot / shared["open"] - 1
        spy_return = spy["price"] / spy["open"] - 1
        relative = spy_return > float(threshold("SPY_NON_DUMP")) and direction * (name - spy_return) >= float(threshold("STACK_RS"))
    flags.append(relative)
    rvol = shared["rvol"]["rvol"]
    flags.append(rvol is not None and rvol >= float(threshold("STACK_RVOL")))
    outer = shared["pos"] >= 0.75 if direction == 1 else shared["pos"] <= 0.25
    making = (snap.bars[-1].high >= shared["high"] - 1e-9 if direction == 1
              else snap.bars[-1].low <= shared["low"] + 1e-9)
    flags.append(bool(outer and making))
    flags.append(_ema_reclaims(direction, shared) >= 2)
    return sum(1 for flag in flags if flag)


def _named_levels(direction, snap):
    """Supports the open can flush into. Puts use the mirrored levels above the open."""
    levels = []
    prem = snap.premarket_low if direction == 1 else snap.premarket_high
    prior = snap.prior_low if direction == 1 else snap.prior_high
    if prem is not None:
        levels.extend((prem, prem))  # PML and the overnight print we actually store
    if prior is not None:
        levels.append(prior)
    anchor = snap.bars[0].open if snap.bars else snap.spot
    step = 10 if anchor >= 50 else 5 if anchor >= 20 else 1
    levels.append((anchor // step) * step)
    if snap.prior_high and snap.prior_low and snap.prior_close:
        pivot = (snap.prior_high + snap.prior_low + snap.prior_close) / 3
        span = snap.prior_high - snap.prior_low
        if direction == 1:
            levels.extend((2 * pivot - snap.prior_high, pivot - span, snap.prior_high - 0.625 * span))
        else:
            levels.extend((2 * pivot - snap.prior_low, pivot + span, snap.prior_low + 0.625 * span))
    open_ = snap.bars[0].open if snap.bars else snap.spot
    kept = []
    for level in levels:
        if level is None:
            continue
        if direction * (open_ - level) <= 0:
            continue
        if all(abs(level - other) > 1e-6 for other in kept):
            kept.append(level)
    return kept


def _flush_v(direction, snap, shared, spy):
    """Open-only flush into a named level and a V reclaim. One event per name, side, and day."""
    minute = shared["minute"]
    if not (float(threshold("FLUSH_START_MINUTE")) <= minute <= float(threshold("FLUSH_END_MINUTE"))):
        return False
    key = (snap.at.astimezone(ET).date().isoformat(), snap.symbol, direction)
    if key in _FLUSH_SEEN:
        return False
    atr = shared["atr"]
    two = shared["two"]
    if not atr or len(two) < 2 or _stack_depth(direction, snap, shared, spy) < int(threshold("FLUSH_DEPTH")):
        return False
    rvol = shared["rvol"]["rvol"]
    burst = shared["burst"] or 0
    if not ((rvol is not None and rvol >= float(threshold("S29A_RVOL"))) or burst >= float(threshold("BURST_MULTIPLE"))):
        return False
    if direction * (two[-1].close - (shared["low"] if direction == 1 else shared["high"])) / atr < float(threshold("FLUSH_LEAVE_ATR")):
        if _days_down(snap.symbol, snap.at.astimezone(ET).date(), direction) < int(threshold("DAYS_DOWN")):
            return False
    last = len(two) - 1
    window = int(threshold("FLUSH_RECLAIM_BARS"))
    for level in _named_levels(direction, snap):
        for tag in range(last + 1):
            bar = two[tag]
            tagged = bar.low <= level if direction == 1 else bar.high >= level
            if not tagged:
                continue
            if last - tag > window - 1:
                continue
            if direction * (two[-1].close - level) <= 0:
                continue
            _FLUSH_SEEN.add(key)
            return True
    return False


def _midday_inside(direction, snap, shared):
    minute = shared["minute"]
    if not (float(threshold("MIDDAY_START_MINUTE")) <= minute < float(threshold("MIDDAY_END_MINUTE"))):
        return False
    prem = snap.premarket_high if direction == 1 else snap.premarket_low
    return not _beyond(direction, snap.spot, prem)


def evaluate_side(snap, direction, legacy_blockers=(), legacy_metrics=None):
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

    # Level breaks, ORB5, S29-A/B and S31 are watchlist inputs. FADE2 and N-day
    # breaks are not triggers. The only side fires are the open flush and a
    # depth-6 EMA reclaim. Exhausted-approach and tight-room holds are not applied.
    watch = triggers & WATCHLIST
    depth = _stack_depth(direction, snap, shared, spy)
    side = set()
    if _flush_v(direction, snap, shared, spy):
        side.add("FLUSH_V")
    if (_pullback(direction, snap, shared, True) and depth >= int(threshold("TREND_DEPTH"))
            and not _midday_inside(direction, snap, shared)):
        side.add("TREND_PULLBACK")
    blockers = []
    if side and shared["minute"] >= float(threshold("AFTER_11_MINUTE")) and not (beyond_pm or beyond_pd):
        side.clear()
        blockers.append("in-range after 11:00")
    if not side:
        blockers.append("no andre setup")
    triggers = side
    rvol_value = shared["rvol"]["rvol"]
    burst = shared["burst"]
    burst_hit = burst is not None and burst >= float(threshold("BURST_MULTIPLE"))
    parts = _parts(direction, snap, shared, spy, day, legacy_metrics, beyond_pm, beyond_pd, burst_hit)
    raw = sum(parts.values())
    # Additive score. Do not floor at 0: after 13:00 the time term is -2.
    score = round(min(100.0, raw), 2)
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
        "andre_watch": ",".join(sorted(watch)),
        "andre_stack": depth,
        "andre_tier": "A" if score >= top else "B",
        "andre_fast_path": fast,
        "andre_rvol": rvol_value if rvol_value is not None else -1,
        "andre_rvol_sessions": shared["rvol"]["sessions"],
        "andre_rvol_status": shared["rvol"]["status"],
        "andre_burst": burst if burst is not None else -1,
        "andre_burst_aligned": int(burst_hit),
        "andre_trending": int(trending),
        "andre_catalyst": int(catalyst),
        "andre_room_atr": -1,
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


def _parts(direction, snap, shared, spy, day, legacy, beyond_pm, beyond_pd, burst_hit):
    """Nia's additive points. Terms that failed verification stay in the dict at 0."""
    rvol = shared["rvol"]["rvol"]
    local = float(legacy.get("local_rvol_5m") or 0) if legacy else 0.0
    rvol_points = weight("RVOL_3") if ((rvol or 0) >= 3 or local >= 3) else 0.0
    pace_points = weight("PACE_2") if _pace(snap, legacy) >= 2 else 0.0
    prior = snap.prior_high if direction == 1 else snap.prior_low
    burst_pd = weight("BURST_PD") if burst_hit and _just_crossed(shared["two"], prior, direction) else 0.0
    gap_points = 0.0
    if shared["atr"] and snap.prior_close and abs(shared["open"] - snap.prior_close) >= shared["atr"]:
        gap_points = weight("GAP_1ATR")
    earnings = weight("EARN_D1") if (snap.symbol, day.isoformat()) in _EARNINGS else 0.0
    inside = shared["minute"] >= float(threshold("AFTER_11_MINUTE")) and not (beyond_pm or beyond_pd)
    return {
        "time_window": time_points(shared["minute"]),
        "inside_after_11": -weight("PENALTY_INSIDE_AFTER11") if inside else 0.0,
        "spy_eff": weight("SPY_EFF") if _spy_efficiency(spy, direction) else 0.0,
        "spy_d3": weight("SPY_D3") if _spy_d3(day, direction) else 0.0,
        "spy_trend": weight("SPY_TREND"),
        "spy_level": weight("SPY_LEVEL"),
        "qqq": weight("QQQ_TREND") + weight("QQQ_LEVEL"),
        "level_pm": weight("LEVEL_PM"),
        "level_pd": weight("LEVEL_PD"),
        "level_n3": weight("LEVEL_N3"),
        "level_n5": weight("LEVEL_N5"),
        "sector_etf": weight("SECTOR_ETF"),
        "peers": weight("PEERS"),
        "leader": weight("LEADER"),
        "pace": pace_points,
        "rvol": rvol_points,
        "burst_pd": burst_pd,
        "gap": gap_points,
        "earnings": earnings,
        "entry": weight("ENTRY"),
        "named_setup": weight("NAMED"),
        "chase": -weight("PENALTY_CHASE"),
        "stretch": -weight("PENALTY_STRETCH"),
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
    order = ("FLUSH_V", "TREND_PULLBACK", "S29A", "S29B", "OR5", "LEVEL_BREAK", "S31")
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


def evaluate_side_full(snap, direction, legacy_blockers=(), legacy_metrics=None):
    return evaluate_side(snap, direction, legacy_blockers, legacy_metrics)


def apply_mode(results):
    """Replace the legacy score on each candidate. Flag 0 does not call this."""
    for candidate in results:
        direction = 1 if candidate.option.side == "CALL" else -1
        spread = candidate.metrics.get("selected_spread_fraction")
        result = evaluate_side_full(candidate.snapshot, direction, candidate.blockers, candidate.metrics)
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
