"""Durable restricted moderation; never replays an uncertain kick request."""

import hashlib
import json
import math
import time
import uuid

try:
    from service.announcements import kick_messages, validate_messages, offline_report_messages, offline_admin_messages
    from service.moderation_logic import resolve_player, kick_player
    from service.rcon import RconError, PunishmentExemptError, PunishmentDeferredError, reserved_ids
except ImportError:
    from announcements import kick_messages, validate_messages, offline_report_messages, offline_admin_messages
    from moderation_logic import resolve_player, kick_player
    from rcon import RconError, PunishmentExemptError, PunishmentDeferredError, reserved_ids


class ContractError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


REQUEST_FIELDS = {'reportId', 'steamId', 'ruleId', 'ruleVersion', 'evidenceVersion',
                  'evidenceRefs', 'decision', 'reason'}


def text(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum and all(c.isprintable() for c in value)


def number(value):
    try:
        return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


ADMIN_FIELDS = {'requestId', 'steamId', 'actor', 'requestedAt'}
ADMIN_ACTOR_FIELDS = {'platform', 'botId', 'groupId', 'memberId', 'role'}
ADMIN_SCOPE_FIELDS = {'platform', 'botId', 'groupId'}
ADMIN_REASON = {'zh': '管理员手动踢出', 'en': 'a manual administrator action', 'ja': '管理者による手動の退出指示'}


def validate_admin_config(config):
    scopes = config.get('admin_moderation_scopes', [])
    if not isinstance(scopes, list) or any(not isinstance(s, dict) or set(s) != ADMIN_SCOPE_FIELDS or
            any(not text(v, 128) or v != v.strip() for v in s.values()) for s in scopes):
        raise ValueError('Invalid admin_moderation_scopes')
    for key in ('admin_moderation_max_age_seconds', 'admin_moderation_future_skew_seconds'):
        value = config.get(key, 120 if key.endswith('max_age_seconds') else 30)
        if not number(value) or value < 0 or (key.endswith('max_age_seconds') and value == 0):
            raise ValueError('Invalid ' + key)


def validate_admin(payload, config, now, check_age=True):
    if not isinstance(payload, dict) or set(payload) != ADMIN_FIELDS:
        raise ContractError(400, 'invalid_admin_request_fields')
    if not text(payload['requestId'], 128) or payload['requestId'] != payload['requestId'].strip():
        raise ContractError(400, 'invalid_requestId')
    identity = payload['steamId']
    if not isinstance(identity, str) or len(identity) != 17 or not identity.isascii() or not identity.isdigit():
        raise ContractError(400, 'invalid_steamId')
    validate_admin_actor(payload['actor'], payload['requestedAt'], config, now, check_age)


def validate_admin_actor(actor, requested, config, now, check_age=True):
    if not isinstance(actor, dict) or set(actor) != ADMIN_ACTOR_FIELDS or any(not text(v, 128) or v != v.strip() for v in actor.values()):
        raise ContractError(400, 'invalid_actor')
    if actor['role'] not in ('owner', 'admin'):
        raise ContractError(403, 'admin_role_required')
    scope = {k: actor[k] for k in ADMIN_SCOPE_FIELDS}
    if scope not in config.get('admin_moderation_scopes', []):
        raise ContractError(403, 'admin_scope_not_allowed')
    if not number(requested) or requested < 0:
        raise ContractError(400, 'invalid_requestedAt')
    if check_age and not -config.get('admin_moderation_future_skew_seconds', 30) <= now - requested <= config.get('admin_moderation_max_age_seconds', 120):
        raise ContractError(422, 'admin_request_stale_or_future')


def validate_report(payload, config, now, check_eligibility=True):
    if not isinstance(payload, dict) or set(payload) != REQUEST_FIELDS:
        raise ContractError(400, 'invalid_request_fields')
    for field, limit in [('reportId', 128), ('ruleVersion', 128), ('evidenceVersion', 128)]:
        if not text(payload[field], limit):
            raise ContractError(400, 'invalid_' + field)
    identity = payload['steamId']
    if not isinstance(identity, str) or len(identity) != 17 or not identity.isascii() or not identity.isdigit():
        raise ContractError(400, 'invalid_steamId')
    if not text(payload['ruleId'], 64):
        raise ContractError(400, 'invalid_ruleId')
    if check_eligibility:
        rules = config['moderation_rules']
        if payload['ruleId'] not in rules:
            raise ContractError(422, 'rule_not_enabled')
        if payload['ruleVersion'] != rules[payload['ruleId']]:
            raise ContractError(422, 'rule_version_mismatch')
    refs = payload['evidenceRefs']
    if not isinstance(refs, list) or not 1 <= len(refs) <= 20 or any(not text(ref, 512) for ref in refs):
        raise ContractError(400, 'invalid_evidenceRefs')
    decision = payload['decision']
    if not isinstance(decision, dict) or set(decision) != {'verified', 'identityScore', 'violationScore', 'observedAt'}:
        raise ContractError(400, 'invalid_decision')
    if not isinstance(decision['verified'], bool):
        raise ContractError(400, 'invalid_verified')
    for score in ['identityScore', 'violationScore']:
        if not number(decision[score]) or not 0 <= decision[score] <= 1:
            raise ContractError(400, 'invalid_' + score)
    if not number(decision['observedAt']) or decision['observedAt'] < 0:
        raise ContractError(400, 'invalid_observedAt')
    if check_eligibility and (not decision['verified'] or decision['identityScore'] < config['moderation_identity_threshold'] or decision['violationScore'] < config['moderation_violation_threshold']):
        raise ContractError(422, 'decision_not_eligible')
    age = now - decision['observedAt']
    if check_eligibility and (age < -config['moderation_future_skew_seconds'] or age > config['moderation_decision_max_age_seconds']):
        raise ContractError(422, 'decision_stale_or_future')
    reason = payload['reason']
    if not isinstance(reason, dict) or set(reason) != {'zh', 'en', 'ja'} or any(not text(v, 160) for v in reason.values()):
        raise ContractError(400, 'invalid_reason')
    try:
        kick_messages(identity, reason['zh'], reason['en'], reason['ja'])
    except ValueError:
        raise ContractError(400, 'announcement_too_long') from None


class Coordinator:
    def __init__(self, engine):
        self.engine = engine
        self.db = engine.db
        self.lock = engine.lock
        self.config = engine.config
        with self.lock:
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS moderation_operations(
                id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                report_id TEXT NOT NULL, evidence_version TEXT NOT NULL,
                steam_id TEXT NOT NULL, source TEXT NOT NULL,
                payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
                status TEXT NOT NULL, kick_status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL,
                error_code TEXT, related_operation_id TEXT, player_name TEXT, execution_token TEXT);
              CREATE UNIQUE INDEX IF NOT EXISTS moderation_report_once
                ON moderation_operations(report_id,evidence_version) WHERE source='report';
              CREATE UNIQUE INDEX IF NOT EXISTS moderation_admin_once
                ON moderation_operations(report_id) WHERE source='admin';
              CREATE TABLE IF NOT EXISTS moderation_targets(
                steam_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
                outcome TEXT NOT NULL, expires REAL);
              CREATE TABLE IF NOT EXISTS moderation_idempotency_keys(
                key TEXT PRIMARY KEY, operation_id TEXT NOT NULL, payload_hash TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS moderation_operation_jobs(
                operation_id TEXT NOT NULL, language TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY(operation_id,language));
              CREATE TABLE IF NOT EXISTS moderation_admin_kick_attempts(
                operation_id TEXT PRIMARY KEY,dispatch_count INTEGER NOT NULL,retry_at REAL);
              CREATE TABLE IF NOT EXISTS moderation_audit(
                id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL,
                phase TEXT NOT NULL, timestamp REAL NOT NULL, detail TEXT NOT NULL);
            ''')
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(moderation_operations)')}
            if 'execution_token' not in columns:
                self.db.execute('ALTER TABLE moderation_operations ADD COLUMN execution_token TEXT')
            # An unresolved request may already have reached RCON. A restart is
            # never permission to send it again; retain the target quarantine.
            with self.db:
                rows = self.db.execute("SELECT id FROM moderation_operations WHERE status='kick_dispatching'").fetchall()
                for (operation_id,) in rows:
                    self._set(operation_id, 'unknown', 'unknown', time.time(), 'restart_during_kick')
                    self.db.execute("UPDATE moderation_targets SET outcome='unknown',expires=NULL WHERE operation_id=?", (operation_id,))
                # Resolving is explicitly before the durable dispatch boundary.
                rows = self.db.execute("SELECT id FROM moderation_operations WHERE status='resolving'").fetchall()
                for (operation_id,) in rows:
                    self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?', (operation_id,))
                    self._set(operation_id, 'accepted', 'not_started', time.time(), None)
                    self.db.execute('UPDATE moderation_operations SET execution_token=NULL WHERE id=?', (operation_id,))
                # Auto jobs are claimed by the sender, not the report worker.
                # A crash before dispatch cancels the old auto task instead of
                # leaving an unprocessable pending operation forever.
                rows = self.db.execute("SELECT id FROM moderation_operations WHERE source='auto_kd' AND status='accepted'").fetchall()
                for (operation_id,) in rows:
                    self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?', (operation_id,))
                    self._set(operation_id, 'cancelled', 'not_needed', time.time(), 'auto_interrupted_before_dispatch')

    def _audit(self, operation_id, phase, now, code=None):
        self.db.execute('INSERT INTO moderation_audit(operation_id,phase,timestamp,detail) VALUES(?,?,?,?)',
                        (operation_id, phase, now, json.dumps({'code': code})))

    def _set(self, operation_id, status, kick_status, now, code=None):
        self.db.execute('UPDATE moderation_operations SET status=?,kick_status=?,updated=?,error_code=? WHERE id=?',
                        (status, kick_status, now, code, operation_id))
        self._audit(operation_id, status, now, code)

    def _insert(self, key, payload, source, now):
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        old = self.db.execute('SELECT operation_id,payload_hash FROM moderation_idempotency_keys WHERE key=?', (key,)).fetchone()
        if old is None and source == 'report':
            old = self.db.execute("SELECT id,payload_hash FROM moderation_operations WHERE source='report' AND report_id=? AND evidence_version=?",
                                  (payload['reportId'], payload['evidenceVersion'])).fetchone()
        if source == 'admin' and getattr(self.engine,'admin_bans',None) is not None and self.engine.admin_bans.has_request(payload['requestId']):
            raise ContractError(409,'idempotency_conflict')
        if source == 'admin' and getattr(self.engine, 'admin_history', None) is not None and self.engine.admin_history.has_request(payload['requestId']):
            raise ContractError(409, 'idempotency_conflict')
        if source == 'admin' and getattr(self.engine, 'admin_cycle', None) is not None and self.engine.admin_cycle.has_request(payload['requestId']):
            raise ContractError(409, 'idempotency_conflict')
        if source == 'admin' and getattr(self.engine, 'admin_unban', None) is not None and self.engine.admin_unban.has_request(payload['requestId']):
            raise ContractError(409, 'idempotency_conflict')
        if old is None and source == 'admin':
            old = self.db.execute("SELECT id,payload_hash FROM moderation_operations WHERE source='admin' AND report_id=?", (payload['requestId'],)).fetchone()
        if old:
            if old[1] != digest:
                raise ContractError(409, 'idempotency_conflict')
            self.db.execute('INSERT OR IGNORE INTO moderation_idempotency_keys VALUES(?,?,?)', (key, old[0], digest))
            return old[0], True
        if source == 'report':
            validate_report(payload, self.config, now)
        elif source == 'admin':
            validate_admin(payload, self.config, now)
        pending = self.db.execute("SELECT count(*) FROM moderation_operations WHERE status IN ('accepted','resolving','kick_dispatching')").fetchone()[0]
        if getattr(self.engine,'admin_history',None) is not None:
            pending += self.engine.admin_history.pending_count()
        if getattr(self.engine,'admin_cycle',None) is not None:
            pending += self.engine.admin_cycle.pending_count()
        if getattr(self.engine,'admin_unban',None) is not None:
            pending += self.engine.admin_unban.pending_count()
        if getattr(self.engine,'admin_bans',None) is not None:pending += self.engine.admin_bans.pending_count()
        if pending >= self.config['moderation_pending_limit']:
            raise ContractError(429, 'moderation_queue_full')
        operation_id = uuid.uuid4().hex
        self.db.execute('''INSERT INTO moderation_operations
            (id,idempotency_key,report_id,evidence_version,steam_id,source,payload_hash,payload_json,status,kick_status,created,updated)
            VALUES(?,?,?,?,?,?,?,?,'accepted','not_started',?,?)''',
            (operation_id, key, payload['requestId'] if source == 'admin' else payload['reportId'],
             '' if source == 'admin' else payload['evidenceVersion'], payload['steamId'], source, digest, encoded, now, now))
        self._audit(operation_id, 'accepted', now)
        self.db.execute('INSERT INTO moderation_idempotency_keys VALUES(?,?,?)', (key, operation_id, digest))
        return operation_id, False

    def submit(self, payload, key, now=None):
        if not text(key, 128):
            raise ContractError(400, 'idempotency_key_required')
        if not isinstance(payload, dict):
            raise ContractError(400, 'body_must_be_object')
        try:
            json.dumps(payload, allow_nan=False)
        except (ValueError, TypeError):
            raise ContractError(400, 'invalid_json_values') from None
        # Require shape before consulting the natural report/evidence key.
        if set(payload) != REQUEST_FIELDS:
            raise ContractError(400, 'invalid_request_fields')
        now = time.time() if now is None else now
        validate_report(payload, self.config, now, check_eligibility=False)
        with self.lock, self.db:
            # Acquire the SQLite writer lock before deduplication reads too.
            self.db.execute('UPDATE moderation_operations SET updated=updated WHERE 0')
            operation_id, duplicate = self._insert('report:' + key, payload, 'report', now)
        result = self.get(operation_id)
        result['deduplicated'] = duplicate
        return result

    def submit_admin(self, payload, key, now=None):
        if not text(key, 128):
            raise ContractError(400, 'idempotency_key_required')
        try:
            json.dumps(payload, allow_nan=False)
        except (ValueError, TypeError):
            raise ContractError(400, 'invalid_json_values') from None
        now = time.time() if now is None else now
        # Scope and role must remain authorized even for retries. Completed
        # immutable requests can be queried idempotently after their TTL.
        validate_admin(payload, self.config, now, check_age=False)
        if getattr(self.engine, 'admin_history', None) is not None and self.engine.admin_history.has_request(payload['requestId']):
            raise ContractError(409, 'idempotency_conflict')
        with self.lock, self.db:
            self.db.execute('UPDATE moderation_operations SET updated=updated WHERE 0')
            operation_id, duplicate = self._insert('admin:' + key, payload, 'admin', now)
        result = self.get(operation_id)
        result['deduplicated'] = duplicate
        return result

    def get(self, operation_id):
        with self.lock:
            cursor = self.db.execute('SELECT * FROM moderation_operations WHERE id=?', (operation_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            op = dict(zip([column[0] for column in cursor.description], row))
            jobs = self.db.execute('''SELECT m.language,j.id,j.status,j.updated
                FROM moderation_operation_jobs m JOIN jobs j ON j.id=m.job_id
                WHERE m.operation_id=? ORDER BY CASE m.language WHEN 'zh' THEN 0 WHEN 'en' THEN 1 ELSE 2 END''',
                (operation_id,)).fetchall()
            jobs, suppressed = self.engine.announcement_policy.split(jobs)
            statuses = [job[2] for job in jobs]
            announcement_status = 'not_queued'
            if statuses:
                if all(s == 'sent' for s in statuses):
                    announcement_status = 'sent'
                elif any(s in ('queued', 'sending') for s in statuses):
                    announcement_status = 'pending'
                elif 'unknown' in statuses:
                    announcement_status = 'unknown'
                elif 'sent' in statuses:
                    announcement_status = 'partial'
                else:
                    announcement_status = 'failed'
            status = op['status']
            if status == 'announcements_pending' and announcement_status != 'pending':
                status = {'sent': 'completed', 'unknown': 'announcement_unknown'}.get(announcement_status, 'announcement_failed')
            result = {'operationId': op['id'], 'reportId': op['report_id'], 'steamId': op['steam_id'],
                    'source': op['source'], 'name': op['player_name'], 'status': status,
                    'kickStatus': op['kick_status'], 'announcementStatus': announcement_status,
                    'announcementSuppressedJobs': suppressed,
                    'announcementJobIds': [job[1] for job in jobs],
                    'announcementJobs': [{'language': lang, 'jobId': identity, 'status': state} for lang, identity, state, _ in jobs],
                    'createdAt': op['created'], 'updatedAt': max([op['updated']] + [job[3] for job in jobs]),
                    'error': {'code': op['error_code']} if op['error_code'] else None,
                    'relatedOperationId': op['related_operation_id']}
            if op['source'] == 'admin':
                payload = json.loads(op['payload_json'])
                result.pop('reportId')
                result.update(requestId=payload['requestId'], actor=payload['actor'], requestedAt=payload['requestedAt'])
            return result

    def _claim(self, operation_id, now):
        with self.lock, self.db:
            token = uuid.uuid4().hex
            changed = self.db.execute("UPDATE moderation_operations SET status='resolving',execution_token=?,updated=? WHERE id=? AND status='accepted'",
                                      (token, now, operation_id)).rowcount
            if not changed:
                return None
            row = self.db.execute('SELECT payload_json,source,steam_id FROM moderation_operations WHERE id=?', (operation_id,)).fetchone()
            payload, source, identity = json.loads(row[0]), row[1], row[2]
            self.db.execute("DELETE FROM moderation_targets WHERE steam_id=? AND outcome='confirmed' AND expires<=?", (identity, now))
            self.db.execute("INSERT OR IGNORE INTO moderation_targets VALUES(?,?,'held',NULL)", (identity, operation_id))
            owner = self.db.execute('SELECT operation_id FROM moderation_targets WHERE steam_id=?', (identity,)).fetchone()[0]
            if owner != operation_id:
                self._set(operation_id, 'superseded', 'not_needed', now, 'target_already_coordinated')
                self.db.execute('UPDATE moderation_operations SET related_operation_id=? WHERE id=?', (owner, operation_id))
                return None
            self._audit(operation_id, 'resolving', now)
            return payload, source, identity, token

    def _end(self, operation_id, status, kick_status, now, code=None, hold_unknown=False, token=None):
        with self.lock, self.db:
            if token is not None and not self._owns(operation_id, token):
                return
            self._set(operation_id, status, kick_status, now, code)
            if hold_unknown:
                self.db.execute("UPDATE moderation_targets SET outcome='unknown',expires=NULL WHERE operation_id=?", (operation_id,))
            else:
                self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?', (operation_id,))

    def _owns(self, operation_id, token):
        row = self.db.execute('SELECT execution_token FROM moderation_operations WHERE id=?', (operation_id,)).fetchone()
        return row is not None and row[0] == token

    def _notices(self, operation_id, messages, now):
        for lang, message in zip(['zh', 'en', 'ja'], messages):
            job = self.engine.add_job(message, now, kind='management', language=lang)
            self.db.execute('INSERT INTO moderation_operation_jobs VALUES(?,?,?)', (operation_id, lang, job))

    def _retry_sleep(self,delay):
        time.sleep(delay)

    def process(self, operation_id, now=None, eligible=None, notice_builder=None):
        clock = time.time if now is None else lambda: now
        claimed = self._claim(operation_id, clock())
        if claimed is None:
            return self.get(operation_id)
        payload, source, identity, token = claimed
        while True:
            retry_count=0
            if source=='admin':
                with self.lock:
                    if not self._owns(operation_id,token):return self.get(operation_id)
                    retry=self.db.execute('SELECT dispatch_count,retry_at FROM moderation_admin_kick_attempts WHERE operation_id=?',(operation_id,)).fetchone()
                if retry:
                    retry_count=retry[0]
                    if retry[1] is not None:self._retry_sleep(min(2,max(0,retry[1]-clock())))
                with self.lock:
                    if not self._owns(operation_id,token):return self.get(operation_id)
            dispatched = False
            try:
                if source == 'report':
                    validate_report(payload, self.config, clock())
                elif source == 'admin':
                    validate_admin(payload, self.config, clock())
                guard = getattr(self.engine, 'kick_store', None)
                if source=='auto_kd' and guard is not None:
                    released=guard.released_at(identity)
                    job_id=payload['reportId'][3:]
                    with self.lock:
                        job=self.db.execute('SELECT created FROM jobs WHERE id=?',(job_id,)).fetchone()
                        created=self.db.execute('SELECT created FROM moderation_operations WHERE id=?',(operation_id,)).fetchone()[0]
                    if released is not None and min(created,job[0] if job else created)<=released:
                        self._end(operation_id,'cancelled','not_needed',clock(),'auto_precedes_unban',token=token)
                        return self.get(operation_id)
                if source=='admin' and retry_count:
                    try:current_reserved=reserved_ids(self.engine.rcon.request('GET','/v1/reserved-slots'))
                    except Exception:raise PunishmentDeferredError('reserved list unavailable') from None
                    if guard is not None:guard.sync_reserved(current_reserved,clock())
                    elif identity in current_reserved:raise PunishmentExemptError('reserved_player_exempt')
                if guard is not None:
                    guard.require_not_exempt(identity)
                player = resolve_player(self.engine.rcon.request, identity)
                if source == 'report':
                    validate_report(payload, self.config, clock())
                elif source == 'admin':
                    validate_admin(payload, self.config, clock())
                if player is None:
                    # Offline is a fact, not proof of voluntary departure or kick success.
                    with self.lock, self.db:
                        if not self._owns(operation_id, token):
                            return self.get(operation_id)
                        if source == 'report':
                            self._notices(operation_id, offline_report_messages(identity), clock())
                        elif source == 'admin':
                            self._notices(operation_id, offline_admin_messages(identity), clock())
                        self._set(operation_id, 'offline', 'not_needed', clock(), 'player_offline')
                        self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?', (operation_id,))
                    return self.get(operation_id)
                if source == 'auto_kd' and self.config.get('exclude_barbed_wire_kills'):
                    player = self.engine.kill_accounting.adjust(player, self.engine.kill_accounting.counts(clock()))
                if eligible is not None and not eligible(player):
                    self._end(operation_id, 'cancelled', 'not_needed', clock(), 'kd_no_longer_eligible', token=token)
                    return self.get(operation_id)
                reason = ADMIN_REASON if source == 'admin' else payload['reason']
                messages = notice_builder(player) if notice_builder else kick_messages(player['name'], reason['zh'], reason['en'], reason['ja'])
                validate_messages(messages)
                if source == 'report':
                    validate_report(payload, self.config, clock())
                elif source == 'admin':
                    validate_admin(payload, self.config, clock())
                auto_native = source == 'auto_kd' and self.config.get('auto_native_ban_enabled',False)
                if auto_native:
                    if guard is None:raise ContractError(503,'native_ban_not_configured')
                    try:current_reserved=reserved_ids(self.engine.rcon.request('GET','/v1/reserved-slots'))
                    except Exception:raise PunishmentDeferredError('reserved list unavailable') from None
                    guard.sync_reserved(current_reserved,clock())
                    guard.require_not_exempt(identity)
                with self.lock, self.db:
                    if not self._owns(operation_id, token):
                        return self.get(operation_id)
                    current = self.db.execute('SELECT status FROM moderation_operations WHERE id=?', (operation_id,)).fetchone()[0]
                    if current != 'resolving':
                        return self.get(operation_id)
                    if source=='admin':
                        validate_admin(payload,self.config,clock())
                        if guard is not None:guard.require_not_exempt(identity)
                        lease=self.db.execute('SELECT operation_id,outcome FROM moderation_targets WHERE steam_id=?',(identity,)).fetchone()
                        if lease!=(operation_id,'held'):
                            self._set(operation_id,'superseded','not_needed',clock(),'target_already_coordinated')
                            if lease:self.db.execute('UPDATE moderation_operations SET related_operation_id=? WHERE id=?',(lease[0],operation_id))
                            self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?',(operation_id,))
                            return self.get(operation_id)
                        attempts=self.db.execute('SELECT dispatch_count FROM moderation_admin_kick_attempts WHERE operation_id=?',(operation_id,)).fetchone()
                        if attempts and attempts[0]>=3:
                            self._set(operation_id,'failed','failed',clock(),'rcon_http_404')
                            self.db.execute('DELETE FROM moderation_targets WHERE operation_id=?',(operation_id,))
                            return self.get(operation_id)
                        self.db.execute('''INSERT INTO moderation_admin_kick_attempts VALUES(?,1,NULL)
                            ON CONFLICT(operation_id) DO UPDATE SET dispatch_count=dispatch_count+1,retry_at=NULL''',(operation_id,))
                    self.db.execute('UPDATE moderation_operations SET player_name=? WHERE id=?', (player['name'], operation_id))
                    self._set(operation_id, 'kick_dispatching', 'dispatching', clock())
                # The committed dispatch boundary MUST precede this external request.
                dispatched = True
                if auto_native:
                    # Stable local receipt, then opt into the existing native-ban-on-sight path.
                    guard.require_not_exempt(identity)
                    guard.restrict(identity,player['name'],reason['zh'],'auto_native',clock(),action_id='auto-native:'+operation_id)
                    guard.request_native_bans([identity],clock())
                    response=self.engine.rcon._kick(identity,{'reason':reason['zh']},expected_scope='permanent')
                    if response.get('guardCancelled'):
                        self._end(operation_id,'offline','not_needed',clock(),'player_offline_or_guard_cancelled',token=token)
                        return self.get(operation_id)
                    if response.get('ok') is not True or response.get('pending'):raise ValueError('native removal not confirmed')
                else:
                    kick_player(self.engine.rcon.request, identity, reason['zh'])
                finished = clock()
                with self.lock, self.db:
                    if not self._owns(operation_id, token):
                        return self.get(operation_id)
                    self._audit(operation_id, 'kick_confirmed', finished)
                    self._notices(operation_id, messages, finished)
                    self._set(operation_id, 'announcements_pending', 'confirmed', finished)
                    self.db.execute("UPDATE moderation_targets SET outcome='confirmed',expires=? WHERE operation_id=?",
                                    (finished + self.config['moderation_target_cooldown_seconds'], operation_id))
            except PunishmentExemptError as error:
                self._end(operation_id, 'cancelled', 'not_needed', clock(), error.code, token=token)
            except PunishmentDeferredError as error:
                self._end(operation_id, 'failed', 'not_started', clock(), error.code, token=token)
            except ContractError as error:
                self._end(operation_id, 'rejected', 'not_started', clock(), error.code, token=token)
            except RconError as error:
                if (source=='admin' and dispatched and error.status==404 and error.method=='POST'
                        and error.path=='/v1/players/'+identity+'/kick'):
                    retry_scheduled=False
                    with self.lock,self.db:
                        if not self._owns(operation_id,token):return self.get(operation_id)
                        current=self.db.execute('SELECT status FROM moderation_operations WHERE id=?',(operation_id,)).fetchone()[0]
                        if current!='kick_dispatching':return self.get(operation_id)
                        count=self.db.execute('SELECT dispatch_count FROM moderation_admin_kick_attempts WHERE operation_id=?',(operation_id,)).fetchone()[0]
                        if count<3:
                            # A proven 404 ends this dispatch. Only this safe state
                            # may be resolved again after restart, with the same budget.
                            self.db.execute('UPDATE moderation_admin_kick_attempts SET retry_at=? WHERE operation_id=?',(clock()+count,operation_id))
                            self._set(operation_id,'resolving','not_started',clock())
                            self._audit(operation_id,'kick_retry_404_scheduled',clock(),'rcon_http_404')
                            retry_scheduled=True
                    if retry_scheduled:continue
                if dispatched and not 400 <= error.status < 500:
                    self._end(operation_id, 'unknown', 'unknown', clock(), 'upstream_outcome_unknown', hold_unknown=True, token=token)
                else:
                    self._end(operation_id, 'failed', 'failed' if dispatched else 'not_started', clock(), 'rcon_http_' + str(error.status), token=token)
            except Exception:
                self._end(operation_id, 'unknown' if dispatched else 'failed', 'unknown' if dispatched else 'not_started',
                          clock(), 'upstream_outcome_unknown' if dispatched else 'lookup_or_validation_failed', hold_unknown=dispatched, token=token)
            return self.get(operation_id)

    def process_one(self, now=None):
        with self.lock:
            row = self.db.execute("SELECT id FROM moderation_operations WHERE source IN ('report','admin') AND status='accepted' ORDER BY created,rowid LIMIT 1").fetchone()
        if row is None:
            return False
        self.process(row[0], now=now)
        return True

    def process_auto(self, job_id, identity, reason, eligible, notice_builder, now=None):
        now_value = time.time() if now is None else now
        payload = {'reportId': 'kd:' + job_id, 'evidenceVersion': 'internal', 'steamId': identity,
                   'reason': {'zh': reason, 'en': '', 'ja': ''}}
        with self.lock, self.db:
            self.db.execute('UPDATE moderation_operations SET updated=updated WHERE 0')
            operation_id, _ = self._insert('auto:' + job_id, payload, 'auto_kd', now_value)
        return self.process(operation_id, now=now, eligible=eligible, notice_builder=notice_builder)
