"""Single-language, forward-only notices from the existing local crash classifier."""
import json
from pathlib import Path
import re
import sqlite3
import time

class HelicopterFun:
 def __init__(self,engine,cache):
  self.e=engine;self.cache=Path(cache);self.last_error=None
  with engine.lock,engine.db:
   engine.db.executescript('''
    CREATE TABLE IF NOT EXISTS helicopter_fun_meta(id INTEGER PRIMARY KEY,generation TEXT,cursor INTEGER,armed REAL);
    CREATE TABLE IF NOT EXISTS helicopter_fun_notices(server TEXT,event_key TEXT,steam_id TEXT,name TEXT,job_id TEXT,created REAL,PRIMARY KEY(server,event_key));
   ''')

 def tick(self,now=None):
  if not self.e.config.get('helicopter_fun_enabled',False):return
  now=time.time() if now is None else now
  try:
   c=sqlite3.connect(self.cache.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
   c.execute('BEGIN')
   state=c.execute("SELECT last_success,error FROM sync_state WHERE kind='events'").fetchone()
   monitor=c.execute('SELECT last_checked FROM event_monitor WHERE id=1').fetchone()
   if not state or state[1] is not None or not 0<=now-state[0]<=15 or not monitor or monitor[0] is None or not 0<=now-monitor[0]<=15:raise ValueError('local_events_or_classifier_stale')
   generation=c.execute('SELECT generation FROM export_meta WHERE id=1').fetchone()[0]
   latest=c.execute('SELECT COALESCE(MAX(archive_seq),0) FROM helicopter_incidents').fetchone()[0]
   with self.e.lock,self.e.db:
    meta=self.e.db.execute('SELECT generation,cursor,armed FROM helicopter_fun_meta WHERE id=1').fetchone()
    if not meta or meta[0]!=generation or meta[1]>latest:
     self.e.db.execute('INSERT OR REPLACE INTO helicopter_fun_meta VALUES(1,?,?,?)',(generation,latest,now))
    else:
     after,armed=meta[1:]
     rows=c.execute('''SELECT h.archive_seq,k.server,k.event_key,k.killer_sid,k.killer_name,e.received
       FROM helicopter_incidents h JOIN kill_records k ON k.seq=h.archive_seq
       JOIN events e ON e.server=k.server AND e.event_key=k.event_key
       WHERE h.archive_seq>? ORDER BY h.archive_seq LIMIT 100''',(after,)).fetchall()
     for seq,server,key,sid,name,received in rows:
      after=seq
      if received<armed or not isinstance(sid,str) or re.fullmatch('[0-9]{17}',sid) is None:continue
      if self.e.db.execute('SELECT 1 FROM helicopter_fun_notices WHERE server=? AND event_key=?',(server,key)).fetchone():continue
      label=''.join(x for x in str(name or '') if x.isprintable()).strip()[:128] or sid
      message=f'Man, what can I say? {label} Out!'
      job=self.e.add_job(message,now,kind='helicopter_fun')
      self.e.db.execute('INSERT INTO helicopter_fun_notices VALUES(?,?,?,?,?,?)',(server,key,sid,label,job,now))
     self.e.db.execute('UPDATE helicopter_fun_meta SET cursor=? WHERE id=1',(after,))
   self.last_error=None
  except Exception as ex:
   self.last_error=type(ex).__name__;raise
  finally:
   if 'c' in locals():c.close()

 def snapshot(self):
  with self.e.lock:
   row=self.e.db.execute('SELECT cursor,armed FROM helicopter_fun_meta WHERE id=1').fetchone()
   count=self.e.db.execute('SELECT count(*) FROM helicopter_fun_notices').fetchone()[0]
  return {'enabled':self.e.config.get('helicopter_fun_enabled',False),'cursor':row[0] if row else None,'armedAt':row[1] if row else None,'noticeCount':count,'lastError':self.last_error}
