"""Read-only WDRCON match snapshot; team scores never inferred from kills."""
import copy
import math
import threading
import time

CACHE_SECONDS = 15


class MatchUnavailable(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def name(value):
    return value if isinstance(value,str) and value.strip() and len(value)<=256 and all(c.isprintable() for c in value) else None


def count(value):
    return value if isinstance(value,int) and not isinstance(value,bool) and value>=0 else None


def score(value):
    try:
        return value if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0 else None
    except OverflowError:
        return None


def normalize(status, player_data, observed):
    if not isinstance(status,dict) or not isinstance(player_data,dict):
        raise MatchUnavailable('match_invalid_upstream')
    players=player_data.get('players')
    valid_players=isinstance(players,list) and all(isinstance(p,dict) for p in players)
    population=status.get('players') if isinstance(status.get('players'),dict) else {}
    current=count(population.get('current'))
    if current is None:
        if not valid_players:raise MatchUnavailable('match_invalid_upstream')
        current=len(players)
    factions={}
    faction_counts_known=valid_players and all(name(p.get('faction')) is not None for p in players)
    if faction_counts_known:
        for p in players:
            factions[p['faction']]=factions.get(p['faction'],0)+1
    raw=status.get('factionScores')
    valid_scores=isinstance(raw,list) and bool(raw) and all(isinstance(t,dict) and name(t.get('name')) is not None for t in raw)
    if valid_scores and len({t['name'] for t in raw})!=len(raw):valid_scores=False
    rows=raw if valid_scores else [{'name':f} for f in factions]
    teams=[{'id':t['name'],'name':t['name'],'score':score(t.get('score')) if valid_scores else None,
            'playerCount':factions.get(t['name'],0) if faction_counts_known else None} for t in rows]
    available=bool(teams) and valid_scores and all(t['score'] is not None for t in teams)
    mode=name(status.get('mode'))
    experiences=status.get('experiences')
    if mode is None and isinstance(experiences,list) and experiences and all(name(e) is not None for e in experiences):
        mode=' + '.join(experiences)
    return {'observedAt':observed,'playerCount':current,'maxPlayers':count(population.get('max')),
            'map':name(status.get('map')),'mode':mode,'teams':teams,
            'scoreAvailable':bool(available),'scoreKind':'faction_score' if available else None}


class MatchQuery:
    def __init__(self,rcon,clock=time.time):
        self.rcon,self.clock=rcon,clock
        self.lock=threading.Lock()
        self.cached=None

    def get(self):
        # Separate lock from penalties/polling: a slow query does not hold the
        # engine's announcement/coordination lock. Cache collapses query bursts.
        with self.lock:
            started=self.clock()
            if self.cached is not None and 0<=started-self.cached['observedAt']<=CACHE_SECONDS:
                return copy.deepcopy(self.cached)
            try:
                status=self.rcon.request('GET','/v1/status')
                players=self.rcon.request('GET','/v1/players')
            except Exception as error:
                raise MatchUnavailable('match_upstream_unavailable') from error
            if not 0<=self.clock()-started<=CACHE_SECONDS:
                raise MatchUnavailable('match_data_stale')
            result=normalize(status,players,started)
            self.cached=result
            return copy.deepcopy(result)
