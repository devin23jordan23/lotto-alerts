"""Fetch read-only Schwab minute history for reproducible session research.

Credentials remain in memory. Only market-data responses are written to disk.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lotto.models import ET
from lotto.schwab import Schwab, epoch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", required=True)
    parser.add_argument("--symbols", default="TSM,GOOGL,SKHY,META,MU,SMH,QQQ,SPY")
    parser.add_argument("--railway-project")
    parser.add_argument("--railway-service")
    parser.add_argument("--extended", action="store_true", help="Also save target-session extended-hours bars and prior daily history")
    args = parser.parse_args()
    if args.railway_project:
        result = subprocess.run(["npx", "--yes", "@railway/cli", "variable", "list", "--project",
                                 args.railway_project, "--service", args.railway_service,
                                 "--environment", "production", "--json"], capture_output=True, text=True)
        if result.returncode:
            raise SystemExit("Could not read authorized Railway market-data configuration")
        for key, value in json.loads(result.stdout).items():
            if key.startswith("SCHWAB_") and isinstance(value, str):
                os.environ[key] = value
        broker = urlsplit(os.getenv("SCHWAB_TOKEN_BROKER_URL", ""))
        if broker.hostname and broker.hostname.endswith(".railway.internal"):
            status = subprocess.run(["npx", "--yes", "@railway/cli", "status", "--project", args.railway_project,
                                     "--environment", "production", "--json"], capture_output=True, text=True)
            if status.returncode == 0:
                service_name = broker.hostname.removesuffix(".railway.internal")
                for env in json.loads(status.stdout)["environments"]["edges"]:
                    for item in env["node"]["serviceInstances"]["edges"]:
                        service = item["node"]
                        domains = service.get("domains", {}).get("serviceDomains", [])
                        if service.get("serviceName") == service_name and domains:
                            os.environ["SCHWAB_TOKEN_BROKER_URL"] = urlunsplit(("https", domains[0]["domain"], broker.path, broker.query, ""))
    end = datetime.fromisoformat(args.day).replace(hour=16, tzinfo=ET)
    root = Path("data/research") / args.day
    root.mkdir(parents=True, exist_ok=True)
    client = Schwab(str(root / "auth"))
    for symbol in args.symbols.split(","):
        try:
            response = client.candles(symbol, end-timedelta(days=45), end)
            (root / f"{symbol}.json").write_text(json.dumps(response))
            if args.extended:
                extended = client.get("/pricehistory", {"symbol":symbol,"periodType":"day",
                    "frequencyType":"minute","frequency":1,
                    "startDate":int(end.replace(hour=0).timestamp()*1000),
                    "endDate":int(end.timestamp()*1000),"needExtendedHoursData":"true"})
                (root / f"{symbol}-extended.json").write_text(json.dumps(extended))
                daily = client.get("/pricehistory", {"symbol":symbol,"periodType":"year","period":1,
                    "frequencyType":"daily","frequency":1,
                    "endDate":int((end-timedelta(days=1)).replace(hour=23,minute=59).timestamp()*1000),
                    "needExtendedHoursData":"false"})
                (root / f"{symbol}-daily.json").write_text(json.dumps(daily))
            sessions = {}
            for candle in response.get("candles", []):
                at = epoch(candle.get("datetime"))
                if at:
                    sessions.setdefault(at.astimezone(ET).date().isoformat(), []).append(candle)
            summary = []
            for day in ("2026-09-21", args.day):
                bars = sessions.get(day, [])
                if bars:
                    summary.append({"day": day, "bars": len(bars), "open": bars[0]["open"],
                                    "close": bars[-1]["close"], "high": max(b["high"] for b in bars),
                                    "low": min(b["low"] for b in bars)})
            print(json.dumps({"symbol": symbol, "sessions": summary, "saved": str(root / f"{symbol}.json")}), flush=True)
        except Exception as exc:
            reason = getattr(exc, "reason", None)
            print(json.dumps({"symbol": symbol, "error": type(exc).__name__, "reason_type": type(reason).__name__,
                              "reason_errno": getattr(reason, "errno", None)}), flush=True)


if __name__ == "__main__":
    main()
