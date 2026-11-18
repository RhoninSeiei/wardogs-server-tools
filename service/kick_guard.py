"""Confirmed removal ledger, permanent enforcement and requested native bans."""
import json
import hashlib
import math
from datetime import datetime
import logging
from pathlib import Path
import sqlite3
import threading
import time
import uuid

try:
    from service.announcements import kick_messages
    from service.rcon import PunishmentExemptError, PunishmentDeferredError
except ImportError:
    from announcements import kick_messages
    from rcon import PunishmentExemptError, PunishmentDeferredError

try:
    from service import ban_statistics as ban_stats
except ImportError:
    import ban_statistics as ban_stats

LOG = logging.getLogger('wardogs')
PERMANENT_SCOPE = 'permanent'
REJOIN_REASON = '曾被踢出，触发永久重入限制'


def native_fingerprint(record):
    """Bind source evidence to this native ban generation, never to a nickname."""
    if not isinstance(record, dict): return None
    stamp = record.get('bannedAtUtc')
    if not isinstance(stamp, str) or not stamp: return None
    try:
        parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.timestamp() <= 0: return None
    except (ValueError, OverflowError): return None
    fields = {key: record.get(key) for key in ('steamId', 'bannedAtUtc', 'bannedBy', 'reason')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def local_origin(db, identity, event_id):
    released = db.execute('SELECT MAX(released_at) FROM restriction_releases WHERE steam_id=?', (identity,)).fetchone()[0]
    row = db.execute("SELECT source,origin_id,effective_at FROM restriction_provenance WHERE kind='local' AND steam_id=? AND event_id=?", (identity,event_id)).fetchone()
    if row is None:
        rows = db.execute('''SELECT source,id,created_at FROM permanent_ban_actions WHERE steam_id=? AND id=?
            UNION ALL SELECT source,id,confirmed_at FROM confirmed_kicks
            WHERE steam_id=? AND id=? AND confirmed_at IS NOT NULL''', (identity,event_id,identity,event_id)).fetchall()
        if len(rows) != 1: return None
        row = rows[0]
    source, origin, effective = row
    if type(effective) not in (int,float) or not math.isfinite(effective) or (released is not None and effective <= released): return None
    return {'source':source, 'originId':origin, 'effectiveAt':effective}


class KickExemptError(PunishmentExemptError):
    """The behavior policy explicitly excludes this ID from service kicks."""
    code = 'behavior_list_kick_exempt'


def map_key(status):
    if not isinstance(status, dict) or not isinstance(status.get('map'), str) or not status['map']:
        raise ValueError('missing map identity')
    experiences = status.get('experiences')
    if not isinstance(experiences, list) or not experiences or any(not isinstance(x, str) for x in experiences):
        raise ValueError('missing map experiences')
    return json.dumps([status['map'], sorted(experiences), status.get('alternator')], ensure_ascii=False)


class KickStore:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS map_scope(id INTEGER PRIMARY KEY, map_key TEXT NOT NULL,
            generation TEXT NOT NULL, observed REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS confirmed_kicks(id TEXT PRIMARY KEY, steam_id TEXT NOT NULL,
            name TEXT NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL,
            confirmed_at REAL, recorded_at REAL NOT NULL, scope_id TEXT);
          CREATE TABLE IF NOT EXISTS permanent_ban_actions(id TEXT PRIMARY KEY, steam_id TEXT NOT NULL,
            name TEXT NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS rejoin_restrictions(steam_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL, scope_id TEXT NOT NULL, name TEXT NOT NULL,
            reason TEXT NOT NULL, eligible_at REAL NOT NULL, status TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS native_ban_sync(steam_id TEXT PRIMARY KEY,
            requested_at REAL NOT NULL, status TEXT NOT NULL, updated_at REAL NOT NULL,
            receipt TEXT);
          CREATE TABLE IF NOT EXISTS behavior_players(steam_id TEXT PRIMARY KEY,
            name TEXT NOT NULL, enabled INTEGER NOT NULL, created REAL NOT NULL,
            next_team REAL NOT NULL, next_kill REAL NOT NULL, seen INTEGER NOT NULL,
            online INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS behavior_events(id TEXT PRIMARY KEY,
            steam_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL, created REAL NOT NULL, finished REAL,
            target TEXT, receipt TEXT, notice_status TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS reserved_players(steam_id TEXT PRIMARY KEY, observed REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS reserved_sync(id INTEGER PRIMARY KEY, observed REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS restriction_releases(id TEXT PRIMARY KEY,steam_id TEXT NOT NULL,
            released_at REAL NOT NULL,reason TEXT NOT NULL,previous_state TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS restriction_provenance(kind TEXT NOT NULL,steam_id TEXT NOT NULL,
            event_id TEXT NOT NULL,source TEXT NOT NULL,origin_id TEXT NOT NULL,effective_at REAL NOT NULL,
            PRIMARY KEY(kind,steam_id));
          CREATE TABLE IF NOT EXISTS silent_restrictions(steam_id TEXT PRIMARY KEY,origin_id TEXT NOT NULL);
        ''')
        ban_stats.initialize(self)

    def ban_statistics(self, now=None):
        return ban_stats.snapshot(self,now)

    def observe_native_bans(self, bans, now):
        return ban_stats.observe_native(self,bans,now)

    def close(self):
        self.db.close()

    def recover(self):
        with self.lock, self.db:
            self.db.execute("UPDATE rejoin_restrictions SET status='unknown' WHERE status='dispatching'")
            self.db.execute("UPDATE native_ban_sync SET status='unknown' WHERE status='dispatching'")
        self.promote_history()

    def promote_history(self):
        """Promote all confirmed IDs, preserving existing uncertain dispatch states."""
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('UPDATE rejoin_restrictions SET scope_id=?', (PERMANENT_SCOPE,))
            rows = self.db.execute('SELECT id,steam_id,name,reason,recorded_at,confirmed_at FROM confirmed_kicks ORDER BY recorded_at,rowid').fetchall()
            latest = {row[1]: row for row in rows}
            releases = dict(self.db.execute('SELECT steam_id,MAX(released_at) FROM restriction_releases GROUP BY steam_id'))
            for event, identity, name, reason, recorded, confirmed in latest.values():
                if identity in releases and (confirmed is None or confirmed <= releases[identity]):
                    continue
                self.db.execute('INSERT OR IGNORE INTO rejoin_restrictions VALUES(?,?,?,?,?,?,?)',
                    (identity, event, PERMANENT_SCOPE, name, reason, recorded, 'active'))

    def released_at(self, identity):
        with self.lock:
            return self.db.execute('SELECT MAX(released_at) FROM restriction_releases WHERE steam_id=?', (identity,)).fetchone()[0]

    def release(self, identity, reason, now, action_id=None):
        """Release old local enforcement; retain receipts and allow future offenses."""
        if not isinstance(identity,str) or len(identity)!=17 or not identity.isascii() or not identity.isdigit():
            raise ValueError('invalid SteamID64')
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if action_id is not None:
                old=self.db.execute('SELECT steam_id FROM restriction_releases WHERE id=?',(action_id,)).fetchone()
                if old:
                    if old[0]!=identity:raise ValueError('release_identity_conflict')
                    return action_id
            previous={table:self.db.execute('SELECT * FROM '+table+' WHERE steam_id=?',(identity,)).fetchall()
                      for table in ('rejoin_restrictions','native_ban_sync')}
            previous['restriction_provenance'] = self.db.execute('SELECT * FROM restriction_provenance WHERE steam_id=?',(identity,)).fetchall()
            previous['silent_restrictions'] = self.db.execute('SELECT * FROM silent_restrictions WHERE steam_id=?',(identity,)).fetchall()
            if any(row[6]=='dispatching' for row in previous['rejoin_restrictions']) or any(row[2]=='dispatching' for row in previous['native_ban_sync']):
                raise ValueError('punishment_dispatch_in_progress')
            action=action_id or uuid.uuid4().hex
            self.db.execute('INSERT INTO restriction_releases VALUES(?,?,?,?,?)',
                            (action,identity,now,reason,json.dumps(previous,ensure_ascii=False)))
            self.db.execute('DELETE FROM rejoin_restrictions WHERE steam_id=?',(identity,))
            self.db.execute('DELETE FROM native_ban_sync WHERE steam_id=?',(identity,))
            self.db.execute('DELETE FROM restriction_provenance WHERE steam_id=?',(identity,))
            self.db.execute('DELETE FROM silent_restrictions WHERE steam_id=?',(identity,))
            return action

    def is_silent(self, identity):
        with self.lock:
            marker=self.db.execute('SELECT origin_id FROM silent_restrictions WHERE steam_id=?',(identity,)).fetchone()
            current=self.db.execute('SELECT event_id FROM rejoin_restrictions WHERE steam_id=?',(identity,)).fetchone()
            origin=local_origin(self.db,identity,current[0]) if current else None
            return bool(marker and origin and marker[0]==origin['originId'])

    def sync(self, status, observed):
        # Retain map metadata for audit only. It no longer controls eligibility.
        key = map_key(status)
        with self.lock, self.db:
            old = self.db.execute('SELECT observed FROM map_scope WHERE id=1').fetchone()
            if old is None or observed >= old[0]:
                self.db.execute('INSERT OR REPLACE INTO map_scope VALUES(1,?,?,?)', (key, PERMANENT_SCOPE, observed))
        return PERMANENT_SCOPE

    def confirm(self, identity, name, reason, source, scope, now, event_id=None, historical=False, continuation_event_id=None):
        if not isinstance(identity, str) or len(identity) != 17 or not identity.isascii() or not identity.isdigit():
            raise ValueError('invalid SteamID64')
        event_id = event_id or uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            previous = self.db.execute('SELECT event_id FROM rejoin_restrictions WHERE steam_id=?',(identity,)).fetchone()
            origin = local_origin(self.db,identity,continuation_event_id) if previous and previous[0] == continuation_event_id else None
            inserted = self.db.execute('INSERT OR IGNORE INTO confirmed_kicks VALUES(?,?,?,?,?,?,?,?)',
                (event_id, identity, name, reason, source, None if historical else now, now, scope)).rowcount
            if inserted:
                self.db.execute('INSERT OR REPLACE INTO rejoin_restrictions VALUES(?,?,?,?,?,?,?)',
                    (identity, event_id, PERMANENT_SCOPE, name, reason, now + 30, 'active'))
                if not historical:
                    origin = origin or {'source':source,'originId':event_id,'effectiveAt':now}
                    self.db.execute("INSERT OR REPLACE INTO restriction_provenance VALUES('local',?,?,?,?,?)",
                        (identity,event_id,origin['source'],origin['originId'],origin['effectiveAt']))
                ban_stats.evidence(self,'kick:'+event_id,identity,None if historical else now,now)
        return event_id

    def activate_history(self, event_id, scope, now):
        # Explicit activation of a verified historical event; retained for callers.
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            event = self.db.execute('SELECT steam_id,name,reason FROM confirmed_kicks WHERE id=?', (event_id,)).fetchone()
            if event is None:
                raise ValueError('confirmed event missing')
            self.db.execute('INSERT OR REPLACE INTO rejoin_restrictions VALUES(?,?,?,?,?,?,?)',
                (event[0], event_id, PERMANENT_SCOPE, event[1], event[2], now, 'active'))

    def restrict(self, identity, name, reason, source, now, action_id=None):
        """Explicit permanent restriction, including offline players; never a kick receipt."""
        if not isinstance(identity, str) or len(identity) != 17 or not identity.isascii() or not identity.isdigit():
            raise ValueError('invalid SteamID64')
        action_id = action_id or uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            inserted = self.db.execute('INSERT OR IGNORE INTO permanent_ban_actions VALUES(?,?,?,?,?,?)',
                (action_id, identity, name, reason, source, now)).rowcount
            if inserted:
                self.db.execute('INSERT OR REPLACE INTO rejoin_restrictions VALUES(?,?,?,?,?,?,?)',
                    (identity, action_id, PERMANENT_SCOPE, name, reason, now, 'active'))
                self.db.execute("INSERT OR REPLACE INTO restriction_provenance VALUES('local',?,?,?,?,?)",
                    (identity,action_id,source,action_id,now))
                ban_stats.evidence(self,'action:'+action_id,identity,now,now)
        return action_id

    def candidates(self, players, scope, now):
        online = {str(p.get('steamId')) for p in players}
        with self.lock, self.db:
            # An observed absence proves the uncertain connection has ended.
            for (identity,) in self.db.execute("SELECT steam_id FROM rejoin_restrictions WHERE status='unknown'").fetchall():
                if identity not in online:
                    self.db.execute("UPDATE rejoin_restrictions SET status='active',eligible_at=? WHERE steam_id=? AND status='unknown'", (now, identity))
            rows = self.db.execute("SELECT steam_id,name,reason FROM rejoin_restrictions WHERE scope_id=? AND status='active' AND eligible_at<=?", (scope, now)).fetchall()
        return [row for row in rows if row[0] in online and not self.kick_exempt(row[0])]

    def kick_exempt(self, identity):
        if self.reserved_exempt(identity):
            return True
        with self.lock:
            return self.db.execute('SELECT 1 FROM behavior_players WHERE steam_id=? AND enabled=1', (identity,)).fetchone() is not None

    def reserved_exempt(self, identity):
        with self.lock:
            return self.db.execute('SELECT 1 FROM reserved_players WHERE steam_id=?', (identity,)).fetchone() is not None

    def require_not_exempt(self, identity):
        if self.reserved_exempt(identity):
            raise PunishmentExemptError('reserved_player_exempt')
        if self.kick_exempt(identity):
            raise KickExemptError('behavior_list_kick_exempt')

    def sync_reserved(self, identities, now):
        identities = list(identities)
        if any(not isinstance(i, str) or len(i) != 17 or not i.isascii() or not i.isdigit() for i in identities):
            raise ValueError('invalid reserved SteamID64')
        with self.lock, self.db:
            self.db.execute('DELETE FROM reserved_players')
            self.db.executemany('INSERT INTO reserved_players VALUES(?,?)', [(i, now) for i in set(identities)])
            self.db.execute('INSERT OR REPLACE INTO reserved_sync VALUES(1,?)', (now,))

    def claim(self, identity, scope, now):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            changed = self.db.execute("UPDATE rejoin_restrictions SET status='dispatching' WHERE steam_id=? AND scope_id=? AND status='active' AND eligible_at<=?", (identity, scope, now)).rowcount
            return bool(changed)

    def failed(self, identity, scope, now, unknown):
        with self.lock, self.db:
            self.db.execute('UPDATE rejoin_restrictions SET status=?,eligible_at=? WHERE steam_id=? AND scope_id=?',
                ('unknown' if unknown else 'active', now + 60, identity, scope))

    def request_native_bans(self, identities, now):
        """Explicit opt-in only; require every ID to have an existing restriction."""
        identities = list(dict.fromkeys(identities))
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for identity in identities:
                if not isinstance(identity, str) or len(identity) != 17 or not identity.isascii() or not identity.isdigit():
                    raise ValueError('invalid SteamID64')
                if self.db.execute('SELECT 1 FROM rejoin_restrictions WHERE steam_id=?', (identity,)).fetchone() is None:
                    raise ValueError('native ban requires existing permanent restriction')
            self.db.executemany('INSERT OR IGNORE INTO native_ban_sync VALUES(?,?,?,?,NULL)',
                               [(identity, now, 'pending', now) for identity in identities])

    def native_ban_state(self, identity):
        with self.lock:
            row = self.db.execute('SELECT status FROM native_ban_sync WHERE steam_id=?', (identity,)).fetchone()
        return row[0] if row else None

    def native_ban_update(self, identity, status, receipt=None):
        with self.lock, self.db:
            now=time.time()
            changed=self.db.execute('UPDATE native_ban_sync SET status=?,updated_at=?,receipt=? WHERE steam_id=?',
                            (status, now, json.dumps(receipt, ensure_ascii=False), identity)).rowcount
            if changed and status=='confirmed':
                ban_stats.evidence(self,'native:'+identity,identity,now,now)

    def snapshot(self):
        with self.lock:
            rows = self.db.execute('SELECT steam_id,name,reason,status,scope_id FROM rejoin_restrictions ORDER BY steam_id').fetchall()
            total = self.db.execute('SELECT count(*) FROM confirmed_kicks').fetchone()[0]
            counts = dict(self.db.execute('SELECT status,count(*) FROM native_ban_sync GROUP BY status').fetchall())
            reserved = [r[0] for r in self.db.execute('SELECT steam_id FROM reserved_players ORDER BY steam_id')]
            observed = self.db.execute('SELECT observed FROM reserved_sync WHERE id=1').fetchone()
        return {'enabled': True, 'mode': 'permanent', 'confirmedKickCount': total,
                'reservedExemptions': {'steamIds': reserved, 'count': len(reserved), 'lastSync': observed[0] if observed else None},
                'nativeBanSync': {'requestedCount': sum(counts.values()),
                                 'confirmedCount': counts.get('confirmed', 0),
                                 'pendingCount': counts.get('pending', 0),
                                 'unknownCount': counts.get('unknown', 0),
                                 'dispatchingCount': counts.get('dispatching', 0)}, 'restrictions': [dict(zip(
            ['steamId', 'name', 'reason', 'status', 'scopeId'], row)) for row in rows]}


try:
    from service.unknown_handoff import DispatchObserver
except ImportError:
    from unknown_handoff import DispatchObserver


class TrackingRcon:
    def __init__(self, base, store, source='monitor'):
        self.base, self.store, self.source = base, store, source
        self.last_event_id = None
        self.dispatch_observer = DispatchObserver()

    def _base_request(self, method, path, body=None):
        return self.dispatch_observer.call(self.base.request, method, path, body)

    def request(self, method, path, body=None):
        if method == 'POST' and path == '/v1/bans' and isinstance(body, dict):
            self.store.require_not_exempt(body.get('steamId'))
        if method == 'POST' and path.startswith('/v1/players/') and path.endswith('/kick'):
            return self._kick(path.split('/')[3], body)
        return self._base_request(method, path, body)

    def _native_record(self, identity):
        result = self._base_request('GET', '/v1/bans')
        entries = result.get('bans')
        if not isinstance(entries, list) or any(not isinstance(x, dict) or not isinstance(x.get('steamId'), str) for x in entries):
            raise ValueError('invalid native bans response')
        matches = [x for x in entries if x['steamId'] == identity]
        if len(matches) > 1: raise ValueError('duplicate native ban identity')
        return matches[0] if matches else None

    def _native_contains(self, identity):
        return self._native_record(identity) is not None

    def _remove(self, identity, body):
        if self.store.native_ban_state(identity) is not None:
            # Reconcile first: a previous timed-out request might have succeeded.
            if self._native_contains(identity):
                self.store.native_ban_update(identity, 'confirmed', {'verifiedInNativeList': True})
                # If a banned ID is nevertheless connected, retain kick enforcement.
            else:
                current = self.store.db.execute('SELECT event_id FROM rejoin_restrictions WHERE steam_id=?',(identity,)).fetchone()
                origin = local_origin(self.store.db,identity,current[0]) if current else None
                self.store.native_ban_update(identity, 'dispatching')
                response, failure = None, None
                try:
                    response = self._base_request('POST', '/v1/bans', {'steamId': identity, 'reason': body['reason']})
                except Exception as error:
                    failure = error
                if isinstance(failure, (PunishmentExemptError, PunishmentDeferredError)):
                    self.store.native_ban_update(identity, 'pending', {'preflightCode': failure.code})
                    raise failure
                try:
                    record = self._native_record(identity)
                    verified = record is not None
                except Exception:
                    self.store.native_ban_update(identity, 'unknown', {'response': response})
                    raise
                if verified:
                    fingerprint = native_fingerprint(record)
                    still = self.store.db.execute('SELECT event_id FROM rejoin_restrictions WHERE steam_id=?',(identity,)).fetchone()
                    if (origin and current == still and fingerprint and failure is None
                            and isinstance(response,dict) and response.get('ok') is True and not response.get('pending')):
                        with self.store.db:
                            self.store.db.execute("INSERT OR REPLACE INTO restriction_provenance VALUES('native',?,?,?,?,?)",
                                (identity,fingerprint,origin['source'],origin['originId'],origin['effectiveAt']))
                    # Ban-list membership proves the ban; absence proves removal.
                    self.store.native_ban_update(identity, 'confirmed', {'response': response, 'verifiedInNativeList': True})
                    players = self._base_request('GET', '/v1/players').get('players')
                    if not isinstance(players, list) or any(not isinstance(p, dict) for p in players):
                        raise ValueError('invalid players after native ban')
                    if any(str(p.get('steamId')) == identity for p in players):
                        raise ValueError('native ban recorded but removal not verified')
                    return {'ok': True, 'pending': False, '_nativeBan': True}
                known_rejection = failure is not None and isinstance(getattr(failure, 'status', None), int) and 400 <= failure.status < 500
                self.store.native_ban_update(identity, 'pending' if known_rejection else 'unknown', {'response': response})
                if known_rejection and failure.status in (400, 403, 405):
                    # An explicit refusal proves no ban was dispatched. Retain
                    # the original kick policy, with native sync still pending.
                    return self._base_request('POST', '/v1/players/' + identity + '/kick', body)
                if failure is not None:
                    raise failure
                raise ValueError('native ban not verified in live list')
        return self._base_request('POST', '/v1/players/' + identity + '/kick', body)

    def _kick(self, identity, body, expected_scope=None):
        # Serialize tracked removals against a local release in this process.
        with self.store.lock:
            return self._kick_locked(identity,body,expected_scope)

    def _kick_locked(self, identity, body, expected_scope=None):
        self.store.require_not_exempt(identity)
        scope = PERMANENT_SCOPE
        if expected_scope is not None and expected_scope != scope:
            return {'ok': False, 'guardCancelled': True}
        players = self._base_request('GET', '/v1/players').get('players')
        if not isinstance(players, list) or any(not isinstance(p, dict) for p in players):
            raise ValueError('invalid live players response')
        matches = [p for p in players if str(p.get('steamId')) == identity]
        if len(matches) != 1:
            if expected_scope is None:
                raise ValueError('target is not uniquely online')
            return {'ok': False, 'guardCancelled': True}
        if expected_scope is not None:
            kick_messages(matches[0]['name'], REJOIN_REASON)
        previous = self.store.db.execute('SELECT event_id FROM rejoin_restrictions WHERE steam_id=?',(identity,)).fetchone() if expected_scope is not None else None
        if expected_scope is not None and not self.store.claim(identity, scope, time.time()):
            return {'ok': False, 'guardCancelled': True}
        try:
            self.store.require_not_exempt(identity)
            response = self._remove(identity, body)
            if not isinstance(response, dict) or response.get('ok') is not True or response.get('pending'):
                raise ValueError('kick outcome not confirmed')
        except Exception as error:
            if expected_scope is not None:
                status = getattr(error, 'status', None)
                known_not_applied = isinstance(error, (PunishmentExemptError, PunishmentDeferredError)) or (isinstance(status, int) and 400 <= status < 500)
                self.store.failed(identity, scope, time.time(), unknown=not known_not_applied)
            raise
        # Persist confirmation before the caller sends any announcements.
        self.last_event_id = self.store.confirm(identity, matches[0]['name'], body['reason'],
            'native_ban' if response.get('_nativeBan') else ('rejoin' if expected_scope is not None else self.source), scope, time.time(),
            continuation_event_id=previous[0] if previous else None)
        return dict(response, _kickPlayerName=matches[0]['name'])


def check_rejoins(engine, players, now):
    store = engine.kick_store
    if store is None:
        return
    try:
        scope = PERMANENT_SCOPE
        for identity, name, reason in store.candidates(players, scope, now)[:5]:
            try:
                # Engine.poll holds engine.lock. Respect explicit-operation
                # ownership, including uncertain native-ban dispatches.
                lease = engine.db.execute('SELECT outcome FROM moderation_targets WHERE steam_id=?', (identity,)).fetchone()
                if lease and lease[0] in ('held', 'unknown'):
                    continue
                result = engine.rcon._kick(identity, {'reason': reason}, expected_scope=scope)
                if result.get('guardCancelled'):
                    continue
                if store.is_silent(identity):
                    LOG.info('silent_rejoin_kick_confirmed steam_id=%s', identity)
                    continue
                messages = kick_messages(result['_kickPlayerName'], '发送广告' if reason == '发送广告' else REJOIN_REASON)
                with engine.db:
                    for message in messages:
                        engine.add_job(message, time.time(), kind='management')
                LOG.info('rejoin_kick_confirmed steam_id=%s', identity)
            except Exception as error:
                LOG.warning('rejoin_kick_failed steam_id=%s error_type=%s', identity, type(error).__name__)
    except Exception as error:
        LOG.warning('kick_guard_check_failed error_type=%s', type(error).__name__)
