"""Compact read-only audit of saved sessions for deployment diagnostics."""
import json
from collections import Counter
from datetime import datetime, timedelta

from .config import Settings
from .features import contract_ok, quote_ok
from .models import ET


def chain_reasons(snap, history, settings):
    """Explain why the option stage had no evaluable candidate."""
    endpoints = []
    for minutes in (5, 10):
        target = snap.at - timedelta(minutes=minutes)
        matches = [s for s in history if s.at <= target and (target-s.at).total_seconds() <= 90]
        if not matches:
            return {"reason": "missing 5/10-minute option observation", "options": len(snap.options)}
        endpoints.append(matches[-1])
    five, ten = endpoints
    window = [s for s in history if s.at >= ten.at]
    if any((b.at-a.at).total_seconds() > 150 for a,b in zip(window,window[1:])):
        return {"reason": "option observation gap over 150 seconds", "options":len(snap.options)}
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
        if o.volume-old[ident].volume >= settings.min_strike_volume_5m:
            active += 1
            groups[(o.expiry.isoformat(),o.side)] += 1
            if contract_ok(o,snap,settings):
                liquid += 1
    return {"options":len(snap.options),"common_contracts":len(ids),"fresh_continuous":fresh,
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
        item={"symbol":symbol,"at":snap.at.isoformat(),"problems":snap.problems(settings.max_quote_age_seconds),
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
