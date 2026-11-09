"""Price paths measurable before the next leg. No eventual HOD/LOD features."""
from dataclasses import dataclass
from math import ceil, floor, isfinite

from .config import ATR_BASIS_VERSION, GATE_VERSION
from .models import ET, Snapshot


@dataclass(frozen=True)
class Setup:
    name: str
    trigger: float
    invalidation: float
    building: bool = False
    target: float | None = None


def path_features(snap: Snapshot, direction: int) -> dict:
    bars = snap.bars
    if len(bars) < 3:
        raise ValueError("Price research needs three completed bars")
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
    opening = bars[:min(10, len(bars)-1)]
    opening_high, opening_low = max(b.high for b in opening), min(b.low for b in opening)
    lookback = min(5, len(bars)//2)
    old = bars[:-lookback]
    old_volume = sum(b.volume for b in old)
    baseline = bars[-1].expected_cumulative_volume
    old_baseline = old[-1].expected_cumulative_volume
    pace = volume / baseline if baseline and baseline > 0 else 0
    old_pace = old_volume / old_baseline if old_baseline and old_baseline > 0 else 0
    last5 = sum(b.volume for b in bars[-lookback:])
    prev5 = sum(b.volume for b in bars[-2*lookback:-lookback])
    expected5 = baseline-old_baseline if baseline and old_baseline else 0
    opposing = sum(b.volume for b in bars[-lookback:] if direction * (b.close-b.open) < 0)
    efficiency_window = min(10, len(bars)-1)
    path = sum(abs(closes[i]-closes[i-1]) for i in range(len(bars)-efficiency_window, len(bars)))
    atr = snap.prior_atr if snap.prior_atr and isfinite(snap.prior_atr) and snap.prior_atr > 0 else None
    # Match the independent benchmark's five-minute horizon even while the
    # opening stock-volume comparison uses shorter, equally sized windows.
    reference5 = closes[-6] if len(bars)>=6 else bars[0].open
    ret5 = direction * (closes[-1]/reference5-1)
    ret_previous = direction * (closes[-6]/closes[-11]-1) if len(bars)>=11 else 0
    extreme = high if direction == 1 else low
    adverse = low if direction == 1 else high
    # Each historical bar is compared with VWAP known at that bar, not today's final VWAP.
    acceptance_bars = min(5, len(bars))
    acceptance = sum(direction*(b.close-v) > 0 for b, v in zip(bars[-acceptance_bars:], vwaps[-acceptance_bars:]))/acceptance_bars
    near = sum(direction*(extreme-b.close)/extreme <= .01 for b in bars[-10:])/len(bars[-10:])
    extreme_index = max(range(len(bars)), key=lambda i: direction*(bars[i].high if direction == 1 else bars[i].low))
    after_extreme = bars[extreme_index:]
    pullback = min(b.low for b in after_extreme) if direction == 1 else max(b.high for b in after_extreme)
    pivot = max(b.high for b in bars[-6:-1]) if direction == 1 else min(b.low for b in bars[-6:-1])
    # Broad price-dependent increments supply context, never a standalone signal.
    step = 50 if snap.spot>=1000 else 10 if snap.spot>=500 else 5 if snap.spot>=100 else 1 if snap.spot>=20 else .5
    round_level = (ceil(snap.spot/step) if direction==1 else floor(snap.spot/step))*step
    return {
        "pace_rvol": pace, "pace_persistence": pace/old_pace if old_pace else 0,
        "volume_acceleration": last5/max(1, prev5), "return_5m_directional": ret5,
        "local_rvol_5m": last5/expected5 if expected5 > 0 else 0,
        "return_3m_directional": direction*(closes[-1]/(closes[-4] if len(bars) >= 4 else bars[0].open)-1),
        "price_acceleration": ret5-ret_previous, "extreme_distance": direction*(extreme-snap.spot)/extreme,
        "vwap": vwaps[-1], "vwap_slope_directional": direction*(vwaps[-1]-vwaps[-1-lookback]),
        "above_vwap_share": acceptance, "efficiency": abs(closes[-1]-closes[-1-efficiency_window])/path if path else 0,
        "opposing_volume_share": opposing/max(1, last5), "near_extreme_share": near,
        "return_from_close": snap.spot/snap.prior_close-1,
        "opening_gap": bars[0].open/snap.prior_close-1,
        "return_from_open": snap.spot/bars[0].open-1,
        "return_from_adverse_extreme": direction*(snap.spot/adverse-1),
        "return_from_low": snap.spot/low-1, "return_from_high": snap.spot/high-1,
        "opening_high": opening_high, "opening_low": opening_low,
        "previous_high": previous_high, "previous_low": previous_low,
        "ema9": ema9[-1], "ema20": ema20[-1],
        "ema_slope_directional": direction*(ema9[-1]-(ema9[-4] if len(bars) >= 4 else ema9[0])),
        "atr_available": int(atr is not None), "session_range_atr": (high-low)/atr if atr else 0,
        "move_from_open_atr": direction*(snap.spot-bars[0].open)/atr if atr else 0,
        "volume_window_minutes": lookback,
        "impulse_atr": direction*(extreme-bars[0].open)/atr if atr else 0,
        "pullback_atr": direction*(extreme-pullback)/atr if atr else 0,
        "minutes_since_extreme": len(bars)-1-extreme_index,
        "pullback_extreme": pullback, "local_pivot": pivot,
        "distance_from_vwap_atr": direction*(snap.spot-vwaps[-1])/atr if atr else 0,
        "recent_move_atr": direction*(closes[-1]-closes[-1-lookback])/atr if atr else 0,
        "next_round_level": round_level,
        "vwap_reclaimed": int(direction*(bars[-1].close-vwaps[-1]) > 0 and
                              any(direction*(b.close-v) <= 0 for b,v in zip(bars[-10:-1], vwaps[-10:-1]))),
    }


def detect_setup(snap: Snapshot, direction: int, metrics: dict) -> Setup | None:
    bars, spot = snap.bars, snap.spot
    vwap = metrics["vwap"]
    atr = snap.prior_atr if metrics["atr_available"] else None
    pivot = metrics["local_pivot"]
    # A recovery can begin below session VWAP. Require a real local pivot break,
    # a turned short trend and measurable recovery; its first target is VWAP.
    if direction*(spot-vwap) <= 0:
        recovered = (direction*(spot-(min(b.low for b in bars[-20:]) if direction==1 else max(b.high for b in bars[-20:])))/atr
                     if atr else 0)
        if (len(bars)>=20 and atr and recovered>=.15 and metrics["recent_move_atr"]>=.06
                and metrics["efficiency"]>=.45 and direction*(spot-pivot)>0
                and direction*(metrics["ema9"]-metrics["ema20"])>0
                and metrics["ema_slope_directional"]>0):
            invalidation = min(b.low for b in bars[-3:]) if direction==1 else max(b.high for b in bars[-3:])
            return Setup("INTRADAY_REVERSAL",pivot,invalidation,target=vwap)
        return None
    session_extreme = max(b.high for b in bars) if direction == 1 else min(b.low for b in bars)
    # Opening momentum has its own short warmup and a recent price trigger. A
    # five-minute opening drive cannot wait for a ten-minute options comparison.
    if (len(bars) <= 20 and atr and metrics["impulse_atr"] >= .20
            and metrics["above_vwap_share"] >= .8 and metrics["return_3m_directional"] > 0
            and metrics["ema_slope_directional"] > 0 and metrics["efficiency"] >= .5
            and 0 < metrics["distance_from_vwap_atr"] <= .65
            and direction*(spot-bars[0].open) > 0):
        return Setup("OPENING_DRIVE", pivot, vwap, building=direction*(spot-pivot) <= 0)
    # Retain the original impulse while the stock rests or briefly loses VWAP.
    # The alert candidate appears only when a local recovery is actually visible.
    if (atr and metrics["impulse_atr"] >= .35 and metrics["minutes_since_extreme"] >= 5
            and metrics["pullback_atr"] >= .10 and metrics["above_vwap_share"] >= .6
            and metrics["return_3m_directional"] > 0 and metrics["ema_slope_directional"] > 0
            and direction*(spot-metrics["ema9"]) > 0 and metrics["recent_move_atr"] >= .04):
        local_risk = min(b.low for b in bars[-3:]) if direction==1 else max(b.high for b in bars[-3:])
        invalidation = max(metrics["pullback_extreme"], vwap, local_risk) if direction == 1 else min(metrics["pullback_extreme"], vwap, local_risk)
        return Setup("PULLBACK_RESUMPTION", pivot, invalidation,
                     building=direction*(spot-pivot) <= 0, target=session_extreme)
    # Levels are frozen pre-session or from already completed opening bars.
    # A touch alone is insufficient: require recent acceptance and direction.
    levels = ((snap.premarket_high, snap.prior_high, metrics["opening_high"])
              if direction == 1 else (snap.premarket_low, snap.prior_low, metrics["opening_low"]))
    if (atr and metrics["above_vwap_share"] >= .8 and metrics["recent_move_atr"] >= .04
            and metrics["ema_slope_directional"] > 0 and metrics["session_range_atr"] >= .25):
        for level in levels:
            if (level is not None and isfinite(level) and level > 0
                    and 0 <= direction*(spot-level) <= .25*atr
                    and any(direction*(b.close-level) <= 0 for b in bars[-6:-1])):
                return Setup("LEVEL_RECLAIM", level, vwap)
    boundary = metrics["opening_high"] if direction == 1 else metrics["opening_low"]
    both_levels = (direction*(spot-bars[0].open) > 0 and direction*(spot-snap.prior_close) > 0)
    if 11 <= len(bars) <= 45 and both_levels and direction*(spot-boundary) > 0 and metrics["return_3m_directional"] > 0:
        return Setup("OPENING_BREAK", boundary, vwap)
    # Established strength + 30/60/120-minute compression near the directional box edge.
    for size in (120, 60, 30):
        if len(bars) < size+10:
            continue
        box = bars[-size-1:-1]  # Current bar may break the already-established box.
        upper, lower = max(b.high for b in box), min(b.low for b in box)
        width = upper-lower
        limit = max(spot*.006, (snap.prior_atr or 0)*.35)
        earlier = bars[:-size-1]
        origin = min(b.low for b in earlier) if direction == 1 else max(b.high for b in earlier)
        impulse = direction*(box[0].open/origin-1)
        position = (spot-lower)/width if direction == 1 and width else (upper-spot)/width if width else .5
        if (width <= limit and impulse >= .01 and position >= .60
                and metrics["above_vwap_share"] >= .8 and metrics["ema_slope_directional"] >= 0):
            metrics["consolidation_minutes"] = size
            metrics["box_high"], metrics["box_low"] = upper, lower
            trigger = upper if direction == 1 else lower
            invalidation = max(lower, vwap) if direction == 1 else min(upper, vwap)
            return Setup("COILED_CONTINUATION", trigger, invalidation, building=direction*(spot-trigger) <= 0)
    # An early reclaim may still be far from the opposite session extreme.
    if (metrics["vwap_reclaimed"] and (metrics["return_from_adverse_extreme"] >= .004 or
                                     (atr and metrics["return_from_adverse_extreme"]*spot/atr>=.15))
            and metrics["return_3m_directional"] > 0 and metrics["ema_slope_directional"] > 0):
        level = max(b.high for b in bars[-6:-1]) if direction == 1 else min(b.low for b in bars[-6:-1])
        return Setup("REVERSAL", level, vwap, building=direction*(spot-level) <= 0)
    if (metrics["extreme_distance"] <= .01 and metrics["return_5m_directional"] > 0
            and metrics["efficiency"] >= .55 and metrics["above_vwap_share"] >= .8
            and metrics["vwap_slope_directional"] > 0):
        level = metrics["previous_high"] if direction == 1 else metrics["previous_low"]
        if (atr and direction*(level-spot)>.06*atr and metrics["recent_move_atr"]>=.06
                and direction*(metrics["ema9"]-metrics["ema20"])>0):
            invalidation = min(b.low for b in bars[-3:]) if direction==1 else max(b.high for b in bars[-3:])
            return Setup("RECOVERY_CONTINUATION",pivot,invalidation,
                         building=direction*(spot-pivot)<=0,target=level)
        return Setup("CONTINUATION", level, vwap, building=direction*(spot-level) <= 0)
    return None


def _positive_atr(value):
    return value is not None and isfinite(value) and value > 0


def true_range_atr(snap: Snapshot):
    """Live prior_atr: 14-day mean of RTH true range. None when missing."""
    value = getattr(snap, "prior_atr", None)
    return value if _positive_atr(value) else None


def research_atr(snap: Snapshot):
    """14-day mean of RTH high-low, the basis the dead-market thresholds were fit on."""
    value = getattr(snap, "prior_hl_atr", None)
    return value if _positive_atr(value) else None


def far_otm_time_metrics(snap: Snapshot, strike: float, expiry, direction: int, close, quote_time=None) -> dict:
    """0DTE strike distance scaled by the time left.

    otm_atr = direction*(strike-spot)/ATR, with ATR the high-low mean when it is
    present and the true range otherwise. otm_time_atr divides that by
    sqrt(minutes_left/390). minutes_left is the time to the session close,
    capped at 390 and floored at 5 once the close is still ahead. Missing ATR,
    a non-zero DTE, a missing spot, or a snapshot at/after the close never flags.
    Bars after `snap.at` are not read. Today's range is not used as ATR.
    """
    out = {
        "dte": None, "otm_atr": None, "minutes_left": None, "otm_time_atr": None,
        "atr_basis": "missing", "atr_value": None, "missing": None, "direction": direction,
        "strike": strike, "spot": snap.spot,
        "spot_time": snap.spot_time.isoformat() if getattr(snap, "spot_time", None) is not None else None,
        "quote_time": quote_time.isoformat() if quote_time is not None else None,
        "expiry": expiry.isoformat() if hasattr(expiry, "isoformat") else None,
        "session_close": close.isoformat() if close is not None and getattr(close, "tzinfo", None) else None,
    }
    hl, tr = research_atr(snap), true_range_atr(snap)
    if hl is not None:
        atr, basis = hl, "hl"
    elif tr is not None:
        atr, basis = tr, "tr"
    else:
        atr, basis = None, "missing"
    out["atr_basis"] = basis
    out["atr_value"] = atr
    try:
        local = snap.at.astimezone(ET)
        out["dte"] = (expiry - local.date()).days
    except (TypeError, ValueError, AttributeError):
        out["missing"] = "bad_expiry"
        return out
    if not _positive_atr(snap.spot) or not _positive_atr(strike):
        out["missing"] = "missing_spot"
        return out
    if direction not in (1, -1):
        out["missing"] = "bad_direction"
        return out
    if out["dte"] != 0:
        out["missing"] = "not_0dte"
        return out
    if close is None or getattr(close, "tzinfo", None) is None or getattr(snap.at, "tzinfo", None) is None:
        out["missing"] = "bad_close"
        return out
    raw_left = (close - snap.at).total_seconds() / 60
    if raw_left <= 0:
        out["missing"] = "at_or_after_close"
        return out
    if atr is None:
        out["missing"] = "missing_atr"
        return out
    left = max(5.0, min(390.0, raw_left))
    out["minutes_left"] = left
    out["otm_atr"] = direction * (strike - snap.spot) / atr
    out["otm_time_atr"] = out["otm_atr"] / (left / 390) ** .5
    return out


def dead_market_metrics(snap: Snapshot, direction: int, basis: str = "tr") -> dict:
    """Mirrored dead-market measures on completed regular-session bars.

    `basis="tr"` uses the live true-range ATR. `basis="hl"` uses the high-low
    mean and, if that value is missing, falls back to true range and sets
    atr_fallback. Missing ATR or fewer than three bars leaves the ratios None,
    which never blocks. The current session is not part of either ATR.
    """
    bars = snap.bars
    requested = basis if basis in {"hl", "tr"} else "tr"
    hl, tr = research_atr(snap), true_range_atr(snap)
    if requested == "hl" and hl is not None:
        atr, used, fallback = hl, "hl", 0
    elif tr is not None:
        atr, used, fallback = tr, "tr", int(requested == "hl")
    else:
        atr, used, fallback = None, None, int(requested == "hl")
    local = snap.at.astimezone(ET)
    minute = local.hour*60 + local.minute - 570
    out = {"session_minute": minute, "net30_atr": None, "vwap_abs_atr": None, "volume_ratio_5_25": None,
           "net5_atr": None, "off_extreme_atr": None, "session_range_atr": None,
           "atr_basis_requested": requested, "atr_basis_used": used, "atr_value": atr,
           "atr_fallback": fallback}
    if atr is None or len(bars) < 3:
        return out
    closes = [b.close for b in bars]
    n = len(bars)
    cumul_volume, cumul_value = 0, 0.0
    for bar in bars:
        cumul_volume += bar.volume
        cumul_value += (bar.high + bar.low + bar.close) / 3 * bar.volume
    vwap = cumul_value / max(1, cumul_volume)
    last = closes[-1]
    reference30 = closes[-30] if n > 30 else bars[0].open
    out["net30_atr"] = direction*(last-reference30)/atr
    out["vwap_abs_atr"] = abs(last-vwap)/atr
    reference5 = closes[-6] if n >= 6 else closes[0]
    out["net5_atr"] = direction*(last-reference5)/atr
    if n > 10:
        recent = [b.volume for b in bars[-5:]]
        prior = [b.volume for b in bars[-30:-5]]
        out["volume_ratio_5_25"] = (sum(recent)/len(recent)) / max(1, sum(prior)/len(prior))
    extreme = max(b.high for b in bars) if direction == 1 else min(b.low for b in bars)
    out["off_extreme_atr"] = direction*(extreme-last)/atr
    out["session_range_atr"] = (max(b.high for b in bars) - min(b.low for b in bars)) / atr
    return out


def dead_market_flags(dead: dict, settings, session_range_atr) -> dict:
    """Same DZ/BF/SP comparisons the live checks use. None ratios never flag."""
    minute = dead["session_minute"]
    net30 = dead["net30_atr"]
    return {
        "dead_zone": bool(net30 is not None
                          and settings.dead_zone_start_minute <= minute < settings.dead_zone_end_minute
                          and net30 < settings.dead_zone_max_net30_atr
                          and dead["vwap_abs_atr"] < settings.dead_zone_max_vwap_atr),
        "burst_flat": bool(dead["volume_ratio_5_25"] is not None and minute >= settings.burst_flat_start_minute
                           and dead["volume_ratio_5_25"] >= settings.burst_flat_min_volume_ratio
                           and dead["net5_atr"] < settings.burst_flat_max_net5_atr),
        "spent": bool(dead["off_extreme_atr"] is not None and session_range_atr is not None
                      and minute >= settings.spent_start_minute
                      and session_range_atr >= settings.spent_min_range_atr
                      and dead["off_extreme_atr"] >= settings.spent_min_off_extreme_atr),
    }


def apply_research_gates(metrics: dict, snap: Snapshot, option, direction: int, settings, close) -> dict:
    """Attach ATR and far-OTM telemetry. Blocking stays on true range unless mode is 2.

    Far-OTM blocks only when far_otm_time_mode is 2. Missing inputs do not flag.
    No market-direction gate and no loss kill-switch is applied here.
    """
    blocking_basis = "hl" if settings.dm_atr_basis_mode == 2 else "tr"
    dead = dead_market_metrics(snap, direction, blocking_basis)
    metrics.update({"dm_" + key: value for key, value in dead.items()})
    tr, hl = true_range_atr(snap), research_atr(snap)
    used = dead.get("atr_basis_used")
    metrics["dm_atr_basis"] = used or "missing"
    metrics["dm_atr_basis_version"] = ATR_BASIS_VERSION
    metrics["dm_prior_atr"] = tr
    metrics["dm_prior_hl_atr"] = hl
    metrics["dm_hl_over_tr"] = (hl / tr) if tr and hl else None
    if used == "hl":
        spent_range = dead.get("session_range_atr")
    else:
        spent_range = metrics.get("session_range_atr") if metrics.get("atr_available") else None
    flags = dead_market_flags(dead, settings, spent_range)
    metrics["dm_dead_zone"] = int(flags["dead_zone"])
    metrics["dm_burst_flat"] = int(flags["burst_flat"])
    metrics["dm_spent"] = int(flags["spent"])
    if settings.dm_atr_basis_mode >= 1 and hl is not None:
        hl_dead = dead if used == "hl" and not dead.get("atr_fallback") else dead_market_metrics(snap, direction, "hl")
        hl_flags = dead_market_flags(hl_dead, settings, hl_dead.get("session_range_atr"))
        for key in ("net30_atr", "vwap_abs_atr", "net5_atr", "off_extreme_atr", "session_range_atr", "volume_ratio_5_25"):
            metrics["dmhl_" + key] = hl_dead.get(key)
        metrics["dmhl_dead_zone"] = int(hl_flags["dead_zone"])
        metrics["dmhl_burst_flat"] = int(hl_flags["burst_flat"])
        metrics["dmhl_spent"] = int(hl_flags["spent"])
        metrics["dmhl_atr_basis"] = hl_dead.get("atr_basis_used")
    far = far_otm_time_metrics(snap, option.strike, option.expiry, direction, close, option.quote_time)
    far_otm = far["otm_time_atr"] is not None and far["otm_time_atr"] >= settings.far_otm_time_max_atr
    metrics.update({"ft_" + key: value for key, value in far.items()})
    metrics["ft_far_otm"] = int(far_otm)
    meta = getattr(snap, "atr_meta", None)
    metrics["provenance"] = {
        "gate_version": GATE_VERSION,
        "atr_basis_version": ATR_BASIS_VERSION,
        "atr": {
            "blocking_basis": metrics["dm_atr_basis"],
            "basis_mode": settings.dm_atr_basis_mode,
            "prior_atr": tr,
            "prior_hl_atr": hl,
            "hl_over_tr": metrics["dm_hl_over_tr"],
            "fallback": dead.get("atr_fallback"),
            "source": "prior completed RTH sessions only; the session being traded is excluded",
            "snapshot_at": snap.at.isoformat(),
            "meta": meta,
        },
        "far_otm": {
            **far,
            "mode": settings.far_otm_time_mode,
            "threshold": settings.far_otm_time_max_atr,
            "flag": int(far_otm),
            "blocks": int(settings.far_otm_time_mode == 2 and far_otm),
            "rule": "0DTE only; otm_atr/sqrt(minutes_left/390) >= threshold; missing data never flags",
        },
        "dead_market": {
            "dead_zone": int(flags["dead_zone"]),
            "burst_flat": int(flags["burst_flat"]),
            "spent": int(flags["spent"]),
            "dead_zone_blocks": int(settings.dead_zone_mode == 2 and flags["dead_zone"]),
            "burst_flat_blocks": int(settings.burst_flat_mode == 2 and flags["burst_flat"]),
            "spent_blocks": int(settings.spent_mode == 2 and flags["spent"]),
            "missing_data": "missing ATR or fewer than 3 bars never blocks",
        },
        "quotes": {
            "contract": option.symbol,
            "side": option.side,
            "strike": option.strike,
            "expiry": option.expiry.isoformat(),
            "bid": option.bid,
            "ask": option.ask,
            "quote_time": option.quote_time.isoformat(),
            "spot": snap.spot,
            "spot_time": snap.spot_time.isoformat(),
            "snapshot_at": snap.at.isoformat(),
        },
    }
    return {"dead_zone": flags["dead_zone"], "burst_flat": flags["burst_flat"],
            "spent": flags["spent"], "far_otm": far_otm}
