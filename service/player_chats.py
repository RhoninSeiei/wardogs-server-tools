"""Admin-only readonly merged chat history; all network I/O belongs to the cache."""
import json,math,sqlite3,time
from pathlib import Path
from datetime import datetime,timezone
try:
    from service.moderation_ops import ContractError,validate_admin_actor
except ImportError:
    from moderation_ops import ContractError,validate_admin_actor

CHANNELS={'Global','Team','Squad','Proximity'}
RULE='chat_strict_unique_time_v1'
def sid(value):return isinstance(value,str) and len(value)==17 and value.isascii() and value.isdigit()
def finite(value):return type(value) in (int,float) and math.isfinite(value) and value>=0

class PlayerChats:
    def __init__(self,engine,path):self.engine,self.path=engine,Path(path)
    def fresh(self,c,now):
        states={kind:(stamp,error) for kind,stamp,error in c.execute("SELECT kind,last_success,error FROM sync_state WHERE kind IN ('events','chat')")}
        if any(k not in states or states[k][1] is not None or not finite(states[k][0]) or not 0<=now-states[k][0]<=15 for k in ('events','chat')):raise ContractError(503,'player_chats_sources_unavailable')
        cursor=c.execute('SELECT generation,after_seq,latest_seq FROM sync_cursor WHERE id=1').fetchone()
        if not cursor or not cursor[0] or type(cursor[1]) is not int or type(cursor[2]) is not int or cursor[1]<0 or cursor[1]!=cursor[2]:raise ContractError(503,'player_chats_sync_incomplete')
        meta={key:json.loads(value) for key,value in c.execute('SELECT key,value FROM chat_meta')}
        generation=c.execute('SELECT generation FROM export_meta WHERE id=1').fetchone()[0]
        latest=c.execute("SELECT COALESCE(MAX(rowid),0) FROM events WHERE type='chat_message'").fetchone()[0]
        callback=meta.get('callback',{});rcon=meta.get('rcon',{})
        if not isinstance(callback,dict) or not isinstance(rcon,dict):raise ValueError('invalid_chat_meta')
        if meta.get('callback_error') is not None or callback.get('generation')!=generation or type(callback.get('afterRowid')) is not int or callback['afterRowid']<latest or rcon.get('caughtUp') is not True:raise ContractError(503,'player_chats_index_incomplete')
        return {'rcon':{'sourceObservedAt':states['chat'][0],'caughtUp':True,'latestSeq':rcon.get('latestSeq'),'retainedWindowCount':rcon.get('retainedCount')},'callback':{'sourceObservedAt':states['events'][0],'caughtUp':True,'indexed':True}}
    def query(self,payload,now=None):
        clock=time.time if now is None else lambda:now
        if not isinstance(payload,dict) or not {'actor','requestedAt'}<=set(payload) or not set(payload)<={'actor','requestedAt','steamId','query','limit','channel'} or ('steamId' in payload)==('query' in payload):raise ContractError(400,'invalid_player_chats_fields')
        validate_admin_actor(payload['actor'],payload['requestedAt'],self.engine.config,clock())
        limit=payload.get('limit',20);channel=payload.get('channel')
        if type(limit) is not int or not 1<=limit<=50:raise ContractError(400,'invalid_player_chats_limit')
        if 'channel' in payload and (not isinstance(channel,str) or channel not in CHANNELS):raise ContractError(400,'invalid_player_chats_channel')
        target=payload.get('steamId');query=payload.get('query')
        if 'steamId' in payload and not sid(target):raise ContractError(400,'invalid_player_chats_steam_id')
        if 'query' in payload:
            if not isinstance(query,str) or not 1<=len(query)<=128 or query!=query.strip() or not all(c.isprintable() for c in query):raise ContractError(400,'invalid_player_chats_query')
            if sid(query):target=query
        c=None
        try:
            c=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1);c.row_factory=sqlite3.Row
            deadline=time.monotonic()+2;c.set_progress_handler(lambda:int(time.monotonic()>deadline),2000)
            # Historical aliases may resolve a renamed/offline player; no refresh or remote request.
            state_path=self.path.parent.parent/'state.db'
            c.execute('ATTACH DATABASE ? AS state',(state_path.resolve().as_uri()+'?mode=ro',))
            c.execute('PRAGMA query_only=ON');c.execute('BEGIN');observed=clock();sources=self.fresh(c,observed)
            if target is None:
                candidates=c.execute('''SELECT steam_id,MAX(seen) seen FROM (
                 SELECT steam_id,last_seen seen FROM chat_aliases WHERE instr(folded,?)>0
                 UNION ALL SELECT steam_id,COALESCE(last_seen,recorded) seen FROM state.player_aliases WHERE instr(folded,?)>0)
                 GROUP BY steam_id ORDER BY seen DESC,steam_id LIMIT 21''',(query.casefold(),query.casefold())).fetchall()
                players=[]
                for row in candidates[:20]:
                    if not sid(row['steam_id']):raise ValueError('invalid_chat_alias')
                    name=c.execute('SELECT name FROM chat_aliases WHERE steam_id=? ORDER BY last_seen DESC,name LIMIT 1',(row['steam_id'],)).fetchone()
                    if name is None:name=c.execute('SELECT name FROM state.player_aliases WHERE steam_id=? ORDER BY COALESCE(last_seen,recorded) DESC LIMIT 1',(row['steam_id'],)).fetchone()
                    players.append({'steamId':row['steam_id'],'name':name[0] if name else None})
                if not players:raise ContractError(404,'player_chats_player_not_found')
                if len(candidates)>1:
                    result={'observedAt':observed,'status':'candidates','query':query,'players':players,'truncated':len(candidates)>20,'complete':len(candidates)<=20,'messages':[]}
                    validate_admin_actor(payload['actor'],payload['requestedAt'],self.engine.config,clock());self.fresh(c,clock());return result
                target=players[0]['steamId']
            condition=' AND channel=?' if channel else '';args=[target]+([channel] if channel else [])
            rows=c.execute('''SELECT * FROM (
             SELECT r.id chat_id,r.id rcon_id,l.callback_id callback_id,r.sent_at timestamp FROM chat_observations r
              LEFT JOIN chat_links l ON l.rcon_id=r.id WHERE r.source='rcon' AND r.steam_id=?'''+condition.replace('channel','r.channel')+'''
             UNION ALL
             SELECT e.id chat_id,NULL rcon_id,e.id callback_id,e.received_at timestamp FROM chat_observations e
              WHERE e.source='callback' AND e.steam_id=?'''+condition.replace('channel','e.channel')+'''
              AND NOT EXISTS(SELECT 1 FROM chat_links l WHERE l.callback_id=e.id))
             WHERE timestamp<=? ORDER BY timestamp DESC,chat_id DESC LIMIT ?''',args+args+[observed,limit+1]).fetchall()
            messages=[]
            for row in rows[:limit]:
                r=c.execute('SELECT * FROM chat_observations WHERE id=?',(row['rcon_id'],)).fetchone() if row['rcon_id'] else None
                e=c.execute('SELECT * FROM chat_observations WHERE id=?',(row['callback_id'],)).fetchone() if row['callback_id'] else None
                main=r if r is not None else e
                if main is None or main['steam_id']!=target or not finite(row['timestamp']):raise ValueError('invalid_chat_record')
                if not isinstance(main['text'],str) or len(main['text'])>8192 or type(main['candidate_count']) is not int or main['candidate_count']<0 or main['anonymous'] not in (None,0,1) or main['ping'] not in (None,0,1):raise ValueError('invalid_chat_record')
                if main['raw_text'] is not None and (not isinstance(main['raw_text'],str) or len(main['raw_text'])>8192):raise ValueError('invalid_chat_record')
                if r is not None and e is not None:
                    if any(r[k]!=e[k] for k in ('server_id','steam_id','signature')) or not r['signature'] or r['candidate_count']!=1 or e['candidate_count']!=1 or not -2<=e['received_at']-r['sent_at']<=10:raise ValueError('invalid_chat_link')
                evidence=[]
                for source,item in [('rcon',r),('callback',e)]:
                    if item is None:continue
                    evidence.append({'observationId':item['id'],'source':source,'name':item['name'],'receivedAt':item['received_at'],'sentAt':item['sent_at'],'rconSeq':item['rcon_seq'],'eventId':item['event_id'],'gameTimeSeconds':item['game_time']})
                merged=r is not None and e is not None
                messages.append({'chatId':row['chat_id'],'serverId':main['server_id'],'steamId':target,'name':main['name'],'hiddenName':main['hidden_name'],'anonymous':bool(main['anonymous']) if main['anonymous'] is not None else None,'channel':main['channel'],'faction':main['faction'],'text':main['text'],'rawText':main['raw_text'],'ping':bool(main['ping']) if main['ping'] is not None else None,'timestamp':row['timestamp'],'timeBasis':'rcon_sent_utc' if r is not None else 'callback_archive_received_at','sources':[x['source'] for x in evidence],'correlation':{'status':'time_correlated' if merged else 'ambiguous' if main['candidate_count'] else 'single_source','rule':RULE if merged else None,'candidateCount':main['candidate_count'],'deltaSeconds':e['received_at']-r['sent_at'] if merged else None},'evidence':evidence})
            since=c.execute('SELECT MIN(COALESCE(sent_at,received_at)) FROM chat_observations').fetchone()[0]
            result={'observedAt':observed,'status':'ok','steamId':target,'limit':limit,'channel':channel,'complete':True,'hasMore':len(rows)>limit,'timeBasis':'per_message','messages':messages,'coverage':{'scope':'retained_local_chat_archive','historyComplete':False,'recordsSince':since,'sources':sources,'correlationRule':RULE,'callbackDeltaWindowSeconds':[-2,10]}}
            validate_admin_actor(payload['actor'],payload['requestedAt'],self.engine.config,clock());self.fresh(c,clock())
            if len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode())>131072:raise ContractError(503,'player_chats_response_too_large')
            return result
        except (sqlite3.Error,OSError,ValueError,TypeError,KeyError,IndexError):raise ContractError(503,'player_chats_storage_unavailable') from None
        finally:
            if c is not None:c.close()
