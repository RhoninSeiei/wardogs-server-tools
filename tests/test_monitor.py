import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('monitor', os.environ.get('WARDOGS_TEST_APP', str(Path(__file__).parents[1] / 'service' / 'app.py')))
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class FakeRcon:
    def __init__(self):
        self.players = []
        self.sent = []
        self.paths = []
        self.kicked = []
        self.fail = False

    def request(self, method, path, body=None):
        if self.fail:
            raise RuntimeError('upstream unavailable')
        if path == '/v1/players':
            return {'players': self.players, 'count': len(self.players)}
        if path.endswith('/kick'):
            self.kicked.append((path, body['reason']))
            return {'ok': True, 'pending': False}
        self.sent.append(body['message'])
        self.paths.append(path)
        return {'ok': True}


def player(kills=0, deaths=0, identity='76561190000000001'):
    return {'steamId': identity, 'name': '玩家', 'kills': kills, 'deaths': deaths}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.db'
        self.config = copy.deepcopy(app.DEFAULT_CONFIG)
        self.config['global_rules'] = self.config['global_rules'][:3]
        self.rcon = FakeRcon()
        self.engine = app.Engine(self.path, self.rcon, self.config)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def test_strict_kd_boundary_and_zero_deaths(self):
        self.assertFalse(app.high_kd(player(11, 1), 11))
        self.assertTrue(app.high_kd(player(12, 1), 11))
        self.assertFalse(app.high_kd(player(0, 0), 11))
        self.assertFalse(app.high_kd(player(11, 0), 11))
        self.assertTrue(app.high_kd(player(12, 0), 11))
        self.assertFalse(app.high_kd(player(-1, 0), 11))
        self.assertFalse(app.high_kd(player('bad', 1), 11))

    def test_join_messages_persist_and_confirmed_rejoin(self):
        self.rcon.players = [player()]
        self.engine.poll(now=100)
        initial = self.engine.snapshot()['queueDepth']
        self.assertEqual(initial, 4)
        self.engine.poll(now=130)
        self.assertEqual(self.engine.snapshot()['queueDepth'], initial)
        self.engine.close()
        self.engine = app.Engine(self.path, self.rcon, self.config)
        self.engine.poll(now=160)
        self.assertEqual(self.engine.snapshot()['queueDepth'], initial)
        self.rcon.players = []
        self.engine.poll(now=190)
        self.rcon.players = [player()]
        self.engine.poll(now=220)
        self.assertEqual(self.engine.snapshot()['queueDepth'], initial)
        self.rcon.players = []
        self.engine.poll(now=250)
        self.engine.poll(now=310)
        self.rcon.players = [player()]
        self.engine.poll(now=340)
        self.assertEqual(self.engine.snapshot()['queueDepth'], initial + 4)

    def test_poll_outage_keeps_presence_and_does_not_rewelcome(self):
        self.rcon.players = [player()]
        self.engine.poll(now=100)
        self.rcon.fail = True
        with self.assertRaises(RuntimeError):
            self.engine.poll(now=130)
        self.rcon.fail = False
        self.engine.poll(now=1000)
        welcomes = self.engine.db.execute("SELECT count(*) FROM jobs WHERE kind='welcome'").fetchone()[0]
        self.assertEqual(welcomes, 4)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 7)

    def test_alert_cooldown_and_message_spacing(self):
        self.rcon.players = [player(24, 2)]
        self.engine.poll(now=100)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 4)
        self.engine.poll(now=130)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 4)
        self.assertTrue(self.engine.send_one(now=100))
        self.assertFalse(self.engine.send_one(now=101))
        self.assertTrue(self.engine.send_one(now=103))
        self.assertEqual(len(self.rcon.sent), 2)
        self.assertEqual(self.rcon.paths, ['/v1/players/76561190000000001/message'] * 2)
        self.engine.poll(now=700)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 8)

    def test_global_round_is_ten_minutes_and_survives_restart(self):
        self.rcon.players = [player(24, 2)]
        self.engine.poll(now=100)
        for n in range(4):
            self.engine.send_one(now=100 + n * 3)
        self.assertTrue(all('/message' in p for p in self.rcon.paths))
        self.engine.poll(now=699)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 0)
        self.engine.poll(now=700)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 6)
        self.engine.close()
        self.engine = app.Engine(self.path, self.rcon, self.config)
        self.engine.poll(now=730)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 6)
        self.engine.poll(now=1300)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 12)

    def test_broadcast_idempotency_and_validation(self):
        first = self.engine.enqueue_broadcast('测试公告', 'request-1')
        second = self.engine.enqueue_broadcast('测试公告', 'request-1')
        self.assertEqual(first, second)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 1)
        with self.assertRaises(ValueError):
            self.engine.enqueue_broadcast('另一条', 'request-1')
        for text in ['', 'x' * 201, 'bad\nmessage']:
            with self.assertRaises(ValueError):
                self.engine.enqueue_broadcast(text)

    def test_failed_send_not_replayed_after_restart(self):
        self.engine.enqueue_broadcast('测试公告')
        self.rcon.fail = True
        self.assertTrue(self.engine.send_one(now=100))
        self.assertEqual(self.engine.snapshot()['queueDepth'], 0)
        self.assertEqual(self.engine.snapshot()['deliveryCounts']['unknown'], 1)
        self.engine.close()
        self.engine = app.Engine(self.path, self.rcon, self.config)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 0)

    def test_rate_limit_is_delayed_then_delivered(self):
        job = self.engine.enqueue_broadcast('测试公告')['jobId']
        original = self.rcon.request
        self.rcon.request = lambda *a, **kw: (_ for _ in ()).throw(app.RconError(429, 20))
        self.engine.send_one(now=100)
        self.assertEqual(self.engine.job(job)['status'], 'queued')
        self.rcon.request = original
        self.assertFalse(self.engine.send_one(now=119))
        self.assertTrue(self.engine.send_one(now=120))
        self.assertEqual(self.engine.job(job)['status'], 'sent')

    def test_queue_overflow_does_not_lose_join_or_alert(self):
        self.engine.config['queue_limit'] = 1
        self.rcon.players = [player(12, 1)]
        with self.assertRaises(ValueError):
            self.engine.poll(now=100)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 0)
        self.engine.config['queue_limit'] = 1000
        self.engine.poll(now=130)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 4)

    def test_invalid_players_response_does_not_clear_presence(self):
        self.rcon.players = [player()]
        self.engine.poll(now=100)
        self.rcon.players = [{}]
        with self.assertRaises(ValueError):
            self.engine.poll(now=130)
        self.rcon.players = [player()]
        self.engine.poll(now=190)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 4)

    def test_spacing_starts_after_slow_request_finishes(self):
        self.engine.enqueue_broadcast('第一条')
        self.engine.enqueue_broadcast('第二条')
        with patch.object(app.time, 'time', side_effect=[100, 110]):
            self.assertTrue(self.engine.send_one())
        self.assertFalse(self.engine.send_one(now=112))
        self.assertTrue(self.engine.send_one(now=113))

    def test_whisper_missing_player_does_not_fallback_to_broadcast(self):
        self.rcon.players = [player()]
        self.engine.poll(now=100)
        self.rcon.request = lambda *a, **kw: (_ for _ in ()).throw(app.RconError(404))
        self.engine.send_one(now=100)
        self.assertEqual(self.engine.snapshot()['deliveryCounts']['failed'], 1)
        self.assertEqual(self.rcon.sent, [])

    def test_legacy_queued_broadcasts_are_cancelled(self):
        import sqlite3
        legacy = Path(self.temp.name) / 'legacy.db'
        connection = sqlite3.connect(legacy)
        connection.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,message TEXT,status TEXT,created REAL,updated REAL)')
        connection.execute("INSERT INTO jobs VALUES('old','旧欢迎公告','queued',1,1)")
        connection.commit()
        connection.close()
        migrated = app.Engine(legacy, self.rcon, self.config)
        try:
            self.assertEqual(migrated.snapshot()['queueDepth'], 0)
            self.assertEqual(migrated.job('old')['status'], 'cancelled')
        finally:
            migrated.close()

    def test_noob_high_kd_kick_then_management_announcement(self):
        self.engine.config['high_kd_kick'] = True
        self.rcon.players = [player(12, 0)]
        self.engine.poll(now=100)
        self.assertTrue(self.engine.send_one(now=100))
        self.assertEqual(self.rcon.kicked[0][0], '/v1/players/76561190000000001/kick')
        self.assertIn('社区服务器', self.rcon.kicked[0][1])
        self.assertEqual(self.rcon.sent, [])
        self.engine.send_one(now=103)
        self.assertEqual(self.rcon.paths[-1], '/v1/broadcast')
        self.assertIn('高KD不等于作弊', self.rcon.sent[-1])
        self.assertIn('已踢出', self.rcon.sent[-1])
        self.engine.poll(now=130)
        self.assertEqual(len(self.rcon.kicked), 1)
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM jobs WHERE kind='kd_kick'").fetchone()[0], 1)

    def test_kick_rechecks_current_kd_before_acting(self):
        self.engine.config['high_kd_kick'] = True
        self.rcon.players = [player(12, 0)]
        self.engine.poll(now=100)
        self.rcon.players = [player(12, 2)]
        self.engine.send_one(now=100)
        self.assertEqual(self.rcon.kicked, [])
        self.assertEqual(self.engine.db.execute("SELECT status FROM jobs WHERE kind='kd_kick'").fetchone()[0], 'cancelled')
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM jobs WHERE kind='management'").fetchone()[0], 0)

    def test_failed_kick_does_not_announce_success(self):
        self.engine.config['high_kd_kick'] = True
        self.rcon.players = [player(12, 0)]
        self.engine.poll(now=100)
        original = self.rcon.request
        def fail_kick(method, path, body=None):
            if path.endswith('/kick'):
                raise app.RconError(404)
            return original(method, path, body)
        self.rcon.request = fail_kick
        self.engine.send_one(now=100)
        self.assertEqual(self.engine.db.execute("SELECT status FROM jobs WHERE kind='kd_kick'").fetchone()[0], 'failed')
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM jobs WHERE kind='management'").fetchone()[0], 0)

    def test_kick_task_survives_restart_without_duplicate_or_unverified_action(self):
        self.engine.config['high_kd_kick'] = True
        config = self.engine.config
        self.rcon.players = [player(12, 0)]
        self.engine.poll(now=100)
        self.engine.close()
        self.engine = app.Engine(self.path, self.rcon, config)
        self.engine.poll(now=130)
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM jobs WHERE kind='kd_kick'").fetchone()[0], 1)
        self.rcon.players = []
        self.engine.send_one(now=130)
        self.assertEqual(self.rcon.kicked, [])

    def test_kick_timeout_is_not_replayed_or_announced(self):
        self.engine.config['high_kd_kick'] = True
        config = self.engine.config
        self.rcon.players = [player(12, 0)]
        self.engine.poll(now=100)
        original = self.rcon.request
        def timeout_kick(method, path, body=None):
            if path.endswith('/kick'):
                raise TimeoutError('timeout')
            return original(method, path, body)
        self.rcon.request = timeout_kick
        self.engine.send_one(now=100)
        self.assertEqual(self.engine.db.execute("SELECT status FROM jobs WHERE kind='kd_kick'").fetchone()[0], 'unknown')
        self.engine.close()
        self.engine = app.Engine(self.path, self.rcon, config)
        self.engine.poll(now=130)
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM jobs WHERE kind='kd_kick'").fetchone()[0], 1)
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM jobs WHERE kind='management'").fetchone()[0], 0)

    def test_separate_notice_and_kick_thresholds(self):
        self.engine.config.update(high_kd_kick=True, kd_threshold=4, kick_kd_threshold=6, kick_min_kills=7)
        for kills, deaths, expected in [(4, 0, False), (5, 0, False), (6, 0, False),
                                         (7, 0, True), (7, 1, True), (12, 2, False),
                                         (13, 2, True), (7, 2, False)]:
            self.assertEqual(app.kick_eligible(player(kills, deaths), self.engine.config), expected)
        self.rcon.players = [player(5, 0)]
        self.engine.poll(now=100)
        self.engine.poll(now=700)
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM jobs WHERE kind='kd'").fetchone()[0], 3)
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM jobs WHERE kind='kd_kick'").fetchone()[0], 0)

    def test_kick_minimum_kills_rechecked_at_execution(self):
        self.engine.config.update(high_kd_kick=True, kd_threshold=4, kick_kd_threshold=6, kick_min_kills=7)
        self.rcon.players = [player(7, 0)]
        self.engine.poll(now=100)
        self.rcon.players = [player(6, 0)]
        self.engine.send_one(now=100)
        self.assertEqual(self.rcon.kicked, [])
        self.assertEqual(self.engine.db.execute("SELECT status FROM jobs WHERE kind='kd_kick'").fetchone()[0], 'cancelled')

    def test_language_batch_is_atomic_and_idempotent(self):
        self.engine.config['queue_limit'] = 2
        with self.assertRaises(ValueError):
            self.engine.enqueue_broadcast('公告', 'three', english='Notice', japanese='お知らせ')
        self.assertEqual(self.engine.snapshot()['queueDepth'], 0)
        self.engine.config['queue_limit'] = 1000
        first = self.engine.enqueue_broadcast('公告', 'three', english='Notice', japanese='お知らせ')
        second = self.engine.enqueue_broadcast('公告', 'three', english='Notice', japanese='お知らせ')
        self.assertEqual(first, second)
        self.assertEqual(len(first['jobIds']), 3)
        self.assertEqual(self.engine.snapshot()['queueDepth'], 3)
        with self.assertRaises(ValueError):
            self.engine.enqueue_broadcast('公告', 'three', english='Different', japanese='お知らせ')



if __name__ == '__main__':
    unittest.main()
