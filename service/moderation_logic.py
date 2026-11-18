"""Shared CLI and service moderation logic. No credential or process handling."""

import time
from urllib.parse import quote

try:
    from service.announcements import kick_messages, validate_messages
    from service.rcon import PunishmentExemptError, PunishmentDeferredError
except ImportError:
    from announcements import kick_messages, validate_messages
    from rcon import PunishmentExemptError, PunishmentDeferredError


class OperationError(Exception):
    def __init__(self, result):
        super().__init__(result['stage'])
        self.result = result


def validate_text(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 200 or any(not c.isprintable() for c in text):
        raise ValueError('消息必须为1至200个可打印字符，不能包含换行')
    return text


def kick_notice(name, reason):
    validate_text(name)
    validate_text(reason)
    return validate_text(f'管理通告：玩家{name}因{reason}已被踢出。'
                         '本服为社区服务器，黄圈外不打架，黄圈内不限制。')


def checked_request(request, method, path, body=None):
    result = request(method, path, body)
    if not isinstance(result, dict) or result.get('ok') is False or result.get('pending'):
        raise ValueError('服务端未确认操作完成')
    return result


def resolve_player(request, target):
    """Resolve a fresh player response without falling back from ID to nickname."""
    players = checked_request(request, 'GET', '/v1/players').get('players')
    if not isinstance(players, list) or any(not isinstance(p, dict) for p in players):
        raise ValueError('玩家列表无效')
    is_id = len(target) == 17 and target.isascii() and target.isdigit()
    matches = [p for p in players if str(p.get('steamId')) == target] if is_id else [
        p for p in players if str(p.get('name', '')).strip().casefold() == target.strip().casefold()]
    if len(matches) > 1:
        raise ValueError('玩家身份不唯一')
    if not matches:
        return None
    if not matches[0].get('steamId') or not isinstance(matches[0].get('name'), str):
        raise ValueError('玩家身份无效')
    return matches[0]


def kick_player(request, steam_id, reason):
    result = checked_request(request, 'POST', '/v1/players/' + quote(steam_id, safe='') + '/kick', {'reason': reason})
    if result.get('ok') is not True:
        raise ValueError('服务端未明确确认踢出')
    return result


def perform(mode, request, target=None, reason=None, message=None, english=None, japanese=None):
    result = {'mode': mode, 'kickExecuted': False, 'announced': False}
    if mode not in ('kick', 'kick-notice', 'announce'):
        raise ValueError('未知操作')
    # Render the entire announcement before any write, so a length error cannot
    # leave a successful kick without a sendable announcement.
    announcements = validate_messages([message, english, japanese]) if mode == 'announce' else kick_messages(target, reason, english, japanese)
    if mode == 'kick':
        result['stage'] = 'lookup'
        try:
            player = resolve_player(request, target)
            if player is None:
                result['matchCount'] = 0
                raise ValueError('必须唯一匹配玩家')
            announcements = kick_messages(str(player['name']), reason, english, japanese)
            result.update(name=player['name'], steamId=str(player['steamId']), stage='kick')
            kick_player(request, result['steamId'], reason)
            result['kickExecuted'] = True
        except Exception as error:
            result['errorType'] = type(error).__name__
            result['outcomeUnknown'] = result['stage'] == 'kick' and not isinstance(error, (PunishmentExemptError, PunishmentDeferredError))
            if isinstance(error, (PunishmentExemptError, PunishmentDeferredError)):
                result['errorCode'] = error.code
            raise OperationError(result) from error
    result['stage'] = 'announcement'
    result['sentMessages'] = []
    try:
        for index, announcement in enumerate(announcements):
            if index:
                time.sleep(3)
            checked_request(request, 'POST', '/v1/broadcast', {'message': announcement})
            result['sentMessages'].append(announcement)
        result.update(announced=True, messages=announcements, stage='complete')
    except Exception as error:
        result.update(errorType=type(error).__name__, outcomeUnknown=True)
        raise OperationError(result) from error
    if mode == 'kick':
        try:
            players = checked_request(request, 'GET', '/v1/players')['players']
            result['targetStillOnline'] = any(str(p.get('steamId')) == result['steamId'] for p in players)
        except Exception as error:
            result['verificationErrorType'] = type(error).__name__
    return result
