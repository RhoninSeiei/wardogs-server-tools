import http.client
import json
import tempfile
import threading
import unittest
from urllib.parse import urlencode
from event_receiver.receiver import Archive, Server, make_handler

A = '76561198000000001'
B = '76561198000000002'
C = '76561198000000003'


class KillQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Archive(self.tmp.name + '/events.db')
        self.ingest([
            {'eventId': 'e1', 'type': 'killed', 'killerSteamId': A, 'killerName': 'Alice', 'victimSteamId': B, 'victimName': 'Bob', 'cause': 'Id.Item.AK74M', 'distance': 1234.5, 'eventTime': 50},
            {'eventId': 'e2', 'type': 'killed', 'killerSteamId': B, 'killerName': 'Bob', 'victimSteamId': A, 'victimName': 'Alice', 'distance': 0, 'eventTime': 60},
            {'eventId': 'e3', 'type': 'killed', 'victimSteamId': A, 'victimName': 'NewAlice', 'cause': None, 'eventTime': 4},
            {'eventId': 'e4', 'type': 'player_joined', 'playerSteamId': A, 'playerName': 'Alice'},
        ])
        self.server = Server(('127.0.0.1', 0), make_handler(self.store, 'push-only', query_token='read-only'))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.db.close()
        self.tmp.cleanup()

    def ingest(self, events, server='boot1'):
        return self.store.ingest(json.dumps({'serverId': server, 'events': events}).encode(), '/wardogs')

    def get(self, params=None, token='read-only', raw=None):
        path = '/wardogs/v1/kills?' + (raw if raw is not None else urlencode(params or {'steamId': A}))
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request('GET', path, headers={'Authorization': 'Bearer ' + token})
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        conn.close()
        return result

    def test_both_directions_and_optional_fields(self):
        status, data = self.get()
        self.assertEqual(status, 200)
        self.assertEqual([e['eventId'] for e in data['events']], ['e3', 'e2', 'e1'])
        self.assertEqual([e['relation'] for e in data['events']], ['death', 'death', 'kill'])
        self.assertIsNone(data['events'][0]['killer']['steamId'])
        self.assertIsNone(data['events'][0]['weapon'])
        self.assertIsNone(data['events'][0]['distance'])
        self.assertEqual(data['events'][1]['distance'], 0)
        self.assertEqual(data['events'][2]['weapon'], 'Id.Item.AK74M')
        self.assertEqual(data['events'][2]['distance'], 1234.5)
        self.assertIsNone(data['distanceUnit'])
        self.assertEqual(data['order'], 'archive_desc')

    def test_direction_filters(self):
        self.assertEqual([e['eventId'] for e in self.get({'steamId': A, 'direction': 'kills'})[1]['events']], ['e1'])
        self.assertEqual([e['eventId'] for e in self.get({'steamId': A, 'direction': 'deaths'})[1]['events']], ['e3', 'e2'])

    def test_exact_casefold_name_resolves_alias_history(self):
        status, data = self.get({'name': 'aLiCe'})
        self.assertEqual(status, 200)
        self.assertEqual(data['player']['steamId'], A)
        self.assertEqual(len(data['events']), 3)
        self.assertEqual(self.get({'name': 'Ali'})[0], 404)
        self.assertEqual(self.get({'name': "' OR 1=1 --"})[0], 404)

    def test_ambiguous_nickname_returns_candidates_without_events(self):
        self.ingest([{'eventId': 'e5', 'type': 'killed', 'killerSteamId': C, 'killerName': 'ALICE', 'victimSteamId': B}])
        status, data = self.get({'name': 'Alice'})
        self.assertEqual(status, 409)
        self.assertEqual(data['error']['code'], 'ambiguous_player')
        self.assertEqual({p['steamId'] for p in data['candidates']}, {A, C})
        self.assertNotIn('events', data)

    def test_pagination_stable_and_deduplication(self):
        first = self.get({'steamId': A, 'limit': '2'})[1]
        self.assertEqual(len(first['events']), 2)
        self.assertIsNotNone(first['nextBefore'])
        self.ingest([{'eventId': 'e2', 'type': 'killed', 'killerSteamId': B, 'killerName': 'Bob', 'victimSteamId': A}])
        self.ingest([{'eventId': 'later', 'type': 'killed', 'killerSteamId': A, 'victimSteamId': B}])
        second = self.get({'steamId': A, 'limit': '2', 'before': first['nextBefore']})[1]
        self.assertEqual([e['eventId'] for e in second['events']], ['e1'])
        self.assertIsNone(second['nextBefore'])

    def test_auth_scopes(self):
        for token in ['push-only', '', 'wrong']:
            self.assertEqual(self.get(token=token)[0], 401)
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request('POST', '/wardogs', b'{"events":[]}', {'Content-Type': 'application/json', 'Authorization': 'Bearer read-only'})
        self.assertEqual(conn.getresponse().status, 401)
        conn.close()

    def test_reject_invalid_parameters(self):
        for query in ['', 'name=Alice&steamId='+A, 'steamId=123', 'steamId='+A+'&limit=0', 'steamId='+A+'&limit=101', 'steamId='+A+'&direction=other', 'steamId='+A+'&before=-1', 'name=Alice&name=Bob', 'name=Alice&unknown=x', 'name=']:
            with self.subTest(query=query):
                self.assertEqual(self.get(raw=query)[0], 400)

    def test_unknown_steam_returns_empty(self):
        status, data = self.get({'steamId': C})
        self.assertEqual(status, 200)
        self.assertEqual(data['events'], [])

    def test_suicide_is_one_event_matching_both_directions(self):
        self.ingest([{'eventId': 'suicide', 'type': 'killed', 'killerSteamId': A, 'victimSteamId': A}])
        data = self.get()[1]
        self.assertEqual(sum(e['eventId']=='suicide' for e in data['events']), 1)
        self.assertEqual(data['events'][0]['relation'], 'suicide')

    def test_invalid_optional_data_never_fabricates_numbers(self):
        self.ingest([{'eventId':'bad', 'type':'killed', 'victimSteamId': A, 'distance':True, 'cause':123, 'eventTime':'bad', 'teamkill':'false'}])
        event = self.get()[1]['events'][0]
        for field in ['distance','weapon','eventTime','teamkill']:
            self.assertIsNone(event[field])

    def test_unicode_and_missing_identity(self):
        self.ingest([{'eventId': 'zh', 'type': 'killed', 'killerName': '测试玩家', 'killerSteamId': C, 'victimSteamId': B}])
        self.assertEqual(self.get({'name': '测试玩家'})[1]['player']['steamId'], C)
        self.ingest([{'eventId': 'unknown', 'type': 'killed', 'killerName': 'UnknownNoId', 'victimSteamId': B}])
        self.assertEqual(self.get({'name': 'UnknownNoId'})[0], 404)

    def test_query_failure_is_503(self):
        with self.store.lock, self.store.db:
            self.store.db.execute('ALTER TABLE kill_records RENAME TO gone')
        status, data = self.get()
        self.assertEqual(status, 503)
        self.assertEqual(data, {'error': {'code': 'query_unavailable'}})

    def test_index_failure_rolls_back_ingest(self):
        before = self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        with self.store.lock, self.store.db:
            self.store.db.execute('ALTER TABLE kill_records RENAME TO gone')
        import sqlite3
        with self.assertRaises(sqlite3.Error):
            self.ingest([{'eventId': 'rollback', 'type': 'killed', 'killerSteamId': A}])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], before)

    def test_repeated_id_in_other_boot_is_distinct(self):
        self.ingest([{'eventId': 'e1', 'type': 'killed', 'killerSteamId': A, 'victimSteamId': B}], server='boot2')
        events = self.get()[1]['events']
        self.assertEqual(sum(e['eventId']=='e1' for e in events), 2)
        self.assertEqual({e['serverId'] for e in events if e['eventId']=='e1'}, {'boot1', 'boot2'})

    def test_reopen_backfills_preexisting_archive(self):
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM kill_records')
        other = Archive(self.tmp.name + '/events.db')
        try:
            self.assertEqual(other.query_kills({'steamId': [A]})['events'][0]['eventId'], 'e3')
        finally:
            other.db.close()


if __name__ == '__main__':
    unittest.main()
