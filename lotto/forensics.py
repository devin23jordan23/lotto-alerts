"""Export a bounded, saved market-data case for offline reconstruction."""
import base64
import gzip
import hashlib
import json
from datetime import datetime, timezone

from .models import ET
from .store import read_snapshot


def export_case(store, day, symbol, start="09:30", end="12:10"):
    opening = datetime.fromisoformat(f"{day}T{start}").replace(tzinfo=ET)
    closing = datetime.fromisoformat(f"{day}T{end}").replace(tzinfo=ET)
    if not 0 < (closing-opening).total_seconds() <= 8*3600:
        raise ValueError("Case window must be at most eight hours")
    symbol = symbol.upper()
    if not symbol.isalnum() or len(symbol) > 8:
        raise ValueError("Invalid case symbol")
    bounds = (symbol, opening.astimezone(timezone.utc).isoformat(), closing.astimezone(timezone.utc).isoformat())
    records = {}
    for table in ("discovery", "chain_coverage", "decisions", "snapshots"):
        rows = store.db.execute(f"SELECT * FROM {table} WHERE symbol=? AND at>=? AND at<=? ORDER BY at", bounds)
        records[table] = []
        for row in rows:
            value = dict(row)
            if table == "snapshots":
                value["payload"] = read_snapshot(value["payload"]).to_dict()
            elif table == "discovery":
                value["payload"] = json.loads(value["payload"])
            elif table == "decisions":
                value["features"] = json.loads(value["features"])
            records[table].append(value)
    case = {"day":day,"symbol":symbol,"start":opening.isoformat(),"end":closing.isoformat(),
            "record_counts":{k:len(v) for k,v in records.items()},"records":records}
    packed = gzip.compress(json.dumps(case,separators=(",", ":")).encode())
    encoded = base64.b64encode(packed).decode()
    digest = hashlib.sha256(packed).hexdigest()
    chunks = [encoded[i:i+2000] for i in range(0,len(encoded),2000)]
    return packed, [{"case":f"{day}-{symbol}","sha256":digest,"part":i,"parts":len(chunks),"data":chunk}
                    for i,chunk in enumerate(chunks)]
