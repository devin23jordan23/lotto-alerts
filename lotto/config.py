from dataclasses import dataclass
from math import isfinite
from pathlib import Path

STRATEGY_VERSION = "model-v-2026-10-26"

# Feature name, tier. One table: the quant replaces the tier and the three
# weights below. Pattern rows are a 0-1 match, not a boolean. Points for every
# row are tier weight × match. Placeholder assignments until that pass.
# Puts use the same rows; the feature functions mirror the side.
CONFLUENCE_TABLE = (
    ("pm_break", "primary"),
    ("pd_break", "primary"),
    ("hod_break", "primary"),
    ("rvol", "primary"),
    ("volume", "primary"),
    ("option_flow", "primary"),
    ("range_break", "primary"),
    ("ema9_pullback", "primary"),
    ("flag", "primary"),
    ("rs", "secondary"),
    ("sector", "secondary"),
    ("spy", "secondary"),
    ("vwap", "secondary"),
    ("w_pattern", "secondary"),
    ("m_pattern", "secondary"),
    ("mtf_flag", "secondary"),
    ("box5", "tertiary"),
    ("atr_move", "tertiary"),
    ("consolidation", "tertiary"),
    ("bounce", "tertiary"),
    ("fashionably_late", "tertiary"),
)
# Vince's handoff logistic, standardization, and strength ranks. One file
# serves both LOTTO_HANDOFF_MODEL_PATH and LOTTO_STRENGTH_DISTRIBUTION_PATH.
HANDOFF_SPEC_PATH = str(Path(__file__).resolve().parent.parent / "config" / "handoff_model_v1026.json")
# v4c is the live book. A feature it does not list scores 0.
WEIGHTS_PATH = str(Path(__file__).resolve().parent.parent / "config" / "vince_additive_weights_v4c.json")


@dataclass(frozen=True)
class Settings:
    # Research defaults, not fitted probabilities or proven thresholds.
    # MIN_SCORE is the legacy Model S floor and must not default above 60.
    # Model V posting uses the four v2 bars below, not this floor and not the
    # live blend cuts (A >= 91, B >= 81).
    min_score: float = 60
    model: str = "S"
    post_v2_option_0dte: float = 40
    post_v2_option_1dte: float = 61
    post_v2_stock_0dte: float = 58
    post_v2_stock_1dte: float = 80
    # Live default is S12. Hybrid posts on v2 points. Handoff posts on fitted
    # win chance. combined remains available as a rollback, with stock_override.
    score_mode: str = "s12"
    post_combined_0dte: float = 40
    post_combined_1dte: float = 61
    stock_max_share: float = 0.40
    # LOTTO_PRESET=S11 selects the tighter handoff floors. S0 turns them off.
    # Floors are P(true win), the same bars as hoF >= A - delta.
    preset: str = "S12"
    probability_path: int = 1
    strength_quintile: float = 0.8016471997604073
    strength_decile: float = 0.8756064690026955
    pwin_normal_0dte: float = 0.43157386
    pwin_normal_1dte: float = 0.47114974
    pwin_quintile_0dte: float = 0.28822075
    pwin_quintile_1dte: float = 0.30901921
    pwin_decile: float = 0.17760635
    pwin_decile_1dte: float = 0.18333891
    handoff_model_path: str = HANDOFF_SPEC_PATH
    strength_distribution_path: str = HANDOFF_SPEC_PATH
    preset_note: str = (
        "S12 handoff floors: normal 0.43157386/0.47114974, "
        "strength >= 0.8016471997604073 → 0.28822075/0.30901921, "
        "strength >= 0.8756064690026955 → 0.17760635/0.18333891. Hybrid v2 bars stay 40/61."
    )
    # Runner names are off until LOTTO_STANDBY_NAMES=on. Those names use the
    # handoff A bar (the normal floors), with no strength step-down.
    standby_names: int = 0
    # Vince ship-1026. One block: bypass entry, marker, freshness, base,
    # confluence weights, and the blocker/override table. Placeholders until
    # the next fitted pass. S12 floors are not in this block and do not read it.
    # Bypass posts the nearest ATM contract when the option card does not.
    # post is the default. on is an alias of post. log writes JSONL only.
    # off skips the rule. None of these fields change the handoff step-down.
    fallback_rule: str = "post"
    # RVOL, push, and room are scored features, not gates. fb_push_atr_min and
    # fb_room_atr_max are the cuts inside those features.
    fb_rvol_min: float = 0
    fb_push_atr_min: float = 0.75
    fb_room_atr_max: float = 1.0
    fb_min_minutes: int = 0
    fb_max_spread_pct: float = 15
    weights_path: str = WEIGHTS_PATH
    # LOTTO_PROFILE names a block in the weights book. Unset matches conservative.
    profile: str = "conservative"
    # Additive post line from the active profile. Not capped. Conservative is 92.
    post_threshold: float = 92
    bypass_threshold: float = 40
    # Primary cards also need this many features with value >= 0.5.
    # Bypass cards do not read this field.
    min_positive_features: int = 0
    # Legacy score floor. The live ⚡ uses the profile post_threshold.
    bolt_min_score: float = 20
    strong_feature_min: int = 10
    blocker_bid: float = 0.05
    blocker_option_volume_5m: float = 50
    strike_window_pct: float = 0.05
    fb_same_move_before_min: int = 15
    fb_same_move_after_min: int = 5
    # Legacy score hook. The live ⚡ uses post_threshold and strong_feature_min.
    strong_score: float = 57
    stale_quote_seconds: float = 5
    stale_quote_penalty: float = 4
    # Base layer. Direction and this volume floor are the only requirements.
    # Missing volume posts when confluence points reach the override.
    base_volume_min: float = 1
    base_override_points: float = 8
    # Blocker table. Bid and 5-minute option volume do not lift.
    # blocker_spread is the old 15% cap. Live cards use spread_bands unless
    # spread_legacy is on, which restores that cap and nearest-ATM selection.
    blocker_spread: float = 0.15
    blocker_spread_override_points: float = 6
    # (ask-bid)/mid. <= the first edge is a normal quote. Each later edge is
    # the top of the next stock bar. Above the last edge there is no ceiling.
    spread_bands: tuple = (0.25, 0.35, 0.40)
    # (rvol, push30_atr21, setups) for the bands above 25%: S2, S3, S4.
    # judgment bars, unfitted (Vince 2026-10-26), keep LLY/MRNA/AAOI/CAT
    # The setups term stays in the triple so a later bar can turn it back on.
    spread_stock_bars: tuple = (
        (0.90, 0.65, 0),
        (1.25, 0.65, 0),
        (1.35, 0.75, 0),
    )
    blocker_spread_legacy: float = 0.15
    spread_legacy: int = 0
    # On: the 5-minute floor still passes, and cumulative session volume >= 100
    # also passes. spread_legacy forces this off.
    day_volume_swap: int = 1
    blocker_option_volume_day: float = 100
    # Stock carries option volume, and one stock-bar term may miss by the
    # tolerance. 0 is the pre-OPTB volume block and exact bars.
    # spread_legacy forces this off.
    optb: int = 1
    stock_bar_tolerance: float = 0.05
    # 1 is the first strike OTM. 0 is nearest ATM, which spread_legacy forces.
    fallback_strike_offset: int = 1
    # Reserved per-feature knobs. The scorer does not read them. The live
    # weight is the tier weight on CONFLUENCE_TABLE, times the 0-1 match.
    # Puts use the mirror (PML, PDL, below VWAP, weakness vs SPY/sector).
    w_conf_rs: float = 1
    w_conf_pm_break: float = 1
    w_conf_pd_break: float = 1
    w_conf_rvol: float = 1
    w_conf_option_flow: float = 1
    w_conf_sector: float = 1
    w_conf_ema9_pullback: float = 1
    w_conf_range_break: float = 1
    w_conf_mtf_flag: float = 1
    w_conf_box5: float = 1
    w_conf_vwap: float = 1
    w_conf_atr_move: float = 1
    w_conf_consolidation: float = 1
    w_conf_bounce: float = 1
    conf_rvol_min: float = 1
    conf_atr_min: float = 0.25
    # Tier weights for CONFLUENCE_TABLE. Placeholder until the quant pass.
    # Score = weight × match. Bypass posts when that sum clears the line.
    tier_weight_primary: float = 3
    tier_weight_secondary: float = 2
    tier_weight_tertiary: float = 1
    bypass_confluence_min: float = 9
    # Loose pattern ranges. Every detector (9 EMA pullback, flag, W, M,
    # Fashionably Late, consolidation, bounce) scans these instead of one
    # template. Inside the core the match is 1. From the core out to the
    # outer edge it fades to 0. Past the outer edge it is 0. Depth and the
    # ATR bands are in units of the 21-day ATR. Bar counts are completed
    # bars on each timeframe in pattern_timeframes. A match at or above
    # pattern_trigger_min is a bypass trigger the bar it first gets there.
    # Ace's playbook is not in this repo. The definitions live in confluence.py.
    pattern_timeframes: str = "2,5,10"
    pattern_bars_outer_min: float = 1
    pattern_bars_core_min: float = 3
    pattern_bars_core_max: float = 8
    pattern_bars_outer_max: float = 16
    pattern_depth_outer_min: float = 0.05
    pattern_depth_core_min: float = 0.15
    pattern_depth_core_max: float = 1.20
    pattern_depth_outer_max: float = 2.50
    pattern_trigger_min: float = 0.50
    pattern_twin_core: float = 0.15
    pattern_twin_outer: float = 0.60
    ema_pullback_period: int = 9
    ema_move_outer_min: float = 0.10
    ema_move_core_min: float = 0.25
    ema_move_core_max: float = 3.0
    ema_move_outer_max: float = 5.0
    ema_tag_core: float = 0.08
    ema_tag_outer: float = 0.45
    flag_impulse_outer_min: float = 0.15
    flag_impulse_core_min: float = 0.30
    flag_impulse_core_max: float = 1.80
    flag_impulse_outer_max: float = 3.0
    flag_pause_outer_min: float = 1
    flag_pause_core_min: float = 2
    flag_pause_core_max: float = 5
    flag_pause_outer_max: float = 8
    flag_range_core: float = 0.25
    flag_range_outer: float = 0.55
    late_outer_min: float = 0.20
    late_core_min: float = 0.50
    late_core_max: float = 1.80
    late_outer_max: float = 3.50
    # Model V scores and posts on this ATR window. 21 is the mean of the prior
    # daily true ranges (today excluded, at least 15). 14 restores the old window.
    atr_days: int = 21
    # Exceptional-stock override. Every check is required. Defaults are high so it is rare.
    override_move_atr: float = 2.5
    override_rvol: float = 3.0
    override_vwap_share: float = 0.80
    override_fresh_seconds: int = 30
    override_trigger_minutes: int = 5
    # Standby names stay in the quote batch. Chains run only while in play, and
    # not more often than this, so a gapping basket does not take the deep budget.
    standby_scan_minutes: int = 3
    standby_quote_minutes: int = 1
    standby_scans_per_cycle: int = 4
    quote_batch: int = 180
    hybrid: int = 0
    soft_blocks: int = 0
    repeat_penalty: int = 10
    tier_a: float = 91
    tier_b: float = 81
    send_tiers: str = "AB"
    send_requote: int = 0
    stock_push_score: float = 18
    stock_push_weight: float = 1
    stock_push_thin_weight: float = 1.5
    stock_push_cap: float = 30
    stock_weak_scale: float = 0.25
    # PLACEHOLDER weights until Vince's 3:00 PM ET pass. Env can replace each one.
    w_move_atr: float = 6
    w_rvol: float = 4
    w_held_pm: float = 6
    w_held_pd: float = 5
    w_held_range: float = 4
    w_vwap: float = 4
    w_fresh: float = 3
    w_spy: float = 3
    w_retest_first: float = 8
    w_retest_later: float = 3
    w_retest_wick: float = 4
    w_vwap_reclaim: float = 4
    w_retest_sync: float = 3
    w_contract_volume: float = 6
    w_premium_expansion: float = 6
    w_spread_penalty: float = 12
    w_tiny_premium: float = 8
    pen_wf: float = -25
    pen_late: float = -25
    pen_open: float = -10
    pen_exhq: float = -10
    pen_w1dte: float = -10
    pen_prem: float = -10
    w_hourly_break: float = 5
    event_dedupe_minutes: int = 5
    standby_gap: float = 0.015
    standby_move: float = 0.01
    standby_atr: float = 0.35
    standby_rvol: float = 1.5
    min_pace_rvol: float = 2.0
    min_local_rvol: float = 1.2
    min_local_acceleration: float = 1.2
    min_range_atr: float = .2
    min_option_acceleration: float = 1.3
    min_cluster: int = 2
    min_strike_volume_5m: int = 75
    confirmation_minutes: int = 1  # Two distinct completed one-minute observations.
    # Zero disables count limits. Same-leg deduplication and quality gates remain.
    max_alerts_per_day: int = 0
    max_alerts_per_ticker: int = 0
    max_alerts_per_cycle: int = 0
    rearm_minutes: int = 3
    max_quote_age_seconds: int = 90
    min_ask: float = 0.10
    max_ask: float = 2.50
    max_scaled_ask: float = 7.50
    premium_atr_fraction: float = .25
    max_spread_fraction: float = 0.15
    max_spread_dollars: float = 0.15
    min_delta: float = 0.10
    max_delta: float = 0.55
    max_dte: int = 1
    entry_cutoff_minutes: int = 15
    max_otm_atr: float = 1.0
    max_building_distance_atr: float = .06
    max_entry_extension_atr: float = .25
    min_opening_bars: int = 3
    active_quality_gate: int = 1  # Keep excluded ideas in candidate/decision history.

    def __post_init__(self):
        if any(not isfinite(value) for value in vars(self).values() if isinstance(value, (int, float))):
            raise ValueError("Settings must be finite")
        if self.model not in {"V", "S"}:
            raise ValueError("LOTTO_MODEL must be V or S")
        if self.model == "V" and self.min_score > 60:
            raise ValueError("Model V MIN_SCORE cannot be above 60")
        if (self.hybrid not in (0, 1) or self.soft_blocks not in (0, 1)
                or self.send_requote not in (0, 1) or self.repeat_penalty < 0
                or self.send_tiers not in ("A", "AB", "ABC")
                or not 0 < self.tier_b < self.tier_a <= 100):
            raise ValueError("Invalid hybrid or tier setting")
        if self.hybrid and (self.min_score > 60 or not self.soft_blocks):
            raise ValueError("Hybrid requires LOTTO_MIN_SCORE <= 60 and LOTTO_SOFT_BLOCKS=1")
        bars = (self.post_v2_option_0dte, self.post_v2_option_1dte,
                self.post_v2_stock_0dte, self.post_v2_stock_1dte)
        if min(bars) <= 0 or self.event_dedupe_minutes < 0:
            raise ValueError("Invalid Model V bar or dedupe window")
        if self.preset not in {"S0", "S11", "S12"}:
            raise ValueError("LOTTO_PRESET must be S0, S11, or S12")
        if self.score_mode not in {"combined", "s12"}:
            raise ValueError("LOTTO_SCORE_MODE must be combined or s12")
        if self.probability_path not in (0, 1):
            raise ValueError("probability_path must be 0 or 1")
        if not 0 < self.stock_max_share < 1:
            raise ValueError("stock_max_share must be between 0 and 1")
        if min(self.post_combined_0dte, self.post_combined_1dte) <= 0:
            raise ValueError("Invalid combined bar")
        floors = (self.strength_quintile, self.strength_decile,
                  self.pwin_normal_0dte, self.pwin_normal_1dte,
                  self.pwin_quintile_0dte, self.pwin_quintile_1dte,
                  self.pwin_decile, self.pwin_decile_1dte,
                  self.override_vwap_share)
        if any(not 0 <= value <= 1 for value in floors) or self.strength_quintile > self.strength_decile:
            raise ValueError("Invalid stock-strength cutoff or probability floor")
        if self.standby_names not in (0, 1):
            raise ValueError("LOTTO_STANDBY_NAMES must be on or off")
        if self.fallback_rule not in {"off", "log", "post", "on"}:
            raise ValueError("LOTTO_FALLBACK_RULE must be off, log, post, or on")
        if self.atr_days not in {14, 21}:
            raise ValueError("LOTTO_ATR_DAYS must be 14 or 21")
        if self.fb_rvol_min < 0 or self.fb_push_atr_min < 0 or self.fb_max_spread_pct <= 0:
            raise ValueError("Invalid fallback threshold")
        if self.fb_room_atr_max <= 0 or self.fb_min_minutes < 0:
            raise ValueError("Invalid fallback room or open delay")
        if self.fb_same_move_before_min < 0 or self.fb_same_move_after_min < 0:
            raise ValueError("Invalid fallback same-move window")
        if self.strong_score < 0 or self.stale_quote_seconds < 0 or self.stale_quote_penalty < 0:
            raise ValueError("Invalid strong-score or stale-quote setting")
        if self.base_volume_min < 0 or self.base_override_points < 0:
            raise ValueError("Invalid base-layer setting")
        if self.blocker_spread <= 0 or self.blocker_spread_override_points < 0:
            raise ValueError("Invalid blocker setting")
        if self.spread_legacy not in (0, 1) or self.blocker_spread_legacy <= 0:
            raise ValueError("Invalid spread legacy setting")
        if self.day_volume_swap not in (0, 1) or self.blocker_option_volume_day < 0:
            raise ValueError("Invalid day-volume swap")
        if self.optb not in (0, 1):
            raise ValueError("Invalid OPTB setting")
        if (not isinstance(self.stock_bar_tolerance, (int, float)) or isinstance(self.stock_bar_tolerance, bool)
                or not isfinite(self.stock_bar_tolerance) or not 0 <= self.stock_bar_tolerance < 1):
            raise ValueError("Invalid stock bar tolerance")
        if self.fallback_strike_offset < 0:
            raise ValueError("Invalid fallback strike offset")
        bands = tuple(self.spread_bands)
        bars = tuple(self.spread_stock_bars)
        if not bands or len(bars) != len(bands):
            raise ValueError("spread_stock_bars needs one bar per spread band edge")
        if any(not isinstance(edge, (int, float)) or not isfinite(edge) or edge <= 0 for edge in bands):
            raise ValueError("Invalid spread band")
        if any(bands[index] > bands[index + 1] for index in range(len(bands) - 1)):
            raise ValueError("Spread bands must be non-decreasing")
        for bar in bars:
            if len(bar) != 3:
                raise ValueError("Each spread stock bar is rvol, push, setups")
            rvol, push, setups = bar
            if (not isinstance(rvol, (int, float)) or not isinstance(push, (int, float))
                    or not isfinite(rvol) or not isfinite(push) or rvol < 0 or push < 0
                    or int(setups) < 0):
                raise ValueError("Invalid spread stock bar")
        if self.spread_legacy:
            object.__setattr__(self, "fallback_strike_offset", 0)
            object.__setattr__(self, "day_volume_swap", 0)
            object.__setattr__(self, "optb", 0)
        if (self.post_threshold < 0 or self.bypass_threshold < 0
                or self.strong_feature_min < 0 or self.bolt_min_score < 0
                or self.min_positive_features < 0):
            raise ValueError("Invalid post or bypass threshold")
        if self.blocker_bid < 0 or self.blocker_option_volume_5m < 0:
            raise ValueError("Invalid bid or option-volume blocker")
        if not 0 < self.strike_window_pct <= 1:
            raise ValueError("Invalid strike window")
        if self.conf_rvol_min < 0 or self.conf_atr_min < 0:
            raise ValueError("Invalid confluence floor")
        weights = (
            self.w_conf_rs, self.w_conf_pm_break, self.w_conf_pd_break, self.w_conf_rvol,
            self.w_conf_option_flow, self.w_conf_sector, self.w_conf_ema9_pullback,
            self.w_conf_range_break, self.w_conf_mtf_flag, self.w_conf_box5,
            self.w_conf_vwap, self.w_conf_atr_move, self.w_conf_consolidation, self.w_conf_bounce,
        )
        if any(weight < 0 for weight in weights):
            raise ValueError("Confluence weights cannot be negative")
        tier_weights = (self.tier_weight_primary, self.tier_weight_secondary, self.tier_weight_tertiary)
        if any(weight < 0 for weight in tier_weights) or self.bypass_confluence_min < 0:
            raise ValueError("Invalid confluence tier weight or bypass threshold")
        bands = (
            ("pattern bar", self.pattern_bars_outer_min, self.pattern_bars_core_min,
             self.pattern_bars_core_max, self.pattern_bars_outer_max),
            ("pattern depth", self.pattern_depth_outer_min, self.pattern_depth_core_min,
             self.pattern_depth_core_max, self.pattern_depth_outer_max),
            ("ema move", self.ema_move_outer_min, self.ema_move_core_min,
             self.ema_move_core_max, self.ema_move_outer_max),
            ("flag impulse", self.flag_impulse_outer_min, self.flag_impulse_core_min,
             self.flag_impulse_core_max, self.flag_impulse_outer_max),
            ("flag pause", self.flag_pause_outer_min, self.flag_pause_core_min,
             self.flag_pause_core_max, self.flag_pause_outer_max),
            ("fashionably late", self.late_outer_min, self.late_core_min,
             self.late_core_max, self.late_outer_max),
        )
        if any(not outer_min <= core_min <= core_max <= outer_max for _label, outer_min, core_min, core_max, outer_max in bands):
            raise ValueError("Invalid pattern band")
        if not 0 < self.pattern_trigger_min <= 1:
            raise ValueError("Invalid pattern trigger")
        if self.ema_pullback_period < 2 or not 0 <= self.ema_tag_core < self.ema_tag_outer:
            raise ValueError("Invalid EMA pullback band")
        if not 0 <= self.flag_range_core < self.flag_range_outer:
            raise ValueError("Invalid flag range band")
        if not 0 <= self.pattern_twin_core < self.pattern_twin_outer:
            raise ValueError("Invalid twin-extreme band")
        try:
            frames = [int(part.strip()) for part in self.pattern_timeframes.split(",") if part.strip()]
        except ValueError:
            frames = []
        if not frames or any(frame < 1 for frame in frames):
            raise ValueError("Invalid pattern timeframes")
        if min(self.override_move_atr, self.override_rvol) <= 0:
            raise ValueError("Invalid exceptional-stock threshold")
        if self.override_fresh_seconds <= 0 or self.override_trigger_minutes <= 0:
            raise ValueError("Invalid exceptional-stock freshness")
        if (self.standby_scan_minutes <= 0 or self.standby_quote_minutes <= 0
                or self.standby_scans_per_cycle <= 0 or self.quote_batch <= 0):
            raise ValueError("Invalid standby scan cadence")
        if self.stock_push_cap < 0 or self.stock_weak_scale < 0:
            raise ValueError("Invalid stock push weight")
        if not 0 < self.min_score <= 100 or min(self.min_pace_rvol,self.min_local_rvol,self.min_local_acceleration,self.min_range_atr,self.min_option_acceleration) <= 0:
            raise ValueError("Invalid score or RVOL threshold")
        for name in ("confirmation_minutes", "rearm_minutes",
                     "max_quote_age_seconds", "min_cluster", "min_strike_volume_5m"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("max_alerts_per_day", "max_alerts_per_ticker", "max_alerts_per_cycle"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.active_quality_gate not in (0, 1):
            raise ValueError("active_quality_gate must be 0 or 1")
        if not 0 < self.min_ask <= self.max_ask or not 0 < self.min_delta <= self.max_delta <= 1:
            raise ValueError("Invalid contract limits")
        if not 0 <= self.max_dte <= 1:
            raise ValueError("Invalid maximum expiration")
        if (not self.max_ask <= self.max_scaled_ask or self.premium_atr_fraction <= 0
                or not 0 < self.max_building_distance_atr <= self.max_entry_extension_atr
                or self.max_otm_atr <= 0 or not 3 <= self.min_opening_bars <= 11
                or not 0 < self.entry_cutoff_minutes < 390
                or not 0 < self.max_spread_fraction < 1 or self.max_spread_dollars <= 0):
            raise ValueError("Invalid phase-one quality limits")


# Probability floors are P(true win). They match hoF >= A - delta on the
# log-odds scale. Cutoffs are the spec's exact q80 and q90.
_STRENGTH = {
    "strength_quintile": 0.8016471997604073,
    "strength_decile": 0.8756064690026955,
    "pwin_normal_0dte": 0.43157386,
    "pwin_normal_1dte": 0.47114974,
}
PRESETS = {
    "S0": {
        "preset": "S0",
        "probability_path": 0,
        **_STRENGTH,
        "pwin_quintile_0dte": 0.28822075,
        "pwin_quintile_1dte": 0.30901921,
        "pwin_decile": 0.17760635,
        "pwin_decile_1dte": 0.18333891,
        "preset_note": "S0 keeps the 58/80 stock-led v2 bars. The probability path is off.",
    },
    "S11": {
        "preset": "S11",
        "probability_path": 1,
        **_STRENGTH,
        "pwin_quintile_0dte": 0.32149954,
        "pwin_quintile_1dte": 0.34696342,
        "pwin_decile": 0.22822777,
        "pwin_decile_1dte": 0.24061730,
        "preset_note": (
            "S11 handoff floors: normal 0.43157386/0.47114974, "
            "strength >= 0.8016471997604073 → 0.32149954/0.34696342, "
            "strength >= 0.8756064690026955 → 0.22822777/0.24061730."
        ),
    },
    "S12": {
        "preset": "S12",
        "probability_path": 1,
        **_STRENGTH,
        "pwin_quintile_0dte": 0.28822075,
        "pwin_quintile_1dte": 0.30901921,
        "pwin_decile": 0.17760635,
        "pwin_decile_1dte": 0.18333891,
        "preset_note": (
            "S12 handoff floors: normal 0.43157386/0.47114974, "
            "strength >= 0.8016471997604073 → 0.28822075/0.30901921, "
            "strength >= 0.8756064690026955 → 0.17760635/0.18333891. Hybrid v2 bars stay 40/61."
        ),
    },
}


def model_v_settings(**overrides) -> Settings:
    """Shipped Model V profile. Score mode defaults to s12. MIN_SCORE stays at 60."""
    preset = str(overrides.get("preset", "S12")).upper()
    if preset not in PRESETS:
        raise ValueError("LOTTO_PRESET must be S0, S11, or S12")
    values = dict(model="V", min_score=60, hybrid=1, soft_blocks=1, send_requote=1,
                  score_mode="s12", standby_names=0,
                  handoff_model_path=HANDOFF_SPEC_PATH,
                  strength_distribution_path=HANDOFF_SPEC_PATH)
    values.update(PRESETS[preset])
    values.update(overrides)
    values["preset"] = preset
    values["score_mode"] = str(values.get("score_mode", "s12")).lower()
    if "fallback_rule" in values:
        values["fallback_rule"] = str(values["fallback_rule"]).lower()
    if values["score_mode"] == "combined" and "preset_note" not in overrides:
        values["preset_note"] = (
            "Rollback: one combined score, bars 40/61, stock share cap 0.40, "
            "plus the exceptional stock_override path."
        )
    if values.get("min_score", 60) > 60:
        values["min_score"] = 60
    return Settings(**values)
