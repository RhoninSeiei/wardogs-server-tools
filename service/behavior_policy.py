"""Explicit SteamID policy: independent randomized team changes and console kills."""
import json
import logging
import random
import time
import uuid
try:
    from service.rcon import PunishmentExemptError, PunishmentDeferredError
except ImportError:
    from rcon import PunishmentExemptError, PunishmentDeferredError

LOG = logging.getLogger('wardogs')
INTERVALS = {'team': (30, 300), 'kill': (90, 600)}


class BehaviorPolicy:
    def __init__(self, store, rcon, rng=None):
        self.store, self.rcon = store, rcon
        self.rng = rng or random.SystemRandom()
        self.retry_after = 0

    def recover(self):
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE behavior_events SET status='unknown' WHERE status='dispatching'")
            self.store.db.execute("UPDATE behavior_events SET notice_status='unknown' WHERE notice_status='sending'")

    def add(self, identity, name, now=None):
        now = time.time() if now is None else now
        if not isinstance(identity, str) or len(identity) != 17 or not identity.isascii() or not identity.isdigit():
            raise ValueError('invalid SteamID64')
        if not isinstance(name, str) or not name or len(name) > 100 or any(not c.isprintable() for c in name):
            raise ValueError('invalid name')
        with self.store.lock, self.store.db:
            # Repeated registration must preserve timers and first-run progress.
            self.store.db.execute('INSERT INTO behavior_players VALUES(?,?,1,?,?,?,0,0) '
                'ON CONFLICT(steam_id) DO UPDATE SET name=excluded.name,enabled=1',
                (identity, name, now, now, now))

    def disable(self, identity):
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE behavior_players SET enabled=0 WHERE steam_id=?', (identity,))

    def snapshot(self):
        with self.store.lock:
            rows = self.store.db.execute('SELECT steam_id,name,enabled,next_team,next_kill,online FROM behavior_players ORDER BY created').fetchall()
            counts = dict(self.store.db.execute('SELECT status,count(*) FROM behavior_events GROUP BY status'))
        return {'players': [dict(zip(['steamId','name','enabled','nextTeamAt','nextKillAt','online'], r)) for r in rows],
                'eventCounts': counts, 'teamIntervalSeconds': list(INTERVALS['team']),
                'killIntervalSeconds': list(INTERVALS['kill']), 'inventorySupported': False,
                'noticeChannel': 'broadcast'}

    def observe(self, players, now):
        grouped = {}
        for player in players:
            grouped.setdefault(str(player.get('steamId')), []).append(player)
        with self.store.lock, self.store.db:
            rows = self.store.db.execute('SELECT steam_id,seen,online FROM behavior_players WHERE enabled=1').fetchall()
            for identity, seen, online in rows:
                matches = grouped.get(identity, [])
                if len(matches) != 1:
                    self.store.db.execute('UPDATE behavior_players SET online=0 WHERE steam_id=?', (identity,))
                    continue
                name = ''.join(c for c in str(matches[0].get('name','')) if c.isprintable())[:100] or identity
                if not online:
                    team = now + self.rng.randint(*INTERVALS['team']) if seen else now
                    kill = now + self.rng.randint(*INTERVALS['kill']) if seen else now
                    self.store.db.execute('UPDATE behavior_players SET next_team=?,next_kill=? WHERE steam_id=?', (team,kill,identity))
                self.store.db.execute('UPDATE behavior_players SET online=1,seen=1,name=? WHERE steam_id=?', (name,identity))

    def _players(self):
        players = self.rcon.request('GET', '/v1/players').get('players')
        if not isinstance(players, list) or any(not isinstance(p, dict) or not p.get('steamId') for p in players):
            raise ValueError('invalid players response')
        return players

    def _claim(self, identity, name, kind, target, now):
        column = 'next_team' if kind == 'team' else 'next_kill'
        with self.store.lock, self.store.db:
            if self.store.reserved_exempt(identity):
                return None
            next_at = now + self.rng.randint(*INTERVALS[kind])
            changed = self.store.db.execute(f'UPDATE behavior_players SET {column}=? WHERE steam_id=? AND enabled=1 AND online=1 AND {column}<=?', (next_at,identity,now)).rowcount
            if not changed:
                return None
            event = uuid.uuid4().hex
            self.store.db.execute('INSERT INTO behavior_events VALUES(?,?,?,?,?,?,NULL,?,NULL,?)',
                                 (event,identity,name,kind,'dispatching',now,target,'not_needed'))
        return event

    def _finish(self, event, status, receipt, now):
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE behavior_events SET status=?,receipt=?,finished=? WHERE id=?',
                                 (status,json.dumps(receipt,ensure_ascii=False),now,event))

    def tick(self, now=None):
        now = time.time() if now is None else now
        if now < self.retry_after:
            return []
        with self.store.lock:
            rows = self.store.db.execute('SELECT steam_id FROM behavior_players WHERE enabled=1 AND online=1 AND (next_team<=? OR next_kill<=?) ORDER BY created LIMIT 5', (now,now)).fetchall()
        completed = []
        for (identity,) in rows:
            if self.store.reserved_exempt(identity):
                continue
            for kind in ('team','kill'):
                try:
                    column = 'next_team' if kind == 'team' else 'next_kill'
                    with self.store.lock:
                        due = self.store.db.execute(f'SELECT 1 FROM behavior_players WHERE steam_id=? AND enabled=1 AND online=1 AND {column}<=?', (identity,now)).fetchone()
                    if not due:
                        continue
                    players = self._players()
                    matches = [p for p in players if str(p['steamId']) == identity]
                    if len(matches) != 1:
                        self.observe(players,now)
                        continue
                    player = matches[0]
                    name = ''.join(c for c in str(player.get('name','')) if c.isprintable())[:100] or identity
                    target = None
                    if kind == 'team':
                        scores = self.rcon.request('GET','/v1/status').get('factionScores')
                        if not isinstance(scores,list) or any(not isinstance(s,dict) or not isinstance(s.get('name'),str) for s in scores):
                            raise ValueError('invalid factions response')
                        factions = list(dict.fromkeys(s['name'] for s in scores))
                        if player.get('faction') not in factions:
                            raise ValueError('unknown current faction')
                        choices = [f for f in factions if f != player['faction']]
                        if not choices:
                            raise ValueError('no alternative faction')
                        target = self.rng.choice(choices)
                    event = self._claim(identity,name,kind,target,now)
                    if event is None:
                        continue
                    try:
                        path = '/v1/players/'+identity
                        result = self.rcon.request('PATCH' if kind == 'team' else 'POST', path if kind == 'team' else path+'/kill', {'faction':target} if kind == 'team' else None)
                        if not isinstance(result,dict) or result.get('ok') is not True or result.get('pending'):
                            raise ValueError('operation outcome not confirmed')
                        if kind == 'team':
                            after = [p for p in self._players() if str(p['steamId']) == identity]
                            if len(after)!=1 or after[0].get('faction')!=target:
                                raise ValueError('faction readback not confirmed')
                    except PunishmentExemptError:
                        self._finish(event,'cancelled',{'code':'reserved_player_exempt'},now)
                        completed.append({'eventId':event,'steamId':identity,'kind':kind,'status':'cancelled'})
                        continue
                    except PunishmentDeferredError as error:
                        self._finish(event,'failed',{'code':error.code},now)
                        completed.append({'eventId':event,'steamId':identity,'kind':kind,'status':'failed'})
                        continue
                    except Exception as error:
                        status = getattr(error,'status',None)
                        state = 'failed' if isinstance(status,int) and 400<=status<500 else 'unknown'
                        self._finish(event,state,{'errorType':type(error).__name__,'httpStatus':status},now)
                        completed.append({'eventId':event,'steamId':identity,'kind':kind,'status':state})
                        continue
                    self._finish(event,'confirmed',result,now)
                    # Persist dispatch before the custom notice too: no blind replay.
                    with self.store.lock, self.store.db:
                        self.store.db.execute("UPDATE behavior_events SET notice_status='sending' WHERE id=?", (event,))
                    try:
                        notice = self.rcon.request('POST','/v1/broadcast',{'message':f'哈哈 {name} [{identity}]'})
                        if not isinstance(notice,dict) or notice.get('ok') is not True or notice.get('pending'):
                            raise ValueError('notice outcome not confirmed')
                        notice_state = 'sent'
                    except Exception as error:
                        status = getattr(error,'status',None)
                        notice_state = 'failed' if isinstance(status,int) and 400<=status<500 else 'unknown'
                    with self.store.lock, self.store.db:
                        self.store.db.execute('UPDATE behavior_events SET notice_status=? WHERE id=?', (notice_state,event))
                    completed.append({'eventId':event,'steamId':identity,'kind':kind,'status':'confirmed','targetFaction':target,'noticeStatus':notice_state})
                    LOG.info('behavior_action steam_id=%s kind=%s status=confirmed notice=%s',identity,kind,notice_state)
                except Exception as error:
                    self.retry_after = now + 5
                    LOG.warning('behavior_check_failed steam_id=%s error_type=%s',identity,type(error).__name__)
                    # A team preflight failure must not suppress the independent
                    # kill schedule (or another listed player) in this cycle.
                    continue
        return completed
