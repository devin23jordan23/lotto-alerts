import json
import logging
import os
import re
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
             else "GREEKS UNAVAILABLE · ACTIVE TRADE IDEA" if option.direct_quote_verified
             else "RISKY OPENING DRIVE · ACTIVE TRADE IDEA" if candidate.metrics.get("opening_fast_lane")
                  and candidate.setup and candidate.setup.name == "OPENING_DRIVE"
             else "RISKY OPENING BREAK · ACTIVE TRADE IDEA" if candidate.metrics.get("opening_fast_lane")
             else "🏆🔥 ACTIVE TRADE IDEA 🔥🏆" if displayed_score >= 92
             else "🏆 ACTIVE TRADE IDEA 🏆" if displayed_score >= 90
             else "🚨 ACTIVE TRADE IDEA")
    tier = candidate.metrics.get("tier")
    if tier in {"A", "B", "C"}:
        title = f"{tier} · {title}"
    marker = candidate.metrics.get("strength_marker")
    if marker:
        title = f"{marker} {title}"
    levels = (f"{'Watch' if phase == 'POTENTIAL' else 'Trigger'} ${setup.trigger:.2f}"
              f" · Invalidation ${setup.invalidation:.2f}") if setup else ""
    bypass = candidate.metrics.get("path") == "bypass"
    score_label = (f"Stock bypass score **{displayed_score}** (minimum {candidate.metrics.get('bypass_threshold', 40):g})"
                   if bypass else f"Scanner score **{displayed_score}**")
    payload = {"username": "Lotto Scanner", "allowed_mentions": {"parse": []}, "embeds": [{
        "title": title,
        "description": f"**{contract_root} {option.strike:g}{side} · {option.expiry.isoformat()} · {dte}DTE**\n"
                       f"Ask **${option.ask:.2f}** · Bid ${option.bid:.2f} · {underlying_label} ${snap.spot:.2f}\n"
                       f"{levels}\n"
                       + score_label
                       + (f" · {score_context}" if score_context else ""),
        "color": 0x2ECC71 if option.side == "CALL" else 0xE74C3C,
        "timestamp": snap.at.isoformat(),
    }]}
    if tier in {"A", "B", "C"}:
        rules = ", ".join(candidate.metrics.get("s_rules", ())) or "none"
        payload["embeds"][0]["description"] += (f"\nTier {tier} · stock/option evidence blend "
            f"{candidate.metrics['tier_blend']:.1f} · caution labels: {rules}")
        if candidate.metrics.get("tier_motion_penalty"):
            payload["embeds"][0]["description"] += " · stock move not yet confirmed"
        if candidate.metrics.get("repeat_number"):
            payload["embeds"][0]["description"] += f" · repeat #{candidate.metrics['repeat_number']}"
    if candidate.metrics.get("send_requote") and not bypass:
        payload["embeds"][0]["description"] += (f"\nChain quote <t:{int(option.quote_time.timestamp())}:T> "
                                                   f"(<t:{int(option.quote_time.timestamp())}:R>)")
    if quality is not None:
        embed=payload['embeds'][0]
        if quality.tier == 'SECONDARY':
            embed['title']=('GREEKS UNAVAILABLE · ' if option.direct_quote_verified else '')+'B — SECONDARY TRADE IDEA'
            embed['color']=0xE6A23C
        details=[setup.name.replace('_',' ').title()] if setup else []
        details.extend(quality.labels)
        if quality.trend_escape:
            details.append('Trend confirmed')
        embed['description'] += '\n' + ' · '.join(details)
        if option.direct_quote_verified:
            embed['description'] += '\nSeparate Schwab contract quote refreshed; provider Greeks unavailable'
        if quality.reasons:
            embed['description'] += '\n' + '; '.join(DESCRIPTIONS.get(r,r) for r in quality.reasons)
    notes = []
    count = candidate.metrics.get("confluence_count")
    total = candidate.metrics.get("confluence_total")
    if not bypass and count is not None and total:
        notes.append(f"{count}/{total} confluence")
    if candidate.metrics.get("stale_quote") and not bypass:
        notes.append("stale quote")
    if candidate.metrics.get("spread_flag"):
        notes.append(candidate.metrics["spread_flag"])
    if candidate.metrics.get("bypass_reason"):
        notes.append(candidate.metrics["bypass_reason"])
    overrides = candidate.metrics.get("overrides") or []
    if overrides:
        notes.append("override " + ", ".join(overrides))
    if notes:
        payload["embeds"][0]["description"] += "\n" + "\n".join(notes)
    return payload


def render_send_card(payload: dict, row, quote: dict | None, now: datetime) -> dict:
    """Show what the market actually quoted at delivery; never silently reprice."""
    payload = json.loads(json.dumps(payload))
    embed = payload["embeds"][0]
    try:
        original = json.loads(row["snapshot"])
        signal_at = next((datetime.fromisoformat(o["quote_time"]) for o in original["options"]
                          if o["symbol"] == row["contract"]), datetime.fromisoformat(row["at"]))
    except (KeyError, ValueError, TypeError):
        signal_at = datetime.fromisoformat(row["at"])
    signal_age = max(0, round((now-signal_at).total_seconds()))
    if quote and quote.get("realtime") is True:
        qt = quote.get("quote_time")
        age = (now-qt).total_seconds() if isinstance(qt, datetime) else -1
        if (0 <= age <= 30 and quote.get("bid", 0) > 0
                and quote.get("ask", 0) >= quote["bid"] and quote.get("spot", 0) > 0):
            underlying = "Index" if row["symbol"] == "SPX" else "Stock"
            new_line = (f"Ask **${quote['ask']:.2f}** · Bid ${quote['bid']:.2f} · "
                        f"{underlying} ${quote['spot']:.2f}")
            embed["description"] = re.sub(
                r"Ask \*\*\$[\d.]+\*\* · Bid \$[\d.]+ · (?:Stock|Index) \$[\d.]+",
                new_line, embed["description"], count=1)
            label = "aging quote" if age >= 15 else "live quote"
            if row["setup_name"] != "STOCK_BYPASS":
                embed["description"] += (f"\n{label} · Quote {round(age)}s old at send · "
                                         f"Schwab /quotes · <t:{int(qt.timestamp())}:R>")
            if row["entry_ask"] and abs(quote["ask"]/row["entry_ask"]-1) >= .10:
                embed["description"] += f"\nSignal ask ${row['entry_ask']:.2f} when queued"
            return payload
    embed["description"] += (f"\n⚠ STALE QUOTE ({signal_age}s) · re-quote failed; "
                             "the displayed bid/ask came from the earlier chain")
    return payload


def deliver_pending(store, webhook: str, secondary_webhook: str | None = None, quoter=None) -> None:
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
    for row in store.db.execute("""SELECT id, at, payload, quality_tier, contract, symbol, setup_name,
                                   entry_ask, snapshot FROM alerts WHERE phase='ACTIVE'
                                   AND delivery='pending' ORDER BY at""").fetchall():
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
        outgoing = json.loads(row["payload"])
        if quoter is not None:
            quote = None
            try:
                quote = quoter(row["contract"], row["symbol"])
            except Exception as exc:
                LOG.warning("Send re-quote %s unavailable (%s)", row["id"], type(exc).__name__)
            send_now = datetime.now(timezone.utc)
            qt = quote.get("quote_time") if isinstance(quote, dict) else None
            try:
                quote_age = (send_now - qt).total_seconds() if isinstance(qt, datetime) else -1
            except TypeError:
                quote_age = -1
            if not (isinstance(quote, dict) and quote.get("realtime") is True
                    and 0 <= quote_age <= 30 and quote.get("bid", 0) > 0
                    and quote.get("ask", 0) >= quote["bid"] and quote.get("spot", 0) > 0):
                with store.db:
                    store.db.execute("UPDATE alerts SET delivery='stale_quote' WHERE id=?", (row["id"],))
                LOG.warning("Alert %s withheld: no fresh send-time quote", row["id"])
                continue
            outgoing = render_send_card(outgoing,row,quote,send_now)
        encoded = json.dumps(outgoing)
        # Mark before sending: a crash after Discord accepts must not automatically resend.
        with store.db:
            store.db.execute("UPDATE alerts SET delivery='uncertain',payload=? WHERE id=?", (encoded,row["id"]))
        request = Request(target, data=encoded.encode(),
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


def deliver_stock_pending(store, webhook: str) -> None:
    """Stock-only cards never post. Pending rows are retained as internal."""
    del webhook
    with store.db:
        store.db.execute("UPDATE stock_ideas SET delivery='internal' WHERE delivery='pending'")
