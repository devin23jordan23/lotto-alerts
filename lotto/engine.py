from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import json
import logging
import os
import sqlite3

from dataclasses import replace

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


def dedupe_ready_cards(ready: list) -> list:
    """One card per contract at one timestamp. The higher score stays.

    0DTE, 1DTE, and a handoff can roll onto the same strike. The alerts
    primary key is that contract at that time, so the second insert would
    abort the cycle.
    """
    best = {}
    order = []
    for candidate in ready:
        snap, option = candidate.snapshot, candidate.option
        if option is None:
            order.append(id(candidate))
            best[id(candidate)] = candidate
            continue
        key = (snap.day, snap.symbol, option.symbol, snap.at.isoformat())
        current = best.get(key)
        if current is None:
            best[key] = candidate
            order.append(key)
            continue
        score = candidate.metrics.get("card_score", candidate.score)
        current_score = current.metrics.get("card_score", current.score)
        if score > current_score:
            best[key] = candidate
    return [best[key] for key in order]


def cooldown_underlying(symbol: str) -> str:
    """SPX and the SPXW option root are one underlying for the post hold."""
    name = (symbol or "").upper()
    if name in {"SPX", "SPXW"}:
        return "SPX"
    return name


def cooldown_reason(symbol: str, side: str, at: datetime, posted_at: datetime | None,
                    minutes: int) -> str | None:
    """None when this card may post. Otherwise the decision line for the hold.

    The window is measured from the last posted card. A card at exactly
    `minutes` is allowed. The line is `cooldown: TICKER SIDE posted Xs ago`.
    """
    if minutes <= 0 or posted_at is None:
        return None
    elapsed = (at - posted_at).total_seconds()
    if elapsed >= minutes * 60:
        return None
    seconds = max(0, int(elapsed))
    return f"cooldown: {(symbol or '').upper()} {(side or '').upper()} posted {seconds}s ago"


# Deliveries that mean the scanner already emitted the card. Internal watches
# and unsent research rows do not start the clock.
_POSTED_DELIVERIES = ("pending", "sent", "uncertain", "failed", "expired", "dry_run")


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
        self.stock_events = []
        self.index_bars = {}
        self.runner_names = frozenset()
        self.fallback_log = None
        self.fallback_state = {}
        self.fallback_pending = []
        self.post_cooldown = {}
        self._seed_post_cooldown()

    def remember_stock_events(self, ideas) -> None:
        self.stock_events.extend(ideas)
        if not ideas:
            return
        newest = max(idea.at for idea in ideas)
        self.stock_events = [idea for idea in self.stock_events if newest - idea.at <= timedelta(minutes=20)]

    def handoff_symbols(self) -> list[str]:
        if not self.stock_events:
            return []
        newest = max(idea.at for idea in self.stock_events)
        return sorted({idea.symbol for idea in self.stock_events if newest - idea.at <= timedelta(minutes=15)})

    def process(self, snapshots: list[Snapshot]) -> list[dict]:
        if self.settings.model == "V":
            return self.process_model_v(snapshots)
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
                phase = 'POTENTIAL' if best.setup and best.setup.building else 'ACTIVE'
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
            phase = "POTENTIAL" if candidate.setup and candidate.setup.building else "ACTIVE"
            last = self.store.last_alert(snap.day, snap.symbol, "POTENTIAL")
            parent_id = (last["id"] if candidate.state == "ACTIVATION" and last else None)
            policy = (quality_decisions.get((snap.symbol,snap.at,candidate.option.symbol))
                      if self.quality.mode == 'enforce' and phase == 'ACTIVE' and not cfg.hybrid else None)
            if cfg.send_requote:
                candidate.metrics["send_requote"] = 1
            payload = alert_payload(candidate, phase, parent_id, quality=policy)
            tier = candidate.metrics.get("tier") if cfg.hybrid else None
            ident = self.store.queue(candidate, payload, self.dry_run, phase, parent_id,
                                     quality=policy, tier=tier,
                                     sendable=not cfg.hybrid or tier in cfg.send_tiers)
            self.rearmed.discard((snap.day, snap.symbol))
            self.store.decision(snap, candidate.state, f"{phase.lower()} alert {ident}", candidate)
            alerts.append({"id": ident, "phase": phase, "parent_id": parent_id, "payload": payload})
        return self._with_shadow(alerts, snapshots)

    def process_model_v(self, snapshots: list[Snapshot]) -> list[dict]:
        """Score every priced contract on v2. Post only when it clears its path bar.

        Nothing else posts. No cap, repeat penalty, or hard gate. One posted
        card per ticker and side is held for cooldown_minutes. The other
        suppression is an exact duplicate: same contract and trigger inside
        the dedupe window.
        """
        from .model_v import apply_v2, missing_v2, pick_one_otm, pick_stock_event_contract, thin_candidate
        from .patterns import Setup
        from .stock_thesis import stock_motion_gate, stock_thesis

        cfg = self.settings
        ready = []
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
            if snap.symbol in {"SPY", "QQQ"} and snap.bars:
                self.index_bars[(snap.day, snap.symbol)] = snap.bars
            problems = snap.problems(cfg.max_quote_age_seconds, min_bars=max(3, min(cfg.min_opening_bars, 3)))
            if problems:
                if not snap.problems(cfg.max_quote_age_seconds, min_bars=1):
                    history.append(snap)
                self._log_missing(snap, "snapshot not scoreable: " + "; ".join(problems))
                continue
            if self.last_bar.get(key) == snap.bars[-1].end:
                continue
            self.last_bar[key] = snap.bars[-1].end
            evaluated = candidates(snap, history, replace(cfg, model="V", hybrid=1, soft_blocks=1,
                                                            min_score=min(cfg.min_score, 60)))
            events = [idea for idea in self.stock_events
                      if idea.symbol == snap.symbol and idea.day == snap.day
                      and timedelta(0) <= snap.at - idea.at <= timedelta(minutes=15)]
            seen = {c.option.symbol for c in evaluated}
            for idea in events:
                option = (pick_stock_event_contract(snap, idea.side)
                          if cfg.stock_thesis_enabled else pick_one_otm(snap, idea.side, cfg))
                if option is None:
                    self._log_missing(snap, "stock event has no fresh nearest-expiry ATM quote")
                    continue
                if option.symbol in seen:
                    for candidate in evaluated:
                        if candidate.option.symbol == option.symbol:
                            candidate.metrics["handoff_trigger"] = idea.trigger
                            candidate.metrics["stock_event"] = int(cfg.stock_thesis_enabled)
                            if cfg.stock_thesis_enabled:
                                candidate.setup = Setup(idea.setup, idea.trigger, idea.invalidation)
                    continue
                setup = Setup(idea.setup, idea.trigger, idea.invalidation)
                added = thin_candidate(snap, option, cfg, setup)
                added.metrics["handoff_trigger"] = idea.trigger
                added.metrics["stock_event"] = int(cfg.stock_thesis_enabled)
                evaluated.append(added)
                seen.add(option.symbol)
            if not evaluated:
                priced_any = any(o.bid > 0 and o.ask > o.bid for o in snap.options)
                reason = "option feature window missing" if priced_any else "no priced contract"
                self._log_missing(snap, reason)
                history.append(snap)
                self.history[key] = [s for s in history if snap.at - s.at <= timedelta(minutes=25)]
                if cfg.fallback_rule in {"log", "post", "on"}:
                    self._observe_fallback_safely(snap, ready)
                continue
            scored = []
            for candidate in evaluated:
                handoff = candidate.metrics.get("handoff_trigger") is not None
                if handoff and candidate.setup is None and events:
                    match = next(idea for idea in events if idea.side == candidate.option.side)
                    candidate.setup = Setup(match.setup, match.trigger, match.invalidation)
                if cfg.stock_thesis_enabled and candidate.metrics.get("stock_event"):
                    thesis, why = stock_thesis(snap, candidate.option.side)
                    if thesis:
                        candidate.setup = Setup(thesis["setup"], thesis["trigger"], thesis["invalidation"])
                    candidate.metrics.update({
                        "stock_thesis": thesis or None, "stock_thesis_reason": why or None,
                        "path": "stock_event", "score_mode": "stock_event", "score_v2": 0,
                        "card_score": 0, "gate_post": int(bool(thesis)),
                    })
                    candidate.score = 0
                    candidate.parts = {"stock_event": 1}
                    candidate.metrics.pop("strength_marker", None)
                    candidate.qualifying = bool(thesis)
                else:
                    apply_v2(candidate, cfg, history, self.index_bars, handoff=handoff,
                             thin=not candidate.metrics.get("cluster_size"),
                             a_bar=snap.symbol in self.runner_names)
                    if cfg.stock_thesis_enabled:
                        motion_ok, motion = stock_motion_gate(snap, candidate.option.side)
                        candidate.metrics["stock_motion"] = motion
                        setup_name = candidate.setup.name if candidate.setup else None
                        if setup_name in {"REVERSAL", "INTRADAY_REVERSAL"}:
                            candidate.metrics["stock_thesis_reason"] = "stock thesis: reversal excluded"
                            candidate.qualifying = False
                        elif not setup_name or setup_name == "STOCK_OVERRIDE":
                            candidate.metrics["stock_thesis_reason"] = "stock thesis: no setup anchor"
                            candidate.qualifying = False
                        elif not motion_ok:
                            candidate.metrics["stock_thesis_reason"] = "stock thesis: nearly stationary"
                            candidate.qualifying = False
                if candidate.metrics.get("score_v2") is None:
                    candidate.metrics.update(missing_v2("score did not compute"))
                scored.append(candidate)
            if cfg.score_mode == "combined":
                self._apply_stock_override(snap, scored, cfg, history)
                if cfg.stock_thesis_enabled:
                    for candidate in scored:
                        motion_ok, motion = stock_motion_gate(snap, candidate.option.side)
                        candidate.metrics["stock_motion"] = motion
                        setup_name = candidate.setup.name if candidate.setup else None
                        if setup_name in {"REVERSAL", "INTRADAY_REVERSAL"}:
                            candidate.metrics["stock_thesis_reason"] = "stock thesis: reversal excluded"
                            candidate.qualifying = False
                        elif not setup_name or setup_name == "STOCK_OVERRIDE":
                            candidate.metrics["stock_thesis_reason"] = "stock thesis: no setup anchor"
                            candidate.qualifying = False
                        elif not motion_ok:
                            candidate.metrics["stock_thesis_reason"] = "stock thesis: nearly stationary"
                            candidate.qualifying = False
            self.store.record_candidates(snap, scored, cfg)
            history.append(snap)
            self.history[key] = [s for s in history if snap.at - s.at <= timedelta(minutes=25)]
            for candidate in scored:
                if candidate.metrics.get("score_v2") is None:
                    self.store.decision(snap, "RESEARCH", candidate.metrics.get("v2_reason") or "no v2 tier", candidate)
                    continue
                if self._duplicate_event(candidate):
                    self.store.decision(snap, "RUNNER", "same contract and trigger inside 5 minutes", candidate)
                    continue
                if candidate.qualifying:
                    candidate.state = "IGNITION"
                    ready.append(candidate)
                else:
                    if candidate.metrics.get("stock_thesis_reason"):
                        self.store.decision(snap, "RESEARCH", candidate.metrics["stock_thesis_reason"], candidate)
                        continue
                    blocked = candidate.metrics.get("gate_blocked_by") or []
                    if candidate.metrics.get("s12_qualifying") and blocked:
                        self.store.decision(snap, "RESEARCH", "card gate: " + ", ".join(blocked), candidate)
                    else:
                        gap = candidate.metrics.get("below_by")
                        self.store.decision(
                            snap, "RESEARCH",
                            f"below bar by {gap} ({candidate.metrics.get('score_mode')} {candidate.metrics.get('path')} "
                            f"bar {candidate.metrics.get('post_bar')}; strength {candidate.metrics.get('stock_strength')}; "
                            f"p {candidate.metrics.get('fitted_win_probability')})",
                            candidate)
            if cfg.fallback_rule in {"log", "post", "on"}:
                self._observe_fallback_safely(snap, ready)
        alerts = []
        ready = dedupe_ready_cards(ready)
        for candidate in sorted(ready, key=lambda c: (-c.metrics.get("card_score", c.score), c.snapshot.symbol)):
            snap = candidate.snapshot
            held = self._cooldown_reason(candidate)
            if held:
                # The score and the contract stay in candidate_evaluations.
                # This decision is the log line. The card is not queued.
                self.store.decision(snap, "RUNNER", held, candidate)
                continue
            if cfg.send_requote:
                candidate.metrics["send_requote"] = 1
            phase = "ACTIVE"
            payload = alert_payload(candidate, phase)
            self._annotate_card(payload, candidate)
            ident = self.store.queue(candidate, payload, self.dry_run, phase, None, tier=None, sendable=True)
            self._remember_post(candidate)
            posted = f"active alert {ident}"
            if candidate.metrics.get("fallback_reason"):
                posted = f"{posted}: {candidate.metrics['fallback_reason']}"
            self.store.decision(snap, candidate.state, posted, candidate)
            alerts.append({"id": ident, "phase": phase, "parent_id": None, "payload": payload})
        return self._with_shadow(alerts, snapshots)

    def _cooldown_reason(self, candidate) -> str | None:
        snap = candidate.snapshot
        side = candidate.option.side if candidate.option is not None else ""
        key = (snap.day, cooldown_underlying(snap.symbol), (side or "").upper())
        return cooldown_reason(
            snap.symbol, side, snap.at, self.post_cooldown.get(key), self.settings.cooldown_minutes)

    def _remember_post(self, candidate) -> None:
        if self.settings.cooldown_minutes <= 0 or candidate.option is None:
            return
        snap = candidate.snapshot
        side = candidate.option.side.upper()
        key = (snap.day, cooldown_underlying(snap.symbol), side)
        current = self.post_cooldown.get(key)
        if current is None or snap.at > current:
            self.post_cooldown[key] = snap.at

    def _seed_post_cooldown(self) -> None:
        """Reload today's posted cards so a restart does not send them again."""
        if self.settings.cooldown_minutes <= 0:
            return
        today = datetime.now(ET).date().isoformat()
        placeholders = ",".join("?" * len(_POSTED_DELIVERIES))
        try:
            rows = self.store.db.execute(
                f"""SELECT symbol, side, at FROM alerts
                    WHERE day=? AND IFNULL(phase, '')!='POTENTIAL'
                      AND delivery IN ({placeholders})""",
                (today, *_POSTED_DELIVERIES),
            ).fetchall()
        except sqlite3.Error:
            logging.getLogger(__name__).warning("post cooldown seed failed")
            return
        for row in rows:
            side = (row["side"] or "").upper()
            if side not in {"CALL", "PUT"} or not row["at"] or not row["symbol"]:
                continue
            at = datetime.fromisoformat(row["at"])
            key = (today, cooldown_underlying(row["symbol"]), side)
            current = self.post_cooldown.get(key)
            if current is None or at > current:
                self.post_cooldown[key] = at

    def _with_shadow(self, alerts, snapshots):
        """Score SHADOW_PROFILE after the live cards are already built.

        A shadow failure is logged and the live return is unchanged.
        """
        try:
            self.score_shadow(snapshots)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "shadow profile failed (%s); live cards unchanged", type(exc).__name__)
        return alerts

    def shadow_profile_name(self) -> str:
        """Empty means off. SHADOW_PROFILE wins when the environment sets it."""
        if "SHADOW_PROFILE" in os.environ:
            return os.environ.get("SHADOW_PROFILE", "").strip()
        return str(getattr(self.settings, "shadow_profile", "") or "").strip()

    def shadow_settings(self):
        """Live settings with the shadow profile's line. None when shadow is off."""
        name = self.shadow_profile_name()
        if not name:
            return None
        from .weights import active_profile, load_weights
        lines = active_profile(load_weights(self.settings.weights_path), name)
        return replace(
            self.settings,
            profile=lines["profile"],
            post_threshold=float(lines["post_threshold"]),
            bypass_threshold=float(lines["bypass_threshold"]),
            min_positive_features=int(lines["min_positive_features"]),
            strong_feature_min=int(lines["strong_feature_min"]),
        )

    def score_shadow(self, snapshots: list[Snapshot]) -> None:
        """Score this cycle on the shadow profile. Write shadow_cards only.

        Does not queue alerts, does not call Discord, and does not touch the
        live history, last-bar cursor, decisions, or alert rows.
        """
        cfg = self.shadow_settings()
        if cfg is None or not snapshots:
            return
        from .model_v import apply_v2, missing_v2, same_event

        if not hasattr(self, "shadow_last_bar"):
            self.shadow_last_bar = {}
        profile = cfg.profile
        window = timedelta(minutes=self.settings.event_dedupe_minutes)
        for snap in snapshots:
            key = (snap.day, snap.symbol)
            if not snap.bars:
                continue
            problems = snap.problems(
                cfg.max_quote_age_seconds, min_bars=max(3, min(cfg.min_opening_bars, 3)))
            if problems:
                continue
            if self.shadow_last_bar.get(key) == snap.bars[-1].end:
                continue
            self.shadow_last_bar[key] = snap.bars[-1].end
            history = list(self.history.get(key, []))
            try:
                evaluated = candidates(snap, history, cfg)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "shadow candidates failed for %s (%s)", snap.symbol, type(exc).__name__)
                continue
            for candidate in evaluated:
                try:
                    apply_v2(candidate, cfg, history, self.index_bars,
                             thin=not candidate.metrics.get("cluster_size"),
                             a_bar=snap.symbol in self.runner_names)
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "shadow score failed for %s (%s)", snap.symbol, type(exc).__name__)
                    continue
                if candidate.metrics.get("score_v2") is None and not candidate.qualifying:
                    candidate.metrics.update(missing_v2("score did not compute"))
                if not candidate.qualifying:
                    continue
                if self._shadow_duplicate(candidate, profile, window, same_event):
                    continue
                points = candidate.metrics.get("confluence_points") or {}
                breakdown = {
                    str(name): value for name, value in points.items()
                    if isinstance(value, (int, float)) and value
                }
                score = candidate.metrics.get("card_score", candidate.score)
                self.store.record_shadow_card(profile, candidate, breakdown, score)

    def _shadow_duplicate(self, candidate, profile: str, window, same_event) -> bool:
        snap = candidate.snapshot
        trigger = candidate.setup.trigger if candidate.setup else candidate.metrics.get("handoff_trigger")
        rows = self.store.db.execute(
            """SELECT at, breakdown FROM shadow_cards
               WHERE profile=? AND day=? AND symbol=? AND contract=? ORDER BY at DESC""",
            (profile, snap.day, snap.symbol, candidate.option.symbol)).fetchall()
        for row in rows:
            at = datetime.fromisoformat(row["at"])
            if snap.at - at >= window or snap.at < at:
                continue
            if same_event(trigger, trigger, snap.spot, snap.prior_atr):
                return True
        return False

    def _apply_stock_override(self, snap, scored, cfg, history) -> None:
        """Post the priced 1-OTM contract when every strong-stock check is on.

        Other strikes stay on the combined bar. No contract means no card.
        """
        from .model_v import apply_v2, pick_one_otm, thin_candidate
        from .patterns import Setup

        sides = {c.option.side for c in scored if c.metrics.get("exceptional_stock")}
        for side in sides:
            picked = pick_one_otm(snap, side, cfg)
            if picked is None:
                self._log_missing(snap, "stock_override has no priced 1-OTM contract")
                continue
            match = next((c for c in scored if c.option.symbol == picked.symbol), None)
            if match is None:
                donor = next(c for c in scored if c.option.side == side and c.metrics.get("exceptional_stock"))
                setup = donor.setup or Setup("STOCK_OVERRIDE", snap.spot, snap.spot)
                match = thin_candidate(snap, picked, cfg, setup)
                apply_v2(match, cfg, history, self.index_bars, handoff=True, thin=True)
                scored.append(match)
            match.metrics["path"] = "stock_override"
            match.metrics["score_mode"] = "combined"
            match.qualifying = bool(match.metrics.get("gate_post", 1))

    def _log_missing(self, snap, reason: str) -> None:
        from .model_v import missing_v2
        detail = missing_v2(reason)
        self.store.decision(snap, "DISCOVERY", detail["v2_reason"])

    def _fallback_log_path(self):
        if self.fallback_log:
            return self.fallback_log
        return Path(os.getenv("DATA_DIR", "data")) / "fallback-rule.jsonl"

    def _option_side_skip(self, snap, side, ready):
        del side
        from .volume_trend import drop_repeat_skip
        if drop_repeat_skip(self.settings.profile):
            return False, None, None
        start = snap.at - timedelta(minutes=self.settings.fb_same_move_before_min)
        for candidate in ready:
            if candidate.snapshot.symbol != snap.symbol:
                continue
            if candidate.setup and candidate.setup.name in {"STRONG_STOCK_FALLBACK", "STOCK_BYPASS"}:
                continue
            if candidate.metrics.get("path") == "bypass":
                continue
            if start <= candidate.snapshot.at <= snap.at:
                return True, "pending", candidate.snapshot.at.astimezone(ET).isoformat()
        rows = self.store.db.execute(
            "SELECT id, at, setup_name FROM alerts WHERE day=? AND symbol=?",
            (snap.day, snap.symbol)).fetchall()
        for row in rows:
            if row["setup_name"] in {"STRONG_STOCK_FALLBACK", "STOCK_BYPASS"}:
                continue
            at = datetime.fromisoformat(row["at"])
            if start <= at <= snap.at:
                return True, row["id"], row["at"]
        return False, None, None

    def _finish_fallback_entries(self, snap) -> None:
        """Write the entry line when bar m+2 arrives. A skipped bar is entry_missing, not a spread fail.

        The fire line is already on disk. Drop the in-memory entry before writing it, so a
        failed write is not retried on the next cycle.
        """
        from .model_v import append_fallback_log, fallback_entry_line
        from .model_v import _minute_index
        minute = _minute_index(snap.bars[-1]) if snap.bars else None
        if minute is None:
            return
        pending = []
        due = []
        for record in self.fallback_pending:
            if record["tk"] != snap.symbol or record["date"] != snap.day or minute < record["entry_m"]:
                pending.append(record)
                continue
            due.append((record, minute == record["entry_m"]))
        self.fallback_pending = pending
        for record, landed in due:
            line = fallback_entry_line(record, snap if landed else None, self.settings)
            append_fallback_log(self._fallback_log_path(), line)

    def _observe_fallback_safely(self, snap, ready) -> None:
        """A fallback failure logs a warning and leaves this cycle's option cards alone."""
        try:
            self._observe_fallback(snap, ready)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "fallback rule failed for %s (%s)", snap.symbol, type(exc).__name__)

    def _primary_ready(self, snap, ready) -> bool:
        for candidate in ready:
            if candidate.snapshot.symbol != snap.symbol:
                continue
            if candidate.setup and candidate.setup.name in {"STRONG_STOCK_FALLBACK", "STOCK_BYPASS"}:
                continue
            if candidate.metrics.get("path") == "bypass":
                continue
            if candidate.qualifying:
                return True
        return False

    def _post_fallback(self, snap, record, ready) -> None:
        from .model_v import apply_v2, thin_candidate
        from .patterns import Setup
        option = record.get("_option")
        if option is None or record["dedupe_flag"] or record["skip_flag"]:
            return
        if self._primary_ready(snap, ready):
            return
        if not record.get("gate_post"):
            return
        base = record.get("base") or {}
        if not base.get("direction") or not base.get("volume"):
            return
        trigger = record.get("trigger_value")
        invalidation = record.get("invalidation_value")
        if not isinstance(trigger, (int, float)) or not isinstance(invalidation, (int, float)):
            return
        if (1 if option.side == "CALL" else -1) * (trigger - invalidation) <= 0:
            return
        setup = Setup("STOCK_BYPASS", float(trigger), float(invalidation))
        candidate = thin_candidate(snap, option, self.settings, setup)
        history = self.history.get((snap.day, snap.symbol), [])
        apply_v2(candidate, self.settings, history, self.index_bars, handoff=False, thin=True,
                 a_bar=snap.symbol in self.runner_names)
        if candidate.metrics.get("card_layers_failed") or not candidate.metrics.get("gate_post"):
            return
        if self.settings.stock_thesis_enabled:
            from .stock_thesis import stock_thesis
            thesis, why = stock_thesis(snap, option.side)
            candidate.metrics["stock_thesis"] = thesis or None
            candidate.metrics["stock_thesis_reason"] = why or None
            if not thesis:
                self.store.decision(snap, "RESEARCH", why, candidate)
                return
        if candidate.metrics.get("score_v2") is None:
            candidate.metrics["score_v2"] = candidate.score
        candidate.setup = setup
        candidate.qualifying = True
        candidate.state = "IGNITION"
        candidate.metrics["path"] = "bypass"
        candidate.metrics["card_score"] = record.get("bypass_score", candidate.metrics.get("card_score"))
        candidate.score = record.get("bypass_score", candidate.score)
        candidate.metrics["confluence_points"] = record.get("confluence_points") or {}
        candidate.metrics["confluence_count"] = record.get("positive_features") or 0
        candidate.metrics["confluence_total"] = len(candidate.metrics["confluence_points"])
        candidate.metrics["bypass_threshold"] = self.settings.bypass_threshold
        positive = record.get("positive_features") or 0
        bypass_score = record.get("bypass_score")
        if (positive >= self.settings.strong_feature_min
                and bypass_score is not None and bypass_score >= self.settings.post_threshold):
            candidate.metrics["strength_marker"] = "⚡"
        else:
            candidate.metrics.pop("strength_marker", None)
        candidate.metrics["fallback_reason"] = record["card_reason"]
        candidate.metrics["bypass_reason"] = record["card_reason"]
        if record["card_reason"] not in candidate.reasons:
            candidate.reasons = list(candidate.reasons) + [record["card_reason"]]
        for key in ("quote_age_s", "bar_age_s", "min_since_trigger"):
            if key in record and key not in candidate.metrics:
                candidate.metrics[key] = record[key]
        ready.append(candidate)
        self.store.record_candidates(snap, [candidate], self.settings)

    def _observe_fallback(self, snap, ready) -> None:
        """Log would-be cards. log mode does not touch the option-side post list."""
        from .model_v import append_fallback_log, evaluate_fallback, new_fallback_state
        if not snap.bars:
            return
        self._finish_fallback_entries(snap)
        for side in ("CALL", "PUT"):
            key = (snap.day, snap.symbol, side)
            state = self.fallback_state.get(key)
            if state is None:
                state = new_fallback_state()
                self.fallback_state[key] = state
            history = self.history.get((snap.day, snap.symbol), [])
            spy_bars = self.index_bars.get((snap.day, "SPY"))
            record = evaluate_fallback(
                snap, side, self.settings, state, self._option_side_skip(snap, side, ready),
                history=history, spy_bars=spy_bars)
            if record is None:
                continue
            fire_line = {key: value for key, value in record.items() if key != "_option"}
            append_fallback_log(self._fallback_log_path(), fire_line)
            self.fallback_pending.append(record)
            if self.settings.fallback_rule in {"post", "on"}:
                self._post_fallback(snap, record, ready)

    def _duplicate_event(self, candidate) -> bool:
        from .model_v import same_event
        snap = candidate.snapshot
        trigger = candidate.setup.trigger if candidate.setup else candidate.metrics.get("handoff_trigger")
        rows = self.store.db.execute(
            """SELECT at, setup_trigger FROM alerts WHERE day=? AND symbol=? AND contract=? ORDER BY at DESC""",
            (snap.day, snap.symbol, candidate.option.symbol)).fetchall()
        window = timedelta(minutes=self.settings.event_dedupe_minutes)
        for row in rows:
            at = datetime.fromisoformat(row["at"])
            if snap.at - at >= window or snap.at < at:
                continue
            if same_event(trigger, row["setup_trigger"], snap.spot, snap.prior_atr):
                return True
        return False

    def _annotate_card(self, payload, candidate) -> None:
        metrics = candidate.metrics
        embed = payload["embeds"][0]
        path = metrics.get("path") or "combined"
        if path in {"bypass", "stock_event"}:
            return
        embed["description"] += (
            f"\n{path} · {metrics.get('score_mode')} v2 **{metrics.get('score_v2')}** · bar {metrics.get('post_bar')}"
            f" · option {metrics.get('option_v2')} + stock {metrics.get('stock_v2')}"
        )
        embed["description"] += (
            f"\nstock_strength {metrics.get('stock_strength') if metrics.get('stock_strength') is not None else 'n/a'}"
            f" · fitted p {metrics.get('fitted_win_probability') if metrics.get('fitted_win_probability') is not None else 'n/a'}"
        )
        if metrics.get("spy_label"):
            embed["description"] += f"\n{metrics['spy_label']}"
        if metrics.get("level_retest"):
            embed["description"] += f"\nLevel retest {metrics.get('retest_level') or ''}".rstrip()
        if path == "stock_override":
            embed["description"] += "\nExceptional stock override"
        if metrics.get("strength_band") == "a_bar":
            embed["description"] += "\nRunner name at the A bar"

    def _clear_confirmation(self, key):
        for score_key in list(self.qualified_since):
            if score_key[:2] == key:
                self.qualified_since.pop(score_key, None)
