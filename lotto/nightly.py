"""Nightly observations and counterfactual price research; no live parameter edits."""
import json
import gzip
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import ET
from .research import price_replay
from .schwab import epoch

LOG = logging.getLogger(__name__)


def run_nightly(store, day, directory, symbols=(), client=None):
    root = Path(directory)/"nightly"/day
    root.mkdir(parents=True, exist_ok=True)
    research, errors, tape = [], [], {}
    # All configured names, including those never promoted intraday. Each successful
    # data file is cached so retries don't need to download the universe again.
    for index, symbol in enumerate(symbols):
        file = root/f"{symbol}-bars.json.gz"
        try:
            if file.exists():
                with gzip.open(file,'rt') as stream:
                    response = json.load(stream)
            elif client:
                end = datetime.fromisoformat(day).replace(hour=16, tzinfo=ET)
                response = client.candles(symbol,end-timedelta(days=45),end)
                if not response.get("candles"):
                    raise ValueError("empty bar response")
                with gzip.open(file,'wt') as stream:
                    json.dump(response,stream)
            else:
                continue
            tape[symbol] = {at+timedelta(minutes=1):c["close"] for c in response.get("candles",[])
                            if (at:=epoch(c.get("datetime"))) and at.astimezone(ET).date().isoformat()==day
                            and (9,30) <= (at.astimezone(ET).hour,at.astimezone(ET).minute) < (16,0)}
            research.append(price_replay(symbol,day,response))
        except Exception as exc:
            errors.append({"symbol":symbol, "error":type(exc).__name__})
        if (index+1)%10==0:
            LOG.info("Nightly price research: %d/%d names",index+1,len(symbols))
    # Fallback to captured session bars when running offline.
    if not tape:
        for snap in store.session_snapshots(day):
            tape.setdefault(snap.symbol,{}).update({b.end:b.close for b in snap.bars})
    rows = store.db.execute("SELECT symbol,at,state,reason,score,features FROM decisions WHERE at>=? AND at<? ORDER BY at",
                            (day,(datetime.fromisoformat(day)+timedelta(days=1)).date().isoformat())).fetchall()
    evaluations, cohorts = [], {}
    for row in rows:
        features = json.loads(row["features"])
        setup = features.get("setup")
        if not setup:
            continue
        spot, at = features["spot"], datetime.fromisoformat(row["at"])
        direction = 1 if features["side"]=="CALL" else -1
        labels = {}
        prices = tape.get(row["symbol"],{})
        for minutes in (5,15,30,60):
            target = at+timedelta(minutes=minutes)
            matches = [(t,p) for t,p in prices.items() if target<=t<=target+timedelta(seconds=60)]
            labels[f"stock_return_{minutes}m_directional"] = direction*(min(matches)[1]/spot-1) if matches else None
        evaluation = {"symbol":row["symbol"], "at":row["at"], "state":row["state"],
                      "reason":row["reason"], "score":row["score"], "features_at_time":features,
                      "future_labels":labels}
        evaluations.append(evaluation)
        key = (row["symbol"],features["side"],setup["name"])
        # One first observation per symbol/side/setup/day; overlapping minute rows aren't independent trials.
        cohorts.setdefault(key,evaluation)
    grouped = {}
    for row in cohorts.values():
        name = row["features_at_time"]["setup"]["name"]
        value = row["future_labels"]["stock_return_30m_directional"]
        if value is not None:
            grouped.setdefault(name,[]).append(value)
    coverage = [dict(r) for r in store.db.execute(
        "SELECT symbol, COUNT(*) AS quote_observations, SUM(promoted) AS promoted_observations FROM discovery WHERE day=? GROUP BY symbol",(day,))]
    chain_coverage = [dict(r) for r in store.db.execute(
        """SELECT symbol, COUNT(*) AS chain_observations,
           SUM(promoted) AS deep_observations, MAX(flow_cluster) AS largest_three_strike_volume_increase,
           SUM(CASE WHEN flow_cluster>=300 THEN 1 ELSE 0 END) AS flow_events
           FROM chain_coverage WHERE day=? GROUP BY symbol""", (day,))]
    flow_events = []
    for row in store.db.execute("""SELECT symbol,at,spot,flow_side,flow_cluster FROM chain_coverage
        WHERE day=? AND flow_cluster>=300 ORDER BY at""", (day,)):
        at = datetime.fromisoformat(row["at"])
        direction = 1 if row["flow_side"] == "CALL" else -1
        prices = tape.get(row["symbol"], {})
        labels = {}
        for minutes in (5, 15, 30, 60):
            target = at+timedelta(minutes=minutes)
            matches = [(t,p) for t,p in prices.items() if target <= t <= target+timedelta(seconds=60)]
            labels[f"stock_return_{minutes}m_directional"] = (
                direction*(min(matches)[1]/row["spot"]-1) if matches and row["spot"] else None)
        flow_events.append({**dict(row), "future_labels":labels})
    result = {
        "day":day,"generated_at":datetime.now(timezone.utc).isoformat(),
        "universe":list(symbols),"universe_size":len(symbols),"full_universe_price_research":client is not None,
        "data_errors":errors,"discovery_coverage":coverage,"chain_coverage":chain_coverage,
        "universe_flow_events":flow_events,
        "research_notes":["Price setups are hypotheses, not confirmed option alerts.",
                          "Historical bars cannot reconstruct unrecorded intraday option quotes or aggressor side.",
                          "Future labels never enter the feature calculation. No automatic live threshold changes.",
                          "Small, selected daily cohorts do not establish predictive accuracy."],
        "decision_reasons":dict(Counter(r["reason"] for r in rows)),
        "candidate_evaluations":evaluations,
        "setup_cohorts":{k:{"independent_symbol_side_setups":len(v),"positive_30m":sum(x>0 for x in v),
                            "mean_stock_30m_directional":sum(v)/len(v)} for k,v in grouped.items()},
        "universe_price_setups":research,"captured_option_returns":store.end_of_day_options(day),
    }
    path = root/"report.json"
    temporary = root/"report.tmp"
    temporary.write_text(json.dumps(result,indent=2))
    temporary.replace(path)
    # An offline report must not suppress the live full-universe nightly scan.
    if client is not None:
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO nightly_runs VALUES (?, ?, ?)",
                             (day,result["generated_at"],str(path)))
    return path


def maybe_nightly(store, client, symbols, directory, now):
    session = client.session(now)
    today = now.astimezone(ET).date().isoformat()
    day = today if session and now>=session[1]+timedelta(minutes=5) else None
    if not day:
        # Catch up one previously captured session after a restart or on a holiday.
        previous = store.db.execute("SELECT substr(at,1,10) AS day FROM snapshots WHERE at<? GROUP BY day ORDER BY day DESC LIMIT 1",(today,)).fetchone()
        day = previous["day"] if previous else None
    if day and not store.db.execute("SELECT 1 FROM nightly_runs WHERE day=?",(day,)).fetchone():
        # Never let catch-up research block the live market collection loop.
        if session and session[0]<=now<session[1]:
            return
        LOG.info("Nightly universe review starting: %s, %d names",day,len(symbols))
        path = run_nightly(store,day,directory,symbols,client)
        LOG.info("Nightly universe review saved: %s",path)
