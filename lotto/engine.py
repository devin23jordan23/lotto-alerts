from collections import defaultdict
from datetime import datetime, timedelta

from .config import Settings
from .discord import alert_payload
from .features import candidates
from .models import ET, Snapshot
from .store import Store


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

    def process(self, snapshots: list[Snapshot]) -> list[dict]:
        ready = []
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
            problems = snap.problems(cfg.max_quote_age_seconds)
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
                continue
            self.cool_since.pop(key, None)
            best = max(valid, key=lambda c: c.score)
            score_key = (*key, best.option.side, best.option.expiry.isoformat(), best.setup.name)
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
            self.qualified_since.setdefault(score_key, snap.at)
            if snap.at - self.qualified_since[score_key] < timedelta(minutes=cfg.confirmation_minutes):
                self.store.decision(snap, "IGNITION", "confirming persistence", best)
                continue
            # Flat strong scores can persist; materially deteriorating scores cannot alert.
            if best.score_change < -2:
                self.store.decision(snap, "COOLING", "score deteriorating", best)
                continue
            close = snap.session_end or snap.at.astimezone(ET).replace(hour=16, minute=0, second=0, microsecond=0)
            if close - snap.at <= timedelta(minutes=cfg.entry_cutoff_minutes):
                self.store.decision(snap, "RUNNER", "entry cutoff; tracking only", best)
                continue
            last = self.store.last_alert(*key)
            if last:
                best.state = "RUNNER"
                if (snap.at - datetime.fromisoformat(last["at"]) < timedelta(minutes=cfg.ticker_cooldown_minutes)
                        or key not in self.rearmed):
                    self.store.decision(snap, "RUNNER", "cooldown or fresh reset required", best)
                    continue
            else:
                best.state = "IGNITION"
            if self.store.already_alerted(snap.day, best.option.symbol):
                self.store.decision(snap, "RUNNER", "contract already alerted today", best)
                continue
            ready.append(best)
        alerts = []
        # Rank the whole scan cycle before spending the daily budget.
        for candidate in sorted(ready, key=lambda c: (-(c.score + 2*min(8, max(-8, c.score_change))), -c.score, c.snapshot.symbol)):
            snap = candidate.snapshot
            if (len(alerts) >= cfg.max_alerts_per_cycle
                    or self.store.count(snap.day) >= cfg.max_alerts_per_day
                    or self.store.count(snap.day, snap.symbol) >= cfg.max_alerts_per_ticker):
                self.store.decision(snap, candidate.state, "alert budget/ranking suppressed", candidate)
                continue
            payload = alert_payload(candidate)
            ident = self.store.queue(candidate, payload, self.dry_run)
            self.rearmed.discard((snap.day, snap.symbol))
            self.store.decision(snap, candidate.state, f"potential alert {ident}", candidate)
            alerts.append({"id": ident, "payload": payload})
        return alerts

    def _clear_confirmation(self, key):
        for score_key in list(self.qualified_since):
            if score_key[:2] == key:
                self.qualified_since.pop(score_key, None)
