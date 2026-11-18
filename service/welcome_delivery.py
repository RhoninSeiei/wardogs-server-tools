"""Bounded, age-aware private welcome delivery; no broadcast fallback."""
import json
import logging
from pathlib import Path
import re
import sqlite3
import time
from contextlib import nullcontext
try:
 from service.rcon import RconError
except ImportError:
 from rcon import RconError
MAX_AGE=180
LOG=logging.getLogger('wardogs')

def pack(parts):
 result=[]
 for text in parts:
  if not isinstance(text,str) or not text or len(text)>200 or any(not c.isprintable() for c in text):raise ValueError('invalid welcome part')
  if result and len(result[-1])+1+len(text)<=200:result[-1]+=' '+text
  else:result.append(text)
 return result

def valid_players(data):
 ps=data.get('players') if isinstance(data,dict) and data.get('ok') is not False and not data.get('pending') else None
 if not isinstance(ps,list) or len(ps)>1000 or any(not isinstance(p,dict) or not isinstance(p.get('steamId'),str) or re.fullmatch('[0-9]{17}',p['steamId']) is None for p in ps):raise ValueError('invalid welcome roster')
 if len({p['steamId'] for p in ps})!=len(ps):raise ValueError('duplicate welcome identity')
 return {p['steamId'] for p in ps}

class WelcomeDelivery:
 def __init__(self,engine,cache):
  self.e=engine;self.cache=Path(cache)
  with engine.lock,engine.db:
   engine.db.executescript('''
    CREATE TABLE IF NOT EXISTS welcome_delivery_state(job_id TEXT PRIMARY KEY,posts INTEGER NOT NULL,not_found INTEGER NOT NULL,retry_at REAL NOT NULL,last_reason TEXT);
    CREATE TABLE IF NOT EXISTS welcome_delivery_attempts(id INTEGER PRIMARY KEY,job_id TEXT NOT NULL,started REAL NOT NULL,finished REAL,outcome TEXT NOT NULL,reason TEXT,http_status INTEGER,online INTEGER,source TEXT);
   ''')
   engine.db.execute("UPDATE welcome_delivery_attempts SET outcome='unknown',reason='restart_during_send',finished=? WHERE outcome='sending'",(time.time(),))

 def record(self,job,status,reason,now,http=None,online=None,source=None,attempt=None):
  with (nullcontext() if self.e.db.in_transaction else self.e.db):
   if attempt is None:
    cur=self.e.db.execute('INSERT INTO welcome_delivery_attempts(job_id,started,finished,outcome,reason,http_status,online,source) VALUES(?,?,?,?,?,?,?,?)',(job,now,None if status=='sending' else now,status,reason,http,online,source));attempt=cur.lastrowid
   else:self.e.db.execute('UPDATE welcome_delivery_attempts SET finished=?,outcome=?,reason=?,http_status=?,online=?,source=? WHERE id=?',(now,status,reason,http,online,source,attempt))
   self.e.db.execute('UPDATE welcome_delivery_state SET last_reason=? WHERE job_id=?',(reason,job))
  if status!='sending':LOG.info('welcome_delivery_result job=%s status=%s reason=%s http_status=%s online=%s source=%s',job,status,reason,http,online,source)
  return attempt

 def cancel_batch(self,job,sid,born,now,reason,record_current=True):
  with self.e.db:
   rows=self.e.db.execute("SELECT id FROM jobs WHERE kind='welcome' AND recipient=? AND ((status='queued' AND created=?) OR (id=? AND status='sending'))",(sid,born,job)).fetchall()
   for (jid,) in rows:
    self.e.db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=?",(now,jid))
    if record_current or jid!=job:self.record(jid,'cancelled',reason,now,online=0 if 'offline' in reason else None)

 def expire(self,now):
  with self.e.db:
   rows=self.e.db.execute("SELECT id FROM jobs WHERE kind='welcome' AND recipient IS NOT NULL AND status='queued' AND created<?",(now-MAX_AGE,)).fetchall()
   for (job,) in rows:
    self.e.db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=?",(now,job));self.record(job,'cancelled','expired',now)

 def live(self):return valid_players(self.e.rcon.request('GET','/v1/players'))
 def online(self,sid,born,now):
  try:
   c=sqlite3.connect(self.cache.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
   row=c.execute("SELECT body,observed FROM source_snapshots WHERE kind='players'").fetchone()
   if row is not None and 0<=now-row[1]<=15:
    ids=valid_players(json.loads(row[0]))
    # An older negative cache cannot refute the newer join that created this job.
    if sid in ids or row[1]>=born:return sid in ids,'cache'
  except (sqlite3.Error,ValueError,TypeError,OSError):pass
  finally:
   if 'c' in locals():c.close()
  return sid in self.live(),'live'

 def defer(self,job,now,delay,reason,http=None,online=None,source=None,attempt=None):
  with self.e.db:self.e.db.execute('UPDATE welcome_delivery_state SET retry_at=? WHERE job_id=?',(now+delay,job))
  self.record(job,'queued',reason,now,http,online,source,attempt)
  return 'queued'

 def backoff(self,job,now,error,source=None,attempt=None,reason='http_429_backoff'):
  deadline=now+error.retry_after
  self.e.next_send=max(self.e.next_send,deadline)
  with self.e.db:
   self.e.state['welcomeRconBackoffUntil']=max(self.e.state.get('welcomeRconBackoffUntil',0),deadline)
   self.e.save_state()
  return self.defer(job,now,error.retry_after,reason,429,None,source,attempt)

 def send(self,job,sid,message,now=None):
  clock=time.time if now is None else lambda:now
  started=clock();born=self.e.db.execute('SELECT created FROM jobs WHERE id=?',(job,)).fetchone()[0]
  with self.e.db:self.e.db.execute('INSERT OR IGNORE INTO welcome_delivery_state VALUES(?,0,0,0,NULL)',(job,))
  if started-born>MAX_AGE:
   self.cancel_batch(job,sid,born,started,'expired');return 'cancelled'
  try:present,source=self.online(sid,born,started)
  except RconError as error:
   if error.status==429:return self.backoff(job,clock(),error,'live')
   return self.defer(job,clock(),15,'roster_unavailable',error.status)
  except Exception:return self.defer(job,clock(),15,'roster_unavailable')
  if not present:
   self.cancel_batch(job,sid,born,clock(),'offline_before_send');return 'cancelled'
  if clock()-born>MAX_AGE:
   self.cancel_batch(job,sid,born,clock(),'expired');return 'cancelled'
  with self.e.db:self.e.db.execute('UPDATE welcome_delivery_state SET posts=posts+1,retry_at=0 WHERE job_id=?',(job,))
  attempt=self.record(job,'sending','post_started',clock(),online=1,source=source)
  try:
   result=self.e.rcon.request('POST','/v1/players/'+sid+'/message',{'message':message})
   if not isinstance(result,dict) or result.get('ok') is not True:
    self.record(job,'unknown','invalid_message_receipt',clock(),online=1,source=source,attempt=attempt);return 'unknown'
   if result.get('pending'):
    self.record(job,'unknown','server_pending_no_confirmation',clock(),online=1,source=source,attempt=attempt);return 'unknown'
   self.record(job,'sent','rcon_accepted',clock(),online=1,source=source,attempt=attempt);return 'sent'
  except RconError as error:
   if error.status==404:
    with self.e.db:
     self.e.db.execute('UPDATE welcome_delivery_state SET not_found=not_found+1 WHERE job_id=?',(job,));misses=self.e.db.execute('SELECT not_found FROM welcome_delivery_state WHERE job_id=?',(job,)).fetchone()[0]
    try:present=sid in self.live()
    except RconError as verification_error:
     if verification_error.status==429:return self.backoff(job,clock(),verification_error,'live',attempt,'http_404_verify_429')
     present=None
    except Exception:present=None
    if present is False:
     self.record(job,'cancelled','http_404_offline',clock(),404,0,'live',attempt)
     self.cancel_batch(job,sid,born,clock(),'offline_after_404',record_current=False);return 'cancelled'
    if misses<2:return self.defer(job,clock(),5 if present else 15,'http_404_online_retry' if present else 'http_404_roster_unavailable',404,present,'live',attempt)
    self.record(job,'failed','http_404_online' if present else 'http_404_unresolved',clock(),404,present,'live',attempt=attempt);return 'failed'
   if error.status==429:return self.backoff(job,clock(),error,source,attempt)
   status='failed' if 400<=error.status<500 else 'unknown'
   self.record(job,status,'http_rejected' if status=='failed' else 'http_outcome_unknown',clock(),error.status,1,source,attempt);return status
  except Exception:
   self.record(job,'unknown','transport_outcome_unknown',clock(),online=1,source=source,attempt=attempt);return 'unknown'

 def snapshot(self):
  with self.e.lock:counts=dict(self.e.db.execute('SELECT outcome,count(*) FROM welcome_delivery_attempts GROUP BY outcome'))
  return {'enabled':self.e.config.get('welcome_delivery_enabled',False),'maxAgeSeconds':MAX_AGE,'max404Retries':1,'clientAcknowledgementAvailable':False,'attemptOutcomes':counts}
