import json
import logging
import os
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .features import Candidate
from .models import ET
from .quality import DESCRIPTIONS

LOG = logging.getLogger(__name__)


def alert_payload(candidate: Candidate, phase: str, parent_id: str | None = None, quality=None) -> dict:
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
    title = ("👀 POTENTIAL TRADE WATCH" if phase == "POTENTIAL"
             else "⚡ RISKY OPENING DRIVE · ACTIVE TRADE IDEA" if candidate.metrics.get("opening_fast_lane")
                  and candidate.setup and candidate.setup.name == "OPENING_DRIVE"
             else "⚡ RISKY OPENING BREAK · ACTIVE TRADE IDEA" if candidate.metrics.get("opening_fast_lane")
             else "🏆🔥 ACTIVE TRADE IDEA 🔥🏆" if displayed_score >= 92
             else "🏆 ACTIVE TRADE IDEA 🏆" if displayed_score >= 90
             else "🚨 ACTIVE TRADE IDEA")
    levels = (f"{'Watch' if phase == 'POTENTIAL' else 'Trigger'} ${setup.trigger:.2f}"
              f" · Invalidation ${setup.invalidation:.2f}") if setup else ""
    payload = {"username": "Lotto Scanner", "allowed_mentions": {"parse": []}, "embeds": [{
        "title": title,
        "description": f"**{contract_root} {option.strike:g}{side} · {option.expiry.isoformat()} · {dte}DTE**\n"
                       f"Ask **${option.ask:.2f}** · Bid ${option.bid:.2f} · {underlying_label} ${snap.spot:.2f}\n"
                       f"{levels}\n"
                       f"Setup score **{displayed_score}/100**"
                       + (f" · {score_context}" if score_context else ""),
        "color": 0x2ECC71 if option.side == "CALL" else 0xE74C3C,
        "timestamp": snap.at.isoformat(),
    }]}
    if quality is not None:
        embed=payload['embeds'][0]
        if quality.tier == 'SECONDARY':
            embed['title']='B — SECONDARY TRADE IDEA'
            embed['color']=0xE6A23C
        details=[setup.name.replace('_',' ').title()] if setup else []
        details.extend(quality.labels)
        if quality.trend_escape:
            details.append('Trend confirmed')
        embed['description'] += '\n' + ' · '.join(details)
        if quality.reasons:
            embed['description'] += '\n' + '; '.join(DESCRIPTIONS.get(r,r) for r in quality.reasons)
    return payload


def deliver_pending(store, webhook: str, secondary_webhook: str | None = None) -> None:
    parsed = urlparse(webhook)
    if (parsed.scheme != "https" or parsed.hostname not in {"discord.com", "discordapp.com"}
            or not parsed.path.startswith("/api/webhooks/")):
        raise ValueError("A Discord HTTPS webhook URL is required for live delivery")
    secondary_webhook = (os.getenv('DISCORD_LOTTO_SECONDARY_WEBHOOK','')
                         if secondary_webhook is None else secondary_webhook)
    if secondary_webhook:
        target=urlparse(secondary_webhook)
        if target.scheme!='https' or target.hostname not in {'discord.com','discordapp.com'} or not target.path.startswith('/api/webhooks/'):
            raise ValueError('DISCORD_LOTTO_SECONDARY_WEBHOOK must be a Discord HTTPS webhook URL')
    # Older queued watches must also stay internal after a deployment/restart.
    with store.db:
        store.db.execute("UPDATE alerts SET delivery='internal' WHERE phase='POTENTIAL' AND delivery='pending'")
    for row in store.db.execute("SELECT id, at, payload, quality_tier FROM alerts WHERE phase='ACTIVE' AND delivery='pending' ORDER BY at").fetchall():
        target = secondary_webhook if row['quality_tier']=='SECONDARY' else webhook
        if not target:
            with store.db:
                store.db.execute("UPDATE alerts SET delivery='internal' WHERE id=?",(row['id'],))
            continue
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["at"])).total_seconds()
        if not 0 <= age <= 120:
            with store.db:
                store.db.execute("UPDATE alerts SET delivery='expired' WHERE id=?", (row["id"],))
            continue
        # Mark before sending: a crash after Discord accepts must not automatically resend.
        with store.db:
            store.db.execute("UPDATE alerts SET delivery='uncertain' WHERE id=?", (row["id"],))
        request = Request(target, data=row["payload"].encode(),
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
            if status == 'sent':
                store.db.execute('UPDATE alerts SET delivered_at=? WHERE id=?',
                                 (datetime.now(timezone.utc).isoformat(),row['id']))
        if status != "sent":
            break
