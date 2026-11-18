"""WARDOGS polling, persistent announcements and authenticated agent API."""

import copy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
import re
from pathlib import Path
import signal
import sqlite3
import threading
import time
import uuid
from urllib.parse import quote

try:
    from service.announcements import EN_RULES, JA_RULES, kd_messages, kick_messages
    from service.rcon import Rcon, RconError, reserved_ids
    from service.unknown_handoff import UnknownHandoff
    from service.moderation_ops import Coordinator, ContractError, validate_admin_config
    from service.match_query import MatchQuery, MatchUnavailable
except ImportError:
    from announcements import EN_RULES, JA_RULES, kd_messages, kick_messages
    from rcon import Rcon, RconError, reserved_ids
    from unknown_handoff import UnknownHandoff
    from moderation_ops import Coordinator, ContractError, validate_admin_config
    from match_query import MatchQuery, MatchUnavailable


try:
    from service.kill_accounting import KillAccounting
except ImportError:
    from kill_accounting import KillAccounting


try:
    from service.steam_profiles import SteamProfiles
except ImportError:
    from steam_profiles import SteamProfiles


try:
    from service.punishment_history import PunishmentHistory
    from service.current_restrictions import CurrentRestrictions
except ImportError:
    from punishment_history import PunishmentHistory
    from current_restrictions import CurrentRestrictions


try:
    from service.player_chats import PlayerChats
    from service.crash_advisory import CrashAdvisory
except ImportError:
    from player_chats import PlayerChats
    from crash_advisory import CrashAdvisory


try:
    from service.announcement_policy import AnnouncementPolicy
except ImportError:
    from announcement_policy import AnnouncementPolicy

DEFAULT_CONFIG = {
    'poll_seconds': 30,
    'japanese_announcements_enabled': True,
    'signature_fun_enabled': False,
    'exclude_barbed_wire_kills': False,
    'kd_threshold': 4,
    'kick_kd_threshold': 6,
    'kick_min_kills': 7,
    'high_kd_kick': False,
    'alert_cooldown_seconds': 600,
    'global_announcement_seconds': 600,
    'message_interval_seconds': 3,
    'rejoin_grace_seconds': 60,
    'queue_limit': 1000,
    'moderation_rules': {'outside_combat': '1', 'cheating': '1'},
    'moderation_identity_threshold': 0.99,
    'moderation_violation_threshold': 0.95,
    'moderation_decision_max_age_seconds': 300,
    'moderation_future_skew_seconds': 30,
    'moderation_target_cooldown_seconds': 60,
    'moderation_pending_limit': 100,
    'rules': [
        '欢迎 {name} 加入服务器！',
        '本服为社区服务器：黄圈外不打架，黄圈内不限制。',
        '高KD不等于作弊。KD大于4将通告；KD大于6且至少7杀将踢出。压家或其他违规，请携完整证据截图到管理员举报。所有踢出均永久限制重入。',
        '感谢配合，祝各位游戏愉快！',
    ],
    'global_rules': [
        '本服为社区服务器：黄圈外不打架，黄圈内不限制。',
        '高KD不等于作弊。KD大于4将通告；KD大于6且至少7杀将踢出。压家或其他违规，请携完整证据截图到管理员举报。所有踢出均永久限制重入。',
        '感谢配合，祝各位游戏愉快！',
    ] + EN_RULES + JA_RULES,
}
try:
    from service.network_announcements import DEFAULTS as NETWORK_DEFAULTS, schedule as schedule_network, validate as validate_network, schedule_community
except ImportError:
    from network_announcements import DEFAULTS as NETWORK_DEFAULTS, schedule as schedule_network, validate as validate_network, schedule_community
DEFAULT_CONFIG.update(NETWORK_DEFAULTS)
DEFAULT_CONFIG['automatic_announcements_enabled'] = True
try:
    from service.language_spacing import language as announcement_language, ready as language_ready, record as record_language
except ImportError:
    from language_spacing import language as announcement_language, ready as language_ready, record as record_language
DEFAULT_CONFIG['language_interval_seconds'] = 10
try:
    from service.kick_guard import KickStore, TrackingRcon, check_rejoins
except ImportError:
    from kick_guard import KickStore, TrackingRcon, check_rejoins
DEFAULT_CONFIG['rejoin_guard_enabled'] = False
try:
    from service.kill_cap import eligible as kill_cap_eligible, schedule as schedule_kill_cap
except ImportError:
    from kill_cap import eligible as kill_cap_eligible, schedule as schedule_kill_cap
DEFAULT_CONFIG['high_kill_kick'] = False
DEFAULT_CONFIG['reserved_exemptions_enabled'] = False
DEFAULT_CONFIG.update(admin_moderation_scopes=[], admin_moderation_max_age_seconds=120,
                      admin_moderation_future_skew_seconds=30)
try:
    from service.behavior_policy import BehaviorPolicy
except ImportError:
    from behavior_policy import BehaviorPolicy
try:
    from service.player_history import PlayerHistory,HistoryUnavailable
    from service.admin_history import AdminHistory
    from service.admin_cycle import AdminCycle
    from service.admin_unban import AdminUnban
    from service.admin_tools import AdminTools
except ImportError:
    from player_history import PlayerHistory,HistoryUnavailable
    from admin_history import AdminHistory
    from admin_cycle import AdminCycle
    from admin_unban import AdminUnban
    from admin_tools import AdminTools
try:
    from service.burst_guard import BurstGuard
except ImportError:
    from burst_guard import BurstGuard
DEFAULT_CONFIG['burst_ban_enabled'] = False
DEFAULT_CONFIG['auto_native_ban_enabled'] = False
try:
    from service.helicopter_fun import HelicopterFun
except ImportError:
    from helicopter_fun import HelicopterFun
DEFAULT_CONFIG['helicopter_fun_enabled'] = False
try:
    from service.admin_bans import AdminBans
    from service.player_sanction_summary import PlayerSanctionSummary
    from service.player_kills import PlayerKills
    from service.recent_kills import RecentKills
except ImportError:
    from admin_bans import AdminBans
    from player_sanction_summary import PlayerSanctionSummary
    from player_kills import PlayerKills
    from recent_kills import RecentKills

try:
    from service.ban_statistics import BanStatistics,StatisticsUnavailable
except ImportError:
    from ban_statistics import BanStatistics,StatisticsUnavailable
try:
    from service.welcome_delivery import WelcomeDelivery,pack as pack_welcome
except ImportError:
    from welcome_delivery import WelcomeDelivery,pack as pack_welcome
DEFAULT_CONFIG['welcome_delivery_enabled'] = False
LOG = logging.getLogger('wardogs')


def clean_name(value):
    return ''.join(c for c in str(value) if c.isprintable())[:40] or '新玩家'


def statistics(player):
    try:
        kills, deaths = float(player['kills']), float(player['deaths'])
        if not all(math.isfinite(x) and x >= 0 and x.is_integer() for x in (kills, deaths)):
            return None
        return int(kills), int(deaths), kills / max(deaths, 1)
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def high_kd(player, threshold):
    values = statistics(player)
    return values is not None and values[2] > threshold


def kick_eligible(player, config):
    values = statistics(player)
    return values is not None and values[0] >= config['kick_min_kills'] and values[2] > config['kick_kd_threshold']


def load_admin_scopes(config, path):
    """Load only the private scopes, never unrelated policy or credentials."""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or set(data) != {'admin_moderation_scopes'}:
        raise ValueError('Invalid private admin scope document')
    updated = copy.deepcopy(config)
    updated['admin_moderation_scopes'] = data['admin_moderation_scopes']
    validate_admin_config(updated)
    config['admin_moderation_scopes'] = updated['admin_moderation_scopes']




class Engine:
    def __init__(self, db_path, rcon, config):
        self.crash_advisory = CrashAdvisory(self,db_path)
        self.identity_path = Path(db_path).with_name('server-identity.json')
        self.config = copy.deepcopy(config)
        self.rcon = rcon
        self.match_query = MatchQuery(rcon)
        self.ban_statistics = BanStatistics(self)
        self.kick_store = KickStore(Path(db_path).with_name('kick_guard.sqlite3')) if config.get('rejoin_guard_enabled', False) or config.get('high_kill_kick', False) or config.get('reserved_exemptions_enabled', False) else None
        if self.kick_store is not None:
            self.kick_store.recover()
            self.rcon = TrackingRcon(rcon, self.kick_store)
        self.behavior = BehaviorPolicy(self.kick_store, self.rcon) if self.kick_store is not None else None
        if self.behavior is not None:
            self.behavior.recover()
        self.lock = threading.RLock()
        self.poll_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, message TEXT NOT NULL, status TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS requests(
            token TEXT PRIMARY KEY, message TEXT NOT NULL, job_id TEXT NOT NULL, created REAL NOT NULL);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        if 'recipient' not in columns:
            # Old welcome jobs have no player ID and must never be replayed globally.
            self.db.execute("UPDATE jobs SET status='cancelled' WHERE status='queued'")
            self.db.execute('ALTER TABLE jobs ADD COLUMN recipient TEXT')
            self.db.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual'")
        self.announcement_policy = AnnouncementPolicy(self)
        row = self.db.execute('SELECT body FROM state WHERE id=1').fetchone()
        self.state = json.loads(row[0]) if row else {'presence': {}, 'alerts': {}, 'lastSend': 0}
        # A crash after claiming a message leaves delivery uncertain; never replay it blindly.
        self.db.execute("UPDATE jobs SET status='unknown' WHERE status='sending'")
        self.db.commit()
        self.kill_accounting = KillAccounting(Path(db_path).parent / 'local-cache' / 'events.sqlite3')
        self.players = []
        self.last_poll = None
        self.last_error = None
        self.next_send = self.state.get('lastSend', 0) + self.config['message_interval_seconds']
        if self.config.get('welcome_delivery_enabled'):
            self.next_send=max(self.next_send,self.state.get('welcomeRconBackoffUntil',0))
        self.player_history = PlayerHistory(self,Path(db_path).parent / 'local-cache' / 'events.sqlite3')
        self.moderation = Coordinator(self)
        self.unknown_handoff = UnknownHandoff(self)
        self.admin_history = AdminHistory(self)
        self.admin_cycle = AdminCycle(self)
        self.admin_unban = AdminUnban(self)
        self.admin_bans = AdminBans(self)
        self.admin_tools = AdminTools(self)
        self.burst_guard = BurstGuard(self,Path(db_path).parent / 'local-cache' / 'events.sqlite3')
        self.helicopter_fun = HelicopterFun(self,Path(db_path).parent / 'local-cache' / 'events.sqlite3')
        self.welcome_delivery = WelcomeDelivery(self,Path(db_path).parent / 'local-cache' / 'events.sqlite3')
        self.player_sanction_summary = PlayerSanctionSummary(self,db_path)
        self.player_chats = PlayerChats(self,Path(db_path).parent / 'local-cache' / 'events.sqlite3')
        self.player_kills = PlayerKills(self,Path(db_path).parent / 'local-cache' / 'events.sqlite3')
        self.recent_kills = RecentKills(self,Path(db_path).parent / 'local-cache' / 'events.sqlite3')
        self.steam_profiles = SteamProfiles(self,Path(db_path).parent / 'steam-profiles.sqlite3')
        self.punishment_history = PunishmentHistory(self,db_path)
        self.current_restrictions = CurrentRestrictions(self,db_path)

    def close(self):
        with self.lock:
            self.db.close()
            if self.kick_store is not None:
                self.kick_store.close()

    def save_state(self):
        self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)', (json.dumps(self.state),))

    def queue_depth(self):
        return self.db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]

    def add_job(self, message, now, recipient=None, kind='manual', language=None):
        if not self.announcement_policy.muted(message, kind, language) and kind not in ('management', 'kill_cap_kick') and self.queue_depth() >= self.config['queue_limit']:
            raise ValueError('announcement queue full')
        job_id = uuid.uuid4().hex
        self.db.execute('INSERT INTO jobs(id,message,status,created,updated,recipient,kind) VALUES(?,?,?,?,?,?,?)',
                        (job_id, message, 'queued', now, now, recipient, kind))
        self.announcement_policy.register(job_id, language)
        self.announcement_policy.suppress(job_id, now)
        return job_id

    def enqueue_broadcast(self, message, token=None, english=None, japanese=None):
        if not isinstance(message, str) or not message.strip() or len(message) > 200:
            raise ValueError('message must contain 1 to 200 characters')
        if any(not c.isprintable() for c in message):
            raise ValueError('message must be a single printable line')
        messages = [message]
        if english is not None or japanese is not None:
            for translated in [english, japanese]:
                if not isinstance(translated, str) or not translated.strip() or len(translated) > 200 or any(not c.isprintable() for c in translated):
                    raise ValueError('english and japanese must be printable lines of 1 to 200 characters')
            messages.extend([english, japanese])
        signature = message if len(messages) == 1 else json.dumps(messages, ensure_ascii=False)
        if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 128):
            raise ValueError('Idempotency-Key must contain 1 to 128 characters')
        with self.lock, self.db:
            if token:
                old = self.db.execute('SELECT message,job_id FROM requests WHERE token=?', (token,)).fetchone()
                if old:
                    if old[0] != signature:
                        raise ValueError('Idempotency-Key already used with a different message')
                    ids = json.loads(old[1]) if old[1].startswith('[') else [old[1]]
                    return {'jobId': ids[0], 'jobIds': ids, 'accepted': True}
            now = time.time()
            ids = [self.add_job(text, now, language=('zh','en','ja')[i] if len(messages)==3 else None) for i,text in enumerate(messages)]
            if token:
                stored_ids = ids[0] if len(ids) == 1 else json.dumps(ids)
                self.db.execute('INSERT INTO requests VALUES(?,?,?,?)', (token, signature, stored_ids, now))
            return {'jobId': ids[0], 'jobIds': ids, 'accepted': True}

    def poll(self, now=None):
        with self.poll_lock:
            try:
                identities = None
                if self.config.get('reserved_exemptions_enabled'):
                    identities = reserved_ids(self.rcon.request('GET', '/v1/reserved-slots'))
                data = self.rcon.request('GET', '/v1/players')
                players = data.get('players')
                if not isinstance(players, list) or any(not isinstance(p, dict) or not p.get('steamId') for p in players):
                    raise ValueError('invalid players response')
                with self.lock:
                    if identities is not None:
                        self.kick_store.sync_reserved(identities, time.time() if now is None else now)
                    now = time.time() if now is None else now
                    before = copy.deepcopy(self.state)
                    try:
                        with self.db:
                            presence = self.state['presence']
                            if 'lastGlobalRound' not in self.state:
                                self.state['lastGlobalRound'] = now
                            automatic_notices = self.config['automatic_announcements_enabled']
                            global_due = automatic_notices and bool(players) and now - self.state['lastGlobalRound'] >= self.config['global_announcement_seconds']
                            if global_due:
                                for rule in self.config['global_rules']:
                                    self.add_job(rule, now, kind='periodic')
                                self.state['lastGlobalRound'] = now
                            if automatic_notices:
                                schedule_network(self, now, players, global_due)
                                schedule_community(self, now, players)
                                self.crash_advisory.schedule(now,players)
                                self.announcement_policy.schedule(now,players)
                            present = {str(p['steamId']) for p in players}
                            for identity, record in presence.items():
                                if identity not in present and record.get('absentSince') is None:
                                    record['absentSince'] = now
                            counts = self.kill_accounting.counts(now) if self.config.get('exclude_barbed_wire_kills') else None
                            for player in players:
                                if self.config.get('exclude_barbed_wire_kills'):
                                    player = self.kill_accounting.adjust(player, counts)
                                identity = str(player['steamId'])
                                record = presence.get(identity)
                                joined = record is None or (record.get('absentSince') is not None and
                                    now - record['absentSince'] >= self.config['rejoin_grace_seconds'])
                                if joined and automatic_notices:
                                    welcome_parts=[rule.replace('{name}', clean_name(player.get('name', ''))) for rule in self.config['rules']]
                                    if self.config.get('welcome_delivery_enabled'):welcome_parts=pack_welcome(welcome_parts)
                                    for rule in welcome_parts:
                                        self.add_job(rule,now,recipient=identity,kind='welcome')
                                presence[identity] = {'lastSeen': now, 'absentSince': None}
                                exempt = self.kick_store is not None and self.kick_store.kick_exempt(identity)
                                kill_cap_due = exempt or schedule_kill_cap(self, player, now)
                                if self.config['high_kd_kick'] and not kill_cap_due:
                                    kick_alerts = self.state.setdefault('kickAlerts', {})
                                    last_kick = kick_alerts.get(identity)
                                    if kick_eligible(player, self.config) and (last_kick is None or
                                            now - last_kick >= self.config['alert_cooldown_seconds']):
                                        self.add_job('本服为社区服务器，高KD不等于作弊，但不符合本服KD要求，予以踢出。',
                                                     now, recipient=identity, kind='kd_kick')
                                        kick_alerts[identity] = now
                                last_alert = self.state['alerts'].get(identity)
                                if global_due and not kill_cap_due and high_kd(player, self.config['kd_threshold']) and not (
                                        self.config['high_kd_kick'] and kick_eligible(player, self.config)) and (last_alert is None or
                                        now - last_alert >= self.config['alert_cooldown_seconds']):
                                    kills, deaths, ratio = statistics(player)
                                    name = clean_name(player.get('name', ''))
                                    for message in kd_messages(name, kills, deaths, ratio):
                                        self.add_job(message, now, kind='kd')
                                    self.state['alerts'][identity] = now
                            self.player_history.observe_roster(players,now)
                            cutoff = now - 7 * 86400
                            self.state['presence'] = {k: v for k, v in presence.items() if v['lastSeen'] >= cutoff}
                            self.state['alerts'] = {k: v for k, v in self.state['alerts'].items() if v >= cutoff}
                            if 'kickAlerts' in self.state:
                                self.state['kickAlerts'] = {k: v for k, v in self.state['kickAlerts'].items() if v >= cutoff}
                            self.db.execute("""DELETE FROM jobs WHERE status!='queued' AND updated<?
                                AND NOT EXISTS(SELECT 1 FROM moderation_operation_jobs m WHERE m.job_id=jobs.id)""", (cutoff,))
                            self.db.execute('DELETE FROM requests WHERE created<?', (cutoff,))
                            self.save_state()
                    except Exception:
                        self.state = before
                        raise
                    if self.behavior is not None:
                        self.behavior.observe(players, now)
                    self.players = players
                    self.last_poll = now
                    self.last_error = None
                    LOG.info('poll_ok players=%s queue=%s', len(players), self.queue_depth())
                    check_rejoins(self, players, now)
            except Exception as error:
                self.last_error = type(error).__name__
                LOG.warning('poll_failed error_type=%s', self.last_error)
                raise

    def send_one(self, now=None):
        with self.send_lock:
            with self.lock:
                fixed_time = now is not None
                now = time.time() if now is None else now
                if self.config.get('welcome_delivery_enabled'):self.welcome_delivery.expire(now)
                if now < self.next_send:
                    return False
                row = self.db.execute("""SELECT id,message,recipient,kind FROM jobs WHERE status='queued'
                    AND (?=0 OR kind<>'welcome' OR NOT EXISTS(SELECT 1 FROM welcome_delivery_state w WHERE w.job_id=jobs.id AND w.retry_at>?))
                    ORDER BY CASE kind WHEN 'management' THEN 0 WHEN 'kill_cap_kick' THEN -1 WHEN 'kd_kick' THEN 1
                    WHEN 'signature_fun' THEN 2 WHEN 'helicopter_fun' THEN 2 WHEN 'periodic' THEN 3 WHEN 'crash_advisory' THEN 3 ELSE 4 END,created,rowid LIMIT 1""",(int(self.config.get('welcome_delivery_enabled',False)),now)).fetchone()
                if row is None:
                    return False
                job_id, message, recipient, kind = row
                with self.db:
                    if self.announcement_policy.suppress(job_id, now):
                        return True
                if kind=='signature_fun' and not self.announcement_policy.ready(message):
                    with self.db:self.db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=?",(now,job_id))
                    return True
                if kind=='crash_advisory' and not self.crash_advisory.ready(message):
                    with self.db:self.db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=?",(now,job_id))
                    LOG.info('crash_advisory_cancelled reason=%s',self.crash_advisory.last_skip)
                    return True
                current_language = announcement_language(message, recipient, kind)
                if not language_ready(self.state, current_language, now, self.config['language_interval_seconds']):
                    return False
                record_language(self.state, current_language, now)
                self.next_send = now + self.config['message_interval_seconds']
                self.state['lastSend'] = now
                with self.db:
                    if kind=='signature_fun':self.announcement_policy.dispatched(now)
                    if kind=='crash_advisory':self.state['lastCrashAdvisoryAttempt']=now
                    self.db.execute("UPDATE jobs SET status='sending',updated=? WHERE id=?", (now, job_id))
                    self.save_state()
            status = 'sent'
            announcement = None
            retry_after = None
            try:
                if kind in ('kd_kick', 'kill_cap_kick'):
                    with self.lock:
                        def notices(player):
                            if kind == 'kill_cap_kick':
                                return kick_messages(clean_name(player.get('name', '')), '不符合本服要求')
                            kills, deaths, ratio = statistics(player)
                            return kd_messages(clean_name(player.get('name', '')), kills, deaths, ratio, kicked=True)
                        outcome = self.moderation.process_auto(job_id, recipient, message,
                            lambda player: kill_cap_eligible(player) if kind == 'kill_cap_kick' else kick_eligible(player, self.config), notices, now=now if fixed_time else None)
                        status = {'confirmed': 'sent', 'unknown': 'unknown', 'failed': 'failed'}.get(outcome['kickStatus'], 'cancelled')
                        if outcome['status'] in ('cancelled', 'offline'):
                            self.state.setdefault('kickAlerts', {}).pop(recipient, None)
                elif kind=='welcome' and self.config.get('welcome_delivery_enabled'):
                    with self.lock:
                        status=self.welcome_delivery.send(job_id,recipient,message,now=now if fixed_time else None)
                else:
                    path = '/v1/players/' + quote(recipient, safe='') + '/message' if recipient else '/v1/broadcast'
                    receipt=self.rcon.request('POST', path, {'message': message})
                    if kind=='crash_advisory' and (not isinstance(receipt,dict) or receipt.get('ok') is not True or receipt.get('pending')):
                        raise ValueError('advisory_broadcast_not_confirmed')
            except RconError as error:
                status = 'queued' if error.status == 429 else 'failed'
                if status == 'queued':
                    retry_after = error.retry_after
                LOG.warning('broadcast_refused job=%s http_status=%s', job_id, error.status)
            except Exception as error:
                status = 'unknown'
                LOG.warning('broadcast_uncertain job=%s error_type=%s', job_id, type(error).__name__)
            with self.lock:
                if retry_after is not None:
                    self.next_send = max(self.next_send, now + retry_after)
                finished = now if fixed_time else time.time()
                self.next_send = max(self.next_send, finished + self.config['message_interval_seconds'])
                self.state['lastSend'] = finished
                record_language(self.state, current_language, finished)
                with self.db:
                    self.db.execute('UPDATE jobs SET status=?,updated=? WHERE id=?', (status, finished, job_id))
                    if announcement is not None:
                        # Critical follow-up notices may exceed the soft queue cap by
                        # this three-message batch; priority delivery drains it first.
                        for translated in announcement:
                            self.add_job(translated, finished, kind='management')
                    self.save_state()
            LOG.info('delivery_result job=%s kind=%s channel=%s status=%s',
                     job_id, kind, 'kick' if kind in ('kd_kick', 'kill_cap_kick') else ('whisper' if recipient else 'broadcast'), status)
            return True

    def snapshot(self):
        with self.lock:
            counts = dict(self.db.execute('SELECT status,count(*) FROM jobs GROUP BY status'))
            public_config = copy.deepcopy(self.config)
            scopes = public_config.pop('admin_moderation_scopes', [])
            return {'lastSuccessfulPoll': self.last_poll, 'lastError': self.last_error,
                    'adminModeration': {'scopeCount': len(scopes)},
                    'kickGuard': self.kick_store.snapshot() if self.kick_store is not None else {'enabled': False},
                    'killAccounting': {'excludeBarbedWireKills': self.config.get('exclude_barbed_wire_kills',False),
                                       'lastError': self.kill_accounting.last_error},
                    'burstGuard': self.burst_guard.snapshot(),
                    'crashAdvisory': self.crash_advisory.snapshot(),
                    'helicopterFun': self.helicopter_fun.snapshot(),
                    'welcomeDelivery': self.welcome_delivery.snapshot(),
                    'behaviorPolicy': self.behavior.snapshot() if self.behavior is not None else {'players': []},
                    'playerCount': len(self.players), 'queueDepth': counts.get('queued', 0),
                    'deliveryCounts': counts, 'config': public_config}

    def job(self, job_id):
        with self.lock:
            row = self.db.execute('SELECT id,status,created,updated FROM jobs WHERE id=?', (job_id,)).fetchone()
            return dict(zip(['jobId', 'status', 'createdAt', 'updatedAt'], row)) if row else None


def api_handler(engine, api_key, moderation_api_key=None, admin_api_key=None):
    # The existing key keeps its query/broadcast contract, never punishment.
    if moderation_api_key == api_key:
        raise ValueError('Moderation execution key must differ from the existing service key')
    if admin_api_key is not None and admin_api_key in (api_key, moderation_api_key):
        raise ValueError('Admin execution key must differ from query and report keys')
    class Handler(BaseHTTPRequestHandler):
        server_version = 'WardogsMonitor/1'

        def log_message(self, fmt, *args):
            # Do not log URL/query strings, player content or auth headers.
            pass

        def respond(self, status, data):
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == '/healthz':
                last = engine.last_poll
                healthy = last is not None and time.time() - last < max(90, engine.config['poll_seconds'] * 3)
                return self.respond(200 if healthy else 503, {'healthy': healthy})
            if not self.authorized(operation=self.path.startswith('/v1/moderation/operations/')):
                return
            if self.path == '/v1/state':
                self.respond(200, engine.snapshot())
            elif self.path == '/v1/players':
                with engine.lock:
                    self.respond(200, {'players': engine.players, 'observedAt': engine.last_poll})
            elif self.path == '/v1/server-identity':
                try:
                    raw = engine.identity_path.read_bytes()
                    if len(raw) > 1048576:
                        raise ValueError('identity_too_large')
                    value = json.loads(raw)
                    sid, observed = value.get('serverId'), value.get('lastSuccess')
                    if (not isinstance(sid, str) or
                        not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', sid) or
                        type(observed) not in (int, float) or
                        not 0 <= time.time() - observed <= 90 or value.get('error') is not None):
                        raise ValueError('identity_unavailable')
                    self.respond(200, {'serverId': sid, 'observedAt': observed})
                except (OSError, ValueError, TypeError, AttributeError):
                    self.respond(503, {'error': {'code': 'server_identity_unavailable'}})
            elif self.path == '/v1/moderation/ban-statistics':
                try:
                    self.respond(200,engine.ban_statistics.get())
                except StatisticsUnavailable as error:
                    self.respond(503,{'error':{'code':error.code}})
            elif self.path == '/v1/match':
                try:
                    self.respond(200, engine.match_query.get())
                except MatchUnavailable as error:
                    self.respond(503, {'error': {'code': error.code}})
            elif self.path == '/openapi.json':
                self.respond(200, json.loads(Path(__file__).with_name('openapi.json').read_text()))
            elif self.path.startswith('/v1/jobs/'):
                result = engine.job(self.path.removeprefix('/v1/jobs/'))
                self.respond(200 if result else 404, result or {'error': 'not_found'})
            elif self.path.startswith('/v1/moderation/operations/'):
                opid=self.path.removeprefix('/v1/moderation/operations/')
                result = engine.moderation.get(opid) or engine.admin_history.get(opid) or engine.admin_cycle.get(opid) or engine.admin_unban.get(opid) or engine.admin_bans.get(opid)
                if result and (result['source'] == 'admin') != (self.operation_reader == 'admin'):
                    result = None
                self.respond(200 if result else 404, result or {'error': {'code': 'operation_not_found'}})
            else:
                self.respond(404, {'error': 'not_found'})

        def authorized(self, moderation=False, admin=False, operation=False):
            actual = self.headers.get('Authorization', '')
            if operation:
                candidates = [(moderation_api_key, 'report'), (admin_api_key, 'admin')]
                if not any(key is not None for key, _ in candidates):
                    self.respond(503, {'error': {'code': 'moderation_not_configured'}})
                    return False
                for key, reader in candidates:
                    if key is not None and hmac.compare_digest(actual.encode(), ('Bearer ' + key).encode()):
                        self.operation_reader = reader
                        return True
                self.respond(401, {'error': {'code': 'unauthorized'}})
                return False
            expected = admin_api_key if admin else (moderation_api_key if moderation else api_key)
            if expected is None:
                self.respond(503, {'error': {'code': 'admin_moderation_not_configured' if admin else 'moderation_not_configured'}})
                return False
            if not hmac.compare_digest(actual.encode(), ('Bearer ' + expected).encode()):
                self.respond(401, {'error': {'code': 'unauthorized'}} if moderation or admin else {'error': 'unauthorized'})
                return False
            return True

        def do_POST(self):
            is_admin = self.path in ('/v1/moderation/player-chats','/v1/moderation/player-sanction-summary','/v1/moderation/admin-bans','/v1/moderation/player-kills','/v1/moderation/current-restrictions','/v1/moderation/admin-kicks','/v1/moderation/admin-cycle-adds','/v1/moderation/admin-unbans','/v1/moderation/unban-search','/v1/moderation/cycle-list','/v1/moderation/admin-restrictions','/v1/moderation/player-search','/v1/moderation/recent-kills','/v1/moderation/steam-profiles','/v1/moderation/punishment-search','/v1/moderation/punishment-history')
            is_moderation = self.path == '/v1/moderation/kicks' or is_admin
            if not self.authorized(moderation=self.path == '/v1/moderation/kicks', admin=is_admin):
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                maximum = 16384 if is_moderation else 4096
                if not 0 < length <= maximum:
                    error = {'error': {'code': 'invalid_body_size'}, 'maxBytes': maximum} if maximum == 16384 else {'error': 'body_size_must_be_1_to_4096'}
                    return self.respond(413, error)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('body must be an object')
                if self.path == '/v1/moderation/kicks':
                    result = engine.moderation.submit(data, self.headers.get('Idempotency-Key'))
                    self.respond(202, result)
                elif self.path == '/v1/moderation/player-search':
                    self.respond(200,engine.admin_history.search(data))
                elif self.path == '/v1/moderation/punishment-search':
                    self.respond(200,engine.punishment_history.search(data))
                elif self.path == '/v1/moderation/punishment-history':
                    self.respond(200,engine.punishment_history.query(data))
                elif self.path == '/v1/moderation/steam-profiles':
                    self.respond(200,engine.steam_profiles.query(data))
                elif self.path == '/v1/moderation/admin-bans':
                    self.respond(202,engine.admin_bans.submit(data,self.headers.get('Idempotency-Key')))
                elif self.path == '/v1/moderation/player-sanction-summary':
                    self.respond(200,engine.player_sanction_summary.query(data))
                elif self.path == '/v1/moderation/player-chats':
                    self.respond(200,engine.player_chats.query(data))
                elif self.path == '/v1/moderation/player-kills':
                    self.respond(200,engine.player_kills.query(data))
                elif self.path == '/v1/moderation/recent-kills':
                    self.respond(200,engine.recent_kills.query(data))
                elif self.path == '/v1/moderation/admin-unbans':
                    self.respond(202,engine.admin_unban.submit(data,self.headers.get('Idempotency-Key')))
                elif self.path == '/v1/moderation/unban-search':
                    self.respond(200,engine.admin_tools.unban_search(data))
                elif self.path == '/v1/moderation/current-restrictions':
                    self.respond(200,engine.current_restrictions.query(data))
                elif self.path == '/v1/moderation/cycle-list':
                    self.respond(200,engine.admin_tools.cycle_list(data))
                elif self.path == '/v1/moderation/admin-cycle-adds':
                    self.respond(202,engine.admin_cycle.submit(data,self.headers.get('Idempotency-Key')))
                elif self.path == '/v1/moderation/admin-restrictions':
                    self.respond(202,engine.admin_history.submit(data,self.headers.get('Idempotency-Key')))
                elif is_admin:
                    result = engine.moderation.submit_admin(data, self.headers.get('Idempotency-Key'))
                    self.respond(202, result)
                elif self.path == '/v1/broadcast':
                    if not data.get('english') or not data.get('japanese'):
                        raise ValueError('provide message, english and japanese together')
                    result = engine.enqueue_broadcast(data.get('message'), self.headers.get('Idempotency-Key'),
                                                      data['english'], data['japanese'])
                    self.respond(202, result)
                else:
                    self.respond(404, {'error': 'not_found'})
            except HistoryUnavailable as error:
                self.respond(503,{'error':{'code':error.code}})
            except sqlite3.Error:
                self.respond(503,{'error':{'code':'history_storage_unavailable'}})
            except ContractError as error:
                self.respond(error.status, {'error': {'code': error.code}})
            except (ValueError, TypeError) as error:
                # Error strings here are fixed validation messages, never raw upstream bodies.
                if is_moderation:
                    self.respond(400, {'error': {'code': 'invalid_json' if isinstance(error, json.JSONDecodeError) else 'invalid_request'}})
                else:
                    self.respond(400, {'error': str(error) if not isinstance(error, json.JSONDecodeError) else 'invalid_json'})

    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = copy.deepcopy(DEFAULT_CONFIG)
    config_path = Path(os.environ.get('CONFIG_FILE', '/app/config.json'))
    config.update(json.loads(config_path.read_text()))
    scope_file = os.environ.get('ADMIN_MODERATION_SCOPES_FILE')
    if scope_file:
        load_admin_scopes(config, scope_file)
    validate_admin_config(config)
    for flag in ('japanese_announcements_enabled','signature_fun_enabled'):
        if not isinstance(config[flag], bool):raise ValueError(flag+' must be boolean')
    if not isinstance(config['exclude_barbed_wire_kills'], bool):
        raise ValueError('exclude_barbed_wire_kills must be boolean')
    if not isinstance(config['high_kd_kick'], bool):
        raise ValueError('high_kd_kick must be boolean')
    for field in ['poll_seconds', 'kd_threshold', 'kick_kd_threshold', 'kick_min_kills', 'alert_cooldown_seconds', 'global_announcement_seconds', 'message_interval_seconds',
                  'rejoin_grace_seconds', 'queue_limit']:
        value = config[field]
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid config field: ' + field)
    if not isinstance(config['kick_min_kills'], int) or isinstance(config['kick_min_kills'], bool):
        raise ValueError('kick_min_kills must be a positive integer')
    for field in ['moderation_identity_threshold', 'moderation_violation_threshold']:
        value = config[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1:
            raise ValueError('Invalid moderation threshold: ' + field)
    for field in ['moderation_decision_max_age_seconds', 'moderation_future_skew_seconds',
                  'moderation_target_cooldown_seconds', 'moderation_pending_limit']:
        if isinstance(config[field], bool) or not isinstance(config[field], int) or config[field] <= 0:
            raise ValueError('Invalid moderation limit: ' + field)
    if not isinstance(config['moderation_rules'], dict) or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in config['moderation_rules'].items()):
        raise ValueError('Invalid moderation rule versions')
    for rules_key in ['rules', 'global_rules']:
        if not isinstance(config[rules_key], list) or not config[rules_key] or any(
                not isinstance(s, str) or not s or len(s.replace('{name}', 'x' * 40)) > 200 or
                any(not c.isprintable() for c in s) for s in config[rules_key]):
            raise ValueError('Invalid announcement rules')
    validate_network(config)
    if not isinstance(config['welcome_delivery_enabled'], bool):
        raise ValueError('welcome_delivery_enabled must be boolean')
    if not isinstance(config['helicopter_fun_enabled'], bool):
        raise ValueError('helicopter_fun_enabled must be boolean')
    if not isinstance(config['auto_native_ban_enabled'], bool) or (config['auto_native_ban_enabled'] and not config['rejoin_guard_enabled']):
        raise ValueError('auto_native_ban_enabled requires boolean and permanent rejoin guard')
    if not isinstance(config['burst_ban_enabled'], bool) or (config['burst_ban_enabled'] and not config['rejoin_guard_enabled']):
        raise ValueError('burst_ban_enabled requires boolean and permanent rejoin guard')
    if not isinstance(config['high_kill_kick'], bool):
        raise ValueError('high_kill_kick must be boolean')
    if not isinstance(config['rejoin_guard_enabled'], bool):
        raise ValueError('rejoin_guard_enabled must be boolean')
    if not isinstance(config['reserved_exemptions_enabled'], bool):
        raise ValueError('reserved_exemptions_enabled must be boolean')
    if isinstance(config['language_interval_seconds'], bool) or not isinstance(config['language_interval_seconds'], (int, float)) or not math.isfinite(config['language_interval_seconds']) or config['language_interval_seconds'] < 10:
        raise ValueError('language_interval_seconds must be at least 10')
    key = Path(os.environ.get('API_KEY_FILE', '/run/secrets/service_api_key')).read_text().strip()
    if len(key) < 32:
        raise ValueError('API token must have at least 32 characters')
    moderation_key = None
    moderation_key_file = os.environ.get('MODERATION_API_KEY_FILE')
    if moderation_key_file:
        moderation_key = Path(moderation_key_file).read_text().strip()
        if len(moderation_key) < 32 or hmac.compare_digest(key.encode(), moderation_key.encode()):
            raise ValueError('Configure a separate moderation execution token of at least 32 characters')
    admin_key = None
    admin_key_file = os.environ.get('ADMIN_MODERATION_API_KEY_FILE')
    if admin_key_file:
        admin_key = Path(admin_key_file).read_text().strip()
        if len(admin_key) < 32 or admin_key in (key, moderation_key):
            raise ValueError('Configure a separate admin execution token of at least 32 characters')
    engine = Engine(Path(os.environ.get('STATE_DB', '/data/state.db')),
                    Rcon(os.environ['RCON_HOST'],
                         int(os.environ.get('RCON_PORT', '10246')),
                         os.environ.get('RCON_KEY_FILE', '/run/secrets/rcon_key')), config)
    stop = threading.Event()

    def poll_loop():
        while not stop.is_set():
            started = time.monotonic()
            try:
                engine.poll()
            except Exception:
                pass
            stop.wait(max(0.1, config['poll_seconds'] - (time.monotonic() - started)))

    def send_loop():
        while not stop.is_set():
            try:
                engine.send_one()
            except Exception as error:
                LOG.error('sender_failed error_type=%s', type(error).__name__)
            stop.wait(0.5)

    def moderation_loop():
        while not stop.is_set():
            try:
                engine.moderation.process_one()
                engine.admin_history.process_one()
                engine.admin_cycle.process_one()
                engine.admin_unban.process_one()
                engine.admin_bans.process_one()
            except Exception as error:
                LOG.error('moderation_worker_failed error_type=%s', type(error).__name__)
            stop.wait(0.5)

    def burst_loop():
        while not stop.is_set():
            try:
                engine.burst_guard.tick()
            except Exception as error:
                LOG.error('burst_worker_failed error_type=%s',type(error).__name__)
            stop.wait(5)

    def helicopter_loop():
        while not stop.is_set():
            try:
                engine.helicopter_fun.tick()
            except Exception as error:
                LOG.error('helicopter_fun_failed error_type=%s',type(error).__name__)
            stop.wait(1)

    def behavior_loop():
        while not stop.is_set():
            try:
                if engine.behavior is not None:
                    engine.behavior.tick()
            except Exception as error:
                LOG.error('behavior_worker_failed error_type=%s', type(error).__name__)
            stop.wait(1)

    server = ThreadingHTTPServer((os.environ.get('API_HOST', '127.0.0.1'), int(os.environ.get('API_PORT', '8080'))), api_handler(engine, key, moderation_key, admin_key))
    server.daemon_threads = True
    server.timeout = 1
    # Limit the time a slow local API client can hold a handler.
    original_get_request = server.get_request
    def get_request():
        sock, address = original_get_request()
        sock.settimeout(5)
        return sock, address
    server.get_request = get_request
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    workers = [threading.Thread(target=poll_loop), threading.Thread(target=send_loop), threading.Thread(target=moderation_loop), threading.Thread(target=behavior_loop), threading.Thread(target=burst_loop), threading.Thread(target=helicopter_loop)]
    for worker in workers:
        worker.start()
    LOG.info('service_started poll_seconds=%s', config['poll_seconds'])
    try:
        while not stop.is_set():
            server.handle_request()
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=25)
        server.server_close()
        engine.close()


if __name__ == '__main__':
    main()
