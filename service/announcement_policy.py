"""Delivery policy and persisted random joke scheduling; never performs moderation."""
import random
try:
    from service.crash_advisory import count_players
except ImportError:
    from crash_advisory import count_players

FUN_MESSAGE = '本服务器一名玩家因使用的英伟达显卡有黄仁勋签名，已被封禁。'
FICTIONAL_NAMES = ('维亚切斯拉夫·巴兰尼科夫', '安东·尤丁采夫', '尼基塔·布亚诺夫')
FICTIONAL_MESSAGES = tuple(name+'因在本服务器中作弊，已被发射。' for name in FICTIONAL_NAMES)
# Alternate the signature joke and one name, retaining the name order.
FUN_MESSAGES = tuple(message for named in FICTIONAL_MESSAGES for message in (FUN_MESSAGE, named))
ACTION_KINDS = frozenset(('kd_kick', 'kill_cap_kick'))

class AnnouncementPolicy:
    def __init__(self, engine):
        self.e = engine
        self.e.db.execute('''CREATE TABLE IF NOT EXISTS announcement_delivery_policy(
            job_id TEXT PRIMARY KEY, language TEXT, suppressed_at REAL, reason TEXT)''')

    def muted(self, message, kind, language=None):
        return (self.e.config.get('japanese_announcements_enabled', True) is False
                and kind not in ACTION_KINDS
                and (language == 'ja' or message.lstrip().startswith('[JA]')))

    def register(self, job_id, language=None):
        self.e.db.execute('INSERT INTO announcement_delivery_policy(job_id,language) VALUES(?,?)', (job_id, language))

    def suppress(self, job_id, now):
        row = self.e.db.execute('SELECT message,kind,status FROM jobs WHERE id=?', (job_id,)).fetchone()
        hint = self.e.db.execute('SELECT language FROM announcement_delivery_policy WHERE job_id=?', (job_id,)).fetchone()
        if row is None or row[2] != 'queued' or not self.muted(row[0], row[1], hint[0] if hint else None):
            return False
        self.e.db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=? AND status='queued'", (now, job_id))
        self.e.db.execute('''INSERT INTO announcement_delivery_policy VALUES(?,'ja',?,'japanese_temporarily_paused')
            ON CONFLICT(job_id) DO UPDATE SET suppressed_at=excluded.suppressed_at,reason=excluded.reason''', (job_id, now))
        return True

    def split(self, jobs):
        active, skipped = [], []
        for job in jobs:
            row = self.e.db.execute('SELECT suppressed_at,reason FROM announcement_delivery_policy WHERE job_id=?', (job[1],)).fetchone()
            if job[2] == 'cancelled' and row and row[0] is not None:
                skipped.append({'language': job[0], 'jobId': job[1], 'status': 'cancelled', 'reason': row[1], 'suppressedAt': row[0]})
            else:
                active.append(job)
        return active, skipped

    def schedule(self, now, players):
        if not self.e.config.get('signature_fun_enabled', False):
            return
        due = self.e.state.get('nextSignatureFunAt')
        if due is None:
            self.e.state['nextSignatureFunAt'] = now + random.SystemRandom().randint(300, 900)
            return
        if now < due or not players or self.e.db.execute("SELECT 1 FROM jobs WHERE kind='signature_fun' AND status IN ('queued','sending')").fetchone():
            return
        index = self.e.state.get('signatureFunSequence', 0)
        self.e.add_job(FUN_MESSAGES[index % len(FUN_MESSAGES)], now, kind='signature_fun')

    def ready(self, message=None):
        if message is not None and message not in FUN_MESSAGES:
            return False
        if not self.e.config.get('signature_fun_enabled', False):
            return False
        try:
            players = self.e.rcon.request('GET', '/v1/players')
            count = count_players(players)
            return count is not None and count > 0
        except Exception:
            return False

    def dispatched(self, now):
        self.e.state['signatureFunSequence'] = (self.e.state.get('signatureFunSequence', 0) + 1) % len(FUN_MESSAGES)
        self.e.state['nextSignatureFunAt'] = now + random.SystemRandom().randint(300, 900)
