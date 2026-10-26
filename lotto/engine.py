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


def hybrid_tier(candidate, policy, settings):
    """Compute the proposed tier blend; it is not a calibrated win probability."""
    rules = set(policy.reasons)
    blend = candidate.score - 10*len(rules & {"OPEN", "EXHQ", "W1DTE", "PREM"})
    if rules & {"WF", "LATE"}:
        blend -= 25
    blend += 5 if candidate.metrics.get("hourly_break") else 0
    tier = "A" if blend >= settings.tier_a else "B" if blend >= settings.tier_b else "C"
    if (settings.hybrid_reversal_c and candidate.setup is not None
            and candidate.setup.name in ("INTRADAY_REVERSAL", "REVERSAL")):
        tier = "C"  # Directional A/B lane only (Andre, Oct 25); reversals stored and graded, not sent.
    candidate.metrics.update({"tier": tier, "tier_blend": round(blend, 2),
                              "tier_v2": None, "score_v2": None,
                              "s_rules": sorted(rules)})
    return tier


def distinct_price_leg(candidate, previous) -> bool:
    """A new level can alert while the ticker is running, without a timer.

    Repeated polls of the same breakout are one thesis. A higher call trigger
    or lower put trigger, accompanied by actual stock progress, is a new leg.
    """
    setup = candidate.setup
    if setup is None or setup.building or previous["setup_trigger"] is None:
        return False
    direction = 1 if candidate.option.side == "CALL" else -1
    prior_spot = json.loads(previous["snapshot"])["spot"]
    atr = candidate.snapshot.prior_atr
    if not atr or atr <= 0:
        return False
    # Rolling pivots change every minute during a single continuation; that
    # alone is not a second setup. Distinct frozen reclaim levels can re-alert.
    if previous["setup_name"] == setup.name and setup.name != "LEVEL_RECLAIM":
        return False
    distance = .15 if previous["setup_name"] != setup.name else .25
    threshold = max(distance * atr, .001 * candidate.snapshot.spot)
    return (direction * (setup.trigger - previous["setup_trigger"]) >= threshold
            and direction * (candidate.snapshot.spot - prior_spot) >= threshold)


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
        self.spy_bars = {}
        self.mover_last = {}
        self.mover_sent = {}
        self.mover_alert_id = {}

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
            if snap.symbol == "SPY" and snap.bars:
                self.spy_bars[snap.day] = snap.bars
            if cfg.mover_enabled:
                from .mover import mover_candidates
                ready.extend(mover_candidates(self, snap, cfg))
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
                phase = 'POTENTIAL' if best.setup and best.setup.building else 'ACTIVE'
                policy = evaluate(best, self.quality.policy, disabled=self.quality.disabled_rules)
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
            phase = "POTENTIAL" if best.setup and best.setup.building else "ACTIVE"
            expiry = best.option.expiry.isoformat()
            if cfg.hybrid:
                fire_n = self.store.count_direction(snap.day,snap.symbol,best.option.side,expiry)+1
                best.metrics["fire_n"] = fire_n
                if fire_n > 1:
                    fire_penalty = 3 if fire_n == 2 else 6
                    best.parts["pen_fire_n"] = -fire_penalty
                    best.score -= fire_penalty
                prior = self.store.last_alert(snap.day,snap.symbol,"ACTIVE",
                    best.option.side,expiry,exclude_tier_c=True)
                if (prior and snap.at-datetime.fromisoformat(prior["at"]) < timedelta(minutes=30)):
                    best.parts["pen_repeat"] = -cfg.repeat_penalty
                    best.score -= cfg.repeat_penalty
                    best.metrics["repeat_number"] = fire_n
                if best.score < cfg.min_score:
                    self.store.decision(snap,"RESEARCH","hybrid score below floor after repeat",best)
                    continue
                policy = evaluate(best, disabled=self.quality.disabled_rules)
                hybrid_tier(best,policy,cfg)
            last = self.store.last_alert(*key, phase,
                side=best.option.side if cfg.hybrid else None,
                expiry=expiry if cfg.hybrid else None,
                exclude_tier_c=bool(cfg.hybrid))
            watch = (self.store.last_alert(*key, "POTENTIAL",
                     side=best.option.side if cfg.hybrid else None,
                     expiry=expiry if cfg.hybrid else None)
                     if phase == "ACTIVE" else None)
            if last and last["side"] == best.option.side and (cfg.hybrid or key not in self.rearmed):
                if not distinct_price_leg(best, last):
                    self.store.decision(snap, "RUNNER", "same price leg already alerted", best)
                    continue
            if cfg.hybrid and best.metrics["tier"] == "C":
                prior_c = self.store.last_alert(*key, phase, side=best.option.side, expiry=expiry)
                if prior_c and prior_c["quality_tier"] == "C" and not distinct_price_leg(best,prior_c):
                    self.store.decision(snap,"RESEARCH","same internal C-tier price leg",best)
                    continue
            promotion = bool(phase == "ACTIVE" and watch and watch["side"] == best.option.side
                             and (not last or watch["at"] > last["at"]))
            best.state = "ACTIVATION" if promotion else "RUNNER" if last else "IGNITION"
            if phase == 'ACTIVE' and self.quality.mode == 'enforce' and not cfg.hybrid:
                policy = quality_decisions[(snap.symbol,snap.at,best.option.symbol)]
                if policy.tier == 'RESEARCH':
                    reason = 'Model S research only: ' + ', '.join(policy.reasons)
                    self.store.research_candidate(best,reason)
                    self.store.decision(snap,'RESEARCH',reason,best)
                    continue
            elif phase == "ACTIVE" and cfg.active_quality_gate and not cfg.hybrid:
                reason = active_quality_reason(best)
                if reason:
                    self.store.research_candidate(best, reason)
                    self.store.decision(snap, "RESEARCH", reason, best)
                    continue
            # A renewed thesis or a materially new price leg can alert again.
            if cfg.keep_gate and best.setup is not None and not best.setup.building:
                from .mover import keep_reason
                keep = keep_reason(self, best, cfg)
                if keep:
                    self.store.research_candidate(best, keep)
                    self.store.decision(snap, "RESEARCH", keep, best)
                    continue
            ready.append(best)
        alerts = []
        # Rank the whole scan cycle. Zero-valued count settings impose no budget.
        cycle_keys = set()
        cycle_alert_ids = {}
        # Let a sendable option-led A/B idea cover the move first. An internal
        # C-tier record must never consume the stock-mover bypass.
        for candidate in sorted(ready, key=lambda c: (c.metrics.get("lane") == "MOVER",
                -(c.score + 2*min(8, max(-8, c.score_change))), -c.score, c.snapshot.symbol)):
            snap = candidate.snapshot
            cycle_key = (snap.day, snap.symbol, candidate.option.side)
            if cycle_key in cycle_keys:
                if candidate.metrics.get("lane") == "MOVER":
                    mover_key = (snap.day, snap.symbol, 1 if candidate.option.side == "CALL" else -1)
                    self.mover_sent[mover_key] = True
                    self.mover_alert_id[mover_key] = cycle_alert_ids[cycle_key]
                    self.store.decision(snap, "MOVER", "mover: covered by same-cycle option-led alert")
                continue
            if ((cfg.max_alerts_per_cycle and len(alerts) >= cfg.max_alerts_per_cycle)
                    or (cfg.max_alerts_per_day and self.store.count(snap.day) >= cfg.max_alerts_per_day)
                    or (cfg.max_alerts_per_ticker and self.store.count(snap.day, snap.symbol) >= cfg.max_alerts_per_ticker)):
                self.store.decision(snap, candidate.state, "alert budget/ranking suppressed", candidate)
                continue
            phase = "POTENTIAL" if candidate.setup and candidate.setup.building else "ACTIVE"
            last = self.store.last_alert(snap.day, snap.symbol, "POTENTIAL")
            parent_id = (last["id"] if candidate.state == "ACTIVATION" and last else None)
            policy = (quality_decisions.get((snap.symbol,snap.at,candidate.option.symbol))
                      if self.quality.mode == 'enforce' and phase == 'ACTIVE' and not cfg.hybrid else None)
            if cfg.send_requote:
                candidate.metrics["send_requote"] = 1
            payload = alert_payload(candidate, phase, parent_id, quality=policy)
            if (self.quality.policy == "S2" and candidate.metrics.get("lane") != "MOVER"
                    and (candidate.metrics.get("move_from_open_atr") or 0) >= .4
                    and candidate.metrics.get("options_persistent_20m") == 1):
                payload["embeds"][0]["title"] = "A+ · " + payload["embeds"][0]["title"]
            if candidate.metrics.get("lane") == "MOVER":
                from .mover import decorate
                decorate(payload, candidate)
            tier = candidate.metrics.get("tier") if cfg.hybrid else None
            sendable = (not cfg.hybrid or candidate.metrics.get("lane") == "MOVER"
                        or (tier is not None and tier in cfg.send_tiers))
            ident = self.store.queue(candidate, payload, self.dry_run, phase, parent_id,
                                     quality=policy, tier=tier,
                                     sendable=sendable)
            if sendable and phase == "ACTIVE":
                cycle_keys.add(cycle_key)
                cycle_alert_ids[cycle_key] = ident
            if candidate.metrics.get("lane") == "MOVER":
                mover_key = (snap.day, snap.symbol, 1 if candidate.option.side == "CALL" else -1)
                self.mover_sent[mover_key] = True
                self.mover_alert_id[mover_key] = ident
            self.rearmed.discard((snap.day, snap.symbol))
            self.store.decision(snap, candidate.state, f"{phase.lower()} alert {ident}", candidate)
            alerts.append({"id": ident, "phase": phase, "parent_id": parent_id, "payload": payload})
        return alerts

    def _clear_confirmation(self, key):
        for score_key in list(self.qualified_since):
            if score_key[:2] == key:
                self.qualified_since.pop(score_key, None)
