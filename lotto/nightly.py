"""Nightly observations and counterfactual price research; no live parameter edits."""
import json
import gzip
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from math import isfinite
from pathlib import Path

from .config import Settings, STRATEGY_VERSION
from .features import quote_ok
from .models import ET
from .research import price_replay
from .schwab import epoch
from .store import read_snapshot

LOG = logging.getLogger(__name__)


def instrument_group(contract):
    return "SPXW" if contract and contract.split()[0] == "SPXW" else "OTHER"


def alert_feedback(store, day):
    """Label delivered ideas from sampled quotes without treating peaks as fills."""
    result = []
    settings = Settings()
    for alert in store.db.execute("SELECT * FROM alerts WHERE day=? ORDER BY at", (day,)).fetchall():
        at = datetime.fromisoformat(alert["at"])
        first = None
        for decision in store.db.execute(
            "SELECT at,features FROM decisions WHERE symbol=? AND at>=? AND at<=? "
            "AND state='IGNITION' AND reason='confirming persistence' ORDER BY at",
            (alert["symbol"], (at-timedelta(minutes=10)).isoformat(), alert["at"]),
        ):
            features = json.loads(decision["features"])
            if features.get("contract") == alert["contract"] and not features.get("blockers"):
                first = {"at": decision["at"], "ask": features.get("entry_ask")}
                break
        current = store.db.execute(
            "SELECT score,features FROM decisions WHERE symbol=? AND at=? "
            "AND (reason LIKE 'potential alert %' OR reason LIKE 'active alert %')",
            (alert["symbol"], alert["at"]),
        ).fetchone()
        features = json.loads(current["features"]) if current else {}
        setup = features.get("setup") or {}
        snapshot = json.loads(alert["snapshot"])
        initial_option = next((o for o in snapshot["options"] if o["symbol"] == alert["contract"]), None)
        expiry = date.fromisoformat(initial_option["expiry"]) if initial_option else None
        initial_quote_at = datetime.fromisoformat(initial_option["quote_time"]) if initial_option else at
        atr = snapshot.get("prior_atr")
        trigger_gap_atr = (abs(features["spot"]-setup["trigger"])/atr
                           if atr and setup.get("trigger") is not None and features.get("spot") else None)
        quotes, invalidated_at = [], None
        direction = 1 if alert["side"]=="CALL" else -1
        for row in store.db.execute(
            "SELECT payload FROM snapshots WHERE symbol=? AND at>=? AND at<? ORDER BY at",
            (alert["symbol"], alert["at"], (at+timedelta(days=1)).date().isoformat()),
        ):
            snap = read_snapshot(row["payload"])
            if (invalidated_at is None and setup.get("invalidation") is not None
                    and 0 <= (snap.at-snap.spot_time).total_seconds() <= settings.max_quote_age_seconds
                    and direction*(snap.spot-setup["invalidation"]) <= 0):
                invalidated_at = snap.at
            option = next((o for o in snap.options if o.symbol == alert["contract"]), None)
            if option and quote_ok(option, snap, settings) and option.quote_time >= initial_quote_at:
                quotes.append((option.quote_time, option.bid))
        if quotes:
            peak_at, peak_bid = max(quotes, key=lambda item: item[1])
            low_at, low_bid = min(quotes, key=lambda item: item[1])
            last_at, last_bid = quotes[-1]
        else:
            peak_at = peak_bid = low_at = low_bid = last_at = last_bid = None
        entry = alert["entry_ask"]
        horizons = {}
        for minutes in (5, 15, 30, 60):
            target = at+timedelta(minutes=minutes)
            match = next((bid for timestamp,bid in quotes
                          if target <= timestamp <= target+timedelta(seconds=90)), None)
            horizons[f"bid_return_{minutes}m"] = match/entry-1 if match is not None else None
        result.append({
            "id":alert["id"], "symbol":alert["symbol"], "contract":alert["contract"],
            "instrument_group":instrument_group(alert["contract"]),
            "side":alert["side"],
            "expiry":expiry.isoformat() if expiry else None,
            "dte":(expiry-at.astimezone(ET).date()).days if expiry else None,
            "phase":alert["phase"], "parent_id":alert["parent_id"], "thesis_id":alert["thesis_id"],
            "delivery":alert["delivery"],
            "alert_at":alert["at"], "entry_ask":entry, "score":current["score"] if current else None,
            "strategy":features.get("strategy","legacy"), "delivery":alert["delivery"],
            "setup":setup.get("name"), "trigger_gap_atr":trigger_gap_atr,
            "first_qualified_at":first["at"] if first else None,
            "first_qualified_ask":first["ask"] if first else None,
            "confirmation_premium_change":entry/first["ask"]-1 if first and first["ask"] else None,
            "sampled_quote_count":len(quotes),
            "peak_bid":peak_bid, "peak_at":peak_at.isoformat() if peak_at else None,
            "peak_ask_to_bid_return":peak_bid/entry-1 if peak_bid is not None else None,
            "minutes_to_peak":max(0, (peak_at-at).total_seconds()/60) if peak_at else None,
            "low_bid":low_bid, "low_at":low_at.isoformat() if low_at else None,
            "low_ask_to_bid_return":low_bid/entry-1 if low_bid is not None else None,
            "last_bid":last_bid, "last_at":last_at.isoformat() if last_at else None,
            "last_ask_to_bid_return":last_bid/entry-1 if last_bid is not None else None,
            "horizon_returns":horizons,
            "sampled_invalidation_at":invalidated_at.isoformat() if invalidated_at else None,
            "first_100pct_at":next((t.isoformat() for t,b in quotes if b>=2*entry),None),
            "peak_return_before_sampled_invalidation":max(
                (b/entry-1 for t,b in quotes if invalidated_at is None or t<invalidated_at),default=None),
            "user_feedback":dict(feedback) if (feedback:=store.db.execute(
                "SELECT outcome,notes,recorded_at FROM user_feedback WHERE alert_id=?",(alert["id"],)).fetchone()) else None,
        })
    return result


def trade_high_path(alert, candles):
    """Research opportunity from later option trades, never an assumed fill."""
    at = datetime.fromisoformat(alert["at"])
    alert_minute = at.replace(second=0, microsecond=0)
    ask = alert["entry_ask"]
    bars = []
    for raw in candles:
        minute = epoch(raw.get("datetime"))
        high, low = raw.get("high"), raw.get("low")
        if (minute is None or minute <= alert_minute or minute.astimezone(ET).date() != at.astimezone(ET).date()
                or not 9*60+30 <= minute.astimezone(ET).hour*60+minute.astimezone(ET).minute < 16*60
                or not isinstance(high, (int, float)) or not isinstance(low, (int, float))
                or not isfinite(high) or not isfinite(low) or high < 0 or low < 0 or high < low):
            continue
        bars.append((minute, float(high), float(low)))
    bars.sort(key=lambda row: row[0])
    within30 = [b for b in bars if b[0] <= alert_minute + timedelta(minutes=30)]
    peak = max(bars, key=lambda b: b[1], default=None)
    peak30 = max(within30, key=lambda b: b[1], default=None)
    before_peak = [b[2] for b in bars if peak and b[0] < peak[0]]
    gain = peak[1]/ask-1 if peak and ask > 0 else None
    gain30 = peak30[1]/ask-1 if peak30 and ask > 0 else None
    if gain is None:
        label = "Unavailable"
    elif gain30 is not None and gain30 >= 1:
        label = "Strong"
    elif gain30 is not None and gain30 >= .3 and peak30[1]-ask >= .10:
        label = "Worked"
    elif gain >= .5:
        label = "Late pop"
    elif gain >= .1:
        label = "Marginal"
    else:
        label = "Flat/failed"
    first_targets = {}
    for percent in (30, 50, 100, 200, 300, 500):
        first = next((b for b in bars if b[1] >= ask*(1+percent/100)), None)
        first_targets[str(percent)] = first[0].isoformat() if first else None
    return {
        "id":alert["id"], "symbol":alert["symbol"], "contract":alert["contract"],
        "instrument_group":instrument_group(alert["contract"]),
        "phase":alert["phase"], "parent_id":alert["parent_id"], "thesis_id":alert["thesis_id"],
        "delivery":alert["delivery"],
        "alert_at":alert["at"], "alert_ask":ask, "later_trade_bar_count":len(bars),
        "peak_30m_trade_high":peak30[1] if peak30 else None,
        "peak_30m_gain":gain30,
        "peak_day_trade_high":peak[1] if peak else None,
        "peak_day_at":peak[0].isoformat() if peak else None,
        "peak_day_gain":gain,
        "lowest_trade_before_peak":min(before_peak) if before_peak else None,
        "adverse_before_peak":min(before_peak)/ask-1 if before_peak and ask > 0 else None,
        "first_target_at":first_targets, "opportunity_label":label,
    }


def option_trade_high_feedback(store, day, root, client):
    """Fetch each alerted contract once and label watches/active ideas separately."""
    alerts = [dict(row) for row in store.db.execute(
        "SELECT id,symbol,contract,at,entry_ask,phase,parent_id,thesis_id,delivery FROM alerts WHERE day=? ORDER BY at", (day,))]
    option_dir = root/"option-trades"
    option_dir.mkdir(parents=True, exist_ok=True)
    bars_by_contract, errors = {}, []
    start = datetime.fromisoformat(day).replace(hour=9, minute=30, tzinfo=ET)
    end = start.replace(hour=16)
    for contract in sorted({a["contract"] for a in alerts}):
        file = option_dir/f"{sha256(contract.encode()).hexdigest()[:20]}.json.gz"
        try:
            if file.exists():
                with gzip.open(file, "rt") as stream:
                    response = json.load(stream)
            elif client is not None:
                response = client.candles(contract, start, end)
                if not response.get("candles"):
                    raise ValueError("empty option trade bars")
                with gzip.open(file, "wt") as stream:
                    json.dump(response, stream)
            else:
                raise FileNotFoundError("option trade bars unavailable offline")
            bars_by_contract[contract] = response["candles"]
        except Exception as exc:
            errors.append({"contract":contract, "error":type(exc).__name__})
    return [trade_high_path(alert, bars_by_contract[alert["contract"]])
            for alert in alerts if alert["contract"] in bars_by_contract], errors


def phase_opportunity_summary(alerts, rows, phase, group=None):
    matching_alerts = [a for a in alerts if a["phase"] == phase
                       and (group is None or instrument_group(a["contract"]) == group)]
    subset = [r for r in rows if r["phase"] == phase
              and (group is None or instrument_group(r["contract"]) == group)]
    setups = {}
    for row in subset:
        setups.setdefault(row.get("setup") or "Unknown", Counter()).update([row["opportunity_label"]])
    return {"alert_count":len(matching_alerts),
            "delivered_count":sum(a["delivery"] == "sent" for a in matching_alerts),
            "observed_count":sum(r["opportunity_label"] != "Unavailable" for r in subset),
            "unobserved_count":len(matching_alerts)-sum(r["opportunity_label"] != "Unavailable" for r in subset),
            "opportunity_labels":dict(Counter(r["opportunity_label"] for r in subset)),
            "delivered_opportunity_labels":dict(Counter(r["opportunity_label"] for r in subset
                                                       if r["delivery"] == "sent")),
            "by_setup":{setup:dict(counts) for setup,counts in setups.items()}}


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
    candidate_rows = []
    candidate_blocks = Counter()
    for row in store.db.execute("SELECT * FROM candidate_evaluations WHERE day=? ORDER BY at",(day,)):
        item = dict(row)
        item["features"] = json.loads(item["features"])
        candidate_blocks.update(item["features"].get("blockers",[]))
        candidate_rows.append(item)
    alert_rows = [dict(r) for r in store.db.execute(
        "SELECT id,contract,phase,parent_id,thesis_id,delivery FROM alerts WHERE day=?", (day,))]
    feedback_rows = alert_feedback(store, day)
    feedback_by_id = {row["id"]:row for row in feedback_rows}
    trade_high_rows, trade_high_errors = option_trade_high_feedback(store, day, root, client)
    for row in trade_high_rows:
        feedback = feedback_by_id.get(row["id"], {})
        row.update({"setup":feedback.get("setup"), "score":feedback.get("score"),
                    "side":feedback.get("side"), "expiry":feedback.get("expiry"),
                    "dte":feedback.get("dte")})
    activated_ids = {r["parent_id"] for r in alert_rows if r["phase"] == "ACTIVE" and r["parent_id"]}
    result = {
        "day":day,"generated_at":datetime.now(timezone.utc).isoformat(),
        "report_strategy":STRATEGY_VERSION,
        "universe":list(symbols),"universe_size":len(symbols),"full_universe_price_research":client is not None,
        "data_errors":errors,"discovery_coverage":coverage,"chain_coverage":chain_coverage,
        "universe_flow_events":flow_events,
        "alert_feedback":feedback_rows,
        "alert_phase_counts":dict(Counter(r["phase"] for r in alert_rows)),
        "activated_watches":len(activated_ids),
        "unactivated_watches":sum(r["phase"] == "POTENTIAL" and r["id"] not in activated_ids for r in alert_rows),
        "direct_active_ideas":sum(r["phase"] == "ACTIVE" and r["parent_id"] is None for r in alert_rows),
        "option_trade_high_feedback":trade_high_rows,
        "option_trade_high_errors":trade_high_errors,
        "active_opportunity_summary":phase_opportunity_summary(alert_rows, trade_high_rows, "ACTIVE"),
        "potential_opportunity_summary":phase_opportunity_summary(alert_rows, trade_high_rows, "POTENTIAL"),
        "opportunity_by_instrument":{
            group:{"active":phase_opportunity_summary(alert_rows, trade_high_rows, "ACTIVE", group),
                   "potential":phase_opportunity_summary(alert_rows, trade_high_rows, "POTENTIAL", group)}
            for group in ("SPXW", "OTHER")},
        "research_notes":["Price setups are hypotheses, not confirmed option alerts.",
                          "Historical bars cannot reconstruct unrecorded intraday option quotes or aggressor side.",
                          "Trade highs are price-potential labels, not realized returns or assumed fills.",
                          "Potential watches never count as active ideas; linked active alerts start a new measurement window.",
                          "Future labels never enter the feature calculation. No automatic live threshold changes.",
                          "Small, selected daily cohorts do not establish predictive accuracy."],
        "decision_reasons":dict(Counter(r["reason"] for r in rows)),
        "candidate_evaluations":evaluations,
        "all_expiry_side_evaluations":candidate_rows,
        "all_candidate_blocker_counts":dict(candidate_blocks),
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
