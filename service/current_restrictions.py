"""Admin-only current restriction projection, with explicit provenance uncertainty."""
import math
from pathlib import Path
import sqlite3
import time

try:
    from service.moderation_ops import ContractError, validate_admin_actor
    from service.player_history import identity
    from service.admin_tools import roster, native_bans
    from service.kick_guard import local_origin, native_fingerprint
except ImportError:
    from moderation_ops import ContractError, validate_admin_actor
    from player_history import identity
    from admin_tools import roster, native_bans
    from kick_guard import local_origin, native_fingerprint

AUTOMATIC = {'auto_kd', 'auto_native', 'auto_burst', 'auto_weapon'}
MANUAL = {'admin', 'admin_offline', 'manual', 'manual_tk', 'manual_on_exit'}


def source(value, active):
    if not active: return {'category':'none', 'code':None}
    code = value.get('source') if value else None
    category = 'automatic' if code in AUTOMATIC else 'manual' if code in MANUAL else 'report' if code == 'report' else 'unknown'
    return {'category':category, 'code':code if category != 'unknown' else None}


class CompleteRead:
    """Do not turn explicitly partial remote lists into absence."""
    def __init__(self, engine): self.rcon = self; self.base = engine.rcon
    def request(self, method, path):
        data = self.base.request(method, path)
        if not isinstance(data,dict) or data.get('complete') is False or data.get('truncated') or data.get('hasMore'):
            raise ValueError('partial_remote_list')
        rows = data.get('players' if path.endswith('players') else 'bans')
        if 'count' in data and (type(data['count']) is not int or not isinstance(rows,list) or data['count'] != len(rows)):
            raise ValueError('remote_count_mismatch')
        return data


class CurrentRestrictions:
    def __init__(self, engine, state_path):
        self.engine = engine
        self.state_path = Path(state_path)
        self.guard_path = self.state_path.with_name('kick_guard.sqlite3')

    def validate(self, payload, now):
        if not isinstance(payload,dict) or set(payload) != {'steamIds','actor','requestedAt'}:
            raise ContractError(400,'invalid_current_restrictions_fields')
        validate_admin_actor(payload['actor'],payload['requestedAt'],self.engine.config,now)
        ids = payload['steamIds']
        if (not isinstance(ids,list) or not 1 <= len(ids) <= 100
                or any(not identity(sid) for sid in ids) or len(set(ids)) != len(ids)):
            raise ContractError(400,'invalid_current_restrictions_ids')

    def local(self, ids):
        c = sqlite3.connect(self.guard_path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)
        try:
            c.execute('ATTACH DATABASE ? AS state',(self.state_path.resolve().as_uri()+'?mode=ro',))
            c.execute('PRAGMA query_only=ON'); c.execute('BEGIN')
            result = {}
            for sid in ids:
                restriction = c.execute('SELECT event_id,scope_id,status FROM rejoin_restrictions WHERE steam_id=?',(sid,)).fetchone()
                sync = c.execute('SELECT requested_at,status,updated_at,receipt FROM native_ban_sync WHERE steam_id=?',(sid,)).fetchone()
                provenance = c.execute('SELECT kind,event_id,source,origin_id,effective_at FROM restriction_provenance WHERE steam_id=? ORDER BY kind',(sid,)).fetchall()
                released = c.execute('SELECT MAX(released_at) FROM restriction_releases WHERE steam_id=?',(sid,)).fetchone()[0]
                lease = c.execute('SELECT operation_id,outcome,expires FROM state.moderation_targets WHERE steam_id=?',(sid,)).fetchone()
                if lease and lease[1] in ('held','unknown'):
                    raise ContractError(503,'current_restrictions_changing')
                if restriction and (restriction[1] != 'permanent' or restriction[2] not in ('active','dispatching','unknown')):
                    raise ValueError('invalid_local_restriction')
                if sync and sync[1] not in ('pending','dispatching','confirmed','unknown'):
                    raise ValueError('invalid_native_sync')
                origin = local_origin(c,sid,restriction[0]) if restriction else None
                result[sid] = {'restriction':restriction, 'sync':sync, 'provenance':provenance,
                               'released':released, 'lease':lease, 'origin':origin}
            return result
        finally: c.close()

    def query(self, payload, now=None):
        clock = time.time if now is None else lambda:now
        self.validate(payload,clock())
        if self.engine.kick_store is None: raise ContractError(503,'current_restrictions_not_configured')
        started = clock(); ids = payload['steamIds']
        try:
            before = self.local(ids)
            reader = CompleteRead(self.engine)
            live = roster(reader); online_at = clock()
            bans = native_bans(reader); native_at = clock()
            after = self.local(ids); local_at = clock()
            if before != after: raise ContractError(503,'current_restrictions_changing')
        except ContractError: raise
        except Exception: raise ContractError(503,'current_restrictions_unavailable') from None
        self.validate(payload,clock())
        observed = clock()
        if not 0 <= observed-started <= 15:
            raise ContractError(503,'current_restrictions_not_fresh')
        players = []
        for sid in ids:
            row = after[sid]; local = row['restriction'] is not None; native = sid in bans
            local_source = source(row['origin'],local)
            native_origin = None
            fingerprint = native_fingerprint(bans[sid]) if native else None
            if fingerprint:
                for kind,event,code,origin,effective in row['provenance']:
                    if (kind == 'native' and event == fingerprint and type(effective) in (int,float)
                            and math.isfinite(effective) and (row['released'] is None or effective > row['released'])):
                        native_origin = {'source':code}
            native_source = source(native_origin,native)
            categories = [x['category'] for x in (local_source,native_source) if x['category'] != 'none']
            automatic = True if 'automatic' in categories else None if 'unknown' in categories else False
            players.append({'steamId':sid, 'online':sid in live, 'onlineObservedAt':online_at,
                'localRestricted':local, 'localRestrictionStatus':row['restriction'][2] if local else None,
                'localObservedAt':local_at, 'nativeBanned':native, 'nativeObservedAt':native_at,
                'nativeSyncState':row['sync'][1] if row['sync'] else None,
                'currentAutomaticRestriction':automatic, 'sourceComplete':'unknown' not in categories,
                'localSource':local_source, 'nativeSource':native_source})
        return {'observedAt':observed,'complete':True,'players':players}
