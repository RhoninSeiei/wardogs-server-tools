"""Admin-only bounded kill history from one readonly retained-archive snapshot."""
import json
import math
from pathlib import Path
import sqlite3
import time
try:
    from service.moderation_ops import ContractError,validate_admin_actor
    from service.recent_kills import steam,text
except ImportError:
    from moderation_ops import ContractError,validate_admin_actor
    from recent_kills import steam,text


def number(value):
    return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else None


def relationship(obj):
    killer,victim=steam(obj.get('killerSteamId')),steam(obj.get('victimSteamId'))
    if killer is not None and killer==victim:return 'self'
    flag=obj.get('teamkill')
    factions={'Lonestar','Manticore','Valkyra'}
    a,b=obj.get('killerFaction'),obj.get('victimFaction')
    same=(a==b) if isinstance(a,str) and isinstance(b,str) and a in factions and b in factions else None
    if type(flag) is bool:
        if same is not None and same!=flag:return 'unknown'
        return 'friendly' if flag else 'enemy'
    if same is not None:return 'friendly' if same else 'enemy'
    return 'unknown'


class PlayerKills:
    def __init__(self,engine,path):self.engine,self.path=engine,Path(path)

    def query(self,payload,now=None):
        clock=time.time if now is None else lambda:now
        if (not isinstance(payload,dict) or not {'steamId','actor','requestedAt'}<=set(payload)
                or not set(payload)<={'steamId','limit','actor','requestedAt'}):
            raise ContractError(400,'invalid_player_kills_fields')
        validate_admin_actor(payload['actor'],payload['requestedAt'],self.engine.config,clock())
        sid=steam(payload['steamId']);limit=payload.get('limit',20)
        if sid is None:raise ContractError(400,'invalid_player_kills_steam_id')
        if type(limit) is not int or not 1<=limit<=50:raise ContractError(400,'invalid_player_kills_limit')
        connection=None
        try:
            connection=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
            deadline=time.monotonic()+2
            connection.set_progress_handler(lambda:int(time.monotonic()>deadline),2000)
            connection.execute('PRAGMA query_only=ON');connection.execute('BEGIN')
            state=connection.execute("SELECT last_success,error FROM sync_state WHERE kind='events'").fetchone()
            observed=clock()
            if not state or state[1] is not None or number(state[0]) is None or not 0<=observed-state[0]<=15:
                raise ContractError(503,'player_kills_events_unavailable')
            cursor=connection.execute('SELECT generation,after_seq,latest_seq FROM sync_cursor WHERE id=1').fetchone()
            if (not cursor or not isinstance(cursor[0],str) or not cursor[0] or type(cursor[1]) is not int
                    or type(cursor[2]) is not int or cursor[1]<0 or cursor[1]!=cursor[2]):
                raise ContractError(503,'player_kills_sync_incomplete')
            coverage=connection.execute('SELECT MIN(received) FROM events WHERE received<=?',(observed,)).fetchone()[0]
            rows=connection.execute('''SELECT s.seq,e.server,e.received,e.body FROM events e
                LEFT JOIN event_sequences s ON s.server=e.server AND s.event_key=e.event_key
                WHERE e.type='killed' AND e.received<=? AND json_extract(e.body,'$.killerSteamId')=?
                ORDER BY e.received DESC,s.seq DESC LIMIT ?''',(observed,sid,limit+1)).fetchall()
            events=[]
            for seq,server,received,body in rows[:limit]:
                obj=json.loads(body)
                if (type(seq) is not int or seq<1 or not isinstance(obj,dict) or obj.get('killerSteamId')!=sid
                        or number(received) is None or not isinstance(server,str) or not server):raise ValueError('invalid_event')
                events.append({'archiveId':str(seq),'eventId':text(obj.get('eventId')),'serverId':server,
                    'receivedAt':received,'gameTimeSeconds':number(obj.get('eventTime')),
                    'killer':{'steamId':sid,'name':text(obj.get('killerName')),'faction':text(obj.get('killerFaction'))},
                    'victim':{'steamId':steam(obj.get('victimSteamId')),'name':text(obj.get('victimName')),'faction':text(obj.get('victimFaction'))},
                    'weapon':text(obj.get('cause')),'relationship':relationship(obj)})
            result={'observedAt':observed,'timeBasis':'archive_received_at','steamId':sid,'limit':limit,
                'events':events,'hasMore':len(rows)>limit,'complete':True,
                'coverage':{'scope':'retained_local_archive','historyComplete':False,'recordsSince':number(coverage),
                    'sourceObservedAt':state[0],'caughtUp':True}}
            if len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode())>65536:
                raise ContractError(503,'player_kills_response_too_large')
        except (sqlite3.Error,OSError,ValueError,TypeError):
            raise ContractError(503,'player_kills_storage_unavailable') from None
        finally:
            if connection is not None:connection.close()
        validate_admin_actor(payload['actor'],payload['requestedAt'],self.engine.config,clock())
        if not 0<=clock()-state[0]<=15:raise ContractError(503,'player_kills_events_unavailable')
        return result
