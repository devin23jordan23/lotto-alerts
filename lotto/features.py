"""Timestamp-bounded, symmetric call/put features. Scores are not probabilities.

Phase one scores observable evidence; strike migration remains a diagnostic,
not a demand for increasingly distant strikes. Chains do not reveal buy intent.
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
    atr = snap.prior_atr if snap.prior_atr and isfinite(snap.prior_atr) and snap.prior_atr > 0 else None
    premium_cap = max(settings.max_ask, min(settings.max_scaled_ask, atr*settings.premium_atr_fraction)) if atr else settings.max_ask
    return (quote_ok(option, snap, settings) and 0 <= dte <= settings.max_dte
            and option.bid > 0 and settings.min_ask <= option.ask <= premium_cap
            and spread / option.ask <= settings.max_spread_fraction
            and spread <= settings.max_spread_dollars
            and option.delta is not None and isfinite(option.delta)
            and direction * option.delta > 0
            and settings.min_delta <= abs(option.delta) <= settings.max_delta
            and option.gamma is not None and isfinite(option.gamma) and option.gamma > 0
            and -0.01 <= moneyness <= 0.08 and option.volume >= 100
            and (atr is None or direction*(option.strike-snap.spot) <= settings.max_otm_atr*atr))


def stock_features(snap: Snapshot, direction: int) -> tuple[dict, dict]:
    metrics = path_features(snap, direction)
    setup = detect_setup(snap, direction, metrics)
    trend = (6 * metrics["above_vwap_share"]
             + 5 * (metrics["ema_slope_directional"] > 0)
             + 5 * metrics["efficiency"] + 5 * (setup is not None)
             + 4 * clamp(metrics["return_3m_directional"] / .003))
    if setup and setup.name == "COILED_CONTINUATION":
        # Compression is the setup: low directional efficiency is expected here.
        trend = (6 * metrics["above_vwap_share"] + 5 * (metrics["ema_slope_directional"] >= 0)
                 + 5 * metrics["near_extreme_share"] + 5 + 4 * clamp(metrics["volume_acceleration"]/1.5))
    volume = (7 * clamp(max(metrics["pace_rvol"], metrics["local_rvol_5m"]) / 1.5)
              + 5 * clamp(metrics["pace_persistence"])
              + 8 * clamp(metrics["volume_acceleration"] / 1.4))
    return metrics, {"price_structure": trend, "volume_persistence": volume}


def response_proxy(option, snap):
    """Local delta/gamma response under a small stock move, not a price forecast.

    Holds IV/time fixed and caps the scoring contribution; live quotes, expiry,
    spread and strike reachability must pass before this can rank a contract.
    """
    move = min(snap.spot*.005, (snap.prior_atr or snap.spot*.02)*.20)
    return (abs(option.delta)*move + .5*option.gamma*move*move)/option.ask


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


def observation_window(snap, history):
    # Windows use real timestamps, never poll counts. Missing windows do not become zeros.
    endpoints, window_minutes = [], 5
    for width in (5, 2):
        endpoints = []
        for minutes in (width, 2*width):
            target = snap.at - timedelta(minutes=minutes)
            matches = [s for s in history if s.at <= target and (target - s.at).total_seconds() <= 90]
            if not matches:
                break
            endpoints.append(max(matches,key=lambda s:s.at))
        if len(endpoints) == 2:
            window_minutes = width
            break
    if len(endpoints) != 2:
        return None
    five, ten = endpoints
    window = sorted([s for s in history if ten.at <= s.at < snap.at] + [snap],key=lambda s:s.at)
    if any((b.at - a.at).total_seconds() > 150 for a, b in zip(window, window[1:])):
        return None
    return five, ten, window, window_minutes


def early_broad_flow(snap: Snapshot, setup: Setup | None, metrics: dict,
                     score: float, option: Option, settings: Settings) -> bool:
    """Recognize a triggered opening move with broad, sustained absolute flow.

    Relative option acceleration alone can miss a strong flow that began early
    and remains steady. This path is restricted to the first 45 bars;
    the usual score, context, price-risk, and confirmation checks still apply.
    """
    local_at = snap.at.astimezone(ET)
    open_at = local_at.replace(hour=9, minute=30, second=0, microsecond=0)
    if (not open_at <= local_at < open_at + timedelta(minutes=45)
            or setup is None or setup.building or len(snap.bars) > 45 or score < 75
            or setup.name not in {"OPENING_DRIVE", "OPENING_BREAK", "LEVEL_RECLAIM", "CONTINUATION"}):
        return False
    return (metrics["impulse_atr"] >= .20
            and metrics["move_from_open_atr"] >= .15
            and metrics["above_vwap_share"] >= .8
            and metrics["return_5m_directional"] >= .003
            and metrics["local_rvol_5m"] >= .8
            and metrics["volume_acceleration"] >= 1.2
            and metrics["cluster_size"] >= 4
            and metrics["option_volume_5m"] >= max(1500, 2*settings.min_strike_volume_5m*metrics["cluster_size"])
            and metrics["contract_volume_rate_5m"] >= 200
            and (option.ask-option.bid)/option.ask <= .10)


def candidates(snap: Snapshot, history: list[Snapshot], settings: Settings) -> list[Candidate]:
    observed = observation_window(snap, history)
    if observed is None:
        return []
    five, ten, window, window_minutes = observed
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
        duration_now = (snap.at - five.at).total_seconds()
        duration_prev = (five.at - ten.at).total_seconds()
        cluster, clusters = [], []
        # Inactive listed strikes break adjacency, as do duplicate/nonstandard strike rows.
        for option in listed:
            active = (option.symbol in usable and
                      (option.volume-old[option.symbol].volume)*300/duration_now >= settings.min_strike_volume_5m)
            if active and (not cluster or option.strike > cluster[-1].strike):
                cluster.append(option)
            else:
                if cluster:
                    clusters.append(cluster)
                cluster = []
        if cluster:
            clusters.append(cluster)
        clusters = [c for c in clusters if len(c) >= settings.min_cluster]
        eligible = [(o,c) for c in clusters for o in c if contract_ok(o, snap, settings)]
        if not eligible:
            continue
        common = [o for o in listed if o.symbol in usable]
        now_vol = {o.symbol: o.volume - old[o.symbol].volume for o in common}
        prev_vol = {o.symbol: old[o.symbol].volume - oldest[o.symbol].volume for o in common}
        now_total, prev_total = sum(now_vol.values()), sum(prev_vol.values())
        if now_total <= 0 or prev_total <= 0:
            continue  # Cannot turn an absent baseline into infinite acceleration.
        acceleration = (now_total / duration_now) / (prev_total / duration_prev)
        centroid_now = sum(now_vol[o.symbol] * log(o.strike / snap.spot) for o in common) / now_total
        centroid_prev = sum(prev_vol[o.symbol] * log(o.strike / five.spot) for o in common) / prev_total
        migration = direction * (centroid_now - centroid_prev)
        metrics, parts = stock_features(snap, direction)
        setup = detect_setup(snap, direction, metrics)
        option, best = max(eligible, key=lambda item: (
            2*(1-(item[0].ask-item[0].bid)/item[0].ask/settings.max_spread_fraction)
            + clamp(now_vol[item[0].symbol]*300/duration_now/1000)
            + 2*clamp(response_proxy(item[0],snap))
            + (1-abs(abs(item[0].delta)-.30)), item[0].symbol))
        persistent = persistent_activity(snap, history, {o.symbol for o in best}, settings)
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
                      or relative >= .0015 and metrics["return_5m_directional"] >= .0005)
        context_confirms = context_valid and independent and (context > 0 or leadership)
        if snap.market_return_5m is not None and snap.market_time is not None:
            if 0 <= (snap.at-snap.market_time).total_seconds() <= settings.max_quote_age_seconds:
                metrics["relative_market_5m"] = metrics["return_5m_directional"] - direction*snap.market_return_5m
        atr = snap.prior_atr if metrics["atr_available"] else None
        gap = direction*(snap.spot-setup.trigger)/atr if setup and atr else None
        distance_limit = settings.max_building_distance_atr if gap is not None and gap < 0 else settings.max_entry_extension_atr
        entry_quality = clamp(1-abs(gap)/distance_limit) if gap is not None else .5
        response = response_proxy(option,snap)
        parts.update({
            "option_velocity": 15 * max(clamp(acceleration / 2), 1 if persistent else 0),
            "strike_breadth": 10 * clamp(len(best) / 4),
            "entry_location": 10 * entry_quality,
            "sector_context": (5+5*clamp(max(context,relative)/.002)) if context_confirms else 0,
            "liquidity": 5 * clamp(1-(option.ask-option.bid)/option.ask/settings.max_spread_fraction),
            "contract_response": 5*clamp(response),
        })
        metrics.update({"option_acceleration": acceleration, "cluster_size": len(best),
                        "migration": migration, "option_volume_5m": now_total,
                        "context_directional_return": context, "relative_sector_5m": relative,
                        "peer_directional_return": peer, "options_persistent_20m": int(persistent),
                        "options_window_minutes": window_minutes, "trigger_gap_atr": gap,
                        "response_proxy": response, "contract_volume_rate_5m": now_vol[option.symbol]*300/duration_now,
                        "selected_spread_fraction": (option.ask-option.bid)/option.ask})
        score = round(sum(parts.values()), 2)
        early_flow = early_broad_flow(snap, setup, metrics, score, option, settings)
        metrics["early_broad_flow"] = int(early_flow)
        coil = setup is not None and setup.name == "COILED_CONTINUATION"
        local_burst = (metrics["local_rvol_5m"] >= settings.min_local_rvol
                       and metrics["volume_acceleration"] >= settings.min_local_acceleration
                       and metrics["atr_available"] and metrics["session_range_atr"] >= settings.min_range_atr)
        metrics["local_volume_burst"] = int(bool(local_burst))
        # Quieter-day recoveries can improve sharply against their own recent
        # tape. Require price progress plus stronger options evidence for this
        # alternative; a low historical RVOL alone is never confirmation.
        improving = (setup is not None and metrics["local_rvol_5m"] >= .35
                     and metrics["volume_acceleration"] >= 1.3 and metrics["recent_move_atr"] >= .04
                     and metrics["efficiency"] >= .4 and len(best) >= 3 and acceleration >= 1.5)
        sustained_coil = coil and persistent and metrics["local_rvol_5m"] >= .8
        metrics["locally_improving_volume"] = int(improving)
        stock_risk = direction*(snap.spot-setup.invalidation) if setup else 0
        target_room = direction*(setup.target-snap.spot) if setup and setup.target is not None else None
        target_risk = target_room/stock_risk if target_room is not None and stock_risk>0 else None
        metrics["target_to_invalidation_ratio"] = target_risk
        close = snap.session_end or snap.at.astimezone(ET).replace(hour=16,minute=0,second=0,microsecond=0)
        late = close-snap.at <= timedelta(minutes=30)
        short = window_minutes == 2
        checks = {
            "score below threshold": score >= settings.min_score+(5 if short else 0)+(3 if late else 0),
            "stock volume pace/burst below threshold": metrics["pace_rvol"] >= settings.min_pace_rvol or local_burst or improving or sustained_coil or early_flow,
            "stock volume pace fading": metrics["pace_persistence"] >= .80,
            "no developing price setup": setup is not None,
            "opposing volume dominates": metrics["opposing_volume_share"] <= (.60 if coil else .50),
            "options activity not replenishing": acceleration >= max(settings.min_option_acceleration,1.5 if len(best)==2 else 0) or persistent or early_flow,
            "independent sector/peer confirmation missing": context_confirms,
            "too far from price trigger": gap is None or -settings.max_building_distance_atr <= gap <= settings.max_entry_extension_atr,
            "price invalidation already crossed": setup is not None and direction*(snap.spot-setup.invalidation) > 0,
            "insufficient room to retest prior extreme": setup is None or setup.name!="PULLBACK_RESUMPTION" or (target_risk is not None and target_risk>=1.0),
            "short history needs a triggered broad setup": not short or (atr and setup is not None and not setup.building and len(best)>=3),
            "late entry needs near-strike confirmed momentum": not late or (setup is not None and not setup.building
                and abs(option.delta)>=.20 and (option.ask-option.bid)/option.ask <= .10
                and (gap is None or 0 <= gap <= .10)),
        }
        blockers = tuple(k for k,v in checks.items() if not v)
        qualifying = not blockers
        context_reason = (f"{snap.context_label} supportive" if context > 0
                          else f"Outperforming {snap.context_label}" if context_confirms
                          else f"{snap.context_label or 'Benchmark'} unconfirmed")
        reasons = [f"Stock pace {metrics['pace_rvol']:.1f}× · {metrics['volume_window_minutes']}m local RVOL {metrics['local_rvol_5m']:.1f}×", f"Options activity {acceleration:.1f}×",
                   f"{len(best)} neighboring strikes", context_reason]
        if improving:
            reasons.append("Local volume improving with price")
        if early_flow:
            reasons.append("Broad early options flow with rising local stock volume")
        if short:
            reasons.append("Early signal · shorter options history")
        if persistent:
            reasons.append("Options activity sustained 20m")
        if peer_valid and peer > 0:
            reasons.append("Peers: " + ", ".join(snap.peer_leaders))
        results.append(Candidate(snap, option, score, parts, metrics, reasons, qualifying, setup=setup, blockers=blockers))
    return results
