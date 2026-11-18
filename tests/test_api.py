import http.client
from http.server import ThreadingHTTPServer
import json
import threading
import tempfile
from pathlib import Path
import unittest
import time

import test_monitor as fixtures
from test_moderation_ops import report

app = fixtures.app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = app.Engine(Path(self.temp.name) / 'state.db', fixtures.FakeRcon(), app.DEFAULT_CONFIG)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), app.api_handler(self.engine, 'test-token', 'test-exec-token'))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.engine.close()
        self.temp.cleanup()

    def request_api(self, method, path, body=None, auth=True, token=None, credential=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Content-Type': 'application/json'}
        if auth:
            selected = credential or ('test-exec-token' if path.startswith('/v1/moderation/') else 'test-token')
            headers['Authorization'] = 'Bearer ' + selected
        if token:
            headers['Idempotency-Key'] = token
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        status, data = response.status, json.loads(response.read())
        connection.close()
        return status, data

    def test_http_auth_enqueue_and_job_status(self):
        for method, path, body in [('GET', '/v1/state', None), ('GET', '/v1/players', None),
                                  ('POST', '/v1/broadcast', {'message': 'test'})]:
            self.assertEqual(self.request_api(method, path, body, auth=False)[0], 401)
        first = self.request_api('POST', '/v1/broadcast', {'message': '公告', 'english': 'Notice', 'japanese': 'お知らせ'}, token='one')
        second = self.request_api('POST', '/v1/broadcast', {'message': '公告', 'english': 'Notice', 'japanese': 'お知らせ'}, token='one')
        self.assertEqual(first, second)
        self.assertEqual(first[0], 202)
        self.assertEqual(len(first[1]['jobIds']), 3)
        job_id = first[1]['jobId']
        self.assertEqual(self.request_api('GET', '/v1/jobs/' + job_id)[1]['status'], 'queued')
        self.engine.send_one(now=100)
        self.assertEqual(self.request_api('GET', '/v1/jobs/' + job_id)[1]['status'], 'sent')
        self.assertEqual(self.request_api('POST', '/v1/broadcast', {'message': 'different'}, token='one')[0], 400)
        self.assertEqual(self.request_api('GET', '/v1/jobs/missing')[0], 404)
        self.assertEqual(self.request_api('GET', '/openapi.json')[1]['openapi'], '3.0.3')

    def test_single_language_broadcast_is_rejected(self):
        self.assertEqual(self.request_api('POST', '/v1/broadcast', {'message': '仅中文'})[0], 400)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 0)

    def test_health_is_stale_until_successful_poll(self):
        self.assertEqual(self.request_api('GET', '/healthz', auth=False)[0], 503)
        self.engine.poll()
        self.assertEqual(self.request_api('GET', '/healthz', auth=False)[0], 200)
        self.engine.last_poll -= 100
        self.assertEqual(self.request_api('GET', '/healthz', auth=False)[0], 503)

    def test_moderation_auth_acceptance_get_and_conflict(self):
        payload = report(now=time.time())
        self.assertEqual(self.request_api('POST', '/v1/moderation/kicks', payload, auth=False, token='report-1')[0], 401)
        self.assertEqual(self.request_api('GET', '/v1/moderation/operations/missing', auth=False)[0], 401)
        self.assertEqual(self.request_api('POST', '/v1/moderation/kicks', payload, token='report-1', credential='test-token')[0], 401)
        self.assertEqual(self.request_api('GET', '/v1/moderation/operations/missing', credential='test-token')[0], 401)
        self.assertEqual(self.request_api('GET', '/v1/state', credential='test-exec-token')[0], 401)
        self.assertEqual(self.request_api('POST', '/v1/moderation/kicks', payload)[0], 400)
        status, first = self.request_api('POST', '/v1/moderation/kicks', payload, token='report-1')
        self.assertEqual(status, 202)
        self.assertEqual(first['kickStatus'], 'not_started')
        self.assertEqual(first['announcementJobIds'], [])
        self.assertEqual(self.engine.rcon.kicked, [])
        status, again = self.request_api('POST', '/v1/moderation/kicks', payload, token='report-1')
        self.assertEqual(status, 202)
        self.assertEqual(first['operationId'], again['operationId'])
        self.assertTrue(again['deduplicated'])
        self.assertEqual(self.request_api('GET', '/v1/moderation/operations/' + first['operationId'])[1]['status'], 'accepted')
        changed = dict(payload, evidenceVersion='2')
        self.assertEqual(self.request_api('POST', '/v1/moderation/kicks', changed, token='report-1')[0], 409)
        self.assertEqual(self.request_api('GET', '/v1/moderation/operations/missing')[0], 404)

    def test_moderation_rule_policy_is_not_arbitrary_rcon(self):
        payload = report(now=time.time())
        payload['ruleId'] = 'base_camping'
        self.assertEqual(self.request_api('POST', '/v1/moderation/kicks', payload, token='report-1')[0], 422)
        payload = report(now=time.time())
        payload['host'] = 'untrusted-host'
        self.assertEqual(self.request_api('POST', '/v1/moderation/kicks', payload, token='report-1')[0], 400)
        self.assertEqual(self.engine.rcon.kicked, [])

    def test_missing_execution_key_preserves_old_api_and_disables_punishment(self):
        self.server.RequestHandlerClass = app.api_handler(self.engine, 'test-token')
        result = self.request_api('POST', '/v1/moderation/kicks', report(now=time.time()), token='one')
        self.assertEqual(result, (503, {'error': {'code': 'moderation_not_configured'}}))
        self.assertEqual(self.request_api('GET', '/v1/state')[0], 200)
        self.assertEqual(self.engine.rcon.kicked, [])
        with self.assertRaises(ValueError):
            app.api_handler(self.engine, 'same-token', 'same-token')

    def test_report_http_to_fake_kick_to_three_queued_notices(self):
        self.engine.rcon.players = [fixtures.player()]
        status, accepted = self.request_api('POST', '/v1/moderation/kicks', report(now=time.time()), token='full-flow')
        self.assertEqual(status, 202)
        self.assertTrue(self.engine.moderation.process_one())
        path = '/v1/moderation/operations/' + accepted['operationId']
        current = self.request_api('GET', path)[1]
        self.assertEqual(current['kickStatus'], 'confirmed')
        self.assertEqual(current['announcementStatus'], 'pending')
        self.assertEqual(len(current['announcementJobIds']), 3)
        self.assertEqual(len(self.engine.rcon.kicked), 1)
        start = time.time()
        for seconds in [0, 3, 6]:
            self.engine.send_one(now=start + seconds)
        done = self.request_api('GET', path)[1]
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['announcementStatus'], 'sent')
        self.assertEqual(len(self.engine.rcon.sent), 3)
