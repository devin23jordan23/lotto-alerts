"""Prefix-only price research. Subsequent returns are labels, never input features."""
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .models import ET, Snapshot
from .patterns import detect_setup, path_features
from .schwab import Schwab, epoch


def price_replay(symbol, day, response):
    end = datetime.fromisoformat(day).replace(hour=16, tzinfo=ET)
    # Reuse precisely the live baseline/bar construction with an in-memory data source.
    client = Schwab.__new__(Schwab)
    client.baselines, client.atrs = {}, {}
    client.candles = lambda *_:response
    bars = client.bars(symbol, end)
    previous = [(epoch(c.get("datetime")), c) for c in response.get("candles", [])]
    previous = [(at,c) for at,c in previous if at and at.astimezone(ET).date() < end.date()
                and (9,30) <= (at.astimezone(ET).hour, at.astimezone(ET).minute) < (16,0)]
    if not previous or not bars:
        return {"symbol":symbol, "day":day, "error":"prior/session bars unavailable"}
    prior_close = max(previous, key=lambda p:p[0])[1]["close"]
    findings, last = [], {}
    for index in range(10, len(bars)-1):
        prefix = bars[:index+1]
        current = prefix[-1]
        snap = Snapshot(symbol, current.end, current.close, current.end, prior_close, prefix, (),
                        prior_atr=client.atrs.get((symbol,end.date())))
        problems = snap.problems()
        if problems:
            continue
        for side, direction in (("CALL",1),("PUT",-1)):
            metrics = path_features(snap, direction)
            setup = detect_setup(snap, direction, metrics)
            key = (side, setup.name if setup else None)
            if not setup or (key in last and current.end-last[key] < timedelta(minutes=30)):
                continue
            last[key] = current.end
            future = bars[index+1:]
            labels = {}
            for horizon in (5,15,30,60):
                target = current.end+timedelta(minutes=horizon)
                later = next((b for b in future if b.end == target), None)
                labels[f"return_{horizon}m_directional"] = direction*(later.close/current.close-1) if later else None
            labels["remaining_favorable_excursion"] = max(direction*((b.high if direction==1 else b.low)/current.close-1) for b in future)
            labels["remaining_adverse_excursion"] = min(direction*((b.low if direction==1 else b.high)/current.close-1) for b in future)
            findings.append({"at":current.end.isoformat(), "side":side, "setup":setup.name,
                             "spot":current.close, "trigger":setup.trigger, "invalidation":setup.invalidation,
                             "building":setup.building, "features_at_time":metrics, "future_labels":labels,
                             "options_confirmation":"unavailable: no historical chain tape"})
    # Fixed prefix landmarks let the nightly review compare eventual HOD/LOD
    # closers with other names without leaking the close into live features.
    landmarks = []
    for elapsed in (15, 30, 60, 120, 180, 240):
        if len(bars) < elapsed:
            continue
        prefix = bars[:elapsed]
        current = prefix[-1]
        snap = Snapshot(symbol, current.end, current.close, current.end, prior_close, prefix, (),
                        prior_atr=client.atrs.get((symbol,end.date())))
        sides = {}
        for side, direction in (("CALL",1),("PUT",-1)):
            metrics = path_features(snap,direction)
            setup = detect_setup(snap,direction,metrics)
            sides[side] = {"setup":setup.name if setup else None, "features":metrics}
        landmarks.append({"at":current.end.isoformat(),"elapsed_minutes":elapsed,
                          "spot":current.close,"sides":sides})
    session_high, session_low = max(b.high for b in bars), min(b.low for b in bars)
    close_location = ((bars[-1].close-session_low)/(session_high-session_low)
                      if session_high > session_low else .5)
    return {"symbol":symbol, "day":day, "source":"Schwab one-minute regular-session bars",
            "open":bars[0].open, "close":bars[-1].close,
            "high":session_high, "low":session_low,
            "prior_close":prior_close, "prior_atr":client.atrs.get((symbol,end.date())),
            "complete_minutes":len(bars), "price_setups":findings,
            "close_location_in_range":close_location,
            "closed_near_high":close_location >= .9,
            "closed_near_low":close_location <= .1,
            "early_landmarks":landmarks}
