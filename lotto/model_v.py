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

Default posting (LOTTO_SCORE_MODE=s12) is two lanes on one priced contract:
    hybrid   v2 >= 40 (0DTE) or >= 61 (1DTE). v2 here is the option point score.
    handoff  fitted win chance >= the S12 or S11 floor for that strength band.
The handoff floors are P(true win). hoF >= A - delta is the same bar on the
log-odds scale (A is 58/80, delta is 200/100 on S12 and 150/75 on S11).

LOTTO_SCORE_MODE=combined is the rollback: one total, bars 40/61, stock capped
at stock_max_share, plus the rare path=stock_override card.

Runner names stay out of the quote batch until LOTTO_STANDBY_NAMES=on. Those
names take the handoff A bar (the normal floor) with no strength step-down.
"""
import json
from bisect import bisect_left
from dataclasses import replace
from datetime import datetime, timedelta
from math import exp, isfinite, log, log1p
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
MODEL_FEATURES = (
    "mo_atr", "mo_pct", "l_rvol", "pm_brk", "pd_brk", "newx5", "vwap_side",
    "eff10", "spread_c", "l_ask", "l_ovol5", "pe5", "cdte", "m",
)
_FILE_CACHE: dict = {}


def _finite(value) -> float | None:
    if isinstance(value, (int, float)) and isfinite(value):
        return float(value)
    return None


def _minute_index(bar) -> int | None:
    """Alert-bar index m. The 09:30-09:31 bar is 0."""
    local = bar.end.astimezone(ET)
    minute = (local.hour * 60 + local.minute) - (9 * 60 + 31)
    if 0 <= minute <= 389:
        return minute
    return None


def features_from_spec_raw(raw: dict) -> dict:
    """Turn one spec test-vector raw row into model features before median fill.

    pe5 is clipped to [-1, 3]. Missing breaks stay None so the fill can set them to 1.
    """
    def num(name):
        return _finite(raw.get(name))

    rvol = num("rvol")
    ask = num("ask")
    bid = num("bid")
    volume = num("ovol5")
    spread = num("spread")
    if spread is None and ask and bid is not None and ask > 0:
        spread = (ask - bid) / ask
    premium = num("prem_exp5")
    if premium is None:
        premium = num("pe5")
    return {
        "mo_atr": num("mo_atr"),
        "mo_pct": num("mo_pct"),
        "rvol": rvol,
        "l_rvol": None if rvol is None else log1p(max(rvol, 0.0)),
        "pm_brk": num("pm_brk"),
        "pd_brk": num("pd_brk"),
        "newx5": num("newx5"),
        "vwap_side": num("vwap_side"),
        "eff10": num("eff10"),
        "spread_c": None if spread is None else max(0.0, min(1.0, spread)),
        "l_ask": None if ask is None or ask < 0 else log1p(ask),
        "l_ovol5": None if volume is None else log1p(max(volume, 0.0)),
        "pe5": None if premium is None else max(-1.0, min(3.0, premium)),
        "cdte": num("cdte"),
        "m": num("m"),
    }


def fill_model_features(raw: dict, medians: dict) -> dict:
    """Apply Vince's fill order. Missing breaks are 1. The VWAP term is not used."""
    def num(name):
        return _finite(raw.get(name))

    l_rvol = num("l_rvol")
    if l_rvol is None:
        rvol = num("rvol")
        l_rvol = log1p(max(rvol, 0.0)) if rvol is not None else 0.0
    filled = {
        "l_rvol": l_rvol,
        "l_ovol5": 0.0 if num("l_ovol5") is None else num("l_ovol5"),
        "l_ask": 0.0 if num("l_ask") is None else num("l_ask"),
        "pe5": 0.0 if num("pe5") is None else max(-1.0, min(3.0, num("pe5"))),
        "spread_c": 1.0 if num("spread_c") is None else max(0.0, min(1.0, num("spread_c"))),
    }
    for name in ("pm_brk", "pd_brk", "newx5"):
        value = num(name)
        filled[name] = 1.0 if value is None else value
    for name in ("mo_atr", "mo_pct", "vwap_side", "eff10", "cdte", "m"):
        value = num(name)
        fallback = medians.get(name, 0.0)
        filled[name] = float(fallback) if value is None else value
    return filled


def fill_strength_inputs(raw: dict, medians: dict) -> dict:
    """Same break fill as the model. RVOL is 0 when it was not observed."""
    def num(name):
        return _finite(raw.get(name))

    mo_atr = num("mo_atr")
    rvol = num("rvol")
    return {
        "mo_atr": float(medians.get("mo_atr", 0.5380223785056588)) if mo_atr is None else mo_atr,
        "rvol": 0.0 if rvol is None else rvol,
        "pm_brk": 1.0 if num("pm_brk") is None else num("pm_brk"),
        "pd_brk": 1.0 if num("pd_brk") is None else num("pd_brk"),
        "newx5": 1.0 if num("newx5") is None else num("newx5"),
    }


def _rank(sorted_values: list, value: float) -> float:
    """count(ref < value) / n. Matches numpy searchsorted(..., side='left')."""
    return bisect_left(sorted_values, value) / len(sorted_values)


def score_handoff(spec: dict, inputs: dict, rvol: float | None = None) -> dict:
    """Fitted win chance, hoF, and stock strength from the committed spec.

    The VWAP coefficient is left out. Its training std is ~0, so the term is 0
    and the code never divides by that std.
    """
    model = spec["handoff_model"]
    mean = model["standardize"]["mean"]
    scale = model["standardize"]["std_plus_1e9"]
    linear = float(model["intercept"])
    for name, weight in model["coef"].items():
        if name == "vwap_side":
            continue
        denom = scale.get(name)
        if not denom:
            continue
        linear += float(weight) * ((float(inputs[name]) - float(mean[name])) / float(denom))
    linear = max(-30.0, min(30.0, linear))
    probability = 1.0 / (1.0 + exp(-linear))
    dte_key = "0DTE" if float(inputs["cdte"]) == 0 else "1DTE"
    mapping = model["v2_equiv_mapping"]["params"][dte_key]
    clipped = min(max(probability, 1e-15), 1 - 1e-15)
    ho_f = (log(clipped) - log(1 - clipped) - float(mapping["a"])) / float(mapping["b"])
    reference = spec["stock_strength"]["reference"]
    mo_atr = float(inputs["mo_atr"])
    ranked_rvol = 0.0 if rvol is None or not isfinite(rvol) else float(rvol)
    breaks = (float(inputs["pm_brk"]) + float(inputs["pd_brk"]) + float(inputs["newx5"])) / 3
    rank_mo = _rank(reference["mo_atr_sorted_exact"], mo_atr)
    rank_rvol = _rank(reference["rvol_sorted_exact"], ranked_rvol)
    strength = (rank_mo + rank_rvol + breaks) / 3
    return {
        "p_win": probability,
        "hoF": ho_f,
        "stock_strength": strength,
        "strength_parts": {"rank_mo_atr": rank_mo, "rank_rvol": rank_rvol, "breaks": breaks},
    }


def handoff_floor(strength: float | None, dte: int, settings: Settings, a_bar: bool = False) -> float:
    """Fitted-probability floor. Runner names stay on the normal (A) floor."""
    normal = settings.pwin_normal_0dte if dte == 0 else settings.pwin_normal_1dte
    if a_bar or strength is None:
        return normal
    if strength >= settings.strength_decile:
        return settings.pwin_decile if dte == 0 else settings.pwin_decile_1dte
    if strength >= settings.strength_quintile:
        return settings.pwin_quintile_0dte if dte == 0 else settings.pwin_quintile_1dte
    return normal


def strength_band(strength: float | None, settings: Settings, a_bar: bool = False) -> str | None:
    if a_bar:
        return "a_bar"
    if strength is None:
        return None
    if strength >= settings.strength_decile:
        return "decile"
    if strength >= settings.strength_quintile:
        return "quintile"
    return "normal"


def lane_decision(lane: str, dte: int, v2_score: float | None, p_win: float | None,
                  strength: float | None, settings: Settings, a_bar: bool = False) -> bool:
    """Hybrid posts on v2 points. Handoff posts on the fitted-probability floor."""
    dte = 0 if int(dte) == 0 else 1
    if lane == "hybrid_v2":
        bar = settings.post_v2_option_0dte if dte == 0 else settings.post_v2_option_1dte
        return v2_score is not None and v2_score >= bar
    if not settings.probability_path:
        return False
    floor = handoff_floor(strength, dte, settings, a_bar=a_bar)
    return p_win is not None and p_win >= floor


def _directional_slots(bars, side: int) -> tuple[list, list]:
    slots = {}
    for bar in bars:
        minute = _minute_index(bar)
        if minute is None:
            continue
        height = bar.high if side == 1 else -bar.low
        slots[minute] = (height, side * bar.close)
    if not slots:
        return [], []
    last = max(slots)
    heights = [None] * (last + 1)
    closes = [None] * (last + 1)
    prev_h = prev_x = None
    for minute in range(last + 1):
        if minute in slots:
            prev_h, prev_x = slots[minute]
        heights[minute] = prev_h
        closes[minute] = prev_x
    return heights, closes


def _break_flag(close: float, level: float | None, side: int) -> float | None:
    if level is None or not isfinite(level):
        return None
    return 1.0 if side * close > side * level else 0.0


def build_handoff_features(snap: Snapshot, option: Option, history: list[Snapshot], side: int) -> dict:
    """Raw handoff inputs. None marks a missing value for the fill step.

    RVOL is 0 until five prior full sessions exist. A missing premarket break,
    prior-day break, or new extreme stays None so the fill can set it to 1.
    """
    bars = list(snap.bars)
    raw = {name: None for name in MODEL_FEATURES}
    raw["rvol"] = 0.0
    if not bars:
        raw["cdte"] = 0.0 if dte_of(option, snap) == 0 else 1.0
        return raw
    close_m = bars[-1].close
    open0 = bars[0].open
    minute = _minute_index(bars[-1])
    atr = snap.prior_atr if snap.prior_atr and isfinite(snap.prior_atr) and snap.prior_atr > 0 else None
    if atr and open0:
        raw["mo_atr"] = (side * close_m - side * open0) / atr
    if open0:
        raw["mo_pct"] = side * (close_m / open0 - 1) * 100
    days = getattr(snap, "prior_full_days", None)
    base = getattr(snap, "rvol_base", None)
    if days is not None and days >= 5 and base and base > 0:
        raw["rvol"] = sum(bar.volume for bar in bars) / base
    else:
        raw["rvol"] = 0.0
    raw["pm_brk"] = _break_flag(close_m, snap.premarket_high if side == 1 else snap.premarket_low, side)
    raw["pd_brk"] = _break_flag(close_m, snap.prior_high if side == 1 else snap.prior_low, side)
    heights, closes = _directional_slots(bars, side)
    if minute is None or minute >= len(heights) or heights[minute] is None:
        raw["newx5"] = None
        raw["eff10"] = None
    elif minute < 5:
        raw["newx5"] = 0.0
    else:
        window = [value for value in heights[minute - 4:minute + 1] if value is not None]
        day = [value for value in heights[:minute + 1] if value is not None]
        prior = [value for value in heights[:minute - 4] if value is not None]
        if window and day and prior:
            raw["newx5"] = 1.0 if max(window) >= max(day) and max(window) > max(prior) else 0.0
        else:
            raw["newx5"] = 0.0
    if minute is not None and minute < len(closes) and closes[minute] is not None:
        k = min(10, minute)
        segment = closes[minute - k:minute + 1]
        if any(value is None for value in segment):
            segment = [value for value in segment if value is not None]
        if len(segment) < 2:
            raw["eff10"] = 0.0
        else:
            path = sum(abs(segment[i] - segment[i - 1]) for i in range(1, len(segment)))
            raw["eff10"] = 0.0 if path == 0 else max(segment[-1] - segment[0], 0.0) / path
    vwap = _vwap_at(bars, bars[-1].end)
    raw["vwap_side"] = None if vwap is None else (1.0 if side * close_m > side * vwap else 0.0)
    if option.ask and option.ask > 0 and isfinite(option.bid):
        raw["spread_c"] = max(0.0, min(1.0, (option.ask - option.bid) / option.ask))
        raw["l_ask"] = log1p(option.ask)
    volume_5 = _option_volume_5(option, history, snap)
    if volume_5 is not None:
        raw["l_ovol5"] = log1p(max(volume_5, 0.0))
    raw["pe5"] = _premium_change_5(option, history, snap)
    raw["cdte"] = 0.0 if dte_of(option, snap) == 0 else 1.0
    raw["m"] = float(minute if minute is not None else 0)
    return raw


def _option_volume_5(option: Option, history: list[Snapshot], snap: Snapshot) -> float | None:
    cutoff = snap.at - timedelta(minutes=5)
    prior = None
    for past in history:
        if past.at > cutoff:
            continue
        match = next((item for item in past.options if item.symbol == option.symbol), None)
        if match is not None:
            prior = match.volume
    if prior is None:
        return None
    return max(0.0, option.volume - prior)


def _premium_change_5(option: Option, history: list[Snapshot], snap: Snapshot) -> float | None:
    if not option.ask or option.ask <= 0:
        return None
    mid = (option.ask + option.bid) / 2
    cutoff = snap.at - timedelta(minutes=5)
    previous = None
    for past in history:
        if past.at > cutoff:
            continue
        match = next((item for item in past.options if item.symbol == option.symbol and item.ask > 0), None)
        if match is not None:
            previous = match
    if previous is None:
        return None
    old = (previous.ask + previous.bid) / 2
    if old <= 0:
        return None
    return max(-1.0, min(3.0, mid / old - 1))


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
    """Stock strength from the spec's sorted ranks, or a legacy reference file."""
    if not isinstance(distribution, dict):
        return None, "stock_strength: reference distribution not loaded"
    if "stock_strength" in distribution:
        medians = distribution.get("feature_definitions", {}).get("missing_values", {}).get("median", {})
        filled = fill_strength_inputs(features, medians)
        try:
            scored = score_handoff(distribution, {
                "mo_atr": filled["mo_atr"], "cdte": 0, "m": 0, "mo_pct": 0, "l_rvol": 0,
                "pm_brk": filled["pm_brk"], "pd_brk": filled["pd_brk"], "newx5": filled["newx5"],
                "vwap_side": 1, "eff10": 0, "spread_c": 0, "l_ask": 0, "l_ovol5": 0, "pe5": 0,
            }, rvol=filled["rvol"])
        except (KeyError, TypeError, ValueError) as exc:
            return None, f"stock_strength: {exc}"
        return scored["stock_strength"], scored["strength_parts"]
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
    """Fitted P(true win) from the committed handoff spec, or a legacy coefficient file."""
    if not isinstance(model, dict):
        return None, "handoff model not loaded"
    if "handoff_model" in model:
        medians = model.get("feature_definitions", {}).get("missing_values", {}).get("median", {})
        filled = fill_model_features(features, medians)
        try:
            scored = score_handoff(model, filled, rvol=features.get("rvol"))
        except (KeyError, TypeError, ValueError) as exc:
            return None, f"handoff model: {exc}"
        return scored["p_win"], None
    if "coefficients" not in model:
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
    return probability, reason


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
             index_bars: dict | None = None, handoff: bool = False, thin: bool = False,
             a_bar: bool = False) -> Candidate:
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
    dte = 0 if dte_of(option, snap) == 0 else 1
    mode = getattr(settings, "score_mode", "s12")
    if mode == "s12":
        if stock_raw >= settings.stock_push_score:
            scale = settings.stock_push_thin_weight if thin else settings.stock_push_weight
            stock_v2 = min(settings.stock_push_cap, stock_raw * scale)
        else:
            stock_v2 = stock_raw * settings.stock_weak_scale
        stock_cap = settings.stock_push_cap
    else:
        stock_v2, stock_cap, score_v2 = cap_stock(option_v2, stock_raw, settings)
    raw_features = build_handoff_features(snap, option, history, direction)
    spec_path = settings.handoff_model_path or settings.strength_distribution_path
    spec, spec_reason = _load_json(spec_path, "handoff spec")
    probability = ho_f = strength = None
    strength_detail = None
    probability_reason = spec_reason
    strength_reason = spec_reason
    if isinstance(spec, dict) and "handoff_model" in spec:
        medians = spec.get("feature_definitions", {}).get("missing_values", {}).get("median", {})
        filled = fill_model_features(raw_features, medians)
        strength_inputs = fill_strength_inputs(raw_features, medians)
        # Model and strength share the break fill and the ATR median.
        filled["mo_atr"] = strength_inputs["mo_atr"]
        filled["pm_brk"] = strength_inputs["pm_brk"]
        filled["pd_brk"] = strength_inputs["pd_brk"]
        filled["newx5"] = strength_inputs["newx5"]
        try:
            scored = score_handoff(spec, filled, rvol=strength_inputs["rvol"])
        except (KeyError, TypeError, ValueError) as exc:
            probability_reason = f"handoff model: {exc}"
            strength_reason = probability_reason
        else:
            probability = scored["p_win"]
            ho_f = scored["hoF"]
            strength = scored["stock_strength"]
            strength_detail = scored["strength_parts"]
            probability_reason = None
            strength_reason = None
    else:
        legacy = {
            "move_from_open_atr": metrics.get("move_from_open_atr") or 0,
            "rvol": flags.get("rvol_used") or 0,
            "held_pm": flags.get("held_pm") or 0,
            "held_pd": flags.get("held_pd") or 0,
            "held_range": flags.get("held_range") or 0,
        }
        strength, strength_detail = stock_strength(legacy, spec)
        if strength is None and isinstance(strength_detail, str):
            strength_reason = strength_detail
        probability, probability_reason = fitted_win_probability(legacy, spec)
    option_bar = post_bar("option", dte, settings)
    band, floor = None, None
    if mode == "combined":
        path = "combined"
        bar = post_bar("combined", dte, settings)
        qualifying = option_v2 >= bar or score_v2 >= bar
        probability_post = 0
    else:
        # The posted hybrid number is the option v2. Stock does not add to that bar.
        score_v2 = round(option_v2, 2)
        hybrid_pass = option_v2 >= option_bar
        handoff_pass = False
        if handoff and settings.probability_path:
            floor = handoff_floor(strength, dte, settings, a_bar=a_bar)
            band = strength_band(strength, settings, a_bar=a_bar)
            handoff_pass = probability is not None and probability >= floor
        elif handoff:
            floor = post_bar("stock", dte, settings)
            band = "v2"
            handoff_pass = (option_v2 + stock_v2) >= floor
        if hybrid_pass:
            path = "hybrid"
            bar = option_bar
            qualifying = True
        elif handoff:
            path = "handoff"
            bar = floor if floor is not None else post_bar("stock", dte, settings)
            qualifying = handoff_pass
        else:
            path = "hybrid"
            bar = option_bar
            qualifying = False
        probability_post = int(bool(handoff_pass and not hybrid_pass))
    if path == "handoff" and settings.probability_path and probability is not None:
        below = round(max(0.0, float(bar) - probability), 4)
    elif mode == "s12":
        below = round(max(0.0, float(bar) - option_v2), 2)
    else:
        below = round(max(0.0, float(bar) - score_v2), 2)
    parts = dict(candidate.parts)
    parts.update(extras)
    parts.update(penalties)
    parts.update({f"stock_{name}": value for name, value in terms.items()})
    parts["hourly_break"] = hourly
    parts["stock_v2"] = round(stock_v2, 2)
    parts["stock_cap"] = stock_cap
    override = int(mode == "combined" and exceptional_stock(metrics, flags, snap, settings, candidate.setup, handoff))
    metrics.update({
        "model": "V", "rules": "B" if dte == 0 else "F",
        "score_mode": mode, "preset": settings.preset,
        "score_v2": score_v2, "option_v2": round(option_v2, 2),
        "stock_v2": round(stock_v2, 2), "stock_raw": round(stock_raw, 2),
        "stock_cap": stock_cap,
        "path": path, "thin_option": int(bool(thin or handoff and not metrics.get("cluster_size"))),
        "post_bar": bar, "handoff": int(handoff),
        "a_bar": int(bool(a_bar)),
        "level_retest": terms["level_retest"],
        "below_by": below,
        "option_below_by": round(max(0.0, option_bar - option_v2), 2),
        "stock_strength": strength,
        "stock_strength_ranks": strength_detail if isinstance(strength_detail, dict) else None,
        "strength_reason": strength_reason,
        "fitted_win_probability": probability,
        "handoff_v2eq": None if ho_f is None else ho_f,
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
