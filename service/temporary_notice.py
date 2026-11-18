"""Bounded temporary announcement worker; normal exit at the UTC deadline."""
import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time

try:
    from service.rcon import Rcon, RconError
    from service.announcements import validate_messages
except ImportError:
    from rcon import Rcon, RconError
    from announcements import validate_messages

LOG = logging.getLogger('temporary_notice')


class Runner:
    def __init__(self, path, config, rcon, clock=time.time, japanese_enabled=lambda: True):
        if set(config) != {'startAt','endAt','intervalSeconds','languageGapSeconds','messages'}:
            raise ValueError('invalid temporary announcement fields')
        for key in ('startAt','endAt','intervalSeconds','languageGapSeconds'):
            v=config[key]
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v):
                raise ValueError('invalid announcement timing')
        if config['endAt']<=config['startAt'] or config['intervalSeconds']<=0 or config['languageGapSeconds']<10:
            raise ValueError('invalid announcement range')
        if not isinstance(config['messages'],list) or len(config['messages'])!=3:
            raise ValueError('three languages required')
        validate_messages(config['messages'])
        self.config,self.rcon,self.clock=config,rcon,clock
        self.japanese_enabled=japanese_enabled
        self.db=sqlite3.connect(path,timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS metadata(id INTEGER PRIMARY KEY,signature TEXT NOT NULL,next_send REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS deliveries(slot INTEGER NOT NULL,language INTEGER NOT NULL,
            status TEXT NOT NULL,attempts INTEGER NOT NULL,updated REAL NOT NULL,receipt TEXT,
            PRIMARY KEY(slot,language));
        ''')
        signature=hashlib.sha256(json.dumps(config,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.db:
            row=self.db.execute('SELECT signature FROM metadata WHERE id=1').fetchone()
            if row and row[0]!=signature:raise ValueError('job config changed; use a new state database')
            self.db.execute('INSERT OR IGNORE INTO metadata VALUES(1,?,0)',(signature,))
            # A process crash cannot justify replaying an uncertain announcement.
            self.db.execute("UPDATE deliveries SET status='unknown' WHERE status='sending'")

    def close(self): self.db.close()

    def step(self):
        now=self.clock();c=self.config
        if not c['startAt']<=now<c['endAt']:return False
        slot=int((now-c['startAt'])//c['intervalSeconds'])
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            next_send=self.db.execute('SELECT next_send FROM metadata WHERE id=1').fetchone()[0]
            if now<next_send:return False
            language=None
            for index in range(3):
                row=self.db.execute('SELECT status FROM deliveries WHERE slot=? AND language=?',(slot,index)).fetchone()
                if row is None or row[0]=='queued':language=index;break
            if language is None:return False
            self.db.execute("INSERT OR IGNORE INTO deliveries VALUES(?,?,'queued',0,?,NULL)",(slot,language,now))
            if language==2 and not self.japanese_enabled():
                self.db.execute("UPDATE deliveries SET status='cancelled',updated=?,receipt=? WHERE slot=? AND language=?",(now,json.dumps({'reason':'japanese_temporarily_paused'}),slot,language))
                return True
            self.db.execute("UPDATE deliveries SET status='sending',attempts=attempts+1,updated=? WHERE slot=? AND language=?",(now,slot,language))
            self.db.execute('UPDATE metadata SET next_send=? WHERE id=1',(now+c['languageGapSeconds'],))
        # Check again immediately before dispatch, including deadline crossings.
        if self.clock()>=c['endAt']:
            with self.db:self.db.execute("UPDATE deliveries SET status='cancelled',updated=? WHERE slot=? AND language=?",(self.clock(),slot,language))
            return False
        state='sent';receipt=None;retry=0
        try:
            receipt=self.rcon.request('POST','/v1/broadcast',{'message':c['messages'][language]})
            if not isinstance(receipt,dict) or receipt.get('ok') is not True or receipt.get('pending'):
                raise ValueError('announcement outcome not confirmed')
        except RconError as error:
            state='queued' if error.status==429 else 'failed'
            retry=error.retry_after if error.status==429 else 0
            receipt={'httpStatus':error.status}
        except Exception as error:
            state='unknown';receipt={'errorType':type(error).__name__}
        finished=self.clock()
        with self.db:
            self.db.execute('UPDATE deliveries SET status=?,updated=?,receipt=? WHERE slot=? AND language=?',
                            (state,finished,json.dumps(receipt,ensure_ascii=False),slot,language))
            self.db.execute('UPDATE metadata SET next_send=? WHERE id=1',
                            (finished+max(c['languageGapSeconds'],retry),))
        LOG.info('round=%s language=%s status=%s',slot,('zh','en','ja')[language],state)
        return True


def standalone_japanese_enabled():
    try:
        return json.loads(Path(os.environ.get('CONFIG_FILE','/app/config.json')).read_text()).get('japanese_announcements_enabled',False) is True
    except (OSError,ValueError,TypeError,AttributeError):
        return False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--key-file',type=Path,required=True)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    config=json.loads(args.config.read_text())
    args.state.parent.mkdir(parents=True,exist_ok=True)
    # One worker for this job database, across host and container restarts.
    with args.state.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        runner=Runner(args.state,config,Rcon(os.environ['RCON_HOST'], int(os.environ.get('RCON_PORT', '10246')), args.key_file),japanese_enabled=standalone_japanese_enabled)
        stop=threading.Event()
        signal.signal(signal.SIGTERM,lambda *_:stop.set())
        signal.signal(signal.SIGINT,lambda *_:stop.set())
        try:
            while not stop.is_set() and time.time()<config['endAt']:
                runner.step();stop.wait(0.5)
            LOG.info('temporary_announcement_stopped deadline_reached=%s',time.time()>=config['endAt'])
        finally:runner.close()

if __name__=='__main__':main()
