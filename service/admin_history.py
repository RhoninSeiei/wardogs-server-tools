"""Administrator-only historical search and explicit offline rejoin restrictions."""
import hashlib
import json
import math
import time
import uuid
try:
    from service.moderation_ops import ContractError,validate_admin,validate_admin_actor,text
    from service.player_history import identity,HistoryUnavailable
    from service.rcon import reserved_ids,PunishmentExemptError
    from service.announcements import validate_messages
except ImportError:
    from moderation_ops import ContractError,validate_admin,validate_admin_actor,text
    from player_history import identity,HistoryUnavailable
    from rcon import reserved_ids,PunishmentExemptError
    from announcements import validate_messages

REASON='管理员离线永久重入限制'


def notices(sid,name):
    label=''.join(c for c in name if c.isprintable())[:60] or sid
    return validate_messages([
        f'管理通告：玩家{label}已登记永久重入限制，后续进入将自动踢出。处理时不在线，本次未实际踢出。',
        f'[EN] {label} is now on the permanent rejoin restriction list. Offline when handled; no kick was performed this time.',
        f'[JA] {label}を無期限の再参加制限に登録しました。対応時はオフラインのため、今回は退出操作を実行していません。',
    ])


class AdminHistory:
    def __init__(self,engine):
        self.engine=engine;self.db=engine.db;self.lock=engine.lock
        with self.lock,self.db:
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS admin_restrictions(
                id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,steam_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,status TEXT NOT NULL,restriction_status TEXT NOT NULL,
                created REAL NOT NULL,updated REAL NOT NULL,error_code TEXT,name TEXT,execution_token TEXT,related_id TEXT);
              CREATE TABLE IF NOT EXISTS admin_restriction_keys(key TEXT PRIMARY KEY,operation_id TEXT NOT NULL,payload_hash TEXT NOT NULL);
            ''')
            resolving=self.db.execute("SELECT id FROM admin_restrictions WHERE status='resolving'").fetchall()
            for (opid,) in resolving:
                self.db.execute("UPDATE admin_restrictions SET status='accepted',execution_token=NULL WHERE id=?",(opid,))
                self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?',(opid,))
            dispatching=self.db.execute("SELECT id,steam_id FROM admin_restrictions WHERE status='restriction_dispatching'").fetchall()
        for opid,sid in dispatching:
            try:receipt=self.receipt(opid,sid)
            except Exception:
                self.end(opid,'unknown','unknown','restriction_receipt_unavailable',time.time(),hold=True);continue
            if receipt:self.finalize(opid,time.time())
            else:self.end(opid,'unknown','unknown','restart_without_restriction_receipt',time.time(),hold=True)

    def pending_count(self):
        return self.db.execute("SELECT count(*) FROM admin_restrictions WHERE status IN ('accepted','resolving','restriction_dispatching')").fetchone()[0]

    def has_request(self,request_id):
        with self.lock:return bool(self.db.execute('SELECT 1 FROM admin_restrictions WHERE request_id=?',(request_id,)).fetchone())

    def search(self,payload,now=None):
        clock=time.time if now is None else lambda:now
        if not isinstance(payload,dict) or set(payload)!={'query','actor','requestedAt'}:raise ContractError(400,'invalid_player_search_fields')
        query=payload['query']
        if not text(query,128) or query!=query.strip():raise ContractError(400,'invalid_player_query')
        validate_admin_actor(payload['actor'],payload['requestedAt'],self.engine.config,clock())
        result=self.engine.player_history.search(query,clock())
        validate_admin_actor(payload['actor'],payload['requestedAt'],self.engine.config,clock())
        with self.lock,self.db:
            self.db.execute('INSERT INTO admin_search_audit(actor,requested,queried,query_hash,result_count) VALUES(?,?,?,?,?)',
                            (json.dumps(payload['actor'],sort_keys=True),payload['requestedAt'],clock(),hashlib.sha256(query.encode()).hexdigest(),len(result['players'])))
        return result

    def submit(self,payload,key,now=None):
        now=time.time() if now is None else now
        if not text(key,128):raise ContractError(400,'idempotency_key_required')
        try:encoded=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        except (ValueError,TypeError):raise ContractError(400,'invalid_json_values') from None
        validate_admin(payload,self.engine.config,now,check_age=False)
        if self.engine.kick_store is None or not self.engine.config.get('rejoin_guard_enabled'):raise ContractError(503,'restriction_not_configured')
        if getattr(self.engine,'admin_bans',None) is not None and self.engine.admin_bans.has_request(payload['requestId']):raise ContractError(409,'idempotency_conflict')
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        with self.lock,self.db:
            self.db.execute('UPDATE admin_restrictions SET updated=updated WHERE 0')
            if getattr(self.engine,'admin_bans',None) is not None and self.engine.admin_bans.has_request(payload['requestId']):raise ContractError(409,'idempotency_conflict')
            if self.db.execute("SELECT 1 FROM moderation_operations WHERE source='admin' AND report_id=?",(payload['requestId'],)).fetchone():raise ContractError(409,'idempotency_conflict')
            if getattr(self.engine,'admin_cycle',None) is not None and self.engine.admin_cycle.has_request(payload['requestId']):raise ContractError(409,'idempotency_conflict')
            if getattr(self.engine,'admin_unban',None) is not None and self.engine.admin_unban.has_request(payload['requestId']):raise ContractError(409,'idempotency_conflict')
            old=self.db.execute('SELECT operation_id,payload_hash FROM admin_restriction_keys WHERE key=?',(key,)).fetchone()
            if old is None:old=self.db.execute('SELECT id,payload_hash FROM admin_restrictions WHERE request_id=?',(payload['requestId'],)).fetchone()
            if old:
                if old[1]!=digest:raise ContractError(409,'idempotency_conflict')
                self.db.execute('INSERT OR IGNORE INTO admin_restriction_keys VALUES(?,?,?)',(key,old[0],digest));opid=old[0];duplicate=True
            else:
                validate_admin(payload,self.engine.config,now)
                pending=self.pending_count()+self.db.execute("SELECT count(*) FROM moderation_operations WHERE status IN ('accepted','resolving','kick_dispatching')").fetchone()[0]
                if getattr(self.engine,'admin_cycle',None) is not None:pending+=self.engine.admin_cycle.pending_count()
                if getattr(self.engine,'admin_unban',None) is not None:pending+=self.engine.admin_unban.pending_count()
                if getattr(self.engine,'admin_bans',None) is not None:pending+=self.engine.admin_bans.pending_count()
                if pending>=self.engine.config['moderation_pending_limit']:raise ContractError(429,'moderation_queue_full')
                opid='ar_'+uuid.uuid4().hex;duplicate=False
                self.db.execute("INSERT INTO admin_restrictions VALUES(?,?,?,?,?,'accepted','pending',?,?,NULL,NULL,NULL,NULL)",
                                (opid,payload['requestId'],payload['steamId'],digest,encoded,now,now))
                self.db.execute('INSERT INTO admin_restriction_keys VALUES(?,?,?)',(key,opid,digest))
                self.engine.moderation._audit(opid,'restriction_accepted',now)
        result=self.get(opid);result['deduplicated']=duplicate;return result

    def get(self,opid):
        with self.lock:
            cursor=self.db.execute('SELECT * FROM admin_restrictions WHERE id=?',(opid,));row=cursor.fetchone()
            if row is None:return None
            op=dict(zip([d[0] for d in cursor.description],row));payload=json.loads(op['payload_json'])
            jobs=self.db.execute('''SELECT m.language,j.id,j.status,j.updated FROM moderation_operation_jobs m
                JOIN jobs j ON j.id=m.job_id WHERE m.operation_id=? ORDER BY CASE m.language WHEN 'zh' THEN 0 WHEN 'en' THEN 1 ELSE 2 END''',(opid,)).fetchall()
            jobs, suppressed = self.engine.announcement_policy.split(jobs)
            statuses=[j[2] for j in jobs]
            announcement='not_queued'
            if statuses:
                announcement='sent' if all(s=='sent' for s in statuses) else ('pending' if any(s in ('queued','sending') for s in statuses) else ('unknown' if 'unknown' in statuses else 'failed'))
            status=op['status']
            if status=='announcements_pending' and announcement!='pending':status='completed' if announcement=='sent' else ('announcement_unknown' if announcement=='unknown' else 'announcement_failed')
            return {'operationId':opid,'requestId':payload['requestId'],'steamId':op['steam_id'],'actor':payload['actor'],'requestedAt':payload['requestedAt'],
                    'source':'admin','action':'restriction','name':op['name'],'status':status,'restrictionStatus':op['restriction_status'],
                    'kickStatus':'not_needed','nativeBanStatus':'not_requested','announcementStatus':announcement,
                    'announcementSuppressedJobs':suppressed,'announcementJobIds':[j[1] for j in jobs],'announcementJobs':[{'language':j[0],'jobId':j[1],'status':j[2]} for j in jobs],
                    'createdAt':op['created'],'updatedAt':max([op['updated']]+[j[3] for j in jobs]),
                    'error':{'code':op['error_code']} if op['error_code'] else None,'relatedOperationId':op['related_id'],
                    'priorAutomaticKick':self.prior_automatic_kick(op['steam_id'],op['created'])}

    def prior_automatic_kick(self,sid,created):
        # Legacy announcements_pending is written by Coordinator.process at the
        # actual kick confirmation boundary, using finished, before delivery.
        # Neither operation.updated nor announcement job times prove a kick.
        row=self.db.execute('''SELECT o.id,MIN(a.timestamp) AS confirmed
            FROM moderation_operations o JOIN moderation_audit a ON a.operation_id=o.id
            WHERE o.steam_id=? AND o.source='auto_kd' AND o.kick_status='confirmed'
              AND a.phase IN ('kick_confirmed','announcements_pending')
              AND a.timestamp>=o.created AND a.timestamp>=0
            GROUP BY o.id HAVING confirmed<=?
            ORDER BY confirmed DESC,o.id LIMIT 1''',(sid,created)).fetchone()
        if row is None or not math.isfinite(row[1]):return None
        return {'operationId':row[0],'steamId':sid,'source':'auto_kd',
                'kickStatus':'confirmed','confirmedAt':row[1]}

    def receipt(self,opid,sid):
        guard=self.engine.kick_store
        if guard is None:raise RuntimeError('restriction_store_unavailable')
        with guard.lock:
            row=guard.db.execute('SELECT steam_id,source FROM permanent_ban_actions WHERE id=?',('admin-restriction:'+opid,)).fetchone()
        return row is not None and row==(sid,'admin_offline')

    def owns(self,opid,token):
        row=self.db.execute('SELECT execution_token FROM admin_restrictions WHERE id=?',(opid,)).fetchone()
        return row is not None and row[0]==token

    def end(self,opid,status,restriction,code,now,hold=False,token=None):
        with self.lock,self.db:
            if token is not None and not self.owns(opid,token):return
            self.db.execute('UPDATE admin_restrictions SET status=?,restriction_status=?,error_code=?,updated=? WHERE id=?',(status,restriction,code,now,opid))
            self.engine.moderation._audit(opid,status,now,code)
            if hold:self.db.execute("UPDATE moderation_targets SET outcome='unknown',expires=NULL WHERE operation_id=?",(opid,))
            else:self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?',(opid,))

    def finalize(self,opid,now,token=None):
        with self.lock,self.db:
            if token is not None and not self.owns(opid,token):return
            row=self.db.execute('SELECT steam_id,name FROM admin_restrictions WHERE id=?',(opid,)).fetchone()
            if not self.db.execute('SELECT 1 FROM moderation_operation_jobs WHERE operation_id=?',(opid,)).fetchone():
                self.engine.moderation._notices(opid,notices(row[0],row[1] or row[0]),now)
            self.db.execute("UPDATE admin_restrictions SET status='announcements_pending',restriction_status='restricted',error_code=NULL,updated=? WHERE id=?",(now,opid))
            self.engine.moderation._audit(opid,'restriction_registered',now)
            self.db.execute("UPDATE moderation_targets SET outcome='confirmed',expires=? WHERE operation_id=?",(now+self.engine.config['moderation_target_cooldown_seconds'],opid))

    def process(self,opid,now=None):
        clock=time.time if now is None else lambda:now
        with self.lock,self.db:
            token=uuid.uuid4().hex
            changed=self.db.execute("UPDATE admin_restrictions SET status='resolving',execution_token=?,updated=? WHERE id=? AND status='accepted'",(token,clock(),opid)).rowcount
            if not changed:return self.get(opid)
            row=self.db.execute('SELECT payload_json FROM admin_restrictions WHERE id=?',(opid,)).fetchone()
            payload=json.loads(row[0]);sid=payload['steamId']
            self.db.execute("DELETE FROM moderation_targets WHERE steam_id=? AND outcome='confirmed' AND expires<=?",(sid,clock()))
            self.db.execute("INSERT OR IGNORE INTO moderation_targets VALUES(?,?,'held',NULL)",(sid,opid))
            owner=self.db.execute('SELECT operation_id FROM moderation_targets WHERE steam_id=?',(sid,)).fetchone()[0]
            if owner!=opid:
                self.db.execute('UPDATE admin_restrictions SET related_id=? WHERE id=?',(owner,opid));self.end(opid,'superseded','cancelled','target_already_coordinated',clock(),token=token);return self.get(opid)
        dispatched=False
        try:
            validate_admin(payload,self.engine.config,clock())
            guard=self.engine.kick_store
            if guard is None or not self.engine.config.get('rejoin_guard_enabled'):raise ContractError(503,'restriction_not_configured')
            guard.require_not_exempt(sid)
            known=self.engine.player_history.known(sid)
            if known is None:raise ContractError(422,'player_not_known')
            # A restriction must never silently turn into an online kick.
            data=self.engine.rcon.request('GET','/v1/players');players=data.get('players')
            if not isinstance(players,list) or any(not isinstance(p,dict) or not identity(p.get('steamId')) for p in players) or len({p['steamId'] for p in players})!=len(players):raise ValueError('invalid_live_roster')
            if any(p['steamId']==sid for p in players):
                self.end(opid,'cancelled','cancelled','player_online_use_kick',clock(),token=token);return self.get(opid)
            current=reserved_ids(self.engine.rcon.request('GET','/v1/reserved-slots'))
            guard.sync_reserved(current,clock())
            messages=notices(sid,known['name'])
            validate_admin(payload,self.engine.config,clock())
            with self.lock,self.db:
                if not self.owns(opid,token):return self.get(opid)
                self.db.execute("UPDATE admin_restrictions SET status='restriction_dispatching',name=?,updated=? WHERE id=?",(known['name'],clock(),opid))
                self.engine.moderation._audit(opid,'restriction_dispatching',clock())
            dispatched=True
            with self.lock,guard.lock:
                if not self.owns(opid,token):return self.get(opid)
                if not self.engine.config.get('rejoin_guard_enabled'):raise ContractError(503,'restriction_not_configured')
                guard.require_not_exempt(sid)
                validate_admin(payload,self.engine.config,clock())
                guard.restrict(sid,known['name'],REASON,'admin_offline',clock(),action_id='admin-restriction:'+opid)
            self.finalize(opid,clock(),token=token)
        except PunishmentExemptError as error:self.end(opid,'cancelled','cancelled',error.code,clock(),token=token)
        except ContractError as error:self.end(opid,'rejected','failed',error.code,clock(),token=token)
        except Exception:
            receipt=None
            if dispatched:
                try:receipt=self.receipt(opid,sid)
                except Exception:pass
            if receipt:
                self.end(opid,'announcement_failed','restricted','announcement_storage_failed',clock(),token=token)
            else:self.end(opid,'unknown' if dispatched else 'failed','unknown' if dispatched else 'failed','restriction_outcome_unknown' if dispatched else 'lookup_or_validation_failed',clock(),hold=dispatched,token=token)
        return self.get(opid)

    def process_one(self,now=None):
        with self.lock:row=self.db.execute("SELECT id FROM admin_restrictions WHERE status='accepted' ORDER BY created,rowid LIMIT 1").fetchone()
        if row is None:return False
        self.process(row[0],now);return True
