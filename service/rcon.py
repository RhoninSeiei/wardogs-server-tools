"""RCON transport; credentials stay inside the request process."""

import http.client
import json
import re
from pathlib import Path


class PunishmentExemptError(ValueError):
    code = 'reserved_player_exempt'


class PunishmentDeferredError(ValueError):
    code = 'reserved_list_unavailable'


def reserved_ids(data):
    values = data.get('reservedSlots') if isinstance(data, dict) else None
    if not isinstance(values, list) or any(not isinstance(v, str) or not re.fullmatch(r'[0-9]{17}', v) for v in values):
        raise ValueError('invalid reserved player list')
    return set(values)


class RconError(Exception):
    def __init__(self, status, retry_after=30, *, method=None, path=None):
        super().__init__(f'RCON HTTP {status}')
        self.status = status
        self.retry_after = retry_after
        self.method, self.path = method, path


class Rcon:
    def __init__(self, host, port, key_file):
        self.host, self.port, self.key_file = host, port, Path(key_file)

    def request(self, method, path, body=None):
        identity = None
        match = re.fullmatch(r'/v1/players/([0-9]{17})(/(kick|kill))?', path)
        if match and ((method == 'POST' and match.group(3) in ('kick', 'kill')) or (method == 'PATCH' and match.group(2) is None)):
            identity = match.group(1)
        elif method == 'POST' and path == '/v1/bans':
            identity = body.get('steamId') if isinstance(body, dict) else None
            if not isinstance(identity, str) or not re.fullmatch(r'[0-9]{17}', identity):
                raise ValueError('invalid ban SteamID64')
        if identity is not None:
            # Fresh authoritative check on every destructive dispatch, including
            # manual scripts. Read failures stop dispatch rather than assume empty.
            try:
                current_reserved = reserved_ids(self._request('GET', '/v1/reserved-slots'))
            except Exception as error:
                raise PunishmentDeferredError('reserved list preflight failed: ' + type(error).__name__) from error
            if identity in current_reserved:
                raise PunishmentExemptError('reserved_player_exempt')
        return self._request(method, path, body)

    def _request(self, method, path, body=None):
        key = self.key_file.read_text(encoding='utf-8-sig').strip()
        if not key:
            raise ValueError('RCON credential file is empty')
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode()
                headers['Content-Type'] = 'application/json'
            conn.request(method, path, payload, headers)
            response = conn.getresponse()
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError('RCON response too large')
            if not 200 <= response.status < 300:
                try:
                    retry = min(300, max(1, int(response.getheader('Retry-After', '30'))))
                except ValueError:
                    retry = 30
                raise RconError(response.status, retry, method=method, path=path)
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get('ok') is False:
                raise ValueError('RCON business operation refused')
            return data
        finally:
            conn.close()
