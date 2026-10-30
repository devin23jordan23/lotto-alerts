"""Replay the actual policy module on the historical 433-alert benchmark.

Stack 8 market labels use a historical return-difference proxy because this
summary export lacks original benchmark timestamps. S routing does not use it.
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lotto.quality import evaluate
from lotto.models import ET,Option,Snapshot
from lotto.patterns import Setup
from lotto.features import Candidate


def replay(path):
    rows=list(map(json.loads,Path(path).read_text().splitlines()))
    result={p:{'tiers':Counter(),'rules':Counter(),'MAIN':Counter()} for p in ('S','stack8')}
    for r in rows:
        at=datetime.fromisoformat(r['at']);expiry=at.astimezone(ET).date()+timedelta(days=r['dte'])
        option=Option(r['contract'],expiry,r['side'],100,r['ask']*.95,r['ask'],100,100,.3,.01,at)
        m=r['metrics'];direction=1 if r['side']=='CALL' else -1
        market=(direction*(m['return_5m_directional']-m['relative_market_5m'])
                if m.get('return_5m_directional') is not None and m.get('relative_market_5m') is not None else None)
        snap=Snapshot(r['symbol'],at,100,at,99,(),(option,),market_return_5m=market,market_time=at)
        c=Candidate(snap,option,r['score'],r['parts'],m,[],True,setup=Setup(r['setup'],100,99))
        for policy in result:
            q=evaluate(c,policy)
            result[policy]['tiers'][q.tier]+=1
            result[policy]['rules'].update(q.reasons)
            if q.tier=='MAIN':
                result[policy]['MAIN'].update(count=1,win40=int(r['peak30']>=.4),win100=int(r['peak30']>=1),below40=int(r['peak30']<.4))
    return {'population':len(rows),'basis':'Retrospective selected-alert replay; sampled bid peaks within 30m. Not a production-state or fill replay.',**result}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('path');parser.add_argument('--assert-benchmark',action='store_true')
    args=parser.parse_args();result=replay(args.path)
    if args.assert_benchmark:
        assert result['population']==433
        assert result['S']['tiers']=={'MAIN':261,'SECONDARY':87,'RESEARCH':85},result
        assert result['S']['MAIN']=={'count':261,'win40':133,'win100':58,'below40':128},result
        assert result['stack8']['tiers']=={'MAIN':254,'SECONDARY':179},result
        assert result['stack8']['MAIN']['win40']==129 and result['stack8']['MAIN']['win100']==54,result
    print(json.dumps(result,indent=2))
