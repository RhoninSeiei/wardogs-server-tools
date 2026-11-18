"""Fixed administrator read-only moderation queries; trusted IDs only."""
import time
try:
    from service.moderation_ops import ContractError, validate_admin_actor, text
    from service.player_history import identity, name_text
    from service.behavior_policy import INTERVALS
except ImportError:
    from moderation_ops import ContractError, validate_admin_actor, text
    from player_history import identity, name_text
    from behavior_policy import INTERVALS


def native_bans(engine):
    data = engine.rcon.request('GET', '/v1/bans')
    bans = data.get('bans') if isinstance(data, dict) else None
    if (not isinstance(bans, list) or len(bans)>10000 or data.get('ok') is False or data.get('pending')
            or any(not isinstance(b,dict) or not identity(b.get('steamId')) for b in bans)
            or len({b['steamId'] for b in bans}) != len(bans)):
        raise ValueError('invalid_native_bans')
    return {b['steamId']:b for b in bans}


def roster(engine):
    data = engine.rcon.request('GET', '/v1/players')
    players = data.get('players') if isinstance(data,dict) else None
    if (not isinstance(players,list) or len(players)>1000 or data.get('ok') is False or data.get('pending')
            or any(not isinstance(p,dict) or not identity(p.get('steamId')) for p in players)
            or len({p['steamId'] for p in players}) != len(players)):
        raise ValueError('invalid_live_roster')
    return {p['steamId']:p for p in players}


def local_player(engine,sid):
    known=engine.player_history.known(sid)
    if known is not None:return known
    guard=engine.kick_store
    if guard is None:return None
    with guard.lock:
        names=guard.db.execute("SELECT name,recorded_at FROM confirmed_kicks WHERE steam_id=? UNION ALL SELECT name,created_at FROM permanent_ban_actions WHERE steam_id=? ORDER BY 2 DESC",(sid,sid)).fetchall()
        released=guard.released_at(sid)
    if not names and released is None:return None
    aliases=list(dict.fromkeys(row[0] for row in names if name_text(row[0])))
    return {'steamId':sid,'name':aliases[0] if aliases else sid,'aliases':aliases[1:21]}


class AdminTools:
    def __init__(self, engine): self.engine=engine

    def validate(self, payload, fields, now):
        if not isinstance(payload,dict) or set(payload)!=set(fields): raise ContractError(400,'invalid_admin_query_fields')
        validate_admin_actor(payload['actor'],payload['requestedAt'],self.engine.config,now)

    def cycle_list(self, payload, now=None):
        clock=time.time if now is None else lambda:now
        self.validate(payload,['actor','requestedAt'],clock())
        if self.engine.behavior is None:raise ContractError(503,'cycle_not_configured')
        started=clock()
        try: live=roster(self.engine)
        except Exception:raise ContractError(503,'cycle_list_players_unavailable') from None
        if not 0<=clock()-started<=15:raise ContractError(503,'cycle_list_players_unavailable')
        guard=self.engine.kick_store
        with guard.lock:
            rows=guard.db.execute('SELECT steam_id,name FROM behavior_players WHERE enabled=1 ORDER BY created,steam_id').fetchall()
        self.validate(payload,['actor','requestedAt'],clock())
        if not 0<=clock()-started<=15:raise ContractError(503,'cycle_list_players_unavailable')
        if any(not identity(sid) for sid,name in rows):raise ContractError(503,'cycle_list_storage_unavailable')
        observed=clock()
        players=[{'steamId':sid,'name':live[sid]['name'] if sid in live and name_text(live[sid].get('name')) else name,
                  'online':sid in live} for sid,name in rows]
        return {'observedAt':observed,'complete':True,'players':players,
                'policy':{'version':'behavior_policy_v1','teamIntervalSeconds':list(INTERVALS['team']),
                          'killIntervalSeconds':list(INTERVALS['kill']),
                          'firstObservationActionsDueImmediately':True,'subsequentJoinReschedulesTimers':True,
                          'workerCheckIntervalSeconds':1,'equipmentPurchaseTriggered':False,
                          'inventoryClearingSupported':False,'reservedPlayersExempt':True,
                          'noticeAfterConfirmedAction':True,'noticeTemplate':'哈哈 {name} [{steamId}]'}}

    def unban_search(self, payload, now=None):
        clock=time.time if now is None else lambda:now
        self.validate(payload,['query','actor','requestedAt'],clock())
        query=payload['query']
        if not text(query,128) or query!=query.strip():raise ContractError(400,'invalid_player_query')
        guard=self.engine.kick_store
        if guard is None:raise ContractError(503,'unban_not_configured')
        started=clock()
        try:bans=native_bans(self.engine)
        except Exception:raise ContractError(503,'unban_search_native_unavailable') from None
        if not 0<=clock()-started<=15:raise ContractError(503,'unban_search_native_unavailable')
        with guard.lock:
            restrictions=dict(guard.db.execute('SELECT steam_id,name FROM rejoin_restrictions'))
            pending=dict(guard.db.execute('SELECT steam_id,status FROM native_ban_sync'))
        ids=set(bans)|set(restrictions)|set(pending)
        if any(not identity(sid) for sid in ids):raise ContractError(503,'unban_search_storage_unavailable')
        if identity(query):
            known=local_player(self.engine,query)
            ids={query} if query in ids or known is not None else set()
        result=[]
        with self.engine.lock:
            for sid in sorted(ids):
                known=local_player(self.engine,sid)
                names=list(dict.fromkeys(([known['name']]+known['aliases'] if known else [])+
                             ([restrictions[sid]] if sid in restrictions else [])+
                             ([bans[sid]['name']] if sid in bans and name_text(bans[sid].get('name')) else [])))
                names=[n for n in names if name_text(n)]
                name=next((n for n in names if n!=sid),sid)
                if not identity(query) and not any(query.casefold() in n.casefold() for n in names):continue
                result.append({'steamId':sid,'name':name,'aliases':[n for n in names if n!=name][:20],
                               'online':None,'nativeBanned':sid in bans,'localRestricted':sid in restrictions,
                               'nativeSyncState':pending.get(sid),'source':'moderation_history'})
                if len(result)>20:break
        self.validate(payload,['query','actor','requestedAt'],clock())
        if not 0<=clock()-started<=15:raise ContractError(503,'unban_search_native_unavailable')
        return {'observedAt':clock(),'complete':True,'players':result[:20],'truncated':len(result)>20}
