import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .config import Settings
from .discord import alert_payload
from .features import Candidate, candidates, contract_ok, stock_features
from .finalb import (
    COOLDOWN_REASON,
    finalb_cooldown_blocks,
    finalb_enabled,
    finalb_overlay,
    live_cooldown_blocks,
    promotion_bypasses,
    rearm_atr_measure,
    rearm_atr_variant,
    session_extreme,
)
from .latency import break_bar_close
from .patterns import detect_setup
from .scanner_fixes import (
    BENCH,
    afternoon_level,
    fixes_active,
    in_stock_first_window,
    invalidation_holds,
    lane_context,
    shadow_flags,
    trigger_gap,
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
        # Latest SPY/QQQ snapshot. Names in the deep set are not ordered first,
        # so a symbol evaluated earlier in the cycle uses the previous snapshot
        # when it is at most 90 seconds old.
        self.bench = {}
        self._lane_alerts = []
        self.option_seeds = defaultdict(list)
        self.first_qual_wall = {}
        self.planned_hot = None
        self._eval_at = {}

    def process(self, snapshots: list[Snapshot]) -> list[dict]:
        ready = []
        cfg = self.settings
        self._cycle_shadow = []
        self._lane_alerts = []
        for snap in snapshots:
            self._eval_at[(snap.day, snap.symbol)] = datetime.now(timezone.utc)
            if snap.symbol in {"SPY", "QQQ"} and snap.bars:
                self.bench[snap.symbol] = snap
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
                self._decide(snap, "DISCOVERY", "; ".join(problems))
                self._clear_confirmation(key)
                self._shadow_clear(key)
                self.cool_since.pop(key, None)
                continue
            # Polling twice within the same completed bar cannot count as persistence.
            if self.last_bar.get(key) == snap.bars[-1].end:
                continue
            self.last_bar[key] = snap.bars[-1].end
            gap_seconds = (snap.at - history[-1].at).total_seconds() if history else 0.0
            bench = self._bench_view(snap)
            seed_gap = cfg.oh_seed_max_gap_s if cfg.oh_seed_mode == 2 else 0
            history_for_eval = self._history_for(snap, history) if seed_gap else history
            token = BENCH.set(bench)
            try:
                if seed_gap:
                    evaluated = candidates(snap, history_for_eval, cfg, seed_gap=seed_gap)
                else:
                    evaluated = candidates(snap, history_for_eval, cfg)
            finally:
                BENCH.reset(token)
            self.store.record_candidates(snap, evaluated, cfg)
            self._log_fix_shadow(snap, evaluated)
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
                if not evaluated and cfg.stock_first_mode >= 1:
                    try:
                        self._lane_alerts.extend(self._stock_first(snap, bench))
                    except Exception:
                        LOG.exception("stock-first lane failed for %s; live path unchanged", snap.symbol)
                seeded = self._would_qualify_seeded(snap, history) if not evaluated else None
                self._decide(snap, "COOLING" if self.store.last_alert(*key) else "DISCOVERY",
                             "; ".join(best.blockers) if best else "options history, liquidity, or neighboring-strike activity unavailable",
                             best, {"would_qualify_seeded": seeded} if seeded is not None else None)
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
                    self.first_qual_wall.pop(other, None)
            if score_key in self.last_evaluation and snap.at - self.last_evaluation[score_key] > timedelta(seconds=150):
                self.qualified_since.pop(score_key, None)
                self.first_qual_wall.pop(score_key, None)
            self.last_evaluation[score_key] = snap.at
            prior_scores = self.scores[score_key]
            old = [s for s in prior_scores if snap.at - s[0] >= timedelta(minutes=3)]
            best.score_change = best.score - old[-1][1] if old else 0
            prior_scores.append((snap.at, best.score))
            self.scores[score_key] = [s for s in prior_scores if snap.at - s[0] <= timedelta(minutes=10)]
            if score_key not in self.qualified_since:
                self.qualified_since[score_key] = snap.bars[-1].end
                self.first_qual_wall[score_key] = self._eval_at.get(key, snap.at)
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
                self._decide(snap, "IGNITION", "confirming persistence", best)
                self._note_shadow(snap, evaluated, key, False)
                continue
            if awaiting_confirmation and opening_fast_lane:
                best.metrics["opening_fast_lane"] = 1
            # Flat strong scores can persist; materially deteriorating scores cannot alert.
            if best.score_change < -5:
                self._decide(snap, "COOLING", "score deteriorating", best)
                self._note_shadow(snap, evaluated, key, False)
                continue
            close = snap.session_end or snap.at.astimezone(ET).replace(hour=16, minute=0, second=0, microsecond=0)
            if close - snap.at <= timedelta(minutes=cfg.entry_cutoff_minutes):
                self._decide(snap, "RUNNER", "entry cutoff; tracking only", best)
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
                        self._decide(snap, "RUNNER", "cooldown or fresh reset required", best)
                        self._note_shadow(snap, evaluated, key, False)
                        continue
                elif not promotion and (snap.at - datetime.fromisoformat(last["at"]) < timedelta(minutes=cfg.ticker_cooldown_minutes)
                        or (key not in self.rearmed and last["side"] == best.option.side)):
                    self._decide(snap, "RUNNER", "cooldown or fresh reset required", best)
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
                self._decide(snap, candidate.state, "alert budget/ranking suppressed", candidate)
                continue
            phase = "POTENTIAL" if candidate.setup.building else "ACTIVE"
            last = self.store.last_alert(snap.day, snap.symbol)
            parent_id = (last["id"] if candidate.state == "ACTIVATION" and last else None)
            payload = alert_payload(candidate, phase, parent_id)
            extreme = session_extreme(snap, candidate.option.side) if cfg.cooldown_mode == 2 else None
            ident = self.store.queue(candidate, payload, self.dry_run, phase, parent_id, session_extreme=extreme,
                                     latency=self._card_latency(candidate))
            self._note_l2(candidate)
            self.rearmed.discard((snap.day, snap.symbol))
            self._decide(snap, candidate.state, f"{phase.lower()} alert {ident}", candidate)
            fired.add((snap.day, snap.symbol))
            alerts.append({"id": ident, "phase": phase, "parent_id": parent_id, "payload": payload})
        self._flush_shadow(fired)
        return alerts + self._lane_alerts

    def _clear_confirmation(self, key):
        for score_key in list(self.qualified_since):
            if score_key[:2] == key:
                self.qualified_since.pop(score_key, None)
                self.first_qual_wall.pop(score_key, None)

    def _decide(self, snap, state, reason, candidate=None, extra=None):
        note = None
        if self.settings.latency_log == 1 or extra:
            note = {}
            if self.settings.latency_log == 1:
                started = self._eval_at.get((snap.day, snap.symbol))
                if started is not None:
                    note["eval_start"] = started.isoformat()
                if snap.bars:
                    note["snapshot_bar_close"] = snap.bars[-1].end.isoformat()
            if extra:
                note.update(extra)
        self.store.decision(snap, state, reason, candidate, note)

    def _history_for(self, snap, history):
        seeds = [seed for seed in self.option_seeds.get(snap.symbol, ()) if seed.at < snap.at]
        if not seeds:
            return history
        return sorted([*history, *seeds], key=lambda item: item.at)

    def _would_qualify_seeded(self, snap, history):
        if self.settings.oh_seed_mode != 1:
            return None
        seeds = [seed for seed in self.option_seeds.get(snap.symbol, ()) if seed.at < snap.at]
        if not seeds:
            return 0
        trial = candidates(snap, sorted([*history, *seeds], key=lambda item: item.at), self.settings,
                           seed_gap=self.settings.oh_seed_max_gap_s)
        return int(any(item.qualifying for item in trial))

    def remember_seed(self, symbol, at, options, spot):
        """Keep a few sweep chains in memory. They are not written to the database."""
        if self.settings.oh_seed_mode < 1 or not options or not spot or spot <= 0:
            return
        seed = Snapshot(symbol, at, spot, at, spot, (), tuple(options), source="sweep-seed")
        ring = self.option_seeds[symbol]
        ring.append(seed)
        if len(ring) > 6:
            del ring[:-6]

    def _card_latency(self, candidate):
        if self.settings.latency_log != 1:
            return None
        snap = candidate.snapshot
        side = candidate.option.side
        score_key = (snap.day, snap.symbol, side, candidate.option.expiry.isoformat())
        started = self._eval_at.get((snap.day, snap.symbol))
        first = self.first_qual_wall.get(score_key)
        qual = self.qualified_since.get(score_key)
        trigger = candidate.setup.trigger if candidate.setup else None
        broken = break_bar_close(snap.bars, trigger, side)
        return {
            "break_bar_close": broken.isoformat() if broken else None,
            "qual_bar_close": qual.isoformat() if qual else None,
            "first_qual_at": first.isoformat() if first else None,
            "eval_start": started.isoformat() if started else None,
            "snapshot_bar_close": snap.bars[-1].end.isoformat() if snap.bars else None,
            "card_created": datetime.now(timezone.utc).isoformat(),
            "setup_trigger": trigger,
        }

    def _note_l2(self, candidate):
        if self.settings.bar_close_eval_mode != 1 or not candidate.snapshot.bars:
            return
        snap = candidate.snapshot
        bar = snap.bars[-1].end
        would = bar + timedelta(seconds=self.settings.bar_close_delay_s)
        hot = self.planned_hot or []
        try:
            rank = hot.index(snap.symbol) + 1
        except ValueError:
            rank = None
        self.store.record_shadow_card({
            "ts": would.isoformat(),
            "mode": "L2",
            "ticker": snap.symbol,
            "side": candidate.option.side,
            "setup": candidate.setup.name if candidate.setup else "",
            "contract": candidate.option.symbol,
            "live_at": snap.at.isoformat(),
            "hot_rank": rank,
            "would_eval_at": would.isoformat(),
        })

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
        # ATR gates on every same-side FINAL-B re-fire. Logging only; never feeds the gate.
        measured = None
        if ready and best is not None and cfg.rearm_atr_mode == 1 and fb_last:
            measured = rearm_atr_measure(snap.at, fb_last, best.option.side, snap)
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
            if candidate is best and measured is not None:
                dist, elapsed, in_window = measured
                bits = (int(dist >= .10), int(dist >= .15))
                if in_window:
                    variant = rearm_atr_variant(snap.at, fb_last, side, snap, cfg)
                    if variant is not None and variant != bits:
                        LOG.error("rearm ATR measure disagrees with rearm_atr_variant for %s", snap.symbol)
                        bits = variant
                row["rearm_atr_010"], row["rearm_atr_015"] = bits
                row["rearm_atr_dist"] = dist
                row["rearm_elapsed_min"] = elapsed
                row["rearm_in_window"] = in_window
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

    def _bench_view(self, snap):
        found = {}
        for symbol in ("SPY", "QQQ"):
            if snap.symbol == symbol:
                found[symbol] = snap
                continue
            prior = self.bench.get(symbol)
            if prior is not None and 0 <= (snap.at - prior.at).total_seconds() <= 90:
                found[symbol] = prior
        return found

    def _log_fix_shadow(self, snap, evaluated):
        if not evaluated or not fixes_active(self.settings):
            return
        best = {}
        for candidate in evaluated:
            side = candidate.option.side
            current = best.get(side)
            if current is None or candidate.score > current.score:
                best[side] = candidate
        rows = []
        for candidate in best.values():
            state = candidate.fix_state or {}
            rows.append({
                "ts": snap.at.isoformat(),
                "ticker": snap.symbol,
                "side": candidate.option.side,
                "contract": candidate.option.symbol,
                "live_decision": "pass" if candidate.qualifying else "block",
                "setup": candidate.setup.name if candidate.setup else "",
                "lm_ext_atr": state.get("lm_ext_atr"),
                "lm_hb": state.get("lm_hb"),
                "lm_fresh_lvl": state.get("lm_fresh_lvl"),
                "lm_rv2": state.get("lm_rv2"),
                "lm_block": state.get("lm_block") if self.settings.late_move_mode >= 1 else None,
                "mkt5": state.get("mkt5"),
                "mkt10": state.get("mkt10"),
                "mkt15": state.get("mkt15"),
                "mkt_state": state.get("mkt_state"),
                "mg_block": state.get("mg_block") if self.settings.mkt_gate_mode >= 1 else None,
                "fix_flags": shadow_flags(self.settings, state),
            })
        try:
            self.store.record_fix_shadow(rows)
        except Exception:
            LOG.exception("fix shadow log failed for %s; live path unchanged", snap.symbol)

    def _stock_first(self, snap, bench):
        """Options-history miss lane. Mode 1 logs. Mode 2 posts an ACTIVE card."""
        cfg = self.settings
        if not in_stock_first_window(snap.at):
            return []
        posted = []
        for direction, side in ((1, "CALL"), (-1, "PUT")):
            try:
                metrics, parts = stock_features(snap, direction)
            except ValueError:
                continue
            setup = detect_setup(snap, direction, metrics)
            if setup is None or setup.building:
                continue
            gap = trigger_gap(snap, direction, setup)
            if gap is None or not (-cfg.max_building_distance_atr <= gap <= cfg.max_entry_extension_atr):
                continue
            if not invalidation_holds(snap, direction, setup):
                continue
            ctx = lane_context(snap, side, cfg, bench)
            extension = ctx.get("lm_ext_atr")
            if extension is None or extension >= cfg.late_move_atr:
                continue
            if snap.symbol not in {"SPY", "QQQ", "SPX"} and ctx.get("mkt_state") != "aligned":
                continue
            cap_blocked, cooldown_blocked = self._lane_limits(snap, side)
            option = self._lane_contract(snap, side)
            no_contract = int(option is None)
            removed = int(cfg.fast_run_mode == 2 and ctx.get("fr_block"))
            afternoon = afternoon_level(snap, side, cfg) if cfg.afternoon_level_mode >= 1 else None
            afternoon_blocked = int(cfg.afternoon_level_mode == 2 and afternoon["al_block"])
            flags = json.loads(shadow_flags(cfg, ctx))
            flags["fr_removed"] = removed
            if afternoon is not None:
                flags["al"] = afternoon
            self.store.record_stock_first([{
                "ts": snap.at.isoformat(),
                "ticker": snap.symbol,
                "side": side,
                "setup": setup.name,
                "trigger": setup.trigger,
                "contract": option.symbol if option else "",
                "entry_bid": option.bid if option else None,
                "entry_ask": option.ask if option else None,
                "lm_ext_atr": extension,
                "mkt_state": ctx.get("mkt_state"),
                "cap_blocked": cap_blocked,
                "cooldown_blocked": cooldown_blocked,
                "no_contract": no_contract,
                "fix_flags": json.dumps(flags, separators=(",", ":")),
            }])
            if cap_blocked or cooldown_blocked or no_contract or removed or afternoon_blocked or cfg.stock_first_mode < 2:
                continue
            if self._lane_over_budget(snap):
                continue
            metrics = dict(metrics)
            metrics["quality_tier"] = "STOCK_FIRST"
            metrics["lm_ext_atr"] = extension
            metrics["mkt_state"] = ctx.get("mkt_state")
            candidate = Candidate(
                snap, option, round(sum(parts.values()), 2), parts, metrics,
                [f"Stock-first {setup.name}"], True, state="IGNITION", setup=setup)
            payload = alert_payload(candidate, "ACTIVE")
            extreme = session_extreme(snap, side) if cfg.cooldown_mode == 2 else None
            ident = self.store.queue(candidate, payload, self.dry_run, "ACTIVE", session_extreme=extreme)
            self.rearmed.discard((snap.day, snap.symbol))
            self._decide(snap, "IGNITION", f"active alert {ident}", candidate)
            posted.append({"id": ident, "phase": "ACTIVE", "parent_id": None, "payload": payload})
        return posted

    def _lane_over_budget(self, snap) -> bool:
        cfg = self.settings
        if cfg.max_alerts_per_day and self.store.count(snap.day) >= cfg.max_alerts_per_day:
            return True
        if cfg.max_alerts_per_ticker and self.store.count(snap.day, snap.symbol) >= cfg.max_alerts_per_ticker:
            return True
        return False

    def _lane_limits(self, snap, side):
        cfg = self.settings
        since5 = (snap.at - timedelta(minutes=5)).isoformat()
        last = self.store.last_alert(snap.day, snap.symbol)
        cooldown = False
        if last is not None:
            cooldown = live_cooldown_blocks(
                snap.at, datetime.fromisoformat(last["at"]), last["side"], side,
                (snap.day, snap.symbol) in self.rearmed, cfg)
        alert_times = {
            row["at"] for row in self.store.db.execute(
                "SELECT at FROM alerts WHERE day=? AND symbol=? AND side=? AND at>=?",
                (snap.day, snap.symbol, side, since5))
        }
        fires = 0
        for row in self.store.db.execute(
                """SELECT ts, cap_blocked, cooldown_blocked, no_contract, fix_flags
                   FROM stock_first_shadow WHERE ticker=? AND side=? AND ts>=?""",
                (snap.symbol, side, since5)):
            if row["cap_blocked"] or row["cooldown_blocked"] or row["no_contract"] or row["ts"] in alert_times:
                continue
            try:
                flags = json.loads(row["fix_flags"] or "{}")
            except ValueError:
                flags = {}
            if flags.get("fr_removed"):
                continue
            fires += 1
        cap_blocked = len(alert_times) + fires >= cfg.stock_first_cap
        return int(cap_blocked), int(cooldown)

    def _lane_contract(self, snap, side):
        day = snap.at.astimezone(ET).date()
        found = []
        for option in snap.options:
            if option.side != side:
                continue
            if not 0 <= (option.expiry - day).days <= self.settings.max_dte:
                continue
            if contract_ok(option, snap, self.settings):
                found.append(option)
        if not found:
            return None
        return min(found, key=lambda option: (abs(abs(option.delta) - 0.30), option.symbol))


def bars_confirm_after_gap(snap, candidate) -> bool:
    """Two completed 1m closes on the card side of a triggered (not building) setup."""
    setup = candidate.setup
    if setup is None or setup.building or setup.trigger is None or len(snap.bars) < 2:
        return False
    direction = 1 if candidate.option.side == "CALL" else -1
    return all(direction * (bar.close - setup.trigger) > 0 for bar in snap.bars[-2:])
