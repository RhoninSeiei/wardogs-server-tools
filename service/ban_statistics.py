"""Aggregate-only, cumulative unique first-effective-ban statistics."""
import math
import re
import sqlite3
import threading
import time

RULE='unique_first_effective_ban_v1'

class StatisticsUnavailable(Exception):
 def __init__(self,code):self.code=code;super().__init__(code)

def identity(value):
 if not isinstance(value,str) or re.fullmatch('[0-9]{17}',value) is None:raise ValueError('invalid_statistics_identity')
 return value

def timestamp(value):
 if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:raise ValueError('invalid_statistics_time')
 return value

def evidence(store,key,sid,effective,observed):
 identity(sid);timestamp(observed)
 if effective is not None:timestamp(effective)
 store.db.execute('INSERT OR IGNORE INTO ban_statistics_evidence VALUES(?,?,?,?)',(key,sid,effective,observed))

def initialize(store):
 with store.lock,store.db:
  store.db.execute('''CREATE TABLE IF NOT EXISTS ban_statistics_evidence(
   evidence_key TEXT PRIMARY KEY,steam_id TEXT NOT NULL,effective_at REAL,observed_at REAL NOT NULL)''')
  store.db.execute('CREATE INDEX IF NOT EXISTS ban_statistics_identity ON ban_statistics_evidence(steam_id)')
  backfill(store,time.time())

def backfill(store,now):
 # Old non-permanent scopes cannot prove when the later permanent policy started.
 kicks=store.db.execute('SELECT id,steam_id,confirmed_at,recorded_at,scope_id FROM confirmed_kicks').fetchall()
 actions=store.db.execute('SELECT id,steam_id,created_at,source FROM permanent_ban_actions').fetchall()
 linked={row[0] for row in kicks}|{row[0] for row in actions}
 for key,sid,confirmed,recorded,scope in kicks:
  effective=confirmed if scope=='permanent' else None
  evidence(store,'kick:'+key,sid,effective,recorded)
 for key,sid,created,source in actions:
  legacy=any(token in str(source).lower() for token in ('histor','import','legacy'))
  evidence(store,'action:'+key,sid,None if legacy else created,created)
 for sid,event in store.db.execute("SELECT steam_id,event_id FROM rejoin_restrictions WHERE scope_id='permanent'"):
  identity(sid)
  if event not in linked:evidence(store,'restriction:'+sid,sid,None,now)
 for sid,requested,updated in store.db.execute("SELECT steam_id,requested_at,updated_at FROM native_ban_sync WHERE status='confirmed'"):
  # updated_at is never the first effective time. Future bookkeeping only gates existence.
  seen=max(now,timestamp(requested),timestamp(updated))
  evidence(store,'native:'+sid,sid,None,seen)

def snapshot(store,now=None):
 with store.lock,store.db:
  # Include out-of-band legacy writers without splitting the snapshot or clock.
  store.db.execute('BEGIN IMMEDIATE')
  observed=time.time() if now is None else timestamp(now)
  backfill(store,observed)
  row=store.db.execute('''WITH firsts AS (
   SELECT steam_id,CASE WHEN SUM(effective_at IS NULL)>0 THEN NULL ELSE MIN(effective_at) END AS first_at
   FROM ban_statistics_evidence WHERE observed_at<=? AND (effective_at IS NULL OR effective_at<=?)
   GROUP BY steam_id)
   SELECT COUNT(*),COALESCE(SUM(first_at>=?),0),COALESCE(SUM(first_at>=?),0),COALESCE(SUM(first_at IS NULL),0)
   FROM firsts''',(observed,observed,observed-86400,observed-43200)).fetchone()
  return dict(observedAt=observed,countingRule=RULE,totalPlayers=row[0],last24HoursPlayers=row[1],last12HoursPlayers=row[2],unknownTimePlayers=row[3],recordsSince=None,historyComplete=False)

def observe_native(store,bans,now):
 if not isinstance(bans,list):raise ValueError('invalid_native_snapshot')
 ids=[identity(b.get('steamId')) if isinstance(b,dict) else identity(None) for b in bans]
 if len(ids)!=len(set(ids)):raise ValueError('duplicate_native_identity')
 with store.lock,store.db:
  for sid in ids:
   # A first observation of an already-existing ban does not prove its effective time.
   evidence(store,'native:'+sid,sid,None,now)

class BanStatistics:
 def __init__(self,engine):self.engine=engine;self.lock=threading.Lock()
 def get(self):
  with self.lock:
   store=self.engine.kick_store
   if store is None or not self.engine.config.get('rejoin_guard_enabled',False):raise StatisticsUnavailable('ban_statistics_not_configured')
   try:
    result=self.engine.rcon.request('GET','/v1/bans')
    bans=result.get('bans') if isinstance(result,dict) else None
    if result.get('ok') is False or result.get('pending') or not isinstance(bans,list):raise ValueError('invalid_native_snapshot')
    ids=[identity(b.get('steamId')) if isinstance(b,dict) else identity(None) for b in bans]
    if len(ids)!=len(set(ids)):raise ValueError('duplicate_native_identity')
   except Exception:raise StatisticsUnavailable('ban_statistics_native_unavailable') from None
   try:
    store.observe_native_bans(bans,time.time())
    return store.ban_statistics()
   except (sqlite3.Error,ValueError,TypeError,OverflowError):raise StatisticsUnavailable('ban_statistics_storage_unavailable') from None
