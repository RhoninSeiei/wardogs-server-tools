"""Administrator-only 60 or 300 seconds of locally synchronized kill events."""
import json
import math
from pathlib import Path
import re
import sqlite3
import time
try:
    from service.moderation_ops import ContractError,validate_admin_actor
except ImportError:
    from moderation_ops import ContractError,validate_admin_actor


def steam(value):
    return value if isinstance(value,str) and re.fullmatch('[0-9]{17}',value) else None


def text(value):
    return value if isinstance(value,str) else None


class RecentKills:
    def __init__(self,engine,path):self.engine=engine;self.path=Path(path)

    def query(self,payload,now=None):
        clock=time.time if now is None else lambda:now
        if (not isinstance(payload,dict) or not {'actor','requestedAt'}<=set(payload)
                or not set(payload)<={'actor','requestedAt','windowSeconds'}):
            raise ContractError(400,'invalid_recent_kills_fields')
        validate_admin_actor(payload['actor'],payload['requestedAt'],self.engine.config,clock())
        window=payload.get('windowSeconds',60)
        if type(window) is not int or window not in (60,300):
            raise ContractError(400,'invalid_recent_kills_window')
        connection=None
        try:
            connection=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
            connection.execute('BEGIN')
            state=connection.execute("SELECT last_success,error FROM sync_state WHERE kind='events'").fetchone()
            observed=clock()
            if (not state or state[1] is not None or type(state[0]) not in (int,float)
                    or not math.isfinite(state[0]) or not 0<=observed-state[0]<=15):
                raise ContractError(503,'recent_kills_events_unavailable')
            cursor=connection.execute('SELECT generation,after_seq,latest_seq FROM sync_cursor WHERE id=1').fetchone()
            if (not cursor or not cursor[0] or type(cursor[1]) is not int or type(cursor[2]) is not int
                    or cursor[1]<0 or cursor[1]!=cursor[2]):
                raise ContractError(503,'recent_kills_sync_incomplete')
            rows=connection.execute('''SELECT s.seq,e.server,e.received,e.body FROM events e
                LEFT JOIN event_sequences s ON s.server=e.server AND s.event_key=e.event_key
                WHERE e.type='killed' AND e.received>=? AND e.received<=?
                ORDER BY e.received DESC,s.seq DESC LIMIT 1001''',(observed-window,observed)).fetchall()
            events=[]
            for seq,server,received,body in rows[:1000]:
                obj=json.loads(body)
                if seq is None or not isinstance(obj,dict):raise ValueError('invalid_event')
                events.append({'archiveId':str(seq),'eventId':text(obj.get('eventId')),'serverId':server,
                    'receivedAt':received,'killer':{'steamId':steam(obj.get('killerSteamId')),'name':text(obj.get('killerName'))},
                    'weapon':text(obj.get('cause')),
                    'victim':{'steamId':steam(obj.get('victimSteamId')),'name':text(obj.get('victimName'))}})
            result={'observedAt':observed,'windowStart':observed-window,'windowEnd':observed,
                    'timeBasis':'archive_received_at','complete':len(rows)<=1000,'events':events}
        except (sqlite3.Error,OSError,ValueError,TypeError):
            raise ContractError(503,'recent_kills_storage_unavailable') from None
        finally:
            if connection is not None:connection.close()
        validate_admin_actor(payload['actor'],payload['requestedAt'],self.engine.config,clock())
        return result
