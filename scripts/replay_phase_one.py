"""Replay saved market snapshots without network, Discord, or order access.

These selected/truncated cases are regression research, not an out-of-sample
backtest. Each case starts cold and has its own alert budget; a live combined
universe may rank competing candidates differently.
"""
import argparse
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lotto.config import Settings, STRATEGY_VERSION
from lotto.engine import Engine
from lotto.models import ET, Snapshot
from lotto.store import Store


def replay_case(path):
    raw = path.read_bytes()
    case = json.loads(raw)
    snapshots = sorted((Snapshot.from_dict(r["payload"]) for r in case["records"]["snapshots"]),key=lambda s:s.at)
    store = Store(":memory:")
    try:
        engine = Engine(store,Settings(),dry_run=True)
        for snap in snapshots:
            engine.process([snap])
        alerts = []
        for row in store.summary():
            decision = store.db.execute("SELECT features FROM decisions WHERE at=? AND reason LIKE 'potential alert %'",(row["at"],)).fetchone()
            features = json.loads(decision[0])
            alerts.append({**row,"setup":features["setup"],"metrics":features["metrics"]})
        blockers = Counter()
        for row in store.db.execute("SELECT features FROM candidate_evaluations"):
            blockers.update(json.loads(row[0])["blockers"])
        # Compare rules at every actual old alert minute, not only at new winners.
        old_alerts = []
        for old in case["records"].get("decisions",[]):
            if not old["reason"].startswith("potential alert "):
                continue
            prior = old["features"]
            if isinstance(prior,str):
                prior = json.loads(prior)
            evaluated = []
            for row in store.db.execute("SELECT contract,qualifying,score,features FROM candidate_evaluations WHERE at=?",(old["at"],)):
                features = json.loads(row["features"])
                if features["side"]==prior.get("side"):
                    evaluated.append({"contract":row["contract"],"qualifying":bool(row["qualifying"]),
                                      "score":row["score"],"setup":features["setup"],"blockers":features["blockers"]})
            old_alerts.append({"at":old["at"],"original_contract":prior.get("contract"),"phase_one_evaluations":evaluated})
        return {"source":str(path),"sha256":sha256(raw).hexdigest(),"symbol":case["symbol"],"day":case["day"],
                "observations":len(snapshots),"option_filter":case.get("option_filter"),
                "observed_until":snapshots[-1].at.isoformat() if snapshots else None,
                "replay_alerts":alerts,"recorded_alert_comparisons":old_alerts,"blockers":dict(blockers)}
    finally:
        store.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exports",nargs="+",type=Path)
    parser.add_argument("--output",type=Path,default=Path("data/research/phase1-replay.json"))
    args = parser.parse_args()
    result = {"strategy":STRATEGY_VERSION,"settings":asdict(Settings()),
              "limitations":["User-selected cases; no inferred win rate.",
                             "Sampled ask-to-bid outcomes are not fills or attainable peak exits.",
                             "Exports may truncate contracts, history, and future outcomes.",
                             "Cases replay independently; live ranking and poll timing may differ."],"cases":[]}
    for path in args.exports:
        case = replay_case(path)
        result["cases"].append(case)
        print(json.dumps({"case":path.name,"alerts":[{"at":a["at"],"contract":a["contract"],"ask":a["entry_ask"],
              "sampled_peak_return":round(a["max_return"],3),"setup":a["setup"]["name"]} for a in case["replay_alerts"]]}),flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2))
    print(str(args.output))


if __name__=="__main__":
    main()
