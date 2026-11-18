"""Durable administrator unbans: local release receipt and one native DELETE."""
import hashlib,json,time,uuid
try:
    from service.moderation_ops import ContractError,validate_admin,text
    from service.admin_tools import native_bans,local_player
except ImportError:
    from moderation_ops import ContractError,validate_admin,text
    from admin_tools import native_bans,local_player

try:
    from service.restart_unban import scheduled as restart_scheduled, validate as validate_restart
except ImportError:
    from restart_unban import scheduled as restart_scheduled, validate as validate_restart

GOOD_LOCAL={'released','already_released','not_restricted'}
GOOD_NATIVE={'unbanned','already_unbanned'}

class AdminUnban:
    def __init__(self,engine):
        self.engine,self.db,self.lock=engine,engine.db,engine.lock
        with self.lock,self.db:
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS admin_unbans(
                id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,steam_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,status TEXT NOT NULL,unban_status TEXT NOT NULL,
                native_status TEXT NOT NULL,local_status TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
                error_code TEXT,name TEXT,execution_token TEXT,related_id TEXT,local_plan TEXT,
                native_started INTEGER NOT NULL DEFAULT 0,native_verified_at REAL,released_at REAL);
              CREATE TABLE IF NOT EXISTS admin_unban_keys(key TEXT PRIMARY KEY,operation_id TEXT NOT NULL,payload_hash TEXT NOT NULL);
            ''')
            rows=self.db.execute("SELECT id FROM admin_unbans WHERE status='resolving'").fetchall()
            for (opid,) in rows:
                with self.engine.unknown_handoff.release_lock(opid) as locked:
                    if not locked:continue
                    self.db.execute("UPDATE admin_unbans SET status='accepted',execution_token=NULL WHERE id=?",(opid,))
                    if self.engine.unknown_handoff.record(opid) is None:
                        self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?',(opid,))
            rows=self.db.execute("SELECT id FROM admin_unbans WHERE status='unban_dispatching'").fetchall()
            for (opid,) in rows:self.db.execute('UPDATE admin_unbans SET execution_token=NULL WHERE id=?',(opid,))
        for (opid,) in rows:
            op=self.row(opid)
            try:receipt=self.receipt(opid,op['steam_id'])
            except Exception:receipt=None
            local=op['local_plan'] if receipt is not None else 'unknown'
            native=op['native_status'] if op['native_status'] in GOOD_NATIVE else ('unknown' if op['native_started'] else 'not_attempted')
            self.finish(opid,native,local,time.time(),'restart_during_unban' if native not in GOOD_NATIVE or receipt is None else None,
                        released=receipt,verified=op['native_verified_at'])

    def row(self,opid):
        cur=self.db.execute('SELECT * FROM admin_unbans WHERE id=?',(opid,));row=cur.fetchone()
        return dict(zip([d[0] for d in cur.description],row)) if row else None

    def has_request(self,request):
        with self.lock:return bool(self.db.execute('SELECT 1 FROM admin_unbans WHERE request_id=?',(request,)).fetchone())

    def pending_count(self):
        return self.db.execute("SELECT count(*) FROM admin_unbans WHERE status IN ('accepted','resolving','unban_dispatching')").fetchone()[0]

    def submit(self,payload,key,now=None):
        now=time.time() if now is None else now
        if not text(key,128):raise ContractError(400,'idempotency_key_required')
        try:encoded=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        except (ValueError,TypeError):raise ContractError(400,'invalid_json_values') from None
        validate_admin(payload,self.engine.config,now,check_age=False)
        if self.engine.kick_store is None:raise ContractError(503,'unban_not_configured')
        if getattr(self.engine,'admin_bans',None) is not None and self.engine.admin_bans.has_request(payload['requestId']):raise ContractError(409,'idempotency_conflict')
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        with self.lock,self.db:
            self.db.execute('UPDATE admin_unbans SET updated=updated WHERE 0')
            if getattr(self.engine,'admin_bans',None) is not None and self.engine.admin_bans.has_request(payload['requestId']):raise ContractError(409,'idempotency_conflict')
            if (self.engine.admin_history.has_request(payload['requestId']) or self.engine.admin_cycle.has_request(payload['requestId'])
                    or self.db.execute("SELECT 1 FROM moderation_operations WHERE source='admin' AND report_id=?",(payload['requestId'],)).fetchone()):raise ContractError(409,'idempotency_conflict')
            old=self.db.execute('SELECT operation_id,payload_hash FROM admin_unban_keys WHERE key=?',(key,)).fetchone()
            if old is None:old=self.db.execute('SELECT id,payload_hash FROM admin_unbans WHERE request_id=?',(payload['requestId'],)).fetchone()
            if old:
                if old[1]!=digest:raise ContractError(409,'idempotency_conflict')
                opid,duplicate=old[0],True;self.db.execute('INSERT OR IGNORE INTO admin_unban_keys VALUES(?,?,?)',(key,opid,digest))
            else:
                validate_admin(payload,self.engine.config,now)
                count=self.pending_count()+self.engine.admin_history.pending_count()+self.engine.admin_cycle.pending_count()
                count+=self.db.execute("SELECT count(*) FROM moderation_operations WHERE status IN ('accepted','resolving','kick_dispatching')").fetchone()[0]
                if getattr(self.engine,'admin_bans',None) is not None:count+=self.engine.admin_bans.pending_count()
                if count>=self.engine.config['moderation_pending_limit']:raise ContractError(429,'moderation_queue_full')
                opid,duplicate='au_'+uuid.uuid4().hex,False
                self.db.execute('''INSERT INTO admin_unbans(id,request_id,steam_id,payload_hash,payload_json,status,unban_status,native_status,local_status,created,updated)
                    VALUES(?,?,?,?,?,'accepted','pending','not_checked','not_attempted',?,?)''',(opid,payload['requestId'],payload['steamId'],digest,encoded,now,now))
                self.db.execute('INSERT INTO admin_unban_keys VALUES(?,?,?)',(key,opid,digest));self.engine.moderation._audit(opid,'unban_accepted',now)
                if not restart_scheduled(payload):self.engine.unknown_handoff.transfer(opid,payload['steamId'],now)
        result=self.get(opid);result['deduplicated']=duplicate;return result

    def get(self,opid):
        with self.lock:
            op=self.row(opid)
            if op is None:return None
            p=json.loads(op['payload_json'])
            return {'unknownHandoff':self.engine.unknown_handoff.public(opid),'operationId':opid,'source':'admin','action':'unban','requestId':p['requestId'],'steamId':op['steam_id'],
                    'actor':p['actor'],'requestedAt':p['requestedAt'],'name':op['name'],'status':op['status'],'unbanStatus':op['unban_status'],
                    'nativeUnbanStatus':op['native_status'],'localReleaseStatus':op['local_status'],
                    'nativeVerifiedAt':op['native_verified_at'],'releasedAt':op['released_at'],
                    'createdAt':op['created'],'updatedAt':op['updated'],'relatedOperationId':op['related_id'],
                    'kickStatus':'not_needed','announcementStatus':'not_queued','announcementJobIds':[],'announcementJobs':[],
                    'error':{'code':op['error_code']} if op['error_code'] else None}

    def owns(self,opid,token):
        row=self.db.execute('SELECT execution_token FROM admin_unbans WHERE id=?',(opid,)).fetchone()
        return row is not None and row[0]==token

    def receipt(self,opid,sid):
        guard=self.engine.kick_store
        with guard.lock:
            row=guard.db.execute('SELECT steam_id,released_at FROM restriction_releases WHERE id=?',('admin-unban:'+opid,)).fetchone()
        return row[1] if row is not None and row[0]==sid else None

    def finish(self,opid,native,local,now,code=None,released=None,verified=None,token=None,status=None):
        if native in GOOD_NATIVE and local in GOOD_LOCAL:outcome='unbanned'
        elif local in GOOD_LOCAL or native in GOOD_NATIVE:outcome='partial'
        elif native=='unknown' or local=='unknown':outcome='unknown'
        else:outcome='failed'
        status=status or ('completed' if outcome=='unbanned' else outcome)
        hold=native=='unknown' or local=='unknown'
        with self.lock,self.db:
            if token is not None and not self.owns(opid,token):return
            before=self.row(opid)
            if before['status'] in ('accepted','resolving') and self.engine.unknown_handoff.record(opid) is not None:
                self.engine.unknown_handoff.restore(opid,now)
            self.db.execute('UPDATE admin_unbans SET status=?,unban_status=?,native_status=?,local_status=?,updated=?,error_code=?,released_at=?,native_verified_at=? WHERE id=?',
                            (status,outcome,native,local,now,code,released,verified,opid))
            self.engine.moderation._audit(opid,'unban_'+status,now,code)
            if hold:self.db.execute("UPDATE moderation_targets SET outcome='unknown',expires=NULL WHERE operation_id=?",(opid,))
            elif outcome=='unbanned':self.db.execute("UPDATE moderation_targets SET outcome='confirmed',expires=? WHERE operation_id=?",(now+self.engine.config['moderation_target_cooldown_seconds'],opid))
            else:self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?',(opid,))

    def process(self,opid,now=None):
        with self.lock:
            op=self.row(opid)
            if op is None or not restart_scheduled(json.loads(op['payload_json'])):
                return self._process(opid,now)
            # Scheduled repairs never adopt old unknown leases. Cross-process
            # exclusion spans both validation points and the one DELETE.
            with self.engine.unknown_handoff.target_lock(op['steam_id']) as locked:
                if not locked:return self.get(opid)
                with self.engine.kick_store.lock:return self._process(opid,now)

    def _process(self,opid,now=None):
        clock=time.time if now is None else lambda:now
        with self.lock,self.db:
            token=uuid.uuid4().hex
            if not self.db.execute("UPDATE admin_unbans SET status='resolving',execution_token=?,updated=? WHERE id=? AND status='accepted'",(token,clock(),opid)).rowcount:return self.get(opid)
            op=self.row(opid);p=json.loads(op['payload_json']);sid=op['steam_id']
            self.db.execute("DELETE FROM moderation_targets WHERE steam_id=? AND outcome='confirmed' AND expires<=?",(sid,clock()))
            self.db.execute("INSERT OR IGNORE INTO moderation_targets VALUES(?,?,'held',NULL)",(sid,opid))
            owner=self.db.execute('SELECT operation_id FROM moderation_targets WHERE steam_id=?',(sid,)).fetchone()[0]
            if owner!=opid:
                self.db.execute('UPDATE admin_unbans SET related_id=? WHERE id=?',(owner,opid))
                lease=self.db.execute('SELECT outcome FROM moderation_targets WHERE steam_id=?',(sid,)).fetchone()
                code='unknown_punishment_not_reconcilable' if lease and lease[0]=='unknown' else 'target_already_coordinated'
                self.finish(opid,'not_checked','not_attempted',clock(),code,token=token,status='superseded');return self.get(opid)
        started=False;local='not_attempted';native='not_checked';released=verified=None
        try:
            validate_admin(p,self.engine.config,clock())
            bans=native_bans(self.engine);known=local_player(self.engine,sid);guard=self.engine.kick_store
            with self.lock,guard.lock,self.engine.unknown_handoff.release_lock(opid) as target_locked:
                if not self.owns(opid,token):return self.get(opid)
                if not target_locked:raise ContractError(409,'unknown_handoff_revalidation_failed')
                validate_admin(p,self.engine.config,clock())
                current=guard.db.execute('SELECT name,status FROM rejoin_restrictions WHERE steam_id=?',(sid,)).fetchone()
                sync=guard.native_ban_state(sid)
                if known is None and sid not in bans and current is None and sync is None:raise ContractError(422,'player_not_known')
                if (current and current[1]=='dispatching') or sync=='dispatching':raise ContractError(409,'punishment_dispatch_in_progress')
                lease=self.db.execute('SELECT operation_id,outcome FROM moderation_targets WHERE steam_id=?',(sid,)).fetchone()
                if lease!=(opid,'held'):raise ContractError(409,'target_already_coordinated')
                validate_restart(self.engine,p,opid,clock())
                oldrelease=guard.released_at(sid)
                plan='released' if current or sync is not None else ('already_released' if oldrelease is not None else 'not_restricted')
                name=known['name'] if known else (current[0] if current else sid)
                with self.db:
                    # Fence recovery in another engine between readback and dispatch.
                    self.db.execute('UPDATE admin_unbans SET updated=updated WHERE 0')
                    if not self.owns(opid,token):return self.get(opid)
                    valid=self.engine.unknown_handoff.revalidate(opid,clock())
                    if valid:
                        if sid not in bans:native='already_unbanned';verified=clock()
                        self.db.execute("UPDATE admin_unbans SET status='unban_dispatching',local_plan=?,name=?,native_status=?,native_verified_at=?,updated=? WHERE id=?",(plan,name,native,verified,clock(),opid));self.engine.moderation._audit(opid,'unban_dispatching',clock())
                if not valid:raise ContractError(409,'unknown_handoff_revalidation_failed')
                started=True
                guard.release(sid,'administrator unban '+opid,clock(),action_id='admin-unban:'+opid)
                released=self.receipt(opid,sid);local=plan
                with self.db:self.db.execute('UPDATE admin_unbans SET local_status=?,released_at=?,updated=? WHERE id=?',(local,released,clock(),opid))
            # Old local enforcement and native-sync requests are now removed.
            # The shared target lease remains held throughout remote reconciliation.
            failure=None
            if sid in bans:
                with self.lock,self.db:
                    if not self.owns(opid,token):return self.get(opid)
                    validate_admin(p,self.engine.config,clock())
                    lease=self.db.execute('SELECT operation_id,outcome FROM moderation_targets WHERE steam_id=?',(sid,)).fetchone()
                    if lease!=(opid,'held'):raise ContractError(409,'target_already_coordinated')
                    validate_restart(self.engine,p,opid,clock())
                    self.db.execute('UPDATE admin_unbans SET native_started=1 WHERE id=?',(opid,))
                try:
                    response=self.engine.rcon.request('DELETE','/v1/bans/'+sid)
                    if not isinstance(response,dict) or response.get('ok') is not True or response.get('pending'):raise ValueError('unban_not_confirmed')
                except Exception as error:failure=error
            try:
                after=native_bans(self.engine);verified=clock()
                if sid not in after:native='unbanned' if sid in bans else 'already_unbanned'
                else:
                    native='failed' if failure is None or (isinstance(getattr(failure,'status',None),int) and 400<=failure.status<500) else 'unknown'
            except Exception:native='unknown'
            with self.lock,self.db:
                if not self.owns(opid,token):return self.get(opid)
                self.db.execute('UPDATE admin_unbans SET native_status=?,native_verified_at=? WHERE id=?',(native,verified,opid))
            self.finish(opid,native,local,clock(),None if native in GOOD_NATIVE else 'native_unban_'+native,released,verified,token)
        except ContractError as error:
            self.finish(opid,native if native in GOOD_NATIVE else 'not_attempted',local,clock(),error.code,released,verified,token,status='partial' if local in GOOD_LOCAL or native in GOOD_NATIVE else 'rejected')
        except Exception:
            if started:
                try:
                    receipt=self.receipt(opid,sid)
                    if receipt is not None:released=receipt;local=self.row(opid)['local_plan']
                    else:local='unknown'
                except Exception:local='unknown'
            self.finish(opid,native if native in GOOD_NATIVE else ('unknown' if started else 'not_checked'),local,clock(),'unban_outcome_unknown' if started else 'lookup_or_validation_failed',released,verified,token)
        return self.get(opid)

    def process_one(self,now=None):
        with self.lock:row=self.db.execute("SELECT id FROM admin_unbans WHERE status='accepted' ORDER BY created,rowid LIMIT 1").fetchone()
        if row is None:return False
        self.process(row[0],now);return True
