"""FINAL-B rules. Mode 0 never calls this from a live decision.

Modes: 0 off, 1 shadow (compute and log, change nothing posted), 2 on.
Discovery ranking stays shadow-only even at mode 2; see discovery.py.
"""
from datetime import datetime, timedelta
from math import isfinite

from .models import ET

# Ten minutes replaces ticker_cooldown_minutes only when cooldown_mode >= 1.
FINALB_COOLDOWN_MINUTES = 10

SCORE_REASON = "score below threshold"
VOLUME_REASON = "stock volume pace/burst below threshold"
LEVEL_SIDE_REASON = "spot not through premarket or prior-day level"
CHASE_REASON = "chase: straight extension at the day's extreme"
AFTER14_REASON = "after 14:00 needs a fresh break, volume, and tape"
COOLDOWN_REASON = "cooldown or fresh reset required"
SOLE_BLOCKERS = {SCORE_REASON, VOLUME_REASON}
AFTER14_EXEMPT = frozenset({"SPY", "QQQ", "IWM", "SPX", "NDX", "XSP"})
CARD_MODES = (
    "level_side_rule_mode", "level_break_override_mode", "after14_mode",
    "chase_block_mode", "ext_penalty_mode",
)


def finalb_card_active(settings) -> bool:
    return any(getattr(settings, name) >= 1 for name in CARD_MODES)


def finalb_enabled(settings) -> bool:
    return finalb_card_active(settings) or settings.cooldown_mode >= 1


def _usable(level) -> bool:
    return level is not None and isfinite(level) and level > 0


def level_side_passes(snap, side: str) -> bool:
    """CALL: spot above PMH or PDH. PUT: spot below PML or PDL.

    A missing level is skipped. If every relevant level is missing, the rule passes.
    If any present level is broken, the rule passes (OR). Equal to a level does not pass.
    """
    if side == "CALL":
        present = [level for level in (snap.premarket_high, snap.prior_high) if _usable(level)]
        return not present or any(snap.spot > level for level in present)
    present = [level for level in (snap.premarket_low, snap.prior_low) if _usable(level)]
    return not present or any(snap.spot < level for level in present)


def _atr(snap):
    atr = snap.prior_atr
    if atr is None or not isfinite(atr) or atr <= 0:
        return None
    return atr


def session_bounds(snap):
    bars = snap.bars
    if not bars:
        return None, None
    return max(b.high for b in bars), min(b.low for b in bars)


def session_extreme(snap, side: str):
    """HOD for calls, LOD for puts, including a live spot beyond the last completed bar."""
    hod, lod = session_bounds(snap)
    if hod is None:
        return None
    if side == "CALL":
        return max(hod, snap.spot)
    return min(lod, snap.spot)


def directional_position(snap, side: str):
    """Fraction of the completed-bar range toward the card's extreme, and the travel off the other extreme."""
    hod, lod = session_bounds(snap)
    if hod is None or hod <= lod:
        return None, None
    span = hod - lod
    if side == "CALL":
        return (snap.spot - lod) / span, snap.spot - lod
    return (hod - snap.spot) / span, hod - snap.spot


def max_leg_pullback(snap, side: str) -> float:
    """Largest adverse move after the most recent session extreme, in price points.

    The extreme bar's own high-low is not a pullback. Later completed bars, and a
    spot that has retraced off the running extreme, are.
    """
    bars = snap.bars
    if not bars:
        return 0.0
    if side == "CALL":
        lod = min(b.low for b in bars)
        start = max(i for i, bar in enumerate(bars) if bar.low == lod)
        leg = bars[start:]
        peak = leg[0].high
        worst = 0.0
        for bar in leg[1:]:
            worst = max(worst, peak - bar.low)
            peak = max(peak, bar.high)
        if snap.spot < peak:
            worst = max(worst, peak - snap.spot)
        return worst
    hod = max(b.high for b in bars)
    start = max(i for i, bar in enumerate(bars) if bar.high == hod)
    leg = bars[start:]
    trough = leg[0].low
    worst = 0.0
    for bar in leg[1:]:
        worst = max(worst, bar.high - trough)
        trough = min(trough, bar.low)
    if snap.spot > trough:
        worst = max(worst, snap.spot - trough)
    return worst


def chase_blocked(snap, side: str) -> bool:
    """Travel >= 1 ATR off the opposite extreme, pullback < 0.25 ATR, spot in the outer 15%."""
    atr = _atr(snap)
    position, travel = directional_position(snap, side)
    if atr is None or position is None or travel is None:
        return False
    if travel < atr or position < 0.85:
        return False
    return max_leg_pullback(snap, side) < 0.25 * atr


def extension_penalty(snap, side: str, metrics) -> bool:
    """Day-range position >= 0.9, move off the opposite extreme >= 0.75 ATR, local rvol >= 2."""
    atr = _atr(snap)
    position, travel = directional_position(snap, side)
    if atr is None or position is None or travel is None:
        return False
    return position >= 0.9 and travel >= 0.75 * atr and metrics.get("local_rvol_5m", 0) >= 2.0


def fresh_extreme_break(snap, side: str) -> bool:
    """A new print in the last 5 completed bars through HOD/PMH/PDH (calls) or LOD/PML/PDL (puts).

    Session HOD/LOD uses the extreme of bars before that window, so the break is fresh.
    A premarket or prior-day level counts only when no earlier bar already traded through it.
    """
    bars = snap.bars
    if len(bars) < 2:
        return False
    recent = bars[-5:]
    earlier = bars[:-5]
    if side == "CALL":
        levels = []
        if earlier:
            levels.append(max(bar.high for bar in earlier))
        for level in (snap.premarket_high, snap.prior_high):
            if _usable(level):
                levels.append(level)
        for level in levels:
            if earlier and any(bar.high > level for bar in earlier):
                continue
            if any(bar.high > level for bar in recent):
                return True
        return False
    levels = []
    if earlier:
        levels.append(min(bar.low for bar in earlier))
    for level in (snap.premarket_low, snap.prior_low):
        if _usable(level):
            levels.append(level)
    for level in levels:
        if earlier and any(bar.low < level for bar in earlier):
            continue
        if any(bar.low < level for bar in recent):
            return True
    return False


def override_frees(blockers, snap, side, metrics, settings) -> bool:
    """Drop score and volume blockers only when they are the only blockers and a fresh break has rvol."""
    reasons = set(blockers)
    if not reasons or not reasons <= SOLE_BLOCKERS:
        return False
    if metrics.get("local_rvol_5m", 0) < settings.level_break_min_local_rvol:
        return False
    return fresh_extreme_break(snap, side)


def fresh_close_extreme(snap, side: str) -> bool:
    """A completed 1m close in the last 5 bars through the prior session HOD (calls) or LOD (puts)."""
    bars = snap.bars
    if len(bars) < 2:
        return False
    recent = bars[-5:]
    for index, bar in enumerate(recent):
        prior = bars[:len(bars) - len(recent) + index]
        if not prior:
            continue
        if side == "CALL" and bar.close > max(item.high for item in prior):
            return True
        if side == "PUT" and bar.close < min(item.low for item in prior):
            return True
    return False


def fresh_box_break(snap, side: str) -> bool:
    """Close in the last 5 bars through the 30- or 60-minute box.

    The box is completed bars k-35..k-6 (30) or k-65..k-6 (60), with k the last bar.
    Bar k-5 sits in neither window, matching the spec's indexes. A close already
    through the box before the last 5 bars is not a fresh break.
    """
    bars = snap.bars
    k = len(bars) - 1
    if k < 6:
        return False
    recent = bars[-5:]
    before = bars[:k - 4]  # through k-5
    for start_back in (35, 65):
        start = k - start_back
        end = k - 6
        if start < 0 or end < start:
            continue
        box = bars[start:end + 1]
        if side == "CALL":
            edge = max(bar.high for bar in box)
            if any(bar.close > edge for bar in before):
                continue
            if any(bar.close > edge for bar in recent):
                return True
        else:
            edge = min(bar.low for bar in box)
            if any(bar.close < edge for bar in before):
                continue
            if any(bar.close < edge for bar in recent):
                return True
    return False


def volume_surge(snap) -> bool:
    """Max volume of the last 3 completed bars >= 1.5x the mean of the prior 20."""
    bars = snap.bars
    if len(bars) < 23:
        return False
    last3 = [bar.volume for bar in bars[-3:]]
    prior = [bar.volume for bar in bars[-23:-3]]
    baseline = sum(prior) / 20
    if baseline <= 0:
        return False
    return max(last3) >= 1.5 * baseline


def _fresh_return(value, when, snap, settings):
    if value is None or when is None or not isfinite(value):
        return None
    if not 0 <= (snap.at - when).total_seconds() <= settings.max_quote_age_seconds:
        return None
    return value


def bar_return_5m(snap):
    bars = snap.bars
    if len(bars) < 6 or bars[-6].close <= 0:
        return None
    return bars[-1].close / bars[-6].close - 1


def _named_return(snap, symbol, settings):
    """SPY or QQQ 5-minute return from fields already on the snapshot, then the name's own bars."""
    if symbol == "SPY":
        value = _fresh_return(getattr(snap, "spy_return_5m", None), getattr(snap, "spy_return_time", None), snap, settings)
    else:
        value = _fresh_return(getattr(snap, "qqq_return_5m", None), getattr(snap, "qqq_return_time", None), snap, settings)
    if value is None and snap.market_label == symbol:
        value = _fresh_return(snap.market_return_5m, snap.market_time, snap, settings)
    if value is None and snap.context_label == symbol:
        value = _fresh_return(snap.context_return_5m, snap.context_time, snap, settings)
    if value is None and snap.symbol == symbol:
        value = bar_return_5m(snap)
    return value


def tape_supports(snap, side: str, settings) -> bool:
    """5-minute return >= 0.05% with the card, on SPY, QQQ, or the sector benchmark."""
    direction = 1 if side == "CALL" else -1
    sector = _fresh_return(snap.context_return_5m, snap.context_time, snap, settings)
    for value in (_named_return(snap, "SPY", settings), _named_return(snap, "QQQ", settings), sector):
        if value is not None and direction * value >= 0.0005:
            return True
    return False


def after14_passes(snap, side: str, settings) -> bool:
    """Exempt indexes and anything before 14:00 ET. Other names need break, volume, and tape."""
    local = snap.at.astimezone(ET)
    if (local.hour, local.minute) < (14, 0):
        return True
    if snap.symbol in AFTER14_EXEMPT:
        return True
    if not (fresh_close_extreme(snap, side) or fresh_box_break(snap, side)):
        return False
    if not volume_surge(snap):
        return False
    return tape_supports(snap, side, settings)


def _score_ok(snap, settings, score, metrics) -> bool:
    short = metrics.get("options_window_minutes", 5) <= 2
    close = snap.session_end or snap.at.astimezone(ET).replace(hour=16, minute=0, second=0, microsecond=0)
    late = close - snap.at <= timedelta(minutes=30)
    return score >= settings.min_score + (5 if short else 0) + (3 if late else 0)


def _volume_ok(snap, metrics, option, setup, settings, score, early, opening) -> bool:
    coil = setup is not None and setup.name == "COILED_CONTINUATION"
    sustained = coil and bool(metrics.get("options_persistent_20m")) and metrics.get("local_rvol_5m", 0) >= .8
    return (metrics.get("pace_rvol", 0) >= settings.min_pace_rvol
            or bool(metrics.get("local_volume_burst"))
            or bool(metrics.get("locally_improving_volume"))
            or sustained or early or opening)


def _without(blockers, reason):
    return [item for item in blockers if item != reason]


def _ensure(blockers, reason, ok, at_start=False):
    if ok:
        return _without(blockers, reason)
    if reason in blockers:
        return list(blockers)
    if at_start:
        return [reason, *blockers]
    return [*blockers, reason]


def finalb_overlay(snap, side, live_score, live_blockers, metrics, option, setup, settings, *, min_mode: int) -> dict:
    """FINAL-B score and blockers. Does not mutate the live candidate.

    A rule applies only when its mode is >= min_mode. Shadow passes 1. The live
    path passes 2 so mode-1 rules stay in the log and off the card.
    """
    from .features import early_broad_flow, opening_expansion

    blockers = list(live_blockers)
    score = live_score
    flags = {
        "level_side": 0,
        "level_break_override": 0,
        "chase_block": 0,
        "after14": 0,
        "ext_penalty": 0,
    }
    early_flag = None
    opening_flag = None
    if settings.ext_penalty_mode >= min_mode:
        fired = extension_penalty(snap, side, metrics)
        flags["ext_penalty"] = int(fired)
        if fired:
            score = round(live_score - settings.ext_penalty_points, 2)
            early = early_broad_flow(snap, setup, metrics, score, option, settings)
            opening = opening_expansion(
                snap, setup, metrics, score, metrics.get("option_acceleration", 0),
                int(metrics.get("cluster_size", 0)))
            early_flag = int(early)
            opening_flag = int(opening)
            blockers = _ensure(blockers, SCORE_REASON, _score_ok(snap, settings, score, metrics), at_start=True)
            volume_ok = _volume_ok(snap, metrics, option, setup, settings, score, early, opening)
            if volume_ok:
                blockers = _without(blockers, VOLUME_REASON)
            elif VOLUME_REASON not in blockers:
                if SCORE_REASON in blockers:
                    blockers.insert(blockers.index(SCORE_REASON) + 1, VOLUME_REASON)
                else:
                    blockers.insert(0, VOLUME_REASON)
    if settings.level_side_rule_mode >= min_mode:
        failed = not level_side_passes(snap, side)
        flags["level_side"] = int(failed)
        if failed and LEVEL_SIDE_REASON not in blockers:
            blockers.append(LEVEL_SIDE_REASON)
    if settings.chase_block_mode >= min_mode:
        failed = chase_blocked(snap, side)
        flags["chase_block"] = int(failed)
        if failed and CHASE_REASON not in blockers:
            blockers.append(CHASE_REASON)
    if settings.after14_mode >= min_mode:
        failed = not after14_passes(snap, side, settings)
        flags["after14"] = int(failed)
        if failed and AFTER14_REASON not in blockers:
            blockers.append(AFTER14_REASON)
    if settings.level_break_override_mode >= min_mode:
        frees = override_frees(blockers, snap, side, metrics, settings)
        flags["level_break_override"] = int(frees)
        if frees:
            blockers = [item for item in blockers if item not in SOLE_BLOCKERS]
    return {
        "score": score,
        "blockers": tuple(blockers),
        "flags": flags,
        "qualifying": not blockers,
        "early_broad_flow": early_flag,
        "opening_expansion": opening_flag,
    }


def apply_mode2_checks(checks: dict, hard: dict, settings) -> None:
    """Reflect a mode-2 overlay in the feature checks. Mode 0 never reaches here."""
    blocked = set(hard["blockers"])
    if settings.ext_penalty_mode == 2:
        checks[SCORE_REASON] = SCORE_REASON not in blocked
        checks[VOLUME_REASON] = VOLUME_REASON not in blocked
    if settings.level_side_rule_mode == 2:
        checks[LEVEL_SIDE_REASON] = LEVEL_SIDE_REASON not in blocked
    if settings.chase_block_mode == 2:
        checks[CHASE_REASON] = CHASE_REASON not in blocked
    if settings.after14_mode == 2:
        checks[AFTER14_REASON] = AFTER14_REASON not in blocked
    if settings.level_break_override_mode == 2 and hard["flags"].get("level_break_override"):
        checks[SCORE_REASON] = True
        checks[VOLUME_REASON] = True


def mode2_card(settings) -> bool:
    return any(getattr(settings, name) == 2 for name in CARD_MODES)


def extreme_beyond(snap, side: str, previous, multiple: float) -> bool:
    """True when the new HOD (calls) or LOD (puts) is at least `multiple` prior-ATR past previous.

    `previous` is the session extreme stored on the prior card. ATR is snap.prior_atr,
    the same 14-day mean the scanner already uses. A live spot beyond the last
    completed bar counts, via session_extreme.
    """
    atr = _atr(snap)
    current = session_extreme(snap, side)
    if atr is None or current is None or previous is None or not isfinite(previous):
        return False
    if side == "CALL":
        return current >= previous + multiple * atr
    if side == "PUT":
        return current <= previous - multiple * atr
    return False


def extreme_extended(snap, side: str, previous, settings) -> bool:
    return extreme_beyond(snap, side, previous, settings.extreme_rearm_atr)


# Logged together when LOTTO_REARM_ATR_MODE=1. Not a cooldown threshold.
REARM_ATR_GATES = (0.10, 0.15)


def rearm_atr_variant(at, last, side: str, snap, settings):
    """(clears 0.10, clears 0.15) for one in-cooldown new-HOD/LOD re-arm, else None.

    The current re-arm (extreme_rearm_atr, default 0.05, and the 5-minute floor)
    still decides whether FINAL-B would fire. This only answers whether that same
    re-arm also cleared 0.10 and 0.15 ATR past the prior card's extreme. Outside
    the 10-minute cooldown the re-arm is not what lets the card through, so the
    result is None. Mode 0 is None.
    """
    if settings.rearm_atr_mode != 1 or not last:
        return None
    if _last_value(last, "side") != side:
        return None
    last_at = _last_value(last, "at")
    if last_at is None:
        return None
    if not isinstance(last_at, datetime):
        last_at = datetime.fromisoformat(last_at)
    elapsed = at - last_at
    if elapsed < timedelta(minutes=settings.extreme_rearm_min_minutes):
        return None
    if elapsed >= timedelta(minutes=FINALB_COOLDOWN_MINUTES):
        return None
    previous = _last_value(last, "extreme", "session_extreme")
    if not extreme_extended(snap, side, previous, settings):
        return None
    return (
        int(extreme_beyond(snap, side, previous, REARM_ATR_GATES[0])),
        int(extreme_beyond(snap, side, previous, REARM_ATR_GATES[1])),
    )


def _last_value(last, *names):
    if isinstance(last, dict):
        for name in names:
            if name in last and last[name] is not None:
                return last[name]
        return None
    keys = set(last.keys())
    for name in names:
        if name in keys and last[name] is not None:
            return last[name]
    return None


def promotion_bypasses(last, phase: str, side: str, rearmed: bool) -> bool:
    if not last or rearmed:
        return False
    return phase == "ACTIVE" and last["phase"] == "POTENTIAL" and last["side"] == side


def finalb_cooldown_blocks(at, last, side: str, rearmed: bool, snap, settings):
    """10-minute cooldown plus extreme re-arm. Returns (blocked, extreme_rearm).

    Same-side re-fire is allowed once extreme_rearm_min_minutes have passed and the
    side's session extreme has extended by extreme_rearm_atr daily ATR.
    """
    if last is None:
        return False, False
    last_at = _last_value(last, "at")
    if not isinstance(last_at, datetime):
        last_at = datetime.fromisoformat(last_at)
    elapsed = at - last_at
    same = last["side"] == side
    minutes = elapsed.total_seconds() / 60
    previous = _last_value(last, "extreme", "session_extreme")
    extended = extreme_extended(snap, side, previous, settings) if same else False
    extreme_ok = bool(same and minutes >= settings.extreme_rearm_min_minutes and extended)
    if extreme_ok:
        return False, True
    time_blocked = elapsed < timedelta(minutes=FINALB_COOLDOWN_MINUTES)
    same_blocked = same and not rearmed
    return time_blocked or same_blocked, False


def live_cooldown_blocks(at, last_at, last_side, side, rearmed, settings) -> bool:
    return (at - last_at < timedelta(minutes=settings.ticker_cooldown_minutes)
            or (not rearmed and last_side == side))


def window_return(history, now, price, seconds):
    """Same 90-second slack the 5-minute discovery return uses."""
    old = [row for row in history if seconds <= (now - row["at"]).total_seconds() <= seconds + 90]
    if not old or not old[-1].get("price"):
        return None
    return price / old[-1]["price"] - 1


def alt_discovery_score(price, ret5, history, now, high, low, excursion, intraday_range, acceleration, flow) -> float:
    """FINAL-B discovery rank. Callers must not feed this into the live rank."""
    ret3 = window_return(history, now, price, 180)
    ret2 = window_return(history, now, price, 120)
    ret1 = window_return(history, now, price, 60)
    if ret5 is not None:
        best = ret5
    elif ret3 is not None:
        best = ret3
    elif ret2 is not None:
        best = ret2
    elif ret1 is not None:
        best = ret1
    else:
        best = 0.0
    bonus = 0.0
    local = now.astimezone(ET)
    after = (local.hour, local.minute, local.second) > (9, 35, 0)
    if after and acceleration is not None and acceleration >= 1.5:
        prior_high = [row["high"] for row in history if row.get("high") is not None]
        prior_low = [row["low"] for row in history if row.get("low") is not None]
        new_high = high is not None and prior_high and high > max(prior_high)
        new_low = low is not None and prior_low and low < min(prior_low)
        if new_high or new_low:
            bonus = 30.0
    return (100 * (3 * abs(best) + excursion + 0.5 * intraday_range)
            + min(3, acceleration or 0) + bonus + 0.5 * (flow or 0))
