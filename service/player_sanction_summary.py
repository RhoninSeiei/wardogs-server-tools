"""Read-only lower bounds of distinct effective sanction cycles, never row counts."""
import bisect
import json
import math
from pathlib import Path
import sqlite3
import time
try:
 from service.moderation_ops import ContractError,validate_admin_actor
 from service.player_history import identity
except ImportError:
 from moderation_ops import ContractError,validate_admin_actor
 from player_history import identity

RULE='verified_sanction_cycles_v1'
COVERAGE='retained_local_sanction_ledger'
MAX_ROWS=5000
# KickStore.restrict commits these local policy receipts atomically with the
# effective permanent restriction. A later uncertain kick is not a rollback.
COMMITTED_LOCAL_SOURCES={'admin','admin_offline','manual','manual_tk','manual_on_exit','auto_kd','auto_native','auto_burst','auto_weapon','report','manual_advertising_report','manual_native_ban','manual_offline_report','manual_offline_request','manual_permanent_ban'}

def stamp(value,now):return type(value) in (int,float) and math.isfinite(value) and 0<=value<=now

def legacy(source):return any(v in str(source).lower() for v in ('histor','import','legacy','verified_conversation','verified_monitor'))

class PlayerSanctionSummary:
 def __init__(self,engine,path):self.e=engine;self.path=Path(path);self.guard_path=self.path.with_name('kick_guard.sqlite3')
 def query(self,p,now=None):
  clock=time.time if now is None else lambda:now
  if not isinstance(p,dict) or set(p)!={'steamId','actor','requestedAt'}:raise ContractError(400,'invalid_player_sanction_summary_fields')
  validate_admin_actor(p['actor'],p['requestedAt'],self.e.config,clock())
  if not identity(p['steamId']):raise ContractError(400,'invalid_player_sanction_summary_steam_id')
  if self.e.kick_store is None:raise ContractError(503,'player_sanction_summary_not_configured')
  c=None
  try:
   c=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
   c.execute('ATTACH DATABASE ? AS guard',(self.guard_path.resolve().as_uri()+'?mode=ro',));c.execute('PRAGMA query_only=ON');c.row_factory=sqlite3.Row
   deadline=time.monotonic()+2;c.set_progress_handler(lambda:int(time.monotonic()>deadline),2000);c.execute('BEGIN')
   # Establish both retained database snapshots before gathering linked records.
   c.execute('SELECT (SELECT count(*) FROM admin_unbans WHERE steam_id=?),(SELECT count(*) FROM guard.restriction_releases WHERE steam_id=?)',(p['steamId'],p['steamId'])).fetchone()
   observed=clock();sid=p['steamId'];rows={}
   tables=['guard.confirmed_kicks','guard.permanent_ban_actions','guard.restriction_releases','guard.rejoin_restrictions','guard.restriction_provenance','guard.native_ban_sync','guard.ban_statistics_evidence','admin_unbans','admin_restrictions','admin_bans','moderation_operations','moderation_targets']
   for table in tables:
    values=c.execute('SELECT * FROM '+table+' WHERE steam_id=? LIMIT ?',(sid,MAX_ROWS+1)).fetchall()
    if len(values)>MAX_ROWS:raise ContractError(503,'player_sanction_summary_too_large')
    rows[table.split('.')[-1]]=[dict(r) for r in values]
   result=self.count(sid,rows,observed)
  except (sqlite3.Error,OSError,ValueError,TypeError,KeyError,OverflowError):raise ContractError(503,'player_sanction_summary_storage_unavailable') from None
  finally:
   if c is not None:c.close()
  validate_admin_actor(p['actor'],p['requestedAt'],self.e.config,clock())
  if not 0<=clock()-observed<=15:raise ContractError(503,'player_sanction_summary_query_expired')
  return result

 @staticmethod
 def count(sid,rows,now):
  unknown=set();points={};proof_roots=set();untimed=False
  actions={a['id']:a for a in rows['permanent_ban_actions']}
  kicks={a['id']:a for a in rows['confirmed_kicks']}
  root_times={}
  for key,a in actions.items():
   if stamp(a['created_at'],now) and not legacy(a['source']):root_times[key]=a['created_at']
  for key,k in kicks.items():
   if k['scope_id']=='permanent' and k['source']!='rejoin' and not legacy(k['source']) and stamp(k['confirmed_at'],now):
    root_times[key]=k['confirmed_at'];points['kick:'+key]=k['confirmed_at']
   elif k['source']!='rejoin':unknown.add('kick:'+key)
  def decode(value):
   data=json.loads(value)
   if not isinstance(data,dict):raise ValueError('not_object')
   return data
  def snapshot_roots(snapshot,at):
   roots=[];restrictions=snapshot.get('rejoin_restrictions',[]);provenance=snapshot.get('restriction_provenance',[])
   if not isinstance(restrictions,list) or not isinstance(provenance,list):raise ValueError('bad_snapshot')
   for r in restrictions:
    if not isinstance(r,list) or len(r)!=7 or r[0]!=sid:raise ValueError('bad_restriction_snapshot')
    if r[2]!='permanent' or r[6]!='active':continue
    key=r[1]
    if not isinstance(key,str) or not key:raise ValueError('bad_root')
    if key in actions and not stamp(actions[key]['created_at'],at):
     unknown.add('action:'+key);continue
    if key in kicks and kicks[key]['confirmed_at'] is not None and not stamp(kicks[key]['confirmed_at'],at):
     unknown.add('kick:'+key);continue
    when=root_times.get(key)
    for v in provenance:
     if (isinstance(v,list) and len(v)==6 and v[0]=='local' and v[1]==sid and v[2]==key
         and isinstance(v[4],str) and stamp(v[5],at) and not legacy(v[3])):
      key,when=v[4],v[5];break
    if when is not None and when>at:when=None
    proof_roots.add(key)
    if when is not None:root_times.setdefault(key,when)
    roots.append((key,when))
   return roots
  releases={r['id']:r for r in rows['restriction_releases']};release_roots={}
  for key,r in releases.items():
   try:
    if not stamp(r['released_at'],now):raise ValueError('bad_time')
    release_roots[key]=snapshot_roots(decode(r['previous_state']),r['released_at'])
   except (ValueError,TypeError):unknown.add('release:'+key)
  changing=any(v['outcome'] in ('held','unknown') for v in rows['moderation_targets'])
  for r in rows['rejoin_restrictions']:
   if r['status']=='active' and not changing:
    roots=snapshot_roots({'rejoin_restrictions':[[r[k] for k in ('steam_id','event_id','scope_id','name','reason','eligible_at','status')]],'restriction_provenance':[[v[k] for k in ('kind','steam_id','event_id','source','origin_id','effective_at')] for v in rows['restriction_provenance']]},now)
    if roots and any(t is None for _,t in roots) and stamp(r['eligible_at'],now):untimed=True;unknown.add('current:unknown_origin')
   elif r['status'] in ('unknown','dispatching') or changing:unknown.add('current:unresolved')
  for a in rows['admin_restrictions']:
   key='admin-restriction:'+a['id']
   if a['restriction_status']=='restricted' and key in actions and stamp(a['updated'],now):proof_roots.add(key)
   elif a['restriction_status']=='unknown':unknown.add('restriction-request:'+a['id'])
  for a in rows['admin_bans']:
   try:
    receipt=decode(a['receipt'])
    if a['status'] in ('completed','partial'):
     if receipt.get('localRestrictionStatus')=='restricted' and 'admin-ban:'+a['id'] in actions:proof_roots.add('admin-ban:'+a['id'])
     if receipt.get('nativeBanStatus') in ('banned','already_banned'):
      verified=receipt.get('nativeVerifiedAt')
      if not stamp(verified,now) or not stamp(a['created'],now) or not a['created']<=verified<=a['updated']<=now:raise ValueError('unverified_native_receipt')
      if receipt['nativeBanStatus']=='banned':points['native-operation:'+a['id']]=verified
      else:untimed=True;unknown.add('ban-request:'+a['id'])
    if receipt.get('nativeBanStatus')=='unknown' or receipt.get('localRestrictionStatus')=='unknown' or a['status']=='unknown':unknown.add('ban-request:'+a['id'])
   except (ValueError,TypeError):unknown.add('ban-request:'+a['id'])
  for key,a in actions.items():
   if (key in proof_roots or a['source'] in COMMITTED_LOCAL_SOURCES) and key in root_times:points['action:'+key]=root_times[key]
   else:unknown.add('action:'+key)
  # Exact native observations prove at least one cycle, but not its chronology.
  for a in rows['native_ban_sync']:
   if a['status']=='confirmed':untimed=True;unknown.add('native:confirmed_undated')
   elif a['status']=='unknown':unknown.add('native:unknown')
  for a in rows['ban_statistics_evidence']:
   if a['evidence_key']=='native:'+sid and stamp(a['observed_at'],now):untimed=True;unknown.add('native-evidence:'+a['evidence_key'])
  for a in rows['moderation_operations']:
   if a['kick_status']=='unknown':unknown.add('kick-request:'+a['id'])
  full=[];linked_releases=set()
  for u in rows['admin_unbans']:
   key='admin-unban:'+u['id'];r=releases.get(key)
   try:
    if (u['status']!='completed' or u['unban_status']!='unbanned' or u['native_status'] not in ('unbanned','already_unbanned')
        or u['local_status'] not in ('released','already_released','not_restricted')):
     if u['status'] not in ('accepted','resolving'):unknown.add('unban:'+u['id'])
     continue
    if (not r or key not in release_roots or not stamp(u['created'],now) or not stamp(u['released_at'],now)
        or not stamp(u['native_verified_at'],now) or r['released_at']!=u['released_at']
        or not u['created']<=u['released_at']<=u['native_verified_at']<=u['updated']<=now):raise ValueError('unlinked_or_bad_time')
    native_changed=u['native_status']=='unbanned' and u['native_started']==1
    if u['native_status']=='unbanned' and not native_changed:raise ValueError('native_dispatch_unproven')
    roots=release_roots[key] if u['local_status']=='released' else []
    if u['local_status']=='released' and not roots and not native_changed:unknown.add('unban:'+u['id'])
    full.append({'start':u['created'],'end':u['native_verified_at'],'roots':roots,'native':native_changed,'id':u['id']})
    linked_releases.add(key)
   except (ValueError,TypeError):unknown.add('unban:'+u['id'])
  for key in releases:
   if key not in linked_releases:unknown.add('release:'+key)
  # Overlapping verifications cannot establish two independent transitions.
  groups=[]
  for u in sorted(full,key=lambda v:(v['start'],v['end'],v['id'])):
   if groups and u['start']<=groups[-1]['end']:
    groups[-1]['end']=max(groups[-1]['end'],u['end']);groups[-1]['items'].append(u);unknown.add('unban:'+u['id'])
   else:groups.append({'end':u['end'],'items':[u]})
  cuts=[g['end'] for g in groups];unbans=0;used_roots=set();previous_end=None
  for index,g in enumerate(groups):
   changed=False
   for u in g['items']:
    if u['native']:changed=True
    for key,at in u['roots']:
     if key in used_roots:continue
     if previous_end is None or (at is not None and at>previous_end):changed=True
     else:unknown.add('unban:'+u['id'])
     used_roots.add(key)
   if changed:
    unbans+=1
    # Actual release itself proves an effective ban in this closed interval.
    points['closed-cycle:'+str(index)]=g['end']
   previous_end=g['end']
  buckets={bisect.bisect_left(cuts,at) for at in points.values() if stamp(at,now)}
  bans=max(len(buckets),1 if untimed else 0)
  return {'observedAt':now,'steamId':sid,'countingRule':RULE,'banCount':bans,'unbanCount':unbans,
   'countsAreLowerBounds':True,'complete':True,'historyComplete':False,'unknownItems':len(unknown),
   'coverage':COVERAGE,'recordsSince':min(points.values()) if points else None,
   'alerts':{'repeatedBan':bans>1,'repeatedUnban':unbans>1}}
