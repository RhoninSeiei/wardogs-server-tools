"""Explicit permanent administrator bans; one native dispatch, separate receipts."""
import hashlib
import json
import time
import uuid
try:
    from service.moderation_ops import ContractError,validate_admin,text
    from service.admin_tools import native_bans,local_player,roster
    from service.rcon import reserved_ids,PunishmentExemptError
    from service.current_restrictions import CompleteRead
    from service.kick_guard import native_fingerprint
except ImportError:
    from moderation_ops import ContractError,validate_admin,text
    from admin_tools import native_bans,local_player,roster
    from rcon import reserved_ids,PunishmentExemptError
    from current_restrictions import CompleteRead
    from kick_guard import native_fingerprint

REASON='管理员明确永久封禁'


class AdminBans:
    def __init__(self,engine):
        self.engine,self.db,self.lock=engine,engine.db,engine.lock
        with self.lock,self.db:
            self.db.executescript('''CREATE TABLE IF NOT EXISTS admin_bans(
                id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,steam_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
                receipt TEXT NOT NULL,execution_token TEXT);
                CREATE TABLE IF NOT EXISTS admin_ban_keys(key TEXT PRIMARY KEY,operation_id TEXT NOT NULL,payload_hash TEXT NOT NULL);''')
            self.db.execute("UPDATE admin_bans SET status='accepted',execution_token=NULL WHERE status='resolving'")
            for (opid,) in self.db.execute("SELECT id FROM admin_bans WHERE status='ban_dispatching'").fetchall():
                row=self.row(opid);receipt=json.loads(row['receipt'])
                # No remote replay after a crash at any side-effect boundary.
                receipt.update(nativeBanStatus='unknown',error={'code':'restart_during_ban'})
                receipt['localRestrictionStatus']=self.local_status(opid,row['steam_id'])
                self.finish(opid,receipt,time.time(),force_unknown=True)

    def row(self,opid):
        cur=self.db.execute('SELECT * FROM admin_bans WHERE id=?',(opid,));row=cur.fetchone()
        return dict(zip([c[0] for c in cur.description],row)) if row else None

    def has_request(self,request_id):
        with self.lock:return self.db.execute('SELECT 1 FROM admin_bans WHERE request_id=?',(request_id,)).fetchone() is not None

    def pending_count(self):
        return self.db.execute("SELECT count(*) FROM admin_bans WHERE status IN ('accepted','resolving','ban_dispatching')").fetchone()[0]

    def submit(self,payload,key,now=None):
        now=time.time() if now is None else now
        if not text(key,128):raise ContractError(400,'idempotency_key_required')
        validate_admin(payload,self.engine.config,now,check_age=False)
        if self.engine.kick_store is None or not self.engine.config.get('rejoin_guard_enabled'):raise ContractError(503,'ban_not_configured')
        try:encoded=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        except (ValueError,TypeError):raise ContractError(400,'invalid_json_values') from None
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        with self.lock,self.db:
            self.db.execute('UPDATE admin_bans SET updated=updated WHERE 0')
            if (any(x.has_request(payload['requestId']) for x in (self.engine.admin_history,self.engine.admin_cycle,self.engine.admin_unban))
                    or self.db.execute("SELECT 1 FROM moderation_operations WHERE source='admin' AND report_id=?",(payload['requestId'],)).fetchone()):
                raise ContractError(409,'idempotency_conflict')
            old=self.db.execute('SELECT operation_id,payload_hash FROM admin_ban_keys WHERE key=?',(key,)).fetchone()
            if old is None:old=self.db.execute('SELECT id,payload_hash FROM admin_bans WHERE request_id=?',(payload['requestId'],)).fetchone()
            if old:
                if old[1]!=digest:raise ContractError(409,'idempotency_conflict')
                opid,duplicate=old[0],True
            else:
                validate_admin(payload,self.engine.config,now)
                pending=sum(x.pending_count() for x in (self,self.engine.admin_history,self.engine.admin_cycle,self.engine.admin_unban))
                pending+=self.db.execute("SELECT count(*) FROM moderation_operations WHERE status IN ('accepted','resolving','kick_dispatching')").fetchone()[0]
                if pending>=self.engine.config['moderation_pending_limit']:raise ContractError(429,'moderation_queue_full')
                opid,duplicate='ab_'+uuid.uuid4().hex,False
                receipt={'banStatus':'pending','nativeBanStatus':'not_checked','localRestrictionStatus':'not_attempted',
                    'onlineBefore':None,'removalStatus':'not_checked','nativeVerifiedAt':None,'localVerifiedAt':None,'error':None}
                self.db.execute("INSERT INTO admin_bans VALUES(?,?,?,?,?,'accepted',?,?,?,NULL)",
                    (opid,payload['requestId'],payload['steamId'],digest,encoded,now,now,json.dumps(receipt)))
                self.engine.moderation._audit(opid,'ban_accepted',now)
            self.db.execute('INSERT OR IGNORE INTO admin_ban_keys VALUES(?,?,?)',(key,opid,digest))
        result=self.get(opid);result['deduplicated']=duplicate;return result

    def get(self,opid):
        with self.lock:
            op=self.row(opid)
            if op is None:return None
            p=json.loads(op['payload_json']);receipt=json.loads(op['receipt'])
            return dict(receipt,operationId=opid,source='admin',action='ban',requestId=p['requestId'],steamId=op['steam_id'],
                actor=p['actor'],requestedAt=p['requestedAt'],status=op['status'],createdAt=op['created'],updatedAt=op['updated'],
                kickStatus='not_attempted',announcementStatus='not_queued',announcementJobIds=[],announcementJobs=[])

    def local_status(self,opid,sid):
        guard=self.engine.kick_store
        if guard is None:return 'unknown'
        with guard.lock:
            row=guard.db.execute('SELECT status FROM rejoin_restrictions WHERE steam_id=?',(sid,)).fetchone()
            return 'restricted' if row and row[0]=='active' else ('unknown' if row else 'not_restricted')

    def finish(self,opid,receipt,now,token=None,force_unknown=False):
        native=receipt['nativeBanStatus'];local=receipt['localRestrictionStatus']
        good_native=native in ('banned','already_banned');good_local=local in ('restricted','already_restricted')
        unknown=force_unknown or native=='unknown' or local=='unknown'
        receipt['banStatus']='banned' if good_native and good_local else ('partial' if good_native or good_local else ('unknown' if unknown else 'failed'))
        status='completed' if receipt['banStatus']=='banned' else receipt['banStatus']
        with self.lock,self.db:
            op=self.row(opid)
            if token is not None and (op is None or op['execution_token']!=token):return
            self.db.execute('UPDATE admin_bans SET status=?,receipt=?,updated=? WHERE id=?',(status,json.dumps(receipt),now,opid))
            self.engine.moderation._audit(opid,'ban_'+status,now,receipt['error']['code'] if receipt['error'] else None)
            if unknown:self.db.execute("UPDATE moderation_targets SET outcome='unknown',expires=NULL WHERE operation_id=?",(opid,))
            elif good_local or good_native:self.db.execute("UPDATE moderation_targets SET outcome='confirmed',expires=? WHERE operation_id=?",(now+self.engine.config['moderation_target_cooldown_seconds'],opid))
            else:self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?',(opid,))

    def process(self,opid,now=None):
        clock=time.time if now is None else lambda:now
        with self.lock,self.db:
            token=uuid.uuid4().hex
            if not self.db.execute("UPDATE admin_bans SET status='resolving',execution_token=?,updated=? WHERE id=? AND status='accepted'",(token,clock(),opid)).rowcount:return self.get(opid)
            op=self.row(opid);p=json.loads(op['payload_json']);sid=op['steam_id'];receipt=json.loads(op['receipt'])
            self.db.execute("DELETE FROM moderation_targets WHERE steam_id=? AND outcome='confirmed' AND expires<=?",(sid,clock()))
            self.db.execute("INSERT OR IGNORE INTO moderation_targets VALUES(?,?,'held',NULL)",(sid,opid))
            if self.db.execute('SELECT operation_id FROM moderation_targets WHERE steam_id=?',(sid,)).fetchone()[0]!=opid:
                receipt['error']={'code':'target_already_coordinated'};self.finish(opid,receipt,clock(),token);return self.get(opid)
        started=False;native_started=False
        try:
            validate_admin(p,self.engine.config,clock());guard=self.engine.kick_store
            if guard is None or not self.engine.config.get('rejoin_guard_enabled'):raise ContractError(503,'ban_not_configured')
            begin=clock();guard.require_not_exempt(sid)
            reader=CompleteRead(self.engine);players=roster(reader)
            online=sid in players;receipt['onlineBefore']=online
            bans=native_bans(reader);known=local_player(self.engine,sid)
            if known is None and not online and sid not in bans:raise ContractError(422,'player_not_known')
            reserved=reserved_ids(self.engine.rcon.request('GET','/v1/reserved-slots'));guard.sync_reserved(reserved,clock())
            if not 0<=clock()-begin<=15:raise ContractError(503,'ban_preflight_stale')
            with self.lock,guard.lock:
                validate_admin(p,self.engine.config,clock());guard.require_not_exempt(sid)
                if self.db.execute('SELECT operation_id,outcome FROM moderation_targets WHERE steam_id=?',(sid,)).fetchone()!=(opid,'held'):raise ContractError(409,'target_already_coordinated')
                current=guard.db.execute('SELECT status FROM rejoin_restrictions WHERE steam_id=?',(sid,)).fetchone();sync=guard.native_ban_state(sid)
                if (current and current[0] in ('dispatching','unknown')) or (sync is not None and sync!='confirmed'):raise ContractError(409,'punishment_outcome_unresolved')
                with self.db:self.db.execute("UPDATE admin_bans SET status='ban_dispatching',updated=? WHERE id=?",(clock(),opid))
                started=True
                if current:receipt['localRestrictionStatus']='already_restricted'
                else:
                    name=known['name'] if known else sid
                    guard.restrict(sid,name,REASON,'admin',clock(),action_id='admin-ban:'+opid)
                    receipt['localRestrictionStatus']='restricted'
                receipt['localVerifiedAt']=clock()
                with self.db:self.db.execute('UPDATE admin_bans SET receipt=? WHERE id=?',(json.dumps(receipt),opid))
            failure=response=None
            if sid not in bans:
                # Rcon.request performs a second fresh reserved preflight. No kick fallback.
                validate_admin(p,self.engine.config,clock())
                with self.lock,guard.lock:
                    guard.require_not_exempt(sid)
                    if self.db.execute('SELECT operation_id,outcome FROM moderation_targets WHERE steam_id=?',(sid,)).fetchone()!=(opid,'held'):raise ContractError(409,'target_already_coordinated')
                    native_started=True
                    try:response=self.engine.rcon.request('POST','/v1/bans',{'steamId':sid,'reason':REASON})
                    except Exception as error:failure=error
            after=native_bans(reader);receipt['nativeVerifiedAt']=clock()
            if sid in after:
                receipt['nativeBanStatus']='already_banned' if sid in bans else 'banned'
                if sid not in bans and failure is None and isinstance(response,dict) and response.get('ok') is True and not response.get('pending'):
                    fingerprint=native_fingerprint(after[sid])
                    if fingerprint:
                        with guard.lock,guard.db:
                            guard.db.execute("INSERT OR REPLACE INTO restriction_provenance VALUES('native',?,?,?,?,?)",(sid,fingerprint,'admin','admin-ban:'+opid,receipt['localVerifiedAt']))
            else:
                status=getattr(failure,'status',None)
                receipt['nativeBanStatus']='failed' if type(status) is int and 400<=status<500 else 'unknown'
                receipt['error']={'code':'native_ban_rejected' if receipt['nativeBanStatus']=='failed' else 'native_ban_outcome_unknown'}
            if online and receipt['nativeBanStatus'] in ('banned','already_banned'):
                values=roster(reader)
                receipt['removalStatus']='confirmed' if sid not in values else 'still_online'
            elif not online:receipt['removalStatus']='not_needed'
        except Exception as error:
            receipt['error']={'code':getattr(error,'code','ban_execution_unavailable')}
            if started:
                try:receipt['localRestrictionStatus']=self.local_status(opid,sid)
                except Exception:receipt['localRestrictionStatus']='unknown'
                if native_started and receipt['nativeBanStatus'] not in ('banned','already_banned'):receipt['nativeBanStatus']='unknown'
                if receipt['onlineBefore'] is True and receipt['removalStatus']=='not_checked':receipt['removalStatus']='unknown'
            elif isinstance(error,PunishmentExemptError):receipt['localRestrictionStatus']='exempt'
        self.finish(opid,receipt,clock(),token,force_unknown=started and (receipt['nativeBanStatus'] in ('not_checked','unknown') or receipt['localRestrictionStatus']=='unknown'))
        return self.get(opid)

    def process_one(self,now=None):
        with self.lock:row=self.db.execute("SELECT id FROM admin_bans WHERE status='accepted' ORDER BY created,rowid LIMIT 1").fetchone()
        if row is None:return False
        self.process(row[0],now);return True
