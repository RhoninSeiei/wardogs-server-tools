"""Administrator registration into the existing behavior policy; no RCON writes."""
import hashlib
import json
import time
import uuid
try:
    from service.moderation_ops import ContractError, validate_admin, text
    from service.player_history import identity, name_text
    from service.rcon import reserved_ids, PunishmentExemptError
except ImportError:
    from moderation_ops import ContractError, validate_admin, text
    from player_history import identity, name_text
    from rcon import reserved_ids, PunishmentExemptError


class AdminCycle:
    def __init__(self, engine):
        self.engine, self.db, self.lock = engine, engine.db, engine.lock
        with self.lock, self.db:
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS admin_cycle_adds(
                id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,steam_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL,
                cycle_status TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,error_code TEXT,
                name TEXT,execution_token TEXT,related_id TEXT,already_enabled INTEGER,
                online_at_check INTEGER,registered_at REAL);
              CREATE TABLE IF NOT EXISTS admin_cycle_keys(
                key TEXT PRIMARY KEY,operation_id TEXT NOT NULL,payload_hash TEXT NOT NULL);
            ''')
        guard = engine.kick_store
        if guard is not None:
            with guard.lock, guard.db:
                guard.db.execute('''CREATE TABLE IF NOT EXISTS admin_cycle_receipts(
                  operation_id TEXT PRIMARY KEY,steam_id TEXT NOT NULL,registered_at REAL NOT NULL)''')
        with self.lock, self.db:
            rows = self.db.execute("SELECT id FROM admin_cycle_adds WHERE status='resolving'").fetchall()
            for (opid,) in rows:
                self.db.execute("UPDATE admin_cycle_adds SET status='accepted',execution_token=NULL WHERE id=?", (opid,))
                self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?', (opid,))
            rows = self.db.execute("SELECT id,steam_id FROM admin_cycle_adds WHERE status='cycle_dispatching'").fetchall()
        for opid, sid in rows:
            try: receipt = self.receipt(opid, sid)
            except Exception: receipt = None
            if receipt is not None: self.finalize(opid, receipt, time.time())
            else: self.end(opid, 'unknown', 'unknown', 'cycle_outcome_unknown', time.time(), hold=True)

    def has_request(self, request_id):
        with self.lock:
            return bool(self.db.execute('SELECT 1 FROM admin_cycle_adds WHERE request_id=?', (request_id,)).fetchone())

    def pending_count(self):
        return self.db.execute("SELECT count(*) FROM admin_cycle_adds WHERE status IN ('accepted','resolving','cycle_dispatching')").fetchone()[0]

    def submit(self, payload, key, now=None):
        now = time.time() if now is None else now
        if not text(key, 128): raise ContractError(400, 'idempotency_key_required')
        try: encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        except (TypeError, ValueError): raise ContractError(400, 'invalid_json_values') from None
        validate_admin(payload, self.engine.config, now, check_age=False)
        if self.engine.behavior is None: raise ContractError(503, 'cycle_not_configured')
        if getattr(self.engine,'admin_bans',None) is not None and self.engine.admin_bans.has_request(payload['requestId']):raise ContractError(409,'idempotency_conflict')
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.lock, self.db:
            self.db.execute('UPDATE admin_cycle_adds SET updated=updated WHERE 0')
            if getattr(self.engine,'admin_bans',None) is not None and self.engine.admin_bans.has_request(payload['requestId']):raise ContractError(409,'idempotency_conflict')
            if (self.db.execute("SELECT 1 FROM moderation_operations WHERE source='admin' AND report_id=?", (payload['requestId'],)).fetchone()
                    or self.engine.admin_history.has_request(payload['requestId'])):
                raise ContractError(409, 'idempotency_conflict')
            if getattr(self.engine,'admin_unban',None) is not None and self.engine.admin_unban.has_request(payload['requestId']):
                raise ContractError(409,'idempotency_conflict')
            old = self.db.execute('SELECT operation_id,payload_hash FROM admin_cycle_keys WHERE key=?', (key,)).fetchone()
            if old is None: old = self.db.execute('SELECT id,payload_hash FROM admin_cycle_adds WHERE request_id=?', (payload['requestId'],)).fetchone()
            if old:
                if old[1] != digest: raise ContractError(409, 'idempotency_conflict')
                opid, duplicate = old[0], True
                self.db.execute('INSERT OR IGNORE INTO admin_cycle_keys VALUES(?,?,?)', (key, opid, digest))
            else:
                validate_admin(payload, self.engine.config, now)
                count = self.pending_count() + self.engine.admin_history.pending_count()
                count += self.db.execute("SELECT count(*) FROM moderation_operations WHERE status IN ('accepted','resolving','kick_dispatching')").fetchone()[0]
                if getattr(self.engine,'admin_unban',None) is not None:count += self.engine.admin_unban.pending_count()
                if getattr(self.engine,'admin_bans',None) is not None:count += self.engine.admin_bans.pending_count()
                if count >= self.engine.config['moderation_pending_limit']: raise ContractError(429, 'moderation_queue_full')
                opid, duplicate = 'ac_' + uuid.uuid4().hex, False
                self.db.execute("INSERT INTO admin_cycle_adds VALUES(?,?,?,?,?,'accepted','pending',?,?,NULL,NULL,NULL,NULL,NULL,NULL,NULL)",
                                (opid, payload['requestId'], payload['steamId'], digest, encoded, now, now))
                self.db.execute('INSERT INTO admin_cycle_keys VALUES(?,?,?)', (key, opid, digest))
                self.engine.moderation._audit(opid, 'cycle_accepted', now)
        result = self.get(opid); result['deduplicated'] = duplicate
        return result

    def get(self, opid):
        with self.lock:
            cur = self.db.execute('SELECT * FROM admin_cycle_adds WHERE id=?', (opid,)); row = cur.fetchone()
            if row is None: return None
            op = dict(zip([d[0] for d in cur.description], row)); payload = json.loads(op['payload_json'])
            return {'operationId': opid, 'source': 'admin', 'action': 'cycle_add', 'requestId': payload['requestId'],
                    'steamId': op['steam_id'], 'name': op['name'], 'actor': payload['actor'], 'requestedAt': payload['requestedAt'],
                    'status': op['status'], 'cycleStatus': op['cycle_status'],
                    'alreadyEnabled': None if op['already_enabled'] is None else bool(op['already_enabled']),
                    'onlineAtCheck': None if op['online_at_check'] is None else bool(op['online_at_check']),
                    'registeredAt': op['registered_at'], 'createdAt': op['created'], 'updatedAt': op['updated'],
                    'kickStatus': 'not_needed', 'nativeBanStatus': 'not_requested', 'announcementStatus': 'not_queued',
                    'announcementJobIds': [], 'announcementJobs': [], 'relatedOperationId': op['related_id'],
                    'error': {'code': op['error_code']} if op['error_code'] else None}

    def owns(self, opid, token):
        row = self.db.execute('SELECT execution_token FROM admin_cycle_adds WHERE id=?', (opid,)).fetchone()
        return row is not None and row[0] == token

    def receipt(self, opid, sid):
        guard = self.engine.kick_store
        if guard is None: raise RuntimeError('cycle_store_unavailable')
        with guard.lock:
            row = guard.db.execute('SELECT steam_id,registered_at FROM admin_cycle_receipts WHERE operation_id=?', (opid,)).fetchone()
        return row[1] if row is not None and row[0] == sid else None

    def end(self, opid, status, cycle, code, now, hold=False, token=None):
        with self.lock, self.db:
            if token is not None and not self.owns(opid, token): return
            self.db.execute('UPDATE admin_cycle_adds SET status=?,cycle_status=?,error_code=?,updated=? WHERE id=?', (status, cycle, code, now, opid))
            self.engine.moderation._audit(opid, status, now, code)
            if hold: self.db.execute("UPDATE moderation_targets SET outcome='unknown',expires=NULL WHERE operation_id=?", (opid,))
            else: self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?', (opid,))

    def finalize(self, opid, registered, now, token=None):
        with self.lock, self.db:
            if token is not None and not self.owns(opid, token): return
            self.db.execute("UPDATE admin_cycle_adds SET status='completed',cycle_status='registered',registered_at=?,error_code=NULL,updated=? WHERE id=?", (registered, now, opid))
            self.engine.moderation._audit(opid, 'cycle_registered', now)
            self.db.execute("UPDATE moderation_targets SET outcome='confirmed',expires=? WHERE operation_id=?", (now + self.engine.config['moderation_target_cooldown_seconds'], opid))

    def process(self, opid, now=None):
        clock = time.time if now is None else lambda: now
        with self.lock, self.db:
            token = uuid.uuid4().hex
            changed = self.db.execute("UPDATE admin_cycle_adds SET status='resolving',execution_token=?,updated=? WHERE id=? AND status='accepted'", (token, clock(), opid)).rowcount
            if not changed: return self.get(opid)
            payload = json.loads(self.db.execute('SELECT payload_json FROM admin_cycle_adds WHERE id=?', (opid,)).fetchone()[0]); sid = payload['steamId']
            self.db.execute("DELETE FROM moderation_targets WHERE steam_id=? AND outcome='confirmed' AND expires<=?", (sid, clock()))
            self.db.execute("INSERT OR IGNORE INTO moderation_targets VALUES(?,?,'held',NULL)", (sid, opid))
            owner = self.db.execute('SELECT operation_id FROM moderation_targets WHERE steam_id=?', (sid,)).fetchone()[0]
            if owner != opid:
                self.db.execute('UPDATE admin_cycle_adds SET related_id=? WHERE id=?', (owner, opid))
                self.end(opid, 'superseded', 'cancelled', 'target_already_coordinated', clock(), token=token)
                return self.get(opid)
        dispatched = False
        try:
            validate_admin(payload, self.engine.config, clock())
            if self.engine.behavior is None: raise ContractError(503, 'cycle_not_configured')
            data = self.engine.rcon.request('GET', '/v1/players'); players = data.get('players')
            if (not isinstance(players, list) or len(players)>1000
                    or any(not isinstance(p,dict) or not identity(p.get('steamId')) for p in players)
                    or len({p['steamId'] for p in players}) != len(players)): raise ValueError('invalid_live_roster')
            player = next((p for p in players if p['steamId'] == sid), None)
            known = self.engine.player_history.known(sid)
            if player is None and known is None: raise ContractError(422, 'player_not_known')
            name = player.get('name') if player is not None else known['name']
            name = name[:100] if name_text(name) else sid
            current = reserved_ids(self.engine.rcon.request('GET', '/v1/reserved-slots'))
            guard = self.engine.kick_store
            guard.sync_reserved(current, clock())
            with self.lock, guard.lock, self.db:
                if not self.owns(opid, token): return self.get(opid)
                validate_admin(payload, self.engine.config, clock())
                if guard.reserved_exempt(sid): raise PunishmentExemptError('reserved_player_exempt')
                lease = self.db.execute('SELECT operation_id,outcome FROM moderation_targets WHERE steam_id=?', (sid,)).fetchone()
                if lease != (opid, 'held'):
                    if lease:self.db.execute('UPDATE admin_cycle_adds SET related_id=? WHERE id=?',(lease[0],opid))
                    self.end(opid,'superseded','cancelled','target_already_coordinated',clock(),token=token)
                    return self.get(opid)
                enabled = guard.db.execute('SELECT enabled FROM behavior_players WHERE steam_id=?', (sid,)).fetchone()
                self.db.execute("UPDATE admin_cycle_adds SET status='cycle_dispatching',name=?,already_enabled=?,online_at_check=?,updated=? WHERE id=?", (name, int(bool(enabled and enabled[0])), int(player is not None), clock(), opid))
                self.engine.moderation._audit(opid, 'cycle_dispatching', clock())
            dispatched = True
            with self.lock, guard.lock:
                if not self.owns(opid, token): return self.get(opid)
                validate_admin(payload, self.engine.config, clock())
                if guard.reserved_exempt(sid): raise PunishmentExemptError('reserved_player_exempt')
                # BehaviorPolicy.add commits this same connection. Inserting the
                # receipt first makes it atomic with the existing policy write.
                registered = clock()
                with guard.db:
                    guard.db.execute('INSERT INTO admin_cycle_receipts VALUES(?,?,?)', (opid, sid, registered))
                    self.engine.behavior.add(sid, name, registered)
            self.finalize(opid, registered, clock(), token=token)
        except PunishmentExemptError as error: self.end(opid,'cancelled','cancelled',error.code,clock(),token=token)
        except ContractError as error: self.end(opid,'rejected','failed',error.code,clock(),token=token)
        except Exception:
            receipt = None
            if dispatched:
                try: receipt = self.receipt(opid, sid)
                except Exception: pass
            if receipt is not None:
                self.finalize(opid, receipt, clock(), token=token)
            else:
                self.end(opid,'unknown' if dispatched else 'failed','unknown' if dispatched else 'failed',
                         'cycle_outcome_unknown' if dispatched else 'lookup_or_validation_failed',clock(),hold=dispatched,token=token)
        return self.get(opid)

    def process_one(self, now=None):
        with self.lock:
            row = self.db.execute("SELECT id FROM admin_cycle_adds WHERE status='accepted' ORDER BY created,rowid LIMIT 1").fetchone()
        if row is None: return False
        self.process(row[0], now)
        return True
