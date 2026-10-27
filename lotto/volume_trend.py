"""Volume-trend profiles. Same weights in all three; only the post line differs: 75, 102, and 120.

SPY_ALIGN_TURN_3M is 15. Its window is SPY_ALIGN_TURN_WINDOW_MIN = 3.5.
3.5 minutes is 210 seconds. The reference close is the linear interpolate
between the two 1-minute bar ends that bracket that timestamp. A whole-minute
window such as 3 is the same function with no interpolate. The row scores
when SPY's return over that window, in the card's direction, is inside
SPY_ALIGN_TURN_MIN/MAX (0.05% to 0.12%).

SPY_ALIGN_RALLY_DAY is 18 at 60–75% breadth, 18 at 75–90%, and 0 at 90% or more.
Puts use the share of names that are red. SECTOR_ETF_PCT_TS is 16 when the
chosen sector ETF's percent from the open is 1.5–2.5x its 20-day same-minute
typical move. That ETF's SECTOR_ETF_WEIGHT (1.0, or 0.5 for the low group)
multiplies every SECTOR_* row. SECTOR_PEER_COUNT is 33 and pays when exactly
3 peers move with the card. SPY_ALIGN_OPEN is 1.

The fitted volume rows are 3 to 11 points: RVOL_CUM_15_25 is 7, RVOL_CUM_ge25
is 11, BU_5_TOD_10 is 4, and the break-bar BRK_TOD_BRK rows are 4, 3, and 4.
VOL_* keys are aliases at weight 0. STACKS stays configured at 0 points.
QQQ, SPY, and IWM have no sector ETF, so their sector rows are 0. SPY rows
still score. The GICS map is the sector source. A peer with no bars counts as
not moving. RF_NEWHI_VOL stays 0. The 15-minute repeat skip is off. The
5-minute same-contract dedupe stays.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from datetime import datetime, timedelta
from math import exp, erf, isfinite, log, sqrt
from pathlib import Path

from .models import ET

LOG = logging.getLogger(__name__)

# Discord spam safety. Not a quality filter. Do not turn this off here.
KEEP_SAME_CONTRACT_DEDUPE = True

PROFILE_ALIAS = {"volume_trend": "volume_trend_balanced"}

# Thresholds. Every SPY_ALIGN_* and SECTOR_* gate reads from this block.
VOLUME_TREND_RULES = {
    "quote_max_age_seconds": 75,
    "strike_count": 40,
    "itm_count": 3,
    "per_side": 20,
    "roll_itm_strikes": 1,
    "delta_cap": 0.70,
    "delta_target": 0.40,
    "delta_target_lo": 0.35,
    "delta_target_hi": 0.45,
    "delta_band_lo": 0.25,
    "delta_band_hi": 0.60,
    "delta_cheap_lo": 0.20,
    "delta_cheap_hi": 0.25,
    "delta_cheap_ask": 0.50,
    "rate": 0.04,
    "dividend": 0.0,
    "rvol_hot": 1.5,
    "rvol_dead_lo": 1.0,
    "rvol_dead_hi": 1.5,
    "brk_window_bars": 15,
    "brk_prior_bars": 10,
    "brk_min_ratio": 1.5,
    "bu_pre_bars": 5,
    "bu_min_ratio": 1.0,
    "push_bars": 30,
    "push_vol_min": 0.6,
    "push_rvol_cap": 3.0,
    "sector_l1_peers_2": 2,
    "sector_l1_peers_3p": 3,
    "sector_l2_min_peers": 2,
    "sector_l3_min_peers": 2,
    "sector_etf_band_lo": 1.0,
    "sector_etf_band_hi": 2.0,
    "sector_peer_count_min": 2,
    "sector_peer_exact": 3,
    "sector_vs_spy_min_pct": 0.0,
    "sector_pm_range_call_min": 0.70,
    "sector_pm_range_put_max": 0.30,
    "sector_session_minutes": 390,
    # SPY_ALIGN_TURN_3M reads this. 3.5 is 210 seconds, interpolated.
    # Set it to 3 for a whole-minute lookback with no interpolation.
    "SPY_ALIGN_TURN_WINDOW_MIN": 3.5,
    "SPY_ALIGN_TURN_5M_WINDOW_MIN": 5.0,
    "SPY_ALIGN_TURN_10M_WINDOW_MIN": 10.0,
    "SPY_ALIGN_TURN_MIN": 0.05,
    "SPY_ALIGN_TURN_MAX": 0.12,
    # Breadth of sector-map names. [lo, hi) maps to points. >= 0.90 is 0.
    "SPY_ALIGN_RALLY_TIERS": (
        {"lo": 0.60, "hi": 0.75, "points": 18},
        {"lo": 0.75, "hi": 0.90, "points": 18},
    ),
    "SECTOR_ETF_TS_MIN": 1.5,
    "SECTOR_ETF_TS_MAX": 2.5,
    "SECTOR_ETF_TS_DAYS": 20,
    "DAY_EXTEND_UNDER": 10,
    "ratio_score_full": 2.0,
    "BRK_BAND_LO": 1.2,
    "BRK_BAND_HI": 3.0,
    "BRK_CLIMAX": 4.0,
    "BRK_CLIMAX_ZERO": 8.0,
    "RANGE_ATR_LO": 0.05,
    "RANGE_ATR_HI": 0.35,
    "RANGE_ATR_ZERO": 1.0,
    "CLOSE_LOC_TOP": 0.70,
    "ROOM_ATR_MAX": 0.70,
    "ROOM_ATR_ZERO": 1.5,
    "SUST_THRESH": 1.2,
    "SUST_W_1M": 5,
    "SUST_W_5M": 3,
    "SUST_SCORE_FROM": 0.60,
    "SUST_CONSECUTIVE": 0,
    "BU_SLOPE_FULL": 1.0,
    "HOLD_W": 5,
    "HOLD_ATR_BUFFER": 0.05,
    "HOLD_VOL_MIN": 1.0,
    "HOLD_CLOSE_FRAC": 0.80,
    "PB_DRY_MAX": 1.0,
    "PB_RESUME_MIN": 1.3,
    "OPT_ADJ_VOL_MIN": 100,
    "OPT_ADJ_RADIUS": 2,
    "OPT_STEADY_CYCLES": 3,
    "STACK_OPT_MIN_STRIKES": 3,
    "STACK_BONUS": 0,
    "STACK_BONUS_3OF4": 0,
    "STACK_BRK_TOD_MIN": 1.5,
    "STACK_BRK_VS_MIN": 1.5,
    "RVOL_CUM_LO": 1.5,
    "RVOL_CUM_HI": 2.5,
    "BU_5_TOD_10_MIN": 1.6,
    "BRK_TOD_BRK_LO": 0.8,
    "BRK_TOD_BRK_MID": 1.2,
    "BRK_TOD_BRK_HI": 3.0,
    "ALT_BRK_VS_DAY_MIN": 1.2,
    "ALT_SUST_SHARE": 0.89,
    "ALT_CLOSE_LOC": 0.9,
    "ALT_RANGE_LO": 0.05,
    "ALT_RANGE_HI": 0.2,
    "ALT_HOLD_MIN": 1.0,
    "SECTOR_ETF_WEIGHT_LOW": 0.5,
    # Score rows only. Never a gate. Points must be <= 0.
    # Spread is (ask-bid)/mid. Edges match spread_bands: <=25% is 0,
    # <=35% is -10, <=40% is -12, above 40% is -15. A band's points are
    # halved when that band's strong-stock bar is met.
    "PENALTY_SPREAD_FREE": 0.25,
    "PENALTY_SPREAD_BANDS": (
        {"hi": 0.25, "points": 0},
        {"hi": 0.35, "points": -10},
        {"hi": 0.40, "points": -12},
        {"hi": None, "points": -15},
    ),
    # 5-minute contract volume under this floor. 0 when the strong-stock
    # bar that waives option volume is met. A missing print scores 0.
    "PENALTY_OPT_VOL_THIN": 50,
    "PENALTY_OPT_VOL_POINTS": -8,
    "SECTOR_ETF_WEIGHT": {
        "XLK": 1.0, "SMH": 1.0, "SOXX": 1.0, "MTUM": 1.0, "XLC": 1.0,
        "XLY": 1.0, "XOP": 1.0, "XLE": 1.0, "XLF": 1.0, "XLV": 1.0,
    },
    "SECTOR_ETF_WEIGHT_LOW_NAMES": ("XLI", "XLU", "XLB", "XLP", "XLRE"),
    "BASKET_LEADER_TOPN": 3,
    # Empty until a tier list is dropped in. A tier replaces that flag's flat weight.
    "DIVERGENCE_TIERS": {},
    "DIV_WRONG_SIDE_MINUTES": 10,
    "DIV_OR_WINDOWS": (15, 30),
    "rf_min_minutes": 5,
    "rf_vol_ratio": 1.3,
    "rf_prior_bars": 5,
}

# Primary book. Identical in the three profiles. Negative rows are penalties.
PRIMARY_WEIGHTS = {
    "sp5": 20,
    "flow200": 10,
    "RVOL_CHAIN_ge1.5": 6,
    "RVOL_CHAIN_1_15": -9,
    "rvol_06_08": -9,
    "BRK_VS_10_ge1.5": 15,
    # Backtested book is 0. All three volume_trend profiles keep 0 so this
    # row does not double-count BU_5_TOD_10. The live JSON profiles do not
    # carry the row. The flag is still computed.
    "BU_5_DAY_ge1": 0,
    "TB_ENTRY": 0,
    "TB_HOLD": 0,
    "TB_CONT": 0,
    "TB_PUT_RETRACE": 0,
    # Held on held-out days. Test-informed. Provisional until the stack pass.
    # RVOL_CUM chain, 10-day first: [1.5, 2.5) = 7, >=2.5 = 11. 2.5 is the upper tier.
    "RVOL_CUM_15_25": 7,
    "RVOL_CUM_ge25": 11,
    # Five bars before a fresh break vs the 10-day TOD norm. >=1.6 = 4.
    "BU_5_TOD_10": 4,
    # Break bar vs the 10-day TOD norm smoothed ±2. Fresh active break only.
    # [0.8, 1.2) = 4, [1.2, 3) = 3, >=3 = 4.
    "BRK_TOD_BRK_08_12": 4,
    "BRK_TOD_BRK_12_3": 3,
    "BRK_TOD_BRK_ge3": 4,
    # Reversed on held-out days. Live weight 0. Fitted alternates are VOLUME_ALTERNATES.
    # BRK_VS_DAY >=1.2 = 10. SUST share >=0.89 = 20. CLOSE_LOC >=0.9 = 6.
    # RANGE_ATR 0.05–0.2 = 6. HOLD_ON_VOL W5 >=1.0 = 4.
    "BRK_VS_DAY": 0,
    "BU_5_DAY": 0,
    "BU_3_DAY": 0,
    "BRK_TOD_10": 0,
    "BRK_TOD_5": 0,
    "BRK_TOD_3": 0,
    "BRK_BAND": 0,
    "RVOL_CUM": 0,
    "RVOL_CUM_10": 0,
    "RVOL_CUM_5": 0,
    "RVOL_CUM_3": 0,
    "RVOL_CUM_TIMESCALE": 0,
    "SUST_VOL_TOD_1M": 0,
    "SUST_VOL_TOD_5M": 0,
    "BU_3_TOD_10": 0,
    "BU_5_TOD_5": 0,
    "BU_3_TOD_5": 0,
    "BU_5_TOD_3": 0,
    "BU_3_TOD_3": 0,
    "BU_SLOPE_5": 0,
    "BU_SLOPE_3": 0,
    "RANGE_ATR": 0,
    "CLOSE_LOC": 0,
    "ROOM": 0,
    "HOLD_ON_VOL": 0,
    "PB_RESUME": 0,
    "OPT_VOL_ADJ": 8,
    "OPT_VOL_STEADY": 6,
    "VOL_DAYREL": 0,
    "VOL_RVOL_TOD": 0,
    "VOL_CUM": 0,
    "VOL_SUSTAINED": 0,
    "VOL_PRE_BREAK": 0,
    "VOL_BREAK_BAR": 0,
    "VOL_HOLD": 0,
    "VOL_DAY": 0,
    "VOL_BRK": 0,
    "VOL_PRE": 0,
    "VOL_5": 0,
    "VOL_15": 0,
    "RS_SPY": 0,
    "RS_SECTOR": 0,
    "UPPER_RANGE_30": 0,
    "RF_NEWHI_VOL": 0,
    # L2 and the plain ETF band are alternates (weight 0).
    # SECTOR_PEER_COUNT pays 33 when exactly 3 peers move with the card.
    "SECTOR_L1_PEERS_2": 0,
    "SECTOR_L1_PEERS_3P": 0,
    "SECTOR_L2_PMH": 0,
    "SECTOR_L3_PDH": 0,
    "SECTOR_ETF_PCT": 0,
    "SECTOR_ETF_PCT_TS": 16,
    "SECTOR_PM_RANGE_POS": 0,
    "SECTOR_VS_SPY": 0,
    "SECTOR_PEER_COUNT": 33,
    "BASKET_PCT_TS": 0,
    "BASKET_SHARE_GREEN": 0,
    "BASKET_SHARE_ABOVE_PMH": 0,
    "BASKET_RANK": 0,
    "BASKET_REL": 0,
    "BASKET_LEADER_RANK": 0,
    # Calls pay 40. The _PUT key is set to 0 so puts do not inherit 40.
    # The other two flags stay 0 on both sides. Tiers live in DIVERGENCE_TIERS.
    "DIV_ETF_WRONG_SIDE_10": 40,
    "DIV_ETF_WRONG_SIDE_10_PUT": 0,
    "DIV_STOCK_ORB_ETF_NOT": 0,
    "DIV_STOCK_ORB_ETF_NOT_PUT": 0,
    "DIV_ETF_OR30_AGAINST": 0,
    "DIV_ETF_OR30_AGAINST_PUT": 0,
    "SPY_ALIGN_OPEN": 1,
    "SPY_ALIGN_TURN_3M": 15,
    "SPY_ALIGN_TURN_5M": 0,
    "SPY_ALIGN_TURN_10M": 0,
    # Tier points (18 / 18 / 0) are added on the primary card. The weight stays 0.
    "SPY_ALIGN_RALLY_DAY": 0,
    # Band points are applied in score_volume_trend. These stay 0 so the
    # weight book cannot add them a second time.
    "PENALTY_SPREAD": 0,
    "PENALTY_OPT_VOL": 0,
    "level_break": 0,
    "a_flushpop": 0,
    "a_pmx_reclaim": 0,
    "a_vwap_slap": 0,
    "a_wm": 0,
    "drp_510_dip_reclaim": 0,
    "rvol15": 0,
    "ema9_2m_dip_reclaim": 0,
    "ema21_pullin_2m": 0,
    "ema21_pullin_5m": 0,
    "ema21_pullin_10m": 0,
    "ema50_touch": 0,
    "e2_ema9_5m_pullin": 0,
    "THESIS_CARRY": 0,
}

# room_used_le_1atr: demoting it to 16, 8, or 0 was tested and rejected.
# That cut cost about 3.5–4 pp of held-out win. The 32 stays configurable.
BYPASS_WEIGHTS = {
    "PUSH_VOL_B": 40,
    "room_used_le_1atr": 32,
    "push30_ge_0.75atr": 13,
    "THESIS_CARRY": 0,
}


def _cutoff(post: float) -> dict:
    return {
        "post_threshold": post,
        "bypass_threshold": 40,
        "min_positive_features": 0,
        "strong_feature_min": 10,
        "primary": copy.deepcopy(PRIMARY_WEIGHTS),
        "bypass": copy.deepcopy(BYPASS_WEIGHTS),
    }


# Post lines: aggressive 75, balanced 102, conservative 120.
# Do not put these profiles in the JSON book.
PROFILES = {
    "volume_trend_aggressive": _cutoff(75),
    "volume_trend_balanced": _cutoff(102),
    "volume_trend_conservative": _cutoff(120),
}

# Fitted points that reversed on held-out days. The live weight above is 0.
# Flip the weight to `points` to try the alternate. The feature is already 1
# when the condition holds.
VOLUME_ALTERNATES = {
    "BRK_VS_DAY": {"min": 1.2, "points": 10},
    "SUST_VOL_TOD_1M": {"min_share": 0.89, "points": 20},
    "CLOSE_LOC": {"min": 0.9, "points": 6},
    "RANGE_ATR": {"lo": 0.05, "hi": 0.2, "points": 6},
    "HOLD_ON_VOL": {"min": 1.0, "points": 4, "bars": 5},
}

# VOL_* names kept so an older note still points at Ace's key.
# A name may point at several live rows. The alias weight stays 0.
VOL_ALIASES = {
    "VOL_DAYREL": ("BRK_VS_DAY",),
    "VOL_RVOL_TOD": ("RVOL_CUM_15_25", "RVOL_CUM_ge25"),
    "VOL_CUM": ("RVOL_CUM_TIMESCALE",),
    "VOL_SUSTAINED": ("SUST_VOL_TOD_1M",),
    "VOL_PRE_BREAK": ("BU_5_TOD_10",),
    "VOL_BREAK_BAR": ("BRK_TOD_BRK_08_12", "BRK_TOD_BRK_12_3", "BRK_TOD_BRK_ge3"),
    "VOL_HOLD": ("HOLD_ON_VOL",),
}


def alias_rows(spec) -> tuple:
    """Rows an alias names. A string is one row."""
    if isinstance(spec, str):
        return (spec,)
    return tuple(spec)


def _raw_number(raw, key):
    try:
        number = float((raw or {}).get(key))
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def vol_bin_flags(raw, rules=None) -> dict:
    """Binary volume rows. A missing ratio is 0, not the continuous score.

    BU_5_TOD_10 is 1 when the 5-bar pre-break ratio is at least BU_5_TOD_10_MIN.
    The break-bar flags read BRK_TOD_10, the fresh-bar ratio versus the 10-day TOD.
    """
    rules = rules or VOLUME_TREND_RULES
    rvol = _raw_number(raw, "RVOL_CUM")
    lo = float(rules["RVOL_CUM_LO"])
    hi = float(rules["RVOL_CUM_HI"])
    bu = _raw_number(raw, "BU_5_TOD_10")
    brk = _raw_number(raw, "BRK_TOD_10")
    band_lo = float(rules["BRK_TOD_BRK_LO"])
    band_mid = float(rules["BRK_TOD_BRK_MID"])
    band_hi = float(rules["BRK_TOD_BRK_HI"])
    return {
        "RVOL_CUM_15_25": 1.0 if rvol is not None and lo <= rvol < hi else 0.0,
        "RVOL_CUM_ge25": 1.0 if rvol is not None and rvol >= hi else 0.0,
        "BU_5_TOD_10": 1.0 if bu is not None and bu >= float(rules["BU_5_TOD_10_MIN"]) else 0.0,
        "BRK_TOD_BRK_08_12": 1.0 if brk is not None and band_lo <= brk < band_mid else 0.0,
        "BRK_TOD_BRK_12_3": 1.0 if brk is not None and band_mid <= brk < band_hi else 0.0,
        "BRK_TOD_BRK_ge3": 1.0 if brk is not None and brk >= band_hi else 0.0,
    }

UNIVERSE_CUT = frozenset({
    "DIA", "USO", "SLV", "ETHA", "TQQQ",
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    "SMH", "SOXX", "MTUM", "XOP",
    "MCD", "SBUX", "LOW", "UNH", "REGN", "ISRG", "LMT", "DE",
    "C", "SCHW", "MA", "AXP", "TGT", "COST", "CMG",
})

STANDBY_NAMES = (
    "IREN", "CRWV", "LITE", "TTMI", "HUT", "CORZ", "DKNG", "RIVN", "LUNR", "HIMS",
    "MARA", "SOXL", "GME", "EWZ", "TLT", "RIOT", "XLI", "TEM", "STX", "IBIT", "U",
)
DEEP_PRIORITY = ("ORCL", "CRWV", "SMCI")

ALIGN_ROWS = (
    "SPY_ALIGN_OPEN",
    "SPY_ALIGN_TURN_3M",
    "SPY_ALIGN_TURN_5M",
    "SPY_ALIGN_TURN_10M",
    "SPY_ALIGN_RALLY_DAY",
    "SECTOR_L1_PEERS_2",
    "SECTOR_L1_PEERS_3P",
    "SECTOR_L2_PMH",
    "SECTOR_L3_PDH",
    "SECTOR_ETF_PCT",
    "SECTOR_ETF_PCT_TS",
    "SECTOR_PM_RANGE_POS",
    "SECTOR_VS_SPY",
    "SECTOR_PEER_COUNT",
)


def canonical_profile(name) -> str:
    raw = str(name or "").strip().lower()
    return PROFILE_ALIAS.get(raw, raw)


def is_volume_trend(name) -> bool:
    return canonical_profile(name) in PROFILES


def volume_trend_lines(name) -> dict | None:
    raw = str(name or "").strip().lower()
    block = PROFILES.get(PROFILE_ALIAS.get(raw, raw))
    if block is None:
        return None
    return {
        "profile": raw,
        "post_threshold": float(block["post_threshold"]),
        "bypass_threshold": float(block["bypass_threshold"]),
        "min_positive_features": int(block["min_positive_features"]),
        "strong_feature_min": int(block["strong_feature_min"]),
    }


def drop_repeat_skip(profile) -> bool:
    """True when the 15-minute same-name skip is off. The 5-minute dedupe stays."""
    return is_volume_trend(profile)


def _spec(points: float) -> dict:
    return {"call": points, "put": points}


DIVERGENCE_FLAGS = (
    "DIV_ETF_WRONG_SIDE_10",
    "DIV_STOCK_ORB_ETF_NOT",
    "DIV_ETF_OR30_AGAINST",
)


def divergence_weight_spec(key: str, primary: dict) -> dict:
    """Call weight, and the put weight. A missing KEY_PUT uses the call weight."""
    call = float((primary or {}).get(key) or 0)
    put_key = f"{key}_PUT"
    if put_key in (primary or {}):
        put = float(primary[put_key])
    else:
        put = call
    return {"call": call, "put": put}


def _put_override_key(key: str) -> bool:
    return key.endswith("_PUT") and key[:-4] in DIVERGENCE_FLAGS


def overlay_book(book: dict, name: str = "volume_trend_balanced") -> dict:
    """Copy of the v4c book with this profile's numbers. Does not mutate the cache."""
    block = PROFILES.get(canonical_profile(name)) or PROFILES["volume_trend_balanced"]
    book = copy.deepcopy(book or {})
    weights = book.setdefault("weights", {})
    penalties = book.setdefault("penalties", {})
    bypass = book.setdefault("bypass_weights", {})
    for key, points in block["primary"].items():
        if _put_override_key(key):
            continue
        target = penalties if points < 0 else weights
        other = weights if target is penalties else penalties
        other.pop(key, None)
        if key in DIVERGENCE_FLAGS:
            target[key] = divergence_weight_spec(key, block["primary"])
        else:
            target[key] = _spec(points)
    for key, points in block["bypass"].items():
        bypass[key] = _spec(points)
    for spec in weights.values():
        if isinstance(spec, dict) and spec.get("requires_min_other_positive"):
            spec["requires_min_other_positive"] = 0
    return book


def _condition_holds(features: dict, condition: dict) -> bool:
    """Missing or non-numeric features are false. They are not an error."""
    try:
        value = (features or {}).get(condition.get("feature"))
        number = float(value)
    except (TypeError, ValueError):
        return False
    if not isfinite(number):
        return False
    op = str(condition.get("op") or ">=")
    try:
        threshold = float(condition.get("threshold"))
    except (TypeError, ValueError):
        return False
    if op == ">=":
        return number >= threshold
    if op == ">":
        return number > threshold
    if op == "<=":
        return number <= threshold
    if op == "<":
        return number < threshold
    if op == "==":
        return number == threshold
    if op == "!=":
        return number != threshold
    return False


def stacks_from_rules(rules=None) -> list:
    """The A+ stack and the exactly-3-of-4 partial. Append more entries to STACKS."""
    rules = rules or VOLUME_TREND_RULES
    conditions = [
        {"feature": "OPT_VOL_ADJ_STRIKES", "op": ">=", "threshold": float(rules["STACK_OPT_MIN_STRIKES"])},
        {"feature": "LEVEL_BREAK", "op": ">=", "threshold": 1},
        {"feature": "SECTOR_HIT", "op": ">=", "threshold": 1},
        {"feature": "BRK_VOL_PDH_R5", "op": ">=", "threshold": 1},
    ]
    return [
        {"name": "STACK_APLUS", "points": float(rules["STACK_BONUS"]), "conditions": conditions},
        {
            "name": "STACK_BONUS_3OF4",
            "points": float(rules["STACK_BONUS_3OF4"]),
            "conditions": conditions,
            "min_true": 3,
            "max_true": 3,
        },
    ]


# Edit this list. Every entry whose conditions hold adds its points.
STACKS = stacks_from_rules()


def evaluate_stacks(features: dict, stacks=None) -> tuple:
    """Sum of points for entries that qualify. Additive. Never blocking."""
    total = 0.0
    fired = []
    for entry in stacks if stacks is not None else STACKS:
        conditions = list(entry.get("conditions") or [])
        hits = sum(1 for condition in conditions if _condition_holds(features, condition))
        if "min_true" in entry or "max_true" in entry:
            need = int(entry.get("min_true", len(conditions)))
            cap = int(entry.get("max_true", len(conditions)))
            ok = need <= hits <= cap
        else:
            ok = bool(conditions) and hits == len(conditions)
        if ok:
            points = float(entry.get("points") or 0)
            total += points
            fired.append(entry.get("name"))
    return total, fired


def stack_view(features: dict) -> dict:
    """Raw ratios win over the 0-1 scores so a condition can say BRK_TOD_10 >= 1.5."""
    view = {}
    for key, value in (features or {}).items():
        if str(key).startswith("_") or str(key).endswith("_RAW"):
            continue
        view[key] = value
    raw = (features or {}).get("_vt_raw") or {}
    if isinstance(raw, dict):
        view.update(raw)
    return view


def score_volume_trend(features: dict, side: str, book: dict, path: str = "primary",
                       name: str = "volume_trend_balanced", parts=None, settings=None):
    """Primary score plus rally tiers and stacks. Bypass does not take those points.

    Sector and SPY weights live on the primary book only.
    """
    from .weights import score_path
    raw_features = dict(features or {})
    scored = overlay_book(book, name)
    points, score, positive = score_path(raw_features, side, scored, path)
    fired = []
    if path == "primary":
        tier = float(raw_features.get("_rally_points") or 0)
        if tier:
            points["SPY_ALIGN_RALLY_DAY"] = tier
            score += tier
            positive += 1
        stack_points, fired = evaluate_stacks(stack_view(raw_features))
        if stack_points:
            points["STACKS"] = stack_points
            score += stack_points
            positive += 1
        score, positive = _scale_sector_points(points, score, positive, raw_features)
        score, positive = _apply_divergence_tiers(points, score, positive, raw_features)
    score = _apply_penalties(points, score, raw_features, parts=parts, settings=settings)
    return points, score, positive, fired


def _scale_sector_points(points, score, positive, features):
    """Multiply every SECTOR_* point by the chosen ETF's SECTOR_ETF_WEIGHT."""
    try:
        mult = float(features.get("_sector_etf_weight", 1.0))
    except (TypeError, ValueError):
        mult = 1.0
    if not isfinite(mult) or mult == 1.0:
        return score, positive
    for key in list(points):
        if not str(key).startswith("SECTOR_"):
            continue
        scaled = points[key] * mult
        score += scaled - points[key]
        if points[key] > 0 and scaled <= 0:
            positive -= 1
        points[key] = scaled
    return score, positive


def _load_gics():
    path = Path(__file__).resolve().parents[1] / "config" / "gics_etf_map.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        LOG.warning("GICS ETF map unreadable (%s)", type(exc).__name__)
        return {}


GICS_MAP = _load_gics()
GICS_TICKERS = {str(k).upper(): v for k, v in (GICS_MAP.get("tickers") or {}).items() if isinstance(v, dict)}
GICS_ETFS = tuple(GICS_MAP.get("etfs") or (
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    "SMH", "SOXX", "MTUM", "XOP",
))
# Indexes have no sector ETF. Sector rows stay 0. SPY alignment rows still score.
NO_SECTOR_ETF = frozenset(
    str(item).upper() for item in (GICS_MAP.get("no_sector_etf") or ("SPY", "QQQ", "IWM", "SPX"))
)

# Theme baskets sit on top of the 15 ETFs. A name may be in more than one.
# Slow groups stay empty until Vince sends the 2–3 names. Row weights are 0.
SECTOR_BASKETS = (
    {"name": "MEMORY", "tickers": ("MU", "SNDK", "WDC", "STX"), "weight": 1.0},
    {"name": "OPTICAL", "tickers": ("AAOI", "LITE", "COHR", "GLW", "ANET"), "weight": 1.0},
    {"name": "DC_AI_INFRA", "tickers": ("NBIS", "CRWV", "DELL", "SMCI", "ANET", "HPE"), "weight": 1.0},
    {"name": "AI_POWER", "tickers": ("BE", "GEV", "VST", "CEG", "OKLO"), "weight": 1.0},
    {"name": "SEMIS_CORE", "tickers": ("NVDA", "AMD", "AVGO", "TSM", "ARM", "QCOM", "MRVL", "INTC", "AMAT"), "weight": 1.0},
    {"name": "CYBER", "tickers": ("PANW", "CRWD", "ZS", "NET", "S", "OKTA"), "weight": 1.0},
    {"name": "SOFTWARE", "tickers": ("NOW", "CRM", "DDOG", "PLTR", "SNOW"), "weight": 1.0},
    {"name": "CRYPTO_FINTECH", "tickers": ("COIN", "HOOD", "MSTR", "MARA", "RIOT", "IREN"), "weight": 1.0},
    {"name": "MEGA", "tickers": ("AAPL", "MSFT", "META", "AMZN", "GOOGL", "TSLA"), "weight": 1.0},
    {"name": "FINANCIALS_SLOW", "tickers": (), "weight": 1.0},
    {"name": "INDUSTRIALS_SLOW", "tickers": (), "weight": 1.0},
    {"name": "UTILITIES_SLOW", "tickers": (), "weight": 1.0},
    {"name": "HOMEBUILDERS_SLOW", "tickers": (), "weight": 1.0},
)


def baskets_for(symbol: str) -> tuple:
    name = str(symbol or "").upper()
    return tuple(
        entry for entry in SECTOR_BASKETS
        if name in {str(item).upper() for item in entry.get("tickers") or ()}
    )


def ticker_row(symbol: str) -> dict:
    return GICS_TICKERS.get(str(symbol or "").upper()) or {}


def etf_multiplier(etf: str, rules=None) -> float:
    """Full names are 1.0. The low group shares SECTOR_ETF_WEIGHT_LOW."""
    rules = rules or VOLUME_TREND_RULES
    name = str(etf or "").upper()
    table = rules.get("SECTOR_ETF_WEIGHT") or {}
    if name in table:
        return float(table[name])
    low = {str(item).upper() for item in rules.get("SECTOR_ETF_WEIGHT_LOW_NAMES") or ()}
    if name in low:
        return float(rules.get("SECTOR_ETF_WEIGHT_LOW", 1.0))
    return 1.0


def sector_row_points(features: dict, etf: str, rules=None, weights=None) -> tuple:
    """SECTOR_* points after this ETF's multiplier. Other rows are not scaled."""
    rules = rules or VOLUME_TREND_RULES
    weights = weights or PRIMARY_WEIGHTS
    mult = etf_multiplier(etf, rules)
    total = 0.0
    scaled = {}
    for key, weight in weights.items():
        if not str(key).startswith("SECTOR_"):
            continue
        try:
            value = float((features or {}).get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if not isfinite(value):
            value = 0.0
        value = max(0.0, min(1.0, value))
        points = float(weight) * value * mult
        scaled[key] = points
        total += points
    return total, scaled, mult


def choose_sector_etf(bundles, primary: str | None = None, rules=None):
    """Pick the mapped ETF whose SECTOR_* points are higher after the multiplier.

    A tie stays with the primary. MTUM can be in the list. It does not win a tie.
    """
    rules = rules or VOLUME_TREND_RULES
    best = None
    for etf, feats in bundles or []:
        total, scaled, mult = sector_row_points(feats, etf, rules)
        prefer = 1 if primary and str(etf).upper() == str(primary).upper() else 0
        rank = (total, prefer)
        if best is None or rank > best[0]:
            best = (rank, str(etf).upper(), feats, mult, total, scaled)
    if best is None:
        return None
    return {"etf": best[1], "features": best[2], "weight": best[3], "total": best[4], "points": best[5]}


def candidate_etfs(symbol: str) -> tuple:
    name = str(symbol or "").upper()
    if name in NO_SECTOR_ETF:
        return ()
    row = ticker_row(symbol)
    out = []
    primary = row.get("primary")
    if primary:
        out.append(str(primary).upper())
    for item in row.get("secondary") or []:
        etf = str(item).upper()
        if etf not in out:
            out.append(etf)
    if "MTUM" not in out:
        out.append("MTUM")
    return tuple(out)


def peers_sharing(etf: str, include_reference: bool = False) -> tuple:
    """Names that list this ETF. MTUM is a reference and has no peer group."""
    etf = str(etf or "").upper()
    if etf == "MTUM":
        return ()
    names = []
    for ticker, row in GICS_TICKERS.items():
        if row.get("reference_only") and not include_reference:
            continue
        listed = [str(row.get("primary") or "").upper(), *[str(item).upper() for item in row.get("secondary") or []]]
        if etf in listed:
            names.append(ticker)
    return tuple(sorted(names))


def etf_for(symbol: str) -> str | None:
    if str(symbol or "").upper() in NO_SECTOR_ETF:
        return None
    primary = ticker_row(symbol).get("primary")
    return str(primary).upper() if primary else None


def secondary_etfs(symbol: str) -> tuple:
    return tuple(str(item).upper() for item in ticker_row(symbol).get("secondary") or [])


def peers_for(symbol: str) -> tuple:
    """Universe names that share this name's primary GICS ETF. The name itself is excluded."""
    etf = etf_for(symbol)
    if not etf:
        return ()
    me = str(symbol or "").upper()
    return tuple(name for name in peers_sharing(etf) if name != me)


def sector_quote_symbols() -> tuple:
    """The 15 ETFs are quotes only. They are not alert tickers."""
    return tuple(sorted({str(item).upper() for item in GICS_ETFS}))


def ensure_etf_typicals(client, now) -> None:
    """Baseline each sector ETF that is missing today's New York session.

    It reloads once per session day so the table always holds the last 20
    completed sessions. baseline() is the existing candle pull. It stores the
    table on the client. An ETF already baselined for that date is skipped.
    """
    if client is None or not hasattr(client, "baseline"):
        return
    day = now.astimezone(ET).date()
    done = getattr(client, "baselines", None) or {}
    for etf in sector_quote_symbols():
        if (etf, day) in done:
            continue
        try:
            client.baseline(etf, now)
        except Exception as exc:
            LOG.warning("ETF typical %s unavailable (%s)", etf, type(exc).__name__)


def group_of(symbol: str) -> str | None:
    """Primary GICS ETF. Kept so older calls still resolve a sector."""
    return etf_for(symbol)


def pct_from_open(open_, last):
    """Percent change from the open. None when the bar is missing. Not dollars."""
    try:
        opened = float(open_)
        price = float(last)
    except (TypeError, ValueError):
        return None
    if not isfinite(opened) or not isfinite(price) or opened <= 0:
        return None
    return (price / opened - 1.0) * 100.0


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _band(direction: int, pct, lo: float, hi: float) -> float:
    if pct is None:
        return 0.0
    signed = direction * float(pct)
    return 1.0 if lo <= signed <= hi else 0.0


def peer_rows(peers) -> list:
    """Normalize peer dicts. A missing open or last is not moving and does not raise."""
    rows = []
    for peer in peers or []:
        if not isinstance(peer, dict):
            continue
        rows.append({
            "symbol": str(peer.get("symbol") or ""),
            "pct": pct_from_open(peer.get("open"), peer.get("last")),
            "last": _finite(peer.get("last")),
            "pmh": _finite(peer.get("pmh")),
            "pml": _finite(peer.get("pml")),
            "pdh": _finite(peer.get("pdh")),
            "pdl": _finite(peer.get("pdl")),
        })
    return rows


def _same_direction(row, direction: int) -> bool:
    pct = row.get("pct")
    return pct is not None and direction * pct > 0


def _beyond_extreme(row, direction: int, high_key: str, low_key: str) -> bool:
    last = row.get("last")
    if last is None:
        return False
    if direction == 1:
        level = row.get(high_key)
        return level is not None and last > level
    level = row.get(low_key)
    return level is not None and last < level


def sector_map_names() -> tuple:
    """Mapped stocks. Reference-only GICS examples and the 15 ETFs are not in the count."""
    return tuple(sorted(
        ticker for ticker, row in GICS_TICKERS.items() if not row.get("reference_only")
    ))


def rally_breadth(direction: int, pct_by_symbol) -> float:
    """Fraction of sector-map names green (calls) or red (puts).

    The denominator is the whole map, so a missing quote counts as not on
    the card's side. It does not raise and it does not inflate the fraction.
    """
    names = sector_map_names()
    if not names:
        return 0.0
    direction = 1 if int(direction) >= 0 else -1
    table = pct_by_symbol or {}
    hit = 0
    for name in names:
        if isinstance(table, dict):
            pct = _finite(table.get(name))
        else:
            pct = None
        if pct is None:
            continue
        if direction * pct > 0:
            hit += 1
    return hit / len(names)


def _band_points(value, bands) -> float:
    """Points for the band that contains value. Positive points are clamped to 0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(number):
        return 0.0
    for band in bands or ():
        try:
            lo = float(band.get("lo"))
            pts = float(band.get("points") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        hi = band.get("hi")
        if hi is not None:
            try:
                hi = float(hi)
            except (TypeError, ValueError):
                continue
        if number + 1e-12 < lo:
            continue
        if hi is not None and number >= hi:
            continue
        if not isfinite(pts) or pts > 0:
            return 0.0
        return pts
    return 0.0


def _spread_penalty_raw(spread, rules) -> float:
    """First band whose upper edge holds the spread. The last band is open."""
    for band in rules.get("PENALTY_SPREAD_BANDS") or ():
        try:
            pts = float(band.get("points") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if not isfinite(pts) or pts > 0:
            pts = 0.0
        hi = band.get("hi")
        if hi is None:
            return pts
        try:
            hi = float(hi)
        except (TypeError, ValueError):
            continue
        if spread <= hi + 1e-12:
            return pts
    return 0.0


def _spread_bar_met(spread, parts, settings) -> bool:
    """The wide-spread stock bar for this band. Band 0 has no bar."""
    if settings is None:
        return False
    from .confluence import spread_band
    from .model_v import stock_bar_status
    band = spread_band(spread, settings)
    if band is None or int(band) <= 0:
        return False
    return bool(stock_bar_status(parts, band, settings)[0])


def _opt_vol_bar_met(spread, parts, settings) -> bool:
    """The stock bar that waives a thin option print. Missing parts do not waive."""
    if settings is None or not parts:
        return False
    from .confluence import spread_band
    from .model_v import optb_volume_waived
    band = None if spread is None else spread_band(spread, settings)
    return bool(optb_volume_waived(parts, band, settings)[0])


def penalty_spread_points(spread, rules=None, parts=None, settings=None) -> float:
    """Spread penalty. At or below 25% of mid the row is 0. Never a block.

    spread is (ask-bid)/mid. A missing spread scores 0. Points are halved
    when that band's strong-stock bar is met.
    """
    rules = rules or VOLUME_TREND_RULES
    try:
        number = float(spread)
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(number) or number < 0:
        return 0.0
    free = float(rules.get("PENALTY_SPREAD_FREE", 0.25))
    if number <= free + 1e-12:
        return 0.0
    points = _spread_penalty_raw(number, rules)
    if points < 0 and _spread_bar_met(number, parts, settings):
        return points / 2.0
    return points


def penalty_opt_vol_points(volume, rules=None, parts=None, settings=None, spread=None) -> float:
    """−8 when 5-minute contract volume is under 50. A missing print scores 0.

    The row is 0 when the strong-stock bar that waives option volume is met.
    It does not block the card.
    """
    rules = rules or VOLUME_TREND_RULES
    try:
        number = float(volume)
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(number) or number < 0:
        return 0.0
    try:
        thin = float(rules.get("PENALTY_OPT_VOL_THIN", 50))
        points = float(rules.get("PENALTY_OPT_VOL_POINTS", -8))
    except (TypeError, ValueError):
        return 0.0
    if number >= thin or not isfinite(points) or points > 0:
        return 0.0
    if _opt_vol_bar_met(spread, parts, settings):
        return 0.0
    return points


def _apply_penalties(points, score, features, rules=None, parts=None, settings=None) -> float:
    """Subtract the two penalty rows. They never add points and never block.

    The weight-book keys stay 0, so score_path does not add them again.
    Model V option_extras is not on this score.
    """
    rules = rules or VOLUME_TREND_RULES
    features = features or {}
    spread_value = features.get("entry_spread")
    volume_value = features.get("opt_volume_5m") if "opt_volume_5m" in features else None
    spread = penalty_spread_points(spread_value, rules, parts=parts, settings=settings)
    volume = penalty_opt_vol_points(
        volume_value, rules, parts=parts, settings=settings, spread=spread_value)
    points["PENALTY_SPREAD"] = spread
    points["PENALTY_OPT_VOL"] = volume
    return score + spread + volume


def rally_points(breadth, rules=None) -> float:
    """18 at 60–75%, 18 at 75–90%, 0 at 90% or above and below 60%."""
    rules = rules or VOLUME_TREND_RULES
    number = _finite(breadth)
    if number is None:
        return 0.0
    for tier in rules["SPY_ALIGN_RALLY_TIERS"]:
        if float(tier["lo"]) <= number < float(tier["hi"]):
            return float(tier["points"])
    return 0.0


def etf_typical_score(direction: int, etf_pct, typical_abs, rules=None) -> float:
    """1 when the ETF's % from the open is 1.5–2.5x the 20-day same-minute typical move.

    typical_abs is the median absolute percent-from-open at this minute
    over the prior 20 sessions (zero readings are skipped). Puts need a down
    move of the same multiple.
    A missing typical scores 0. It is not an error.
    """
    rules = rules or VOLUME_TREND_RULES
    pct = _finite(etf_pct)
    typical = _finite(typical_abs)
    if pct is None or typical is None or typical <= 0:
        return 0.0
    signed = direction * pct
    if signed <= 0:
        return 0.0
    multiple = signed / typical
    lo = float(rules["SECTOR_ETF_TS_MIN"])
    hi = float(rules["SECTOR_ETF_TS_MAX"])
    return 1.0 if lo <= multiple <= hi else 0.0


def _median(values):
    rows = sorted(float(value) for value in values)
    if not rows:
        return None
    mid = len(rows) // 2
    if len(rows) % 2:
        return rows[mid]
    return (rows[mid - 1] + rows[mid]) / 2.0


def etf_typical_table(day_curves, atr_pct=None, days=None) -> tuple:
    """Median |% from open| at each minute over the last 20 sessions.

    Fewer stored sessions use those days. No sessions use the ETF's ATR as a
    percent of price, scaled by the square root of the elapsed session.
    """
    days = int(days or VOLUME_TREND_RULES["SECTOR_ETF_TS_DAYS"])

    def curve_from(subset):
        out = {}
        for minute in range(390):
            values = []
            for item in subset:
                if minute not in item:
                    continue
                try:
                    number = float(item[minute])
                except (TypeError, ValueError):
                    continue
                if isfinite(number) and number > 0:
                    values.append(number)
            value = _median(values)
            if value is not None:
                out[minute] = value
        return out

    usable = [dict(curve) for curve in (day_curves or []) if curve]
    if len(usable) >= days:
        return curve_from(usable[-days:]), "20-day"
    if usable:
        return curve_from(usable), "available-days"
    try:
        atr = float(atr_pct)
    except (TypeError, ValueError):
        atr = None
    if atr is not None and isfinite(atr) and atr > 0:
        return {
            minute: atr * sqrt((minute + 1) / 390.0)
            for minute in range(390)
        }, "atr-scaled"
    return {}, "missing"


def alignment_features(*, direction: int, peers=None, etf_pct=None, spy_pct=None,
                       qqq_pct=None, spy_above_vwap=None, spy_bars=None,
                       name_pcts=None, minute=None, etf_last=None, etf_pmh=None,
                       etf_pml=None, etf_typical=None, pct_by_symbol=None,
                       rules=None, symbol: str = "") -> dict:
    """Every SECTOR_* and SPY_ALIGN_* value, calls and puts. Weights are separate."""
    rules = rules or VOLUME_TREND_RULES
    direction = 1 if int(direction) >= 0 else -1
    rows = peer_rows(peers)
    same = [row for row in rows if _same_direction(row, direction)]
    known = [row["pct"] for row in rows if row["pct"] is not None]
    net = sum(known) if known else 0.0
    LOG.info(
        "SECTOR %s direction=%s peers=%s same=%s net_pct=%.4f",
        symbol, direction, len(rows), len(same), net,
    )
    l2 = sum(1 for row in rows if _beyond_extreme(row, direction, "pmh", "pml"))
    l3 = sum(1 for row in rows if _beyond_extreme(row, direction, "pdh", "pdl"))
    count = len(same)
    breadth = rally_breadth(direction, pct_by_symbol if pct_by_symbol is not None else {})
    lo = float(rules["sector_etf_band_lo"])
    hi = float(rules["sector_etf_band_hi"])
    etf = _finite(etf_pct)
    out = {
        "SECTOR_L1_PEERS_2": 1.0 if count == int(rules["sector_l1_peers_2"]) else 0.0,
        "SECTOR_L1_PEERS_3P": 1.0 if count >= int(rules["sector_l1_peers_3p"]) else 0.0,
        "SECTOR_L2_PMH": 1.0 if l2 >= int(rules["sector_l2_min_peers"]) else 0.0,
        "SECTOR_L3_PDH": 1.0 if l3 >= int(rules["sector_l3_min_peers"]) else 0.0,
        "SECTOR_ETF_PCT": _band(direction, etf, lo, hi),
        "SECTOR_ETF_PCT_TS": etf_typical_score(direction, etf, etf_typical, rules),
        "SECTOR_PM_RANGE_POS": _pm_range(direction, etf_last, etf_pmh, etf_pml, rules),
        "SECTOR_VS_SPY": _sector_vs_spy(direction, etf, _finite(spy_pct), rules),
        "SECTOR_PEER_COUNT": 1.0 if count == int(rules["sector_peer_exact"]) else 0.0,
        "SPY_ALIGN_OPEN": _spy_open(direction, _finite(spy_pct), spy_above_vwap),
        "SPY_ALIGN_TURN_3M": spy_turn(spy_bars, direction, float(rules["SPY_ALIGN_TURN_WINDOW_MIN"]), rules),
        "SPY_ALIGN_TURN_5M": spy_turn(spy_bars, direction, float(rules["SPY_ALIGN_TURN_5M_WINDOW_MIN"]), rules),
        "SPY_ALIGN_TURN_10M": spy_turn(spy_bars, direction, float(rules["SPY_ALIGN_TURN_10M_WINDOW_MIN"]), rules),
        "SPY_ALIGN_RALLY_DAY": 1.0 if rally_points(breadth, rules) > 0 else 0.0,
        "_sector_l1_count": float(count),
        "_sector_l1_net_pct": float(net),
        "_sector_l2_count": float(l2),
        "_rally_breadth": breadth,
        "_rally_points": rally_points(breadth, rules),
        "_etf_ts_pace": 0.0,
    }
    del qqq_pct, minute, name_pcts
    return out


def _etf_time_scaled(direction, etf_pct, minute, lo, hi, rules) -> float:
    """Full-session pace: percent from the open divided by the fraction of the day elapsed.

    The band is the same +1% to +2% (puts −1% to −2%) applied to that pace.
    Minute 0 has no pace and scores 0.
    """
    if etf_pct is None or minute is None:
        return 0.0
    try:
        minute = float(minute)
    except (TypeError, ValueError):
        return 0.0
    session = float(rules.get("sector_session_minutes") or 390)
    if minute <= 0 or session <= 0:
        return 0.0
    pace = float(etf_pct) / (minute / session)
    return _band(direction, pace, lo, hi)


def _pm_range(direction, last, pm_high, pm_low, rules) -> float:
    last, pm_high, pm_low = _finite(last), _finite(pm_high), _finite(pm_low)
    if last is None or pm_high is None or pm_low is None or pm_high <= pm_low:
        return 0.0
    pos = (last - pm_low) / (pm_high - pm_low)
    if direction == 1:
        return 1.0 if pos >= float(rules["sector_pm_range_call_min"]) else 0.0
    return 1.0 if pos <= float(rules["sector_pm_range_put_max"]) else 0.0


def _sector_vs_spy(direction, etf_pct, spy_pct, rules) -> float:
    if etf_pct is None or spy_pct is None:
        return 0.0
    edge = float(rules["sector_vs_spy_min_pct"])
    return 1.0 if direction * (float(etf_pct) - float(spy_pct)) >= edge else 0.0


def _spy_open(direction, spy_pct, above_vwap) -> float:
    """Calls: SPY above its open and above VWAP. Puts: below its open and below VWAP."""
    if spy_pct is None or above_vwap is None:
        return 0.0
    try:
        above = float(above_vwap)
    except (TypeError, ValueError):
        return 0.0
    if direction == 1:
        return 1.0 if spy_pct > 0 and above > 0 else 0.0
    return 1.0 if spy_pct < 0 and above < 0 else 0.0


def _bars_through(bars, card_time):
    """Completed bars whose end is at or before the card. Later bars are ignored."""
    rows = []
    for bar in bars or []:
        end = getattr(bar, "end", None)
        if card_time is not None and end is not None and end > card_time:
            continue
        rows.append(bar)
    return rows


def _session_open(bars):
    """Open of the 9:30 bar. None when that bar is not in the list."""
    for bar in bars or []:
        if _minute_index(bar) == 0:
            opened = _finite(getattr(bar, "open", None))
            if opened is not None and opened > 0:
                return opened
    return None


def _wrong_side_minutes(bars, direction: int) -> int:
    """Consecutive minutes ending now on the wrong side of the session open.

    Calls: close below the open. Puts: close above the open. A missing minute
    ends the streak. The count is 0 when the session open bar is absent.
    """
    rows = [bar for bar in bars or [] if _minute_index(bar) is not None]
    opened = _session_open(rows)
    if opened is None or not rows:
        return 0
    streak = 0
    previous = None
    for bar in reversed(rows):
        minute = _minute_index(bar)
        if previous is not None and minute != previous - 1:
            break
        close = _finite(getattr(bar, "close", None))
        if close is None:
            break
        wrong = close < opened if direction == 1 else close > opened
        if not wrong:
            break
        streak += 1
        previous = minute
    return streak


def _or_window(bars, minutes: int):
    """The first `minutes` session bars, or None when that window is incomplete."""
    window = []
    for bar in bars or []:
        minute = _minute_index(bar)
        if minute is None or minute >= minutes:
            continue
        window.append(bar)
    if len(window) < minutes:
        return None
    return window


def _broke_or(bars, minutes: int, direction: int) -> bool:
    """A later bar traded through the opening-range high (calls) or low (puts)."""
    window = _or_window(bars, minutes)
    if not window:
        return False
    if direction == 1:
        extreme = max(bar.high for bar in window)
        return any(
            _minute_index(bar) is not None and _minute_index(bar) >= minutes and bar.high > extreme
            for bar in bars
        )
    extreme = min(bar.low for bar in window)
    return any(
        _minute_index(bar) is not None and _minute_index(bar) >= minutes and bar.low < extreme
        for bar in bars
    )


def _etf_bars_for(snap, etf: str):
    """Minute bars already on the snap for this primary ETF. No new request."""
    if not etf:
        return ()
    table = getattr(snap, "vt_etf_bars", None) or {}
    if isinstance(table, dict) and etf in table:
        return table[etf] or ()
    return ()


def divergence_sector(*, symbol: str, stock_bars, etf_bars, direction: int,
                      spy_pct, spy_above_vwap, card_time=None, rules=None) -> dict:
    """DIVERGENCE_SECTOR flags. The ETF is choose_sector_etf's primary.

    QQQ, SPY, IWM, and SPX have no sector ETF, so every flag is 0. Each flag
    also requires SPY_ALIGN_OPEN (SPY on the card's side of its open and of
    VWAP). Puts mirror. Bars after card_time are not read.
    """
    rules = rules or VOLUME_TREND_RULES
    flags = {name: 0.0 for name in DIVERGENCE_FLAGS}
    etf = etf_for(symbol)
    flags["_div_etf"] = etf or ""
    flags["_div_wrong_side_minutes"] = 0.0
    if not etf:
        return flags
    direction = 1 if int(direction) >= 0 else -1
    stock = _bars_through(stock_bars, card_time)
    sector = _bars_through(etf_bars, card_time)
    flags["_div_wrong_side_minutes"] = float(_wrong_side_minutes(sector, direction))
    if _spy_open(direction, _finite(spy_pct), spy_above_vwap) < 1:
        return flags
    need = int(rules.get("DIV_WRONG_SIDE_MINUTES") or 10)
    if flags["_div_wrong_side_minutes"] >= need:
        flags["DIV_ETF_WRONG_SIDE_10"] = 1.0
    windows = tuple(rules.get("DIV_OR_WINDOWS") or (15, 30))
    for window in windows:
        # Both ranges have to be visible. A missing ETF window is not a break.
        if _or_window(stock, int(window)) is None or _or_window(sector, int(window)) is None:
            continue
        if _broke_or(stock, int(window), direction) and not _broke_or(sector, int(window), direction):
            flags["DIV_STOCK_ORB_ETF_NOT"] = 1.0
            break
    against = -direction
    if _broke_or(sector, 30, against):
        flags["DIV_ETF_OR30_AGAINST"] = 1.0
    return flags


def _divergence_tier_points(flag: str, features: dict, rules=None):
    """Points from DIVERGENCE_TIERS, or None when that flag has no tiers."""
    rules = rules or VOLUME_TREND_RULES
    tiers = (rules.get("DIVERGENCE_TIERS") or {}).get(flag) or ()
    if not tiers:
        return None
    for tier in tiers:
        feature = tier.get("feature") or flag
        value = _finite((features or {}).get(feature))
        if value is None:
            continue
        lo = tier.get("lo")
        hi = tier.get("hi")
        if lo is not None and value < float(lo):
            continue
        if hi is not None and value >= float(hi):
            continue
        try:
            points = float(tier.get("points") or 0)
        except (TypeError, ValueError):
            return 0.0
        return points if isfinite(points) else 0.0
    return 0.0


def _apply_divergence_tiers(points, score, positive, features, rules=None):
    """A configured tier replaces the flat weight. An empty map changes nothing."""
    for flag in DIVERGENCE_FLAGS:
        tier = _divergence_tier_points(flag, features, rules)
        if tier is None:
            continue
        previous = float(points.get(flag) or 0)
        score += tier - previous
        if previous > 0:
            positive -= 1
        if tier > 0:
            positive += 1
        points[flag] = tier
    return score, positive


def _bar_end_close(bars):
    rows = []
    if not bars:
        return rows
    if isinstance(bars[0], (int, float)) and not isinstance(bars[0], bool):
        origin = datetime(2026, 1, 2, 14, 31, tzinfo=ET)
        for index, close in enumerate(bars):
            close = _finite(close)
            if close is None:
                continue
            rows.append((origin + timedelta(minutes=index), close))
        return rows
    for bar in bars:
        end = getattr(bar, "end", None)
        close = _finite(getattr(bar, "close", None))
        if end is None or close is None:
            continue
        rows.append((end, close))
    rows.sort(key=lambda item: item[0])
    return rows


def reference_close(bars, window_min: float):
    """Close `window_min` minutes before the last bar.

    Whole minutes land on a bar end. A fraction (3.5 -> 210 seconds) is the
    linear interpolate between the two 1-minute closes whose ends bracket
    that timestamp. Missing history returns None.
    """
    series = _bar_end_close(bars)
    if not series:
        return None
    try:
        window_min = float(window_min)
    except (TypeError, ValueError):
        return None
    if window_min < 0:
        return None
    last_end = series[-1][0]
    if window_min == 0:
        return series[-1][1]
    target = last_end - timedelta(seconds=window_min * 60.0)
    older = None
    newer = None
    for end, close in series:
        if end <= target:
            older = (end, close)
        elif newer is None:
            newer = (end, close)
            break
    if older is None:
        return None
    if older[0] == target or newer is None:
        return older[1] if older[0] == target else None
    span = (newer[0] - older[0]).total_seconds()
    if span <= 0:
        return None
    frac = (target - older[0]).total_seconds() / span
    return older[1] + frac * (newer[1] - older[1])


def spy_window_return(bars, direction: int, window_min: float):
    """Percent return over the window, signed so the card's direction is positive.

    `window_min` comes from config. 3.5 is 210 seconds. reference_close
    interpolates between the 1-minute bars that bracket that timestamp.
    """
    direction = 1 if int(direction) >= 0 else -1
    now = reference_close(bars, 0)
    ref = reference_close(bars, window_min)
    if now is None or ref is None or ref == 0:
        return None
    return direction * (now / ref - 1.0) * 100.0


def spy_turn(bars, direction: int, window_min: float, rules=None) -> float:
    """1 when SPY's directional return over the window is inside the config band.

    The band is SPY_ALIGN_TURN_MIN to SPY_ALIGN_TURN_MAX (0.05% to 0.12%).
    Puts score on a SPY decline of that size. Outside the band scores 0.
    """
    rules = rules or VOLUME_TREND_RULES
    ret = spy_window_return(bars, direction, window_min)
    if ret is None:
        return 0.0
    return 1.0 if float(rules["SPY_ALIGN_TURN_MIN"]) <= ret <= float(rules["SPY_ALIGN_TURN_MAX"]) else 0.0


def rally_day(direction: int, spy_pct, qqq_pct, name_pcts, rules=None) -> float:
    """Calls: SPY and QQQ up, and enough of the universe green. Puts mirror.

    Thresholds are SPY_ALIGN_RALLY_PCT (percent from the open) and
    SPY_ALIGN_RALLY_BREADTH (fraction green for calls, red for puts).
    A name with no bars is left out of the fraction. It is not an error.
    """
    rules = rules or VOLUME_TREND_RULES
    direction = 1 if int(direction) >= 0 else -1
    spy_pct, qqq_pct = _finite(spy_pct), _finite(qqq_pct)
    if spy_pct is None or qqq_pct is None:
        return 0.0
    need = float(rules["SPY_ALIGN_RALLY_PCT"])
    if direction * spy_pct < need or direction * qqq_pct < need:
        return 0.0
    known = []
    for value in name_pcts or []:
        number = _finite(value)
        if number is not None:
            known.append(number)
    if not known:
        return 0.0
    if direction == 1:
        hit = sum(1 for value in known if value > 0)
    else:
        hit = sum(1 for value in known if value < 0)
    breadth = hit / len(known)
    return 1.0 if breadth + 1e-12 >= float(rules["SPY_ALIGN_RALLY_BREADTH"]) else 0.0


def _match(ratio) -> float:
    if ratio is None:
        return 0.0
    number = _finite(ratio)
    if number is None:
        return 0.0
    return max(0.0, min(1.0, (number - 0.5)))


def _mean(values) -> float | None:
    values = [float(value) for value in values]
    if not values:
        return None
    return sum(values) / len(values)


def rvol_chain(snap, bars) -> tuple[float, str]:
    """10 -> 5 -> 3 -> any days (up to 10) -> bar vs day-so-far -> 1.0.

    The number is never missing. A neutral 1.0 is the last step, not a blank.
    """
    rows = list(bars or [])
    cum = sum(int(getattr(bar, "volume", 0) or 0) for bar in rows)

    def from_base(base, sessions, need, source):
        if sessions is None:
            return None
        try:
            sessions = int(sessions)
            base = float(base)
        except (TypeError, ValueError):
            return None
        if sessions < need or not isfinite(base):
            return None
        if base == 0:
            return 0.0, source
        if base < 0 or not rows:
            return None
        return cum / base, source

    days = getattr(snap, "prior_full_days", None)
    base = getattr(snap, "rvol_base", None)
    hit = from_base(base, days, 10, "RVOL_CUM_10")
    if hit is None:
        hit = from_base(
            getattr(snap, "fallback_rvol_base", None),
            getattr(snap, "fallback_rvol_sessions", None),
            10, "RVOL_CUM_10")
    if hit is not None:
        return hit
    for sessions_attr, base_attr, need, source in (
        ("vt_rvol_sessions_5", "vt_rvol_base_5", 5, "RVOL_CUM_5"),
        ("vt_rvol_sessions_3", "vt_rvol_base_3", 3, "RVOL_CUM_3"),
        ("vt_rvol_sessions_any", "vt_rvol_base_any", 1, "RVOL_ANY"),
    ):
        hit = from_base(getattr(snap, base_attr, None), getattr(snap, sessions_attr, None), need, source)
        if hit is not None:
            return hit
    if len(rows) >= 2:
        average = _mean([bar.volume for bar in rows[:-1]])
        if average and average > 0:
            return rows[-1].volume / average, "BAR_VS_DAY"
    return 1.0, "NEUTRAL"


def _fresh_break(bars, levels, direction: int, window: int):
    rows = list(bars or [])
    usable = [level for level in levels if _finite(level) is not None]
    if len(rows) < 2 or not usable:
        return None
    start = max(1, len(rows) - int(window))
    found = None
    for index in range(start, len(rows)):
        prev = rows[index - 1].close
        close = rows[index].close
        for level in usable:
            level = float(level)
            crossed = direction * (close - level) > 0 and direction * (prev - level) <= 0
            held = direction * (rows[-1].close - level) > 0
            if crossed and held:
                found = (index, level)
                break
    return found


def _levels_for(snap, direction: int) -> list:
    if direction == 1:
        names = ("premarket_high", "prior_high", "high_5d", "high_md", "week_high")
    else:
        names = ("premarket_low", "prior_low", "low_5d", "low_md", "week_low")
    return [getattr(snap, name, None) for name in names]


def break_features(bars, direction: int, snap, rules=None) -> dict:
    rules = rules or VOLUME_TREND_RULES
    found = _fresh_break(
        bars, _levels_for(snap, direction), direction, int(rules["brk_window_bars"]))
    brk_ratio = bu_ratio = None
    if found is not None:
        index, _level = found
        prior_n = int(rules["brk_prior_bars"])
        pre_n = int(rules["bu_pre_bars"])
        if index >= prior_n:
            prior = _mean([bars[j].volume for j in range(index - prior_n, index)])
            if prior and prior > 0:
                brk_ratio = bars[index].volume / prior
        if index >= pre_n:
            pre = _mean([bars[j].volume for j in range(index - pre_n, index)])
            day = _mean([bars[j].volume for j in range(index)])
            if pre is not None and day and day > 0:
                bu_ratio = pre / day
    hot = float(rules["brk_min_ratio"])
    built = float(rules["bu_min_ratio"])
    return {
        "BRK_VS_10_ge1.5": 1.0 if brk_ratio is not None and brk_ratio >= hot else 0.0,
        "BU_5_DAY_ge1": 1.0 if bu_ratio is not None and bu_ratio >= built else 0.0,
        "VOL_BRK": _match(brk_ratio if brk_ratio is not None else _bar_vs_day(bars)),
        "VOL_PRE": _match(bu_ratio if bu_ratio is not None else 1.0),
        "_brk_ratio": brk_ratio,
        "_break_index": None if found is None else found[0],
    }


def _bar_vs_day(bars) -> float:
    rows = list(bars or [])
    if len(rows) < 2:
        return 1.0
    average = _mean([bar.volume for bar in rows[:-1]])
    if not average or average <= 0:
        return 1.0
    return rows[-1].volume / average


def _window_ratio(bars, size: int) -> float:
    rows = list(bars or [])
    if len(rows) < size * 2:
        return 1.0
    recent = sum(bar.volume for bar in rows[-size:])
    chunks = []
    end = len(rows) - size
    while end >= size and len(chunks) < 5:
        chunks.append(sum(bar.volume for bar in rows[end - size:end]))
        end -= size
    average = _mean(chunks)
    if not average or average <= 0:
        return 1.0
    return recent / average


def push_vol_b(bars, direction: int, atr, rvol, rules=None) -> float:
    """1 when a fresh 30-bar push times capped RVOL clears 0.6 and the bar is a new extreme."""
    rules = rules or VOLUME_TREND_RULES
    rows = list(bars or [])
    atr = _finite(atr)
    rvol = _finite(rvol)
    if len(rows) < 2 or atr is None or atr <= 0 or rvol is None:
        return 0.0
    span = int(rules["push_bars"])
    window = rows[-(span + 1):]
    close = rows[-1].close
    if direction == 1:
        push = (close - min(bar.low for bar in window)) / atr
        prior = rows[:-1][-span:]
        extreme = bool(prior) and rows[-1].high > max(bar.high for bar in prior)
    else:
        push = (max(bar.high for bar in window) - close) / atr
        prior = rows[:-1][-span:]
        extreme = bool(prior) and rows[-1].low < min(bar.low for bar in prior)
    if not extreme:
        return 0.0
    product = push * min(rvol, float(rules["push_rvol_cap"]))
    return 1.0 if product >= float(rules["push_vol_min"]) else 0.0


def _vwap_side(bars, direction_unused=0):
    del direction_unused
    rows = [bar for bar in (bars or []) if getattr(bar, "volume", 0)]
    if not rows:
        return None
    volume = 0.0
    total = 0.0
    for bar in rows:
        typical = (bar.high + bar.low + bar.close) / 3.0
        volume += bar.volume
        total += typical * bar.volume
    if volume <= 0:
        return None
    vwap = total / volume
    close = rows[-1].close
    if close > vwap:
        return 1.0
    if close < vwap:
        return -1.0
    return 0.0


def _spy_view(snap, spy_bars):
    pct = getattr(snap, "vt_spy_pct", None)
    qqq = getattr(snap, "vt_qqq_pct", None)
    above = getattr(snap, "vt_spy_vwap_side", None)
    if spy_bars:
        opened = getattr(spy_bars[0], "open", None)
        last = getattr(spy_bars[-1], "close", None)
        computed = pct_from_open(opened, last)
        if computed is not None:
            pct = computed
        side = _vwap_side(spy_bars)
        if side is not None:
            above = side
    if pct is None and getattr(snap, "market_label", "") == "SPY":
        ret = getattr(snap, "market_return_from_open", None)
        if ret is not None and _finite(ret) is not None:
            pct = float(ret) * 100.0
        if above is None:
            above = getattr(snap, "market_vwap_side", None)
    return pct, qqq, above


def rf_newhi_vol(bars, side: str, prior, now, rules=None) -> float:
    """1 on a new high (low for puts) with 1.3x the prior 5-bar volume, at least 5 minutes later.

    The weight is 0 until a fit says otherwise. The rule stays in the book.
    """
    rules = rules or VOLUME_TREND_RULES
    rows = list(bars or [])
    if not prior or not rows or now is None:
        return 0.0
    ts = prior.get("ts")
    if ts is None:
        return 0.0
    if (now - ts).total_seconds() < float(rules["rf_min_minutes"]) * 60.0:
        return 0.0
    if side == "PUT":
        if rows[-1].low >= float(prior.get("low", rows[-1].low)):
            return 0.0
    else:
        if rows[-1].high <= float(prior.get("high", rows[-1].high)):
            return 0.0
    width = int(rules["rf_prior_bars"])
    if len(rows) < width + 1:
        return 0.0
    baseline = _mean([bar.volume for bar in rows[-(width + 1):-1]])
    if not baseline or baseline <= 0:
        return 0.0
    if rows[-1].volume < float(rules["rf_vol_ratio"]) * baseline:
        return 0.0
    return 1.0


def _minute_index(bar):
    if bar is None or getattr(bar, "end", None) is None:
        return None
    local = bar.end.astimezone(ET)
    minute = (local.hour * 60 + local.minute) - (9 * 60 + 31)
    if 0 <= minute <= 389:
        return minute
    return None


def _tb_features(bars, direction, atr, found_index, rules) -> dict:
    rows = list(bars or [])
    if found_index is None or not rows:
        return {"TB_ENTRY": 0.0, "TB_HOLD": 0.0, "TB_CONT": 0.0, "TB_PUT_RETRACE": 0.0, "UPPER_RANGE_30": _upper(rows, direction)}
    level = None
    # Hold uses the close relative to the break bar's close side of the prior close.
    terms = 0
    bar = rows[found_index]
    span = bar.high - bar.low
    if span > 0:
        loc = (bar.close - bar.low) / span if direction == 1 else (bar.high - bar.close) / span
        if loc >= 0.60:
            terms += 1
    atr = _finite(atr)
    if atr and atr > 0 and span > 0 and 0.05 <= span / atr <= 0.30:
        terms += 1
    if atr and atr > 0:
        ext = direction * (rows[-1].close - rows[0].open) / atr
        if ext <= 0.25:
            terms += 1
    entry = min(1.0, 0.2 + 0.16 * terms)
    after = rows[found_index + 1:found_index + 11]
    hold = 0.0
    if len(after) >= 10:
        # Still on the trade side of the break bar's open.
        anchor = rows[found_index].open
        held = sum(1 for item in after if direction * (item.close - anchor) > 0)
        hold = 1.0 if held / 10.0 >= 0.8 else 0.0
    minute = _minute_index(rows[-1])
    cont = 0.0
    if minute is not None and minute >= 30 and atr and atr > 0 and len(rows) >= 4:
        move = direction * (rows[-1].close - rows[0].open) / atr
        if direction == 1:
            stairs = rows[-1].low > rows[-2].low > rows[-3].low
        else:
            stairs = rows[-1].high < rows[-2].high < rows[-3].high
        if move > 1.0 and stairs:
            cont = 1.0
    retrace = 0.0
    if minute is not None and 45 <= minute <= 150 and len(rows) >= 6:
        # A light mirror: the last three closes stepped back while still on the trade side of the open.
        stepped = direction * (rows[-1].close - rows[-3].close) < 0
        with_open = direction * (rows[-1].close - rows[0].open) > 0
        if stepped and with_open:
            retrace = 1.0
    return {
        "TB_ENTRY": entry,
        "TB_HOLD": hold,
        "TB_CONT": cont,
        "TB_PUT_RETRACE": retrace if direction == -1 else (retrace if direction == 1 else 0.0),
        "UPPER_RANGE_30": _upper(rows, direction),
    }


def _upper(bars, direction: int) -> float:
    rows = list(bars or [])
    if len(rows) < 2:
        return 0.0
    window = rows[-30:]
    high = max(bar.high for bar in rows)
    low = min(bar.low for bar in rows)
    span = high - low
    if span <= 0:
        return 0.0
    if direction == 1:
        line = low + 0.7 * span
        return sum(1 for bar in window if bar.close >= line) / len(window)
    line = high - 0.7 * span
    return sum(1 for bar in window if bar.close <= line) / len(window)


def sector_feature_choice(snap, direction, spy_bars, rules=None) -> dict:
    """Score each mapped ETF and keep the higher total after SECTOR_ETF_WEIGHT.

    QQQ, SPY, and IWM get SPY rows only. Their SECTOR_* values are 0.
    A snap with no per-ETF tape keeps the single primary ETF already on it.
    """
    rules = rules or VOLUME_TREND_RULES
    symbol = getattr(snap, "symbol", "")
    bars = list(getattr(snap, "bars", None) or [])
    minute = _minute_index(bars[-1]) if bars else None
    spy_pct, qqq_pct, above = _spy_view(snap, spy_bars)
    common = dict(
        direction=direction,
        spy_pct=spy_pct,
        qqq_pct=qqq_pct,
        spy_above_vwap=above,
        spy_bars=spy_bars,
        name_pcts=getattr(snap, "vt_name_pcts", None),
        minute=minute,
        pct_by_symbol=getattr(snap, "vt_pct_by_symbol", None),
        rules=rules,
        symbol=symbol,
    )
    if str(symbol).upper() in NO_SECTOR_ETF:
        align = alignment_features(**common, peers=[], etf_pct=None, etf_typical=None)
        for key in list(align):
            if str(key).startswith("SECTOR_"):
                align[key] = 0.0
        align["_sector_etf_weight"] = 1.0
        align["_sector_etf"] = ""
        align["_sector_etf_pct"] = None
        return align
    stats = getattr(snap, "vt_etf_stats", None) or {}
    peersets = getattr(snap, "vt_peer_sets", None) or {}
    names = candidate_etfs(symbol)
    if stats and names:
        typicals = getattr(snap, "vt_etf_typical_by_etf", None) or {}
        bundles = []
        for etf in names:
            info = stats.get(etf) or {}
            bundles.append((etf, alignment_features(
                **common,
                peers=peersets.get(etf) or [],
                etf_pct=info.get("pct"),
                etf_last=info.get("last"),
                etf_pmh=info.get("pmh"),
                etf_pml=info.get("pml"),
                etf_typical=typicals.get(etf),
            )))
        chosen = choose_sector_etf(bundles, primary=etf_for(symbol), rules=rules)
        feats = dict(chosen["features"])
        info = stats.get(chosen["etf"]) or {}
        feats["_sector_etf_weight"] = chosen["weight"]
        feats["_sector_etf"] = chosen["etf"]
        feats["_sector_etf_pct"] = info.get("pct")
        LOG.info(
            "SECTOR ETF %s chose %s weight=%s total=%.2f",
            symbol, chosen["etf"], chosen["weight"], chosen["total"],
        )
        return feats
    primary = etf_for(symbol)
    align = alignment_features(
        **common,
        peers=getattr(snap, "vt_peers", None),
        etf_pct=getattr(snap, "vt_etf_pct", None),
        etf_last=getattr(snap, "vt_etf_last", None),
        etf_pmh=getattr(snap, "vt_etf_pmh", None),
        etf_pml=getattr(snap, "vt_etf_pml", None),
        etf_typical=getattr(snap, "vt_etf_typical", None),
    )
    align["_sector_etf_weight"] = etf_multiplier(primary, rules) if primary else 1.0
    align["_sector_etf"] = primary or ""
    align["_sector_etf_pct"] = getattr(snap, "vt_etf_pct", None)
    return align


def enrich_features(features: dict, snap, settings, prior=None, spy_bars=None) -> dict:
    """Add the volume-trend rows. Every volume read is a float. Nothing is left blank."""
    rules = VOLUME_TREND_RULES
    features = dict(features or {})
    side = "CALL"
    option = None
    bars = list(getattr(snap, "bars", None) or [])
    direction = 1
    # The caller scores one side. A feature dict has no side; the option on the
    # snap is not unique. prior and settings do not carry it either. The
    # enrich wrapper below is called with an explicit direction via _side.
    direction = int(features.pop("_vt_direction", 1) or 1)
    side = "PUT" if direction < 0 else "CALL"
    chain, source = rvol_chain(snap, bars)
    if source == "NEUTRAL":
        hot = dead = 0.0
    else:
        hot = 1.0 if chain >= float(rules["rvol_hot"]) else 0.0
        dead = 1.0 if float(rules["rvol_dead_lo"]) <= chain < float(rules["rvol_dead_hi"]) else 0.0
    features["RVOL_CHAIN_ge1.5"] = hot
    features["RVOL_CHAIN_1_15"] = dead
    features["_rvol"] = float(chain)
    features["_rvol_src"] = source
    day_ratio = chain if source.startswith("RVOL") else _bar_vs_day(bars)
    features["VOL_DAY"] = _match(day_ratio)
    features["VOL_5"] = _match(_window_ratio(bars, 5))
    features["VOL_15"] = _match(_window_ratio(bars, 15))
    broken = break_features(bars, direction, snap, rules)
    features.update(broken)
    atr = getattr(snap, "prior_atr21", None)
    if atr is None:
        atr = getattr(snap, "prior_atr", None)
    tb = _tb_features(bars, direction, atr, broken.get("_break_index"), rules)
    # Credit T1/T2 inside TB_ENTRY when those ratios cleared. Weight stays 0.
    extra = 0
    if features["BU_5_DAY_ge1"]:
        extra += 1
    if features["BRK_VS_10_ge1.5"]:
        extra += 1
    if broken.get("_break_index") is not None:
        tb["TB_ENTRY"] = min(1.0, tb["TB_ENTRY"] + 0.16 * extra)
    features.update(tb)
    align = sector_feature_choice(snap, direction, spy_bars, rules)
    spy_pct, _qqq_pct, _above = _spy_view(snap, spy_bars)
    for key, value in align.items():
        if key.startswith("_"):
            features[key] = value
        else:
            features[key] = float(value)
    features["_sector_etf_weight"] = align.get("_sector_etf_weight", 1.0)
    features["_sector_etf"] = align.get("_sector_etf") or ""
    stock_pct = None
    if bars:
        stock_pct = pct_from_open(bars[0].open, bars[-1].close)
    features["RS_SPY"] = 1.0 if stock_pct is not None and spy_pct is not None and direction * (stock_pct - float(spy_pct)) > 0 else 0.0
    etf_pct = _finite(align.get("_sector_etf_pct"))
    if etf_pct is None and str(getattr(snap, "symbol", "")).upper() not in NO_SECTOR_ETF:
        etf_pct = _finite(getattr(snap, "vt_etf_pct", None))
    features["RS_SECTOR"] = 1.0 if stock_pct is not None and etf_pct is not None and direction * (stock_pct - etf_pct) > 0 else 0.0
    primary_etf = etf_for(getattr(snap, "symbol", ""))
    divergence = divergence_sector(
        symbol=getattr(snap, "symbol", ""),
        stock_bars=bars,
        etf_bars=_etf_bars_for(snap, primary_etf),
        direction=direction,
        spy_pct=spy_pct,
        spy_above_vwap=_above,
        card_time=getattr(snap, "at", None),
        rules=rules,
    )
    features.update(divergence)
    LOG.debug(
        "DIVERGENCE_SECTOR symbol=%s etf=%s side=%s DIV_ETF_WRONG_SIDE_10=%s "
        "DIV_STOCK_ORB_ETF_NOT=%s DIV_ETF_OR30_AGAINST=%s",
        getattr(snap, "symbol", ""),
        divergence.get("_div_etf") or "",
        side,
        divergence["DIV_ETF_WRONG_SIDE_10"],
        divergence["DIV_STOCK_ORB_ETF_NOT"],
        divergence["DIV_ETF_OR30_AGAINST"],
    )
    features["PUSH_VOL_B"] = push_vol_b(bars, direction, atr, chain, rules)
    features["RF_NEWHI_VOL"] = rf_newhi_vol(bars, side, prior, getattr(snap, "at", None), rules)
    from .vt_measure import measure_volume, option_rows
    levels = {
        "premarket_high": getattr(snap, "premarket_high", None),
        "premarket_low": getattr(snap, "premarket_low", None),
        "prior_high": getattr(snap, "prior_high", None),
        "prior_low": getattr(snap, "prior_low", None),
        "high_5d": getattr(snap, "high_5d", None),
        "low_5d": getattr(snap, "low_5d", None),
        "high_md": getattr(snap, "high_md", None),
        "low_md": getattr(snap, "low_md", None),
        "week_high": getattr(snap, "week_high", None),
        "week_low": getattr(snap, "week_low", None),
    }
    minute = _minute_index(bars[-1]) if bars else None
    measured = measure_volume(
        bars, direction, levels, atr,
        tod=getattr(snap, "vt_tod", None),
        premarket=getattr(snap, "vt_premarket", None),
        cum_curves=_cum_for(snap, minute),
        full_day=getattr(snap, "vt_full_day", None),
        rules=rules,
        symbol=getattr(snap, "symbol", ""),
    )
    features.update(measured["flags"])
    features.update(vol_bin_flags(measured["raw"], rules))
    features["_vt_raw"] = measured["raw"]
    features["_vt_sources"] = measured["sources"]
    for alias, spec in VOL_ALIASES.items():
        present = []
        for primary in alias_rows(spec):
            value = features.get(primary)
            if isinstance(value, (int, float)) and isfinite(float(value)):
                present.append(float(value))
        if present:
            features[alias] = max(present)
    sector_on = any(
        isinstance(features.get(key), (int, float)) and float(features[key]) >= 1
        for key in features
        if str(key).startswith("SECTOR_") and not str(key).startswith("SECTOR_HIT")
    )
    features["SECTOR_HIT"] = 1.0 if sector_on else 0.0
    picked = features.pop("_vt_option", None)
    history = features.pop("_vt_option_history", None)
    features.update(option_rows(getattr(snap, "options", None), picked, history, rules))
    thesis = (
        (features["SECTOR_L2_PMH"] >= 1 or features["SECTOR_ETF_PCT"] >= 1)
        and (features["BRK_VS_10_ge1.5"] >= 1 or features["RVOL_CHAIN_ge1.5"] >= 1)
    )
    features["THESIS_CARRY"] = 1.0 if thesis else 0.0
    for key in (
        "RVOL_CHAIN_ge1.5", "RVOL_CHAIN_1_15", "VOL_DAY", "VOL_BRK", "VOL_PRE",
        "VOL_5", "VOL_15", "BRK_VS_10_ge1.5", "BU_5_DAY_ge1", "PUSH_VOL_B", "RF_NEWHI_VOL",
    ):
        value = features.get(key)
        if value is None or not isinstance(value, (int, float)) or not isfinite(float(value)):
            features[key] = 0.0
    return features


def _cum_for(snap, minute):
    if minute is None:
        minute_key = None
    else:
        minute_key = minute
    curves = {}
    for key, attr, sessions_attr, need in (
        (10, "rvol_base", "prior_full_days", 10),
        (5, "vt_rvol_base_5", "vt_rvol_sessions_5", 5),
        (3, "vt_rvol_base_3", "vt_rvol_sessions_3", 3),
        ("any", "vt_rvol_base_any", "vt_rvol_sessions_any", 1),
    ):
        base = getattr(snap, attr, None)
        sessions = getattr(snap, sessions_attr, None)
        if key == 10 and base is None:
            base = getattr(snap, "fallback_rvol_base", None)
            sessions = getattr(snap, "fallback_rvol_sessions", None)
        try:
            sessions = int(sessions) if sessions is not None else 0
        except (TypeError, ValueError):
            sessions = 0
        if sessions >= need and isinstance(base, (int, float)) and isfinite(base):
            curves[key] = float(base)
    tod = getattr(snap, "vt_tod", None) or {}
    cum = tod.get("cum") if isinstance(tod, dict) else None
    if isinstance(cum, dict) and minute_key is not None:
        for key in (10, 5, 3, "any"):
            if key in curves:
                continue
            value = (cum.get(key) or {}).get(minute_key)
            if isinstance(value, (int, float)) and isfinite(value):
                curves[key] = float(value)
    return curves


def option_spread_fraction(option):
    """(ask-bid)/mid. None when the quote is missing. Not a block."""
    if option is None:
        return None
    try:
        bid = float(option.bid)
        ask = float(option.ask)
    except (TypeError, ValueError, AttributeError):
        return None
    mid = (ask + bid) / 2.0
    if not isfinite(bid) or not isfinite(ask) or mid <= 0 or ask < bid:
        return None
    return (ask - bid) / mid


def option_contract_volume(option):
    """Day contract volume. None when the print is missing."""
    if option is None:
        return None
    try:
        volume = float(option.volume)
    except (TypeError, ValueError, AttributeError):
        return None
    if not isfinite(volume) or volume < 0:
        return None
    return volume


def prepare_features(features, snap, settings, *, direction: int, prior=None, spy_bars=None,
                     option=None, option_history=None):
    if not is_volume_trend(getattr(settings, "profile", "")):
        return features
    features = dict(features or {})
    features["_vt_direction"] = 1 if direction >= 0 else -1
    if option is not None:
        features["_vt_option"] = option
        features["entry_spread"] = option_spread_fraction(option)
        features["opt_volume"] = option_contract_volume(option)
    if option_history is not None:
        features["_vt_option_history"] = option_history
    return enrich_features(features, snap, settings, prior=prior, spy_bars=spy_bars)


# --- strikes -----------------------------------------------------------------

def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def _year_fraction(snap_at, expiry) -> float:
    if snap_at is None or expiry is None:
        return 1.0 / 365.0
    close = datetime(expiry.year, expiry.month, expiry.day, 16, 0, tzinfo=ET)
    seconds = (close - snap_at.astimezone(ET)).total_seconds()
    return max(seconds, 60.0) / (365.0 * 24.0 * 3600.0)


def _bs_price(spot, strike, time_years, rate, sigma, side: str) -> float:
    if time_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        intrinsic = max(0.0, spot - strike) if side == "CALL" else max(0.0, strike - spot)
        return intrinsic
    vol = sigma * sqrt(time_years)
    d1 = (log(spot / strike) + (rate + 0.5 * sigma * sigma) * time_years) / vol
    d2 = d1 - vol
    disc = exp(-rate * time_years)
    if side == "CALL":
        return spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    return strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _bs_delta(spot, strike, time_years, rate, sigma, side: str) -> float:
    vol = sigma * sqrt(time_years)
    d1 = (log(spot / strike) + (rate + 0.5 * sigma * sigma) * time_years) / vol
    if side == "CALL":
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0


def implied_vol(spot, strike, time_years, rate, price, side: str):
    if price is None or price <= 0 or spot <= 0 or strike <= 0 or time_years <= 0:
        return None
    lo, hi = 1e-4, 5.0
    low_price = _bs_price(spot, strike, time_years, rate, lo, side)
    high_price = _bs_price(spot, strike, time_years, rate, hi, side)
    if price < low_price - 1e-6 or price > high_price + 1e-3:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        value = _bs_price(spot, strike, time_years, rate, mid, side)
        if value > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def resolved_delta(option, snap, rules=None):
    """Feed delta when it is a real greek. Otherwise Black-Scholes on the mid, r=4%, no dividend.

    A failure leaves None. The caller must not treat that as a reject.
    """
    rules = rules or VOLUME_TREND_RULES
    raw = getattr(option, "delta", None)
    number = _finite(raw)
    if number is not None and abs(number) <= 1.0:
        return number
    bid = _finite(getattr(option, "bid", None))
    ask = _finite(getattr(option, "ask", None))
    spot = _finite(getattr(snap, "spot", None))
    strike = _finite(getattr(option, "strike", None))
    if bid is None or ask is None or spot is None or strike is None or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    time_years = _year_fraction(getattr(snap, "at", None), getattr(option, "expiry", None))
    sigma = implied_vol(spot, strike, time_years, float(rules["rate"]), mid, option.side)
    if sigma is None:
        return None
    return _bs_delta(spot, strike, time_years, float(rules["rate"]), sigma, option.side)


def delta_ok(delta, ask, rules=None) -> bool:
    """Hard cap 0.70. 0.20–0.25 only when the ask is under $0.50. Missing delta is allowed."""
    rules = rules or VOLUME_TREND_RULES
    if delta is None:
        return True
    mag = abs(float(delta))
    cap = float(rules["delta_cap"])
    if mag > cap + 1e-12:
        return False
    cheap_lo = float(rules["delta_cheap_lo"])
    cheap_hi = float(rules["delta_cheap_hi"])
    if mag < cheap_lo - 1e-12:
        return False
    if mag < cheap_hi - 1e-12:
        ask = _finite(ask)
        return ask is not None and ask < float(rules["delta_cheap_ask"])
    return True


def strike_window(strikes, spot: float, side: str, itm_count: int | None = None,
                  per_side: int | None = None) -> list:
    """Up to 3 ITM strikes plus the OTM side, about 20 per side, around the current spot."""
    rules = VOLUME_TREND_RULES
    itm_count = int(rules["itm_count"] if itm_count is None else itm_count)
    per_side = int(rules["per_side"] if per_side is None else per_side)
    ordered = sorted({float(strike) for strike in strikes})
    spot = float(spot)
    if side == "CALL":
        itm = [strike for strike in ordered if strike < spot][-itm_count:]
        otm = [strike for strike in ordered if strike >= spot]
    else:
        itm = [strike for strike in ordered if strike > spot][:itm_count]
        otm = [strike for strike in ordered if strike <= spot]
        otm = list(reversed(otm))
    room = max(0, per_side - len(itm))
    chosen = itm + otm[:room]
    if side == "PUT":
        chosen = sorted(chosen)
    return chosen[:per_side]


def fallback_strike_order(strikes, spot: float, side: str) -> list:
    """1 OTM, then ATM, then one ITM strike, then further OTM. Never the second ITM."""
    ordered = sorted({float(strike) for strike in strikes})
    if not ordered:
        return []
    spot = float(spot)
    atm = min(ordered, key=lambda strike: (abs(strike - spot), strike))
    if side == "CALL":
        otm = [strike for strike in ordered if strike > atm]
        itm = [strike for strike in reversed(ordered) if strike < atm]
    else:
        otm = [strike for strike in reversed(ordered) if strike < atm]
        itm = [strike for strike in ordered if strike > atm]
    sequence = []
    if otm:
        sequence.append(otm[0])
    sequence.append(atm)
    if itm:
        sequence.append(itm[0])
    sequence.extend(otm[1:])
    seen = set()
    unique = []
    for strike in sequence:
        if strike not in seen:
            seen.add(strike)
            unique.append(strike)
    return unique


def filter_chain_options(options, spot: float) -> list:
    kept = []
    by_side = {}
    for option in options or []:
        by_side.setdefault(option.side, []).append(option)
    for side, rows in by_side.items():
        allowed = set(strike_window([option.strike for option in rows], spot, side))
        kept.extend(option for option in rows if option.strike in allowed)
    return kept


def volume_trend_symbols(symbols) -> list:
    """Profile universe. IWM stays. Standby names move into the main list."""
    kept = []
    seen = set()
    for raw in symbols or []:
        name = str(raw).strip().upper()
        if not name or name in UNIVERSE_CUT or name in seen:
            continue
        seen.add(name)
        kept.append(name)
    for name in STANDBY_NAMES:
        if name not in seen:
            seen.add(name)
            kept.append(name)
    return kept


def pin_deep_names(discovery) -> None:
    """ORCL, CRWV, SMCI first among always-deep. Capacity is not raised."""
    cap = int(discovery.capacity)
    room = cap - max(1, cap // 4)
    extra = [name for name in DEEP_PRIORITY if name not in discovery.always_deep]
    if len(discovery.always_deep) + len(extra) <= room:
        discovery.always_deep.update(extra)
    discovery.deep_priority = DEEP_PRIORITY


# Fit-period +200% runners per active day. Earlier is a higher RUNNER_RANK.
# Stocks only. Startup loads the first N that the call budget allows.
EXTRA_UNIVERSE = (
    "MARA", "RIVN", "RIOT", "DKNG", "OXY", "EOSE", "RDDT", "APP", "APLD", "HIMS",
    "LUNR", "DAL", "CVS", "GILD", "TXN", "STX", "CRWV", "WULF", "AAL", "ONDS",
    "WFC", "NVTS", "USAR", "MP", "SOUN", "CLF", "CCJ", "TWLO", "U", "ALAB",
    "GME", "RKT", "F", "GLW", "LCID", "AI", "UPS", "ON", "NXPI", "DIS",
    "CORZ", "ASTS", "LITE", "BIDU", "NU", "JNJ", "MDT", "QS", "BMNR", "CIFR",
    "KO", "VZ", "RGTI", "T", "SMR", "CRDO", "OPEN", "QUBT", "HUT", "UUUU",
)

# How many net-new EXTRA_UNIVERSE names startup may load. The ranked list
# stays complete. Edit this cap, or set EXTRA_UNIVERSE_MAX, to add or swap
# later. Names already in the universe are skipped and do not count.
# This does not add deep slots and does not change the rate limit.
EXTRA_UNIVERSE_MAX = 20

# Poll-loop constants. Strike count is a parameter on one /chains call, so
# 80 and 40 issue the same number of requests. Do not raise these.
PAID_MARKET_DATA_PER_MINUTE = 120
CALL_BUDGET_MARGIN = 0.20
POLL_SECONDS = 60
SWEEP_PER_CYCLE = 26
DEEP_SLOTS = 60
QUOTE_BATCH = 180


def poll_cycle_calls(deep_slots: int = DEEP_SLOTS, sweep_slots: int = SWEEP_PER_CYCLE,
                     spx_proxy: bool = True) -> int:
    """Market-data GETs one steady poll issues.

    1 batched /quotes, up to `sweep_slots` /chains, and each deep name costs
    one /pricehistory plus one /chains. SPX adds one SPY /pricehistory.
    strikeCount is not a second call.
    """
    return 1 + int(sweep_slots) + 2 * int(deep_slots) + (1 if spx_proxy else 0)


def extra_universe_cap() -> int:
    """EXTRA_UNIVERSE_MAX. A set env value overrides the constant."""
    raw = os.getenv("EXTRA_UNIVERSE_MAX")
    if raw is None or not str(raw).strip():
        return max(0, int(EXTRA_UNIVERSE_MAX))
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        LOG.warning("EXTRA_UNIVERSE_MAX=%s is not an integer; using %s", raw, EXTRA_UNIVERSE_MAX)
        return max(0, int(EXTRA_UNIVERSE_MAX))


def call_budget(core_count: int) -> dict:
    """Before/after the 40-strike cut, and how many extra names fit.

    The paid ceiling is the 120/min figure the scanner already uses.
    Usable calls are 20% under that. Extra names do not raise deep slots or
    the sweep cap, so they do not add a steady per-minute call. Each name
    that startup accepts costs one chain probe plus one history backfill,
    and that startup burst stays inside the usable cap. The load itself
    stops at EXTRA_UNIVERSE_MAX even when the margin would allow more.
    """
    steady = poll_cycle_calls()
    per_minute = steady * (60 / POLL_SECONDS)
    usable = int(PAID_MARKET_DATA_PER_MINUTE * (1.0 - CALL_BUDGET_MARGIN))
    fixed = 1 + SWEEP_PER_CYCLE + 1  # quote, sweep, SPX proxy
    max_deep = max(0, (usable - fixed) // 2)
    quote_room = max(0, QUOTE_BATCH - int(core_count))
    startup_each = 2  # chain probe + RVOL history
    startup_n = usable // startup_each
    budget_room = min(quote_room, startup_n)
    cap = extra_universe_cap()
    extra_names = min(budget_room, cap)
    return {
        "calls_per_cycle_80": steady,
        "calls_per_minute_80": per_minute,
        "calls_per_cycle_40": steady,
        "calls_per_minute_40": per_minute,
        "paid_per_minute": PAID_MARKET_DATA_PER_MINUTE,
        "usable_per_minute": usable,
        "margin": CALL_BUDGET_MARGIN,
        "configured_deep_slots": DEEP_SLOTS,
        "max_deep_slots_under_margin": max_deep,
        "extra_deep_slots": max(0, max_deep - DEEP_SLOTS),
        "quote_room": quote_room,
        "extra_names_budget": budget_room,
        "extra_universe_max": cap,
        "extra_names": extra_names,
        "startup_calls_per_loaded_name": startup_each,
    }


def runner_ranks(symbols=None) -> dict:
    """Higher is more likely. The head of EXTRA_UNIVERSE outranks the tail."""
    chosen = EXTRA_UNIVERSE if symbols is None else symbols
    order = {name: index for index, name in enumerate(EXTRA_UNIVERSE)}
    return {name: float(len(EXTRA_UNIVERSE) - order[name]) for name in chosen if name in order}


def select_extra_names(existing, has_chain, n: int, ranked=None) -> list:
    """First N names with a chain, skipping names already in the universe.

    A name already in `existing` is not counted toward N. A name whose chain
    probe is false is logged and skipped, and is not counted either.
    """
    have = {str(name).upper() for name in existing or ()}
    loaded = []
    limit = max(0, int(n))
    for raw in ranked or EXTRA_UNIVERSE:
        name = str(raw).upper()
        if name in have or name in UNIVERSE_CUT:
            continue
        if len(loaded) >= limit:
            break
        try:
            ok = bool(has_chain(name))
        except Exception as exc:
            LOG.warning("EXTRA_UNIVERSE %s chain check failed (%s); skipping", name, type(exc).__name__)
            ok = False
        if not ok:
            LOG.info("EXTRA_UNIVERSE %s has no chain; skipping", name)
            continue
        loaded.append(name)
    return loaded


def install_extra_universe(symbols, client, settings) -> tuple:
    """Add the ranked names that fit the budget and have a chain.

    Returns (symbols, loaded, budget). A chain miss is logged and skipped.
    Deep capacity and the sweep cap stay where they are.
    """
    from datetime import datetime, timezone

    budget = call_budget(len(symbols))
    LOG.info("Call budget %s", json.dumps(budget))
    strikes = VOLUME_TREND_RULES["strike_count"] if is_volume_trend(getattr(settings, "profile", "")) else 80
    max_dte = getattr(settings, "max_dte", 1)

    def has_chain(name: str) -> bool:
        now = datetime.now(timezone.utc)
        try:
            data = client.option_chain(name, now, max_dte, strikes)
        except Exception as exc:
            LOG.warning("EXTRA_UNIVERSE %s chain check failed (%s); skipping", name, type(exc).__name__)
            return False
        try:
            parsed = client.parse_chain(data)
        except Exception as exc:
            LOG.warning("EXTRA_UNIVERSE %s chain parse failed (%s); skipping", name, type(exc).__name__)
            return False
        if not parsed:
            return False
        _backfill_rvol(client, name, now)
        return True

    loaded = select_extra_names(symbols, has_chain, budget["extra_names"])
    if getattr(client, "discovery", None) is not None:
        client.discovery.runner_rank = runner_ranks(loaded)
    have = {str(name).upper() for name in symbols}
    merged = list(symbols)
    for name in loaded:
        if name not in have:
            merged.append(name)
            have.add(name)
    LOG.info("EXTRA_UNIVERSE loaded %d/%d: %s", len(loaded), budget["extra_names"], ",".join(loaded) or "none")
    return merged, loaded, budget


def _backfill_rvol(client, symbol: str, now) -> str:
    """Build a TOD curve for a new name. Log which fallback was used."""
    day = now.astimezone(ET).date()
    stock_days = []
    try:
        client.baseline(symbol, now)
        tod = getattr(client, "vt_tod", {}).get((symbol, day)) or {}
        if tod.get(10):
            LOG.info("RVOL backfill %s source=10-day", symbol)
            return "10-day"
        if tod.get(5) or tod.get(3) or tod.get("any"):
            LOG.info("RVOL backfill %s source=available-days", symbol)
            return "available-days"
    except Exception as exc:
        LOG.warning("RVOL backfill %s bars failed (%s)", symbol, type(exc).__name__)
    etf = None
    mapped = etf_for(symbol)
    if mapped:
        etf = (getattr(client, "vt_tod", {}) or {}).get((mapped, day), {}).get(10)
    peers = []
    for name, pack in (getattr(client, "vt_tod", {}) or {}).items():
        curve = (pack or {}).get(10)
        if curve and (not isinstance(name, tuple) or name[0] != symbol):
            peers.append(curve)
    curve, source = rvol_backfill_curve(stock_days, etf, peers)
    if not hasattr(client, "vt_tod"):
        client.vt_tod = {}
    client.vt_tod[(symbol, day)] = {10: curve, 5: curve, 3: curve, "any": curve}
    LOG.info("RVOL backfill %s source=%s", symbol, source)
    return source


def rvol_backfill_curve(stock_days, etf_curve=None, universe_curves=None) -> tuple:
    """10-day TOD curve, else available days, else ETF-scaled, else median.

    The curve is never empty. The last resort is a flat 1 so RVOL is not blank.
    """
    def mean(days):
        out = {}
        count = len(days)
        for minute in range(390):
            vals = []
            for curve in days:
                value = curve.get(minute)
                if value is not None:
                    vals.append(float(value))
            out[minute] = sum(vals) / len(vals) if vals else 0.0
        return out

    usable = [curve for curve in (stock_days or []) if curve]
    if len(usable) >= 10:
        return mean(usable[-10:]), "10-day"
    if usable:
        return mean(usable), "available-days"
    etf = {}
    for minute, value in (etf_curve or {}).items():
        try:
            minute = int(minute)
            value = float(value)
        except (TypeError, ValueError):
            continue
        if 0 <= minute < 390 and isfinite(value):
            etf[minute] = value
    medians = []
    for curve in universe_curves or []:
        if curve:
            medians.append(curve)
    median = {}
    if medians:
        for minute in range(390):
            vals = sorted(float(curve[minute]) for curve in medians if curve.get(minute) is not None)
            if not vals:
                continue
            mid = len(vals) // 2
            median[minute] = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
    if etf and median:
        etf_sum = sum(etf.values()) or 1.0
        med_sum = sum(median.values()) or 1.0
        factor = med_sum / etf_sum
        return {minute: etf.get(minute, 0.0) * factor for minute in range(390)}, "sector-etf-scaled"
    if median:
        return {minute: median.get(minute, 0.0) for minute in range(390)}, "universe-median"
    return {minute: 1.0 for minute in range(390)}, "flat-1"


def pack_vt_rvol(full_days, observations) -> dict:
    """Shorter curves beside the live 10-day curve. Does not replace that curve."""
    def mean_curve(curves):
        if not curves:
            return {}
        count = len(curves)
        return {minute: sum(curve[minute] for curve in curves) / count for minute in range(390)}

    any_base = {}
    any_n = 0
    for minute in range(390):
        values = list((observations or {}).get(minute + 1) or [])
        used = values[-10:]
        if used:
            any_base[minute] = sum(used) / len(used)
            any_n = max(any_n, len(used))
    full_days = list(full_days or [])
    totals = [curve.get(389) for curve in full_days if curve.get(389)]
    full_day = sum(totals) / len(totals) if totals else None
    return {
        "base5": mean_curve(full_days[-5:]) if len(full_days) >= 5 else {},
        "sessions5": 5 if len(full_days) >= 5 else 0,
        "base3": mean_curve(full_days[-3:]) if len(full_days) >= 3 else {},
        "sessions3": 3 if len(full_days) >= 3 else 0,
        "base_any": any_base,
        "sessions_any": any_n,
        "full_day": full_day,
    }


def pack_tod(profiles, today) -> dict:
    """Smoothed same-minute volume. ±2 uses prior days only, never today's later bars."""
    days = []
    for day in sorted(profiles or {}):
        if day >= today:
            continue
        minutes = profiles[day]
        if sum(1 for minute in range(390) if minute in minutes) < 380:
            continue
        days.append(minutes)

    def smoothed(subset):
        out = {}
        for minute in range(390):
            acc = []
            for curve in subset:
                for delta in range(-2, 3):
                    value = curve.get(minute + delta)
                    if value is not None:
                        acc.append(float(value))
            if acc:
                out[minute] = sum(acc) / len(acc)
        return out

    return {
        10: smoothed(days[-10:]) if len(days) >= 10 else {},
        5: smoothed(days[-5:]) if len(days) >= 5 else {},
        3: smoothed(days[-3:]) if len(days) >= 3 else {},
        "any": smoothed(days[-10:]) if days else {},
    }


def pack_range_levels(daily, today) -> dict:
    sessions = []
    for day in sorted(daily or {}):
        if day >= today:
            continue
        rows = daily[day]
        try:
            high = max(float(row["high"]) for row in rows)
            low = min(float(row["low"]) for row in rows)
        except (TypeError, ValueError, KeyError):
            continue
        sessions.append((day, high, low))
    if not sessions:
        return {}
    last5 = sessions[-5:]
    week_key = sessions[-1][0].isocalendar()[:2]
    week = [item for item in sessions if item[0].isocalendar()[:2] == week_key] or last5
    return {
        "high_5d": max(item[1] for item in last5),
        "low_5d": min(item[2] for item in last5),
        "high_md": max(item[1] for item in sessions),
        "low_md": min(item[2] for item in sessions),
        "week_high": max(item[1] for item in week),
        "week_low": min(item[2] for item in week),
    }


def _quote_row(name, parsed, levels, day):
    quote = (parsed or {}).get(name) or {}
    level = (levels or {}).get((name, day), {}) if levels else {}
    if not isinstance(quote, dict):
        quote = {}
    return {
        "symbol": name,
        "open": quote.get("open"),
        "last": quote.get("price"),
        "pmh": level.get("premarket_high"),
        "pml": level.get("premarket_low"),
        "pdh": level.get("prior_high"),
        "pdl": level.get("prior_low"),
    }


def sector_tape(symbol: str, parsed: dict, levels: dict, day):
    """Peer rows from the GICS map. A name with no bars is not moving. It is not an error."""
    primary = etf_for(symbol)
    peers = []
    for name in peers_for(symbol) if primary else ():
        peers.append(_quote_row(name, parsed, levels, day))
    names = () if str(symbol).upper() in NO_SECTOR_ETF else candidate_etfs(symbol)
    etf_stats = {}
    peer_sets = {}
    me = str(symbol or "").upper()
    for etf in names:
        row = _quote_row(etf, parsed, levels, day)
        etf_stats[etf] = {
            "pct": pct_from_open(row["open"], row["last"]),
            "last": row["last"],
            "pmh": row["pmh"],
            "pml": row["pml"],
        }
        if etf == "MTUM":
            peer_sets[etf] = []
        else:
            peer_sets[etf] = [
                _quote_row(name, parsed, levels, day)
                for name in peers_sharing(etf)
                if name != me
            ]
    etf = primary
    etf_quote = (parsed or {}).get(etf) or {} if etf else {}
    etf_level = (levels or {}).get((etf, day), {}) if etf and levels else {}
    spy = (parsed or {}).get("SPY") or {}
    qqq = (parsed or {}).get("QQQ") or {}
    name_pcts = []
    pct_by_symbol = {}
    for name in sector_map_names():
        quote = (parsed or {}).get(name) or {}
        if not isinstance(quote, dict):
            continue
        pct = pct_from_open(quote.get("open"), quote.get("price"))
        pct_by_symbol[name] = pct
        name_pcts.append(pct)
    return {
        "peers": peers,
        "etf_pct": pct_from_open(etf_quote.get("open"), etf_quote.get("price")),
        "etf_last": etf_quote.get("price"),
        "etf_pmh": etf_level.get("premarket_high"),
        "etf_pml": etf_level.get("premarket_low"),
        "spy_pct": pct_from_open(spy.get("open"), spy.get("price")),
        "qqq_pct": pct_from_open(qqq.get("open"), qqq.get("price")),
        "name_pcts": name_pcts,
        "pct_by_symbol": pct_by_symbol,
        "etf_stats": etf_stats,
        "peer_sets": peer_sets,
        "quotes": {
            name: _quote_row(name, parsed, levels, day)
            for name in sector_map_names()
        },
    }


def attach_cycle_context(snap, client, symbol, day, minute, parsed, profile) -> None:
    """Shorter RVOL and range levels on the snap. Sector tape only for this profile."""
    pack = getattr(client, "vt_rvol", {}).get((symbol, day), {}) if client is not None else {}
    for attr, key in (
        ("vt_rvol_base_5", "base5"),
        ("vt_rvol_base_3", "base3"),
        ("vt_rvol_base_any", "base_any"),
    ):
        object.__setattr__(snap, attr, (pack.get(key) or {}).get(minute))
    object.__setattr__(snap, "vt_rvol_sessions_5", pack.get("sessions5"))
    object.__setattr__(snap, "vt_rvol_sessions_3", pack.get("sessions3"))
    object.__setattr__(snap, "vt_rvol_sessions_any", pack.get("sessions_any"))
    object.__setattr__(snap, "vt_full_day", pack.get("full_day"))
    tod = getattr(client, "vt_tod", {}).get((symbol, day), {}) if client is not None else {}
    object.__setattr__(snap, "vt_tod", tod)
    for name, value in (getattr(client, "vt_levels", {}).get((symbol, day), {}) if client is not None else {}).items():
        object.__setattr__(snap, name, value)
    if not is_volume_trend(profile):
        return
    tape = sector_tape(symbol, parsed, getattr(client, "levels", {}), day)
    object.__setattr__(snap, "vt_peers", tape["peers"])
    object.__setattr__(snap, "vt_etf_pct", tape["etf_pct"])
    object.__setattr__(snap, "vt_etf_last", tape["etf_last"])
    object.__setattr__(snap, "vt_etf_pmh", tape["etf_pmh"])
    object.__setattr__(snap, "vt_etf_pml", tape["etf_pml"])
    object.__setattr__(snap, "vt_spy_pct", tape["spy_pct"])
    object.__setattr__(snap, "vt_qqq_pct", tape["qqq_pct"])
    object.__setattr__(snap, "vt_name_pcts", tape["name_pcts"])
    object.__setattr__(snap, "vt_pct_by_symbol", tape["pct_by_symbol"])
    object.__setattr__(snap, "vt_etf_stats", tape["etf_stats"])
    object.__setattr__(snap, "vt_peer_sets", tape["peer_sets"])
    object.__setattr__(snap, "vt_quotes", tape["quotes"])
    tables = getattr(client, "vt_etf_typical", {}) if client is not None else {}
    by_etf = {}
    for etf in tape["etf_stats"]:
        pack = tables.get(etf) or {}
        curve = pack.get("by_minute") or {}
        value = curve.get(minute) if minute is not None else None
        by_etf[etf] = value
    object.__setattr__(snap, "vt_etf_typical_by_etf", by_etf)
    primary = etf_for(symbol)
    object.__setattr__(snap, "vt_etf_typical", by_etf.get(primary) if primary else None)
    cached = getattr(client, "bar_cache", None) if client is not None else None
    packed = (cached or {}).get((primary, day)) if primary else None
    if primary and isinstance(packed, dict) and not (getattr(snap, "vt_etf_bars", None) or {}).get(primary):
        rows = tuple(
            packed[key] for key in sorted(packed)
            if getattr(packed[key], "end", None) is not None
        )
        held = dict(getattr(snap, "vt_etf_bars", None) or {})
        held[primary] = rows
        object.__setattr__(snap, "vt_etf_bars", held)
