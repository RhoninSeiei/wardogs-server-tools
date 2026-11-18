import copy
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

import test_monitor as fixtures
from service.moderation_ops import ContractError, validate_admin_config
from test_moderation_ops import report

app=fixtures.app
ID='76561190000000001'
SCOPE={'platform':'aiocqhttp:test-instance','botId':'10001','groupId':'1000000001'}

def admin_payload(now=1000,request_id='a-1',identity=ID):
    return {'requestId':request_id,'steamId':identity,
            'actor':dict(SCOPE,memberId='20002',role='admin'),'requestedAt':now}

def config():
    value=copy.deepcopy(app.DEFAULT_CONFIG)
    value['admin_moderation_scopes']=[copy.deepcopy(SCOPE)]
    return value

class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'state.db'
        self.fake=fixtures.FakeRcon();self.fake.players=[fixtures.player()]
        self.config=config();self.engine=app.Engine(self.path,self.fake,self.config)
    def tearDown(self):self.engine.close();self.temp.cleanup()
    def submit(self,payload=None,key='a-key',now=1000):
        return self.engine.moderation.submit_admin(payload or admin_payload(),key,now=now)
    def test_distinct_contract_acceptance_audit_and_fixed_notices(self):
        accepted=self.submit()
        self.assertEqual(accepted['source'],'admin')
        self.assertEqual(accepted['requestId'],'a-1')
        self.assertNotIn('reportId',accepted)
        self.assertEqual(accepted['actor'],admin_payload()['actor'])
        self.assertEqual(self.fake.kicked,[])
        stored=json.loads(self.engine.db.execute('SELECT payload_json FROM moderation_operations WHERE id=?',(accepted['operationId'],)).fetchone()[0])
        self.assertEqual(stored,admin_payload())
        result=self.engine.moderation.process(accepted['operationId'],now=1001)
        self.assertEqual(result['kickStatus'],'confirmed')
        self.assertEqual(self.fake.kicked[0][1],'管理员手动踢出')
        self.assertEqual(len(result['announcementJobIds']),3)
    def test_idempotency_alias_and_conflict(self):
        first=self.submit();again=self.submit(now=2000)
        alias=self.submit(key='alias',now=2000)
        self.assertEqual(first['operationId'],again['operationId'])
        self.assertEqual(first['operationId'],alias['operationId'])
        self.assertTrue(alias['deduplicated'])
        changed=admin_payload(identity='76561190000000002')
        with self.assertRaises(ContractError) as e:self.submit(changed)
        self.assertEqual(e.exception.status,409)
        with self.assertRaises(ContractError):self.submit(changed,key='new-alias')
    def test_key_and_exact_id_required(self):
        with self.assertRaises(ContractError):self.submit(key=None)
        for identity in ['player-name',12345678901234567,'１２３４５６７８９０１２３４５６７','7656119000000000','76561190000000001 ']:
            with self.subTest(identity=identity):
                with self.assertRaises(ContractError) as e:self.submit(admin_payload(identity=identity))
                self.assertEqual(e.exception.status,400)
    def test_roles_and_scope_not_user_controlled(self):
        for field,value in [('role','member'),('role','owner '),('platform','other'),('platform','aiocqhttp'),('platform','aiocqhttp:other-instance'),('botId','other'),('groupId','other'),('memberId','')]:
            p=admin_payload();p['actor'][field]=value
            with self.subTest(field=field,value=value):
                with self.assertRaises(ContractError):self.submit(p)
        p=admin_payload();p['actor']['role']='owner'
        self.assertEqual(self.submit(p)['status'],'accepted')
    def test_strict_fields_and_no_report_evidence(self):
        for key,value in [('reason',{'zh':'custom'}),('decision',{'verified':True}),('evidenceRefs',['fake']),('host','other')]:
            p=admin_payload();p[key]=value
            with self.assertRaises(ContractError):self.submit(p)
        p=admin_payload();p['actor']['extra']='bad'
        with self.assertRaises(ContractError):self.submit(p)
    def test_age_finite_and_revalidated_before_dispatch(self):
        for timestamp in [0,879,1031,True,float('nan'),float('inf'),'1000']:
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(ContractError):self.submit(admin_payload(now=timestamp))
        accepted=self.submit()
        result=self.engine.moderation.process(accepted['operationId'],now=1121)
        self.assertEqual(result['status'],'rejected')
        self.assertEqual(self.fake.kicked,[])
    def test_scope_revocation_cancels_accepted_operation(self):
        op=self.submit();self.engine.config['admin_moderation_scopes']=[]
        result=self.engine.moderation.process(op['operationId'],now=1001)
        self.assertEqual(result['status'],'rejected');self.assertEqual(self.fake.kicked,[])
    def test_scope_is_rechecked_after_live_identity_lookup(self):
        op=self.submit();original=self.fake.request
        def request(method,path,body=None):
            if path=='/v1/players':self.engine.config['admin_moderation_scopes']=[]
            return original(method,path,body)
        self.fake.request=request
        result=self.engine.moderation.process(op['operationId'],now=1001)
        self.assertEqual(result['status'],'rejected');self.assertEqual(self.fake.kicked,[])
    def test_ttl_boundaries_and_invalid_scope_configuration(self):
        for requested in [880,1030]:
            op=self.submit(admin_payload(now=requested,request_id=str(requested)),key=str(requested))
            self.assertEqual(op['status'],'accepted')
        for patch in [{'admin_moderation_scopes':[{'platform':'qq'}]},
                      {'admin_moderation_scopes':[dict(SCOPE,groupId='*',extra='x')]},
                      {'admin_moderation_max_age_seconds':True},
                      {'admin_moderation_future_skew_seconds':float('nan')}]:
            value=config();value.update(patch)
            with self.assertRaises(ValueError):validate_admin_config(value)
    def test_report_and_admin_share_single_target_coordination(self):
        op=self.submit()
        r=self.engine.moderation.submit(report(), 'r-key',now=1000)
        self.engine.moderation.process(op['operationId'],now=1001)
        result=self.engine.moderation.process(r['operationId'],now=1001)
        self.assertEqual(result['status'],'superseded');self.assertEqual(len(self.fake.kicked),1)
    def test_worker_processes_admin_without_report_forgery(self):
        op=self.submit();self.assertTrue(self.engine.moderation.process_one(now=1001))
        self.assertEqual(self.engine.moderation.get(op['operationId'])['kickStatus'],'confirmed')
    def test_timeout_is_unknown_and_not_replayed_after_restart(self):
        # Fixture uses a request callback to simulate a timeout after dispatch.
        original=self.fake.request
        def request(method,path,body=None):
            if path.endswith('/kick'):raise TimeoutError('injected')
            return original(method,path,body)
        self.fake.request=request
        op=self.submit();result=self.engine.moderation.process(op['operationId'],now=1001)
        self.assertEqual(result['kickStatus'],'unknown')
        self.engine.close();self.engine=app.Engine(self.path,self.fake,self.config)
        self.assertFalse(self.engine.moderation.process_one(now=1002))
        self.assertEqual(self.submit(now=1002)['operationId'],op['operationId'])
        second=self.submit(admin_payload(now=1002,request_id='new-request'),key='new-key',now=1002)
        second_result=self.engine.moderation.process(second['operationId'],now=1002)
        self.assertEqual(second_result['status'],'superseded')
    def test_offline_is_not_a_confirmed_kick(self):
        self.fake.players=[];op=self.submit()
        result=self.engine.moderation.process(op['operationId'],now=1001)
        self.assertEqual(result['status'],'offline');self.assertEqual(self.fake.kicked,[])
        for (message,) in self.engine.db.execute('SELECT message FROM jobs'):
            self.assertNotIn('核验',message)
    def test_reserved_and_behavior_exemptions_win(self):
        self.engine.close();self.config['rejoin_guard_enabled']=True
        self.engine=app.Engine(self.path,self.fake,self.config)
        self.engine.kick_store.sync_reserved([ID],1000)
        op=self.submit();result=self.engine.moderation.process(op['operationId'],now=1001)
        self.assertEqual(result['error']['code'],'reserved_player_exempt')
        self.assertEqual(self.fake.kicked,[])

        self.engine.kick_store.sync_reserved([],1002)
        self.engine.behavior.add(ID,'玩家',1002)
        second=self.submit(admin_payload(now=1002,request_id='behavior-exempt'),key='behavior-key',now=1002)
        result=self.engine.moderation.process(second['operationId'],now=1003)
        self.assertEqual(result['error']['code'],'behavior_list_kick_exempt')
        self.assertEqual(self.fake.kicked,[])

    def test_private_scope_file_only_changes_admin_authorization(self):
        p=Path(self.temp.name)/'scopes.json';p.write_text(json.dumps({'admin_moderation_scopes':[SCOPE]}))
        original=copy.deepcopy(app.DEFAULT_CONFIG);actual=copy.deepcopy(original)
        app.load_admin_scopes(actual,p)
        self.assertEqual(actual.pop('admin_moderation_scopes'),[SCOPE])
        original.pop('admin_moderation_scopes')
        self.assertEqual(actual,original)

    def test_invalid_private_scope_file_cannot_change_policy(self):
        p=Path(self.temp.name)/'scopes.json'
        original=copy.deepcopy(app.DEFAULT_CONFIG)
        for data in [{'admin_moderation_scopes':[SCOPE],'high_kill_kick':False}, {'admin_moderation_scopes':[{}]}]:
            p.write_text(json.dumps(data));actual=copy.deepcopy(original)
            with self.assertRaises(ValueError):app.load_admin_scopes(actual,p)
            self.assertEqual(actual,original)

    def test_public_state_redacts_private_scope_identifiers(self):
        result=self.engine.snapshot()
        self.assertNotIn('admin_moderation_scopes',result['config'])
        self.assertEqual(result['adminModeration'],{'scopeCount':1})

class AdminHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.fake=fixtures.FakeRcon();self.fake.players=[fixtures.player()]
        self.engine=app.Engine(Path(self.temp.name)/'state.db',self.fake,config())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),app.api_handler(self.engine,'query-token','report-token','admin-token'))
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.engine.close();self.temp.cleanup()
    def call(self,method,path,body=None,key='admin-token',token='admin-http'):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        headers={'Content-Type':'application/json'}
        if key:headers['Authorization']='Bearer '+key
        if token:headers['Idempotency-Key']=token
        c.request(method,path,json.dumps(body) if body is not None else None,headers)
        r=c.getresponse();result=(r.status,json.loads(r.read()));c.close();return result
    def test_separate_key_acceptance_poll_and_no_inline_kick(self):
        p=admin_payload(now=time.time())
        for key in [None,'query-token','report-token']:
            self.assertEqual(self.call('POST','/v1/moderation/admin-kicks',p,key=key)[0],401)
        status,result=self.call('POST','/v1/moderation/admin-kicks',p)
        self.assertEqual(status,202);self.assertEqual(self.fake.kicked,[])
        path='/v1/moderation/operations/'+result['operationId']
        self.assertEqual(self.call('GET',path)[0],200)
        self.assertEqual(self.call('GET',path,key='report-token')[0],404)
        self.assertEqual(self.call('GET','/v1/state')[0],401)
        self.assertEqual(self.call('POST','/v1/moderation/kicks',report(now=time.time()))[0],401)
    def test_admin_cannot_read_report_operations(self):
        r=self.engine.moderation.submit(report(now=time.time()),'report-key')
        self.assertEqual(self.call('GET','/v1/moderation/operations/'+r['operationId'])[0],404)
    def test_missing_key_and_payload_validation(self):
        p=admin_payload(now=time.time())
        self.assertEqual(self.call('POST','/v1/moderation/admin-kicks',p,token=None)[0],400)
        p['actor']['role']='member'
        self.assertEqual(self.call('POST','/v1/moderation/admin-kicks',p)[0],403)
    def test_missing_admin_configuration_and_key_collision(self):
        with self.assertRaises(ValueError):app.api_handler(self.engine,'q','r','q')
        with self.assertRaises(ValueError):app.api_handler(self.engine,'q','r','r')
        self.server.RequestHandlerClass=app.api_handler(self.engine,'query-token','report-token')
        self.assertEqual(self.call('POST','/v1/moderation/admin-kicks',admin_payload(now=time.time()))[0],503)

if __name__=='__main__':unittest.main()
