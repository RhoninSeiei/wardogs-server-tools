"""Narrow, fail-closed checks for the two explicitly authorized restart repairs."""
import json
import math
from datetime import datetime
from pathlib import Path
import re
from zoneinfo import ZoneInfo
try:
    from service.moderation_ops import ContractError
    from service.admin_tools import native_bans
    from service.current_restrictions import CompleteRead
except ImportError:
    from moderation_ops import ContractError
    from admin_tools import native_bans
    from current_restrictions import CompleteRead

PREFIX='restart-unban-v1:'
ZONE=ZoneInfo('Asia/Shanghai')

def scheduled(payload):
    return isinstance(payload,dict) and isinstance(payload.get('requestId'),str) and payload['requestId'].startswith(PREFIX)

def policy(root):
    try:
        value=json.loads((Path(root)/'restart-unban-policy.json').read_text())
        assert type(value['version']) is int and value['version']==1 and value['enabled'] is True
        assert isinstance(value['targets'],dict) and 1<=len(value['targets'])<=2
        for sid,target in value['targets'].items():
            assert re.fullmatch(r'[0-9]{17}',sid)
            assert isinstance(target['releaseOperationId'],str) and target['releaseOperationId'].startswith('au_')
            assert type(target['releaseAt']) in (int,float) and math.isfinite(target['releaseAt']) and target['releaseAt']>0
            assert target['nativeEntry']=={'steamId':sid,'bannedBy':'config','bannedAtUtc':'0001-01-01T00:00:00.000Z','reason':None}
        return value
    except (OSError,ValueError,KeyError,TypeError,AssertionError):
        raise ContractError(409,'restart_unban_policy_unavailable') from None

def check_boot(now,health):
    local=datetime.fromtimestamp(now,ZONE)
    if not (5*60+5 <= local.hour*60+local.minute < 5*60+30):
        raise ContractError(409,'restart_unban_outside_window')
    if not isinstance(health,dict) or health.get('ok') is False or health.get('pending'):
        raise ContractError(409,'restart_unban_restart_unverified')
    uptime=health.get('uptimeSeconds')
    if type(uptime) not in (float,int) or not math.isfinite(uptime) or uptime<60:
        raise ContractError(409,'restart_unban_restart_unverified')
    boot=now-uptime;day=datetime.fromtimestamp(boot,ZONE)
    if day.date()!=local.date() or not (5*60 <= day.hour*60+day.minute < 5*60+30):
        raise ContractError(409,'restart_unban_restart_unverified')
    return local.date().isoformat(),boot

def validate(engine,payload,current,now):
    """Called under the engine, target and guard locks, before release AND DELETE."""
    if not scheduled(payload):return
    sid=payload['steamId'];root=engine.unknown_handoff.directory.parent
    rule=policy(root);target=rule['targets'].get(sid)
    if target is None:raise ContractError(409,'restart_unban_target_not_authorized')
    date,_=check_boot(now,engine.rcon.request('GET','/v1/health'))
    if payload['requestId']!=PREFIX+date+':'+sid:raise ContractError(409,'restart_unban_invalid_daily_key')
    base=engine.db.execute('SELECT steam_id,status,unban_status,native_status,local_status,released_at FROM admin_unbans WHERE id=?',(target['releaseOperationId'],)).fetchone()
    if base!=(sid,'completed','unbanned','unbanned','already_released',target['releaseAt']):
        # Some verified base releases actually released local restrictions.
        if not (base and base[0:4]==(sid,'completed','unbanned','unbanned') and base[4] in ('released','already_released','not_restricted') and base[5]==target['releaseAt']):
            raise ContractError(409,'restart_unban_release_unverified')
    guard=engine.kick_store
    for table in ('rejoin_restrictions','native_ban_sync','restriction_provenance','silent_restrictions'):
        if guard.db.execute('SELECT 1 FROM '+table+' WHERE steam_id=? LIMIT 1',(sid,)).fetchone():
            raise ContractError(409,'restart_unban_current_restriction')
    if guard.db.execute('SELECT 1 FROM behavior_players WHERE steam_id=? AND enabled=1',(sid,)).fetchone():
        raise ContractError(409,'restart_unban_behavior_active')
    for table,column in [('confirmed_kicks','confirmed_at'),('permanent_ban_actions','created_at')]:
        if guard.db.execute('SELECT 1 FROM '+table+' WHERE steam_id=? AND '+column+'>=? LIMIT 1',(sid,target['releaseAt'])).fetchone():
            raise ContractError(409,'restart_unban_new_punishment')
    if any(not (root/name).is_file() for name in ('weapon-kick.sqlite3','name-ban-watch.sqlite3','cycle-exit-bans.sqlite3')):
        raise ContractError(409,'restart_unban_ledger_unavailable')
    if not engine.unknown_handoff.no_other_tasks(sid,'',current,target['releaseAt']):
        raise ContractError(409,'restart_unban_new_or_uncertain_task')
    entry=native_bans(CompleteRead(engine)).get(sid)
    if entry!=target['nativeEntry']:raise ContractError(409,'restart_unban_native_entry_changed')
