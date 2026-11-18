"""Forward-only five-second triple-kill rule, backed by the local event cache."""
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid
try:
 from service.rcon import reserved_ids,PunishmentExemptError
except ImportError:
 from rcon import reserved_ids,PunishmentExemptError

try:
 from service.kill_accounting import barbed_wire
except ImportError:
 from kill_accounting import barbed_wire

def steam(value):return isinstance(value,str) and re.fullmatch('[0-9]{17}',value) is not None
REASON='触发本服5秒内至少3杀规则'

def high_score(status):
 rows=status.get('factionScores') if isinstance(status,dict) else None
 if not isinstance(rows,list) or not rows:raise ValueError('score_unavailable')
 values=[r.get('score') if isinstance(r,dict) else None for r in rows]
 if any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) or v<0 for v in values):raise ValueError('score_unavailable')
 return any(v>98 for v in values)

def vehicle_explosion(event):
 """Only the explicit game context tag proves an explosion, not a weapon name."""
 tags=event.get('contextTags')
 return isinstance(tags,list) and 'Meta.Progression.Context.Player.KillContext.VehicleExplosion' in tags

def excluded_event(event):return barbed_wire(event) or vehicle_explosion(event)

def friendly_hazard(events):
 if not all(o.get('teamkill') is True for o in events):return False
 return any(any(k in str(o.get('cause','')).lower() for k in ('c4','vehicle','helicopter','rotary')) or
            any('VehicleExplosion' in str(t) for t in o.get('contextTags',[]) or []) for o in events)

class BurstGuard:
 def __init__(self,engine,cache):
  self.e=engine;self.cache=Path(cache);self.last_error=None
  with engine.lock,engine.db:
   engine.db.executescript('''
    CREATE TABLE IF NOT EXISTS burst_meta(id INTEGER PRIMARY KEY,generation TEXT,cursor INTEGER,armed REAL);
    CREATE TABLE IF NOT EXISTS burst_events(server TEXT,event_key TEXT,steam_id TEXT,match_id TEXT,t REAL,received REAL,body TEXT,PRIMARY KEY(server,event_key));
    CREATE TABLE IF NOT EXISTS burst_skips(id TEXT PRIMARY KEY,reason TEXT,evidence TEXT,created REAL);
    CREATE INDEX IF NOT EXISTS burst_window ON burst_events(server,steam_id,match_id,t);
    CREATE TABLE IF NOT EXISTS burst_actions(id TEXT PRIMARY KEY,steam_id TEXT,name TEXT,evidence TEXT,status TEXT,created REAL,updated REAL,retry_at REAL,error TEXT);
   ''')
   interrupted=engine.db.execute("SELECT id,steam_id FROM burst_actions WHERE status='dispatching'").fetchall()
   for aid,sid in interrupted:
    engine.db.execute("UPDATE burst_actions SET status='unknown',error='restart_during_dispatch' WHERE id=?",(aid,))
    engine.db.execute("UPDATE moderation_targets SET outcome='unknown',expires=NULL WHERE operation_id=?",(aid,))
    if engine.kick_store is not None:engine.kick_store.failed(sid,'permanent',time.time(),True)

 def ingest(self,now):
  c=sqlite3.connect(self.cache.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
  try:
   c.execute('BEGIN')
   state=c.execute("SELECT last_success,error FROM sync_state WHERE kind='events'").fetchone()
   if not state or state[1] is not None or not 0<=now-state[0]<=15:raise ValueError('event_feed_not_fresh')
   status=c.execute("SELECT body,observed FROM source_snapshots WHERE kind='status'").fetchone()
   if not status or not 0<=now-status[1]<=15:raise ValueError('score_snapshot_stale')
   paused=high_score(json.loads(status[0]))
   generation=c.execute('SELECT generation FROM export_meta WHERE id=1').fetchone()[0]
   latest=c.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]
   with self.e.lock,self.e.db:
    meta=self.e.db.execute('SELECT generation,cursor,armed FROM burst_meta WHERE id=1').fetchone()
    if not meta or meta[0]!=generation or meta[1]>latest:
     self.e.db.execute('DELETE FROM burst_events')
     self.e.db.execute('INSERT OR REPLACE INTO burst_meta VALUES(1,?,?,?)',(generation,latest,now));return
    after,armed=meta[1:]
    if paused:
     self.e.db.execute('DELETE FROM burst_events')
     self.e.db.execute('UPDATE burst_meta SET cursor=? WHERE id=1',(latest,))
     self.e.db.execute("UPDATE burst_actions SET status='ignored_high_score',updated=? WHERE status='accepted'",(now,))
     return
    for seq,server,key,received,kind,body in c.execute('SELECT rowid,server,event_key,received,type,body FROM events WHERE rowid>? ORDER BY rowid LIMIT 1000',(after,)):
     after=seq
     if kind!='killed' or received<armed:continue
     o=json.loads(body)
     if excluded_event(o):continue
     sid=o.get('killerSteamId');victim=o.get('victimSteamId');t=o.get('eventTime');match=o.get('matchId')
     if not steam(sid) or not steam(victim) or sid==victim or not isinstance(t,(int,float)) or isinstance(t,bool) or not math.isfinite(t) or t<0 or not isinstance(match,str) or not match or not key or not isinstance(o.get('teamkill'),bool):continue
     self.e.db.execute('INSERT OR IGNORE INTO burst_events VALUES(?,?,?,?,?,?,?)',(server,key,sid,match,t,received,body))
     # Check every five-second window containing the new event, also for late/out-of-order arrivals.
     near=self.e.db.execute('SELECT t,body FROM burst_events WHERE server=? AND steam_id=? AND match_id=? AND t BETWEEN ? AND ? ORDER BY t,event_key',(server,sid,match,t-5,t+5)).fetchall()
     evidence=None
     for i in range(len(near)):
      window=[json.loads(x[1]) for x in near[i:] if x[0]-near[i][0]<=5 and not excluded_event(json.loads(x[1]))]
      if len(window)<3:continue
      if friendly_hazard(window):
       encoded=json.dumps(window,ensure_ascii=False)
       skip=hashlib.sha256((str(server)+encoded).encode()).hexdigest()
       self.e.db.execute('INSERT OR IGNORE INTO burst_skips VALUES(?,?,?,?)',(skip,'all_friendly_explosive_or_vehicle',encoded,now))
       continue
      evidence=window;break
     if evidence:
      aid='burst:'+hashlib.sha256(json.dumps([generation,server,sid,match]).encode()).hexdigest()
      name=''.join(x for x in str(o.get('killerName',sid)) if x.isprintable())[:60] or sid
      self.e.db.execute("INSERT OR IGNORE INTO burst_actions VALUES(?,?,?,?,'accepted',?,?,0,NULL)",(aid,sid,name,json.dumps(evidence,ensure_ascii=False),now,now))
    self.e.db.execute('UPDATE burst_meta SET cursor=? WHERE id=1',(after,))
    self.e.db.execute('DELETE FROM burst_events WHERE received<?',(now-3600,))
  finally:c.close()

 def finish(self,aid,status,now,error=None):
  with self.e.db:
   self.e.db.execute('UPDATE burst_actions SET status=?,updated=?,error=? WHERE id=?',(status,now,error,aid))
   if status=='unknown':self.e.db.execute("UPDATE moderation_targets SET outcome='unknown',expires=NULL WHERE operation_id=?",(aid,))
   else:self.e.db.execute('DELETE FROM moderation_targets WHERE operation_id=?',(aid,))

 def process_one(self,now=None):
  now=time.time() if now is None else now
  with self.e.lock:
   row=self.e.db.execute("SELECT id,steam_id FROM burst_actions WHERE status='accepted' AND retry_at<=? ORDER BY created,id LIMIT 1",(now,)).fetchone()
   if not row:return False
   aid,sid=row;handoff=self.e.unknown_handoff
   with handoff.target_lock(sid) as locked:
    if not locked:return False
    # Another instance may have completed this action before lock acquisition.
    current=self.e.db.execute("SELECT status FROM burst_actions WHERE id=?",(aid,)).fetchone()
    if current!=('accepted',):return False
    observer=getattr(self.e.rcon,'dispatch_observer',None)
    if observer is None:return self._process_one(now,aid)
    initial=handoff.guard_snapshot(sid) if handoff.tracked_executor() else None;attempt=uuid.uuid4().hex
    with observer.observe(sid) as trace:
     result=self._process_one(now,aid)
    handoff.publish(aid,sid,attempt,initial,trace)
    return result

 def _process_one(self,now=None,expected_id=None):
  now=time.time() if now is None else now
  with self.e.lock:
   row=self.e.db.execute("SELECT id,steam_id,name FROM burst_actions WHERE id=? AND status='accepted' AND retry_at<=?",(expected_id,now)).fetchone()
   if not row:return False
   aid,sid,name=row;g=self.e.kick_store
   if g is None:raise ValueError('burst_requires_rejoin_guard')
   evidence=json.loads(self.e.db.execute('SELECT evidence FROM burst_actions WHERE id=?',(aid,)).fetchone()[0])
   if len([event for event in evidence if not excluded_event(event)])<3:
    reason='vehicle_explosion_excluded' if any(vehicle_explosion(event) for event in evidence) else 'barbed_wire_excluded'
    self.finish(aid,'cancelled',now,reason);return True
   dispatched=False
   released=g.released_at(sid)
   created=self.e.db.execute('SELECT created FROM burst_actions WHERE id=?',(aid,)).fetchone()[0]
   if released is not None and created<=released:
    self.finish(aid,'cancelled',now,'auto_precedes_unban');return True
   try:
    # Exact shared target lock; uncertain moderation is never bypassed.
    with self.e.db:
     self.e.db.execute("DELETE FROM moderation_targets WHERE steam_id=? AND outcome='confirmed' AND expires<=?",(sid,now))
     self.e.db.execute("INSERT OR IGNORE INTO moderation_targets VALUES(?,?,'held',NULL)",(sid,aid))
     owner=self.e.db.execute('SELECT operation_id FROM moderation_targets WHERE steam_id=?',(sid,)).fetchone()[0]
     if owner!=aid:
      self.e.db.execute('UPDATE burst_actions SET retry_at=? WHERE id=?',(now+30,aid));return False
    if high_score(self.e.rcon.request('GET','/v1/status')):
     self.finish(aid,'ignored_high_score',now);return True
    g.sync_reserved(reserved_ids(self.e.rcon.request('GET','/v1/reserved-slots')),now);g.require_not_exempt(sid)
    ps=self.e.rcon.request('GET','/v1/players').get('players')
    if not isinstance(ps,list) or any(not isinstance(p,dict) or not steam(p.get('steamId')) for p in ps) or len({p['steamId'] for p in ps})!=len(ps):raise ValueError('invalid_roster')
    online=any(p['steamId']==sid for p in ps)
    with self.e.db:self.e.db.execute("UPDATE burst_actions SET status='dispatching',updated=? WHERE id=?",(now,aid))
    dispatched=True
    g.require_not_exempt(sid)
    g.restrict(sid,name,REASON,'auto_burst',now,action_id=aid)
    g.request_native_bans([sid],now)
    removed=False
    if online:
     observer=getattr(self.e.rcon,'dispatch_observer',None)
     trace=getattr(observer.local,'trace',None) if observer is not None else None
     if trace is not None:trace.executor_entered=True
     response=self.e.rcon._kick(sid,{'reason':REASON},expected_scope='permanent')
     if response.get('guardCancelled'):online=False
     else:
      if response.get('ok') is not True or response.get('pending'):raise ValueError('removal_not_confirmed')
      removed=True
    native=g.native_ban_state(sid)=='confirmed'
    # Actual receipt drives wording; offline/queued never becomes a fake ban or kick.
    if removed:
     zh=f'管理通告：{name}触发5秒内至少3杀规则，已移出服务器。'+('原生封禁已确认。' if native else '永久重入限制已登记，原生封禁待处理。')
     en=f'[EN] {name} triggered the 3-kills-in-5-seconds rule and was removed. '+('Native ban confirmed.' if native else 'Permanent rejoin restriction active; native ban pending.')
     ja=f'[JA] {name}は5秒以内に3キル以上のルールに該当し、退出処理しました。'+('ゲーム側のBANを確認済みです。' if native else '再参加制限を登録し、ゲーム側のBANは保留中です。')
    else:
     zh=f'管理通告：{name}触发5秒内至少3杀规则，已登记永久重入限制。处理时离线，未实际踢出；原生封禁待上线执行。'
     en=f'[EN] {name} triggered the 3-kills-in-5-seconds rule. Offline; permanent rejoin restriction active and native ban queued. No kick performed.'
     ja=f'[JA] {name}は5秒以内に3キル以上のルールに該当しました。オフラインのため再参加制限を登録し、次回接続時にBAN予定です。今回は退出操作を行っていません。'
    with self.e.db:
     for m in [zh,en,ja]:
      if len(m)>200:raise ValueError('notice_too_long')
      self.e.add_job(m,now,kind='management')
     self.finish(aid,'confirmed' if removed else 'offline',now)
   except PunishmentExemptError as ex:self.finish(aid,'exempt',now,ex.code)
   except Exception as ex:
    if dispatched:
     g.failed(sid,'permanent',now,True);self.finish(aid,'unknown',now,type(ex).__name__)
    else:
     with self.e.db:
      self.e.db.execute('UPDATE burst_actions SET retry_at=?,error=? WHERE id=?',(now+60,type(ex).__name__,aid))
      self.e.db.execute('DELETE FROM moderation_targets WHERE operation_id=?',(aid,))
   return True

 def snapshot(self):
  with self.e.lock:
   meta=self.e.db.execute('SELECT cursor,armed FROM burst_meta WHERE id=1').fetchone()
   counts=dict(self.e.db.execute('SELECT status,count(*) FROM burst_actions GROUP BY status'))
  return {'enabled':self.e.config.get('burst_ban_enabled',False),'windowSeconds':5,'minimumKills':3,'includesTeamkills':True,'includesExplosivesAndVehicles':True,'allFriendlyHazardExempt':True,'vehicleExplosionContextExempt':True,'ignoreAnyTeamScoreAbove':98,'armedAt':meta[1] if meta else None,'cursor':meta[0] if meta else None,'actionCounts':counts,'lastError':self.last_error}

 def tick(self,now=None):
  if not self.e.config.get('burst_ban_enabled',False):return
  now=time.time() if now is None else now
  try:
   self.ingest(now)
   self.process_one(now)
   self.last_error=None
  except Exception as ex:
   self.last_error=type(ex).__name__;raise
