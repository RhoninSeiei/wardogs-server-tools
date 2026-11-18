"""Administrator-only public Steam profile and ban lookup; never executes punishment."""
from contextlib import closing
from email.utils import parsedate_to_datetime
import http.client
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlencode
try:
    from service.moderation_ops import ContractError, validate_admin_actor
except ImportError:
    from moderation_ops import ContractError, validate_admin_actor

CACHE_SECONDS = 600
METHODS = {'profile':'GetPlayerSummaries/v2/', 'bans':'GetPlayerBans/v1/'}


def identity(value):
    return isinstance(value,str) and re.fullmatch('[0-9]{17}',value) is not None


def string(value, maximum=256):
    return value if isinstance(value,str) and len(value)<=maximum and all(c.isprintable() for c in value) else None


def integer(value):
    return value if type(value) is int and 0<=value<=2**63-1 else None


class SteamFailure(Exception):
    def __init__(self,code,retry_after=None):
        super().__init__(code)
        self.code,self.retry_after=code,retry_after


def retry_seconds(header,now=None):
    now=time.time() if now is None else now
    try:
        value=int(header)
    except (TypeError,ValueError):
        try:value=math.ceil(parsedate_to_datetime(header).timestamp()-now)
        except (TypeError,ValueError,OverflowError):value=60
    return max(1,min(86400,value))


class SteamTransport:
    def __init__(self,key_file):self.key_file=Path(key_file)

    def fetch(self,kind,ids):
        if kind not in METHODS or not 1<=len(ids)<=100 or any(not identity(i) for i in ids):
            raise SteamFailure('steam_invalid_request')
        try:key=self.key_file.read_text(encoding='utf-8-sig').strip()
        except (OSError,UnicodeError):raise SteamFailure('steam_api_not_configured') from None
        if not re.fullmatch('[0-9A-Fa-f]{32}',key):raise SteamFailure('steam_api_not_configured')
        connection=None
        try:
            # Fixed TLS host/path only. The key is never returned, logged, or
            # exposed through redirects; http.client never follows redirects.
            connection=http.client.HTTPSConnection('api.steampowered.com',443,timeout=15 if kind=='bans' else 4)
            path='/ISteamUser/'+METHODS[kind]+'?'+urlencode({'key':key,'steamids':','.join(ids)})
            connection.request('GET',path,headers={'Accept':'application/json','User-Agent':'WARDOGS-Admin/1.11'})
            response=connection.getresponse()
            if response.status==429:raise SteamFailure('steam_rate_limited',retry_seconds(response.getheader('Retry-After')))
            if response.status in (401,403):raise SteamFailure('steam_auth_failed')
            if response.status!=200:raise SteamFailure('steam_http_error')
            body=response.read(2097153)
            if len(body)>2097152:raise SteamFailure('steam_response_too_large')
            return json.loads(body)
        except SteamFailure:raise
        except TimeoutError:raise SteamFailure('steam_timeout') from None
        except (OSError,http.client.HTTPException,ValueError,UnicodeError):raise SteamFailure('steam_upstream_error') from None
        finally:
            if connection is not None:connection.close()


def normalize(kind,row):
    if kind=='profile':
        country=row.get('loccountrycode')
        country=country if isinstance(country,str) and re.fullmatch('[A-Z]{2}',country) else None
        return {'name':string(row.get('personaname')), 'avatarUrl':string(row.get('avatarfull'),1024),
                'visibilityState':integer(row.get('communityvisibilitystate')),
                'countryCode':country,'stateCode':string(row.get('locstatecode'),32),
                'cityId':integer(row.get('loccityid')),'countrySource':'self_reported_profile',
                'accountCreatedAt':integer(row.get('timecreated'))}
    bools={'vacBanned':'VACBanned','communityBanned':'CommunityBanned'}
    ints={'numberOfVacBans':'NumberOfVACBans','daysSinceLastBan':'DaysSinceLastBan','numberOfGameBans':'NumberOfGameBans'}
    if any(type(row.get(v)) is not bool for v in bools.values()) or any(integer(row.get(v)) is None for v in ints.values()):
        raise SteamFailure('steam_invalid_ban_response')
    economy=string(row.get('EconomyBan'),64)
    if economy is None:raise SteamFailure('steam_invalid_ban_response')
    return {**{k:row[v] for k,v in bools.items()},**{k:row[v] for k,v in ints.items()},'economyBan':economy}


def parse_rows(kind,data,requested):
    try:rows=data['response']['players'] if kind=='profile' else data['players']
    except (KeyError,TypeError):raise SteamFailure('steam_invalid_response') from None
    if not isinstance(rows,list) or len(rows)>len(requested):raise SteamFailure('steam_invalid_response')
    result={};field='steamid' if kind=='profile' else 'SteamId'
    for row in rows:
        if not isinstance(row,dict) or row.get(field) not in requested or row[field] in result:
            raise SteamFailure('steam_identity_mismatch')
        result[row[field]]=normalize(kind,row)
    return result


class SteamProfiles:
    def __init__(self,engine,path,key_file=None,transport=None):
        self.engine,self.path=engine,Path(path)
        self.transport=transport or SteamTransport(key_file or os.environ.get('STEAM_WEB_API_KEY_FILE','/run/secrets/steam_web_api_key'))
        self.lock=threading.Lock()
        with closing(self.connect()) as c, c:
            c.executescript('''CREATE TABLE IF NOT EXISTS profiles(steam_id TEXT,kind TEXT,observed REAL,body TEXT,PRIMARY KEY(steam_id,kind));
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value REAL);''')

    def connect(self):
        fd=os.open(self.path,os.O_CREAT|os.O_WRONLY,0o600);os.close(fd)
        return sqlite3.connect(self.path,timeout=1)

    def validate(self,payload,now):
        if not isinstance(payload,dict) or set(payload)!={'steamIds','actor','requestedAt'}:
            raise ContractError(400,'invalid_steam_profiles_fields')
        validate_admin_actor(payload['actor'],payload['requestedAt'],self.engine.config,now)
        ids=payload['steamIds']
        if (not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not identity(i) for i in ids)
                or len(set(ids))!=len(ids)):raise ContractError(400,'invalid_steamIds')

    def query(self,payload,now=None):
        clock=time.time if now is None else lambda:now
        self.validate(payload,clock())
        if not self.lock.acquire(timeout=1):raise ContractError(503,'steam_profiles_busy')
        c=None
        try:
            self.validate(payload,clock());ids=payload['steamIds'];parts={i:{} for i in ids};started=clock()
            c=self.connect()
            for kind in METHODS:
                self.validate(payload,clock())
                missing=[]
                for sid in ids:
                    row=c.execute('SELECT observed,body FROM profiles WHERE steam_id=? AND kind=?',(sid,kind)).fetchone()
                    if row and 0<=clock()-row[0]<=CACHE_SECONDS:
                        parts[sid][kind]={'status':'ok','observedAt':row[0],'cacheHit':True,**json.loads(row[1])}
                    else:missing.append(sid)
                if not missing:continue
                cooldown=c.execute("SELECT value FROM metadata WHERE key='retry_until'").fetchone()
                code=None
                if cooldown and clock()<cooldown[0]:code='steam_rate_limited'
                elif clock()-started>10:code='steam_query_deadline'
                else:
                    try:
                        rows=parse_rows(kind,self.transport.fetch(kind,missing),missing)
                        fetched=clock()
                        self.validate(payload,fetched)
                        with c:
                            for sid in missing:
                                if sid in rows:
                                    c.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?)',(sid,kind,fetched,json.dumps(rows[sid],ensure_ascii=False)))
                                    parts[sid][kind]={'status':'ok','observedAt':fetched,'cacheHit':False,**rows[sid]}
                                else:
                                    parts[sid][kind]={'status':'not_found' if kind=='profile' else 'unavailable','observedAt':fetched,'cacheHit':False}
                            c.execute('DELETE FROM profiles WHERE observed<?',(fetched-86400,))
                            c.execute('DELETE FROM profiles WHERE rowid NOT IN (SELECT rowid FROM profiles ORDER BY observed DESC LIMIT 10000)')
                    except SteamFailure as error:
                        code=error.code
                        if code=='steam_api_not_configured':raise ContractError(503,code) from None
                        if code=='steam_rate_limited':
                            with c:c.execute("INSERT OR REPLACE INTO metadata VALUES('retry_until',?)",(clock()+(error.retry_after or 60),))
                if code:
                    for sid in missing:parts[sid][kind]={'status':'unavailable','observedAt':None,'cacheHit':False,'error':{'code':code}}
            self.validate(payload,clock())
            if not any(part['status'] in ('ok','not_found') for row in parts.values() for part in row.values()):
                raise ContractError(503,'steam_profiles_unavailable')
            return {'observedAt':clock(),'cacheTtlSeconds':CACHE_SECONDS,'source':'steam_web_api',
                    'complete':all(part['status']=='ok' for row in parts.values() for part in row.values()),
                    'players':[{'steamId':sid,'profileUrl':'https://steamcommunity.com/profiles/'+sid+'/',**parts[sid]} for sid in ids]}
        except (sqlite3.Error,OSError,ValueError,TypeError):
            raise ContractError(503,'steam_profiles_storage_unavailable') from None
        finally:
            if c is not None:c.close()
            self.lock.release()
