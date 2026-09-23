"""Read-only preflight. Never posts a Discord message or prints credentials."""
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .models import ET


def check(client, symbols, directory):
    now=datetime.now(timezone.utc)
    result={"at":now.isoformat(),"universe_count":len(symbols),"delivery_enabled":os.getenv("LOTTO_SEND_ALERTS", "false").lower()=="true"}
    result['persistent_volume_mounted']=os.getenv('RAILWAY_VOLUME_MOUNT_PATH')==str(directory)
    database=Path(directory)/'live.sqlite3'
    if database.exists():
        with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as connection:
            result['database_integrity']=connection.execute('PRAGMA quick_check').fetchone()[0]
            result['recorded_snapshots']=connection.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]
            result['latest_snapshot']=connection.execute('SELECT MAX(at) FROM snapshots').fetchone()[0]
            result['decision_counts']=[{'reason':r[0],'count':r[1]} for r in connection.execute(
                'SELECT reason,COUNT(*) FROM decisions GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 8')]
    session=client.session(now)
    result["session"]=[t.isoformat() for t in session] if session else None
    quote_response=client.get('/quotes',{'symbols':','.join(symbols)})
    missing=[s for s in symbols if s not in quote_response or not quote_response[s].get('quote',{}).get('lastPrice')]
    delayed=[s for s in symbols if quote_response.get(s,{}).get('realtime') is False]
    result.update({"quotes_received":len(symbols)-len(missing),"missing_quotes":missing,"delayed_quotes":delayed})
    chain=client.get('/chains',{'symbol':'QQQ','contractType':'ALL','strategy':'SINGLE','strikeCount':20,
                                'fromDate':now.astimezone(ET).date().isoformat(),
                                'toDate':(now.astimezone(ET).date()+timedelta(days=7)).isoformat()})
    result['chain_contracts_parsed']=len(client.parse_chain(chain))
    result['chain_delayed']=chain.get('isDelayed') is True
    # Webhook metadata GET checks that the saved URL exists without sending anything.
    webhook=os.getenv('DISCORD_LOTTO_WEBHOOK','')
    url=urlsplit(webhook)
    result['webhook_configured']=bool(webhook)
    result['webhook_valid']=False
    if url.scheme=='https' and url.hostname in {'discord.com','discordapp.com'} and url.path.startswith('/api/webhooks/'):
        try:
            with urlopen(Request(webhook,headers={'User-Agent':'LottoScanner/0.2'}),timeout=10) as response:
                data=json.load(response)
            result['webhook_valid']=bool(data.get('id') and data.get('channel_id'))
            result['webhook_name']=data.get('name')
            result['channel_id']=data.get('channel_id')
        except Exception as exc:
            result['webhook_error']=type(exc).__name__
    result['ready']=(not missing and not delayed and result['chain_contracts_parsed']>0
                     and not result['chain_delayed'] and result['webhook_valid'] and result['delivery_enabled'])
    if os.getenv('RAILWAY_SERVICE_ID'):
        result['ready']=result['ready'] and result['persistent_volume_mounted'] and result.get('database_integrity')=='ok'
    result['market_open_now']=bool(session and session[0]<=now<session[1])
    result['limitation']='After-hours connectivity check does not establish live quote freshness or strategy performance.'
    return result
