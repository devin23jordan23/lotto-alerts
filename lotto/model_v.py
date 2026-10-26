"""Model V score. One channel, priced option cards only.

v2 is computed directly. It is not the live hybrid tier blend.

Live blend (not used for posting):
    blend = feature_score
            - 10 * |{OPEN, EXHQ, W1DTE, PREM}|
            - 25 if WF or LATE
            + 5 if hourly_break
    tier A if blend >= 91, B if blend >= 81, else C.

v2 (used for posting):
    option_v2 = hybrid feature score
        (rule set B on 0DTE, foundation rule set F from 6d8cf1f on 1DTE)
        + contract-volume points + premium-expansion points
        + the same S-rule point penalties the blend subtracts
        + spread and tiny-premium penalties
        + hourly_break points (weight defaults to the blend's +5)
    stock_v2 = ATR move, RVOL, held PMH/PDH/range breaks, VWAP side,
        freshness, the SPY day/VWAP label, and level retest-and-hold.
    A weak stock is scaled down so it cannot carry a weak option.
    A strong stock, or a thin option tape, can add more, up to a cap.
    score_v2 = option_v2 + stock_v2

When a stored row has only the live blend, invert it if the rule counts are
present. If they are not, score_v2 stays empty and the reason is logged.
Those rows are kept. They are not posted and they are not dropped silently.

Default posting (LOTTO_SCORE_MODE=combined) is one total and one bar per expiry.
The bars and weights are PLACEHOLDER until Vince's 3:00 PM ET pass.
Stock points are capped at stock_max_share of that total, so stock alone
cannot clear the bar. Option points at or above the bar post with no stock help.
An exceptional stock that hits every strong-stock check can still post the
priced 1-OTM contract. That card is path=stock_override.

LOTTO_SCORE_MODE=s12 keeps the two-path comparison:
    option-led  0DTE >= 40, 1DTE >= 61
    stock-led   0DTE >= 58, 1DTE >= 80
    plus the fitted-probability floors when a handoff model file is loaded.
"""
import json
from dataclasses import replace
from datetime import datetime, timedelta
from math import exp, isfinite
from pathlib import Path

from .config import Settings
from .features import Candidate
from .models import ET, Option, Snapshot
from .patterns import Setup
from .quality import evaluate, spy_context_label


def priced(option: Option, snap: Snapshot, settings: Settings) -> bool:
    """A quote exists that can price the contract. Spread and premium are not gates."""
    if not option.valid() or option.bid <= 0 or option.ask <= option.bid:
        return False
    dte = (option.expiry - snap.at.astimezone(ET).date()).days
    if not 0 <= dte <= settings.max_dte:
        return False
    age = (snap.at - option.quote_time).total_seconds()
    return -5 <= age <= 900


def dte_of(option: Option, snap: Snapshot) -> int:
    return (option.expiry - snap.at.astimezone(ET).date()).days


def pick_one_otm(snap: Snapshot, side: str, settings: Settings) -> Option | None:
    """Nearest expiry (0DTE if any priced contract exists, else 1DTE), one strike OTM.

    When the nearest strike is already out of the money, that strike is the contract.
    """
    today = snap.at.astimezone(ET).date()
    pool = [o for o in snap.options if o.side == side and priced(o, snap, settings)]
    if not pool:
        return None
    use = 0 if any((o.expiry - today).days == 0 for o in pool) else 1
    pool = [o for o in pool if (o.expiry - today).days == use]
    if not pool:
        return None
    direction = 1 if side == "CALL" else -1
    strikes = sorted({o.strike for o in pool})
    atm = min(strikes, key=lambda strike: (abs(strike - snap.spot), strike))
    if direction == 1:
        further = [strike for strike in strikes if strike > atm]
        target = further[0] if further and atm <= snap.spot else atm
    else:
        further = [strike for strike in strikes if strike < atm]
        target = further[-1] if further and atm >= snap.spot else atm
    choices = [o for o in pool if o.strike == target]
    return min(choices, key=lambda o: (o.ask - o.bid) / o.ask)


def _clamp(value: float, limit: float = 1) -> float:
    return max(0.0, min(limit, value))


def five_minute_bars(bars) -> list[dict]:
    """Completed clock-aligned 5-minute bars from 9:30. No partial bucket."""
    buckets: dict[int, list] = {}
    for bar in bars:
        local = bar.end.astimezone(ET)
        minute = local.hour * 60 + local.minute
        open_min = 9 * 60 + 30
        if minute <= open_min:
            continue
        index = (minute - open_min - 1) // 5
        buckets.setdefault(index, []).append(bar)
    built = []
    if not buckets:
        return built
    last_index = max(buckets)
    for index in sorted(buckets):
        group = buckets[index]
        end_min = 9 * 60 + 30 + (index + 1) * 5
        last = group[-1].end.astimezone(ET)
        if index == last_index and (last.hour * 60 + last.minute) < end_min:
            continue
        built.append({
            "index": index,
            "end": group[-1].end,
            "open": group[0].open,
            "high": max(b.high for b in group),
            "low": min(b.low for b in group),
            "close": group[-1].close,
        })
    return built


def _vwap_at(bars, end) -> float | None:
    volume = 0.0
    total = 0.0
    for bar in bars:
        if bar.end > end:
            break
        typical = (bar.high + bar.low + bar.close) / 3
        volume += bar.volume
        total += typical * bar.volume
    return total / volume if volume else None


def level_retest(snap: Snapshot, direction: int) -> dict:
    """Retest-and-hold components. A green VWAP tap alone scores nothing.

    Points require a retest of a level that already broke (30-minute opening
    range or a prior breakout), with a wick into the level and a 5-minute close
    back through it. VWAP reclaim and an SPY+QQQ sync are extra. Later retests
    of the same level are worth less. Every component is logged.
    """
    empty = {
        "retest_break": 0, "retest_first": 0, "retest_later": 0, "retest_wick": 0,
        "vwap_reclaim": 0, "vwap_tap_only": 0, "retest_level": None,
        "retest_count": 0, "retest_index": None,
    }
    bars = list(snap.bars)
    if len(bars) < 6:
        return empty
    fives = five_minute_bars(bars)
    if not fives:
        return empty
    atr = snap.prior_atr if snap.prior_atr and snap.prior_atr > 0 else snap.spot * 0.01
    tolerance = max(0.05 * atr, snap.spot * 0.0005)
    or_bars = [b for b in bars if b.end.astimezone(ET).time().hour == 9
               or (b.end.astimezone(ET).hour == 10 and b.end.astimezone(ET).minute == 0)]
    # Opening range is the first 30 minutes, usable only after it has closed.
    or_done = any(b.end.astimezone(ET).hour > 10 or (b.end.astimezone(ET).hour == 10 and b.end.astimezone(ET).minute >= 1)
                  for b in bars)
    or_high = max((b.high for b in or_bars), default=None) if or_done else None
    or_low = min((b.low for b in or_bars), default=None) if or_done else None
    if direction == 1:
        levels = [("ORH30", or_high), ("PDH", snap.prior_high), ("PMH", snap.premarket_high)]
    else:
        levels = [("ORL30", or_low), ("PDL", snap.prior_low), ("PML", snap.premarket_low)]
    best = None
    for name, level in levels:
        if level is None or not isfinite(level):
            continue
        broke_at = None
        retests = []
        for bar in fives:
            beyond = direction * (bar["close"] - level) > tolerance
            if broke_at is None and beyond:
                broke_at = bar["index"]
                continue
            if broke_at is None or bar["index"] <= broke_at:
                continue
            touched = bar["low"] - tolerance <= level <= bar["high"] + tolerance
            held = direction * (bar["close"] - level) > 0
            if touched and held:
                retests.append(bar)
        if not retests:
            continue
        bar = retests[-1]
        count = len(retests)
        first = count == 1
        candidate = {
            "retest_break": 1, "retest_first": int(first), "retest_later": int(not first),
            "retest_wick": 1, "vwap_reclaim": 0, "vwap_tap_only": 0,
            "retest_level": name, "retest_count": count, "retest_index": bar["index"],
            "retest_close": bar["close"], "retest_end": bar["end"],
        }
        vwap = _vwap_at(bars, bar["end"])
        if vwap is not None and direction * (bar["close"] - vwap) > 0:
            earlier = [b for b in fives if b["index"] < bar["index"]]
            was_other_side = any(direction * (b["close"] - (_vwap_at(bars, b["end"]) or vwap)) <= 0 for b in earlier[-6:])
            if was_other_side or name != "VWAP":
                candidate["vwap_reclaim"] = 1
        if best is None or (first and not best["retest_first"]) or count < best["retest_count"]:
            best = candidate
    # A VWAP tap that is not a broken-level retest is logged and scores zero.
    if best is None:
        last = fives[-1]
        vwap = _vwap_at(bars, last["end"])
        tapped = vwap is not None and last["low"] <= vwap <= last["high"] and direction * (last["close"] - last["open"]) > 0
        empty["vwap_tap_only"] = int(bool(tapped))
        empty["retest_index"] = last["index"]
        return empty
    return best


def retest_points(parts: dict, settings: Settings, sync: bool) -> float:
    if not parts.get("retest_break") or not parts.get("retest_wick"):
        return 0.0
    points = settings.w_retest_first if parts.get("retest_first") else settings.w_retest_later
    points += settings.w_retest_wick
    if parts.get("vwap_reclaim"):
        points += settings.w_vwap_reclaim
    if sync:
        points += settings.w_retest_sync
    return points


def held_level(bars, level, direction: int, closes: int = 2) -> bool:
    if level is None or not isfinite(level) or len(bars) < closes:
        return False
    return all(direction * (bar.close - level) > 0 for bar in bars[-closes:])


def stock_terms(snap: Snapshot, direction: int, metrics: dict, settings: Settings, sync: bool) -> dict:
    atr = snap.prior_atr if snap.prior_atr and snap.prior_atr > 0 else None
    move = metrics.get("move_from_open_atr") or 0
    move_points = min(settings.w_move_atr * 2, settings.w_move_atr * max(0.0, move))
    rvol = max(metrics.get("pace_rvol") or 0, metrics.get("local_rvol_5m") or 0)
    rvol_points = min(settings.w_rvol * 2, settings.w_rvol * max(0.0, rvol - 1))
    if direction == 1:
        held_pm = held_level(snap.bars, snap.premarket_high, direction)
        held_pd = held_level(snap.bars, snap.prior_high, direction)
        prior_extreme = max((b.high for b in snap.bars[:-2]), default=None)
    else:
        held_pm = held_level(snap.bars, snap.premarket_low, direction)
        held_pd = held_level(snap.bars, snap.prior_low, direction)
        prior_extreme = min((b.low for b in snap.bars[:-2]), default=None) if len(snap.bars) > 2 else None
    held_range = held_level(snap.bars, prior_extreme, direction) if prior_extreme is not None else False
    vwap_points = settings.w_vwap if (metrics.get("above_vwap_share") or 0) >= 0.6 else 0.0
    quote_age = (snap.at - snap.spot_time).total_seconds()
    fresh = 1.0 if quote_age <= 30 else 0.5 if quote_age <= 90 else 0.0
    label = spy_context_label(snap, direction)
    if label and "aligned · pulling back" in label:
        spy_points = settings.w_spy * 0.5
    elif label and label.endswith("aligned"):
        spy_points = settings.w_spy
    elif label and label.endswith("opposing"):
        spy_points = -settings.w_spy
    else:
        spy_points = 0.0
    retest = level_retest(snap, direction)
    retest["index_sync"] = int(sync and retest.get("retest_break"))
    terms = {
        "move_atr": round(move_points, 2),
        "rvol": round(rvol_points, 2),
        "held_pm": settings.w_held_pm if held_pm else 0.0,
        "held_pd": settings.w_held_pd if held_pd else 0.0,
        "held_range": settings.w_held_range if held_range else 0.0,
        "vwap_side": vwap_points,
        "freshness": round(settings.w_fresh * fresh, 2),
        "spy_context": round(spy_points, 2),
        "level_retest": round(retest_points(retest, settings, bool(retest["index_sync"])), 2),
    }
    return terms, retest, label, {
        "held_pm": int(held_pm), "held_pd": int(held_pd), "held_range": int(held_range),
        "rvol_used": rvol, "quote_age_seconds": quote_age,
    }


def option_extras(candidate: Candidate, settings: Settings, history: list[Snapshot]) -> dict:
    option, snap = candidate.option, candidate.snapshot
    rate = candidate.metrics.get("contract_volume_rate_5m") or 0
    volume_points = settings.w_contract_volume * _clamp(rate / 500)
    old_ask = None
    for prior in reversed(history):
        match = next((item for item in prior.options if item.symbol == option.symbol and item.ask > 0), None)
        if match and prior.at < snap.at:
            old_ask = match.ask
            break
    expansion = (option.ask / old_ask - 1) if old_ask else 0
    expansion_points = settings.w_premium_expansion * _clamp(max(0.0, expansion) / 0.25)
    spread = (option.ask - option.bid) / option.ask if option.ask else 1
    spread_points = -settings.w_spread_penalty * _clamp(max(0.0, spread - settings.max_spread_fraction) / 0.20)
    tiny = -settings.w_tiny_premium if option.ask < settings.min_ask else 0.0
    return {
        "contract_volume": round(volume_points, 2),
        "premium_expansion": round(expansion_points, 2),
        "spread_penalty": round(spread_points, 2),
        "tiny_premium": tiny,
    }


def s_rule_penalties(candidate: Candidate, settings: Settings) -> tuple[dict, tuple, tuple]:
    policy = evaluate(candidate)
    weights = {
        "WF": settings.pen_wf, "LATE": settings.pen_late, "OPEN": settings.pen_open,
        "EXHQ": settings.pen_exhq, "W1DTE": settings.pen_w1dte, "PREM": settings.pen_prem,
    }
    points = {f"pen_{name.lower()}": weights[name] for name in policy.reasons if name in weights}
    return points, policy.reasons, policy.labels


STRENGTH_FEATURES = ("move_from_open_atr", "rvol", "held_pm", "held_pd", "held_range")
_FILE_CACHE: dict = {}


def post_bar(path: str, dte: int, settings: Settings) -> float:
    if path == "combined":
        return settings.post_combined_0dte if dte == 0 else settings.post_combined_1dte
    if path == "stock":
        return settings.post_v2_stock_0dte if dte == 0 else settings.post_v2_stock_1dte
    return settings.post_v2_option_0dte if dte == 0 else settings.post_v2_option_1dte


def cap_stock(option_v2: float, stock_raw: float, settings: Settings) -> tuple[float, float, float]:
    """Cap positive stock points so they stay within stock_max_share of the total.

    A zero option score gets no stock help. A negative stock score still subtracts.
    Returns (stock_used, cap, total).
    """
    if stock_raw <= 0:
        return round(stock_raw, 2), 0.0, round(option_v2 + stock_raw, 2)
    share = settings.stock_max_share
    ratio_cap = (share / (1.0 - share)) * max(option_v2, 0.0)
    cap = min(settings.stock_push_cap, ratio_cap)
    used = min(stock_raw, cap)
    return round(used, 2), round(cap, 2), round(option_v2 + used, 2)


def exceptional_stock(metrics: dict, flags: dict, snap: Snapshot, settings: Settings,
                      setup, handoff: bool) -> bool:
    """Every strong-stock check at once. Defaults are high so this stays rare."""
    move = metrics.get("move_from_open_atr") or 0
    rvol = flags.get("rvol_used") or 0
    held = any(flags.get(name) for name in ("held_pm", "held_pd", "held_range"))
    vwap_ok = (metrics.get("above_vwap_share") or 0) >= settings.override_vwap_share
    quote_age = (snap.at - snap.spot_time).total_seconds()
    fresh_quote = 0 <= quote_age <= settings.override_fresh_seconds
    atr = snap.prior_atr if snap.prior_atr and snap.prior_atr > 0 else snap.spot * 0.01
    near_trigger = False
    if setup is not None and not getattr(setup, "building", False):
        near_trigger = abs(snap.spot - setup.trigger) <= max(0.25 * atr, 0.001 * snap.spot)
    fresh = fresh_quote and (handoff or near_trigger)
    return bool(move >= settings.override_move_atr and rvol >= settings.override_rvol
                and held and vwap_ok and fresh)


def _load_json(path: str, kind: str):
    if not path:
        return None, f"{kind} not configured"
    file = Path(path)
    try:
        stat = file.stat()
    except OSError:
        return None, f"{kind} unreadable"
    key = (kind, str(file), stat.st_mtime_ns, stat.st_size)
    if key in _FILE_CACHE:
        return _FILE_CACHE[key]
    try:
        payload = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError):
        result = (None, f"{kind} invalid")
        _FILE_CACHE[key] = result
        return result
    result = (payload, None)
    _FILE_CACHE[key] = result
    return result


def stock_strength(features: dict, distribution) -> tuple[float | None, dict | str]:
    """Mean percentile rank of the five strength inputs. None until a reference file is loaded."""
    if not isinstance(distribution, dict):
        return None, "stock_strength: reference distribution not loaded"
    ranks = {}
    for name in STRENGTH_FEATURES:
        sample = distribution.get(name)
        value = features.get(name)
        if not isinstance(sample, list) or not sample or value is None or not isfinite(value):
            return None, f"stock_strength: {name} missing"
        numeric = [float(item) for item in sample if isinstance(item, (int, float)) and isfinite(item)]
        if not numeric:
            return None, f"stock_strength: {name} missing"
        ranks[name] = round(sum(1 for item in numeric if item <= value) / len(numeric), 4)
    return round(sum(ranks.values()) / len(ranks), 4), ranks


def fitted_win_probability(features: dict, model) -> tuple[float | None, str | None]:
    """Logistic handoff model.

    TODO: Vince's coefficient file was not in the attached run. Point
    LOTTO_HANDOFF_MODEL_PATH at {"link": "logistic", "intercept": n, "coefficients": {...}}.
    Until that file is present this returns None and the s12 probability path does not post.
    """
    if not isinstance(model, dict) or "coefficients" not in model:
        return None, "handoff model not loaded"
    if model.get("link", "logistic") != "logistic":
        return None, f"unsupported link {model.get('link')}"
    coefficients = model.get("coefficients") or {}
    missing = [name for name in coefficients if name not in features or features.get(name) is None]
    z = float(model.get("intercept") or 0)
    for name, weight in coefficients.items():
        raw = features.get(name)
        z += float(weight) * (float(raw) if isinstance(raw, (int, float)) and isfinite(raw) else 0.0)
    z = max(-30.0, min(30.0, z))
    probability = 1.0 / (1.0 + exp(-z))
    reason = f"missing features zero-filled: {','.join(missing)}" if missing else None
    return round(probability, 4), reason


def quote_universe(core, standby, cycle: int, batch: int, quote_every: int) -> list[str]:
    """Core names every cycle. Standby names fill the leftover quote-batch room.

    One /quotes call stays one call. Overflow standby names rotate instead of
    opening a second request. Chains are a separate, slower decision.
    """
    core = list(dict.fromkeys(core))
    seen = set(core)
    standby = [symbol for symbol in dict.fromkeys(standby) if symbol not in seen]
    if quote_every > 1 and cycle % quote_every != 0:
        standby = []
    room = max(0, batch - len(core))
    if len(standby) > room:
        if room == 0:
            standby = []
        else:
            start = (cycle * room) % len(standby)
            rotated = standby[start:] + standby[:start]
            standby = rotated[:room]
    return core + standby


def due_standby(symbols, scanned_at, now, scan_minutes: int, limit: int) -> list[str]:
    """In-play names whose chain is due. At most `limit` per cycle, oldest first."""
    fresh = []
    for symbol in symbols:
        last = scanned_at.get(symbol)
        if last is not None and (now - last).total_seconds() < scan_minutes * 60:
            continue
        fresh.append(symbol)
    fresh.sort(key=lambda symbol: scanned_at.get(symbol) or datetime.min.replace(tzinfo=now.tzinfo))
    return fresh[:max(0, limit)]


def scan_budget(n_core: int, n_standby: int, scan_minutes: int = 3, quote_batch: int = 180,
                min_interval: float = 0.25, snapshot_bytes: int = 12000,
                session_minutes: int = 390, scans_per_cycle: int = 4) -> dict:
    """Extra symbols, requests, and sqlite bytes versus the core universe.

    Quotes stay on the existing batched /quotes call. Chain work is the part
    that spends requests, and only for standby names that are actually in play.
    """
    quoted = min(n_standby, max(0, quote_batch - n_core))
    quote_calls = 1
    extra_quote_calls = 0
    # Each due name can cost a sweep chain plus a deep bars call and a deep chain.
    # The per-cycle cap keeps that off the 120/min market-data budget.
    per_scan = 3
    worst_extra_per_min = round(min(n_standby, scans_per_cycle) * per_scan, 2)
    spacing_starts_per_min = round(60 / min_interval, 1) if min_interval else None
    # A name that is in play for the whole session, scanned on the cadence.
    scans_each = session_minutes / max(scan_minutes, 1)
    worst_snapshot_bytes = int(n_standby * scans_each * snapshot_bytes)
    return {
        "core_symbols": n_core,
        "standby_symbols": n_standby,
        "standby_quoted_per_cycle": quoted,
        "quote_calls_per_minute": quote_calls,
        "extra_quote_calls_per_minute": extra_quote_calls,
        "standby_scans_per_cycle": scans_per_cycle,
        "worst_extra_chain_requests_per_minute": worst_extra_per_min,
        "spacing_request_starts_per_minute": spacing_starts_per_min,
        "published_schwab_market_data_per_minute": 120,
        "worst_extra_snapshot_bytes_per_day": worst_snapshot_bytes,
        "scan_minutes": scan_minutes,
    }


def index_sync(index_bars: dict, day: str, snap_at: datetime) -> bool:
    """True when SPY and QQQ both printed a broken-level retest in the same 5-minute bucket."""
    found = {}
    for symbol in ("SPY", "QQQ"):
        bars = index_bars.get((day, symbol))
        if not bars:
            continue
        if abs((snap_at - bars[-1].end).total_seconds()) > 180:
            continue
        probe = Snapshot(symbol, snap_at, bars[-1].close, snap_at, bars[0].open, tuple(bars), (),
                         prior_atr=bars[-1].close * 0.01)
        # prior_high is unknown here; ORH/ORL still qualifies once the range has broken.
        detail = level_retest(probe, 1) 
        if not detail.get("retest_break"):
            detail = level_retest(probe, -1)
        if detail.get("retest_break"):
            found[symbol] = detail.get("retest_index")
    return "SPY" in found and "QQQ" in found and found["SPY"] == found["QQQ"]


def apply_v2(candidate: Candidate, settings: Settings, history: list[Snapshot],
             index_bars: dict | None = None, handoff: bool = False, thin: bool = False) -> Candidate:
    """Fill score_v2, path, and every term. Does not drop the candidate."""
    snap, option = candidate.snapshot, candidate.option
    direction = 1 if option.side == "CALL" else -1
    sync = index_sync(index_bars or {}, snap.day, snap.at)
    metrics = dict(candidate.metrics)
    if "move_from_open_atr" not in metrics and snap.bars:
        from .patterns import path_features
        try:
            metrics.update(path_features(snap, direction))
        except ValueError:
            pass
    terms, retest, label, flags = stock_terms(snap, direction, metrics, settings, sync and bool(retest_ready(metrics)))
    # retest_points already included sync inside stock_terms when retest fired.
    extras = option_extras(candidate, settings, history)
    penalties, reasons, labels = s_rule_penalties(candidate, settings)
    hourly = settings.w_hourly_break if metrics.get("hourly_break") else 0.0
    option_v2 = candidate.score + sum(extras.values()) + sum(penalties.values()) + hourly
    stock_raw = sum(terms.values())
    thin = thin or not metrics.get("cluster_size")
    dte = dte_of(option, snap)
    mode = getattr(settings, "score_mode", "combined")
    if mode == "s12":
        if stock_raw >= settings.stock_push_score:
            scale = settings.stock_push_thin_weight if thin else settings.stock_push_weight
            stock_v2 = min(settings.stock_push_cap, stock_raw * scale)
        else:
            stock_v2 = stock_raw * settings.stock_weak_scale
        score_v2 = round(option_v2 + stock_v2, 2)
        stock_cap = settings.stock_push_cap
        option_bar = post_bar("option", dte, settings)
        if handoff and option_v2 < option_bar:
            path = "stock"
        elif option_v2 >= option_bar:
            path = "option"
        elif handoff or stock_raw >= settings.stock_push_score:
            path = "stock"
        else:
            path = "option"
        bar = post_bar(path, dte, settings)
    else:
        stock_v2, stock_cap, score_v2 = cap_stock(option_v2, stock_raw, settings)
        path = "combined"
        bar = post_bar("combined", dte, settings)
        option_bar = bar
    parts = dict(candidate.parts)
    parts.update(extras)
    parts.update(penalties)
    parts.update({f"stock_{name}": value for name, value in terms.items()})
    parts["hourly_break"] = hourly
    parts["stock_v2"] = round(stock_v2, 2)
    parts["stock_cap"] = stock_cap
    strength_features = {
        "move_from_open_atr": metrics.get("move_from_open_atr") or 0,
        "rvol": flags.get("rvol_used") or 0,
        "held_pm": flags.get("held_pm") or 0,
        "held_pd": flags.get("held_pd") or 0,
        "held_range": flags.get("held_range") or 0,
        "option_v2": round(option_v2, 2),
        "stock_raw": round(stock_raw, 2),
        "score_v2": score_v2,
        "dte": dte,
    }
    distribution, dist_reason = _load_json(settings.strength_distribution_path, "strength distribution")
    strength, strength_detail = stock_strength(strength_features, distribution)
    if strength is None and isinstance(strength_detail, str):
        strength_reason = strength_detail if settings.strength_distribution_path or not dist_reason else dist_reason
    else:
        strength_reason = dist_reason
    model, model_reason = _load_json(settings.handoff_model_path, "handoff model")
    probability, probability_reason = fitted_win_probability(strength_features, model)
    if probability is None:
        probability_reason = probability_reason or model_reason
    band, floor = None, None
    if mode == "s12" and path == "stock" and strength is not None and settings.probability_path:
        if strength >= settings.strength_decile:
            band, floor = "decile", settings.pwin_decile
        elif strength >= settings.strength_quintile:
            band = "quintile"
            floor = settings.pwin_quintile_0dte if dte == 0 else settings.pwin_quintile_1dte
    probability_post = int(floor is not None and probability is not None and probability >= floor and score_v2 < bar)
    override = int(mode == "combined" and exceptional_stock(metrics, flags, snap, settings, candidate.setup, handoff))
    if mode == "combined":
        qualifying = option_v2 >= bar or score_v2 >= bar
    else:
        qualifying = score_v2 >= bar or bool(probability_post)
    metrics.update({
        "model": "V", "rules": "B" if dte == 0 else "F",
        "score_mode": mode, "preset": settings.preset,
        "score_v2": score_v2, "option_v2": round(option_v2, 2),
        "stock_v2": round(stock_v2, 2), "stock_raw": round(stock_raw, 2),
        "stock_cap": stock_cap,
        "path": path, "thin_option": int(bool(thin or handoff and not metrics.get("cluster_size"))),
        "post_bar": bar, "handoff": int(handoff),
        "level_retest": terms["level_retest"],
        "below_by": round(max(0.0, bar - score_v2), 2),
        "option_below_by": round(max(0.0, bar - option_v2), 2),
        "stock_strength": strength,
        "stock_strength_ranks": strength_detail if isinstance(strength_detail, dict) else None,
        "strength_reason": strength_reason,
        "fitted_win_probability": probability,
        "fitted_win_reason": probability_reason,
        "strength_band": band, "probability_floor": floor, "probability_post": probability_post,
        "exceptional_stock": override,
        "spy_label": label, "s_rule_labels": list(labels), "s_rules": list(reasons),
        "tier_v2": None, "v2_reason": None,
        "market_return_5m": snap.market_return_5m,
        "market_return_from_open": snap.market_return_from_open,
        "market_vwap_side": snap.market_vwap_side,
        **{f"retest_{key}" if not str(key).startswith("retest_") and key != "index_sync" else key: value
           for key, value in retest.items() if key != "retest_end"},
        **flags,
    })
    # Keep the logged retest dict flat and JSON-safe.
    if "retest_end" in metrics:
        metrics["retest_end"] = metrics["retest_end"].isoformat()
    candidate.parts = parts
    candidate.metrics = metrics
    candidate.score = score_v2
    if label and label not in candidate.reasons:
        candidate.reasons = list(candidate.reasons) + [label]
    if terms["level_retest"]:
        candidate.reasons = list(candidate.reasons) + [
            f"Level retest {retest.get('retest_level')} ×{retest.get('retest_count')}"
        ]
    candidate.blockers = tuple(candidate.blockers)
    candidate.qualifying = qualifying
    return candidate


def retest_ready(_metrics) -> bool:
    return True


def missing_v2(reason: str) -> dict:
    """A candidate that cannot be placed on the v2 scale. Logged, never posted."""
    return {"score_v2": None, "tier_v2": None, "v2_reason": f"no v2 tier: {reason}", "path": None}


def invert_live_blend(blend: float | None, rules, hourly_break: bool | None) -> tuple[float | None, str | None]:
    """Map a stored live blend back to the pre-tier feature score.

    Returns (score_v2, reason). reason is set when the blend cannot be inverted.
    """
    if blend is None:
        return None, "no v2 tier: live blend missing and score_v2 was not stored"
    if rules is None:
        return None, "no v2 tier: live blend stored without the S-rule counts needed to invert it"
    names = set(rules)
    restored = blend + 10 * len(names & {"OPEN", "EXHQ", "W1DTE", "PREM"})
    if names & {"WF", "LATE"}:
        restored += 25
    if hourly_break is None:
        return None, "no v2 tier: live blend stored without hourly_break, so the +5 cannot be removed"
    if hourly_break:
        restored -= 5
    return round(restored, 2), None


def same_event(trigger: float | None, previous: float | None, spot: float, atr: float | None = None) -> bool:
    """Same contract and level inside the dedupe window.

    A continuation pivot ratchets with the session high. That is the same
    break until the level moves by a quarter ATR. A later, separate break
    still posts inside the window.
    """
    if trigger is None or previous is None:
        return trigger is None and previous is None
    tolerance = max(0.05, 0.001 * (spot or 0))
    if atr and atr > 0:
        tolerance = max(tolerance, 0.25 * atr)
    return abs(trigger - previous) <= tolerance


def quote_in_play(quote: dict | None, settings: Settings) -> bool:
    """Standby names are scanned only when they are gapping, moving, or loud."""
    if not quote:
        return False
    price, opening, previous = quote.get("price"), quote.get("open") or quote.get("opening"), quote.get("previous")
    if any(not isinstance(value, (int, float)) or not isfinite(value) or value <= 0
           for value in (price, opening, previous)):
        return False
    gap = abs(opening / previous - 1)
    move = abs(price / opening - 1)
    atr = quote.get("prior_atr")
    move_atr = abs(price - opening) / atr if isinstance(atr, (int, float)) and atr and atr > 0 else 0
    rvol = quote.get("volume_acceleration") or quote.get("pace_rvol") or 0
    return (gap >= settings.standby_gap or move >= settings.standby_move
            or move_atr >= settings.standby_atr or rvol >= settings.standby_rvol)


def classify_runner(runner: dict, alerts: list[dict], evaluations: list[dict], scanned: set[str]) -> dict:
    """Why a +200% runner was or was not covered. Research labels, not gates."""
    symbol, side, day = runner["symbol"], runner["side"], runner["day"]
    when = runner.get("at")
    if symbol not in scanned:
        return {"result": "not_in_universe", "below_by": None}
    posts = [row for row in alerts if row["symbol"] == symbol and row["side"] == side and row["day"] == day
             and row.get("posted")]
    evals = [row for row in evaluations if row["symbol"] == symbol and row["side"] == side and row["day"] == day]
    if when is not None:
        timely = [row for row in posts if row["at"] <= when]
        if timely:
            return {"result": "covered", "below_by": None, "alert_at": timely[0]["at"].isoformat()}
        if posts:
            return {"result": "late", "below_by": None, "alert_at": posts[0]["at"].isoformat()}
    elif posts:
        return {"result": "covered", "below_by": None}
    if not evals:
        return {"result": "never_scored", "below_by": None}
    scored = [row for row in evals if row.get("score_v2") is not None]
    if not scored:
        return {"result": "never_scored", "below_by": None,
                "v2_reason": evals[-1].get("v2_reason")}
    best = max(scored, key=lambda row: row["score_v2"])
    gap = best.get("below_by")
    if gap is None and best.get("post_bar") is not None:
        gap = round(max(0.0, best["post_bar"] - best["score_v2"]), 2)
    return {"result": "below_bar", "below_by": gap, "score_v2": best["score_v2"], "path": best.get("path")}


def true_win(quotes: list[tuple[datetime, float]], entry_ask: float) -> bool | None:
    """+40% before -35%, stop checked first on each quote. None if unresolved."""
    if entry_ask <= 0:
        return None
    up, down = entry_ask * 1.40, entry_ask * 0.65
    for _at, bid in quotes:
        if bid <= down:
            return False
        if bid >= up:
            return True
    return None


def runner_doubled(quotes: list[tuple[datetime, float]], entry_ask: float) -> bool:
    """Bid reached +200% at any point. Used only as a coverage label."""
    if entry_ask <= 0:
        return False
    return any(bid >= entry_ask * 3 for _at, bid in quotes)


def thin_candidate(snap: Snapshot, option: Option, settings: Settings, setup: Setup | None) -> Candidate:
    """Priced contract with no option-flow window. The option score stays light."""
    spread = (option.ask - option.bid) / option.ask if option.ask else 1
    liquidity = 5 * _clamp(1 - spread / max(settings.max_spread_fraction, 1e-6))
    return Candidate(snap, option, round(liquidity, 2), {"liquidity": round(liquidity, 2)},
                     {"cluster_size": 0, "thin_option": 1}, ["Thin option tape"],
                     False, setup=setup, blockers=("option feature window missing",))
