from dataclasses import asdict, dataclass
from math import isfinite

STRATEGY_VERSION = "phase1-three-bar-opening-drive-2026-10-14"
# Stamp stored with each decision. This is not a score-model version.
ATR_BASIS_VERSION = "rth14-true-range-vs-high-low-2026-11-09"
GATE_VERSION = "friday-fix-gates-2026-11-09"


@dataclass(frozen=True)
class Settings:
    # Research defaults, not fitted probabilities or proven thresholds.
    min_score: float = 70
    min_pace_rvol: float = 2.0
    min_local_rvol: float = 1.2
    min_local_acceleration: float = 1.2
    min_range_atr: float = .2
    min_option_acceleration: float = 1.3
    min_cluster: int = 2
    min_strike_volume_5m: int = 75
    confirmation_minutes: int = 1  # Two distinct completed one-minute observations.
    # Zero disables count limits. Distinct-setup, cooldown, and quality gates remain.
    max_alerts_per_day: int = 0
    max_alerts_per_ticker: int = 0
    max_alerts_per_cycle: int = 0
    ticker_cooldown_minutes: int = 30
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
    # Dead-market blocks (desk fix 2026-11-05, FINAL). Mode: 0 off, 1 shadow (log only, no alert change), 2 block.
    # Recommended live values: dead zone and burst-on-flat block (2); spent is shadow only (1).
    # Python defaults stay shadow (1) so a merge does not change live posts. Enable block later with
    # LOTTO_DEAD_ZONE_MODE=2 and LOTTO_BURST_FLAT_MODE=2. Spent stays LOTTO_SPENT_MODE=1.
    # Env: LOTTO_DEAD_ZONE_MODE, LOTTO_BURST_FLAT_MODE, LOTTO_SPENT_MODE and the threshold names below.
    # Thresholds were fit on Oct 2025-Jul 2026 cards using the 14-day high-low mean.
    # Live prior_atr is the 14-day true range. Modes 0 and 1 keep blocking on that
    # true range. High-low is telemetry unless dm_atr_basis_mode is 2.
    dead_zone_mode: int = 1
    dead_zone_start_minute: int = 90      # minutes after 09:30 (11:00 ET)
    dead_zone_end_minute: int = 300       # 14:30 ET
    dead_zone_max_net30_atr: float = .12  # directional close-to-close move over last 30 bars
    dead_zone_max_vwap_atr: float = .226  # |close - VWAP|
    burst_flat_mode: int = 1
    burst_flat_start_minute: int = 90
    burst_flat_min_volume_ratio: float = 1.268  # mean vol last 5 bars / mean vol prior 25 bars
    burst_flat_max_net5_atr: float = .116       # directional move over last 5 bars
    spent_mode: int = 1
    spent_start_minute: int = 90
    spent_min_range_atr: float = 1.061      # session high-low already spent
    spent_min_off_extreme_atr: float = .035 # not printing a fresh extreme on the card side
    # 0DTE only: direction*(strike-spot)/ATR / sqrt(minutes_left/390) >= max.
    # ATR for this gate is the high-low mean when it is present, else true range.
    # Mode 0 off, 1 log only (default), 2 block. The 0.8 cutoff was picked after
    # seeing every split, so this build does not turn mode 2 on by itself.
    # LOTTO_FAR_OTM_TIME_MODE, LOTTO_FAR_OTM_TIME_MAX_ATR.
    far_otm_time_mode: int = 1
    far_otm_time_max_atr: float = .8
    # 0 = true-range blocks only. 1 = same blocks, plus high-low flags (default).
    # 2 = block on the high-low mean. Not the default: live true range is what is
    # blocking today, and the Friday high-low recount was about 4/41.
    # LOTTO_DM_ATR_BASIS_MODE.
    dm_atr_basis_mode: int = 1
    # Operational only. The loader casts with type(default)(raw); bool("0") is True.
    day_export: int = 0
    day_export_hour: int = 16
    day_export_minute: int = 20
    day_export_backfill_days: int = 5
    # 0 uploads to Google Drive. 1 uploads to S3. Integer for the same cast reason.
    day_export_target: int = 0

    def __post_init__(self):
        if any(not isfinite(value) for value in vars(self).values()):
            raise ValueError("Settings must be finite")
        if not 0 < self.min_score <= 100 or min(self.min_pace_rvol,self.min_local_rvol,self.min_local_acceleration,self.min_range_atr,self.min_option_acceleration) <= 0:
            raise ValueError("Invalid score or RVOL threshold")
        for name in ("confirmation_minutes", "ticker_cooldown_minutes", "rearm_minutes",
                     "max_quote_age_seconds", "min_cluster", "min_strike_volume_5m"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("max_alerts_per_day", "max_alerts_per_ticker", "max_alerts_per_cycle"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if not 0 < self.min_ask <= self.max_ask or not 0 < self.min_delta <= self.max_delta <= 1:
            raise ValueError("Invalid contract limits")
        if any(getattr(self, n) not in (0, 1, 2) for n in (
                "dead_zone_mode", "burst_flat_mode", "spent_mode", "far_otm_time_mode", "dm_atr_basis_mode")):
            raise ValueError("Gate modes must be 0, 1, or 2")
        if self.far_otm_time_max_atr <= 0:
            raise ValueError("far_otm_time_max_atr must be positive")
        if not 0 <= self.max_dte <= 1:
            raise ValueError("Invalid maximum expiration")
        if (not self.max_ask <= self.max_scaled_ask or self.premium_atr_fraction <= 0
                or not 0 < self.max_building_distance_atr <= self.max_entry_extension_atr
                or self.max_otm_atr <= 0 or not 3 <= self.min_opening_bars <= 11
                or not 0 < self.entry_cutoff_minutes < 390
                or not 0 < self.max_spread_fraction < 1 or self.max_spread_dollars <= 0):
            raise ValueError("Invalid phase-one quality limits")
        if type(self.day_export) is not int or self.day_export not in (0, 1):
            raise ValueError("day_export must be 0 or 1")
        if type(self.day_export_hour) is not int or not 0 <= self.day_export_hour <= 23:
            raise ValueError("Invalid day export hour")
        if type(self.day_export_minute) is not int or not 0 <= self.day_export_minute <= 59:
            raise ValueError("Invalid day export minute")
        if type(self.day_export_backfill_days) is not int or not 0 <= self.day_export_backfill_days <= 366:
            raise ValueError("Invalid day export backfill")
        if type(self.day_export_target) is not int or self.day_export_target not in (0, 1):
            raise ValueError("day_export_target must be 0 or 1")


def recorded_settings(settings: Settings) -> dict:
    """Strategy thresholds stored on candidate rows. Export flags stay operational."""
    return {key: value for key, value in asdict(settings).items() if not key.startswith("day_export")}
