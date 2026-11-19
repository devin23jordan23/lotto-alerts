import logging
from collections import defaultdict
from datetime import datetime, timedelta

from .config import Settings
from .discord import alert_payload
from .features import candidates
from .finalb import (
    COOLDOWN_REASON,
    finalb_cooldown_blocks,
    finalb_enabled,
    finalb_overlay,
    live_cooldown_blocks,
    promotion_bypasses,
    rearm_atr_variant,
    session_extreme,
)
from .models import ET, Snapshot
from .resilience import gap_is_bridgeable, note_feed_time
from .store import Store

LOG = logging.getLogger(__name__)


class Engine:
    def __init__(self, store: Store, settings: Settings | None = None, dry_run: bool = True):
        self.store = store
        self.settings = settings or Settings()
        self.dry_run = dry_run
        self.history = {}
        self.scores = defaultdict(list)
        self.qualified_since = {}
        self.last_evaluation = {}
        self.last_bar = {}
        self.cool_since = {}
        self.last_cooling = {}
        self.rearmed = set()
        # FINAL-B shadow cooldown. Never read by the live 30-minute path.
        self.finalb_scores = defaultdict(list)
        self.finalb_qualified_since = {}
        self.finalb_last_evaluation = {}
        self.finalb_cool_since = {}
        self.finalb_last_cooling = {}
        self.finalb_rearmed = set()
        self.finalb_last = {}
        self._cycle_shadow = []

    def process(self, snapshots: list[Snapshot]) -> list[dict]:
        ready = []
        cfg = self.settings
        self._cycle_shadow = []
        for snap in snapshots:
            note_feed_time(snap.at)
            key = (snap.day, snap.symbol)
            if key not in self.history:
                self.history[key] = [s for s in self.store.recent(snap.symbol, snap.day)
                                     if not s.problems(cfg.max_quote_age_seconds, min_bars=1)]
            history = self.history[key]
            if history and snap.at <= history[-1].at:
                continue
            self.store.record(snap)
            self.store.track(snap, cfg)
            local = snap.at.astimezone(ET)
            early_opening_warmup = (local.hour == 9 and 33 <= local.minute < 35
                                    and cfg.min_opening_bars <= 5)
            required_bars = 3 if early_opening_warmup else cfg.min_opening_bars
            problems = snap.problems(cfg.max_quote_age_seconds, min_bars=required_bars)
            if problems:
                if not snap.problems(cfg.max_quote_age_seconds, min_bars=1):
                    history.append(snap)  # Capture opening options history before price setup warmup.
                self.store.decision(snap, "DISCOVERY", "; ".join(problems))
                self._clear_confirmation(key)
                self._shadow_clear(key)
                self.cool_since.pop(key, None)
                continue
            # Polling twice within the same completed bar cannot count as persistence.
            if self.last_bar.get(key) == snap.bars[-1].end:
                continue
            self.last_bar[key] = snap.bars[-1].end
            gap_seconds = (snap.at - history[-1].at).total_seconds() if history else 0.0
            evaluated = candidates(snap, history, cfg)
            self.store.record_candidates(snap, evaluated, cfg)
            history.append(snap)
            self.history[key] = [s for s in history if snap.at - s.at <= timedelta(minutes=25)]
            valid = [c for c in evaluated if c.qualifying]
            best = max(evaluated, key=lambda c: c.score, default=None)
            if not valid:
                self._clear_confirmation(key)
                if best is not None:
                    if key in self.last_cooling and snap.at - self.last_cooling[key] > timedelta(seconds=150):
                        self.cool_since.pop(key, None)
                    self.last_cooling[key] = snap.at
                    self.cool_since.setdefault(key, snap.at)
                    if snap.at - self.cool_since[key] >= timedelta(minutes=cfg.rearm_minutes):
                        self.rearmed.add(key)
                else:
                    self.cool_since.pop(key, None)
                self.store.decision(snap, "COOLING" if self.store.last_alert(*key) else "DISCOVERY",
                                    "; ".join(best.blockers) if best else "options history, liquidity, or neighboring-strike activity unavailable", best)
                self._note_shadow(snap, evaluated, key, False)
                continue
            self.cool_since.pop(key, None)
            best = max(valid, key=lambda c: c.score)
            # A reclaim becoming a continuation is still the same directional
            # thesis. Require distinct completed bars, not a perfect poll timer.
            score_key = (*key, best.option.side, best.option.expiry.isoformat())
            for other in list(self.qualified_since):
                if other[:2] == key and other != score_key:
                    self.qualified_since.pop(other, None)
            if score_key in self.last_evaluation and snap.at - self.last_evaluation[score_key] > timedelta(seconds=150):
                self.qualified_since.pop(score_key, None)
            self.last_evaluation[score_key] = snap.at
            prior_scores = self.scores[score_key]
            old = [s for s in prior_scores if snap.at - s[0] >= timedelta(minutes=3)]
            best.score_change = best.score - old[-1][1] if old else 0
            prior_scores.append((snap.at, best.score))
            self.scores[score_key] = [s for s in prior_scores if snap.at - s[0] <= timedelta(minutes=10)]
            self.qualified_since.setdefault(score_key, snap.bars[-1].end)
            local = snap.at.astimezone(ET)
            opening_fast_lane = (local.hour == 9 and 33 <= local.minute < 60
                                 and best.setup is not None and best.setup.name == "OPENING_DRIVE"
                                 and not best.setup.building and best.score >= 82)
            opening_fast_lane = opening_fast_lane or bool(best.metrics.get("opening_expansion"))
            awaiting_confirmation = (snap.bars[-1].end - self.qualified_since[score_key]
                                     < timedelta(minutes=cfg.confirmation_minutes))
            if (awaiting_confirmation and not opening_fast_lane and gap_seconds > 150
                    and gap_is_bridgeable(snap.at - timedelta(seconds=gap_seconds), snap.at, "LOTTO_GAP_CONFIRM_MODE")
                    and bars_confirm_after_gap(snap, best)):
                # The feed gap, not the setup, interrupted the persistence check. Two completed
                # one-minute bars already closed through the trigger on the card side.
                # best.option was required to get here, so the card is still that contract.
                awaiting_confirmation = False
                best.metrics["gap_bar_confirmation"] = 1
            if awaiting_confirmation and not opening_fast_lane:
                self.store.decision(snap, "IGNITION", "confirming persistence", best)
                self._note_shadow(snap, evaluated, key, False)
                continue
            if awaiting_confirmation and opening_fast_lane:
                best.metrics["opening_fast_lane"] = 1
            # Flat strong scores can persist; materially deteriorating scores cannot alert.
            if best.score_change < -5:
                self.store.decision(snap, "COOLING", "score deteriorating", best)
                self._note_shadow(snap, evaluated, key, False)
                continue
            close = snap.session_end or snap.at.astimezone(ET).replace(hour=16, minute=0, second=0, microsecond=0)
            if close - snap.at <= timedelta(minutes=cfg.entry_cutoff_minutes):
                self.store.decision(snap, "RUNNER", "entry cutoff; tracking only", best)
                self._note_shadow(snap, evaluated, key, False)
                continue
            phase = "POTENTIAL" if best.setup.building else "ACTIVE"
            last = self.store.last_alert(*key)
            # A watch becoming a confirmed setup is a new, separately measured
            # alert, not a duplicate blocked by the ticker cooldown.
            promotion = bool(last and phase == "ACTIVE" and last["phase"] == "POTENTIAL"
                             and last["side"] == best.option.side and key not in self.rearmed)
            if last:
                best.state = "ACTIVATION" if promotion else "RUNNER"
                if not promotion and cfg.cooldown_mode == 2:
                    blocked, _rearmed = finalb_cooldown_blocks(
                        snap.at, last, best.option.side, key in self.rearmed, snap, cfg)
                    if blocked:
                        self.store.decision(snap, "RUNNER", "cooldown or fresh reset required", best)
                        self._note_shadow(snap, evaluated, key, False)
                        continue
                elif not promotion and (snap.at - datetime.fromisoformat(last["at"]) < timedelta(minutes=cfg.ticker_cooldown_minutes)
                        or (key not in self.rearmed and last["side"] == best.option.side)):
                    self.store.decision(snap, "RUNNER", "cooldown or fresh reset required", best)
                    self._note_shadow(snap, evaluated, key, False)
                    continue
            else:
                best.state = "IGNITION"
            # A previously alerted contract may form a genuinely new leg after
            # cooldown and a recorded reset.
            ready.append(best)
            self._note_shadow(snap, evaluated, key, True)
        alerts = []
        fired = set()
        # Rank the whole scan cycle. Zero-valued count settings impose no budget.
        for candidate in sorted(ready, key=lambda c: (-(c.score + 2*min(8, max(-8, c.score_change))), -c.score, c.snapshot.symbol)):
            snap = candidate.snapshot
            if ((cfg.max_alerts_per_cycle and len(alerts) >= cfg.max_alerts_per_cycle)
                    or (cfg.max_alerts_per_day and self.store.count(snap.day) >= cfg.max_alerts_per_day)
                    or (cfg.max_alerts_per_ticker and self.store.count(snap.day, snap.symbol) >= cfg.max_alerts_per_ticker)):
                self.store.decision(snap, candidate.state, "alert budget/ranking suppressed", candidate)
                continue
            phase = "POTENTIAL" if candidate.setup.building else "ACTIVE"
            last = self.store.last_alert(snap.day, snap.symbol)
            parent_id = (last["id"] if candidate.state == "ACTIVATION" and last else None)
            payload = alert_payload(candidate, phase, parent_id)
            extreme = session_extreme(snap, candidate.option.side) if cfg.cooldown_mode == 2 else None
            ident = self.store.queue(candidate, payload, self.dry_run, phase, parent_id, session_extreme=extreme)
            self.rearmed.discard((snap.day, snap.symbol))
            self.store.decision(snap, candidate.state, f"{phase.lower()} alert {ident}", candidate)
            fired.add((snap.day, snap.symbol))
            alerts.append({"id": ident, "phase": phase, "parent_id": parent_id, "payload": payload})
        self._flush_shadow(fired)
        return alerts

    def _clear_confirmation(self, key):
        for score_key in list(self.qualified_since):
            if score_key[:2] == key:
                self.qualified_since.pop(score_key, None)

    def _note_shadow(self, snap, evaluated, key, live_pre_ready):
        if not finalb_enabled(self.settings):
            return
        self._cycle_shadow.append((snap, evaluated, key, live_pre_ready))

    def _shadow_clear(self, key):
        if not finalb_enabled(self.settings):
            return
        try:
            for score_key in list(self.finalb_qualified_since):
                if score_key[:2] == key:
                    self.finalb_qualified_since.pop(score_key, None)
        except Exception:
            LOG.exception("FINAL-B shadow clear failed for %s; live path unchanged", key)

    def _flush_shadow(self, fired):
        if not self._cycle_shadow:
            return
        try:
            for snap, evaluated, key, live_pre_ready in self._cycle_shadow:
                self._shadow_snapshot(snap, evaluated, key, live_pre_ready, key in fired)
        except Exception:
            LOG.exception("FINAL-B shadow log failed; live path unchanged")
        finally:
            self._cycle_shadow = []

    def _shadow_snapshot(self, snap, evaluated, key, live_pre_ready, live_fired):
        try:
            self._shadow_snapshot_inner(snap, evaluated, key, live_pre_ready, live_fired)
        except Exception:
            LOG.exception("FINAL-B shadow failed for %s; live path unchanged", snap.symbol)

    def _ensure_overlay(self, candidate):
        if candidate.finalb is not None:
            return
        side = candidate.option.side
        live_score = candidate.live_score if candidate.live_score is not None else candidate.score
        live_blockers = candidate.live_blockers if candidate.live_blockers is not None else candidate.blockers
        candidate.live_score = live_score
        candidate.live_blockers = live_blockers
        candidate.finalb = finalb_overlay(
            candidate.snapshot, side, live_score, live_blockers, candidate.metrics,
            candidate.option, candidate.setup, self.settings, min_mode=1)

    def _shadow_snapshot_inner(self, snap, evaluated, key, live_pre_ready, live_fired):
        import json
        cfg = self.settings
        for candidate in evaluated:
            self._ensure_overlay(candidate)
        finalb_valid = [c for c in evaluated if c.finalb and c.finalb["qualifying"]]
        best = max(finalb_valid, key=lambda c: c.finalb["score"], default=None)
        prior_fb = self.finalb_last.get(key)
        prior_fb = dict(prior_fb) if prior_fb else None
        ready, gate_reason, best_cooldown = self._shadow_gate(snap, key, best, evaluated)
        last = self.store.last_alert(*key)
        fb_last = prior_fb
        # Two ATR gates on this re-arm only. Absent unless the flag is on and this
        # fire is a new HOD/LOD inside the 10-minute cooldown. Never feeds the gate.
        variant = None
        if (ready and best is not None and best_cooldown is not None and best_cooldown[1]
                and cfg.rearm_atr_mode == 1):
            variant = rearm_atr_variant(snap.at, fb_last, best.option.side, snap, cfg)
        picked = best.option.symbol if best is not None else ""
        rows = []
        for candidate in evaluated:
            side = candidate.option.side
            live_blockers = candidate.live_blockers if candidate.live_blockers is not None else candidate.blockers
            live_score = candidate.live_score if candidate.live_score is not None else candidate.score
            overlay = candidate.finalb
            fb_blockers = list(overlay["blockers"])
            fb_decision = "pass" if overlay["qualifying"] else "block"
            cooldown = self._cooldown_state(snap, key, candidate, last, fb_last, best, ready, best_cooldown)
            if candidate is best and gate_reason == COOLDOWN_REASON:
                fb_decision = "block"
                if COOLDOWN_REASON not in fb_blockers:
                    fb_blockers.append(COOLDOWN_REASON)
            flags = dict(overlay["flags"])
            flags["cooldown"] = int(bool(cooldown.get("finalb_cooldown_blocks")))
            flags["extreme_rearm"] = int(bool(cooldown.get("extreme_rearm")))
            flags["finalb_picked_contract"] = picked
            row = {
                "ts": snap.at.isoformat(),
                "ticker": snap.symbol,
                "side": side,
                "live_decision": "pass" if not live_blockers else "block",
                "live_block_reasons": json.dumps(list(live_blockers)),
                "finalb_decision": fb_decision,
                "finalb_block_reasons": json.dumps(fb_blockers),
                "live_score": live_score,
                "finalb_score": overlay["score"],
                "rule_flags": json.dumps(flags),
                "cooldown_state": json.dumps(cooldown),
                "contract": candidate.option.symbol,
            }
            if candidate is best and variant is not None:
                row["rearm_atr_010"], row["rearm_atr_015"] = variant
            rows.append(row)
        self.store.record_finalb_shadow(rows)
        if live_fired and not ready:
            blocked = best.option.side if best is not None else (evaluated[0].option.side if evaluated else "")
            LOG.info("FINAL-B would block %s %s (live fired)", snap.symbol, blocked)
        if ready and not live_fired and not live_pre_ready:
            why = gate_reason or ("; ".join(best.live_blockers or best.blockers) if best is None else "")
            if best is not None and not best.finalb["qualifying"]:
                why = "; ".join(best.finalb["blockers"])
            elif not live_pre_ready:
                live_best = max(evaluated, key=lambda c: c.live_score if c.live_score is not None else c.score, default=None)
                live_reasons = ()
                if live_best is not None:
                    live_reasons = live_best.live_blockers if live_best.live_blockers is not None else live_best.blockers
                why = "; ".join(live_reasons) or "cooldown or fresh reset required"
            LOG.info("FINAL-B would fire %s %s (live blocked: %s)", snap.symbol, best.option.side, why or "cooldown or fresh reset required")

    def _cooldown_state(self, snap, key, candidate, last, fb_last, best, ready, best_cooldown):
        side = candidate.option.side
        state = {
            "applied": candidate is best,
            "live_rearmed": key in self.rearmed,
            "finalb_rearmed": key in self.finalb_rearmed,
            "live_last_at": last["at"] if last else None,
            "live_last_side": last["side"] if last else None,
            "live_last_extreme": None,
            "finalb_last_at": fb_last["at"].isoformat() if fb_last else None,
            "finalb_last_side": fb_last["side"] if fb_last else None,
            "finalb_last_extreme": fb_last["extreme"] if fb_last else None,
            "session_extreme": session_extreme(snap, side),
            "finalb_ready": bool(ready and candidate is best),
            "extreme_rearm": False,
            "live_cooldown_blocks": False,
            "finalb_cooldown_blocks": False,
        }
        if last is not None and "session_extreme" in last.keys():
            state["live_last_extreme"] = last["session_extreme"]
        if last is not None:
            state["live_cooldown_blocks"] = live_cooldown_blocks(
                snap.at, datetime.fromisoformat(last["at"]), last["side"], side, key in self.rearmed, self.settings)
        if candidate is best and best_cooldown is not None:
            state["finalb_cooldown_blocks"], state["extreme_rearm"] = best_cooldown
        elif fb_last is not None:
            phase = "POTENTIAL" if candidate.setup and candidate.setup.building else "ACTIVE"
            if promotion_bypasses(fb_last, phase, side, key in self.finalb_rearmed):
                state["finalb_cooldown_blocks"] = False
            else:
                blocked, rearm = finalb_cooldown_blocks(
                    snap.at, fb_last, side, key in self.finalb_rearmed, snap, self.settings)
                state["finalb_cooldown_blocks"] = blocked
                state["extreme_rearm"] = rearm
        return state

    def _shadow_gate(self, snap, key, best, evaluated):
        """Mirror confirmation, cutoff, and the 10-minute cooldown on shadow state only."""
        cfg = self.settings
        if best is None:
            self._shadow_clear(key)
            any_candidate = max(evaluated, key=lambda c: c.finalb["score"], default=None) if evaluated else None
            if any_candidate is not None:
                if key in self.finalb_last_cooling and snap.at - self.finalb_last_cooling[key] > timedelta(seconds=150):
                    self.finalb_cool_since.pop(key, None)
                self.finalb_last_cooling[key] = snap.at
                self.finalb_cool_since.setdefault(key, snap.at)
                if snap.at - self.finalb_cool_since[key] >= timedelta(minutes=cfg.rearm_minutes):
                    self.finalb_rearmed.add(key)
            else:
                self.finalb_cool_since.pop(key, None)
            return False, None, None
        self.finalb_cool_since.pop(key, None)
        side = best.option.side
        score_key = (*key, side, best.option.expiry.isoformat())
        for other in list(self.finalb_qualified_since):
            if other[:2] == key and other != score_key:
                self.finalb_qualified_since.pop(other, None)
        if score_key in self.finalb_last_evaluation and snap.at - self.finalb_last_evaluation[score_key] > timedelta(seconds=150):
            self.finalb_qualified_since.pop(score_key, None)
        self.finalb_last_evaluation[score_key] = snap.at
        fb_score = best.finalb["score"]
        prior = self.finalb_scores[score_key]
        old = [item for item in prior if snap.at - item[0] >= timedelta(minutes=3)]
        change = fb_score - old[-1][1] if old else 0
        prior.append((snap.at, fb_score))
        self.finalb_scores[score_key] = [item for item in prior if snap.at - item[0] <= timedelta(minutes=10)]
        self.finalb_qualified_since.setdefault(score_key, snap.bars[-1].end)
        local = snap.at.astimezone(ET)
        opening = best.finalb.get("opening_expansion")
        if opening is None:
            opening = best.metrics.get("opening_expansion")
        fast = (local.hour == 9 and 33 <= local.minute < 60
                and best.setup is not None and best.setup.name == "OPENING_DRIVE"
                and not best.setup.building and fb_score >= 82) or bool(opening)
        awaiting = snap.bars[-1].end - self.finalb_qualified_since[score_key] < timedelta(minutes=cfg.confirmation_minutes)
        if awaiting and not fast:
            return False, "confirming persistence", None
        if change < -5:
            return False, "score deteriorating", None
        close = snap.session_end or snap.at.astimezone(ET).replace(hour=16, minute=0, second=0, microsecond=0)
        if close - snap.at <= timedelta(minutes=cfg.entry_cutoff_minutes):
            return False, "entry cutoff; tracking only", None
        phase = "POTENTIAL" if best.setup and best.setup.building else "ACTIVE"
        fb_last = self.finalb_last.get(key)
        cooldown = (False, False)
        if fb_last is not None and not promotion_bypasses(fb_last, phase, side, key in self.finalb_rearmed):
            cooldown = finalb_cooldown_blocks(snap.at, fb_last, side, key in self.finalb_rearmed, snap, cfg)
            if cooldown[0]:
                return False, COOLDOWN_REASON, cooldown
        self.finalb_last[key] = {
            "at": snap.at, "side": side, "extreme": session_extreme(snap, side), "phase": phase,
        }
        self.finalb_rearmed.discard(key)
        return True, None, cooldown


def bars_confirm_after_gap(snap, candidate) -> bool:
    """Two completed 1m closes on the card side of a triggered (not building) setup."""
    setup = candidate.setup
    if setup is None or setup.building or setup.trigger is None or len(snap.bars) < 2:
        return False
    direction = 1 if candidate.option.side == "CALL" else -1
    return all(direction * (bar.close - setup.trigger) > 0 for bar in snap.bars[-2:])
