"""Shadow log for entry and veto ideas. Nothing here is queued to Discord.

Detectors read completed session bars and write wave_shadow. They do not change
scores, setups, cooldowns, or the alert payload. ATR21 is required for the
would-be detectors; ATR14 is never substituted.
"""
import json
import logging
from datetime import datetime, time, timedelta

from .config import Settings
from .features import broke_20d_range, quote_ok
from .models import ET, Bar, Snapshot
from .patterns import Setup
from .schwab import SECTORS

LOG = logging.getLogger(__name__)

KEEP_DAYS = 30
SPY_NEAR_ATR = 0.10
CARD_WINDOW = timedelta(minutes=30)
QUICK_PULLIN = timedelta(minutes=10)


def shadow_enabled(settings: Settings) -> bool:
    return bool(settings.wave_shadow or settings.wave_card_shadow or settings.wave_f4_shadow
                or settings.wave_f7_shadow or settings.wave_f1_shadow or settings.wave_f2_shadow)


def _family_on(settings: Settings, flag: int) -> bool:
    """Master enables every family. A per-item flag enables that family alone."""
    return bool(settings.wave_shadow) or bool(flag)


def _num(value, places=4):
    if value is None:
        return None
    return round(float(value), places)


def _side(direction: int) -> str:
    return "CALL" if direction > 0 else "PUT"


def _cs(bar: Bar, direction: int):
    """Map a bar into call space. Puts flip high/low and negate prices."""
    if direction > 0:
        return bar.open, bar.high, bar.low, bar.close
    return -bar.open, -bar.low, -bar.high, -bar.close


def ema(values, period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    value = values[0]
    out = []
    for item in values:
        value += alpha * (item - value)
        out.append(value)
    return out


def anchored(bars) -> bool:
    return bool(bars) and bars[0].end.astimezone(ET).time() == time(9, 31)


def aggregate(bars, k: int) -> tuple[Bar, ...]:
    """Completed k-minute buckets anchored at 09:30. A partial tail is dropped."""
    if k <= 0 or not anchored(bars):
        return ()
    count = len(bars) - (len(bars) % k)
    out = []
    for index in range(0, count, k):
        group = bars[index:index + k]
        out.append(Bar(
            end=group[-1].end, open=group[0].open,
            high=max(bar.high for bar in group), low=min(bar.low for bar in group),
            close=group[-1].close, volume=sum(bar.volume for bar in group),
            expected_cumulative_volume=group[-1].expected_cumulative_volume))
    return tuple(out)


def _vwap_series(bars) -> list[float]:
    volume = value = 0.0
    out = []
    for bar in bars:
        volume += bar.volume
        value += (bar.high + bar.low + bar.close) / 3 * bar.volume
        out.append(value / max(1.0, volume))
    return out


def _rv5(bars, bucket: int) -> float | None:
    """5m volume versus the same-slot expected volume. None when the baseline is missing."""
    end_index = (bucket + 1) * 5 - 1
    if end_index >= len(bars):
        return None
    end_bar = bars[end_index]
    if end_bar.expected_cumulative_volume is None:
        return None
    if bucket == 0:
        previous = 0.0
    else:
        prior = bars[bucket * 5 - 1]
        if prior.expected_cumulative_volume is None:
            return None
        previous = prior.expected_cumulative_volume
    expected = end_bar.expected_cumulative_volume - previous
    if expected <= 0:
        return None
    actual = sum(bar.volume for bar in bars[bucket * 5:(bucket + 1) * 5])
    return actual / expected


def _minute_index(bar_end: datetime) -> int:
    local = bar_end.astimezone(ET)
    opened = local.replace(hour=9, minute=31, second=0, microsecond=0)
    return max(0, int((local - opened).total_seconds() // 60))


def pick_contract(snap: Snapshot, direction: int, atr: float | None):
    """Nearest 0/1DTE contract around a 0.40 delta, else the spec's strike target."""
    side = _side(direction)
    day = snap.at.astimezone(ET).date()
    listed = []
    for option in snap.options:
        if option.side != side or option.ask <= 0 or option.bid < 0:
            continue
        dte = (option.expiry - day).days
        if 0 <= dte <= 1:
            listed.append((option, dte))
    if not listed:
        return None
    def ranked(pool):
        return min(pool, key=lambda item: (
            abs(abs(item[0].delta) - 0.40),
            (item[0].ask - item[0].bid) / item[0].ask,
            item[0].symbol))[0]
    band = [item for item in listed
            if item[0].delta is not None and 0.35 <= abs(item[0].delta) <= 0.45
            and direction * item[0].delta > 0]
    if band:
        return ranked(band)
    with_delta = [item for item in listed
                  if item[0].delta is not None and direction * item[0].delta > 0]
    if with_delta:
        return ranked(with_delta)
    if not atr or atr <= 0:
        return min(listed, key=lambda item: (abs(item[0].strike - snap.spot), item[0].symbol))[0]
    minute = _minute_index(snap.bars[-1].end if snap.bars else snap.at)
    def distance(item):
        option, dte = item
        target = snap.spot + direction * 0.25 * (atr / 1.4) * ((390 - minute + 390 * dte) / 390) ** 0.5
        return (abs(option.strike - target), option.symbol)
    return min(listed, key=distance)[0]


def detect_f4(snap: Snapshot, direction: int):
    """Energy 5m flag. First flag length that has a pole is the only one judged."""
    atr = snap.prior_atr21
    if not atr or atr <= 0 or len(snap.bars) < 5 or len(snap.bars) % 5 != 0:
        return None
    bars5 = aggregate(snap.bars, 5)
    if not bars5:
        return None
    index = len(bars5) - 1
    if not 4 <= index <= 71:
        return None
    opens, highs, lows, closes, volumes = [], [], [], [], []
    for bar in bars5:
        opened, high, low, closed = _cs(bar, direction)
        opens.append(opened)
        highs.append(high)
        lows.append(low)
        closes.append(closed)
        volumes.append(bar.volume)
    for length in range(2, 7):
        pole_end = index - length - 1
        if pole_end < 0:
            break
        pole = None
        for size in range(1, 5):
            pole_start = pole_end - size + 1
            if pole_start < 0:
                break
            pole_high = max(highs[pole_start:pole_end + 1])
            pole_low = min(lows[pole_start:pole_end + 1])
            low_at = min(range(pole_start, pole_end + 1), key=lambda i: lows[i])
            high_at = max(range(pole_start, pole_end + 1), key=lambda i: highs[i])
            if (closes[pole_end] > opens[pole_start]
                    and highs[pole_end] >= pole_high - 0.05 * atr
                    and (pole_high - pole_low) >= 0.60 * atr
                    and low_at <= high_at):
                pole = (pole_high, pole_low, size)
                break
        if pole is None:
            continue
        pole_high, pole_low, size = pole
        flag = slice(index - length, index)
        flag_high = max(highs[flag])
        flag_low = min(lows[flag])
        span = pole_high - pole_low
        retrace = (pole_high - flag_low) / span if span else 99
        touches = sum(1 for high in highs[flag] if high >= flag_high - 0.10 * atr)
        features = {"pole": _num((pole_high - pole_low) / atr), "nf": length, "k": size,
                    "touches": touches, "retr": _num(retrace)}
        if retrace <= 0.50 and flag_high <= pole_high + 0.05 * atr and touches <= 2:
            prior = volumes[max(0, index - 10):index]
            breakout_volume = bool(prior) and volumes[index] >= 1.5 * (sum(prior) / len(prior))
            relative = _rv5(snap.bars, index)
            relative_ok = relative is not None and relative >= 1.5
            features.update(rv5=_num(relative), bv=int(breakout_volume))
            if closes[index] > flag_high + 0.02 * atr and (breakout_volume or relative_ok):
                trigger = direction * (flag_high + 0.02 * atr)
                invalid = direction * flag_low
                return {"family": "F4", "setup": Setup("WAVE_F4_FLAG", trigger, invalid),
                        "features": features}
        else:
            features.update(rv5=None, bv=0)
        break
    return None


def _base(index, bars2, bars5, ema21, ema9, vwaps, atr, direction, bars):
    if index < 5 or atr <= 0:
        return None
    end_index = 2 * index + 1
    if end_index >= len(bars):
        return None
    count5 = (end_index + 1) // 5
    if count5 < 3 or count5 > len(bars5):
        return None
    window = bars[:end_index + 1]
    extreme = max(bar.high for bar in window) if direction > 0 else min(bar.low for bar in window)
    vwap = vwaps[end_index]
    if direction * extreme - direction * vwap < 0.30 * atr:
        return None
    base = bars5[count5 - 3:count5]
    highs, lows = [], []
    tagged = False
    for offset, bar in enumerate(base):
        absolute = count5 - 3 + offset
        _, high, low, closed = _cs(bar, direction)
        ema_now = direction * ema9[absolute]
        vwap_index = 5 * absolute + 4
        if vwap_index >= len(vwaps):
            return None
        vwap_now = direction * vwaps[vwap_index]
        if not (closed > ema_now and closed > vwap_now):
            return None
        if low <= ema_now + 0.03 * atr:
            tagged = True
        highs.append(high)
        lows.append(low)
    if not tagged or max(highs) - min(lows) > 0.40 * atr:
        return None
    _, _, _, close2 = _cs(bars2[index], direction)
    vwap_now = direction * vwap
    if not (0 < close2 - vwap_now <= 0.60 * atr):
        return None
    ema_now = direction * ema21[index]
    ema_then = direction * ema21[index - 5]
    if not (ema_now > vwap_now and ema_now > ema_then and close2 > ema_now):
        return None
    return {
        "base_high_cs": max(highs), "base_low_cs": min(lows),
        "base_high": max(bar.high for bar in base), "base_low": min(bar.low for bar in base),
        "e9": ema9[count5 - 1], "vwap": vwap, "e21": ema21[index],
    }


def detect_f7(snap: Snapshot, direction: int) -> list[dict]:
    """F7a rejection, F7b base break, F7c expansion. Logged, never posted."""
    atr = snap.prior_atr21
    if not atr or atr <= 0 or len(snap.bars) < 16 or len(snap.bars) % 2 != 0:
        return []
    bars2 = aggregate(snap.bars, 2)
    bars5 = aggregate(snap.bars, 5)
    if len(bars2) < 6 or len(bars5) < 3:
        return []
    ema21 = ema([bar.close for bar in bars2], 21)
    ema9 = ema([bar.close for bar in bars5], 9)
    vwaps = _vwap_series(snap.bars)
    index = len(bars2) - 1
    current = _base(index, bars2, bars5, ema21, ema9, vwaps, atr, direction, snap.bars)
    events = []
    bar = bars2[index]
    _, _, low2, close2 = _cs(bar, direction)
    body = abs(bar.close - bar.open)
    if current is not None:
        ema_now = direction * current["e9"]
        wick = close2 - low2
        shared = {
            "base_high": _num(current["base_high"]), "base_low": _num(current["base_low"]),
            "e9_5": _num(current["e9"]), "e21_2": _num(current["e21"]),
            "vwap": _num(current["vwap"]), "wick": _num(wick), "body": _num(body),
        }
        if low2 <= ema_now + 0.03 * atr and close2 > _cs(bar, direction)[0] and wick >= 0.05 * atr:
            events.append({"family": "F7A", "setup": Setup(
                "WAVE_F7A", bar.close, bar.low if direction > 0 else bar.high),
                "features": {**shared, "reg": 1, "wick03": 1}})
        elif low2 <= ema_now + 0.03 * atr and close2 > _cs(bar, direction)[0] and wick >= 0.03 * atr:
            events.append({"family": "F7A_WICK", "setup": Setup(
                "WAVE_F7A_WICK", bar.close, bar.low if direction > 0 else bar.high),
                "features": {**shared, "reg": 0, "wick03": 1}})
    previous = _base(index - 1, bars2, bars5, ema21, ema9, vwaps, atr, direction, snap.bars)
    if previous is not None and close2 > previous["base_high_cs"] + 0.02 * atr:
        trigger = direction * (previous["base_high_cs"] + 0.02 * atr)
        invalid = direction * previous["base_low_cs"]
        events.append({"family": "F7B", "setup": Setup("WAVE_F7B", trigger, invalid), "features": {
            "base_high": _num(previous["base_high"]), "base_low": _num(previous["base_low"]),
            "e9_5": _num(previous["e9"]), "e21_2": _num(previous["e21"]),
            "vwap": _num(previous["vwap"]), "body": _num(body),
        }})
    latest = None
    for older in range(max(0, index - 15), index):
        found = _base(older, bars2, bars5, ema21, ema9, vwaps, atr, direction, snap.bars)
        if found is not None:
            latest = found
    if latest is not None and body >= 0.12 * atr and close2 > latest["base_high_cs"]:
        events.append({"family": "F7C", "setup": Setup(
            "WAVE_F7C", bar.close, bar.low if direction > 0 else bar.high), "features": {
                "base_high": _num(latest["base_high"]), "base_low": _num(latest["base_low"]),
                "e9_5": _num(latest["e9"]), "e21_2": _num(latest["e21"]),
                "vwap": _num(latest["vwap"]), "body": _num(body),
            }})
    return events


def detect_f1(snap: Snapshot, direction: int):
    """5m close reclaim of VWAP (calls) or loss of VWAP (puts)."""
    if len(snap.bars) < 10 or len(snap.bars) % 5 != 0:
        return None
    bars5 = aggregate(snap.bars, 5)
    if len(bars5) < 2:
        return None
    vwaps = _vwap_series(snap.bars)
    index = len(bars5) - 1
    previous, current = bars5[index - 1], bars5[index]
    previous_vwap = vwaps[5 * (index - 1) + 4]
    current_vwap = vwaps[5 * index + 4]
    if direction > 0:
        crossed = previous.close <= previous_vwap and current.close > current_vwap
        invalid = current.low
    else:
        crossed = previous.close >= previous_vwap and current.close < current_vwap
        invalid = current.high
    if not crossed:
        return None
    return {"family": "F1", "setup": Setup("WAVE_F1_VWAP", current_vwap, invalid), "features": {
        "px": _num(current.close), "vw": _num(current_vwap), "prev_vw": _num(previous_vwap),
    }}


def detect_f2(snap: Snapshot, direction: int):
    """Failed break of the prior session high (puts) or low (calls)."""
    if len(snap.bars) < 10 or len(snap.bars) % 5 != 0:
        return None
    bars5 = aggregate(snap.bars, 5)
    if len(bars5) < 2:
        return None
    index = len(bars5) - 1
    current = bars5[index]
    prior = snap.bars[:5 * index]
    if not prior:
        return None
    if direction < 0:
        level = max(bar.high for bar in prior)
        if current.high > level and current.close < level:
            return {"family": "F2", "setup": Setup("WAVE_F2_FAIL", level, current.high), "features": {
                "lvl": _num(level), "ext": _num(current.high),
            }}
    else:
        level = min(bar.low for bar in prior)
        if current.low < level and current.close > level:
            return {"family": "F2", "setup": Setup("WAVE_F2_FAIL", level, current.low), "features": {
                "lvl": _num(level), "ext": _num(current.low),
            }}
    return None


def detect_signals(snap: Snapshot, direction: int, settings: Settings):
    if not snap.bars:
        return [], None
    if not anchored(snap.bars):
        return [], "wave: session not anchored"
    atr = snap.prior_atr21
    if atr is None or atr <= 0:
        return [], "wave: atr21 unavailable"
    local = snap.bars[-1].end.astimezone(ET)
    if not time(9, 40) <= local.time() <= time(15, 30):
        return [], None
    events = []
    if local.minute % 5 == 0 and _family_on(settings, settings.wave_f4_shadow):
        found = detect_f4(snap, direction)
        if found:
            events.append(found)
    if local.minute % 5 == 0 and _family_on(settings, settings.wave_f1_shadow):
        found = detect_f1(snap, direction)
        if found:
            events.append(found)
    if local.minute % 5 == 0 and _family_on(settings, settings.wave_f2_shadow):
        found = detect_f2(snap, direction)
        if found:
            events.append(found)
    if local.minute % 2 == 0 and _family_on(settings, settings.wave_f7_shadow):
        events.extend(detect_f7(snap, direction))
    return events, None


def _h2(snap: Snapshot, direction: int, atr: float):
    """Within 0.10 ATR under an unbroken HOD/PMH, mirrored for puts."""
    if direction > 0:
        levels = [("HOD", max(bar.high for bar in snap.bars))]
        premarket = snap.premarket_high
        if premarket and premarket > 0 and levels[0][1] <= premarket:
            levels.append(("PMH", premarket))
        spot = snap.spot
        best = None
        for name, level in levels:
            if spot < level:
                distance = (level - spot) / atr
                if 0 < distance <= SPY_NEAR_ATR and (best is None or distance < best[0]):
                    best = (distance, name)
        hod_distance = (levels[0][1] - spot) / atr
        if best:
            return 1, best[0], best[1]
        return 0, hod_distance, "HOD"
    levels = [("LOD", min(bar.low for bar in snap.bars))]
    premarket = snap.premarket_low
    if premarket and premarket > 0 and levels[0][1] >= premarket:
        levels.append(("PML", premarket))
    best = None
    for name, level in levels:
        if snap.spot > level:
            distance = (snap.spot - level) / atr
            if 0 < distance <= SPY_NEAR_ATR and (best is None or distance < best[0]):
                best = (distance, name)
    lod_distance = (snap.spot - levels[0][1]) / atr
    if best:
        return 1, best[0], best[1]
    return 0, lod_distance, "LOD"


def card_tags(snap: Snapshot, direction: int, spy: Snapshot | None) -> dict:
    atr = snap.prior_atr if snap.prior_atr and snap.prior_atr > 0 else None
    if direction > 0:
        v6 = None if snap.prior_high_20d is None else int(broke_20d_range(snap, direction))
    else:
        v6 = None if snap.prior_low_20d is None else int(broke_20d_range(snap, direction))
    h2 = h2_distance = h2_level = None
    thrust = v3 = day_move = None
    if atr and snap.bars:
        h2, h2_distance, h2_level = _h2(snap, direction, atr)
        if len(snap.bars) >= 3:
            thrust = direction * (snap.bars[-1].close - snap.bars[-3].close) / atr
            v3 = int(thrust < 0.15)
        if snap.bars[0].open:
            day_move = (snap.spot - snap.bars[0].open) / atr
    relative = None
    if spy is not None and spy.bars and snap.bars and snap.bars[0].open and spy.bars[0].open:
        relative = (snap.spot / snap.bars[0].open - 1) - (spy.spot / spy.bars[0].open - 1)
    spy_distance = spy_near = None
    if spy is not None and spy.bars and spy.prior_atr and spy.prior_atr > 0:
        spy_distance = (max(bar.high for bar in spy.bars) - spy.spot) / spy.prior_atr
        spy_near = int(0 <= spy_distance <= SPY_NEAR_ATR)
    return {
        "v6": v6, "h2": h2, "h2d": _num(h2_distance), "h2l": h2_level,
        "v3": v3, "th2": _num(thrust), "day": _num(day_move),
        "day_dir": None if day_move is None else _num(direction * day_move),
        "rs": _num(relative), "spy_hod": _num(spy_distance), "spy_010": spy_near,
        "atr": _num(atr), "atr21": _num(snap.prior_atr21), "x": SPY_NEAR_ATR,
    }


def _modifiers(snap: Snapshot, direction: int, spy, peer, atr21):
    bars = snap.bars
    opened = bars[0].open if bars else None
    signed_day = direction * (snap.spot - opened) if opened else None
    dm2 = int(atr21 and atr21 > 0 and signed_day is not None and signed_day >= 2 * atr21)
    relative = None
    relative_ok = 0
    if spy is not None and spy.bars and opened and spy.bars[0].open:
        relative = (snap.spot / opened - 1) - (spy.spot / spy.bars[0].open - 1)
        relative_ok = int(direction * relative > 0.005)
    spy_extreme = None
    spy_flag = 0
    if spy is not None and spy.bars and spy.prior_atr21 and spy.prior_atr21 > 0:
        if direction > 0:
            spy_extreme = (max(bar.high for bar in spy.bars) - spy.spot) / spy.prior_atr21
        else:
            spy_extreme = (spy.spot - min(bar.low for bar in spy.bars)) / spy.prior_atr21
        spy_flag = int(0 <= spy_extreme <= SPY_NEAR_ATR)
    peer_ok = 0
    if peer is not None and peer.bars and peer.bars[0].open:
        peer_vwap = _vwap_series(peer.bars)[-1]
        peer_return = peer.spot / peer.bars[0].open - 1
        if direction * (peer.spot - peer_vwap) > 0 and direction * peer_return > 0:
            peer_ok = 1
    aligned = 0
    bars2 = aggregate(bars, 2) if bars else ()
    if len(bars2) >= 6:
        ema21 = ema([bar.close for bar in bars2], 21)
        vwap = _vwap_series(bars)[-1]
        slope = ema21[-1] - ema21[-6]
        if direction * (ema21[-1] - vwap) > 0 and direction * slope > 0:
            aligned = 1
    seeded = 0
    if bars:
        seeded = int(bars[-1].end.astimezone(ET).time() < time(10, 15))
    return {
        "dm2": dm2, "rs": _num(relative), "rs_ok": relative_ok,
        "spy_hi": spy_flag, "spy_x": _num(spy_extreme), "peer": peer_ok,
        "emaal": aligned, "ema_seed": seeded,
    }


def _row(snap: Snapshot, family: str, side: str, setup: Setup | None, features: dict,
         contract, card_id=None, bar_end=None):
    return {
        "day": snap.day, "symbol": snap.symbol, "at": snap.at.isoformat(),
        "family": family, "side": side,
        "bar_end": (bar_end or snap.bars[-1].end).isoformat(),
        "trigger": None if setup is None else setup.trigger,
        "invalidation": None if setup is None else setup.invalidation,
        "spot": snap.spot, "atr21": snap.prior_atr21,
        "contract": None if contract is None else contract.symbol,
        "ask": None if contract is None else contract.ask,
        "bid": None if contract is None else contract.bid,
        "delta": None if contract is None else contract.delta,
        "card_alert_id": card_id, "features": features,
    }


def _cooled(store, snap: Snapshot, family: str, side: str, minutes: int) -> bool:
    if minutes <= 0:
        return True
    row = store.db.execute(
        """SELECT at FROM wave_shadow
           WHERE day=? AND symbol=? AND family=? AND side=? ORDER BY at DESC LIMIT 1""",
        (snap.day, snap.symbol, family, side)).fetchone()
    if row is None:
        return True
    return snap.at - datetime.fromisoformat(row["at"]) >= timedelta(minutes=minutes)


def _nearby_alert(store, snap: Snapshot, side: str):
    best = None
    for row in store.db.execute(
            "SELECT id, at FROM alerts WHERE day=? AND symbol=? AND side=?",
            (snap.day, snap.symbol, side)):
        delta = abs(datetime.fromisoformat(row["at"]) - snap.at)
        if delta <= CARD_WINDOW and (best is None or delta < best[0]):
            best = (delta, row["id"])
    return None if best is None else best[1]


def _link_card(store, snap: Snapshot, side: str, alert_id: str) -> None:
    rows = store.db.execute(
        """SELECT symbol, bar_end, family, side, at FROM wave_shadow
           WHERE day=? AND symbol=? AND side=? AND card_alert_id IS NULL""",
        (snap.day, snap.symbol, side)).fetchall()
    with store.db:
        for row in rows:
            if abs(datetime.fromisoformat(row["at"]) - snap.at) <= CARD_WINDOW:
                store.db.execute(
                    """UPDATE wave_shadow SET card_alert_id=?
                       WHERE symbol=? AND bar_end=? AND family=? AND side=?""",
                    (alert_id, row["symbol"], row["bar_end"], row["family"], row["side"]))


def _write(store, memory, row) -> None:
    status = store.wave_shadow(row)
    if status == "inserted":
        LOG.info("wave_shadow %s %s %s %s", row["family"], row["symbol"], row["side"], row["at"])
    elif status == "capped" and not memory.get("cap_warned"):
        LOG.warning("wave_shadow daily cap reached for %s; further new rows are skipped", row["day"])
        memory["cap_warned"] = True


def checkpoints(bars, after_end: datetime, direction: int, atr: float | None, deadline: datetime):
    """First quick 2m pull-in that holds EMA9, and the first rejection/resumption bar."""
    bars2 = aggregate(bars, 2)
    if len(bars2) < 2:
        return None, None
    emas = ema([bar.close for bar in bars2], 9)
    favorable = None
    for bar in bars:
        if bar.end > after_end:
            break
        extreme = bar.high if direction > 0 else bar.low
        favorable = extreme if favorable is None else (
            max(favorable, extreme) if direction > 0 else min(favorable, extreme))
    pull = reject = None
    previous = None
    for bar, ema9 in zip(bars2, emas):
        if direction > 0:
            held = bar.close >= ema9 and bar.low <= ema9
            directional = bar.close > bar.open
            wick = bar.close - bar.low
            tested = atr is None or bar.low <= ema9 + 0.03 * atr
            adverse = bar.low
            resumes = previous is not None and bar.close > previous.high
        else:
            held = bar.close <= ema9 and bar.high >= ema9
            directional = bar.close < bar.open
            wick = bar.high - bar.close
            tested = atr is None or bar.high >= ema9 - 0.03 * atr
            adverse = bar.high
            resumes = previous is not None and bar.close < previous.low
        if after_end < bar.end <= deadline:
            depth = None if favorable is None else direction * (favorable - adverse)
            quick = (depth is not None and depth > 0 and bar.end <= after_end + QUICK_PULLIN
                     and (atr is None or depth <= 0.50 * atr))
            if pull is None and held and quick:
                pull = {"bar": bar, "ema": ema9, "depth": depth}
            wick_ok = atr is not None and wick >= 0.03 * atr
            if reject is None and held and directional and tested and (wick_ok or resumes):
                reject = {"bar": bar, "ema": ema9, "wick": wick}
        if favorable is not None:
            favorable = max(favorable, bar.high) if direction > 0 else min(favorable, bar.low)
        elif bar.end <= after_end:
            favorable = bar.high if direction > 0 else bar.low
        previous = bar
    return pull, reject


def _log_card(store, settings, candidate, alert_id, memory) -> None:
    snap = candidate.snapshot
    direction = 1 if candidate.option.side == "CALL" else -1
    spy = snap if snap.symbol == "SPY" else memory["symbols"].get("SPY")
    features = card_tags(snap, direction, spy)
    features.update(score=_num(candidate.score, 2),
                    setup=None if candidate.setup is None else candidate.setup.name,
                    max_bid=candidate.option.bid, min_bid=candidate.option.bid,
                    max_ask=candidate.option.ask, min_ask=candidate.option.ask,
                    n=1, q_at=candidate.option.quote_time.isoformat())
    _write(store, memory, _row(
        snap, "CARD", candidate.option.side, candidate.setup, features, candidate.option,
        card_id=alert_id))
    _link_card(store, snap, candidate.option.side, alert_id)


def update_paths(store, snap: Snapshot, settings: Settings, memory=None) -> None:
    if not snap.bars:
        return
    window_start = (snap.at - CARD_WINDOW).isoformat()
    alerts = store.db.execute(
        "SELECT id, at, contract, side FROM alerts WHERE day=? AND symbol=? AND at>=?",
        (snap.day, snap.symbol, window_start)).fetchall()
    atr = snap.prior_atr if snap.prior_atr and snap.prior_atr > 0 else None
    for alert in alerts:
        card_at = datetime.fromisoformat(alert["at"])
        if card_at > snap.at or snap.at > card_at + CARD_WINDOW:
            continue
        card = store.db.execute(
            "SELECT bar_end, features FROM wave_shadow WHERE card_alert_id=? AND family='CARD'",
            (alert["id"],)).fetchone()
        if card is None:
            continue
        option = next((item for item in snap.options if item.symbol == alert["contract"]), None)
        features = json.loads(card["features"])
        changed = False
        if option is not None and quote_ok(option, snap, settings):
            if features.get("q_at") != option.quote_time.isoformat():
                features["max_bid"] = max(features.get("max_bid", option.bid), option.bid)
                features["min_bid"] = min(features.get("min_bid", option.bid), option.bid)
                features["max_ask"] = max(features.get("max_ask", option.ask), option.ask)
                features["min_ask"] = min(features.get("min_ask", option.ask), option.ask)
                features["n"] = int(features.get("n") or 0) + 1
                features["q_at"] = option.quote_time.isoformat()
                changed = True
        direction = 1 if alert["side"] == "CALL" else -1
        after = datetime.fromisoformat(card["bar_end"])
        pull, reject = checkpoints(snap.bars, after, direction, atr, card_at + CARD_WINDOW)
        have = {row["family"] for row in store.db.execute(
            "SELECT family FROM wave_shadow WHERE card_alert_id=? AND family IN ('CARD_PULLIN','CARD_REJECT')",
            (alert["id"],))}
        quote = option if option is not None and option.ask > 0 else None
        shadow_memory = memory if memory is not None else {"cap_warned": False}
        if pull is not None and "pull_at" not in features:
            bar = pull["bar"]
            features.update(pull_at=bar.end.isoformat(),
                            pull_ask=None if quote is None else quote.ask,
                            pull_bid=None if quote is None else quote.bid)
            changed = True
            if "CARD_PULLIN" not in have:
                _write(store, shadow_memory, _row(
                    snap, "CARD_PULLIN", alert["side"],
                    Setup("CARD_PULLIN", bar.close, bar.low if direction > 0 else bar.high),
                    {"ema9": _num(pull["ema"]), "depth": _num(pull["depth"]),
                     "mins": _num((bar.end - after).total_seconds() / 60, 2),
                     "lag": _num((snap.at - bar.end).total_seconds(), 1), "px": _num(bar.close)},
                    quote, card_id=alert["id"], bar_end=bar.end))
        if reject is not None and "rej_at" not in features:
            bar = reject["bar"]
            features.update(rej_at=bar.end.isoformat(),
                            rej_ask=None if quote is None else quote.ask,
                            rej_bid=None if quote is None else quote.bid)
            changed = True
            if "CARD_REJECT" not in have:
                _write(store, shadow_memory, _row(
                    snap, "CARD_REJECT", alert["side"],
                    Setup("CARD_REJECT", bar.close, bar.low if direction > 0 else bar.high),
                    {"ema9": _num(reject["ema"]), "wick": _num(reject["wick"]),
                     "mins": _num((bar.end - after).total_seconds() / 60, 2),
                     "lag": _num((snap.at - bar.end).total_seconds(), 1), "px": _num(bar.close)},
                    quote, card_id=alert["id"], bar_end=bar.end))
        if changed:
            store.wave_shadow_features(snap.symbol, card["bar_end"], "CARD", alert["side"], features)


def _log_signals(store, settings, snap: Snapshot, memory) -> None:
    spy = snap if snap.symbol == "SPY" else memory["symbols"].get("SPY")
    peer_name = SECTORS.get(snap.symbol)
    peer = memory["symbols"].get(peer_name) if peer_name else None
    for direction in (1, -1):
        events, blocker = detect_signals(snap, direction, settings)
        if blocker:
            key = (snap.day, snap.symbol, blocker)
            if key not in memory["skips"]:
                memory["skips"].add(key)
                LOG.info("wave shadow %s %s %s", snap.symbol, snap.day, blocker)
        if not events:
            continue
        contract = pick_contract(snap, direction, snap.prior_atr21)
        shared = _modifiers(snap, direction, spy, peer, snap.prior_atr21)
        side = _side(direction)
        for event in events:
            if not _cooled(store, snap, event["family"], side, settings.wave_cooldown_minutes):
                continue
            features = {**event["features"], **shared}
            _write(store, memory, _row(
                snap, event["family"], side, event["setup"], features, contract,
                card_id=_nearby_alert(store, snap, side)))


def run_shadow(store, settings: Settings, fresh, posted, batch, memory) -> None:
    stamps = [snap.at for snap in list(batch) + list(fresh)]
    stamps.extend(item[0].snapshot.at for item in posted)
    if stamps:
        day = max(stamps).astimezone(ET).date().isoformat()
        if memory.get("purged") != day:
            store.purge_wave_shadow(day, KEEP_DAYS)
            memory["purged"] = day
    for snap in list(batch) + list(fresh):
        if snap.bars:
            memory["symbols"][snap.symbol] = snap
    if _family_on(settings, settings.wave_card_shadow):
        for candidate, alert_id in posted:
            _log_card(store, settings, candidate, alert_id, memory)
        for snap in fresh:
            update_paths(store, snap, settings, memory)
    if any(_family_on(settings, flag) for flag in (
            settings.wave_f4_shadow, settings.wave_f7_shadow,
            settings.wave_f1_shadow, settings.wave_f2_shadow)):
        for snap in fresh:
            _log_signals(store, settings, snap, memory)
