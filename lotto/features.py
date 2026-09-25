"""Timestamp-bounded, symmetric call/put features from snapshots.

Schwab chains cannot establish trade aggressor or opening/closing intent.
The eight signed-flow points and five catalyst points stay unavailable in v1.
"""
from dataclasses import dataclass
from datetime import timedelta
from math import isfinite, log

from .config import Settings
from .models import ET, Option, Snapshot
from .patterns import Setup, detect_setup, path_features


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
    setup: Setup | None = None
    blockers: tuple[str, ...] = ()


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
    metrics = path_features(snap, direction)
    setup = detect_setup(snap, direction, metrics)
    trend = (5 * metrics["above_vwap_share"]
             + 4 * (metrics["ema_slope_directional"] > 0)
             + 4 * clamp(1 - max(0, metrics["extreme_distance"]) / .02)
             + 4 * metrics["efficiency"] + 3 * clamp(metrics["return_5m_directional"] / .01))
    if setup and setup.name == "COILED_CONTINUATION":
        # Compression is the setup: low directional efficiency is expected here.
        trend = (5 * metrics["above_vwap_share"] + 4 * (metrics["ema_slope_directional"] >= 0)
                 + 4 * metrics["near_extreme_share"] + 4 + 3)
    elif setup and setup.name == "REVERSAL":
        trend = (5 * metrics["above_vwap_share"] + 4 * (metrics["ema_slope_directional"] > 0)
                 + 4 * clamp(metrics["return_from_adverse_extreme"] / .01)
                 + 4 * metrics["efficiency"] + 3 * clamp(metrics["return_3m_directional"] / .005))
    volume = (10 * clamp(max(metrics["pace_rvol"], metrics["local_rvol_5m"]) / 3) + 5 * clamp(metrics["pace_persistence"])
              + 5 * clamp(metrics["volume_acceleration"] / 1.5))
    return metrics, {"price_structure": trend, "volume_persistence": volume}


def persistent_activity(snap, history, contract_ids, settings):
    """Twenty minutes of replenishment, using identical contracts and fresh samples."""
    frames = sorted([s for s in history if snap.at-timedelta(minutes=22) <= s.at < snap.at] + [snap], key=lambda s:s.at)
    endpoints = []
    for minutes in (20, 15, 10, 5, 0):
        target = snap.at-timedelta(minutes=minutes)
        eligible = [s for s in frames if s.at <= target and (target-s.at).total_seconds() <= 90]
        if not eligible:
            return False
        endpoints.append(eligible[-1])
    window = [s for s in frames if s.at >= endpoints[0].at]
    if any((b.at-a.at).total_seconds() > 150 for a,b in zip(window, window[1:])):
        return False
    totals = []
    for frame in window:
        options = {o.symbol:o for o in frame.options}
        if any(i not in options or not quote_ok(options[i], frame, settings) for i in contract_ids):
            return False
        totals.append({i:options[i].volume for i in contract_ids})
    if any(b[i] < a[i] for a,b in zip(totals,totals[1:]) for i in contract_ids):
        return False
    values = [sum(o.volume for o in f.options if o.symbol in contract_ids) for f in endpoints]
    rates = [(b-a)/((y.at-x.at).total_seconds()/300) for a,b,x,y in zip(values,values[1:],endpoints,endpoints[1:])]
    return all(v >= settings.min_strike_volume_5m*len(contract_ids) for v in rates) and rates[-1] >= .8*rates[0]


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
        setup = detect_setup(snap, direction, metrics)
        persistent = persistent_activity(snap, history, {o.symbol for o in best}, settings)
        option = max(eligible, key=lambda o: (
            2 * (1 - (o.ask - o.bid) / o.ask) + clamp(now_vol[o.symbol] / 1000)
            + (1 - abs(abs(o.delta) - .30)), o.symbol))
        context_valid = (snap.context_return_5m is not None and isfinite(snap.context_return_5m)
                         and snap.context_time is not None
                         and 0 <= (snap.at - snap.context_time).total_seconds() <= settings.max_quote_age_seconds)
        context = direction * snap.context_return_5m if context_valid else 0
        relative = metrics["return_5m_directional"] - context
        peer_valid = (snap.peer_return_5m is not None and isfinite(snap.peer_return_5m)
                      and snap.peer_time is not None
                      and 0 <= (snap.at-snap.peer_time).total_seconds() <= settings.max_quote_age_seconds)
        peer = direction*snap.peer_return_5m if peer_valid else 0
        independent = snap.context_label != snap.symbol
        # Relative leadership can qualify against a flat/slightly opposing tape.
        etf_leadership = snap.symbol in {"SPY", "QQQ", "IWM", "DIA", "SMH", "SOXX", "XLK", "XLF", "XLE", "GLD", "USO", "SLV", "MTUM"} and context >= -.001
        # A name can lead its group even while the broad tape is flat or weak.
        # Independent, fresh context remains required; it is no longer a blanket
        # veto of a strong stock + options setup.
        leadership = (relative >= .001 and (peer > 0 or etf_leadership)
                      or relative >= .002 and metrics["return_5m_directional"] >= .001)
        context_confirms = context_valid and independent and (context > 0 or leadership)
        if snap.market_return_5m is not None and snap.market_time is not None:
            if 0 <= (snap.at-snap.market_time).total_seconds() <= settings.max_quote_age_seconds:
                metrics["relative_market_5m"] = metrics["return_5m_directional"] - direction*snap.market_return_5m
        parts.update({
            "option_velocity": 12 * max(clamp(acceleration / 3), .8 if persistent else 0),
            "signed_flow_unavailable": 0,
            "strike_breadth": 12 * clamp(len(best) / 5),
            "spot_adjusted_migration": 13 * clamp(migration / .01),
            "sector_context": 5 * clamp(max(context, relative if leadership else 0) / .003),
            "catalyst_unavailable": 0,
            "liquidity": 5 * clamp(1 - (option.ask - option.bid) / option.ask),
        })
        metrics.update({"option_acceleration": acceleration, "cluster_size": len(best),
                        "migration": migration, "option_volume_5m": now_total,
                        "context_directional_return": context, "relative_sector_5m": relative,
                        "peer_directional_return": peer, "options_persistent_20m": int(persistent)})
        score = round(sum(parts.values()), 2)
        coil = setup is not None and setup.name == "COILED_CONTINUATION"
        local_burst = (metrics["local_rvol_5m"] >= settings.min_local_rvol
                       and metrics["volume_acceleration"] >= settings.min_local_acceleration
                       and metrics["atr_available"] and metrics["session_range_atr"] >= settings.min_range_atr)
        metrics["local_volume_burst"] = int(bool(local_burst))
        checks = {
            "score below threshold": score >= settings.min_score,
            "stock volume pace/burst below threshold": metrics["pace_rvol"] >= settings.min_pace_rvol or local_burst,
            "stock volume pace fading": metrics["pace_persistence"] >= .80,
            "no developing price setup": setup is not None,
            "opposing volume dominates": metrics["opposing_volume_share"] <= (.65 if coil else .40),
            "options activity not replenishing": persistent if coil else acceleration >= settings.min_option_acceleration or persistent,
            "independent sector/peer confirmation missing": context_confirms,
        }
        blockers = tuple(k for k,v in checks.items() if not v)
        qualifying = not blockers
        context_reason = (f"{snap.context_label} supportive" if context > 0
                          else f"Outperforming {snap.context_label}" if context_confirms
                          else f"{snap.context_label or 'Benchmark'} unconfirmed")
        reasons = [f"Stock pace {metrics['pace_rvol']:.1f}× · 5m local RVOL {metrics['local_rvol_5m']:.1f}×", f"Options velocity {acceleration:.1f}×",
                   f"{len(best)} neighboring strikes", context_reason]
        if persistent:
            reasons.append("Options activity sustained 20m")
        if peer_valid and peer > 0:
            reasons.append("Peers: " + ", ".join(snap.peer_leaders))
        results.append(Candidate(snap, option, score, parts, metrics, reasons, qualifying, setup=setup, blockers=blockers))
    return results
