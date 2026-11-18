"""Read-only scoped moderation evidence, not a count of distinct punishments."""
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
try:
    from service.moderation_ops import ContractError,validate_admin_actor,text
    from service.player_history import identity
except ImportError:
    from moderation_ops import ContractError,validate_admin_actor,text
    from player_history import identity

MAX_BYTES=65536
# Table and column expressions are constants; user values are bound parameters.
SOURCES=[
 dict(key='kick',table='guard.confirmed_kicks',kind='kick_confirmation',stamp='COALESCE(a.confirmed_at,a.recorded_at)',reason='a.reason',source='a.source',stage="'confirmed'",kick="'confirmed'",effective='a.confirmed_at',name='a.name'),
 dict(key='restriction',table='guard.permanent_ban_actions',kind='rejoin_restriction',stamp='a.created_at',reason='a.reason',source='a.source',stage="'applied'",effective='a.created_at',name='a.name'),
 dict(key='request',table='moderation_operations',kind='kick_request',stamp='a.created',name='a.player_name',source='a.source',stage='a.status',kick='a.kick_status',
      reason="CASE WHEN a.source='admin' THEN '管理员手动踢出' WHEN json_valid(a.payload_json) AND json_type(a.payload_json,'$.reason.zh')='text' THEN json_extract(a.payload_json,'$.reason.zh') END",
      effective="CASE WHEN a.kick_status='confirmed' THEN (SELECT MIN(timestamp) FROM moderation_audit m WHERE m.operation_id=a.id AND m.phase IN ('kick_confirmed','announcements_pending') AND m.timestamp>=a.created AND m.timestamp<=:now) END"),
 dict(key='restriction_request',table='admin_restrictions',kind='restriction_request',stamp='a.created',name='a.name',source="'admin_offline'",reason="'管理员离线永久重入限制'",stage='a.restriction_status',
      effective="(SELECT created_at FROM guard.permanent_ban_actions p WHERE p.id='admin-restriction:'||a.id AND p.steam_id=a.steam_id)",
      where="NOT EXISTS(SELECT 1 FROM guard.permanent_ban_actions p WHERE p.id='admin-restriction:'||a.id AND p.steam_id=a.steam_id)"),
 dict(key='cycle',table='admin_cycle_adds',kind='cycle_registration',stamp='a.created',name='a.name',source="'admin'",stage='a.cycle_status',effective='a.registered_at',already='a.already_enabled'),
 dict(key='unban',table='admin_unbans',kind='unban_verification',stamp='a.created',name='a.name',source="'admin'",stage='a.unban_status',native='a.native_status',local='a.local_status',effective="CASE WHEN a.unban_status='unbanned' AND (a.released_at IS NOT NULL OR a.native_verified_at IS NOT NULL) THEN MAX(COALESCE(a.released_at,0),COALESCE(a.native_verified_at,0)) END"),
 dict(key='release',table='guard.restriction_releases',kind='restriction_release',stamp='a.released_at',reason='a.reason',source="'local_release'",stage="'applied'",effective='a.released_at',
      where="NOT EXISTS(SELECT 1 FROM admin_unbans u WHERE 'admin-unban:'||u.id=a.id AND u.steam_id=a.steam_id)"),
 dict(key='behavior',table='guard.behavior_events',kind='behavior_action',stamp='a.created',name='a.name',source="'behavior_cycle'",stage='a.status',effective="CASE WHEN a.status='confirmed' THEN a.finished END",action='a.kind'),
 dict(key='burst',table='burst_actions',kind='burst_detection',stamp='a.created',name='a.name',source="'auto_burst'",reason="'5秒内至少3杀检测'",stage='a.status',
      where='NOT EXISTS(SELECT 1 FROM guard.permanent_ban_actions p WHERE p.id=a.id AND p.steam_id=a.steam_id)')
]
CATEGORIES={**{k:'automatic' for k in ('auto_kd','auto_native','auto_burst','auto_weapon','rejoin','behavior_cycle')},
            **{k:'manual' for k in ('admin','admin_offline','manual','manual_tk','local_release')},'report':'report','native_ban':'unknown','monitor':'unknown'}


def clean(value,limit):
    if not isinstance(value,str):return None
    result=''.join(c for c in value if c.isprintable()).strip()
    return result[:limit] or None


def number(value):
    return type(value) in (int,float) and math.isfinite(value) and value>=0


class PunishmentHistory:
    def __init__(self,engine,state_path,cursor_key=None):
        self.e=engine;self.path=Path(state_path);self.guard=self.path.with_name('kick_guard.sqlite3');self.cursor_key=cursor_key

    def connect(self):
        if self.e.kick_store is None:raise ContractError(503,'punishment_history_not_configured')
        c=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
        try:
            c.execute('ATTACH DATABASE ? AS guard',(self.guard.resolve().as_uri()+'?mode=ro',))
            c.execute('PRAGMA query_only=ON');c.row_factory=sqlite3.Row;c.execute('BEGIN')
            return c
        except Exception:c.close();raise

    def validate(self,p,required,optional,now):
        if not isinstance(p,dict) or not set(required)<=set(p) or not set(p)<=set(required)|set(optional):
            raise ContractError(400,'invalid_punishment_fields')
        validate_admin_actor(p['actor'],p['requestedAt'],self.e.config,now)

    def key(self):
        if self.cursor_key is not None:return self.cursor_key
        try:
            key=Path(os.environ.get('ADMIN_MODERATION_API_KEY_FILE','/run/secrets/admin_moderation_api_key')).read_bytes().strip()
            if len(key)<32:raise ValueError()
            return key
        except (OSError,ValueError):raise ContractError(503,'punishment_history_not_configured') from None

    def signature(self,encoded,p):
        binding=json.dumps(p['actor'],sort_keys=True,separators=(',',':')).encode()+b'\n'+p['steamId'].encode()+b'\n'+encoded
        return hmac.new(self.key(),b'punishment-history-v1\n'+binding,hashlib.sha256).digest()

    def cursor(self,data,p):
        raw=json.dumps(data,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')+'.'+base64.urlsafe_b64encode(self.signature(raw,p)).decode().rstrip('=')

    def decode(self,value,p,now,limit):
        try:
            if not isinstance(value,str) or not 1<=len(value)<=4096:raise ValueError()
            body,mac=value.split('.')
            raw=base64.b64decode(body+'='*(-len(body)%4),altchars=b'-_',validate=True)
            actual=base64.b64decode(mac+'='*(-len(mac)%4),altchars=b'-_',validate=True)
            if not hmac.compare_digest(actual,self.signature(raw,p)):raise ValueError()
            d=json.loads(raw)
            if not isinstance(d,dict) or set(d)!={'v','at','exp','cuts','last','limit'} or d['v']!=1 or d['limit']!=limit:raise ValueError()
            if not number(d['at']) or not number(d['exp']) or d['exp']!=d['at']+600 or now<d['at']:raise ValueError()
            if now>d['exp']:raise ContractError(400,'punishment_cursor_expired')
            if not isinstance(d['cuts'],dict) or set(d['cuts'])!={s['key'] for s in SOURCES}:raise ValueError()
            if any(type(v) is not int or not 0<=v<=2**63-1 for v in d['cuts'].values()):raise ValueError()
            last=d['last']
            if not isinstance(last,list) or len(last)!=3 or not number(last[0]) or type(last[1]) is not int or not 0<=last[1]<len(SOURCES) or type(last[2]) is not int or last[2]<1:raise ValueError()
            return d
        except ContractError:raise
        except (ValueError,TypeError,KeyError,UnicodeError,OverflowError):raise ContractError(400,'invalid_punishment_cursor') from None

    def names_sql(self):
        parts=["SELECT steam_id,name,COALESCE(last_seen,recorded) AS stamp FROM player_aliases"]
        for s in SOURCES:
            parts.append('SELECT a.steam_id,'+s.get('name','NULL')+','+s['stamp']+' AS stamp FROM '+s['table']+' a')
        parts.extend(['SELECT steam_id,name,eligible_at FROM guard.rejoin_restrictions','SELECT steam_id,NULL,updated_at FROM guard.native_ban_sync','SELECT steam_id,name,created FROM guard.behavior_players'])
        return ' UNION ALL '.join(parts)

    def person(self,c,sid):
        rows=c.execute('SELECT name,stamp FROM ('+self.names_sql()+') WHERE steam_id=? ORDER BY stamp DESC,name LIMIT 30',(sid,)).fetchall()
        if not rows:return None
        names=list(dict.fromkeys(n for r in rows if (n:=clean(r['name'],128)) is not None))
        return {'steamId':sid,'name':names[0] if names else sid,'aliases':names[1:6],
                'aliasesTruncated':len(names)>6 or len(rows)==30,'online':None,'source':'local_moderation_history'}

    def search(self,p,now=None):
        clock=time.time if now is None else lambda:now
        if not isinstance(p,dict) or set(p)!={'query','actor','requestedAt'}:raise ContractError(400,'invalid_punishment_search_fields')
        self.validate(p,{'query','actor','requestedAt'},set(),clock())
        q=p['query']
        if not text(q,128) or q!=q.strip():raise ContractError(400,'invalid_player_query')
        c=None
        try:
            c=self.connect()
            if identity(q):
                person=self.person(c,q);players=[person] if person else [];truncated=False
            else:
                rows=c.execute('SELECT steam_id,MAX(stamp) AS seen FROM ('+self.names_sql()+') WHERE instr(lower(name),lower(?))>0 GROUP BY steam_id ORDER BY seen DESC,steam_id LIMIT 21',(q,)).fetchall()
                if any(not identity(r['steam_id']) for r in rows):raise ValueError('invalid_identity')
                truncated=len(rows)>20;players=[self.person(c,r['steam_id']) for r in rows[:20]]
            self.validate(p,{'query','actor','requestedAt'},set(),clock())
            result={'observedAt':clock(),'complete':True,'truncated':truncated,'historyComplete':False,'players':players}
            while len(json.dumps(result,ensure_ascii=False).encode())>MAX_BYTES:
                result['players'].pop();result['truncated']=True
            return result
        except (sqlite3.Error,OSError,ValueError,TypeError,OverflowError):raise ContractError(503,'punishment_history_storage_unavailable') from None
        finally:
            if c is not None:c.close()

    def record(self,row,s,order):
        d=dict(row);stamp=d['stamp'];effective=d['effective']
        if not number(stamp) or (effective is not None and not number(effective)):raise ValueError('invalid_record_time')
        source=d['source'] if d['source'] in CATEGORIES else 'unknown';category=CATEGORIES.get(source,'unknown')
        kind=s['kind'];stage=d['stage'];kick='not_applicable';status='unknown';basis='operation_created_at'
        if kind=='kick_confirmation':
            status='confirmed';kick='confirmed';basis='kick_confirmed_at' if effective is not None else 'ledger_recorded_at_effective_unknown'
        elif kind in ('rejoin_restriction','restriction_release'):
            status='applied';basis='restriction_created_at' if kind=='rejoin_restriction' else 'release_recorded_at'
            if any(t in str(d['source']).lower() for t in ('histor','legacy','import')):effective=None;basis='ledger_recorded_at_effective_unknown'
        elif kind=='kick_request':
            raw=d['kick']
            if raw=='confirmed':status='confirmed';kick='confirmed'
            elif raw in ('failed','unknown'):status=raw;kick=raw
            elif stage in ('accepted','resolving','kick_dispatching'):status='pending';kick='pending'
            else:status='not_executed';kick='not_executed'
        elif kind=='cycle_registration':
            status=('already_registered' if d['already'] else 'registered') if stage=='registered' and effective is not None else self.stage(stage)
        elif kind=='unban_verification':
            if stage=='unbanned':status='verified_clear' if d['native']=='already_unbanned' and d['local'] in ('already_released','not_restricted') else 'confirmed'
            else:status=self.stage(stage)
        elif kind=='behavior_action':
            kind={'team':'team_switch','kill':'console_kill'}.get(d['action'],'behavior_action');status=self.stage(stage);basis='behavior_action_created_at'
        elif kind=='burst_detection':status=self.stage(stage);basis='detection_created_at'
        else:status=self.stage(stage)
        if kind=='restriction_request' and stage=='restricted' and effective is not None:status='applied'
        reason=clean(d['reason'],256)
        return {'recordId':s['key']+'-'+hashlib.sha256((s['key']+':'+str(d['id'])).encode()).hexdigest()[:24],
                'type':kind,'name':clean(d['name'],128),'timestamp':stamp,'timeBasis':basis,'effectiveAt':effective,
                'reason':reason,'reasonKnown':reason is not None,'reasonTruncated':isinstance(d['reason'],str) and len(d['reason'])>256,
                'source':{'category':category,'code':source},'executionStatus':status,'kickStatus':kick,
                'outcomes':{'nativeUnbanStatus':d['native'],'localReleaseStatus':d['local']} if kind=='unban_verification' else None,
                'errorCode':None},(stamp,order,d['rid'])

    def stage(self,value):
        if value in ('confirmed','applied','failed','unknown','partial'):return value
        if value in ('accepted','resolving','dispatching','pending','not_checked','not_attempted'):return 'pending'
        if value in ('cancelled','exempt','ignored_high_score','offline','rejected','superseded','not_needed'):return 'not_executed'
        return 'unknown'

    def current(self,c,sid):
        local=c.execute('SELECT status FROM guard.rejoin_restrictions WHERE steam_id=?',(sid,)).fetchone()
        native=c.execute('SELECT status,updated_at FROM guard.native_ban_sync WHERE steam_id=?',(sid,)).fetchone()
        cycle=c.execute('SELECT enabled FROM guard.behavior_players WHERE steam_id=?',(sid,)).fetchone()
        if local and local[0] not in ('active','dispatching','unknown'):raise ValueError('invalid_local_status')
        if native and (native[0] not in ('pending','dispatching','confirmed','unknown') or not number(native[1])):raise ValueError('invalid_native_status')
        if cycle and cycle[0] not in (0,1):raise ValueError('invalid_cycle_status')
        return {'localRestrictionStatus':local[0] if local else None,'nativeBanSyncStatus':native[0] if native else None,
                'nativeBanSyncObservedAt':native[1] if native else None,'nativeBanLiveVerified':False,'cycleEnabled':bool(cycle and cycle[0])}

    def query(self,p,now=None):
        clock=time.time if now is None else lambda:now
        self.validate(p,{'steamId','actor','requestedAt'},{'limit','cursor'},clock())
        if not identity(p['steamId']):raise ContractError(400,'invalid_steamId')
        limit=p.get('limit',20)
        if type(limit) is not int or not 1<=limit<=50:raise ContractError(400,'invalid_punishment_limit')
        token=p.get('cursor');data=self.decode(token,p,clock(),limit) if token is not None else None
        c=None
        try:
            c=self.connect();person=self.person(c,p['steamId'])
            if person is None:raise ContractError(404,'player_not_known')
            if data is None:
                snapshot=clock();data={'v':1,'at':snapshot,'exp':snapshot+600,'limit':limit,'cuts':{s['key']:c.execute('SELECT COALESCE(MAX(rowid),0) FROM '+s['table']).fetchone()[0] for s in SOURCES}}
            candidates=[]
            for order,s in enumerate(SOURCES):
                cols={'id':'a.id','name':s.get('name','NULL'),'stamp':s['stamp'],'reason':s.get('reason','NULL'),'source':s.get('source',"'unknown'"),'stage':s.get('stage',"'unknown'"),'kick':s.get('kick',"'not_applicable'"),'effective':s.get('effective','NULL'),'already':s.get('already','NULL'),'native':s.get('native','NULL'),'local':s.get('local','NULL'),'action':s.get('action','NULL')}
                select=','.join(expr+' AS '+key for key,expr in cols.items())
                sql='SELECT a.rowid AS rid,'+select+' FROM '+s['table']+' a WHERE a.steam_id=:sid AND a.rowid<=:cut AND ('+s.get('where','1')+')'
                params={'sid':p['steamId'],'cut':data['cuts'][s['key']],'n':limit+1,'now':clock()}
                if 'last' in data:
                    t,o,r=data['last'];sql+=' AND ('+s['stamp']+'<:last_t OR ('+s['stamp']+'=:last_t AND (:ord<:last_o OR (:ord=:last_o AND a.rowid<:last_r))))'
                    params.update(last_t=t,last_o=o,last_r=r,ord=order)
                sql+=' ORDER BY '+s['stamp']+' DESC,a.rowid DESC LIMIT :n'
                for row in c.execute(sql,params):candidates.append(self.record(row,s,order))
            candidates.sort(key=lambda item:item[1],reverse=True)
            chosen=candidates[:limit]
            result={'observedAt':clock(),'snapshotAt':data['at'],'snapshotMode':'membership_fenced_live_status','player':person,
                    'historyComplete':False,'coverage':'local_management_ledger_only','currentState':self.current(c,p['steamId']),
                    'records':[],'hasMore':len(candidates)>limit,'nextCursor':None,'responseLimited':False}
            while True:
                result['records']=[r for r,_ in chosen]
                if len(candidates)>len(chosen):
                    result['hasMore']=True;d=dict(data,last=list(chosen[-1][1]));result['nextCursor']=self.cursor(d,p)
                else:result['nextCursor']=None
                if len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode())<=MAX_BYTES:break
                if len(chosen)<=1:raise ValueError('record_size')
                chosen.pop();result['responseLimited']=True
            self.validate(p,{'steamId','actor','requestedAt'},{'limit','cursor'},clock())
            return result
        except (sqlite3.Error,OSError,ValueError,TypeError,OverflowError):raise ContractError(503,'punishment_history_storage_unavailable') from None
        finally:
            if c is not None:c.close()
