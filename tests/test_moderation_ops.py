import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_monitor as fixtures
from service.moderation_ops import ContractError

app = fixtures.app
IDENTITY = '76561190000000001'


def report(now=1000, report_id='r-1', identity=IDENTITY):
    return {
        'reportId': report_id, 'steamId': identity, 'ruleId': 'outside_combat',
        'ruleVersion': '1', 'evidenceVersion': '1', 'evidenceRefs': ['qq-message:example'],
        'decision': {'verified': True, 'identityScore': 0.99, 'violationScore': 0.99, 'observedAt': now},
        'reason': {'zh': '黄圈外杀人', 'en': 'killing outside the hot zone', 'ja': 'ホットゾーン外でのキル'},
    }


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.db'
        self.rcon = fixtures.FakeRcon()
        self.rcon.players = [fixtures.player()]
        self.config = copy.deepcopy(app.DEFAULT_CONFIG)
        self.engine = app.Engine(self.path, self.rcon, self.config)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def submit(self, payload=None, key='business-1', now=1000):
        return self.engine.moderation.submit(payload or report(), key, now=now)

    def reopen(self):
        self.engine.close()
        self.engine = app.Engine(self.path, self.rcon, self.config)

    def test_acceptance_has_no_rcon_side_effect_and_retries_are_idempotent(self):
        first = self.submit()
        self.assertEqual(first['status'], 'accepted')
        self.assertEqual(first['kickStatus'], 'not_started')
        self.assertEqual(self.rcon.kicked, [])
        self.assertEqual(self.rcon.paths, [])
        self.assertEqual(first['operationId'], self.submit(now=2000)['operationId'])
        self.assertTrue(self.submit()['deduplicated'])
        self.reopen()
        self.assertEqual(first['operationId'], self.submit(now=2000)['operationId'])

    def test_natural_report_key_and_alias_key_are_bound(self):
        first = self.submit()
        alias = self.submit(key='alias')
        self.assertEqual(first['operationId'], alias['operationId'])
        different = report(report_id='r-other')
        with self.assertRaises(ContractError) as caught:
            self.submit(different, key='alias')
        self.assertEqual(caught.exception.status, 409)

    def test_changed_payload_same_idempotency_key_conflicts(self):
        self.submit()
        changed = report()
        changed['reason']['en'] = 'different reason'
        with self.assertRaises(ContractError) as caught:
            self.submit(changed)
        self.assertEqual(caught.exception.code, 'idempotency_conflict')

    def test_rule_allowlist_and_version_gate(self):
        for rule_id, version, code in [('base_camping', '1', 'rule_not_enabled'),
                                       ('arbitrary', '1', 'rule_not_enabled'),
                                       ('outside_combat', '2', 'rule_version_mismatch')]:
            payload = report()
            payload.update(ruleId=rule_id, ruleVersion=version)
            with self.assertRaises(ContractError) as caught:
                self.submit(payload)
            self.assertEqual(caught.exception.code, code)
        self.engine.config['moderation_rules']['base_camping'] = 'reviewed-v1'
        payload = report()
        payload.update(ruleId='base_camping', ruleVersion='reviewed-v1')
        self.assertEqual(self.submit(payload)['status'], 'accepted')

    def test_low_scores_unverified_and_stale_decisions_do_not_execute(self):
        for field, value in [('verified', False), ('identityScore', .98), ('violationScore', .94),
                             ('observedAt', 600), ('observedAt', 1031)]:
            payload = report()
            payload['decision'][field] = value
            with self.assertRaises(ContractError) as caught:
                self.submit(payload)
            self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.rcon.kicked, [])
        payload = report()
        payload['decision'].update(identityScore=.99, violationScore=.95)
        self.assertEqual(self.submit(payload)['status'], 'accepted')

    def test_malformed_fields_rejected_without_sql_or_network_errors(self):
        for field, value in [('steamId', 'short'), ('steamId', 76561190000000001), ('ruleId', []),
                             ('reportId', []), ('evidenceRefs', []), ('evidenceRefs', ['x' * 513]),
                             ('reason', {'zh': '原因'}), ('decision', {})]:
            payload = report()
            payload[field] = value
            with self.assertRaises(ContractError):
                self.submit(payload)
        with self.assertRaises(ContractError):
            self.submit(key=None)
        payload = report()
        payload['decision']['identityScore'] = float('nan')
        with self.assertRaises(ContractError):
            self.submit(payload)
        payload['decision']['identityScore'] = 10 ** 1000
        with self.assertRaises(ContractError):
            self.submit(payload)

    def test_fresh_lookup_committed_dispatch_and_linked_notices(self):
        op = self.submit()
        original = self.rcon.request
        def check_dispatch(method, path, body=None):
            if path.endswith('/kick'):
                other = sqlite3.connect(self.path)
                self.assertEqual(other.execute('SELECT status FROM moderation_operations WHERE id=?', (op['operationId'],)).fetchone()[0], 'kick_dispatching')
                other.close()
            return original(method, path, body)
        self.rcon.request = check_dispatch
        result = self.engine.moderation.process(op['operationId'], now=1001)
        self.assertEqual(result['kickStatus'], 'confirmed')
        self.assertEqual(result['announcementStatus'], 'pending')
        self.assertEqual(len(result['announcementJobIds']), 3)
        self.assertEqual(self.rcon.sent, [])
        self.assertEqual(len(self.rcon.kicked), 1)
        for t in [1002, 1005, 1008]:
            self.engine.send_one(now=t)
        done = self.engine.moderation.get(op['operationId'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual([j['language'] for j in done['announcementJobs']], ['zh', 'en', 'ja'])
        self.reopen()
        self.assertFalse(self.engine.moderation.process_one(now=1010))
        self.assertEqual(len(self.rcon.kicked), 1)

    def test_offline_and_duplicate_live_id_never_kick(self):
        op = self.submit()
        self.rcon.players = []
        self.assertEqual(self.engine.moderation.process(op['operationId'], now=1001)['status'], 'offline')
        self.assertEqual(self.rcon.kicked, [])
        other = self.submit(report(report_id='r-2'), key='two')
        self.rcon.players = [fixtures.player(), fixtures.player()]
        self.assertEqual(self.engine.moderation.process(other['operationId'], now=1002)['status'], 'failed')
        self.assertEqual(self.rcon.kicked, [])

    def test_decision_expiration_while_waiting_does_not_kick(self):
        op = self.submit()
        result = self.engine.moderation.process(op['operationId'], now=1400)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['error']['code'], 'decision_stale_or_future')
        self.assertEqual(self.rcon.kicked, [])

    def test_expiration_during_lookup_is_checked_before_dispatch(self):
        op = self.submit()
        with patch('service.moderation_ops.time.time', side_effect=[1001, 1002, 1400, 1401]):
            result = self.engine.moderation.process(op['operationId'])
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.rcon.kicked, [])

    def test_restart_dispatch_is_unknown_and_never_replayed(self):
        op = self.submit()
        self.engine.moderation._claim(op['operationId'], 1001)
        with self.engine.db:
            self.engine.moderation._set(op['operationId'], 'kick_dispatching', 'dispatching', 1001)
        self.reopen()
        self.assertEqual(self.engine.moderation.get(op['operationId'])['kickStatus'], 'unknown')
        self.assertFalse(self.engine.moderation.process_one(now=1002))
        another = self.submit(report(report_id='r-2'), key='two')
        result = self.engine.moderation.process(another['operationId'], now=1003)
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(result['relatedOperationId'], op['operationId'])
        self.assertEqual(self.rcon.kicked, [])

    def test_restart_before_dispatch_can_resolve_once(self):
        op = self.submit()
        self.engine.moderation._claim(op['operationId'], 1001)
        self.reopen()
        self.assertEqual(self.engine.moderation.process(op['operationId'], now=1002)['kickStatus'], 'confirmed')
        self.assertEqual(len(self.rcon.kicked), 1)

    def test_kick_timeout_or_pending_has_no_success_notice(self):
        original = self.rcon.request
        for index, pending in enumerate([False, True]):
            payload = report(report_id='r-' + str(index), identity='7656119000000000' + str(index))
            self.rcon.players = [fixtures.player(identity=payload['steamId'])]
            op = self.submit(payload, key='key-' + str(index))
            def uncertain(method, path, body=None):
                if path.endswith('/kick'):
                    if pending:
                        return {'ok': True, 'pending': True}
                    raise TimeoutError()
                return original(method, path, body)
            self.rcon.request = uncertain
            result = self.engine.moderation.process(op['operationId'], now=1001)
            self.assertEqual(result['kickStatus'], 'unknown')
            self.assertEqual(result['announcementJobIds'], [])

    def test_partial_notice_failure_never_repeats_kick(self):
        op = self.submit()
        self.engine.moderation.process(op['operationId'], now=1001)
        self.engine.send_one(now=1002)
        self.rcon.fail = True
        self.engine.send_one(now=1005)
        self.rcon.fail = False
        self.engine.send_one(now=1008)
        result = self.engine.moderation.get(op['operationId'])
        self.assertEqual(result['status'], 'announcement_unknown')
        self.assertEqual(result['kickStatus'], 'confirmed')
        self.engine.moderation.process(op['operationId'], now=1010)
        self.assertEqual(len(self.rcon.kicked), 1)

    def test_kd_and_report_share_target_claim(self):
        op = self.submit()
        self.engine.moderation.process(op['operationId'], now=1001)
        self.rcon.players = [fixtures.player(12, 0)]
        self.engine.config['high_kd_kick'] = True
        self.engine.poll(now=1002)
        # Drain linked notices, then the auto KD task reaches the same coordinator.
        for t in [1002, 1005, 1008, 1011]:
            self.engine.send_one(now=t)
        self.assertEqual(len(self.rcon.kicked), 1)
        auto = self.engine.db.execute("SELECT status FROM moderation_operations WHERE source='auto_kd'").fetchone()
        self.assertEqual(auto[0], 'superseded')

    def test_concurrent_workers_have_one_target_owner(self):
        first = self.submit()
        second = self.submit(report(report_id='r-2'), key='two')
        other = app.Engine(self.path, self.rcon, self.config)
        started = threading.Event()
        release = threading.Event()
        original = self.rcon.request
        def blocked(method, path, body=None):
            if path == '/v1/players':
                started.set()
                if not release.wait(3):
                    raise TimeoutError()
            return original(method, path, body)
        self.rcon.request = blocked
        thread = threading.Thread(target=self.engine.moderation.process, args=(first['operationId'],), kwargs={'now': 1001})
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            result = other.moderation.process(second['operationId'], now=1001)
            self.assertEqual(result['status'], 'superseded')
        finally:
            release.set()
            thread.join(3)
            other.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.rcon.kicked), 1)

    def test_restart_recovery_fences_a_still_running_resolver(self):
        op = self.submit()
        started, release = threading.Event(), threading.Event()
        original = self.rcon.request
        def blocked(method, path, body=None):
            if path == '/v1/players' and threading.current_thread().name == 'old-resolver':
                started.set()
                if not release.wait(3):
                    raise TimeoutError()
            return original(method, path, body)
        self.rcon.request = blocked
        thread = threading.Thread(name='old-resolver', target=self.engine.moderation.process,
                                  args=(op['operationId'],), kwargs={'now': 1001})
        thread.start()
        other = None
        try:
            self.assertTrue(started.wait(2))
            other = app.Engine(self.path, self.rcon, self.config)
            self.assertEqual(other.moderation.process(op['operationId'], now=1002)['kickStatus'], 'confirmed')
        finally:
            release.set()
            thread.join(3)
            if other:
                other.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.rcon.kicked), 1)

    def test_confirmed_kick_and_outbox_failure_is_not_replayed(self):
        op = self.submit()
        original = self.engine.moderation._notices
        def broken_outbox(*args):
            original(*args)
            raise sqlite3.OperationalError('simulated failed transaction')
        self.engine.moderation._notices = broken_outbox
        result = self.engine.moderation.process(op['operationId'], now=1001)
        self.assertEqual(result['kickStatus'], 'unknown')
        self.assertEqual(result['announcementJobIds'], [])
        self.assertEqual(self.engine.snapshot()['queueDepth'], 0)
        self.reopen()
        self.assertFalse(self.engine.moderation.process_one(now=1002))
        self.assertEqual(len(self.rcon.kicked), 1)

    def test_notice_history_is_preserved_past_normal_queue_retention(self):
        op = self.submit()
        self.engine.moderation.process(op['operationId'], now=1001)
        for t in [1002, 1005, 1008]:
            self.engine.send_one(now=t)
        self.engine.poll(now=1001 + 8 * 86400)
        self.assertEqual(self.engine.moderation.get(op['operationId'])['status'], 'completed')

    def test_known_rejection_vs_ambiguous_server_failure(self):
        original = self.rcon.request
        for index, code in enumerate([401, 404, 429, 500]):
            identity = '7656119000000001' + str(index)
            self.rcon.players = [fixtures.player(identity=identity)]
            op = self.submit(report(report_id='http-' + str(code), identity=identity), key='http-' + str(code))
            def refused(method, path, body=None):
                if path.endswith('/kick'):
                    raise app.RconError(code)
                return original(method, path, body)
            self.rcon.request = refused
            result = self.engine.moderation.process(op['operationId'], now=1001)
            self.assertEqual(result['kickStatus'], 'unknown' if code == 500 else 'failed')
            self.assertEqual(result['announcementJobIds'], [])

    def test_empty_success_body_does_not_confirm_kick(self):
        op = self.submit()
        original = self.rcon.request
        def empty_ack(method, path, body=None):
            return {} if path.endswith('/kick') else original(method, path, body)
        self.rcon.request = empty_ack
        self.assertEqual(self.engine.moderation.process(op['operationId'], now=1001)['kickStatus'], 'unknown')

    def test_pending_limit_preserves_original_operation(self):
        self.engine.config['moderation_pending_limit'] = 1
        first = self.submit()
        with self.assertRaises(ContractError) as caught:
            self.submit(report(report_id='two'), key='two')
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.submit()['operationId'], first['operationId'])
        self.assertEqual(self.rcon.kicked, [])
