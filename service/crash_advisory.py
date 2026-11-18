"""One Chinese whole-message advisory, conditional on a fresh online roster."""
import json
from pathlib import Path
import re

KIND='crash_advisory'
INTERVAL=300
MINIMUM_EXCLUSIVE=20


def count_players(data):
    if not isinstance(data,dict) or data.get('ok') is False or data.get('pending') or data.get('complete') is False or data.get('hasMore') or data.get('truncated'):
        return None
    players=data.get('players')
    if not isinstance(players,list):return None
    ids=[p.get('steamId') if isinstance(p,dict) else None for p in players]
    if any(not isinstance(sid,str) or not re.fullmatch(r'[0-9]{17}',sid) for sid in ids) or len(ids)!=len(set(ids)):return None
    if 'count' in data and (type(data['count']) is not int or data['count']!=len(ids)):return None
    return len(ids)


class CrashAdvisory:
    def __init__(self,engine,path):
        self.e=engine;self.path=Path(path).with_name('crash-advisory-policy.json');self.last_error=None;self.last_skip=None

    def policy(self):
        if not self.path.exists():return None
        try:
            value=json.loads(self.path.read_text())
            message=value['message']
            if type(value['version']) is not int or value['version']!=1 or type(value['enabled']) is not bool:
                raise ValueError('invalid_advisory_policy')
            if not isinstance(message,str) or not 1<=len(message)<=200 or any(not c.isprintable() for c in message):
                raise ValueError('invalid_advisory_message')
            followup=value.get('followup')
            if followup is not None and (not isinstance(followup,str) or not 1<=len(followup)<=200 or any(not c.isprintable() for c in followup)):
                raise ValueError('invalid_advisory_followup')
            self.last_error=None
            return value if value['enabled'] else None
        except (OSError,ValueError,KeyError,TypeError):
            self.last_error='advisory_policy_invalid';return None

    def schedule(self,now,players):
        policy=self.policy();count=count_players({'players':players})
        if not policy or count is None or count<=MINIMUM_EXCLUSIVE:
            self.e.db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE kind=? AND status='queued'",(now,KIND));return
        if self.e.db.execute("SELECT 1 FROM jobs WHERE kind=? AND status IN ('queued','sending') LIMIT 1",(KIND,)).fetchone():return
        last=self.e.state.get('lastCrashAdvisoryAttempt')
        if last is not None and now-last<INTERVAL:
            # Hot-adding the second notice may complete the already-sent round,
            # once only. A cancelled/failed/unknown followup is never re-enqueued.
            if policy.get('followup'):
                primary=self.e.db.execute("SELECT created,updated,status FROM jobs WHERE kind=? AND message=? ORDER BY created DESC,rowid DESC LIMIT 1",(KIND,policy['message'])).fetchone()
                if primary and primary[2]=='sent' and 0<=now-primary[1]<INTERVAL and not self.e.db.execute("SELECT 1 FROM jobs WHERE kind=? AND message=? AND created>=?",(KIND,policy['followup'],primary[0])).fetchone():
                    self.e.add_job(policy['followup'],now,kind=KIND)
            return
        self.e.add_job(policy['message'],now,kind=KIND)
        if policy.get('followup'):self.e.add_job(policy['followup'],now,kind=KIND)

    def ready(self,message):
        policy=self.policy()
        if not policy or message not in (policy['message'],policy.get('followup')):
            self.last_skip='disabled_or_replaced';return False
        if message==policy.get('followup'):
            primary=self.e.db.execute("SELECT status FROM jobs WHERE kind=? AND message=? ORDER BY created DESC,rowid DESC LIMIT 1",(KIND,policy['message'])).fetchone()
            if not primary or primary[0]!='sent':
                self.last_skip='primary_not_confirmed';return False
        try:count=count_players(self.e.rcon.request('GET','/v1/players'))
        except Exception:count=None
        if count is None or count<=MINIMUM_EXCLUSIVE:
            self.last_skip='online_count_unavailable' if count is None else 'online_count_not_above_20';return False
        self.last_skip=None;return True

    def snapshot(self):
        policy=self.policy();last=self.e.state.get('lastCrashAdvisoryAttempt')
        return {'enabled':bool(policy),'intervalSeconds':INTERVAL,'minimumPlayersExclusive':MINIMUM_EXCLUSIVE,'language':'zh','splitMessage':False,'messageCount':2 if policy and policy.get('followup') else 1,'lastAttemptAt':last,'nextEligibleAt':last+INTERVAL if last is not None else None,'lastError':self.last_error,'lastSkip':self.last_skip}
