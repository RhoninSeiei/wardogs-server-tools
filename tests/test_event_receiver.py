import http.client
import json
import tempfile
import threading
import unittest
from event_receiver.receiver import Archive, Server, make_handler


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Archive(self.tmp.name + '/events.db')
        self.server = Server(('127.0.0.1', 0), make_handler(self.store, 'test-secret'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.db.close()
        self.tmp.cleanup()

    def post(self, doc, path='/wardogs', token='test-secret', extra=None):
        body = json.dumps(doc).encode()
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        headers.update(extra or {})
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request('POST', path, body, headers)
        res = conn.getresponse()
        result = res.status, json.loads(res.read())
        conn.close()
        return result

    def test_real_batch_shape_and_dedupe(self):
        doc = {'serverId': 'boot-1', 'serverName': 'test', 'events': [
            {'eventId': 'e1', 'type': 'killed', 'killerSteamId': '76561190000000001', 'victimName': 'v', 'eventTime': 12.5}]}
        self.assertEqual(self.post(doc)[1]['inserted'], 1)
        self.assertEqual(self.post(doc, '/wardogs/api/ingest/events')[1]['inserted'], 0)
        row = self.store.db.execute('SELECT body FROM events').fetchone()
        self.assertEqual(json.loads(row[0]), doc['events'][0])
        doc['serverId'] = 'boot-2'
        self.assertEqual(self.post(doc)[1]['inserted'], 1)

    def test_auth_failure_never_stores(self):
        self.assertEqual(self.post({'events': []}, token='bad')[0], 401)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0], 0)

    def test_invalid_batch_is_atomic(self):
        self.assertEqual(self.post({'events': [{'eventId': 'ok'}, {'eventId': 1}]})[0], 400)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
        self.assertEqual(self.post({'events': [None]})[0], 400)
        self.assertEqual(self.post({'events': 'bad'})[0], 400)

    def test_unknown_event_preserved_and_noid_deduplicated(self):
        doc = {'events': [{'type': 'future_type', 'custom': {'a': 1}}]}
        self.assertEqual(self.post(doc, '/api/ingest/events')[1]['inserted'], 1)
        self.assertEqual(self.post(doc)[1]['inserted'], 0)

    def test_routes_and_limits(self):
        self.assertEqual(self.post({'events': []}, '/other')[0], 404)
        self.assertEqual(self.post({'events': []}, extra={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.post({'events': []}, extra={'Content-Length': '1048577'})[0], 413)

    def test_storage_failure_not_acknowledged(self):
        self.store.db.execute('PRAGMA query_only=ON')
        self.assertEqual(self.post({'events': []})[0], 503)

    def test_busy_server_returns_503_then_recovers(self):
        for _ in range(8):
            self.assertTrue(self.server.slots.acquire(blocking=False))
        try:
            conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
            conn.request('GET', '/healthz')
            response = conn.getresponse()
            self.assertEqual(response.status, 503)
            response.read()
            conn.close()
        finally:
            for _ in range(8):
                self.server.slots.release()
        self.assertEqual(self.post({'events': []})[0], 200)

    def test_persists_on_reopen(self):
        self.post({'serverId': 's', 'events': [{'eventId': '1'}]})
        other = Archive(self.tmp.name + '/events.db')
        try:
            self.assertEqual(other.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
        finally:
            other.db.close()


if __name__ == '__main__':
    unittest.main()
