"""Flag-gated scanner fixes from SCANNER-FIXES-1124.

Every mode is an integer: 0 off, 1 shadow (log only), 2 on. Mode 0 does not
write feature fields, shadow rows, or checks. SPX volume is already the SPY
share volume on ``snap.bars``; prices stay SPX.
"""
import contextvars
import json
from datetime import datetime, time
from math import isfinite

from .models import ET

# SPY/QQQ for the symbol currently being evaluated. Kept off the candidates()
# signature so existing callers and test doubles stay unchanged.
BENCH = contextvars.ContextVar("lotto_fix_bench", default=None)

FAST_RUN_MAX_PULLBACK = 0.05
_EXEMPT_SETUPS = frozenset({"PULLBACK_RESUMPTION", "LEVEL_RECLAIM"})
_INDEX_LANE = frozenset({"SPY", "QQQ", "SPX"})
LM_KEYS = ("lm_ext_atr", "lm_hb", "lm_fresh_lvl", "lm_rv2", "lm_block")
MKT_KEYS = ("mkt5", "mkt10", "mkt15", "mkt_state", "mg_block")
FR_KEYS = (
    "fr_lvl", "fr_run5", "fr_run10", "fr_run15", "fr_st10", "fr_pb10", "fr_start_ext10",
    "fr_m_run10", "fr_m_st10", "fr_m_run15", "fr_m_pierce", "fr_mkt_class", "fr_block", "fr_fast",
)
LATE_MOVE_CHECK = "late move: HOD/LOD break >= {atr:g} ATR from LOD/HOD"
MKT_CHECK = "SPY/QQQ chop on a breakout"
FAST_RUN_CHECK = "fast straight run into a level"
AFTERNOON_CHECK = "afternoon: needs a completed HOD/LOD or PM/PD break"
AFTERNOON_INDEX = frozenset({"SPY", "QQQ", "IWM", "SPX"})


def fixes_active(settings) -> bool:
    return any(getattr(settings, name) >= 1 for name in (
        "late_move_mode", "mkt_gate_mode", "stock_first_mode", "fast_run_mode",
        "afternoon_level_mode"))


def late_move_check(settings) -> str:
    return LATE_MOVE_CHECK.format(atr=settings.late_move_atr)


def _atr(snap):
    atr = snap.prior_atr
    if atr is None or not isfinite(atr) or atr <= 0:
        return None
    return atr


def _empty_late():
    return {key: None for key in LM_KEYS}


def late_move(snap, side: str, settings) -> dict:
    """F1 fields from completed RTH bars. Missing ATR fails open (every field None)."""
    atr = _atr(snap)
    bars = snap.bars
    if atr is None or not bars:
        return _empty_late()
    last_close = bars[-1].close
    lod = min(bar.low for bar in bars)
    hod = max(bar.high for bar in bars)
    if side == "CALL":
        extension = (last_close - lod) / atr
    else:
        extension = (hod - last_close) / atr
    if len(bars) <= 3:
        broke = True
    elif side == "CALL":
        broke = max(bar.high for bar in bars[-3:]) >= max(bar.high for bar in bars[:-3])
    else:
        broke = min(bar.low for bar in bars[-3:]) <= min(bar.low for bar in bars[:-3])
    fresh = _fresh_level(bars, snap, side)
    rvol = _rv2(bars)
    override = (settings.late_move_override_mode == 1 and fresh
                and rvol is not None and rvol >= settings.late_move_override_rvol)
    blocked = bool(broke and extension >= settings.late_move_atr and not override)
    return {
        "lm_ext_atr": extension,
        "lm_hb": int(broke),
        "lm_fresh_lvl": int(fresh),
        "lm_rv2": rvol,
        "lm_block": int(blocked),
    }


def _fresh_level(bars, snap, side: str) -> bool:
    levels = (snap.premarket_high, snap.prior_high) if side == "CALL" else (snap.premarket_low, snap.prior_low)
    for level in levels:
        if level is None or not isfinite(level):
            continue
        first = None
        for index, bar in enumerate(bars):
            beyond = bar.close > level if side == "CALL" else bar.close < level
            if beyond:
                first = index
                break
        if first is not None and first >= len(bars) - 5 and first > 0:
            return True
    return False


def _rv2(bars):
    if len(bars) < 6:
        return None
    base = bars[-22:-2]
    if not base:
        return None
    mean = sum(bar.volume for bar in base) / len(base)
    if mean <= 0:
        return None
    return (bars[-1].volume + bars[-2].volume) / (2 * mean)


def _closes_through(snap, at):
    bars = [bar for bar in snap.bars if bar.end <= at]
    return bars


def _window_return(closes, width, sign, atr):
    if atr is None or len(closes) <= width:
        return None
    return sign * (closes[-1] - closes[-1 - width]) / atr


def _mean(values):
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)


def market_gate(snap, side: str, settings, bench, hod_break) -> dict:
    """F2 SPY/QQQ state. Neither ETF available is ``unknown`` and never blocks."""
    sign = 1 if side == "CALL" else -1
    returns = []
    for symbol in ("SPY", "QQQ"):
        other = (bench or {}).get(symbol)
        if other is None:
            returns.append(None)
            continue
        bars = _closes_through(other, snap.at)
        atr = _atr(other)
        if not bars or atr is None:
            returns.append(None)
            continue
        closes = [bar.close for bar in bars]
        returns.append({width: _window_return(closes, width, sign, atr) for width in (5, 10, 15)})
    usable = [item for item in returns if item is not None]
    if not usable:
        state, m5, m10, m15 = "unknown", None, None, None
    else:
        m5 = _mean(item[5] for item in usable)
        m10 = _mean(item[10] for item in usable)
        m15 = _mean(item[15] for item in usable)
        if m15 is None:
            state = "unknown"
        elif m15 >= settings.mkt_gate_th15 and (
                (m5 is not None and m5 >= settings.mkt_gate_th5)
                or (m10 is not None and m10 >= settings.mkt_gate_th10)):
            state = "aligned"
        elif m15 <= -settings.mkt_gate_th15:
            state = "opposed"
        else:
            state = "chop"
    return {"mkt5": m5, "mkt10": m10, "mkt15": m15, "mkt_state": state, "mg_block": 0, "_hod": hod_break}


def mg_block(state: str, hod_break, setup) -> int:
    if state in (None, "unknown"):
        return 0
    name = setup.name if setup is not None and getattr(setup, "name", None) else ""
    return int(bool(hod_break) and state in ("chop", "opposed") and name not in _EXEMPT_SETUPS)


def _straight(closes, width, sign):
    if len(closes) <= width:
        return None
    net = sign * (closes[-1] - closes[-1 - width])
    path = sum(abs(closes[index] - closes[index - 1]) for index in range(len(closes) - width, len(closes)))
    if path == 0:
        return 0.0
    return net / path


def _pullback(closes, width, sign, atr):
    if atr is None or len(closes) <= width:
        return None
    window = closes[-(width + 1):]
    base = window[0]
    peak = 0.0
    worst = 0.0
    for close in window:
        run = sign * (close - base)
        if run > peak:
            peak = run
        if peak - run > worst:
            worst = peak - run
    return worst / atr


def _start_extension(bars, side, atr):
    if atr is None or len(bars) < 11:
        return None
    known = bars[:-10]
    start = bars[-11].close
    if side == "CALL":
        return (start - min(bar.low for bar in known)) / atr
    return (max(bar.high for bar in known) - start) / atr


def _level_crossed(bars, level, side) -> bool:
    if level is None or not isfinite(level) or len(bars) < 4:
        return False
    near = bars[-4].close
    last = bars[-3:]
    if side == "CALL":
        return near <= level and max(bar.high for bar in last) > level
    return near >= level and min(bar.low for bar in last) < level


def level_break(bars, snap, side: str) -> bool:
    """A PM, prior-day, prior-30-bar, or session HOD/LOD level crossed in the last 3 bars."""
    if len(bars) < 4:
        return False
    if side == "CALL":
        levels = [snap.premarket_high, snap.prior_high]
        prior = bars[:-3]
        if prior:
            levels.append(max(bar.high for bar in prior))
            levels.append(max(bar.high for bar in prior[-30:]))
    else:
        levels = [snap.premarket_low, snap.prior_low]
        prior = bars[:-3]
        if prior:
            levels.append(min(bar.low for bar in prior))
            levels.append(min(bar.low for bar in prior[-30:]))
    return any(_level_crossed(bars, level, side) for level in levels)


def _series(snap, at, side):
    bars = _closes_through(snap, at) if at is not None else list(snap.bars)
    atr = _atr(snap)
    if not bars or atr is None:
        return None
    closes = [bar.close for bar in bars]
    sign = 1 if side == "CALL" else -1
    return {
        "bars": bars,
        "run5": _window_return(closes, 5, sign, atr),
        "run10": _window_return(closes, 10, sign, atr),
        "run15": _window_return(closes, 15, sign, atr),
        "st10": _straight(closes, 10, sign),
        "pb10": _pullback(closes, 10, sign, atr),
        "pierce": _pierce(bars, side),
    }


def _pierce(bars, side) -> int:
    if len(bars) <= 3:
        return 1
    if side == "CALL":
        return int(max(bar.high for bar in bars[-3:]) >= max(bar.high for bar in bars[:-3]))
    return int(min(bar.low for bar in bars[-3:]) <= min(bar.low for bar in bars[:-3]))


def fast_run(snap, side: str, settings, bench) -> dict:
    """F5 fast straight run into a level. Missing SPY and QQQ is ``unknown`` and never blocks."""
    sign = 1 if side == "CALL" else -1
    atr = _atr(snap)
    bars = list(snap.bars)
    closes = [bar.close for bar in bars]
    broke = level_break(bars, snap, side) if bars else False
    run5 = _window_return(closes, 5, sign, atr)
    run10 = _window_return(closes, 10, sign, atr)
    run15 = _window_return(closes, 15, sign, atr)
    st10 = _straight(closes, 10, sign)
    pb10 = _pullback(closes, 10, sign, atr)
    fast = bool(
        broke and run10 is not None and run10 >= settings.fast_run_atr
        and st10 is not None and st10 >= settings.fast_run_straight
        and pb10 is not None and pb10 <= FAST_RUN_MAX_PULLBACK)
    market = []
    for symbol in ("SPY", "QQQ"):
        other = (bench or {}).get(symbol)
        if other is None:
            continue
        series = _series(other, snap.at, side)
        if series is not None:
            market.append(series)
    if not market:
        klass = "unknown"
        m_run10 = m_st10 = m_run15 = m_pierce = None
    else:
        m_run10 = _mean(item["run10"] for item in market)
        m_st10 = _mean(item["st10"] for item in market)
        m_run15 = _mean(item["run15"] for item in market)
        m_pierce = int(any(item["pierce"] for item in market))
        if m_run10 is None or m_st10 is None or m_run15 is None:
            klass = "unknown"
        elif m_run10 >= 0.15 and m_st10 >= 0.60:
            klass = "spiked"
        elif m_run15 >= 0.10:
            klass = "steady"
        else:
            klass = "alone"
    blocked = bool(fast and klass != "unknown" and (
        settings.fast_run_mkt == "all" or klass == settings.fast_run_mkt))
    return {
        "fr_lvl": int(broke),
        "fr_run5": run5,
        "fr_run10": run10,
        "fr_run15": run15,
        "fr_st10": st10,
        "fr_pb10": pb10,
        "fr_start_ext10": _start_extension(bars, side, atr),
        "fr_m_run10": m_run10,
        "fr_m_st10": m_st10,
        "fr_m_run15": m_run15,
        "fr_m_pierce": m_pierce,
        "fr_mkt_class": klass,
        "fr_fast": int(fast),
        "fr_block": int(blocked),
    }


def _beyond(close, level, side) -> bool:
    return close > level if side == "CALL" else close < level


def _crossed_recent(bars, level, side, window) -> bool:
    """A completed close crossed the level inside the last `window` bars and is still beyond it."""
    if level is None or not isfinite(level) or len(bars) < 2:
        return False
    if not _beyond(bars[-1].close, level, side):
        return False
    start = max(1, len(bars) - window)
    for index in range(start, len(bars)):
        if _beyond(bars[index].close, level, side) and not _beyond(bars[index - 1].close, level, side):
            return True
    return False


def _hod_lod_break(bars, side) -> bool:
    """One of the last 3 completed closes is beyond the earlier session high or low, and the last close still is."""
    if len(bars) <= 3:
        return False
    earlier, last = bars[:-3], bars[-3:]
    if side == "CALL":
        level = max(bar.high for bar in earlier)
    else:
        level = min(bar.low for bar in earlier)
    if not _beyond(bars[-1].close, level, side):
        return False
    return any(_beyond(bar.close, level, side) for bar in last)


def afternoon_level(snap, side, settings) -> dict:
    """After the start clock, a non-index name needs a completed-bar break.

    Uses completed RTH bars only. The live spot, which can sit in a still-forming
    minute, does not count. HOD/LOD, premarket, and prior-day levels all qualify.
    """
    hour, minute = settings.afternoon_level_start.split(":")
    start = time(int(hour), int(minute))
    local = snap.at.astimezone(ET).time()
    active = local >= start
    index = snap.symbol in AFTERNOON_INDEX
    hod = _hod_lod_break(snap.bars, side)
    pm_level = snap.premarket_high if side == "CALL" else snap.premarket_low
    pd_level = snap.prior_high if side == "CALL" else snap.prior_low
    pm = _crossed_recent(snap.bars, pm_level, side, 5)
    pd = _crossed_recent(snap.bars, pd_level, side, 5)
    blocked = active and not index and not (hod or pm or pd)
    return {
        "al_block": int(blocked),
        "al_hodlod": int(hod),
        "al_pm": int(pm),
        "al_pd": int(pd),
    }


def evaluate_fixes(snap, side, setup, settings, bench) -> dict | None:
    """Compute every enabled fix. None when every mode is 0."""
    if not fixes_active(settings):
        return None
    state = {}
    need_late = settings.late_move_mode >= 1 or settings.mkt_gate_mode >= 1
    if need_late:
        state.update(late_move(snap, side, settings))
    if settings.mkt_gate_mode >= 1:
        gate = market_gate(snap, side, settings, bench, state.get("lm_hb"))
        gate["mg_block"] = mg_block(gate["mkt_state"], state.get("lm_hb"), setup)
        gate.pop("_hod", None)
        state.update(gate)
    if settings.fast_run_mode >= 1:
        state.update(fast_run(snap, side, settings, bench))
    if settings.afternoon_level_mode >= 1:
        state.update(afternoon_level(snap, side, settings))
    return state


def apply_scanner_fixes(snap, side, setup, metrics, checks, settings, bench) -> dict | None:
    """Mode 1 writes fields. Mode 2 also adds a failing check. Mode 0 returns None."""
    state = evaluate_fixes(snap, side, setup, settings, bench)
    if state is None:
        return None
    if settings.late_move_mode >= 1:
        for key in LM_KEYS:
            metrics[key] = state.get(key)
    if settings.mkt_gate_mode >= 1:
        for key in MKT_KEYS:
            metrics[key] = state.get(key)
    if settings.late_move_mode == 2:
        checks[late_move_check(settings)] = not bool(state.get("lm_block"))
    if settings.mkt_gate_mode == 2:
        checks[MKT_CHECK] = not bool(state.get("mg_block"))
    if settings.fast_run_mode == 2:
        checks[FAST_RUN_CHECK] = not bool(state.get("fr_block"))
    if settings.afternoon_level_mode == 2:
        checks[AFTERNOON_CHECK] = not bool(state.get("al_block"))
    return state


def in_stock_first_window(at) -> bool:
    local = at.astimezone(ET)
    return time(9, 45) <= local.time() < time(14, 0)


def trigger_gap(snap, direction: int, setup):
    atr = _atr(snap)
    if setup is None or atr is None:
        return None
    return direction * (snap.spot - setup.trigger) / atr


def invalidation_holds(snap, direction: int, setup) -> bool:
    """Price is still on the trade side of the stop.

    The live check passes when ``direction * (spot - invalidation) > 0``.
    That is the scanner's "not crossed" condition. The spec text wrote ``<= 0``
    for the same words; the live inequality is the one that keeps a valid setup.
    """
    if setup is None:
        return False
    return direction * (snap.spot - setup.invalidation) > 0


def lane_context(snap, side, settings, bench) -> dict:
    """F1/F2/F5 fields the stock-first lane needs, whether or not those modes are on."""
    late = late_move(snap, side, settings)
    gate = market_gate(snap, side, settings, bench, late.get("lm_hb"))
    gate.pop("_hod", None)
    run = fast_run(snap, side, settings, bench)
    return {**late, **gate, **run}


def shadow_flags(settings, state) -> str:
    payload = {"modes": {
        "lm": settings.late_move_mode,
        "mg": settings.mkt_gate_mode,
        "sf": settings.stock_first_mode,
        "fr": settings.fast_run_mode,
        "fr_mkt": settings.fast_run_mkt,
        "al": settings.afternoon_level_mode,
    }}
    if settings.fast_run_mode >= 1 and state:
        payload["fr"] = {key: _compact(state.get(key)) for key in FR_KEYS}
    if settings.afternoon_level_mode >= 1 and state:
        payload["al"] = {key: state.get(key) for key in ("al_block", "al_hodlod", "al_pm", "al_pd")}
    return json.dumps(payload, separators=(",", ":"))


def _compact(value):
    if isinstance(value, float):
        return round(value, 5)
    return value


def _et_day(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(ET).date().isoformat()


def _opt_label(entry_mid, mids):
    """OPT: +40% before -35% on the option mid. No hit counts as a loss."""
    if entry_mid is None or entry_mid <= 0 or not mids:
        return None
    up, down = entry_mid * 1.40, entry_mid * 0.65
    for mid in mids:
        if mid >= up:
            return "win"
        if mid <= down:
            return "loss"
    return "loss"


def _alert_mids(store, alert):
    from .config import Settings
    from .features import quote_ok
    from .models import Snapshot
    from .store import read_snapshot

    raw = alert["snapshot"] if "snapshot" in alert.keys() else None
    if not raw:
        return None, []
    try:
        entry_snap = Snapshot.from_dict(json.loads(raw))
    except (TypeError, ValueError, KeyError):
        return None, []
    option = next((item for item in entry_snap.options if item.symbol == alert["contract"]), None)
    if option is None or option.bid is None or option.ask is None:
        return None, []
    entry_mid = (option.bid + option.ask) / 2
    settings = Settings()
    mids = []
    rows = store.db.execute(
        "SELECT payload FROM snapshots WHERE symbol=? AND at>=? ORDER BY at",
        (alert["symbol"], alert["at"]))
    for (payload,) in rows:
        try:
            snap = read_snapshot(payload)
        except (TypeError, ValueError, KeyError):
            continue
        match = next((item for item in snap.options if item.symbol == alert["contract"]), None)
        if match is None or not quote_ok(match, snap, settings):
            continue
        if match.quote_time < option.quote_time:
            continue
        mids.append((match.bid + match.ask) / 2)
    return entry_mid, mids


def scanner_fix_lines(store, day: str) -> list[str]:
    """Three nightly lines: cards each block would have removed, with OPT grade."""
    tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    blank = [
        "late-move would block 0 cards, OPT 0/0",
        "market-gate would block 0 cards, OPT 0/0",
        "fast-run all would block 0 cards, OPT 0/0; spiked 0 OPT 0/0; alone 0 OPT 0/0",
    ]
    if "fix_shadow" not in tables:
        return blank
    alerts = [row for row in store.db.execute("SELECT * FROM alerts WHERE day=?", (day,))]
    by_cycle = {}
    for alert in alerts:
        by_cycle.setdefault((alert["symbol"], alert["side"], alert["at"]), alert)
    grades = {}

    def grade(alert):
        ident = alert["id"]
        if ident not in grades:
            entry, mids = _alert_mids(store, alert)
            grades[ident] = _opt_label(entry, mids)
        return grades[ident]

    def summarize(chosen):
        seen = []
        for row in chosen:
            alert = by_cycle.get((row["ticker"], row["side"], row["ts"]))
            if alert is not None and alert["id"] not in {item["id"] for item in seen}:
                seen.append(alert)
        wins = graded = 0
        for alert in seen:
            label = grade(alert)
            if label is None:
                continue
            graded += 1
            wins += label == "win"
        return len(seen), wins, graded

    rows = [row for row in store.db.execute("SELECT * FROM fix_shadow") if _et_day(row["ts"]) == day]
    late_n, late_w, late_g = summarize(row for row in rows if row["lm_block"] == 1)
    mkt_n, mkt_w, mkt_g = summarize(row for row in rows if row["mg_block"] == 1)

    def fast_rows(kind):
        chosen = []
        for row in rows:
            try:
                payload = json.loads(row["fix_flags"] or "{}")
            except ValueError:
                continue
            fields = payload.get("fr") or {}
            if not fields.get("fr_fast"):
                continue
            klass = fields.get("fr_mkt_class")
            if kind == "all":
                if klass != "unknown":
                    chosen.append(row)
            elif klass == kind:
                chosen.append(row)
        return chosen

    all_n, all_w, all_g = summarize(fast_rows("all"))
    spiked_n, spiked_w, spiked_g = summarize(fast_rows("spiked"))
    alone_n, alone_w, alone_g = summarize(fast_rows("alone"))
    return [
        f"late-move would block {late_n} cards, OPT {late_w}/{late_g}",
        f"market-gate would block {mkt_n} cards, OPT {mkt_w}/{mkt_g}",
        (f"fast-run all would block {all_n} cards, OPT {all_w}/{all_g}; "
         f"spiked {spiked_n} OPT {spiked_w}/{spiked_g}; "
         f"alone {alone_n} OPT {alone_w}/{alone_g}"),
    ]
