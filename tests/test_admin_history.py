import copy
from contextlib import closing
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import test_monitor as fixtures
from test_admin_moderation import config,admin_payload,ID,SCOPE
from service.moderation_ops import ContractError
from service.player_history import HistoryUnavailable

app=fixtures.app
OTHER='76561190000000002'


class Rcon(fixtures.FakeRcon):
    def __init__(self):super().__init__();self.reserved=[];self.calls=[]
    def request(self,method,path,body=None):
        self.calls.append((method,path))
        if path=='/v1/reserved-slots':return {'reservedSlots':self.reserved,'count':len(self.reserved)}
        return super().request(method,path,body)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.db'
        self.cfg=config();self.cfg['rejoin_guard_enabled']=True
        self.fake=Rcon();self.engine=app.Engine(self.path,self.fake,self.cfg)
        self.cache=self.path.parent/'local-cache/events.sqlite3';self.cache.parent.mkdir()
        with closing(sqlite3.connect(self.cache)) as c, c:
            c.executescript('CREATE TABLE source_snapshots(kind TEXT PRIMARY KEY,body TEXT,observed REAL); CREATE TABLE events(server TEXT,event_key TEXT,received REAL,type TEXT,body TEXT); CREATE TABLE export_meta(id INTEGER PRIMARY KEY,generation TEXT); INSERT INTO export_meta VALUES(1,"fixture");')
        self.roster([])
        with self.engine.lock,self.engine.db:self.engine.player_history.observe(ID,'OldFixture','roster_history',900,900)
    def tearDown(self):self.engine.close();self.tmp.cleanup()
    def roster(self,players,observed=1000):
        with closing(sqlite3.connect(self.cache)) as c, c:c.execute('INSERT OR REPLACE INTO source_snapshots VALUES(?,?,?)',('players',json.dumps({'players':players}),observed))
    def search(self,query='Fixture',now=1001,**changes):
        p={'query':query,'actor':admin_payload()['actor'],'requestedAt':1000};p.update(changes)
        return self.engine.admin_history.search(p,now=now)
    def submit(self,request_id='off-1',sid=ID,key='off-key',now=1000):
        return self.engine.admin_history.submit(admin_payload(now=now,request_id=request_id,identity=sid),key,now=now)
    def actions(self):return self.engine.kick_store.db.execute('SELECT count(*) FROM permanent_ban_actions').fetchone()[0]
    def test_offline_aliases_and_online_from_fresh_local_cache_only(self):
        self.roster([dict(fixtures.player(),name='CurrentFixture')])
        result=self.search('Fixture')
        self.assertEqual(result['observedAt'],1000)
        self.assertEqual(len(result['players']),1)
        p=result['players'][0];self.assertEqual(p['name'],'CurrentFixture');self.assertTrue(p['online']);self.assertIn('OldFixture',p['aliases'])
        self.assertEqual(self.fake.calls,[])
        self.roster([],observed=1005)
        self.assertFalse(self.search('OldFixture',now=1006)['players'][0]['online'])
    def test_stale_missing_and_malformed_rosters_fail_instead_of_offline(self):
        for players,observed in [([],900),([{'steamId':'bad'}],1000),([fixtures.player(),fixtures.player()],1000)]:
            self.roster(players,observed)
            with self.assertRaises(HistoryUnavailable):self.search()
        self.cache.unlink()
        with self.assertRaises(HistoryUnavailable):self.search()
    def test_event_identity_has_unknown_seen_time_and_invalid_identity_ignored(self):
        with closing(sqlite3.connect(self.cache)) as c, c:
            for sid,name in [(OTHER,'EventFixture'),('invalid','UntrustedFixture')]:
                c.execute('INSERT INTO events VALUES(?,?,?,?,?)',('boot',name,950,'player_left',json.dumps({'playerSteamId':sid,'playerName':name})))
        p=self.search('EventFixture')['players'][0]
        self.assertEqual(p['steamId'],OTHER);self.assertIsNone(p['lastSeen']);self.assertIn('event_history',p['sources'])
        self.assertEqual(self.search('UntrustedFixture')['players'],[])
    def test_welcome_backfill_is_persistent(self):
        with self.engine.db:
            self.engine.add_job('欢迎 FormerFixture 加入服务器！',800,recipient=OTHER,kind='welcome')
            self.engine.db.execute("DELETE FROM player_history_meta WHERE key='welcome_seeded'")
        self.engine.close();self.engine=app.Engine(self.path,self.fake,self.cfg)
        p=self.search('FormerFixture')['players'][0]
        self.assertEqual(p['steamId'],OTHER);self.assertEqual(p['lastSeen'],800);self.assertIn('welcome_history',p['sources'])
        with self.engine.db:self.engine.db.execute('DELETE FROM jobs')
        self.assertEqual(self.search('FormerFixture')['players'][0]['steamId'],OTHER)
    def test_query_bounds_and_literal_wildcards(self):
        with self.engine.db:
            self.engine.player_history.observe(OTHER,'Literal_%Fixture','roster_history',900,900)
            for i in range(25):self.engine.player_history.observe(str(76561190000000100+i),'ManyFixture'+str(i),'roster_history',900,900)
        self.assertEqual(len(self.search('_%')['players']),1)
        result=self.search('ManyFixture');self.assertEqual(len(result['players']),20);self.assertTrue(result['truncated'])
        self.assertEqual(self.search("' OR 1=1 --")['players'],[])
        self.assertEqual(self.search(ID)['players'][0]['steamId'],ID)
        for query in ['', 'x'*129,' trailing ']:
            with self.assertRaises(ContractError):self.search(query)
    def test_actor_role_scope_and_age_apply_to_search(self):
        for patch in [{'role':'member'},{'groupId':'other'},{'platform':'other'}]:
            actor=copy.deepcopy(admin_payload()['actor']);actor.update(patch)
            with self.assertRaises(ContractError):self.search(actor=actor)
        with self.assertRaises(ContractError):self.search(now=1121)
        self.assertEqual(self.fake.calls,[])
    def test_offline_registers_receipt_and_notices_without_game_writes(self):
        op=self.submit();self.assertEqual(op['restrictionStatus'],'pending');self.assertEqual(op['kickStatus'],'not_needed')
        result=self.engine.admin_history.process(op['operationId'],now=1001)
        self.assertEqual(result['restrictionStatus'],'restricted');self.assertEqual(result['nativeBanStatus'],'not_requested')
        self.assertEqual(result['source'],'admin');self.assertNotIn('reportId',result)
        self.assertEqual(self.actions(),1);self.assertEqual(len(result['announcementJobIds']),3)
        self.assertEqual(self.engine.kick_store.db.execute('SELECT count(*) FROM confirmed_kicks').fetchone()[0],0)
        self.assertTrue(all(method=='GET' for method,_ in self.fake.calls))
        text=' '.join(r[0] for r in self.engine.db.execute('SELECT message FROM jobs'))
        self.assertIn('本次未实际踢出',text);self.assertNotIn('已被踢出',text)
    def test_online_target_requires_kick_route(self):
        self.fake.players=[fixtures.player()];op=self.submit()
        result=self.engine.admin_history.process(op['operationId'],now=1001)
        self.assertEqual(result['error']['code'],'player_online_use_kick');self.assertEqual(self.actions(),0);self.assertEqual(self.fake.kicked,[])
    def test_unknown_id_does_not_create_restriction(self):
        op=self.submit(sid=OTHER)
        result=self.engine.admin_history.process(op['operationId'],now=1001)
        self.assertEqual(result['error']['code'],'player_not_known');self.assertEqual(self.actions(),0)
    def test_fresh_reserved_and_behavior_exemptions_win(self):
        self.fake.reserved=[ID];op=self.submit()
        result=self.engine.admin_history.process(op['operationId'],now=1001)
        self.assertEqual(result['error']['code'],'reserved_player_exempt');self.assertEqual(self.actions(),0)
        self.fake.reserved=[];self.engine.kick_store.sync_reserved([],1002);self.engine.behavior.add(ID,'Fixture',1002)
        op=self.submit('behavior',key='behavior',now=1002)
        result=self.engine.admin_history.process(op['operationId'],now=1003)
        self.assertEqual(result['error']['code'],'behavior_list_kick_exempt');self.assertEqual(self.actions(),0)
    def test_lost_scope_and_expiry_prevent_dispatch(self):
        op=self.submit();self.engine.config['admin_moderation_scopes']=[]
        self.assertEqual(self.engine.admin_history.process(op['operationId'],now=1001)['status'],'rejected')
        self.engine.config['admin_moderation_scopes']=[SCOPE]
        op=self.submit('expired',key='expired')
        self.assertEqual(self.engine.admin_history.process(op['operationId'],now=1121)['status'],'rejected');self.assertEqual(self.actions(),0)
    def test_scope_rechecked_after_live_lookup(self):
        original=self.fake.request
        def request(method,path,body=None):
            if path=='/v1/reserved-slots':self.engine.config['admin_moderation_scopes']=[]
            return original(method,path,body)
        self.fake.request=request;op=self.submit()
        self.assertEqual(self.engine.admin_history.process(op['operationId'],now=1001)['status'],'rejected');self.assertEqual(self.actions(),0)
    def test_idempotency_conflict_and_cross_action_request_id(self):
        self.submit()
        with self.assertRaises(ContractError) as error:self.submit(now=2000)
        self.assertEqual(error.exception.code,'idempotency_conflict')
    def test_immutable_retry_and_alias_do_not_repeat_registration(self):
        p=admin_payload(request_id='stable')
        first=self.engine.admin_history.submit(p,'stable',now=1000)
        self.engine.admin_history.process(first['operationId'],now=1001)
        for key in ['stable','alias']:
            other=self.engine.admin_history.submit(p,key,now=2000)
            self.assertEqual(first['operationId'],other['operationId']);self.assertTrue(other['deduplicated'])
        self.assertEqual(self.actions(),1)
        with self.assertRaises(ContractError):self.engine.moderation.submit_admin(p,'different-action',now=2000)
        changed=copy.deepcopy(p);changed['steamId']=OTHER
        with self.assertRaises(ContractError):self.engine.admin_history.submit(changed,'stable',now=2000)
    def test_existing_kick_request_cannot_be_repurposed(self):
        p=admin_payload();self.engine.moderation.submit_admin(p,'kick',now=1000)
        with self.assertRaises(ContractError):self.engine.admin_history.submit(p,'restriction',now=1000)
    def test_unknown_write_is_quarantined_and_never_replayed(self):
        calls=[]
        def fail(*args,**kwargs):calls.append(1);raise TimeoutError('simulated')
        self.engine.kick_store.restrict=fail;op=self.submit()
        result=self.engine.admin_history.process(op['operationId'],now=1001)
        self.assertEqual(result['restrictionStatus'],'unknown');self.assertEqual(result['kickStatus'],'not_needed')
        self.engine.admin_history.process(op['operationId'],now=1002)
        another=self.submit('another',key='another',now=1002)
        self.assertEqual(self.engine.admin_history.process(another['operationId'],now=1003)['status'],'superseded')
        self.assertEqual(calls,[1])
    def test_committed_receipt_survives_lost_ack(self):
        original=self.engine.kick_store.restrict
        def lost(*args,**kwargs):original(*args,**kwargs);raise TimeoutError('after commit')
        self.engine.kick_store.restrict=lost;op=self.submit()
        result=self.engine.admin_history.process(op['operationId'],now=1001)
        self.assertEqual(result['restrictionStatus'],'restricted');self.assertEqual(result['kickStatus'],'not_needed');self.assertEqual(self.actions(),1)
    def test_restart_recovers_receipt_without_restriction_replay(self):
        op=self.submit();oid=op['operationId']
        with self.engine.db:
            self.engine.db.execute("UPDATE admin_restrictions SET status='restriction_dispatching',name='Fixture' WHERE id=?",(oid,))
            self.engine.db.execute("INSERT INTO moderation_targets VALUES(?,?,'held',NULL)",(ID,oid))
        self.engine.kick_store.restrict(ID,'Fixture','reason','admin_offline',1001,action_id='admin-restriction:'+oid)
        self.engine.close();self.engine=app.Engine(self.path,self.fake,self.cfg)
        self.assertEqual(self.engine.admin_history.get(oid)['restrictionStatus'],'restricted');self.assertEqual(self.actions(),1)
        self.assertEqual(len(self.engine.admin_history.get(oid)['announcementJobIds']),3)
        self.engine.close();self.engine=app.Engine(self.path,self.fake,self.cfg)
        self.assertEqual(len(self.engine.admin_history.get(oid)['announcementJobIds']),3)
    def test_restart_without_receipt_is_unknown_and_not_replayed(self):
        op=self.submit();oid=op['operationId']
        with self.engine.db:
            self.engine.db.execute("UPDATE admin_restrictions SET status='restriction_dispatching' WHERE id=?",(oid,))
            self.engine.db.execute("INSERT INTO moderation_targets VALUES(?,?,'held',NULL)",(ID,oid))
        self.engine.close();self.engine=app.Engine(self.path,self.fake,self.cfg)
        self.assertEqual(self.engine.admin_history.get(oid)['restrictionStatus'],'unknown')
        self.assertFalse(self.engine.admin_history.process_one(now=1002));self.assertEqual(self.actions(),0)

    def test_concurrent_workers_claim_once(self):
        op=self.submit();entered=threading.Event();release=threading.Event();original=self.fake.request
        def request(method,path,body=None):
            if path=='/v1/players':entered.set();release.wait(2)
            return original(method,path,body)
        self.fake.request=request
        worker=threading.Thread(target=lambda:self.engine.admin_history.process(op['operationId'],now=1001))
        worker.start();self.assertTrue(entered.wait(2))
        self.engine.admin_history.process(op['operationId'],now=1001)
        release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual(self.actions(),1)
        self.assertEqual(len(self.engine.admin_history.get(op['operationId'])['announcementJobIds']),3)

    def test_alias_list_is_bounded(self):
        with self.engine.db:
            for i in range(25):self.engine.player_history.observe(ID,'AliasFixture'+str(i),'roster_history',900+i,900+i)
        result=self.search(ID)['players'][0]
        self.assertEqual(len(result['aliases']),20);self.assertTrue(result['aliasesTruncated'])

    def test_old_resolver_cannot_write_after_recovery_reclaims_it(self):
        op=self.submit();created=[];original=self.fake.request
        def request(method,path,body=None):
            if path=='/v1/players' and not created:
                created.append(app.Engine(self.path,self.fake,self.cfg))
            return original(method,path,body)
        self.fake.request=request
        try:
            self.engine.admin_history.process(op['operationId'],now=1001)
            self.assertEqual(self.actions(),0)
            result=created[0].admin_history.process(op['operationId'],now=1002)
            self.assertEqual(result['restrictionStatus'],'restricted');self.assertEqual(self.actions(),1)
        finally:
            for engine in created:engine.close()

    def test_cross_action_request_id_race_has_one_winner(self):
        other=app.Engine(self.path,self.fake,self.cfg);barrier=threading.Barrier(2);results=[]
        def submit(which):
            barrier.wait()
            try:
                if which=='kick':self.engine.moderation.submit_admin(admin_payload(),'race-kick',now=1000)
                else:other.admin_history.submit(admin_payload(),'race-restrict',now=1000)
                results.append('accepted')
            except ContractError as error:results.append(error.code)
        a=threading.Thread(target=submit,args=('kick',));b=threading.Thread(target=submit,args=('restrict',))
        try:
            a.start();b.start();a.join(5);b.join(5)
            self.assertFalse(a.is_alive());self.assertFalse(b.is_alive())
            self.assertCountEqual(results,['accepted','idempotency_conflict']);self.assertEqual(self.actions(),0)
        finally:other.close()

    def test_history_batches_mark_incomplete_until_tail_indexed(self):
        with closing(sqlite3.connect(self.cache)) as c,c:
            rows=[('boot',str(i),950,'player_left',json.dumps({'playerSteamId':OTHER,'playerName':'TailFixture' if i==5000 else 'BatchFixture'})) for i in range(5001)]
            c.executemany('INSERT INTO events VALUES(?,?,?,?,?)',rows)
        first=self.search('TailFixture');self.assertFalse(first['historyComplete']);self.assertEqual(first['players'],[])
        second=self.search('TailFixture');self.assertTrue(second['historyComplete']);self.assertEqual(second['players'][0]['steamId'],OTHER)

    def test_search_scope_is_rechecked_after_history_lookup(self):
        original=self.engine.player_history.search
        def search(query,now):
            result=original(query,now);self.engine.config['admin_moderation_scopes']=[];return result
        self.engine.player_history.search=search
        with self.assertRaises(ContractError):self.search()
        self.assertEqual(self.engine.db.execute('SELECT count(*) FROM admin_search_audit').fetchone()[0],0)

    def test_config_disable_and_upstream_failure_are_safe(self):
        self.engine.config['rejoin_guard_enabled']=False
        with self.assertRaises(ContractError):self.submit()
        self.engine.config['rejoin_guard_enabled']=True;op=self.submit()
        self.fake.fail=True
        self.assertEqual(self.engine.admin_history.process(op['operationId'],now=1001)['restrictionStatus'],'failed');self.assertEqual(self.actions(),0)
    def test_target_lock_is_shared_with_online_kick(self):
        kick=self.engine.moderation.submit_admin(admin_payload(),'kick',now=1000)
        self.engine.moderation._claim(kick['operationId'],1001)
        op=self.submit()
        self.assertEqual(self.engine.admin_history.process(op['operationId'],now=1001)['status'],'superseded');self.assertEqual(self.actions(),0)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.fixture=HistoryTests();self.fixture.setUp();self.engine=self.fixture.engine
        self.server=ThreadingHTTPServer(('127.0.0.1',0),app.api_handler(self.engine,'query','report','admin'))
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.fixture.tearDown()
    def call(self,path,payload,key='admin',method='POST',idem='http'):
        c=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        c.request(method,path,json.dumps(payload) if payload is not None else None,{'Authorization':'Bearer '+key,'Content-Type':'application/json','Idempotency-Key':idem})
        r=c.getresponse();value=r.status,json.loads(r.read());c.close();return value
    def test_admin_only_search_and_restriction_polling(self):
        p={'query':'Fixture','actor':admin_payload()['actor'],'requestedAt':time.time()}
        self.fixture.roster([],time.time())
        for key in ['query','report','wrong']:
            self.assertEqual(self.call('/v1/moderation/player-search',p,key=key)[0],401)
            self.assertEqual(self.call('/v1/moderation/admin-restrictions',admin_payload(now=time.time()),key=key)[0],401)
        self.assertEqual(self.call('/v1/moderation/player-search',p)[0],200)
        status,op=self.call('/v1/moderation/admin-restrictions',admin_payload(now=time.time()))
        self.assertEqual(status,202)
        path='/v1/moderation/operations/'+op['operationId']
        self.assertEqual(self.call(path,None,method='GET')[0],200)
        self.assertEqual(self.call(path,None,key='report',method='GET')[0],404)
        self.assertEqual(self.engine.kick_store.db.execute('SELECT count(*) FROM permanent_ban_actions').fetchone()[0],0)
    def test_stale_history_http_503_and_no_fake_offline(self):
        p={'query':'Fixture','actor':admin_payload()['actor'],'requestedAt':time.time()}
        status,data=self.call('/v1/moderation/player-search',p)
        self.assertEqual(status,503);self.assertEqual(data['error']['code'],'player_snapshot_unavailable')


if __name__=='__main__':unittest.main()
