"""Immutable chat observations and reversible, mutually unique cross-source links."""
from bisect import bisect_left,bisect_right
from datetime import datetime
import hashlib,json,math,re

RULE='chat_strict_unique_time_v1'
CHANNELS={'Global','Team','Squad','Proximity'}
def number(value):return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else None
def steam(value):return value if isinstance(value,str) and re.fullmatch('[0-9]{17}',value) else None
def string(value,maximum):return value if isinstance(value,str) and len(value)<=maximum else None
def utc(value):
    if not isinstance(value,str):return None
    try:
        d=datetime.fromisoformat(value.replace('Z','+00:00'))
        return number(d.timestamp()) if d.tzinfo is not None else None
    except (ValueError,OverflowError):return None

def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS chat_observations(
       id TEXT PRIMARY KEY,source TEXT NOT NULL,server_id TEXT NOT NULL,steam_id TEXT,name TEXT,hidden_name TEXT,
       channel TEXT,faction TEXT,text TEXT NOT NULL,raw_text TEXT,anonymous INTEGER,ping INTEGER,
       sent_at REAL,received_at REAL NOT NULL,game_time REAL,rcon_seq INTEGER,event_id TEXT,
       signature TEXT,candidate_count INTEGER NOT NULL DEFAULT 0,body TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS chat_sender_time ON chat_observations(steam_id,sent_at,received_at);
      CREATE INDEX IF NOT EXISTS chat_signature ON chat_observations(signature);
      CREATE TABLE IF NOT EXISTS chat_links(rcon_id TEXT PRIMARY KEY,callback_id TEXT NOT NULL UNIQUE,rule TEXT NOT NULL,delta REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS chat_aliases(steam_id TEXT,name TEXT,folded TEXT,last_seen REAL,PRIMARY KEY(steam_id,name));
      CREATE INDEX IF NOT EXISTS chat_alias_search ON chat_aliases(folded);
      CREATE TABLE IF NOT EXISTS chat_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    ''')

class ChatArchive:
    def __init__(self,db):self.db=db;initialize(db)
    def meta(self,key,default=None):
        row=self.db.execute('SELECT value FROM chat_meta WHERE key=?',(key,)).fetchone();return json.loads(row[0]) if row else default
    def setmeta(self,key,value):self.db.execute('INSERT OR REPLACE INTO chat_meta VALUES(?,?)',(key,json.dumps(value,separators=(',',':'))))
    def cursor(self):return {'after':str(self.meta('rcon',{}).get('scanAfter',0)),'limit':'1000'}
    def add(self,source,server,key,obj,received,sequence=None):
        if source=='rcon':
            sent=utc(obj.get('sentUtc'));sid=steam(obj.get('steamId'));name=obj.get('realName');game_time=None;event_id=None
            if sent is None:raise ValueError('invalid_chat_utc')
        else:sent=None;sid=steam(obj.get('playerSteamId'));name=obj.get('playerName');game_time=number(obj.get('eventTime'));event_id=string(obj.get('eventId'),256)
        text=string(obj.get('text'),8192)
        if text is None:raise ValueError('invalid_chat_text')
        raw=string(obj.get('rawText'),8192)
        if obj.get('rawText') is not None and raw is None:raise ValueError('invalid_chat_raw_text')
        name=string(name,128);hidden=string(obj.get('hiddenName'),128)
        channel=string(obj.get('channel'),64);faction=string(obj.get('faction'),64)
        anonymous=obj.get('anonymous') if type(obj.get('anonymous')) is bool else None
        ping=obj.get('ping') if type(obj.get('ping')) is bool else None
        sig=None
        if sid and channel in CHANNELS and faction and anonymous is not None and ping is not None:
            sig=hashlib.sha256(json.dumps([sid,channel,faction,text,raw,anonymous,ping],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        identity=source+':'+hashlib.sha256(json.dumps([server,key],separators=(',',':')).encode()).hexdigest()
        body=json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        old=self.db.execute('SELECT body FROM chat_observations WHERE id=?',(identity,)).fetchone()
        if old:
            if old[0]!=body:raise ValueError('chat_observation_conflict')
            return None
        self.db.execute('''INSERT INTO chat_observations(id,source,server_id,steam_id,name,hidden_name,channel,faction,text,raw_text,anonymous,ping,sent_at,received_at,game_time,rcon_seq,event_id,signature,body)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(identity,source,server,sid,name,hidden,channel,faction,text,raw,anonymous,ping,sent,received,game_time,sequence,event_id,sig,body))
        if sid:
            for alias in [name,hidden if anonymous else None]:
                if alias and alias.strip():self.db.execute('''INSERT INTO chat_aliases VALUES(?,?,?,?) ON CONFLICT(steam_id,name) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen)''',(sid,alias,alias.casefold(),received))
        return sig
    def reconcile(self,signatures):
        for sig in signatures-{None}:
            rows=self.db.execute('SELECT id,source,server_id,sent_at,received_at FROM chat_observations WHERE signature=?',(sig,)).fetchall()
            # Signature intentionally excludes server; scope each association by server.
            for server in {r[2] for r in rows}:
                rcon=sorted([(r[3],r[0]) for r in rows if r[1]=='rcon' and r[2]==server]);callbacks=sorted([(r[4],r[0]) for r in rows if r[1]=='callback' and r[2]==server])
                rt=[x[0] for x in rcon];et=[x[0] for x in callbacks]
                self.db.execute('DELETE FROM chat_links WHERE rcon_id IN (SELECT id FROM chat_observations WHERE signature=? AND server_id=?)',(sig,server))
                for timestamp,identity in callbacks:
                    count=bisect_right(rt,timestamp+2)-bisect_left(rt,timestamp-10)
                    self.db.execute('UPDATE chat_observations SET candidate_count=? WHERE id=?',(count,identity))
                for timestamp,identity in rcon:
                    lo,hi=bisect_left(et,timestamp-2),bisect_right(et,timestamp+10);count=hi-lo
                    self.db.execute('UPDATE chat_observations SET candidate_count=? WHERE id=?',(count,identity))
                    if count==1:
                        other_time,other=callbacks[lo]
                        if bisect_right(rt,other_time+2)-bisect_left(rt,other_time-10)==1:
                            self.db.execute('INSERT INTO chat_links VALUES(?,?,?,?)',(identity,other,RULE,other_time-timestamp))
    def index_callbacks(self,now,limit=1000):
        generation=self.db.execute('SELECT generation FROM export_meta WHERE id=1').fetchone()[0]
        latest=self.db.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]
        old=self.meta('callback',{});after=old.get('afterRowid',0) if old.get('generation')==generation else 0
        if after>latest:after=0
        rows=self.db.execute("SELECT rowid,server,event_key,received,body FROM events WHERE rowid>? AND type='chat_message' ORDER BY rowid LIMIT ?",(after,limit)).fetchall();changed=set()
        for seq,server,key,received,body in rows:
            obj=json.loads(body);changed.add(self.add('callback',server,key,obj,received));after=seq
        if len(rows)<limit:after=latest
        self.reconcile(changed);self.setmeta('callback',{'generation':generation,'afterRowid':after,'indexedAt':now})
    def ingest_rcon(self,envelope,now):
        if not isinstance(envelope,dict):raise ValueError('invalid_chat_envelope')
        server=envelope.get('serverId');page=envelope.get('chat');after=envelope.get('after',0)
        if type(after) is not int or after<0:raise ValueError('invalid_chat_cursor')
        if not isinstance(server,str) or not server or len(server)>256 or not isinstance(page,dict) or page.get('ok') is False or page.get('pending') or page.get('complete') is False:raise ValueError('invalid_chat_envelope')
        messages=page.get('messages');latest=page.get('latestSeq')
        if type(latest) is not int or latest<0 or not isinstance(messages,list) or len(messages)>1000 or type(page.get('count')) is not int or page['count']!=len(messages):raise ValueError('invalid_chat_page')
        seqs=[m.get('seq') if isinstance(m,dict) else None for m in messages]
        if any(type(s) is not int or s<1 or s>latest for s in seqs) or seqs!=sorted(set(seqs)):raise ValueError('invalid_chat_sequence')
        changed=set()
        for obj,seq in zip(messages,seqs):
            sent=utc(obj.get('sentUtc'))
            if sent is None or sent>now+30:raise ValueError('invalid_chat_clock')
            changed.add(self.add('rcon',server,[seq,sent],obj,now,seq))
        self.reconcile(changed)
        caught=(seqs[-1]==latest) if seqs else after<=latest
        self.setmeta('rcon',{'serverId':server,'latestSeq':latest,'maxSeq':seqs[-1] if seqs else None,'caughtUp':caught,'scanAfter':0 if caught or after>latest else seqs[-1] if seqs else 0,'observedAt':now,'retainedCount':len(messages)})
