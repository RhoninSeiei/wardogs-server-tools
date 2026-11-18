"""Read-only incremental export of durable events for a local cache."""
import json
from pathlib import Path
import re
import sqlite3
import uuid


def initialize(db):
    with db:
        db.executescript('''
          CREATE TABLE IF NOT EXISTS export_meta(id INTEGER PRIMARY KEY CHECK(id=1),generation TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS event_sequences(seq INTEGER PRIMARY KEY AUTOINCREMENT,server TEXT NOT NULL,event_key TEXT NOT NULL,UNIQUE(server,event_key));
        ''')
        db.execute('INSERT OR IGNORE INTO export_meta VALUES(1,?)',(uuid.uuid4().hex,))
        rows=db.execute('SELECT e.server,e.event_key FROM events e LEFT JOIN event_sequences s ON e.server=s.server AND e.event_key=s.event_key WHERE s.seq IS NULL ORDER BY e.received,e.rowid')
        for server,key in rows:index_event(db,server,key)


def index_event(db,server,key):
    db.execute('INSERT OR IGNORE INTO event_sequences(server,event_key) VALUES(?,?)',(server,key))


def query(path,params):
    if __package__:
        from .kill_query import QueryError
    else:
        from kill_query import QueryError
    if set(params)-{'after','limit','generation'} or any(len(v)!=1 for v in params.values()):
        raise QueryError(400,'invalid_query')
    values={k:v[0] for k,v in params.items()}
    after,limit=values.get('after','0'),values.get('limit','500')
    if not re.fullmatch(r'[0-9]{1,19}',after) or int(after)>9223372036854775807 or not re.fullmatch(r'[0-9]{1,4}',limit) or not 1<=int(limit)<=1000:
        raise QueryError(400,'invalid_query')
    if int(after)>0 and 'generation' not in values:raise QueryError(400,'generation_required')
    conn=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    try:
        conn.execute('BEGIN')
        generation=conn.execute('SELECT generation FROM export_meta WHERE id=1').fetchone()[0]
        if 'generation' in values and values['generation']!=generation:raise QueryError(409,'archive_changed')
        latest=conn.execute('SELECT COALESCE(MAX(seq),0) FROM event_sequences').fetchone()[0]
        if int(after)>latest:raise QueryError(409,'cursor_ahead')
        rows=[]; size=0
        cursor_rows=conn.execute('SELECT s.seq,e.server,e.received,e.body FROM event_sequences s JOIN events e ON e.server=s.server AND e.event_key=s.event_key WHERE s.seq>? ORDER BY s.seq LIMIT ?',(int(after),int(limit)))
        for row in cursor_rows:
            length=len(row[3].encode())
            if rows and size+length>1048576:break
            rows.append(row);size+=length
        cursor=rows[-1][0] if rows else int(after)
        return {'generation':generation,'nextAfter':str(cursor),'latestSequence':str(latest),'hasMore':cursor<latest,
                'events':[{'sequence':str(seq),'serverId':server,'receivedAt':received,'event':json.loads(body)} for seq,server,received,body in rows]}
    finally:conn.close()
