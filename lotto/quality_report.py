"""Sampled quote outcomes for policy observations; never used in live rules."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import json


def scorecard(store, day):
    end=(datetime.fromisoformat(day)+timedelta(days=1)).date().isoformat()
    observations=store.db.execute('SELECT * FROM quality_shadow WHERE at>=? AND at<? ORDER BY at',(day,end)).fetchall()
    groups=defaultdict(Counter)
    details=[]
    for row in observations:
        start=datetime.fromisoformat(row['at'])
        path=store.db.execute('SELECT * FROM quality_quotes WHERE candidate_id=? ORDER BY at',(row['candidate_id'],)).fetchall()
        path=[p for p in path if 0<=(datetime.fromisoformat(p['at'])-start).total_seconds()<=1800]
        changes=[p['bid']/row['entry_ask']-1 for p in path]
        first40=next((i for i,x in enumerate(changes) if x+1e-9>=.4),None)
        first28=next((i for i,x in enumerate(changes) if x<=-.28+1e-9),None)
        gaps=[(datetime.fromisoformat(b['at'])-datetime.fromisoformat(a['at'])).total_seconds() for a,b in zip(path,path[1:])]
        complete=bool(path and (datetime.fromisoformat(path[-1]['at'])-start).total_seconds()>=1710 and max(gaps,default=0)<=150)
        values={'count':1,'WIN40':int(first40 is not None),'WIN100':int(any(x+1e-9>=1 for x in changes)),
                'WIN_CLEAN':int(first40 is not None and (first28 is None or first40<first28)),
                'WIN_BADENTRY':int(first40 is not None and first28 is not None and first28<first40),
                'LOSS_STOPPED':int(complete and first40 is None and first28 is not None),
                'LOSS_DEAD':int(complete and first40 is None and first28 is None),
                'incomplete_or_gapped':int(not complete)}
        scope='alert' if row['alert_id'] else 'research' if (row['live_outcome'] or '').startswith('RESEARCH:') else 'suppressed_candidate'
        keys=[(row['policy_id'],row['phase'],scope,'tier',row['tier'])]
        keys += [(row['policy_id'],row['phase'],scope,'rule',r) for r in json.loads(row['reasons'])]
        for key in keys:groups[key].update(values)
        details.append({'candidate_id':row['candidate_id'],'policy':row['policy_id'],'tier':row['tier'],
                        'phase':row['phase'],'scope':scope,'alert_id':row['alert_id'],
                        'live_outcome':row['live_outcome'],'samples':len(path),**values})
    return {'basis':'Observed bid versus candidate ask; 30 minutes; sampled -28% before +40%. Not tick-complete or actual fills.',
            'coverage':'Only already-collected chains. Missing or gapped nonwinners are not counted as losses. Watches and suppressed candidate observations are separate from alert events.',
            'groups':[dict(policy=k[0],phase=k[1],scope=k[2],dimension=k[3],value=k[4],**v) for k,v in sorted(groups.items())],
            'observations':details}
