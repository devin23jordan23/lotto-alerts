import json
import logging
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .features import Candidate
from .models import ET

LOG = logging.getLogger(__name__)


def alert_payload(candidate: Candidate) -> dict:
    snap, option = candidate.snapshot, candidate.option
    dte = (option.expiry - snap.at.astimezone(ET).date()).days
    side = "C" if option.side == "CALL" else "P"
    setup = candidate.setup
    structure = (f"**{setup.name.replace('_', ' ').title()} · {'BUILDING' if setup.building else 'TRIGGERED'}**\n"
                 f"Watch ${setup.trigger:.2f} · Invalidation ${setup.invalidation:.2f}\n") if setup else ""
    metrics = candidate.metrics
    return {"username": "Lotto Scanner", "allowed_mentions": {"parse": []}, "embeds": [{
        "title": "🚨 POTENTIAL LOTTO" if candidate.state != "RUNNER" else "🚀 POTENTIAL RUNNER — NEW LEG",
        "description": f"**{snap.symbol} {option.strike:g}{side} · {option.expiry.isoformat()} · {dte}DTE**\n"
                       f"Ask **${option.ask:.2f}** · Bid ${option.bid:.2f}\n"
                       + structure + f"Stock ${snap.spot:.2f} · From open {metrics['return_from_open']:+.2%} · From prior close {metrics['return_from_close']:+.2%}\n"
                       f"Setup score: **{candidate.score:.0f}/100** ({candidate.score_change:+.1f} over 3m)\n" + " · ".join(candidate.reasons),
        "color": 0x2ECC71 if option.side == "CALL" else 0xE74C3C,
        "timestamp": snap.at.isoformat(),
    }]}


def deliver_pending(store, webhook: str) -> None:
    parsed = urlparse(webhook)
    if (parsed.scheme != "https" or parsed.hostname not in {"discord.com", "discordapp.com"}
            or not parsed.path.startswith("/api/webhooks/")):
        raise ValueError("A Discord HTTPS webhook URL is required for live delivery")
    for row in store.db.execute("SELECT id, at, payload FROM alerts WHERE delivery='pending' ORDER BY at").fetchall():
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["at"])).total_seconds()
        if not 0 <= age <= 120:
            with store.db:
                store.db.execute("UPDATE alerts SET delivery='expired' WHERE id=?", (row["id"],))
            continue
        # Mark before sending: a crash after Discord accepts must not automatically resend.
        with store.db:
            store.db.execute("UPDATE alerts SET delivery='uncertain' WHERE id=?", (row["id"],))
        request = Request(webhook, data=row["payload"].encode(),
                          headers={"Content-Type": "application/json", "User-Agent": "LottoScanner/0.1"})
        status = "sent"
        try:
            with urlopen(request, timeout=10) as response:
                response.read()
        except HTTPError as exc:
            status = "pending" if exc.code == 429 else "failed" if 400 <= exc.code < 500 else "uncertain"
            LOG.warning("Discord delivery %s: HTTP %s (%s)", row["id"], exc.code, status)
        except (URLError, TimeoutError, OSError):
            status = "uncertain"
            LOG.warning("Discord delivery %s uncertain; check channel before any manual resend", row["id"])
        with store.db:
            store.db.execute("UPDATE alerts SET delivery=? WHERE id=?", (status, row["id"]))
        if status != "sent":
            break
