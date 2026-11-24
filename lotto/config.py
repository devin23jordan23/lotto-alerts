from dataclasses import asdict, dataclass
from math import isfinite

STRATEGY_VERSION = "phase1-three-bar-opening-drive-2026-10-14"


def _valid_hhmm(value) -> bool:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    hour, minute = value.split(":")
    if not (hour.isdigit() and minute.isdigit()):
        return False
    return 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59


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
    # Thresholds fit on Oct 2025-Jul 2026 cards only; units are prior ATR (14-day mean daily range).
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
    # FINAL-B. Mode: 0 off (byte-identical to the live path), 1 shadow (compute and log,
    # change nothing posted), 2 on. Env: LOTTO_<FIELD>. Discovery stays shadow-only at mode 2.
    level_side_rule_mode: int = 0
    level_break_override_mode: int = 0
    level_break_min_local_rvol: float = 1.5
    cooldown_mode: int = 0
    extreme_rearm_min_minutes: int = 5
    extreme_rearm_atr: float = 0.05
    after14_mode: int = 0
    chase_block_mode: int = 0
    ext_penalty_mode: int = 0
    ext_penalty_points: int = 15
    discovery_priority_mode: int = 0
    # Shadow study of the FINAL-B re-arm only. 0 off. 1 logs both a 0.10 ATR and a
    # 0.15 ATR gate on a new HOD/LOD inside the 10-minute cooldown. No mode 2:
    # this never changes a live card, a Discord post, the 30-minute cooldown, or
    # any other FINAL-B mode. Env: LOTTO_REARM_ATR_MODE.
    rearm_atr_mode: int = 0
    # Scanner fixes (SCANNER-FIXES-1124). Each mode is 0 off, 1 shadow, 2 on.
    # Env: LOTTO_<FIELD>. Flag values are left out of recorded_settings.
    # late_move_override_mode 1 keeps a fresh PM/PD break on 1.5x 2-bar volume.
    late_move_mode: int = 0
    late_move_atr: float = 1.1
    late_move_override_mode: int = 1
    late_move_override_rvol: float = 1.5
    mkt_gate_mode: int = 0
    mkt_gate_th5: float = 0.05
    mkt_gate_th10: float = 0.075
    mkt_gate_th15: float = 0.10
    stock_first_mode: int = 0
    stock_first_cap: int = 2
    fast_run_mode: int = 0
    fast_run_mkt: str = "alone"
    fast_run_atr: float = 0.20
    fast_run_straight: float = 0.60
    # After this clock, a non-index card needs a completed-bar HOD/LOD, PM, or PD break.
    afternoon_level_mode: int = 0
    afternoon_level_start: str = "12:30"
    # Ops. 1 and 2 both enable the direct-refresh fallback, the 10-minute repeat
    # notice, and the 20-minute self-restart. Schwab reads the same env var.
    token_fallback_mode: int = 0
    # Latency (SCANNER-LATENCY-1124). Defaults stay off. 1 logs, 2 is live for the
    # bar-close cycle and the options-history seed. Persistence is not changed.
    latency_log: int = 0
    bar_close_eval_mode: int = 0
    bar_close_delay_s: int = 3
    hot_max: int = 12
    schwab_rps: float = 1.8
    oh_seed_mode: int = 0
    oh_seed_max_gap_s: int = 200
    # Operational only. The loader casts with type(default)(raw); bool("0") is True.
    day_export: int = 0
    day_export_hour: int = 16
    day_export_minute: int = 20
    day_export_backfill_days: int = 5
    # 0 uploads to Google Drive. 1 uploads to S3. Integer for the same cast reason.
    day_export_target: int = 0
    # Operational only. 1 archives old nightly/forensic files to the export bucket, then frees them.
    space_cleanup: int = 0
    # 0 leaves live.sqlite3 unchanged. N keeps today plus N weekdays and prunes older high-churn rows.
    db_prune_keep_days: int = 0
    # 1 also prunes days older than the oldest day-export object (archived before exports existed).
    db_prune_allow_pre_export: int = 0

    def __post_init__(self):
        if any(not isfinite(value) for value in vars(self).values()
               if isinstance(value, (int, float)) and not isinstance(value, bool)):
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
        if any(getattr(self, n) not in (0, 1, 2) for n in ("dead_zone_mode", "burst_flat_mode", "spent_mode")):
            raise ValueError("Dead-market block modes must be 0, 1 or 2")
        finalb_modes = ("level_side_rule_mode", "level_break_override_mode", "cooldown_mode",
                        "after14_mode", "chase_block_mode", "ext_penalty_mode", "discovery_priority_mode")
        if any(type(getattr(self, name)) is not int or getattr(self, name) not in (0, 1, 2) for name in finalb_modes):
            raise ValueError("FINAL-B modes must be 0, 1 or 2")
        if self.level_break_min_local_rvol <= 0 or self.extreme_rearm_atr <= 0:
            raise ValueError("Invalid FINAL-B threshold")
        if type(self.extreme_rearm_min_minutes) is not int or self.extreme_rearm_min_minutes <= 0:
            raise ValueError("extreme_rearm_min_minutes must be a positive integer")
        if type(self.ext_penalty_points) is not int or self.ext_penalty_points < 0:
            raise ValueError("ext_penalty_points must be a non-negative integer")
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
        if type(self.space_cleanup) is not int or self.space_cleanup not in (0, 1):
            raise ValueError("space_cleanup must be 0 or 1")
        if type(self.rearm_atr_mode) is not int or self.rearm_atr_mode not in (0, 1):
            raise ValueError("rearm_atr_mode must be 0 or 1")
        fix_modes = ("late_move_mode", "mkt_gate_mode", "stock_first_mode", "fast_run_mode",
                     "token_fallback_mode", "afternoon_level_mode")
        if any(type(getattr(self, name)) is not int or getattr(self, name) not in (0, 1, 2) for name in fix_modes):
            raise ValueError("scanner fix modes must be 0, 1 or 2")
        if type(self.late_move_override_mode) is not int or self.late_move_override_mode not in (0, 1):
            raise ValueError("late_move_override_mode must be 0 or 1")
        if type(self.stock_first_cap) is not int or self.stock_first_cap <= 0:
            raise ValueError("stock_first_cap must be a positive integer")
        if self.fast_run_mkt not in ("all", "spiked", "alone"):
            raise ValueError("fast_run_mkt must be all, spiked, or alone")
        if not _valid_hhmm(self.afternoon_level_start):
            raise ValueError("afternoon_level_start must be HH:MM in ET")
        if not (self.late_move_atr > 0 and self.late_move_override_rvol > 0 and self.fast_run_atr > 0
                and 0 < self.fast_run_straight <= 1 and self.mkt_gate_th5 > 0
                and self.mkt_gate_th10 > 0 and self.mkt_gate_th15 > 0):
            raise ValueError("Invalid scanner-fix threshold")
        if type(self.latency_log) is not int or self.latency_log not in (0, 1):
            raise ValueError("latency_log must be 0 or 1")
        if type(self.bar_close_eval_mode) is not int or self.bar_close_eval_mode not in (0, 1, 2):
            raise ValueError("bar_close_eval_mode must be 0, 1 or 2")
        if type(self.oh_seed_mode) is not int or self.oh_seed_mode not in (0, 1, 2):
            raise ValueError("oh_seed_mode must be 0, 1 or 2")
        if type(self.bar_close_delay_s) is not int or not 0 <= self.bar_close_delay_s < 60:
            raise ValueError("bar_close_delay_s must be a whole number of seconds under 60")
        if type(self.hot_max) is not int or self.hot_max <= 0:
            raise ValueError("hot_max must be a positive integer")
        if type(self.oh_seed_max_gap_s) is not int or self.oh_seed_max_gap_s < 150:
            raise ValueError("oh_seed_max_gap_s must be at least 150")
        if not 0 < self.schwab_rps <= (1 / 0.55) + 1e-9:
            raise ValueError("schwab_rps cannot exceed today's request rate")
        if type(self.db_prune_keep_days) is not int or self.db_prune_keep_days < 0:
            raise ValueError("db_prune_keep_days must be a non-negative integer")
        if type(self.db_prune_allow_pre_export) is not int or self.db_prune_allow_pre_export not in (0, 1):
            raise ValueError("db_prune_allow_pre_export must be 0 or 1")


# Mode flags are operational. A Railway change must not rewrite the settings hash
# stored on candidate rows. Thresholds stay, so a shadow row still records the cutoff.
_UNRECORDED_FLAGS = {
    "rearm_atr_mode",
    "late_move_mode",
    "late_move_override_mode",
    "mkt_gate_mode",
    "stock_first_mode",
    "fast_run_mode",
    "fast_run_mkt",
    "token_fallback_mode",
    "afternoon_level_mode",
    "latency_log",
    "bar_close_eval_mode",
    "oh_seed_mode",
}


def recorded_settings(settings: Settings) -> dict:
    """Strategy thresholds stored on candidate rows. Export and mode flags stay operational."""
    return {key: value for key, value in asdict(settings).items()
            if key not in _UNRECORDED_FLAGS and not key.startswith(("day_export", "space_cleanup", "db_prune"))}
