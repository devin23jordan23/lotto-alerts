import json
import logging
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .features import Candidate
from .models import ET

LOG = logging.getLogger(__name__)


def alert_payload(candidate: Candidate, phase: str, parent_id: str | None = None) -> dict:
    snap, option = candidate.snapshot, candidate.option
    contract_root = option.symbol.split()[0] if snap.symbol == "SPX" else snap.symbol
    underlying_label = "Index" if snap.symbol == "SPX" else "Stock"
    dte = (option.expiry - snap.at.astimezone(ET).date()).days
    side = "C" if option.side == "CALL" else "P"
    setup = candidate.setup
    context = next((reason for reason in candidate.reasons
                    if reason.startswith("Outperforming ") or reason.endswith(" supportive")), None)
    peers = next((reason for reason in candidate.reasons if reason.startswith("Peers: ")), None)
    score_context = " · ".join(part for part in (context, peers) if part)
    displayed_score = round(candidate.score)
    title = ("STOCK-FIRST IDEA (options history thin)" if candidate.metrics.get("quality_tier") == "STOCK_FIRST"
             else "👀 POTENTIAL TRADE WATCH" if phase == "POTENTIAL"
             else "⚡ RISKY OPENING DRIVE · ACTIVE TRADE IDEA" if candidate.metrics.get("opening_fast_lane")
                  and candidate.setup and candidate.setup.name == "OPENING_DRIVE"
             else "⚡ RISKY OPENING BREAK · ACTIVE TRADE IDEA" if candidate.metrics.get("opening_fast_lane")
             else "🏆🔥 ACTIVE TRADE IDEA 🔥🏆" if displayed_score >= 92
             else "🏆 ACTIVE TRADE IDEA 🏆" if displayed_score >= 90
             else "🚨 ACTIVE TRADE IDEA")
    levels = (f"{'Watch' if phase == 'POTENTIAL' else 'Trigger'} ${setup.trigger:.2f}"
              f" · Invalidation ${setup.invalidation:.2f}") if setup else ""
    return {"username": "Lotto Scanner", "allowed_mentions": {"parse": []}, "embeds": [{
        "title": title,
        "description": f"**{contract_root} {option.strike:g}{side} · {option.expiry.isoformat()} · {dte}DTE**\n"
                       f"Ask **${option.ask:.2f}** · Bid ${option.bid:.2f} · {underlying_label} ${snap.spot:.2f}\n"
                       f"{levels}\n"
                       f"Setup score **{displayed_score}/100**"
                       + (f" · {score_context}" if score_context else ""),
        "color": 0x2ECC71 if option.side == "CALL" else 0xE74C3C,
        "timestamp": snap.at.isoformat(),
    }]}


# A burst must not sit in Retry-After forever. Three attempts, then the next card.
DISCORD_429_ATTEMPTS = 3


def retry_after_seconds(exc) -> float:
    """Discord sends retry_after in the JSON body and sometimes in Retry-After."""
    raw = None
    try:
        body = exc.read()
        if body:
            raw = json.loads(body.decode() if isinstance(body, bytes) else body).get("retry_after")
    except Exception:
        raw = None
    if raw is None and getattr(exc, "headers", None) is not None:
        raw = exc.headers.get("Retry-After")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def stamp_would_send(store, alerts, now=None):
    """Mode 1 of inline delivery: record the time, do not post."""
    now = now or datetime.now(timezone.utc)
    ident = [item["id"] for item in alerts if item.get("phase") == "ACTIVE" and item.get("id")]
    if not ident:
        return
    with store.db:
        store.db.executemany(
            "UPDATE alerts SET inline_would_send_at=? WHERE id=? AND inline_would_send_at IS NULL",
            [(now.isoformat(), item) for item in ident])


def publish_ready(alerts, mode, live, deliver, stamp):
    """Run the moment cards are built. Mode 0 leaves the end-of-cycle send in charge."""
    if mode == 0 or not alerts:
        return
    if mode == 1:
        stamp(alerts)
        return
    if live:
        deliver()


def deliver_pending(store, webhook: str, latency_log: int = 0, retry_429: int = 0, sleeper=None) -> None:
    parsed = urlparse(webhook)
    if (parsed.scheme != "https" or parsed.hostname not in {"discord.com", "discordapp.com"}
            or not parsed.path.startswith("/api/webhooks/")):
        raise ValueError("A Discord HTTPS webhook URL is required for live delivery")
    sleeper = sleeper or time.sleep
    # Older queued watches must also stay internal after a deployment/restart.
    with store.db:
        store.db.execute("UPDATE alerts SET delivery='internal' WHERE phase='POTENTIAL' AND delivery='pending'")
    for row in store.db.execute("SELECT id, at, payload FROM alerts WHERE phase='ACTIVE' AND delivery='pending' ORDER BY at").fetchall():
        status = "pending"
        attempts = 0
        while True:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["at"])).total_seconds()
            if not 0 <= age <= 120:
                status = "expired"
                break
            # Mark before sending: a crash after Discord accepts must not automatically resend.
            with store.db:
                store.db.execute("UPDATE alerts SET delivery='uncertain' WHERE id=?", (row["id"],))
            request = Request(webhook, data=row["payload"].encode(),
                              headers={"Content-Type": "application/json", "User-Agent": "LottoScanner/0.1"})
            try:
                with urlopen(request, timeout=10) as response:
                    response.read()
                status = "sent"
                break
            except HTTPError as exc:
                status = "pending" if exc.code == 429 else "failed" if 400 <= exc.code < 500 else "uncertain"
                LOG.warning("Discord delivery %s: HTTP %s (%s)", row["id"], exc.code, status)
                if exc.code == 429:
                    attempts += 1
                    if latency_log == 1 or retry_429 >= 1:
                        with store.db:
                            store.db.execute(
                                "UPDATE alerts SET delivery_attempts=COALESCE(delivery_attempts,0)+1 WHERE id=?",
                                (row["id"],))
                    if retry_429 >= 1 and attempts < DISCORD_429_ATTEMPTS:
                        wait = retry_after_seconds(exc)
                        if age + wait > 120:
                            LOG.warning("Discord delivery %s not retried; age would pass 120s", row["id"])
                            break
                        if wait:
                            sleeper(wait)
                        continue
                break
            except (URLError, TimeoutError, OSError):
                status = "uncertain"
                LOG.warning("Discord delivery %s uncertain; check channel before any manual resend", row["id"])
                break
        with store.db:
            if latency_log == 1 and status == "sent":
                store.db.execute("UPDATE alerts SET delivery=?, delivered_at=? WHERE id=?",
                                 (status, datetime.now(timezone.utc).isoformat(), row["id"]))
            else:
                store.db.execute("UPDATE alerts SET delivery=? WHERE id=?", (status, row["id"]))
        if status == "expired":
            continue
        if status != "sent" and retry_429 < 1:
            break
        if status != "sent" and status != "pending":
            break
