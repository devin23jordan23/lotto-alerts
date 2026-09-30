"""One incident notification for a sustained scan outage and its recovery."""
import json
from datetime import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .models import ET


def watch_hours(now):
    local = now.astimezone(ET)
    return local.weekday() < 5 and time(9) <= local.time() < time(16)


def post_status(webhook, payload):
    parsed = urlparse(webhook)
    if (parsed.scheme != "https" or parsed.hostname not in {"discord.com", "discordapp.com"}
            or not parsed.path.startswith("/api/webhooks/")):
        raise ValueError("A Discord HTTPS webhook URL is required")
    request = Request(webhook, data=json.dumps(payload).encode(),
                      headers={"Content-Type":"application/json","User-Agent":"LottoScanner/0.2"})
    with urlopen(request, timeout=10) as response:
        response.read()


class HealthMonitor:
    def __init__(self, store, webhook, sender=post_status):
        self.store, self.webhook, self.sender = store, webhook, sender
        self.failures = 0

    def failed_cycle(self, now, reason):
        if not watch_hours(now):
            self.failures = 0
            return False
        self.failures += 1
        if self.failures < 3:
            return False
        incident = self.store.db.execute(
            "SELECT id,notified_at FROM health_incidents WHERE recovered_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        if incident is None:
            with self.store.db:
                cursor = self.store.db.execute(
                    "INSERT INTO health_incidents (opened_at,reason) VALUES (?,?)", (now.isoformat(),reason))
            ident, notified = cursor.lastrowid, None
        else:
            ident, notified = incident["id"], incident["notified_at"]
        if notified:
            return False
        self.sender(self.webhook, {"username":"Lotto Scanner","allowed_mentions":{"parse":[]},
            "embeds":[{"title":"⚠️ Lotto scanner data unavailable",
                       "description":f"Three consecutive scan cycles failed. No new lotto ideas can be evaluated. Cause: {reason}.",
                       "color":0xE67E22,"timestamp":now.isoformat()}]})
        with self.store.db:
            self.store.db.execute("UPDATE health_incidents SET notified_at=? WHERE id=?",
                                  (now.isoformat(),ident))
        return True

    def successful_cycle(self, now, observations):
        if not observations:
            return False
        self.failures = 0
        incident = self.store.db.execute(
            "SELECT id,notified_at FROM health_incidents WHERE recovered_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        if incident is None:
            return False
        if incident["notified_at"]:
            self.sender(self.webhook, {"username":"Lotto Scanner","allowed_mentions":{"parse":[]},
                "embeds":[{"title":"✅ Lotto scanner data restored",
                           "description":f"Market-data scans resumed; {observations} names received deep observations this cycle.",
                           "color":0x2ECC71,"timestamp":now.isoformat()}]})
        with self.store.db:
            self.store.db.execute("UPDATE health_incidents SET recovered_at=? WHERE id=?",
                                  (now.isoformat(),incident["id"]))
        return bool(incident["notified_at"])
