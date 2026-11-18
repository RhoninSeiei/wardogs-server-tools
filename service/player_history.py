"""Bounded administrator history index. Online state comes from the fresh local cache."""
import json
from pathlib import Path
import re
import sqlite3
import time


def identity(value):
    return isinstance(value,str) and re.fullmatch(r'[0-9]{17}',value) is not None


def name_text(value):
    return isinstance(value,str) and bool(value.strip()) and len(value)<=128 and all(c.isprintable() for c in value)


class HistoryUnavailable(Exception):
    code='player_snapshot_unavailable'


class PlayerHistory:
    def __init__(self,engine,cache_path):
        self.engine=engine
        self.path=Path(cache_path)
        self.db=engine.db
        self.lock=engine.lock
        with self.lock,self.db:
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS player_aliases(steam_id TEXT NOT NULL,name TEXT NOT NULL,folded TEXT NOT NULL,
                source TEXT NOT NULL,last_seen REAL,recorded REAL NOT NULL,PRIMARY KEY(steam_id,name,source));
              CREATE INDEX IF NOT EXISTS aliases_folded ON player_aliases(folded);
              CREATE TABLE IF NOT EXISTS player_history_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS admin_search_audit(id INTEGER PRIMARY KEY,actor TEXT NOT NULL,
                requested REAL NOT NULL,queried REAL NOT NULL,query_hash TEXT NOT NULL,result_count INTEGER NOT NULL);
            ''')
            if not self.db.execute("SELECT 1 FROM player_history_meta WHERE key='welcome_seeded'").fetchone():
                for sid,message,created in self.db.execute("SELECT recipient,message,created FROM jobs WHERE kind='welcome' AND recipient IS NOT NULL ORDER BY created,rowid"):
                    match=re.fullmatch(r'欢迎 (.+) 加入服务器！',message)
                    if match:self.observe(sid,match.group(1),'welcome_history',created,created)
                self.db.execute("INSERT INTO player_history_meta VALUES('welcome_seeded','1')")
            guard=engine.kick_store
            if guard is not None:
                with guard.lock:
                    for sid,name,seen,recorded in guard.db.execute('SELECT steam_id,name,confirmed_at,recorded_at FROM confirmed_kicks'):
                        self.observe(sid,name,'kick_history',seen,recorded)

    def observe(self,sid,name,source,seen,recorded):
        if not identity(sid):return
        name=name if name_text(name) else sid
        self.db.execute('''INSERT INTO player_aliases VALUES(?,?,?,?,?,?) ON CONFLICT(steam_id,name,source)
            DO UPDATE SET last_seen=CASE WHEN excluded.last_seen IS NULL THEN player_aliases.last_seen
            ELSE MAX(COALESCE(player_aliases.last_seen,excluded.last_seen),excluded.last_seen) END,
            recorded=MAX(player_aliases.recorded,excluded.recorded)''',(sid,name,name.casefold(),source,seen,recorded))

    def observe_roster(self,players,when):
        for p in players:self.observe(str(p['steamId']),p.get('name'),'roster_history',when,when)

    def refresh(self,now):
        try:
            conn=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
            deadline=time.monotonic()+2
            conn.set_progress_handler(lambda:int(time.monotonic()>deadline),2000)
            conn.execute('BEGIN')
            row=conn.execute("SELECT body,observed FROM source_snapshots WHERE kind='players'").fetchone()
            if row is None or not 0<=now-row[1]<=15:raise HistoryUnavailable()
            roster=json.loads(row[0]).get('players')
            if not isinstance(roster,list) or len(roster)>1000 or any(not isinstance(p,dict) or not identity(p.get('steamId')) for p in roster):raise HistoryUnavailable()
            if len({p['steamId'] for p in roster})!=len(roster):raise HistoryUnavailable()
            generation=conn.execute('SELECT generation FROM export_meta WHERE id=1').fetchone()[0]
            with self.lock,self.db:
                old=self.db.execute("SELECT value FROM player_history_meta WHERE key='cache_generation'").fetchone()
                after=self.db.execute("SELECT value FROM player_history_meta WHERE key='event_rowid'").fetchone()
                after=int(after[0]) if after and old and old[0]==generation else 0
                latest=conn.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]
                if latest<after:after=0
                cursor=conn.execute('SELECT rowid,received,body FROM events WHERE rowid>? ORDER BY rowid LIMIT 5000',(after,))
                for seq,recorded,body in cursor:
                    obj=json.loads(body)
                    if not isinstance(obj,dict):raise HistoryUnavailable()
                    for prefix in ('player','killer','victim'):
                        sid=obj.get(prefix+'SteamId')
                        if identity(sid):self.observe(sid,obj.get(prefix+'Name'),'event_history',None,recorded)
                    after=seq
                self.observe_roster(roster,row[1])
                self.db.execute("INSERT OR REPLACE INTO player_history_meta VALUES('cache_generation',?)",(generation,))
                self.db.execute("INSERT OR REPLACE INTO player_history_meta VALUES('event_rowid',?)",(str(after),))
            return roster,row[1],after>=latest
        except (sqlite3.Error,ValueError,TypeError,KeyError,IndexError,OSError):
            raise HistoryUnavailable() from None
        finally:
            if 'conn' in locals():conn.close()

    def known(self,sid,online=None):
        with self.lock:
            rows=self.db.execute('SELECT name,source,last_seen,recorded FROM player_aliases WHERE steam_id=? ORDER BY COALESCE(last_seen,-1) DESC,recorded DESC,name',(sid,)).fetchall()
        if not rows:return None
        names=list(dict.fromkeys(row[0] for row in rows if row[0]!=sid))
        current=online.get('name') if online is not None and name_text(online.get('name')) else (names[0] if names else sid)
        aliases=[n for n in names if n!=current]
        seen=[r[2] for r in rows if r[2] is not None]
        return {'steamId':sid,'name':current,'aliases':aliases[:20],'aliasesTruncated':len(aliases)>20,
                'online':online is not None,'lastSeen':max(seen) if seen else None,
                'source':'online' if online is not None else 'history','sources':sorted({r[1] for r in rows})}

    def search(self,query,now):
        roster,observed,complete=self.refresh(now)
        live={p['steamId']:p for p in roster}
        with self.lock:
            deadline=time.monotonic()+1
            self.db.set_progress_handler(lambda:int(time.monotonic()>deadline),2000)
            try:
                if identity(query):
                    ids=self.db.execute('SELECT steam_id FROM player_aliases WHERE steam_id=? LIMIT 1',(query,)).fetchall()
                else:
                    ids=self.db.execute('''SELECT steam_id FROM player_aliases WHERE instr(folded,?)>0 GROUP BY steam_id
                        ORDER BY MAX(last_seen) DESC,MAX(recorded) DESC,steam_id LIMIT 21''',(query.casefold(),)).fetchall()
                players=[self.known(sid,live.get(sid)) for (sid,) in ids[:20]]
            except sqlite3.Error:raise HistoryUnavailable() from None
            finally:self.db.set_progress_handler(None,0)
        return {'observedAt':observed,'players':players,'truncated':len(ids)>20,'historyComplete':complete}
