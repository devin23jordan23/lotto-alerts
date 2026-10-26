"""Replay one session through the real Engine, read-only against production snapshots.

Usage: python3 scripts/replay_hybrid_mover.py <code_root> <db_path> <day>
       <BASE|MOVER> <out_dir>. Only sendable ACTIVE rows count as alerts.
The grade uses sampled post-alert bids against the signal ask; it cannot claim
an executable fill or see adverse moves between samples.
"""
import sys, sqlite3, csv, os, json, time as _t
from dataclasses import replace
from datetime import datetime, timedelta, timezone
root, dbp, day, cfgname, out = sys.argv[1:6]
sys.path.insert(0, root)
from lotto.store import Store, read_snapshot
from lotto.engine import Engine
from lotto.main import env_settings
from lotto.models import ET
from lotto.quality import QualityConfig

cfg = env_settings()
if hasattr(cfg, 'mover_enabled'):
    cfg = replace(cfg, mover_enabled=int(cfgname in ('NEW', 'MOVER')), keep_gate=int(cfgname in ('NEW', 'KEEP')))
Store.record = lambda self, snap: None          # do not keep 24k payloads in memory
st = Store(':memory:')
eng = Engine(st, cfg, dry_run=True, quality=QualityConfig.from_env())
src = sqlite3.connect(f'file:{dbp}?mode=ro', uri=True)
open_alerts = {}   # id -> dict
rows = []
t0 = _t.time(); n = 0
for (p,) in src.execute("select payload from snapshots where at>=? and at<? order by at", (day + 'T13:25', day + 'T20:05')):
    snap = read_snapshot(p); n += 1
    # outcome tracking for alerts already open on this symbol (quotes from later snapshots only)
    for a in open_alerts.values():
        if a['symbol'] != snap.symbol or snap.at <= a['at']:
            continue
        o = next((x for x in snap.options if x.symbol == a['contract']), None)
        if (o is None or o.bid is None or o.bid <= 0 or o.quote_time > snap.at
                or o.quote_time <= a['last_quote_time']):
            continue
        a['last_quote_time'] = o.quote_time
        r = o.bid / a['ask'] - 1; mins = (snap.at - a['at']).total_seconds() / 60
        a['quote_count'] += 1
        a['day_max'] = max(a['day_max'], r)
        if mins <= 30: a['m30'] = max(a['m30'], r)
        if mins <= 60: a['m60'] = max(a['m60'], r)
        if a['t40'] is None:
            a['low'] = min(a['low'], r)
            if r >= .40: a['t40'] = round(mins, 1)
    for al in eng.process([snap]):
        row = st.db.execute("select id,symbol,side,contract,at,entry_ask,phase,quality_tier,payload,snapshot from alerts where id=?", (al['id'],)).fetchone()
        lane = 'MOVER' if 'STOCK MOVER' in row[8] else 'LEGACY'
        if row['phase'] != 'ACTIVE' or (cfg.hybrid and lane != 'MOVER' and row['quality_tier'] not in cfg.send_tiers):
            continue  # Internal C-tier and POTENTIAL records are not Discord sends.
        source = json.loads(row['snapshot'])
        entry_quote = next((datetime.fromisoformat(x['quote_time']) for x in source['options']
                            if x['symbol'] == row['contract']), snap.at)
        open_alerts[row[0]] = {'id': row[0], 'symbol': row[1], 'side': row[2], 'contract': row[3],
                               'at': snap.at, 'ask': row[5], 'phase': row[6], 'tier': row[7] or '', 'lane': lane,
                               'm30': -1, 'm60': -1, 'day_max': -1, 't40': None, 'low': 0.0,
                               'last_quote_time': entry_quote, 'quote_count': 0}
os.makedirs(out, exist_ok=True)
with open(f'{out}/alerts_{cfgname}_{day}.csv', 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['day', 'time', 'symbol', 'side', 'contract', 'ask', 'phase', 'tier', 'lane', 'm30', 'm60', 'day_max', 't40', 'low_before40', 'quote_count'])
    for a in open_alerts.values():
        w.writerow([day, a['at'].astimezone(ET).strftime('%H:%M'), a['symbol'], a['side'], a['contract'], a['ask'], a['phase'], a['tier'], a['lane'],
                    round(a['m30'], 3), round(a['m60'], 3), round(a['day_max'], 3), a['t40'], round(a['low'], 3), a['quote_count']])
res = st.db.execute("select count(*) from research_ideas").fetchone()[0]
print(cfgname, day, 'snapshots', n, 'alerts', len(open_alerts), 'research', res, 'secs', round(_t.time() - t0))
