"""Indexed, read-only kill history queries over the local event archive."""
import json
import math
from pathlib import Path
import re
import sqlite3
import time
if __package__:
    from .event_monitor import INTERVAL
else:
    from event_monitor import INTERVAL


class QueryError(ValueError):
    def __init__(self, status, code, **extra):
        self.status = status
        self.body = {'error': {'code': code}, **extra}


def text(value):
    return value if isinstance(value, str) else None


def steam(value):
    return value if isinstance(value, str) and re.fullmatch(r'[0-9]{17}', value) else None


def number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def index_event(db, server, key, body):
    obj = json.loads(body)
    killer, victim = text(obj.get('killerName')), text(obj.get('victimName'))
    db.execute('INSERT OR IGNORE INTO kill_records(server,event_key,killer_sid,killer_name,killer_fold,victim_sid,victim_name,victim_fold) VALUES(?,?,?,?,?,?,?,?)',
               (server, key, steam(obj.get('killerSteamId')), killer, killer.casefold() if killer else None,
                steam(obj.get('victimSteamId')), victim, victim.casefold() if victim else None))


def initialize(db):
    with db:
        db.executescript('''
          CREATE INDEX IF NOT EXISTS events_type ON events(type);
          CREATE TABLE IF NOT EXISTS kill_records(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            server TEXT NOT NULL,event_key TEXT NOT NULL,
            killer_sid TEXT,killer_name TEXT,killer_fold TEXT,
            victim_sid TEXT,victim_name TEXT,victim_fold TEXT,
            UNIQUE(server,event_key));
          CREATE INDEX IF NOT EXISTS kills_killer ON kill_records(killer_sid,seq DESC);
          CREATE INDEX IF NOT EXISTS kills_victim ON kill_records(victim_sid,seq DESC);
          CREATE INDEX IF NOT EXISTS kills_killer_name ON kill_records(killer_fold,killer_sid);
          CREATE INDEX IF NOT EXISTS kills_victim_name ON kill_records(victim_fold,victim_sid);
        ''')
        # Rebuild missing indexes for old archives without changing original events.
        rows = db.execute('''SELECT e.server,e.event_key,e.body FROM events e
            LEFT JOIN kill_records k ON e.server=k.server AND e.event_key=k.event_key
            WHERE e.type='killed' AND k.seq IS NULL ORDER BY e.received,e.rowid''')
        for server, key, body in rows:
            index_event(db, server, key, body)


def parameters(params, player_optional=False):
    allowed = {'steamId', 'name', 'direction', 'limit', 'before'}
    if player_optional:
        allowed.remove('direction')
    if set(params) - allowed or any(len(v) != 1 for v in params.values()):
        raise QueryError(400, 'invalid_query')
    values = {k: v[0] for k, v in params.items()}
    if ('steamId' in values and 'name' in values) or (not player_optional and not ('steamId' in values or 'name' in values)):
        raise QueryError(400, 'specify_one_player')
    if 'steamId' in values and steam(values['steamId']) is None:
        raise QueryError(400, 'invalid_steam_id')
    if 'name' in values:
        name = values['name']
        if not name or name.isspace() or len(name) > 128 or any(ord(c) < 32 for c in name):
            raise QueryError(400, 'invalid_name')
    values.setdefault('direction', 'both')
    if values['direction'] not in ('both', 'kills', 'deaths'):
        raise QueryError(400, 'invalid_direction')
    for key, default, maximum in [('limit', '20', 100), ('before', None, 9223372036854775807)]:
        value = values.get(key, default)
        if value is not None:
            if not re.fullmatch(r'[0-9]{1,19}', value) or not 1 <= int(value) <= maximum:
                raise QueryError(400, 'invalid_' + key)
            values[key] = int(value)
    return values


def query(path, params, incident_mode=False):
    opts = parameters(params, player_optional=incident_mode)
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic() + 1
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 2000)
    try:
        conn.execute('BEGIN')
        sid = opts.get('steamId')
        if sid is None and 'name' in opts:
            fold = opts['name'].casefold()
            candidates = conn.execute('''SELECT killer_sid AS sid FROM kill_records WHERE killer_fold=? AND killer_sid IS NOT NULL
                UNION SELECT victim_sid AS sid FROM kill_records WHERE victim_fold=? AND victim_sid IS NOT NULL LIMIT 21''', (fold, fold)).fetchall()
            if not candidates:
                raise QueryError(404, 'player_not_found')
            if len(candidates) > 1:
                raise QueryError(409, 'ambiguous_player', candidates=[{'steamId': x['sid'], 'name': opts['name']} for x in candidates[:20]], candidatesTruncated=len(candidates)>20)
            sid = candidates[0]['sid']
        clauses, args = [], []
        if sid is None:
            pass
        elif opts['direction'] == 'both':
            clauses.append('(k.killer_sid=? OR k.victim_sid=?)')
            args.extend((sid, sid))
        else:
            clauses.append('k.' + ('killer_sid' if opts['direction'] == 'kills' else 'victim_sid') + '=?')
            args.append(sid)
        if incident_mode:
            clauses.append('k.seq IN (SELECT archive_seq FROM helicopter_incidents)')
        if opts.get('before') is not None:
            clauses.append('k.seq<?')
            args.append(opts['before'])
        args.append(opts['limit'] + 1)
        rows = conn.execute('''SELECT k.seq,e.server,e.event_key,e.received,e.body FROM kill_records k
            JOIN events e ON e.server=k.server AND e.event_key=k.event_key WHERE ''' + ' AND '.join(clauses) + ' ORDER BY k.seq DESC LIMIT ?', args).fetchall()
        events = []
        for row in rows[:opts['limit']]:
            obj = json.loads(row['body'])
            killer, victim = steam(obj.get('killerSteamId')), steam(obj.get('victimSteamId'))
            def player(prefix):
                return {'steamId': steam(obj.get(prefix+'SteamId')), 'name': text(obj.get(prefix+'Name')), 'faction': text(obj.get(prefix+'Faction'))}
            events.append({'eventId': text(obj.get('eventId')), 'archiveId': str(row['seq']),
                'serverId': row['server'], 'matchId': text(obj.get('matchId')), 'map': text(obj.get('mapName')),
                'receivedAt': row['received'], 'eventTime': number(obj.get('eventTime')),
                'relation': 'suicide' if killer == victim and killer is not None else ('kill' if killer == sid else 'death'),
                'killer': player('killer'), 'victim': player('victim'), 'weapon': text(obj.get('cause')),
                'distance': number(obj.get('distance')), 'teamkill': obj.get('teamkill') if type(obj.get('teamkill')) is bool else None})
        latest = conn.execute('SELECT MAX(received) FROM events').fetchone()[0]
        result = {'player': {'steamId': sid} if sid else None, 'direction': opts['direction'], 'order': 'archive_desc',
                'distanceUnit': None, 'queriedAt': time.time(), 'archiveLastReceivedAt': latest,
                'events': events, 'nextBefore': str(rows[opts['limit']-1]['seq']) if len(rows) > opts['limit'] else None}
        if incident_mode:
            checkpoint = conn.execute('SELECT through_rowid,last_checked FROM event_monitor WHERE id=1').fetchone()
            latest_seq = conn.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]
            result['classification'] = 'helicopter_self_explosion'
            result['monitor'] = {'intervalSeconds':INTERVAL,'lastCheckedAt':checkpoint[1],
                                 'throughEventRowId':str(checkpoint[0]),'latestEventRowId':str(latest_seq),
                                 'caughtUp':checkpoint[0] >= latest_seq}
            result.pop('direction')
        return result
    finally:
        conn.close()
