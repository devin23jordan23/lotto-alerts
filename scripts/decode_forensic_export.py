"""Verify and decode market-data export chunks retrieved from Railway logs."""
import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("input")
parser.add_argument("output")
args = parser.parse_args()
parts = {}
for line in Path(args.input).read_text().splitlines():
    try:
        row = json.loads(line)
    except ValueError:
        continue
    message = row.get("message", "")
    if "Forensic export: " in message:
        part = json.loads(message.split("Forensic export: ",1)[1])
        parts[part["part"]] = part
if not parts:
    raise SystemExit("No export parts")
first = next(iter(parts.values()))
if len(parts) != first["parts"] or set(parts) != set(range(first["parts"])):
    raise SystemExit(f"Incomplete export: {len(parts)}/{first['parts']} parts")
if any(p["sha256"] != first["sha256"] or p["case"] != first["case"] for p in parts.values()):
    raise SystemExit("Mixed export runs")
packed = base64.b64decode("".join(parts[i]["data"] for i in range(first["parts"])))
if hashlib.sha256(packed).hexdigest() != first["sha256"]:
    raise SystemExit("Export checksum mismatch")
raw = gzip.decompress(packed)
case = json.loads(raw)
Path(args.output).write_bytes(raw)
print(json.dumps({"case":first["case"],"sha256":first["sha256"],"record_counts":case["record_counts"]}))
