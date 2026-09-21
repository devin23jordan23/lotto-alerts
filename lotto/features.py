"""Timestamp-bounded, symmetric call/put features from snapshots.

Schwab chains cannot establish trade aggressor or opening/closing intent.
The eight signed-flow points and five catalyst points stay unavailable in v1.
"""
from dataclasses import dataclass
from datetime import timedelta
from math import isfinite, log

from .config import Settings
from .models import ET, Option, Snapshot


def clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


@dataclass
class Candidate:
    snapshot: Snapshot
    option: Option
    score: float
    parts: dict[str, float]
    metrics: dict[str, float]
    reasons: list[str]
    qualifying: bool
    score_change: float = 0.0
    state: str = "DISCOVERY"


def quote_ok(option: Option, snap: Snapshot, settings: Settings) -> bool:
    return (option.valid() and 0 <= (snap.at - option.quote_time).total_seconds()
            <= settings.max_quote_age_seconds
            and abs((option.quote_time - snap.spot_time).total_seconds()) <= settings.max_quote_age_seconds)


def contract_ok(option: Option, snap: Snapshot, settings: Settings) -> bool:
    dte = (option.expiry - snap.at.astimezone(ET).date()).days
    spread = option.ask - option.bid
    direction = 1 if option.side == "CALL" else -1
    moneyness = direction * (option.strike / snap.spot - 1)
    return (quote_ok(option, snap, settings) and 0 <= dte <= settings.max_dte
            and option.bid > 0 and settings.min_ask <= option.ask <= settings.max_ask
            and spread / option.ask <= settings.max_spread_fraction
            and spread <= settings.max_spread_dollars
            and option.delta is not None and isfinite(option.delta)
            and direction * option.delta > 0
            and settings.min_delta <= abs(option.delta) <= settings.max_delta
            and option.gamma is not None and isfinite(option.gamma) and option.gamma > 0
            and -0.01 <= moneyness <= 0.08 and option.volume >= 100)


def stock_features(snap: Snapshot, direction: int) -> tuple[dict, dict]:
    bars = snap.bars
    total_volume = sum(b.volume for b in bars)
    vwap = sum((b.high + b.low + b.close) / 3 * b.volume for b in bars) / max(1, total_volume)
    older = bars[:-5]
    old_volume = sum(b.volume for b in older)
    old_vwap = sum((b.high + b.low + b.close) / 3 * b.volume for b in older) / max(1, old_volume)
    pace = total_volume / bars[-1].expected_cumulative_volume
    old_baseline = older[-1].expected_cumulative_volume
    old_pace = old_volume / old_baseline if old_baseline and old_baseline > 0 else pace
    recent_volume = sum(b.volume for b in bars[-5:])
    previous_volume = sum(b.volume for b in bars[-10:-5])
    volume_acceleration = recent_volume / max(1, previous_volume)
    ret5 = direction * (bars[-1].close / bars[-6].close - 1)
    ret_previous = direction * (bars[-6].close / bars[-11].close - 1)
    excursion = max(b.high for b in bars) if direction == 1 else min(b.low for b in bars)
    distance = direction * (excursion - snap.spot) / excursion
    path = sum(abs(bars[i].close - bars[i - 1].close) for i in range(len(bars) - 10, len(bars)))
    efficiency = abs(bars[-1].close - bars[-11].close) / path if path else 0
    above_share = sum(direction * (b.close - vwap) > 0 for b in bars[-5:]) / 5
    opposing_volume = sum(b.volume for b in bars[-5:] if direction * (b.close - b.open) < 0)
    opposing_share = opposing_volume / max(1, recent_volume)
    trend = (5 * above_share + 4 * (direction * (vwap - old_vwap) > 0)
             + 4 * clamp(1 - max(0, distance) / .02)
             + 4 * efficiency + 3 * clamp(ret5 / .01))
    volume = (10 * clamp(pace / 3) + 5 * clamp(pace / max(.01, old_pace))
              + 5 * clamp(volume_acceleration / 1.5))
    metrics = {
        "pace_rvol": pace, "pace_persistence": pace / max(.01, old_pace),
        "volume_acceleration": volume_acceleration, "return_5m_directional": ret5,
        "price_acceleration": ret5 - ret_previous, "extreme_distance": distance,
        "vwap": vwap, "vwap_slope_directional": direction * (vwap - old_vwap),
        "above_vwap_share": above_share, "efficiency": efficiency,
        "opposing_volume_share": opposing_share,
        "return_from_close": snap.spot / snap.prior_close - 1,
        "opening_gap": bars[0].open / snap.prior_close - 1,
        "return_from_open": snap.spot / bars[0].open - 1,
    }
    return metrics, {"price_structure": trend, "volume_persistence": volume}


def candidates(snap: Snapshot, history: list[Snapshot], settings: Settings) -> list[Candidate]:
    # Windows use real timestamps, never poll counts. Missing windows do not become zeros.
    endpoints = []
    for minutes in (5, 10):
        target = snap.at - timedelta(minutes=minutes)
        matches = [s for s in history if s.at <= target and (target - s.at).total_seconds() <= 90]
        if not matches:
            return []
        endpoints.append(matches[-1])
    five, ten = endpoints
    window = [s for s in history if s.at >= ten.at] + [snap]
    if any((b.at - a.at).total_seconds() > 150 for a, b in zip(window, window[1:])):
        return []
    old = {o.symbol: o for o in five.options}
    oldest = {o.symbol: o for o in ten.options}
    current_by_id = {o.symbol: o for o in snap.options}
    # Reject any observed counter reset, stale endpoint, or missing contract in the window.
    usable = {}
    for ident, option in current_by_id.items():
        observations = [next((o for o in s.options if o.symbol == ident), None) for s in window]
        if (ident not in old or ident not in oldest or any(o is None for o in observations)
                or any(not quote_ok(o, s, settings) for o, s in zip(observations, window))
                or any(b.volume < a.volume for a, b in zip(observations, observations[1:]))):
            continue
        usable[ident] = option
    groups = {(o.expiry, o.side) for o in snap.options
              if 0 <= (o.expiry - snap.at.astimezone(ET).date()).days <= settings.max_dte}
    results = []
    for expiry, side in sorted(groups):
        direction = 1 if side == "CALL" else -1
        listed = sorted([o for o in snap.options if (o.expiry, o.side) == (expiry, side)], key=lambda o: o.strike)
        cluster, best = [], []
        # Inactive listed strikes break adjacency, as do duplicate/nonstandard strike rows.
        for option in listed:
            active = option.symbol in usable and option.volume - old[option.symbol].volume >= settings.min_strike_volume_5m
            if active and (not cluster or option.strike > cluster[-1].strike):
                cluster.append(option)
            else:
                cluster = []
            if len(cluster) > len(best):
                best = cluster[:]
        eligible = [o for o in best if contract_ok(o, snap, settings)]
        if not eligible or len(best) < settings.min_cluster:
            continue
        common = [o for o in listed if o.symbol in usable]
        now_vol = {o.symbol: o.volume - old[o.symbol].volume for o in common}
        prev_vol = {o.symbol: old[o.symbol].volume - oldest[o.symbol].volume for o in common}
        now_total, prev_total = sum(now_vol.values()), sum(prev_vol.values())
        if now_total <= 0 or prev_total <= 0:
            continue  # Cannot turn an absent baseline into infinite acceleration.
        duration_now = (snap.at - five.at).total_seconds()
        duration_prev = (five.at - ten.at).total_seconds()
        acceleration = (now_total / duration_now) / (prev_total / duration_prev)
        centroid_now = sum(now_vol[o.symbol] * log(o.strike / snap.spot) for o in common) / now_total
        centroid_prev = sum(prev_vol[o.symbol] * log(o.strike / five.spot) for o in common) / prev_total
        migration = direction * (centroid_now - centroid_prev)
        metrics, parts = stock_features(snap, direction)
        option = max(eligible, key=lambda o: (
            2 * (1 - (o.ask - o.bid) / o.ask) + clamp(now_vol[o.symbol] / 1000)
            + (1 - abs(abs(o.delta) - .30)), o.symbol))
        context_valid = (snap.context_return_5m is not None and isfinite(snap.context_return_5m)
                         and snap.context_time is not None
                         and 0 <= (snap.at - snap.context_time).total_seconds() <= settings.max_quote_age_seconds)
        context = direction * snap.context_return_5m if context_valid else 0
        parts.update({
            "option_velocity": 12 * clamp(acceleration / 3),
            "signed_flow_unavailable": 0,
            "strike_breadth": 12 * clamp(len(best) / 5),
            "spot_adjusted_migration": 13 * clamp(migration / .01),
            "sector_context": 5 * clamp(context / .003),
            "catalyst_unavailable": 0,
            "liquidity": 5 * clamp(1 - (option.ask - option.bid) / option.ask),
        })
        metrics.update({"option_acceleration": acceleration, "cluster_size": len(best),
                        "migration": migration, "option_volume_5m": now_total,
                        "context_directional_return": context})
        score = round(sum(parts.values()), 2)
        # No prior-close % veto. Strong structure and replenishing demand are required.
        qualifying = (score >= settings.min_score and metrics["pace_rvol"] >= settings.min_pace_rvol
                      and metrics["pace_persistence"] >= .80
                      and metrics["return_5m_directional"] > 0
                      and metrics["vwap_slope_directional"] > 0 and metrics["above_vwap_share"] >= .8
                      and metrics["extreme_distance"] <= .01 and metrics["efficiency"] >= .55
                      and metrics["opposing_volume_share"] <= .40
                      and acceleration >= settings.min_option_acceleration
                      and context_valid and context > 0)
        reasons = [f"Stock pace {metrics['pace_rvol']:.1f}×", f"Options velocity {acceleration:.1f}×",
                   f"{len(best)} neighboring strikes", f"{snap.context_label or 'Benchmark'} confirms"]
        results.append(Candidate(snap, option, score, parts, metrics, reasons, qualifying))
    return results
