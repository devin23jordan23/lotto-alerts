"""Price paths measurable before the next leg. No eventual HOD/LOD features."""
from dataclasses import dataclass
from math import isfinite

from .models import Snapshot


@dataclass(frozen=True)
class Setup:
    name: str
    trigger: float
    invalidation: float
    building: bool = False


def path_features(snap: Snapshot, direction: int) -> dict:
    bars = snap.bars
    closes = [b.close for b in bars]
    volume = sum(b.volume for b in bars)
    cumul_volume, cumul_value, vwaps = 0, 0.0, []
    for bar in bars:
        cumul_volume += bar.volume
        cumul_value += (bar.high + bar.low + bar.close) / 3 * bar.volume
        vwaps.append(cumul_value / max(1, cumul_volume))
    def ema(period):
        value = closes[0]
        result = []
        for close in closes:
            value += 2 / (period + 1) * (close - value)
            result.append(value)
        return result
    ema9, ema20 = ema(9), ema(20)
    high, low = max(b.high for b in bars), min(b.low for b in bars)
    previous_high = max(b.high for b in bars[:-1])
    previous_low = min(b.low for b in bars[:-1])
    opening_high, opening_low = max(b.high for b in bars[:10]), min(b.low for b in bars[:10])
    old = bars[:-5]
    old_volume = sum(b.volume for b in old)
    baseline = bars[-1].expected_cumulative_volume
    old_baseline = old[-1].expected_cumulative_volume
    pace = volume / baseline if baseline and baseline > 0 else 0
    old_pace = old_volume / old_baseline if old_baseline and old_baseline > 0 else 0
    last5 = sum(b.volume for b in bars[-5:])
    prev5 = sum(b.volume for b in bars[-10:-5])
    expected5 = baseline-old_baseline if baseline and old_baseline else 0
    opposing = sum(b.volume for b in bars[-5:] if direction * (b.close-b.open) < 0)
    path = sum(abs(closes[i]-closes[i-1]) for i in range(len(bars)-10, len(bars)))
    atr = snap.prior_atr if snap.prior_atr and isfinite(snap.prior_atr) and snap.prior_atr > 0 else None
    ret5 = direction * (closes[-1]/closes[-6]-1)
    ret_previous = direction * (closes[-6]/closes[-11]-1)
    extreme = high if direction == 1 else low
    adverse = low if direction == 1 else high
    # Each historical bar is compared with VWAP known at that bar, not today's final VWAP.
    acceptance = sum(direction*(b.close-v) > 0 for b, v in zip(bars[-5:], vwaps[-5:]))/5
    near = sum(direction*(extreme-b.close)/extreme <= .01 for b in bars[-10:])/10
    return {
        "pace_rvol": pace, "pace_persistence": pace/old_pace if old_pace else 0,
        "volume_acceleration": last5/max(1, prev5), "return_5m_directional": ret5,
        "local_rvol_5m": last5/expected5 if expected5 > 0 else 0,
        "return_3m_directional": direction*(closes[-1]/closes[-4]-1),
        "price_acceleration": ret5-ret_previous, "extreme_distance": direction*(extreme-snap.spot)/extreme,
        "vwap": vwaps[-1], "vwap_slope_directional": direction*(vwaps[-1]-vwaps[-6]),
        "above_vwap_share": acceptance, "efficiency": abs(closes[-1]-closes[-11])/path if path else 0,
        "opposing_volume_share": opposing/max(1, last5), "near_extreme_share": near,
        "return_from_close": snap.spot/snap.prior_close-1,
        "opening_gap": bars[0].open/snap.prior_close-1,
        "return_from_open": snap.spot/bars[0].open-1,
        "return_from_adverse_extreme": direction*(snap.spot/adverse-1),
        "return_from_low": snap.spot/low-1, "return_from_high": snap.spot/high-1,
        "opening_high": opening_high, "opening_low": opening_low,
        "previous_high": previous_high, "previous_low": previous_low,
        "ema9": ema9[-1], "ema20": ema20[-1],
        "ema_slope_directional": direction*(ema9[-1]-ema9[-4]),
        "atr_available": int(atr is not None), "session_range_atr": (high-low)/atr if atr else 0,
        "move_from_open_atr": direction*(snap.spot-bars[0].open)/atr if atr else 0,
        "vwap_reclaimed": int(direction*(bars[-1].close-vwaps[-1]) > 0 and
                              any(direction*(b.close-v) <= 0 for b,v in zip(bars[-10:-1], vwaps[-10:-1]))),
    }


def detect_setup(snap: Snapshot, direction: int, metrics: dict) -> Setup | None:
    bars, spot = snap.bars, snap.spot
    vwap = metrics["vwap"]
    if direction*(spot-vwap) <= 0:
        return None
    boundary = metrics["opening_high"] if direction == 1 else metrics["opening_low"]
    both_levels = (direction*(spot-bars[0].open) > 0 and direction*(spot-snap.prior_close) > 0)
    if len(bars) <= 45 and both_levels and direction*(spot-boundary) > 0 and metrics["return_3m_directional"] > 0:
        return Setup("OPENING_BREAK", boundary, vwap)
    # Established strength + 30/60/120-minute compression near the directional box edge.
    for size in (120, 60, 30):
        if len(bars) < size+10:
            continue
        box = bars[-size:]
        upper, lower = max(b.high for b in box), min(b.low for b in box)
        width = upper-lower
        limit = max(spot*.006, (snap.prior_atr or 0)*.35)
        earlier = bars[:-size]
        origin = min(b.low for b in earlier) if direction == 1 else max(b.high for b in earlier)
        impulse = direction*(box[0].open/origin-1)
        position = (spot-lower)/width if direction == 1 and width else (upper-spot)/width if width else .5
        if (width <= limit and impulse >= .01 and position >= .60
                and metrics["above_vwap_share"] >= .8 and metrics["ema_slope_directional"] >= 0):
            metrics["consolidation_minutes"] = size
            metrics["box_high"], metrics["box_low"] = upper, lower
            trigger = upper if direction == 1 else lower
            invalidation = max(lower, vwap) if direction == 1 else min(upper, vwap)
            return Setup("COILED_CONTINUATION", trigger, invalidation, building=True)
    # An early reclaim may still be far from the opposite session extreme.
    if (metrics["vwap_reclaimed"] and metrics["return_from_adverse_extreme"] >= .004
            and metrics["return_3m_directional"] > 0 and metrics["ema_slope_directional"] > 0):
        level = max(b.high for b in bars[-6:-1]) if direction == 1 else min(b.low for b in bars[-6:-1])
        return Setup("REVERSAL", level, vwap, building=direction*(spot-level) <= 0)
    if (metrics["extreme_distance"] <= .01 and metrics["return_5m_directional"] > 0
            and metrics["efficiency"] >= .55 and metrics["above_vwap_share"] >= .8
            and metrics["vwap_slope_directional"] > 0):
        level = metrics["previous_high"] if direction == 1 else metrics["previous_low"]
        return Setup("CONTINUATION", level, vwap, building=direction*(spot-level) <= 0)
    return None
