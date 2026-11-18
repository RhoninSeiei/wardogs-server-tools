"""Live, acknowledged automatic-dispatch evidence. Never reconstruct after restart."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import threading
import sqlite3
import uuid


class DispatchTrace:
    def __init__(self, identity):
        self.identity = identity
        self.safe = True
        self.writes = 0
        self.completed = False
        self.executor_entered = False

    def call(self, request, method, path, body=None):
        write = method not in ('GET', 'HEAD')
        if write:
            self.writes += 1
            allowed = (method == 'POST' and (
                path == '/v1/bans' and isinstance(body, dict) and body.get('steamId') == self.identity
                or path == '/v1/players/' + self.identity + '/kick'))
            if not allowed:
                self.safe = False
        try:
            result = request(method, path, body)
        except BaseException:
            if write:
                self.safe = False
            raise
        if write and (not isinstance(result, dict) or result.get('ok') is not True or result.get('pending') is not False):
            self.safe = False
        return result


class DispatchObserver:
    """A thread-local observer; all TrackingRcon base writes pass through call."""
    def __init__(self):
        self.local = threading.local()

    def call(self, request, method, path, body=None):
        trace = getattr(self.local, 'trace', None)
        return request(method, path, body) if trace is None else trace.call(request, method, path, body)

    @contextmanager
    def observe(self, identity):
        if getattr(self.local, 'trace', None) is not None:
            raise RuntimeError('nested_dispatch_trace')
        trace = DispatchTrace(identity)
        self.local.trace = trace
        try:
            yield trace
            trace.completed = True
        finally:
            self.local.trace = None


class UnknownHandoff:
    def __init__(self, engine):
        self.e = engine
        self.owner = uuid.uuid4().hex
        self.proofs = {}
        filename = engine.db.execute('PRAGMA database_list').fetchone()[2]
        self.directory = Path(filename).parent / '.burst-dispatch-locks'
        self.directory.mkdir(mode=0o700, exist_ok=True)
        with engine.db:
            engine.db.execute('''CREATE TABLE IF NOT EXISTS unknown_unban_handoffs(
                operation_id TEXT PRIMARY KEY, steam_id TEXT NOT NULL, previous_id TEXT NOT NULL,
                owner_token TEXT NOT NULL, attempt_token TEXT NOT NULL, transferred REAL NOT NULL,
                status TEXT NOT NULL)''')

    @contextmanager
    def target_lock(self, identity):
        path = self.directory / hashlib.sha256(identity.encode()).hexdigest()
        with path.open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def guard_snapshot(self, identity):
        guard = self.e.kick_store
        if guard is None:
            return None
        with guard.lock:
            return {
                'local': guard.db.execute('SELECT event_id,scope_id,status,eligible_at FROM rejoin_restrictions WHERE steam_id=?', (identity,)).fetchone(),
                'native': guard.db.execute('SELECT requested_at,status,updated_at,receipt FROM native_ban_sync WHERE steam_id=?', (identity,)).fetchone(),
                'release': guard.released_at(identity),
            }

    def tracked_executor(self):
        # A custom executor can issue writes without passing through our observer.
        # Fail closed rather than interpreting an empty trace as no remote work.
        try:
            from service.kick_guard import TrackingRcon
            from service.rcon import Rcon
        except ImportError:
            from kick_guard import TrackingRcon
            from rcon import Rcon
        tracked = self.e.rcon
        if type(tracked) is not TrackingRcon or type(tracked.base) is not Rcon:
            return False
        for name in ('request', '_base_request', '_native_record', '_native_contains', '_remove', '_kick', '_kick_locked'):
            if getattr(getattr(tracked, name), '__func__', None) is not getattr(TrackingRcon, name):
                return False
        return (type(tracked.dispatch_observer) is DispatchObserver
                and getattr(tracked.base.request, '__func__', None) is Rcon.request
                and getattr(tracked.base._request, '__func__', None) is Rcon._request)

    def publish(self, aid, identity, attempt, initial, trace):
        # Called only after trace context exits, while worker still owns both locks.
        self.proofs.pop(aid, None)
        if initial is None or initial['local'] is not None or initial['native'] is not None:
            return
        if not self.tracked_executor() or not trace.completed or not trace.safe or (trace.executor_entered and trace.writes == 0):
            return
        row = self.e.db.execute('SELECT steam_id,status,created,updated,error FROM burst_actions WHERE id=?', (aid,)).fetchone()
        lease = self.e.db.execute('SELECT operation_id,outcome,expires FROM moderation_targets WHERE steam_id=?', (identity,)).fetchone()
        if row is None or row[0] != identity or row[1] != 'unknown' or lease != (aid, 'unknown', None):
            return
        final = self.guard_snapshot(identity)
        if final is None or final['release'] != initial['release']:
            return
        if final['local'] is None or final['local'][0:2] != (aid, 'permanent') or final['local'][2] == 'dispatching':
            return
        if final['native'] is not None and final['native'][1] == 'dispatching':
            return
        # Dropping evidence only disables reconciliation; never drops a barrier.
        if len(self.proofs) >= 1024:
            self.proofs.pop(next(iter(self.proofs)))
        self.proofs[aid] = {'owner': self.owner, 'attempt': attempt, 'identity': identity,
                            'row': row, 'guard': final, 'writes': trace.writes}

    def no_other_tasks(self, identity, previous, current, created):
        db = self.e.db
        for table in ('moderation_operations', 'admin_bans', 'admin_restrictions', 'admin_cycle_adds'):
            if db.execute('SELECT 1 FROM '+table+" WHERE steam_id=? AND (created>=? OR status IN ('accepted','resolving','dispatching','kick_dispatching','unknown')) LIMIT 1", (identity, created)).fetchone():
                return False
        if db.execute("SELECT 1 FROM burst_actions WHERE steam_id=? AND id!=? AND (created>=? OR status IN ('accepted','dispatching','unknown')) LIMIT 1", (identity, previous, created)).fetchone():
            return False
        if db.execute("SELECT 1 FROM admin_unbans WHERE steam_id=? AND id!=? AND status IN ('accepted','resolving','unban_dispatching','unknown') LIMIT 1", (identity, current)).fetchone():
            return False
        if db.execute("SELECT 1 FROM jobs WHERE recipient=? AND kind IN ('kd_kick','kill_cap_kick') AND (created>=? OR status IN ('queued','sending','unknown')) LIMIT 1", (identity, created)).fetchone():
            return False
        for filename, table, timestamp in (
                ('weapon-kick.sqlite3', 'actions', 'created'),
                ('name-ban-watch.sqlite3', 'watches', 'armed'),
                ('cycle-exit-bans.sqlite3', 'tasks', 'armed')):
            path = self.directory.parent / filename
            if not path.exists():
                continue
            try:
                connection = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=0.2)
                try:
                    connection.execute('PRAGMA query_only=ON')
                    busy = connection.execute('SELECT 1 FROM '+table+" WHERE steam_id=? AND ("+timestamp+">=? OR status IN ('accepted','pending','watching','bound','dispatching','unknown')) LIMIT 1", (identity, created)).fetchone()
                    if busy:
                        return False
                finally:
                    connection.close()
            except sqlite3.Error:
                return False
        return True

    def eligible(self, previous, identity, current):
        proof = self.proofs.get(previous)
        if not proof or proof['owner'] != self.owner or proof['identity'] != identity:
            return None
        row = self.e.db.execute('SELECT steam_id,status,created,updated,error FROM burst_actions WHERE id=?', (previous,)).fetchone()
        if row != proof['row'] or self.guard_snapshot(identity) != proof['guard']:
            return None
        if not self.no_other_tasks(identity, previous, current, row[2]):
            return None
        return proof

    def transfer(self, current, identity, now):
        """Caller owns engine.lock and existing submit SQLite transaction."""
        lease = self.e.db.execute('SELECT operation_id,outcome,expires FROM moderation_targets WHERE steam_id=?', (identity,)).fetchone()
        if lease is None or lease[1:] != ('unknown', None):
            return
        with self.target_lock(identity) as locked:
            if not locked:
                return
            proof = self.eligible(lease[0], identity, current)
            if proof is None:
                return
            changed = self.e.db.execute("UPDATE moderation_targets SET operation_id=?,outcome='held' WHERE steam_id=? AND operation_id=? AND outcome='unknown' AND expires IS NULL", (current, identity, lease[0])).rowcount
            if changed != 1:
                raise RuntimeError('unknown_handoff_cas_failed')
            self.e.db.execute('INSERT INTO unknown_unban_handoffs VALUES(?,?,?,?,?,?,?)',
                              (current, identity, lease[0], self.owner, proof['attempt'], now, 'transferred'))
            self.e.moderation._audit(lease[0], 'unknown_lease_transferred_for_unban', now, current)
            self.e.moderation._audit(current, 'unknown_lease_received_for_unban', now, lease[0])

    def record(self, current):
        return self.e.db.execute('SELECT steam_id,previous_id,owner_token,attempt_token,transferred,status FROM unknown_unban_handoffs WHERE operation_id=?', (current,)).fetchone()

    @contextmanager
    def release_lock(self, current):
        record = self.record(current)
        if record is None:
            yield True
        else:
            with self.target_lock(record[0]) as locked:
                yield locked

    def restore(self, current, now):
        record = self.record(current)
        if record is None:
            return
        identity, previous = record[:2]
        self.proofs.pop(previous, None)
        # Preserve the old barrier, but never replace a different owner.
        self.e.db.execute("UPDATE moderation_targets SET operation_id=?,outcome='unknown',expires=NULL WHERE steam_id=? AND operation_id=? AND outcome='held'", (previous, identity, current))
        self.e.db.execute("UPDATE unknown_unban_handoffs SET status='blocked' WHERE operation_id=?", (current,))
        self.e.moderation._audit(current, 'unknown_handoff_blocked', now)

    def revalidate(self, current, now):
        # Caller owns engine + guard + target locks and the SQLite transaction.
        record = self.record(current)
        if record is None:
            return True
        identity, previous, owner, attempt, _, status = record
        proof = self.eligible(previous, identity, current)
        lease = self.e.db.execute('SELECT operation_id,outcome FROM moderation_targets WHERE steam_id=?', (identity,)).fetchone()
        valid = status == 'transferred' and owner == self.owner and proof is not None and proof['attempt'] == attempt and lease == (current, 'held')
        if valid:
            self.e.db.execute("UPDATE unknown_unban_handoffs SET status='verified' WHERE operation_id=?", (current,))
            self.e.moderation._audit(current, 'unknown_handoff_verified_before_release', now)
            return True
        self.restore(current, now)
        return False

    def public(self, current):
        row = self.record(current)
        if row is None:
            return None
        return {'previousOperationId': row[1], 'source': 'auto_burst', 'previousStatus': 'unknown',
                'status': row[5], 'transferredAt': row[4]}
