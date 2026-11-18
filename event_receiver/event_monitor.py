"""One local incremental event-processing cycle shared by event-based rules."""
import json
import threading
import time
if __package__:
    from .helicopter_monitor import matches
else:
    from helicopter_monitor import matches

INTERVAL = 5


def initialize(db):
    with db:
        db.executescript('''
          CREATE TABLE IF NOT EXISTS event_monitor(
            id INTEGER PRIMARY KEY CHECK(id=1),through_rowid INTEGER NOT NULL,last_checked REAL);
          INSERT OR IGNORE INTO event_monitor VALUES(1,0,NULL);
          CREATE TABLE IF NOT EXISTS helicopter_incidents(
            archive_seq INTEGER PRIMARY KEY,detected_at REAL NOT NULL);
        ''')


def tick(archive, now=None, batch_size=1000):
    now = time.time() if now is None else now
    with archive.lock, archive.db:
        archive.db.execute('BEGIN IMMEDIATE')
        checkpoint = archive.db.execute('SELECT through_rowid FROM event_monitor WHERE id=1').fetchone()[0]
        rows = archive.db.execute('''SELECT e.rowid,e.type,e.body,k.seq FROM events e
            LEFT JOIN kill_records k ON e.server=k.server AND e.event_key=k.event_key
            WHERE e.rowid>? ORDER BY e.rowid LIMIT ?''',(checkpoint,batch_size)).fetchall()
        found = 0
        for event_seq, kind, body, kill_seq in rows:
            obj = json.loads(body)
            # Further local event rules can reuse this same parsed batch.
            # This worker never calls RCON or sends external notifications.
            if kind == 'killed' and kill_seq is not None and matches(obj):
                found += archive.db.execute('INSERT OR IGNORE INTO helicopter_incidents VALUES(?,?)',(kill_seq,now)).rowcount
        through = rows[-1][0] if rows else checkpoint
        archive.db.execute('UPDATE event_monitor SET through_rowid=?,last_checked=? WHERE id=1',(through,now))
    return {'newIncidents':found,'checked':len(rows),'throughEventRowId':str(through)}


def start(archive):
    stop = threading.Event()
    def work():
        while not stop.is_set():
            try:
                tick(archive)
            except Exception as error:
                print(json.dumps({'component':'event_monitor','errorType':type(error).__name__}),flush=True)
            stop.wait(INTERVAL)
    threading.Thread(target=work,name='event-monitor',daemon=True).start()
    return stop
