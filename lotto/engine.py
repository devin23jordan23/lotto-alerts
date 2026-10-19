from collections import defaultdict
from datetime import datetime, timedelta
import json
import logging
import os

from .config import Settings
from .discord import alert_payload
from .features import candidates
from .models import ET, Snapshot
from .store import Store
from .quality import QualityConfig, evaluate


def active_quality_reason(candidate) -> str | None:
    """Historical all-session hypothesis using only information at alert time."""
    metrics = candidate.metrics
    response = metrics.get("response_proxy")
    momentum = metrics.get("return_5m_directional")
    if (not metrics.get("opening_expansion") and response is not None and momentum is not None
            and response < .5 and momentum < .005):
        return "research only: weak option response and five-minute stock follow-through"
    local = candidate.snapshot.at.astimezone(ET)
    extension = metrics.get("move_from_open_atr")
    if local.hour >= 14 and extension is not None and extension >= 1:
        return "research only: late entry after one ATR move from open"
    return None


class Engine:
    def __init__(self, store: Store, settings: Settings | None = None, dry_run: bool = True, quality: QualityConfig | None = None):
        self.store = store
        self.settings = settings or Settings()
        self.dry_run = dry_run
        self.quality = quality or QualityConfig.from_env()
        self.history = {}
        self.scores = defaultdict(list)
        self.qualified_since = {}
        self.last_evaluation = {}
        self.last_bar = {}
        self.cool_since = {}
        self.last_cooling = {}
        self.rearmed = set()

    def process(self, snapshots: list[Snapshot]) -> list[dict]:
        ready = []
        quality_decisions = {}
        cfg = self.settings
        for snap in snapshots:
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
                self.cool_since.pop(key, None)
                continue
            # Polling twice within the same completed bar cannot count as persistence.
            if self.last_bar.get(key) == snap.bars[-1].end:
                continue
            self.last_bar[key] = snap.bars[-1].end
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
                if best is None:
                    from .audit import chain_reasons
                    detail = chain_reasons(snap, history, cfg)
                    reason = 'option stage: ' + json.dumps(detail, sort_keys=True)
                    if snap.symbol in os.getenv('LOTTO_DIAGNOSE_SYMBOLS', '').upper().split(','):
                        logging.getLogger(__name__).info('Option stage %s: %s', snap.symbol, json.dumps(detail))
                else:
                    reason = '; '.join(best.blockers)
                self.store.decision(snap, "COOLING" if self.store.last_alert(*key) else "DISCOVERY", reason, best)
                continue
            self.cool_since.pop(key, None)
            best = max(valid, key=lambda c: c.score)
            if self.quality.mode != 'off':
                phase = 'POTENTIAL' if best.setup.building else 'ACTIVE'
                policy = evaluate(best, disabled=self.quality.disabled_rules)
                quality_decisions[(snap.symbol,snap.at,best.option.symbol)] = policy
                self.store.quality_shadow(best,policy,phase)
                if self.quality.shadow_extra:
                    self.store.quality_shadow(best,evaluate(best,'stack8',self.quality.disabled_rules),phase)
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
            if awaiting_confirmation and not opening_fast_lane:
                self.store.decision(snap, "IGNITION", "confirming persistence", best)
                continue
            if awaiting_confirmation and opening_fast_lane:
                best.metrics["opening_fast_lane"] = 1
            # Flat strong scores can persist; materially deteriorating scores cannot alert.
            if best.score_change < -5:
                self.store.decision(snap, "COOLING", "score deteriorating", best)
                continue
            close = snap.session_end or snap.at.astimezone(ET).replace(hour=16, minute=0, second=0, microsecond=0)
            if close - snap.at <= timedelta(minutes=cfg.entry_cutoff_minutes):
                self.store.decision(snap, "RUNNER", "entry cutoff; tracking only", best)
                continue
            phase = "POTENTIAL" if best.setup.building else "ACTIVE"
            last = self.store.last_alert(*key)
            # A watch becoming a confirmed setup is a new, separately measured
            # alert, not a duplicate blocked by the ticker cooldown.
            promotion = bool(last and phase == "ACTIVE" and last["phase"] == "POTENTIAL"
                             and last["side"] == best.option.side and key not in self.rearmed)
            if last:
                best.state = "ACTIVATION" if promotion else "RUNNER"
                if not promotion:
                    if (snap.at - datetime.fromisoformat(last["at"]) < timedelta(minutes=cfg.ticker_cooldown_minutes)
                            or (key not in self.rearmed and last["side"] == best.option.side)):
                        self.store.decision(snap, "RUNNER", "cooldown or fresh reset required", best)
                        continue
            else:
                best.state = "IGNITION"
            if phase == 'ACTIVE' and self.quality.mode == 'enforce':
                policy = quality_decisions[(snap.symbol,snap.at,best.option.symbol)]
                if policy.tier == 'RESEARCH':
                    reason = 'Model S research only: ' + ', '.join(policy.reasons)
                    self.store.research_candidate(best,reason)
                    self.store.decision(snap,'RESEARCH',reason,best)
                    continue
            elif phase == "ACTIVE" and cfg.active_quality_gate:
                reason = active_quality_reason(best)
                if reason:
                    self.store.research_candidate(best, reason)
                    self.store.decision(snap, "RESEARCH", reason, best)
                    continue
            # A previously alerted contract may form a genuinely new leg after
            # cooldown and a recorded reset.
            ready.append(best)
        alerts = []
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
            policy = (quality_decisions.get((snap.symbol,snap.at,candidate.option.symbol))
                      if self.quality.mode == 'enforce' and phase == 'ACTIVE' else None)
            payload = alert_payload(candidate, phase, parent_id, quality=policy)
            ident = self.store.queue(candidate, payload, self.dry_run, phase, parent_id, quality=policy)
            self.rearmed.discard((snap.day, snap.symbol))
            self.store.decision(snap, candidate.state, f"{phase.lower()} alert {ident}", candidate)
            alerts.append({"id": ident, "phase": phase, "parent_id": parent_id, "payload": payload})
        return alerts

    def _clear_confirmation(self, key):
        for score_key in list(self.qualified_since):
            if score_key[:2] == key:
                self.qualified_since.pop(score_key, None)
