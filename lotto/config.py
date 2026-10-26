from dataclasses import dataclass
from math import isfinite

STRATEGY_VERSION = "model-v-2026-10-26"


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
    # Live default is one combined score and one bar per expiry.
    # PLACEHOLDER until Vince replaces the bar, the share cap, and the weights
    # (due 3:00 PM ET). A strong option clears the bar with no stock help.
    # stock_max_share keeps stock from being enough to clear the bar alone.
    score_mode: str = "combined"
    post_combined_0dte: float = 40
    post_combined_1dte: float = 61
    stock_max_share: float = 0.40
    # S12 is the optional two-path comparison (LOTTO_SCORE_MODE=s12).
    # Probability floors are P(true win), not v2 points.
    preset: str = "S12"
    probability_path: int = 1
    strength_quintile: float = 0.802
    strength_decile: float = 0.876
    pwin_quintile_0dte: float = 0.29
    pwin_quintile_1dte: float = 0.31
    pwin_decile: float = 0.18
    handoff_model_path: str = ""
    strength_distribution_path: str = ""
    preset_note: str = "combined PLACEHOLDER bars 40/61 and stock share cap 0.40 until Vince's 3:00 PM ET pass."
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
                  self.pwin_quintile_0dte, self.pwin_quintile_1dte, self.pwin_decile,
                  self.override_vwap_share)
        if any(not 0 <= value <= 1 for value in floors) or self.strength_quintile > self.strength_decile:
            raise ValueError("Invalid stock-strength cutoff or probability floor")
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


# Probability floors are P(true win), in [0, 1]. They are not v2 points.
# S11 floors are placeholders: Vince has not published them. 1.0 does not clear
# for a real fitted probability, so S11 does not open the strong-stock path.
PRESETS = {
    "S0": {
        "preset": "S0",
        "probability_path": 0,
        "strength_quintile": 0.802,
        "strength_decile": 0.876,
        "pwin_quintile_0dte": 0.29,
        "pwin_quintile_1dte": 0.31,
        "pwin_decile": 0.18,
        "preset_note": "S0 keeps the 58/80 stock-led v2 bars. The probability path is off.",
    },
    "S11": {
        "preset": "S11",
        "probability_path": 1,
        "strength_quintile": 0.802,
        "strength_decile": 0.876,
        # PLACEHOLDER — not a Vince measurement. Replace when S11 floors are published.
        "pwin_quintile_0dte": 1.0,
        "pwin_quintile_1dte": 1.0,
        "pwin_decile": 1.0,
        "preset_note": "S11 PLACEHOLDER floors are 1.0. Same structure as S12; floors are unpublished.",
    },
    "S12": {
        "preset": "S12",
        "probability_path": 1,
        "strength_quintile": 0.802,
        "strength_decile": 0.876,
        "pwin_quintile_0dte": 0.29,
        "pwin_quintile_1dte": 0.31,
        "pwin_decile": 0.18,
        "preset_note": "S12 probability floors: quintile 0.29 (0DTE) / 0.31 (1DTE), decile 0.18 either expiry.",
    },
}


def model_v_settings(**overrides) -> Settings:
    """Shipped Model V profile. Score mode defaults to combined. MIN_SCORE stays at 60."""
    preset = str(overrides.get("preset", "S12")).upper()
    if preset not in PRESETS:
        raise ValueError("LOTTO_PRESET must be S0, S11, or S12")
    values = dict(model="V", min_score=60, hybrid=1, soft_blocks=1, send_requote=1, score_mode="combined")
    values.update(PRESETS[preset])
    values.update(overrides)
    values["preset"] = preset
    values["score_mode"] = str(values.get("score_mode", "combined")).lower()
    if values["score_mode"] == "combined" and preset == "S12" and "preset_note" not in overrides:
        values["preset_note"] = (
            "combined PLACEHOLDER bars 40/61 and stock share cap 0.40 until Vince's 3:00 PM ET pass."
        )
    if values.get("min_score", 60) > 60:
        values["min_score"] = 60
    return Settings(**values)
