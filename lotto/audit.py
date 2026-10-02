"""Compact read-only audit of saved sessions for deployment diagnostics."""
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
import re

from .config import Settings
from .features import contract_ok, quote_ok, observation_window
from .models import ET


def chain_reasons(snap, history, settings):
    """Explain why the option stage had no evaluable candidate."""
    observed = observation_window(snap,history)
    if observed is None:
        return {"reason":"missing contiguous 2/4 or 5/10-minute option history","options":len(snap.options)}
    five, ten, window, width = observed
    old = {o.symbol:o for o in five.options}
    oldest = {o.symbol:o for o in ten.options}
    current = {o.symbol:o for o in snap.options}
    ids = set(current) & set(old) & set(oldest)
    fresh, active, liquid, groups = 0,0,0,Counter()
    indexed = [{o.symbol:o for o in s.options} for s in window]
    for ident in ids:
        observations = [items.get(ident) for items in indexed]
        if (any(o is None for o in observations)
                or any(not quote_ok(o,s,settings) for o,s in zip(observations,window))
                or any(b.volume<a.volume for a,b in zip(observations,observations[1:]))):
            continue
        fresh += 1
        o = current[ident]
        if (o.volume-old[ident].volume)*300/(snap.at-five.at).total_seconds() >= settings.min_strike_volume_5m:
            active += 1
            groups[(o.expiry.isoformat(),o.side)] += 1
            if contract_ok(o,snap,settings):
                liquid += 1
    return {"options":len(snap.options),"window_minutes":width,"common_contracts":len(ids),"fresh_continuous":fresh,
            "active_5m_contracts":active,"liquid_active_contracts":liquid,
            "max_expiry_side_active":max(groups.values(),default=0)}


def audit_store(store, day, settings=None):
    settings = settings or Settings()
    tomorrow = (datetime.fromisoformat(day)+timedelta(days=1)).date().isoformat()
    rows = store.db.execute("SELECT symbol,at,state,reason,score,features FROM decisions WHERE at>=? AND at<? ORDER BY at",
                            (day,tomorrow)).fetchall()
    reasons=Counter()
    candidates=[]
    for row in rows:
        reasons.update(part.strip() for part in row["reason"].split(';') if part.strip())
        if row["score"] is not None:
            f=json.loads(row["features"])
            candidates.append({"symbol":row["symbol"],"at":row["at"],"score":row["score"],
                               "side":f.get("side"),"setup":(f.get("setup") or {}).get("name"),
                               "blockers":f.get("blockers",()),"pace":round(f.get("metrics",{}).get("pace_rvol",0),2),
                               "local_rvol":round(f.get("metrics",{}).get("local_rvol_5m",0),2),
                               "option_acceleration":round(f.get("metrics",{}).get("option_acceleration",0),2)})
    latest={}
    for row in rows:
        latest[row["symbol"]]=row
    symbols = sorted(latest, key=lambda symbol:(latest[symbol]["score"] is not None,
                        latest[symbol]["score"] or -1),reverse=True)
    chain=[]
    for symbol in symbols[:15]:
        observations=store.recent(symbol,day)
        if not observations:
            continue
        snap=observations[-1]
        item={"symbol":symbol,"at":snap.at.isoformat(),"problems":snap.problems(settings.max_quote_age_seconds,min_bars=settings.min_opening_bars),
              "bars":len(snap.bars),"baseline":snap.bars[-1].expected_cumulative_volume if snap.bars else None,
              "context":snap.context_label,"context_return":snap.context_return_5m,
              "chain":chain_reasons(snap,observations,settings)}
        chain.append(item)
    counts=store.db.execute("SELECT COUNT(*),COUNT(DISTINCT symbol) FROM snapshots WHERE at>=? AND at<?",(day,tomorrow)).fetchone()
    discovery=store.db.execute("SELECT COUNT(*),COUNT(DISTINCT symbol),SUM(promoted) FROM discovery WHERE day=?",(day,)).fetchone()
    alert=store.db.execute("SELECT delivery,COUNT(*) FROM alerts WHERE day=? GROUP BY delivery",(day,)).fetchall()
    return {"day":day,"snapshots":counts[0],"symbols_with_chains":counts[1],
            "discovery_observations":discovery[0],"discovery_symbols":discovery[1],
            "promoted_observations":discovery[2] or 0,"decisions":len(rows),
            "top_reasons":reasons.most_common(12),
            "top_candidates":sorted(candidates,key=lambda r:r["score"],reverse=True)[:12],
            "latest_chain_checks":chain,"alerts_by_delivery":{r[0]:r[1] for r in alert}}


def diagnose_symbols(store, day, symbols, settings=None):
    """Read-only, bounded evidence for a missed-name review."""
    settings = settings or Settings()
    result = {}
    for symbol in symbols:
        discovery = [json.loads(r["payload"]) for r in store.db.execute(
            "SELECT payload FROM discovery WHERE day=? AND symbol=? ORDER BY at", (day,symbol))]
        coverage = list(store.db.execute(
            "SELECT at,promoted,contracts,spot,flow_side,flow_cluster FROM chain_coverage "
            "WHERE day=? AND symbol=? ORDER BY at", (day,symbol)))
        decisions = list(store.db.execute(
            "SELECT at,state,reason,score,features FROM decisions WHERE symbol=? AND substr(at,1,10)=? ORDER BY at",
            (symbol,day)))
        scored = []
        for row in decisions:
            if row["score"] is None:
                continue
            feature = json.loads(row["features"])
            metrics = feature.get("metrics", {})
            scored.append({"at":row["at"],"score":row["score"],"side":feature.get("side"),
                           "setup":(feature.get("setup") or {}).get("name"),
                           "blockers":feature.get("blockers", []),
                           "pace":metrics.get("pace_rvol"),"local_rvol":metrics.get("local_rvol_5m"),
                           "option_volume_5m":metrics.get("option_volume_5m")})
        snapshots = store.recent(symbol,day)
        latest = snapshots[-1] if snapshots else None
        flow = [dict(r) for r in coverage if r["flow_cluster"] >= 300]
        result[symbol] = {
            "stock_quote_observations":len(discovery),
            "deep_promotions":sum(bool(r.get("promoted")) for r in discovery),
            "first_deep_at":next((r["observed_at"] for r in discovery if r.get("promoted")),None),
            "chain_observations":len(coverage),
            "deep_chain_observations":sum(r["promoted"] for r in coverage),
            "flow_events":flow[:8],"flow_event_count":len(flow),
            "max_abs_five_minute_stock_return":max((abs(r.get("return_5m") or 0) for r in discovery),default=0),
            "max_discovery_priority":max((r.get("priority",0) for r in discovery),default=0),
            "decision_reasons":Counter(r["reason"] for r in decisions).most_common(8),
            "scored_decisions":len(scored),
            "top_scored":sorted(scored,key=lambda r:r["score"],reverse=True)[:5],
            "last_scored":scored[-3:],
            "latest_snapshot":({"at":latest.at.isoformat(),
                                "problems":latest.problems(settings.max_quote_age_seconds,min_bars=settings.min_opening_bars),
                                "chain":chain_reasons(latest,snapshots,settings)} if latest else None),
            "alerts":store.db.execute("SELECT COUNT(*) FROM alerts WHERE day=? AND symbol=?",(day,symbol)).fetchone()[0],
        }
    return result


def audit_coil_burst_hypothesis(store, day, directory):
    """Read-only cohort audit; it does not change or replay live alert decisions."""
    report_path = Path(directory) / "nightly" / day / "report.json"
    labels = {}
    if report_path.exists():
        report = json.loads(report_path.read_text())
        labels = {(r["symbol"], r["at"]): r["future_labels"]
                  for r in report.get("candidate_evaluations", [])}
    samples = []
    for row in store.db.execute(
        "SELECT symbol,at,score,features FROM decisions WHERE substr(at,1,10)=? AND score>=72 ORDER BY at",
        (day,),
    ):
        feature = json.loads(row["features"])
        if ((feature.get("setup") or {}).get("name") != "COILED_CONTINUATION"
                or feature.get("blockers") != ["options activity not replenishing"]):
            continue
        match = re.search(r"(\d{6})[CP]\d{8}$", feature.get("contract", ""))
        if not match:
            continue
        expiry = datetime.strptime(match.group(1), "%y%m%d").date()
        at = datetime.fromisoformat(row["at"])
        dte = (expiry - at.astimezone(ET).date()).days
        metrics = feature.get("metrics", {})
        samples.append({"symbol":row["symbol"], "at":row["at"], "score":row["score"],
                        "contract":feature["contract"], "dte":dte,
                        "option_acceleration":round(metrics.get("option_acceleration", 0), 2),
                        "local_rvol":round(metrics.get("local_rvol_5m", 0), 2),
                        "stock_volume_acceleration":round(metrics.get("volume_acceleration", 0), 2),
                        "option_volume_5m":metrics.get("option_volume_5m"),
                        "future":labels.get((row["symbol"], row["at"]), {})})
    short = [r for r in samples if r["dte"] in (0,1) and r["option_acceleration"]>=2
             and r["local_rvol"]>=2]
    first = {}
    for row in short:
        first.setdefault((row["symbol"], row["contract"]), row)
    return {"day":day,"nightly_labels_available":bool(labels),
            "all_single_blocker_observations":len(samples),
            "short_dte_strong_burst_observations":len(short),
            "independent_symbol_contract_cases":list(first.values())[:40]}
