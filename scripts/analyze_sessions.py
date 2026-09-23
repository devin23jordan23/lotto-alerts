"""Analyze the captured case studies; does not synthesize historical option quotes."""
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lotto.research import price_replay

root = Path(sys.argv[1])
results = []
for symbol, day in (("TSM","2026-09-22"),("GOOGL","2026-09-22"),("SKHY","2026-09-22"),
                    ("META","2026-09-21"),("MU","2026-09-22"),("QQQ","2026-09-22"),("SPY","2026-09-22")):
    path = root/f"{symbol}.json"
    if not path.exists():
        continue
    result = price_replay(symbol,day,json.loads(path.read_text()))
    results.append(result)
    seen = set()
    compact = []
    for finding in result.get("price_setups",[]):
        key = finding["side"],finding["setup"]
        if key not in seen:
            seen.add(key)
            compact.append({"at":finding["at"], "side":key[0], "setup":key[1], "spot":finding["spot"],
                            "rvol":round(finding["features_at_time"]["pace_rvol"],2),
                            "next30m":finding["future_labels"]["return_30m_directional"]})
    print(json.dumps({"symbol":symbol,"day":day,"first_price_setups":compact}))
(root/"analysis.json").write_text(json.dumps(results,indent=2))
