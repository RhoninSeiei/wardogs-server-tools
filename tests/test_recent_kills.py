import copy
from contextlib import closing
import http.client
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
import time
import unittest
import test_admin_history as h


class RecentKillsTests(unittest.TestCase):
    roster=h.HistoryTests.roster
    def setUp(self):
        h.HistoryTests.setUp(self)
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.executescript("CREATE UNIQUE INDEX events_identity ON events(server,event_key); CREATE TABLE event_sequences(seq INTEGER PRIMARY KEY,server TEXT,event_key TEXT,UNIQUE(server,event_key)); CREATE TABLE sync_state(kind TEXT PRIMARY KEY,last_success REAL,error TEXT); INSERT INTO sync_state VALUES('events',1000,NULL); CREATE TABLE sync_cursor(id INTEGER PRIMARY KEY,generation TEXT,after_seq INTEGER,latest_seq INTEGER); INSERT INTO sync_cursor VALUES(1,'remote',0,0);")
    tearDown=h.HistoryTests.tearDown
    def payload(self,now=1000):return {'actor':h.admin_payload()['actor'],'requestedAt':now}
    def query(self,now=1000,payload=None):return self.engine.recent_kills.query(self.payload(now) if payload is None else payload,now=now)
    def add(self,seq=1,received=999,server='boot',event_id='kill',**changes):
        event={'type':'killed','eventId':event_id,'killerSteamId':h.ID,'killerName':'Fixture','victimSteamId':h.OTHER,'victimName':'Victim','cause':'ID.Item.Weapon.Test'};event.update(changes)
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute('INSERT INTO events VALUES(?,?,?,?,?)',(server,'id:'+event_id,received,event.get('type'),json.dumps(event)))
            db.execute('INSERT INTO event_sequences VALUES(?,?,?)',(seq,server,'id:'+event_id))
    def test_empty_complete_window_and_no_rcon_or_jobs(self):
        r=self.query();self.assertEqual(r,{'observedAt':1000,'windowStart':940,'windowEnd':1000,'timeBasis':'archive_received_at','complete':True,'events':[]})
        self.assertEqual(self.fake.calls,[]);self.assertEqual(self.engine.queue_depth(),0)
    def test_window_edges_and_future_excluded_stable_order(self):
        for i,t in enumerate([939.999,940,999,1000,1000.001],1):self.add(i,t,event_id=str(i))
        r=self.query();self.assertEqual([e['archiveId'] for e in r['events']],['4','3','2'])
        e=r['events'][0];self.assertEqual(e['killer'],{'steamId':h.ID,'name':'Fixture'});self.assertEqual(e['weapon'],'ID.Item.Weapon.Test');self.assertEqual(e['victim']['steamId'],h.OTHER)
    def test_same_event_id_different_servers_and_ties_not_merged(self):
        self.add(1,999,'a');self.add(2,999,'b')
        events=self.query()['events'];self.assertEqual([e['archiveId'] for e in events],['2','1']);self.assertEqual({e['serverId'] for e in events},{'a','b'});self.assertEqual(len(events),2)
    def test_missing_or_invalid_ids_stay_null_even_with_matching_nickname(self):
        self.add(killerSteamId=None,killerName='OldFixture',victimSteamId='bad',cause=None,eventId=None)
        e=self.query()['events'][0];self.assertIsNone(e['killer']['steamId']);self.assertEqual(e['killer']['name'],'OldFixture');self.assertIsNone(e['victim']['steamId']);self.assertIsNone(e['weapon']);self.assertIsNone(e['eventId'])
    def test_only_killed_events(self):self.add(type='player_joined');self.assertEqual(self.query()['events'],[])
    def test_overflow_returns_1000_and_complete_false(self):
        with closing(sqlite3.connect(self.cache)) as db, db:
            for i in range(1001):
                key='id:'+str(i);db.execute('INSERT INTO events VALUES(?,?,?,?,?)',('boot',key,999,'killed',json.dumps({'type':'killed','eventId':str(i)})));db.execute('INSERT INTO event_sequences VALUES(?,?,?)',(i+1,'boot',key))
        r=self.query();self.assertFalse(r['complete']);self.assertEqual(len(r['events']),1000)
    def test_freshness_boundary_and_error_future_missing_state(self):
        self.assertTrue(self.query(1015)['complete'])
        for last,error in [(984.99,None),(1001,None),(1000,'TimeoutError'),(None,None)]:
            with closing(sqlite3.connect(self.cache)) as db, db:db.execute('UPDATE sync_state SET last_success=?,error=?',(last,error))
            with self.assertRaises(h.ContractError) as e:self.query()
            self.assertEqual(e.exception.status,503)
    def test_caught_up_required_and_invalid_cursor_rejected(self):
        for generation,after,latest in [(None,0,0),('g',0,1),('g',2,1)]:
            with closing(sqlite3.connect(self.cache)) as db, db:db.execute('UPDATE sync_cursor SET generation=?,after_seq=?,latest_seq=?',(generation,after,latest))
            with self.assertRaises(h.ContractError) as e:self.query()
            self.assertEqual(e.exception.status,503)
    def test_missing_database_and_broken_event_fail_503(self):
        self.add()
        with closing(sqlite3.connect(self.cache)) as db, db:db.execute("UPDATE events SET body='broken'")
        with self.assertRaises(h.ContractError) as e:self.query()
        self.assertEqual(e.exception.status,503)
        self.cache.unlink()
        with self.assertRaises(h.ContractError) as e:self.query()
        self.assertEqual(e.exception.status,503)
    def test_exact_body_role_scope_and_age_validation(self):
        for extra in ['limit','query','windowSeconds']:
            p=self.payload();p[extra]=1
            with self.assertRaises(h.ContractError) as e:self.query(payload=p)
            self.assertEqual(e.exception.status,400)
        for field,value in [('role','member'),('groupId','untrusted')]:
            p=self.payload();p['actor'][field]=value
            with self.assertRaises(h.ContractError) as e:self.query(payload=p)
            self.assertEqual(e.exception.status,403)
        with self.assertRaises(h.ContractError) as e:self.query(payload=self.payload(879))
        self.assertEqual(e.exception.status,422)
    def test_snapshot_consistency_during_concurrent_commit(self):
        self.add()
        from unittest.mock import patch
        import service.recent_kills as module
        original=module.sqlite3.connect
        base=original('file:'+str(self.cache)+'?mode=ro',uri=True)
        base.execute('PRAGMA journal_mode')
        class Connection:
            def execute(inner,sql,*args):
                cursor=base.execute(sql,*args)
                if 'FROM sync_state' in sql:
                    cursor_rows=cursor.fetchall()
                    with closing(original(self.cache)) as writer, writer:
                        writer.execute("UPDATE sync_state SET error='TimeoutError'")
                        writer.execute('DELETE FROM events')
                    class Rows:
                        def fetchone(inner):return cursor_rows[0] if cursor_rows else None
                    return Rows()
                return cursor
            def close(inner):base.close()
        # WAL permits the writer while a readonly query retains the old snapshot.
        with closing(original(self.cache)) as writer, writer:writer.execute('PRAGMA journal_mode=WAL')
        with patch.object(module.sqlite3,'connect',return_value=Connection()):r=self.query()
        self.assertEqual(len(r['events']),1);self.assertTrue(r['complete'])
    def test_http_admin_only_with_existing_actor_contract(self):
        now=time.time()
        with closing(sqlite3.connect(self.cache)) as db, db:db.execute('UPDATE sync_state SET last_success=?',(now,))
        server=ThreadingHTTPServer(('127.0.0.1',0),h.app.api_handler(self.engine,'query','report','admin'))
        thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            for token,status in [('query',401),('report',401),(None,401),('admin',200)]:
                conn=http.client.HTTPConnection(*server.server_address,timeout=3);headers={'Content-Type':'application/json'}
                if token:headers['Authorization']='Bearer '+token
                conn.request('POST','/v1/moderation/recent-kills',json.dumps(self.payload(now)),headers)
                response=conn.getresponse();body=json.loads(response.read());conn.close();self.assertEqual(response.status,status,body)
                if status==200:self.assertEqual(body['timeBasis'],'archive_received_at')
        finally:server.shutdown();server.server_close();thread.join()
    def test_explicit_60_matches_legacy_body_exactly(self):
        self.add();self.assertEqual(self.query(),self.query(payload=dict(self.payload(),windowSeconds=60)))
    def test_300_second_boundaries_and_no_silent_60_truncation(self):
        for i,t in enumerate([699.999,700,939.999,940,1000,1000.001],1):self.add(i,t,event_id=str(i))
        r=self.query(payload=dict(self.payload(),windowSeconds=300))
        self.assertEqual(r['windowStart'],700);self.assertEqual(r['windowEnd'],1000);self.assertEqual(r['observedAt'],1000)
        self.assertEqual([e['archiveId'] for e in r['events']],['5','4','3','2']);self.assertEqual(r['timeBasis'],'archive_received_at');self.assertTrue(r['complete'])
    def test_only_integer_60_or_300_windows_accepted(self):
        for window in [None,True,False,60.0,300.0,0,-1,1,61,299,301,'300',[],{}]:
            with self.subTest(window=window),self.assertRaises(h.ContractError) as e:self.query(payload=dict(self.payload(),windowSeconds=window))
            self.assertEqual(e.exception.status,400);self.assertEqual(e.exception.code,'invalid_recent_kills_window')
    def test_required_original_fields_still_required_with_window(self):
        for p in [{'actor':self.payload()['actor'],'windowSeconds':300},{'requestedAt':1000,'windowSeconds':300},{'windowSeconds':300}]:
            with self.assertRaises(h.ContractError) as e:self.query(payload=p)
            self.assertEqual(e.exception.code,'invalid_recent_kills_fields')
    def test_300_window_exact_1000_complete_then_overflow_false(self):
        with closing(sqlite3.connect(self.cache)) as db,db:
            for i in range(1000):
                key='old:'+str(i);db.execute('INSERT INTO events VALUES(?,?,?,?,?)',('boot',key,750,'killed',json.dumps({'type':'killed','eventId':str(i)})));db.execute('INSERT INTO event_sequences VALUES(?,?,?)',(i+1,'boot',key))
        r=self.query(payload=dict(self.payload(),windowSeconds=300));self.assertTrue(r['complete']);self.assertEqual(len(r['events']),1000)
        self.add(1001,750,event_id='extra');r=self.query(payload=dict(self.payload(),windowSeconds=300));self.assertFalse(r['complete']);self.assertEqual(len(r['events']),1000);self.assertEqual(r['events'][0]['archiveId'],'1001')
        self.assertEqual(self.query()['events'],[])
    def test_300_preserves_freshness_and_caught_up_requirements(self):
        self.assertTrue(self.query(1015,dict(self.payload(1015),windowSeconds=300))['complete'])
        with self.assertRaises(h.ContractError) as e:self.query(1015.001,dict(self.payload(1015.001),windowSeconds=300))
        self.assertEqual(e.exception.status,503)
        with closing(sqlite3.connect(self.cache)) as db,db:db.execute('UPDATE sync_cursor SET latest_seq=1')
        with self.assertRaises(h.ContractError) as e:self.query(payload=dict(self.payload(),windowSeconds=300))
        self.assertEqual(e.exception.code,'recent_kills_sync_incomplete')
    def test_300_preserves_role_scope_and_age(self):
        for field,value in [('role','member'),('groupId','untrusted'),('botId','other'),('platform','other')]:
            p=dict(self.payload(),windowSeconds=300);p['actor'][field]=value
            with self.assertRaises(h.ContractError) as e:self.query(payload=p)
            self.assertEqual(e.exception.status,403)
        with self.assertRaises(h.ContractError) as e:self.query(payload=dict(self.payload(879),windowSeconds=300))
        self.assertEqual(e.exception.status,422)
    def test_300_retains_same_event_ids_across_servers(self):
        self.add(1,710,'a');self.add(2,710,'b');r=self.query(payload=dict(self.payload(),windowSeconds=300))
        self.assertEqual([e['archiveId'] for e in r['events']],['2','1']);self.assertEqual(len(r['events']),2)
    def test_http_300_member_rejected_and_admin_success(self):
        now=time.time()
        with closing(sqlite3.connect(self.cache)) as db,db:db.execute('UPDATE sync_state SET last_success=?',(now,))
        server=ThreadingHTTPServer(('127.0.0.1',0),h.app.api_handler(self.engine,'query','report','admin'));thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            for role,expected in [('member',403),('admin',200)]:
                p=dict(self.payload(now),windowSeconds=300);p['actor']['role']=role
                conn=http.client.HTTPConnection(*server.server_address,timeout=3);conn.request('POST','/v1/moderation/recent-kills',json.dumps(p),{'Content-Type':'application/json','Authorization':'Bearer admin'})
                response=conn.getresponse();body=json.loads(response.read());conn.close();self.assertEqual(response.status,expected)
                if expected==200:self.assertEqual(body['windowEnd']-body['windowStart'],300)
        finally:server.shutdown();server.server_close();thread.join()
