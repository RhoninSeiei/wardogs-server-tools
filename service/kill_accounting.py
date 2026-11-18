"""Local, fail-closed exclusion of confirmed barbed-wire kills from automatic rules."""
import json
import math
from pathlib import Path
import re
import sqlite3
import time

BARBED_WIRE = 'Id.Buildable.BarbedWire'


def barbed_wire(event):
    return isinstance(event, dict) and event.get('cause') == BARBED_WIRE


def round_identity(connection, boundary, server):
    """Resolve identity inside the anchored round, never from kills or older rounds.

    The first PreMatch event may precede matchId creation. Later waiting/playing
    lifecycle events can fill missing fields; every supplied field must agree.
    The original boundary remains the start of kill accounting.
    """
    identity = {}
    for body, in connection.execute("""SELECT body FROM events WHERE server=?
            AND rowid>=? AND type='match_state' ORDER BY rowid""", (server, boundary)):
        event = json.loads(body)
        state = event.get('state')
        if not isinstance(state, str) or not any(
                state == phase or state.startswith(phase + '.')
                for phase in ('MatchState.PreMatch', 'MatchState.Playing')):
            continue
        for field in ('matchId', 'mapName'):
            value = event.get(field)
            if value is None or value == '':
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError('round_identity_invalid')
            if field in identity and identity[field] != value:
                raise ValueError('round_identity_conflict')
            identity[field] = value
    if not identity.get('matchId') or not identity.get('mapName'):
        raise ValueError('round_identity_missing')
    return identity


class KillAccounting:
    def __init__(self, path):
        self.path = Path(path)
        self.last_error = None

    def counts(self, now=None):
        now = time.time() if now is None else now
        c = None
        try:
            c = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)
            c.execute('BEGIN')
            sync = c.execute("SELECT last_success,error FROM sync_state WHERE kind='events'").fetchone()
            if (not sync or sync[1] is not None or type(sync[0]) not in (int,float)
                    or not math.isfinite(sync[0]) or not 0 <= now-sync[0] <= 15):
                raise ValueError('events_not_fresh')
            cursor = c.execute('SELECT generation,after_seq,latest_seq FROM sync_cursor WHERE id=1').fetchone()
            if not cursor or not cursor[0] or type(cursor[1]) is not int or type(cursor[2]) is not int or cursor[1] < 0 or cursor[1] != cursor[2]:
                raise ValueError('events_not_caught_up')
            # matchId can survive repeated rounds and map changes. A recorded
            # PreMatch boundary, rather than matchId alone, scopes the deduction.
            boundary = c.execute("""SELECT rowid,server,body FROM events WHERE type='match_state'
                AND json_extract(body,'$.state')='MatchState.PreMatch'
                ORDER BY rowid DESC LIMIT 1""").fetchone()
            if not boundary: raise ValueError('round_boundary_missing')
            seq, server, _ = boundary
            if c.execute("SELECT 1 FROM events WHERE server=? AND rowid>? AND type='match_ended' LIMIT 1",(server,seq)).fetchone():
                raise ValueError('round_ended')
            # Multiple server archives must never be merged into one scoreboard.
            if c.execute('SELECT 1 FROM events WHERE rowid>? AND server!=? LIMIT 1',(seq,server)).fetchone():
                raise ValueError('ambiguous_server')
            start = round_identity(c, seq, server)
            result = {}
            for body, in c.execute("""SELECT body FROM events WHERE server=? AND rowid>?
                    AND type='killed' AND json_extract(body,'$.cause')=?""",(server,seq,BARBED_WIRE)):
                event = json.loads(body)
                sid, victim = event.get('killerSteamId'), event.get('victimSteamId')
                if (not isinstance(sid,str) or not re.fullmatch('[0-9]{17}',sid)
                        or not isinstance(victim,str) or not re.fullmatch('[0-9]{17}',victim)
                        or sid == victim): continue
                if event.get('matchId') != start['matchId'] or event.get('mapName') != start['mapName']: continue
                result[sid] = result.get(sid,0) + 1
            self.last_error = None
            return result
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as error:
            self.last_error = str(error) if isinstance(error,ValueError) else type(error).__name__
            return None
        finally:
            if c is not None: c.close()

    def adjust(self, player, counts):
        result = dict(player)
        if counts is None:
            # Unknown exclusion counts cannot authorize an automatic punishment.
            result['kills'] = None
            return result
        kills = player.get('kills')
        try:
            valid = type(kills) in (int,float) and math.isfinite(kills) and kills >= 0 and int(kills) == kills
        except (ValueError, OverflowError):
            valid = False
        if not valid: return result
        result['rawKills'] = kills
        result['excludedBarbedWireKills'] = min(kills, counts.get(str(player.get('steamId')),0))
        result['kills'] = max(0, kills-counts.get(str(player.get('steamId')),0))
        return result
