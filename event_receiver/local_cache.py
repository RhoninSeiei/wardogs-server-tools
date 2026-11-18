"""Independent two-second event and five-second game snapshot collectors."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler

if __package__:
    from .receiver import Archive, Server, make_handler
    from .event_connection import EventClient
    from .chat_archive import ChatArchive
    from . import kill_query, event_export, event_monitor
else:
    from receiver import Archive, Server, make_handler
    from event_connection import EventClient
    from chat_archive import ChatArchive
    import kill_query, event_export, event_monitor

INTERVAL=5
EVENT_INTERVAL=2
MAX_AGE=15
BACKOFF_SOURCES={'players':'rcon','status':'rcon','events':'events','chat':'rcon'}


def retry_delay(headers,now,attempt):
    """Return a delay, respecting valid seconds or HTTP dates without a short cap."""
    raw=headers.get('Retry-After') if headers else None
    if isinstance(raw,str):
        raw=raw.strip()
        try:
            if raw.isascii() and raw.isdigit():
                seconds=float(raw)
            else:
                def timestamp(value):
                    date=parsedate_to_datetime(value)
                    if date.tzinfo is None:date=date.replace(tzinfo=timezone.utc)
                    return date.timestamp()
                baseline=now
                if headers.get('Date'):
                    try:baseline=timestamp(headers['Date'])
                    except (TypeError,ValueError,OverflowError):pass
                seconds=timestamp(raw)-baseline
            if math.isfinite(seconds):return max(INTERVAL,seconds)
        except (TypeError,ValueError,OverflowError):pass
    return min(300,INTERVAL*2**min(max(attempt-1,0),6))


class Cache:
    def __init__(self,path,wall_clock=None,monotonic_clock=None):
        self.wall_clock=wall_clock or time.time
        self.monotonic_clock=monotonic_clock or time.monotonic
        self.archive=Archive(path)
        with self.archive.db:
            self.archive.db.executescript('''
              CREATE TABLE IF NOT EXISTS source_snapshots(kind TEXT PRIMARY KEY,body TEXT NOT NULL,observed REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS sync_state(kind TEXT PRIMARY KEY,last_success REAL,error TEXT);
              CREATE TABLE IF NOT EXISTS sync_cursor(id INTEGER PRIMARY KEY CHECK(id=1),generation TEXT,after_seq INTEGER NOT NULL,latest_seq INTEGER NOT NULL DEFAULT 0);
              INSERT OR IGNORE INTO sync_cursor(id,generation,after_seq) VALUES(1,NULL,0);
              CREATE TABLE IF NOT EXISTS sync_backoff(source TEXT PRIMARY KEY,retry_at REAL NOT NULL,consecutive429 INTEGER NOT NULL);
            ''')
        self.chats=ChatArchive(self.archive.db)
        self.backoff_generation={source:0 for source in ('rcon','events')}
        now,mono=self.wall_clock(),self.monotonic_clock()
        self.deadlines={source:mono+max(0,retry_at-now) for source,retry_at in self.archive.db.execute('SELECT source,retry_at FROM sync_backoff')}

    def deferred(self,kind):
        with self.archive.lock:
            return self.monotonic_clock()<self.deadlines.get(BACKOFF_SOURCES[kind],0)

    def defer(self,kind,headers,count_attempt=True):
        source=BACKOFF_SOURCES[kind]
        now,mono=self.wall_clock(),self.monotonic_clock()
        with self.archive.lock,self.archive.db:
            self.backoff_generation[source]+=1
            row=self.archive.db.execute('SELECT consecutive429 FROM sync_backoff WHERE source=?',(source,)).fetchone()
            attempt=(row[0] if row else 0)+int(count_attempt)
            delay=retry_delay(headers,now,max(1,attempt))
            deadline=max(self.deadlines.get(source,0),mono+delay)
            retry_at=now+max(0,deadline-mono)
            self.archive.db.execute('INSERT OR REPLACE INTO sync_backoff VALUES(?,?,?)',(source,retry_at,max(1,attempt)))
            self.deadlines[source]=deadline

    def clear_backoff(self,source,expected_generation=None):
        with self.archive.lock,self.archive.db:
            if expected_generation is not None and self.backoff_generation[source]!=expected_generation:return
            self.archive.db.execute('DELETE FROM sync_backoff WHERE source=?',(source,))
            self.deadlines.pop(source,None)

    def cursor(self):
        with self.archive.lock:
            generation,seq=self.archive.db.execute('SELECT generation,after_seq FROM sync_cursor WHERE id=1').fetchone()
        return {'after':str(seq),'limit':'500',**({'generation':generation} if generation else {})}

    def save(self,kind,doc,now):
        if not isinstance(doc,dict) or doc.get('ok') is False or (kind=='players' and (not isinstance(doc.get('players'),list) or any(not isinstance(p,dict) for p in doc['players']))):
            raise ValueError('invalid_snapshot')
        body=json.dumps(doc,ensure_ascii=False,allow_nan=False)
        with self.archive.lock,self.archive.db:
            self.archive.db.execute('INSERT OR REPLACE INTO source_snapshots VALUES(?,?,?)',(kind,body,now))
            self.archive.db.execute('INSERT OR REPLACE INTO sync_state VALUES(?,?,NULL)',(kind,now))

    def apply_events(self,page,now):
        if not isinstance(page,dict) or not isinstance(page.get('generation'),str) or not page['generation'] or not isinstance(page.get('events'),list) or len(page['events'])>1000:
            raise ValueError('invalid_page')
        def seq(v):
            if not isinstance(v,str) or not v.isascii() or not v.isdigit() or len(v)>19 or int(v)>9223372036854775807:raise ValueError('invalid_sequence')
            return int(v)
        next_seq,latest=seq(page.get('nextAfter')),seq(page.get('latestSequence'))
        if next_seq>latest or type(page.get('hasMore')) is not bool or page['hasMore']!=(next_seq<latest):raise ValueError('invalid_page')
        with self.archive.lock,self.archive.db:
            self.archive.db.execute('BEGIN IMMEDIATE')
            generation,previous=self.archive.db.execute('SELECT generation,after_seq FROM sync_cursor WHERE id=1').fetchone()
            if generation and generation!=page['generation']:raise ValueError('generation_mismatch')
            if next_seq<previous:raise ValueError('cursor_regressed')
            rows=[];last=previous
            for item in page['events']:
                current=seq(item.get('sequence'))
                server,received,obj=item.get('serverId'),item.get('receivedAt'),item.get('event')
                if not last<current<=next_seq or not isinstance(server,str) or len(server)>256 or type(received) not in (int,float) or not math.isfinite(received) or received<0 or not isinstance(obj,dict):raise ValueError('invalid_event')
                body=json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
                event_id=obj.get('eventId')
                if event_id is not None and (not isinstance(event_id,str) or not event_id or len(event_id)>256):raise ValueError('invalid_event_id')
                key='id:'+event_id if event_id else 'hash:'+hashlib.sha256(body.encode()).hexdigest()
                rows.append((server,key,received,obj.get('type'),body));last=current
            if last!=next_seq:raise ValueError('cursor_gap')
            for row in rows:
                inserted=self.archive.db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)',row).rowcount
                if inserted:
                    event_export.index_event(self.archive.db,row[0],row[1])
                    if row[3]=='killed':kill_query.index_event(self.archive.db,row[0],row[1],row[4])
            self.archive.db.execute('UPDATE sync_cursor SET generation=?,after_seq=?,latest_seq=? WHERE id=1',(page['generation'],next_seq,latest))
            self.archive.db.execute('INSERT OR REPLACE INTO sync_state VALUES(?,?,NULL)',('events',now))

    def index_chat_events(self,now):
        try:
            with self.archive.lock,self.archive.db:
                self.chats.index_callbacks(now)
                self.chats.setmeta('callback_error',None)
        except Exception as error:
            with self.archive.lock,self.archive.db:self.chats.setmeta('callback_error',type(error).__name__)

    def apply_chat(self,doc,now):
        with self.archive.lock,self.archive.db:
            self.chats.ingest_rcon(doc,now)
            self.archive.db.execute('INSERT OR REPLACE INTO sync_state VALUES(?,?,NULL)',('chat',now))
        self.index_chat_events(now)

    def failed(self,kind,error,count_attempt=True):
        code=type(error).__name__
        if isinstance(error,HTTPError) and error.code==409 and kind=='events':
            # Re-bootstrap a replaced/restored remote archive; keep local history.
            with self.archive.lock,self.archive.db:
                self.archive.db.execute('UPDATE sync_cursor SET generation=NULL,after_seq=0,latest_seq=0 WHERE id=1')
        if isinstance(error,HTTPError) and kind=='chat' and error.code in (400,409):
            with self.archive.lock,self.archive.db:
                meta=self.chats.meta('rcon',{});meta.update(scanAfter=0,caughtUp=False);self.chats.setmeta('rcon',meta)
        if isinstance(error,HTTPError):
            try:
                if error.code==429:self.defer(kind,error.headers,count_attempt)
            finally:error.close()
        with self.archive.lock,self.archive.db:
            self.archive.db.execute('INSERT INTO sync_state VALUES(?,NULL,?) ON CONFLICT(kind) DO UPDATE SET error=excluded.error',(kind,code))

    def cycle(self,fetch,now=None,kinds=('players','status','events')):
        if not kinds or len(set(kinds))!=len(kinds) or any(k not in BACKOFF_SOURCES for k in kinds):
            raise ValueError('invalid_sync_sources')
        now_is_live = now is None
        now=self.wall_clock() if now is None else now
        params=self.cursor() if 'events' in kinds else {}
        with self.archive.lock:
            backoff_generation=dict(self.backoff_generation)
            chat_params=self.chats.cursor() if 'chat' in kinds else {}
        succeeded,limited=set(),set()
        with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
            pending={pool.submit(fetch,kind,params if kind=='events' else chat_params if kind=='chat' else {}):kind
                     for kind in kinds if not self.deferred(kind)}
            for future in as_completed(pending):
                kind=pending[future]
                source=BACKOFF_SOURCES[kind]
                try:
                    doc=future.result()
                    observed = self.wall_clock() if now_is_live else now
                    if kind=='events':
                        self.apply_events(doc,observed)
                        event_monitor.tick(self.archive)
                        self.index_chat_events(observed)
                    elif kind=='chat':self.apply_chat(doc,observed)
                    else:self.save(kind,doc,observed)
                    succeeded.add(source)
                except Exception as error:
                    is_limited=isinstance(error,HTTPError) and error.code==429
                    self.failed(kind,error,count_attempt=source not in limited)
                    if is_limited:limited.add(source)
        # A successful sibling response must never cancel a 429 from this round.
        for source in succeeded-limited:self.clear_backoff(source,backoff_generation[source])
        if 'events' in kinds:event_monitor.tick(self.archive)

    def snapshot(self,kind,now=None):
        now=time.time() if now is None else now
        with self.archive.lock:
            row=self.archive.db.execute('SELECT body,observed FROM source_snapshots WHERE kind=?',(kind,)).fetchone()
        if row is None or not 0<=now-row[1]<=MAX_AGE:
            raise kill_query.QueryError(503,'stale_snapshot')
        return {**json.loads(row[0]),'observedAt':row[1],'ageSeconds':now-row[1]}

    def status(self):
        with self.archive.lock:
            states={k:{'lastSuccessfulAt':when,'lastError':err} for k,when,err in self.archive.db.execute('SELECT kind,last_success,error FROM sync_state')}
            generation,seq,latest=self.archive.db.execute('SELECT generation,after_seq,latest_seq FROM sync_cursor WHERE id=1').fetchone()
            chat_rcon=self.chats.meta('rcon',{})
            chat_callback=self.chats.meta('callback',{})
            chat_error=self.chats.meta('callback_error')
            records={source:(retry_at,count) for source,retry_at,count in self.archive.db.execute('SELECT source,retry_at,consecutive429 FROM sync_backoff')}
            mono=self.monotonic_clock()
            backoff={source:{'retryAt':records[source][0] if source in records else None,
                             'retryInSeconds':max(0,self.deadlines.get(source,0)-mono),
                             'consecutive429':records[source][1] if source in records else 0}
                     for source in ('rcon','events')}
        return {'intervalSeconds':INTERVAL,'rconIntervalSeconds':INTERVAL,'eventIntervalSeconds':EVENT_INTERVAL,
                'maxSnapshotAgeSeconds':MAX_AGE,'sources':states,'backoff':backoff,'eventCursor':str(seq),'eventLatestSequence':str(latest),'eventsCaughtUp':generation is not None and seq>=latest,'eventGeneration':generation,'chat':{'rcon':chat_rcon,'callback':chat_callback,'callbackError':chat_error}}


def collect_group(cache,fetch,stop,kinds,interval,monotonic=time.monotonic):
    """Each group has one worker, so slow requests never accumulate overlaps."""
    while not stop.is_set():
        started=monotonic()
        try:cache.cycle(fetch,kinds=kinds)
        except Exception as error:
            print(json.dumps({'component':'local_sync','sources':list(kinds),'errorType':type(error).__name__}),flush=True)
        stop.wait(max(0.1,interval-(monotonic()-started)))


def fetcher(event_key,rcon_key,rcon_host='127.0.0.1:10246'):
    events=EventClient(event_key)
    def fetch(kind,params):
        if kind=='events':
            return events.get(params)
        def get(path):
            request=Request('http://'+rcon_host+path,headers={'Authorization':'Bearer '+rcon_key,'User-Agent':'WardogsQueryClient/1.0','Accept':'application/json'})
            with build_opener(ProxyHandler({})).open(request,timeout=5) as response:
                body=response.read(4194305)
                if len(body)>4194304:raise ValueError('response_too_large')
                return json.loads(body)
        if kind=='chat':
            server=get('/v1/server-id')
            if not isinstance(server,dict) or server.get('ok') is False or server.get('pending') or not isinstance(server.get('serverId'),str) or not server['serverId']:raise ValueError('server_identity_unavailable')
            chat=get('/v1/chat?'+urlencode(params))
            after_server=get('/v1/server-id')
            if not isinstance(after_server,dict) or after_server.get('ok') is False or after_server.get('pending') or after_server.get('serverId')!=server['serverId']:raise ValueError('server_identity_changed')
            return {'serverId':server['serverId'],'after':int(params['after']),'chat':chat}
        return get('/v1/'+kind)
    return fetch


def handler(cache,query_key):
    base=make_handler(cache.archive,secrets.token_urlsafe(32),query_token=query_key,incident_queries=True)
    class Handler(base):
        def reply(self,status,value):
            if status==200 and isinstance(value,dict) and 'events' in value:
                value={**value,'sync':cache.status()}
                if isinstance(value.get('monitor'),dict):
                    value['monitor']={**value['monitor'],'intervalSeconds':EVENT_INTERVAL}
            return super().reply(status,value)
        def do_POST(self):self.reply(405,{'error':{'code':'read_only'}})
        def do_GET(self):
            route=urlsplit(self.path)
            if route.path in ('/v1/players','/v1/status','/v1/sync-status'):
                if not self.authorized(query_key):return self.reply(401,{'error':{'code':'unauthorized'}})
                try:
                    if route.query:raise kill_query.QueryError(400,'invalid_query')
                    result=cache.status() if route.path.endswith('/sync-status') else cache.snapshot(route.path.rsplit('/',1)[1])
                    return self.reply(200,result)
                except kill_query.QueryError as error:return self.reply(error.status,error.body)
                except sqlite3.Error:return self.reply(503,{'error':{'code':'query_unavailable'}})
            if route.path in ('/v1/kills','/v1/helicopter-incidents'):
                self.path='/wardogs'+self.path
            return super().do_GET()
    return Handler


if __name__=='__main__':
    os.umask(0o077)
    query_key=Path(os.environ.get('EVENT_QUERY_TOKEN_FILE', '/run/secrets/event_query_token')).read_text().strip()
    rcon_key=Path(os.environ.get('RCON_KEY_FILE', '/run/secrets/rcon_key')).read_text().strip()
    if not query_key or not rcon_key:raise ValueError('empty credential')
    cache=Cache(os.environ.get('EVENT_DB', '/data/events.sqlite3'))
    fetch=fetcher(query_key,rcon_key,os.environ['RCON_HOSTPORT'])
    stop=threading.Event()
    workers = [('rcon-sync', ('players', 'status'), INTERVAL), ('event-sync', ('events',), EVENT_INTERVAL)]
    if os.environ.get('ENABLE_RCON_CHAT_SYNC') == '1':
        workers.append(('chat-sync', ('chat',), INTERVAL))
    for name,kinds,interval in workers:
        threading.Thread(target=collect_group,args=(cache,fetch,stop,kinds,interval),name=name,daemon=True).start()
    Server(('0.0.0.0',8080),handler(cache,query_key)).serve_forever()
