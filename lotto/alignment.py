"""Optional alignment gates and score penalties. Defaults leave the live score alone.

Hard gates (LOTTO_GATE_ALIGNMENT, LOTTO_GATE_S12_BAR) default off.
LOTTO_ALIGNMENT_PACK=1 turns on the proposed penalty weights. A set per-term
env var still wins. The gates are not part of the pack.
"""
from datetime import timedelta
from math import isfinite

from .models import ET

PACK = {
    "pen_spy_opposing": -25.0,
    "pen_spy_level_break_against": -10.0,
    "pen_qqq_opposing": -10.0,
    "pen_sector_opposing": -20.0,
    "pen_both_opposing": -15.0,
    "pen_s12_below_bar_per_point": -1.0,
    "pen_s12_below_bar_cap": -30.0,
    "broad_rows_side_aware": 1,
    "cooldown_reset_on_level_break": 1,
    "pen_range_pos_mid": -30.0,
    "pen_range_pos_extreme": -50.0,
    "pen_vwap_side_wrong": -20.0,
    "pen_otm_far": -15.0,
    "pen_stock_vol_weak": -25.0,
    "pen_stock_vol_very_weak": -40.0,
    "sector_peer_side_aware": 1,
    "signed_volume": 1,
    "pen_stock_flat": -40.0,
    "pen_stock_slow": -20.0,
}

# These four score without a CALL/PUT direction test. The flag cannot invent
# one, so it leaves them. The other four only pay when the tape agrees.
NOT_MIRRORED_BROAD_ROWS = ("sp5", "flow85", "flow200", "OPT_VOL_ADJ")
MIRRORED_BROAD_ROWS = ("vwap_pb", "SPY_ALIGN_TURN_3M", "spy_with", "SPY_ALIGN_OPEN")

OTM_FAR_FRACTION = 0.015
SECTOR_AGAINST_SHARE = 0.60


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    number = round(float(value), 2)
    if number == int(number):
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _minute(bar):
    end = getattr(bar, "end", None)
    if end is None:
        return None
    local = end.astimezone(ET)
    minute = (local.hour * 60 + local.minute) - (9 * 60 + 31)
    if 0 <= minute <= 389:
        return minute
    return None


def session_vwap(bars):
    volume = 0.0
    total = 0.0
    for bar in bars or []:
        try:
            vol = float(bar.volume)
            typical = (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0
        except (TypeError, ValueError, AttributeError):
            continue
        if not isfinite(vol) or vol <= 0 or not isfinite(typical):
            continue
        volume += vol
        total += typical * vol
    if volume <= 0:
        return None
    return total / volume


def _card_direction(side: str) -> int:
    return 1 if str(side).upper() == "CALL" else -1 if str(side).upper() == "PUT" else 0


def index_trend_against(bars, side: str):
    """SPY/QQQ 5-minute trend against the card.

    A put is against when price is above VWAP and 5-minute lows are rising.
    A call is the mirror: below VWAP with lower highs. Missing data is None.
    """
    rows = [bar for bar in bars or [] if _minute(bar) is not None]
    if len(rows) < 6:
        return None
    vwap = session_vwap(rows)
    if vwap is None:
        return None
    buckets = {}
    for bar in rows:
        buckets.setdefault(_minute(bar) // 5, []).append(bar)
    keys = sorted(buckets)
    if len(keys) < 2:
        return None
    low_now = min(float(bar.low) for bar in buckets[keys[-1]])
    low_prev = min(float(bar.low) for bar in buckets[keys[-2]])
    high_now = max(float(bar.high) for bar in buckets[keys[-1]])
    high_prev = max(float(bar.high) for bar in buckets[keys[-2]])
    close = float(rows[-1].close)
    if side == "PUT":
        return close > vwap and low_now > low_prev
    if side == "CALL":
        return close < vwap and high_now < high_prev
    return None


def _upward(side: str, against: bool) -> bool:
    """Upward breaks oppose a put and go with a call."""
    return (side == "PUT" and against) or (side == "CALL" and not against)


def directional_breaks(bars, side: str, levels=None, *, against: bool = False):
    """First prints of HOD/PMH/PDH or the LOD/PML/PDL mirror.

    `against` selects the breaks that fight the card. Otherwise the breaks
    are the ones in the card's direction.
    """
    rows = list(bars or [])
    if not rows or side not in {"CALL", "PUT"}:
        return []
    levels = levels or {}
    upward = _upward(side, against)
    found = []
    running_high = None
    running_low = None
    saw_pmh = saw_pdh = saw_pml = saw_pdl = False
    pmh = _num(levels.get("premarket_high"))
    pdh = _num(levels.get("prior_high"))
    pml = _num(levels.get("premarket_low"))
    pdl = _num(levels.get("prior_low"))
    for bar in rows:
        try:
            high = float(bar.high)
            low = float(bar.low)
        except (TypeError, ValueError, AttributeError):
            continue
        if upward:
            if running_high is not None and high > running_high:
                found.append((bar, "HOD"))
            if pmh is not None and high > pmh and not saw_pmh:
                found.append((bar, "PMH"))
                saw_pmh = True
            if pdh is not None and high > pdh and not saw_pdh:
                found.append((bar, "PDH"))
                saw_pdh = True
        else:
            if running_low is not None and low < running_low:
                found.append((bar, "LOD"))
            if pml is not None and low < pml and not saw_pml:
                found.append((bar, "PML"))
                saw_pml = True
            if pdl is not None and low < pdl and not saw_pdl:
                found.append((bar, "PDL"))
                saw_pdl = True
        running_high = high if running_high is None else max(running_high, high)
        running_low = low if running_low is None else min(running_low, low)
    return found


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _recent(bars, count: int):
    rows = list(bars or [])
    if len(rows) < count:
        return rows
    return rows[-count:]


def breaks_in_window(bars, side, levels, *, against: bool, last_n: int | None = None, since=None, within_minutes=None):
    hits = directional_breaks(bars, side, levels, against=against)
    if not hits:
        return []
    allowed = None
    if last_n is not None:
        allowed = {id(bar) for bar in _recent(bars, last_n)}
    now = getattr(list(bars)[-1], "end", None) if bars else None
    kept = []
    for bar, kind in hits:
        if allowed is not None and id(bar) not in allowed:
            continue
        end = getattr(bar, "end", None)
        if since is not None and (end is None or end <= since):
            continue
        if within_minutes is not None and now is not None and end is not None:
            if now - end > timedelta(minutes=within_minutes):
                continue
        kept.append((bar, kind))
    return kept


def level_break_resets_cooldown(candidate, posted_at) -> bool:
    """A fresh same-side level break after the last post clears the hold."""
    if posted_at is None or getattr(candidate, "option", None) is None:
        return False
    snap = candidate.snapshot
    return bool(breaks_in_window(
        snap.bars, candidate.option.side, _snap_levels(snap),
        against=False, since=posted_at,
    ))


def _snap_levels(snap) -> dict:
    return {
        "premarket_high": getattr(snap, "premarket_high", None),
        "premarket_low": getattr(snap, "premarket_low", None),
        "prior_high": getattr(snap, "prior_high", None),
        "prior_low": getattr(snap, "prior_low", None),
    }


def _peer_pct(peer) -> float | None:
    if isinstance(peer, dict) and "pct" in peer and peer.get("pct") is not None:
        return _num(peer.get("pct"))
    if isinstance(peer, dict):
        opened = _num(peer.get("open"))
        last = _num(peer.get("last"))
    else:
        opened = _num(getattr(peer, "open", None))
        last = _num(getattr(peer, "last", None))
    if opened is None or last is None or opened <= 0:
        return None
    return (last / opened - 1.0) * 100.0


def sector_against(peers, etf_pct, side: str):
    """True when >=60% of peers or the sector ETF lean against the card.

    None when both inputs are missing. A flat ETF is not against.
    """
    direction = _card_direction(side)
    if direction == 0:
        return None
    known = []
    for peer in peers or []:
        pct = _peer_pct(peer)
        if pct is not None:
            known.append(pct)
    etf = _num(etf_pct)
    if not known and etf is None:
        return None
    peer_hit = bool(known) and sum(1 for pct in known if direction * pct < 0) / len(known) >= SECTOR_AGAINST_SHARE
    etf_hit = etf is not None and direction * etf < 0
    return peer_hit or etf_hit


def _sector_net(candidate):
    flags = (getattr(candidate, "metrics", None) or {}).get("confluence_flags") or {}
    if "_sector_l1_net_pct" not in flags:
        return None
    return _num(flags.get("_sector_l1_net_pct"))


def alignment_blocks(candidate) -> bool:
    """Both the card's SPY label and the SECTOR-log net lean oppose the side."""
    label = str((getattr(candidate, "metrics", None) or {}).get("spy_label") or "")
    if not label.endswith("opposing"):
        return False
    net = _sector_net(candidate)
    if net is None or net == 0:
        return False
    side = candidate.option.side if getattr(candidate, "option", None) is not None else ""
    card = _card_direction(side)
    if card == 0:
        return False
    sector_dir = 1 if net > 0 else -1
    return sector_dir * card < 0


def _dte(candidate) -> int:
    option = getattr(candidate, "option", None)
    snap = getattr(candidate, "snapshot", None)
    if option is None or snap is None:
        return 1
    try:
        days = (option.expiry - snap.at.astimezone(ET).date()).days
    except (TypeError, ValueError, AttributeError):
        return 1
    return 0 if days == 0 else 1


def s12_bar(settings, dte: int) -> float:
    if dte == 0:
        return float(settings.post_v2_option_0dte)
    return float(settings.post_v2_option_1dte)


def s12_gate_reason(candidate, settings) -> str | None:
    metrics = getattr(candidate, "metrics", None) or {}
    score = metrics.get("option_v2")
    if score is None:
        score = metrics.get("score_v2")
    bar = s12_bar(settings, _dte(candidate))
    if score is None or float(score) < bar:
        return f"gate: s12 below bar {_fmt(score)}<{_fmt(bar)}"
    return None


def post_gate_reasons(candidate, settings) -> list[str]:
    reasons = []
    if getattr(settings, "gate_alignment", 0):
        if alignment_blocks(candidate):
            reasons.append("gate: alignment (SPY and sector opposing)")
    if getattr(settings, "gate_s12_bar", 0):
        reason = s12_gate_reason(candidate, settings)
        if reason:
            reasons.append(reason)
    return reasons


def _take(rows: dict, name: str, weight, active: bool) -> None:
    if not active:
        return
    amount = _num(weight)
    if amount is None or amount == 0:
        return
    rows[name] = amount


def _range_pos(bars):
    rows = list(bars or [])
    if not rows:
        return None
    try:
        high = max(float(bar.high) for bar in rows)
        low = min(float(bar.low) for bar in rows)
        price = float(rows[-1].close)
    except (TypeError, ValueError, AttributeError):
        return None
    if not all(isfinite(number) for number in (high, low, price)) or high == low:
        return None
    return (price - low) / (high - low)


def _otm_far(side, spot, strike) -> bool:
    spot = _num(spot)
    strike = _num(strike)
    if spot is None or strike is None or spot <= 0:
        return False
    if side == "PUT":
        return (spot - strike) / spot > OTM_FAR_FRACTION
    if side == "CALL":
        return (strike - spot) / spot > OTM_FAR_FRACTION
    return False


def _pace(bars, side, atr):
    atr = _num(atr)
    rows = list(bars or [])
    if atr is None or atr <= 0 or not rows:
        return None, None
    direction = _card_direction(side)
    if direction == 0:
        return None, None
    try:
        opened = float(rows[0].open)
        price = float(rows[-1].close)
    except (TypeError, ValueError, AttributeError):
        return None, None
    move = direction * (price - opened) / atr
    latest = getattr(rows[-1], "end", None)
    if latest is None:
        return move, None
    window = [bar for bar in rows if getattr(bar, "end", None) is not None
              and latest - timedelta(minutes=14) <= bar.end <= latest]
    # A missing one-minute bar is tolerated; a short opening window is unknown.
    if len(window) < 13 or window[0].end > latest - timedelta(minutes=14):
        return move, None
    span = (max(float(bar.high) for bar in window) - min(float(bar.low) for bar in window)) / atr
    return move, span


def _volume_normal(bar, bars, tod):
    minute = _minute(bar)
    curve = {}
    if isinstance(tod, dict):
        curve = tod.get(10) or tod.get("10") or {}
    normal = _num(curve.get(minute)) if minute is not None and isinstance(curve, dict) else None
    if normal is not None and normal > 0:
        return normal
    prior = []
    end = getattr(bar, "end", None)
    for other in bars or []:
        if other is bar:
            continue
        other_end = getattr(other, "end", None)
        if end is not None and other_end is not None and other_end >= end:
            continue
        vol = _num(getattr(other, "volume", None))
        if vol is not None:
            prior.append(vol)
    if not prior:
        return None
    return sum(prior) / len(prior)


def _volume_exempt(bars, side, levels, tod) -> bool:
    from .vt_measure import counted_volume

    hits = breaks_in_window(bars, side, levels, against=False, last_n=3)
    for bar, _kind in hits:
        normal = _volume_normal(bar, bars, tod)
        # A wick break on a bar that closes against the card is not signed flow.
        vol = counted_volume(bar, _card_direction(side), True)
        if normal is not None and vol is not None and vol >= 1.5 * normal:
            return True
    return False


def _raw_ratio(raw, *names):
    if not isinstance(raw, dict):
        return None
    for name in names:
        if name in raw:
            return _num(raw.get(name))
    return None


def zero_broad_rows(points, score, positive, features, side: str):
    """Drop a broad row whose direction fights the card.

    sp5, flow85, flow200, and OPT_VOL_ADJ have no tape direction, so they stay.
    vwap_pb can leak a partial on the wrong side of VWAP; that partial is zeroed.
    The other mirrored rows are already 0 when they disagree.
    """
    points = dict(points or {})
    card = _card_direction(side)
    if card == 0:
        return points, score, positive
    vwap_side = _num((features or {}).get("_vwap_side"))
    if vwap_side in (1.0, -1.0) and vwap_side * card < 0:
        value = points.get("vwap_pb")
        if isinstance(value, (int, float)) and value != 0:
            score -= value
            if value > 0:
                positive = max(0, int(positive) - 1)
            points["vwap_pb"] = 0.0
    return points, score, positive


def _peers_from(snap, features):
    features = features or {}
    etf = str(features.get("_sector_etf") or "")
    sets = getattr(snap, "vt_peer_sets", None) or {}
    peers = sets.get(etf) if etf else None
    if not peers:
        peers = getattr(snap, "vt_peers", None)
    etf_pct = features.get("_sector_etf_pct")
    if etf_pct is None:
        etf_pct = getattr(snap, "vt_etf_pct", None)
    return peers, etf_pct


def _index_levels(index_levels, snap, symbol: str):
    if not isinstance(index_levels, dict) or snap is None:
        return {}
    return index_levels.get((getattr(snap, "day", None), symbol)) or {}


def alignment_rows(settings, *, side, option_v2=None, dte=1, bars=None, spot=None, strike=None,
                   atr=None, spy_bars=None, qqq_bars=None, spy_levels=None, qqq_levels=None,
                   peers=None, etf_pct=None, levels=None, raw=None, sources=None, tod=None) -> dict:
    """Non-zero penalty rows. Stock-volume and pace rows are last."""
    rows = {}
    side = str(side or "").upper()
    spy_against = index_trend_against(spy_bars, side)
    qqq_against = index_trend_against(qqq_bars, side)
    sector = sector_against(peers, etf_pct, side)
    _take(rows, "PEN_SPY_OPPOSING", settings.pen_spy_opposing, spy_against is True)
    spy_level = bool(breaks_in_window(
        spy_bars, side, spy_levels, against=True, within_minutes=30))
    _take(rows, "PEN_SPY_LEVEL_BREAK_AGAINST", settings.pen_spy_level_break_against, spy_level)
    _take(rows, "PEN_QQQ_OPPOSING", settings.pen_qqq_opposing, qqq_against is True)
    _take(rows, "PEN_SECTOR_OPPOSING", settings.pen_sector_opposing, sector is True)
    _take(rows, "PEN_BOTH_OPPOSING", settings.pen_both_opposing, spy_against is True and sector is True)
    bar = s12_bar(settings, 0 if dte == 0 else 1)
    score = _num(option_v2)
    per = _num(getattr(settings, "pen_s12_below_bar_per_point", 0)) or 0.0
    cap = _num(getattr(settings, "pen_s12_below_bar_cap", 0)) or 0.0
    if per and score is not None and score < bar:
        raw_penalty = (bar - score) * per
        if cap < 0 and raw_penalty < 0:
            raw_penalty = max(raw_penalty, cap)
        elif cap > 0 and raw_penalty > 0:
            raw_penalty = min(raw_penalty, cap)
        _take(rows, "PEN_S12_BELOW_BAR", raw_penalty, True)

    pos = _range_pos(bars)
    same_side_3 = bool(breaks_in_window(bars, side, levels, against=False, last_n=3))
    if pos is not None and not same_side_3:
        if side == "PUT":
            if pos > 0.60:
                _take(rows, "PEN_RANGE_POS", settings.pen_range_pos_extreme, True)
            elif pos >= 0.40:
                _take(rows, "PEN_RANGE_POS", settings.pen_range_pos_mid, True)
        elif side == "CALL":
            if pos < 0.40:
                _take(rows, "PEN_RANGE_POS", settings.pen_range_pos_extreme, True)
            elif pos <= 0.60:
                _take(rows, "PEN_RANGE_POS", settings.pen_range_pos_mid, True)
    vwap = session_vwap(bars)
    close = _num(getattr(list(bars)[-1], "close", None)) if bars else None
    wrong_vwap = False
    if vwap is not None and close is not None:
        wrong_vwap = (side == "PUT" and close > vwap) or (side == "CALL" and close < vwap)
    _take(rows, "PEN_VWAP_SIDE_WRONG", settings.pen_vwap_side_wrong, wrong_vwap)
    _take(rows, "PEN_OTM_FAR", settings.pen_otm_far, _otm_far(side, spot, strike) and not same_side_3)

    # The scored BU_5_TOD row can point to a prior breakout. This penalty
    # specifically measures the five completed bars ending at the card.
    bu = _raw_ratio(raw, "BU_5_TOD_LAST")
    rvol = _raw_ratio(raw, "RVOL_CUM")
    rvol_source = (sources or {}).get("RVOL_CUM") if isinstance(sources, dict) else None
    cumulative_baseline = rvol_source in {"RVOL_CUM_10", "RVOL_CUM_5", "RVOL_CUM_3", "RVOL_ANY", "RVOL_CUM_TIMESCALE"}
    if (bu is not None and rvol is not None and cumulative_baseline
            and bu < 1.0 and rvol < 1.0 and not _volume_exempt(bars, side, levels, tod)):
        if bu < 0.8:
            _take(rows, "PEN_STOCK_VOL_VERY_WEAK", settings.pen_stock_vol_very_weak, True)
        else:
            _take(rows, "PEN_STOCK_VOL_WEAK", settings.pen_stock_vol_weak, True)

    move, span = _pace(bars, side, atr)
    if move is not None and move <= 0.3 and span is not None and span <= 0.15:
        _take(rows, "PEN_STOCK_FLAT", settings.pen_stock_flat, True)
    elif move is not None and move <= 0.5 and not same_side_3:
        _take(rows, "PEN_STOCK_SLOW", settings.pen_stock_slow, True)
    return rows


def apply_alignment_penalties(score, candidate, settings, features=None, spy_bars=None,
                              qqq_bars=None, index_levels=None):
    """Subtract the pack rows from an additive score. Zero weights add nothing."""
    features = features if features is not None else (candidate.metrics or {}).get("confluence_flags") or {}
    snap = candidate.snapshot
    side = candidate.option.side if candidate.option is not None else ""
    metrics = candidate.metrics or {}
    option_v2 = metrics.get("option_v2")
    if option_v2 is None:
        option_v2 = metrics.get("score_v2")
    peers, etf_pct = _peers_from(snap, features)
    rows = alignment_rows(
        settings,
        side=side,
        option_v2=option_v2,
        dte=_dte(candidate),
        bars=list(snap.bars or []),
        spot=snap.spot,
        strike=candidate.option.strike if candidate.option is not None else None,
        atr=getattr(snap, "prior_atr21", None) or snap.prior_atr,
        spy_bars=spy_bars,
        qqq_bars=qqq_bars,
        spy_levels=_index_levels(index_levels, snap, "SPY"),
        qqq_levels=_index_levels(index_levels, snap, "QQQ"),
        peers=peers,
        etf_pct=etf_pct,
        levels=_snap_levels(snap),
        raw=features.get("_vt_raw") if isinstance(features, dict) else None,
        sources=features.get("_vt_sources") if isinstance(features, dict) else None,
        tod=getattr(snap, "vt_tod", None),
    )
    if candidate.metrics is not None:
        candidate.metrics["alignment_rows"] = dict(rows)
    adjusted = float(score) + sum(rows.values())
    return adjusted, rows
