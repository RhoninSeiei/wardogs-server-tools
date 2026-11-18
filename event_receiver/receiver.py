"""Standalone authenticated WARDOGS feed archive; never issues game commands."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

if __package__:
    from . import kill_query, event_monitor, event_export
else:
    import kill_query, event_monitor, event_export

MAX_BODY = 1048576
PATHS = {'/wardogs', '/wardogs/api/ingest/events', '/api/ingest/events'}


class Archive:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS batches(hash TEXT PRIMARY KEY,received REAL NOT NULL,
            path TEXT NOT NULL,body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(server TEXT NOT NULL,event_key TEXT NOT NULL,
            received REAL NOT NULL,type TEXT,body TEXT NOT NULL,
            PRIMARY KEY(server,event_key));
          CREATE INDEX IF NOT EXISTS events_time ON events(received);
        ''')
        kill_query.initialize(self.db)
        event_monitor.initialize(self.db)
        event_export.initialize(self.db)

    def query_kills(self, params, incident_mode=False):
        return kill_query.query(self.path, params, incident_mode=incident_mode)

    def ingest(self, raw, path):
        doc = json.loads(raw)
        if not isinstance(doc, dict) or not isinstance(doc.get('events'), list):
            raise ValueError('expected events array')
        if len(doc['events']) > 2000 or any(not isinstance(e, dict) for e in doc['events']):
            raise ValueError('invalid events')
        server = doc.get('serverId', '')
        if not isinstance(server, str) or len(server) > 256:
            raise ValueError('invalid serverId')
        rows = []
        now = time.time()
        for event in doc['events']:
            body = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            key = event.get('eventId')
            if key is not None and (not isinstance(key, str) or not key or len(key) > 256):
                raise ValueError('invalid eventId')
            key = 'id:' + key if key else 'hash:' + hashlib.sha256(body.encode()).hexdigest()
            kind = event.get('type')
            if kind is not None and not isinstance(kind, str):
                raise ValueError('invalid type')
            rows.append((server, key, now, kind, body))
        canonical = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self.lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO batches VALUES(?,?,?,?)', (digest, now, path, canonical))
            count = 0
            for row in rows:
                inserted = self.db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)', row).rowcount
                count += inserted
                if inserted:
                    event_export.index_event(self.db, row[0], row[1])
                if inserted and row[3] == 'killed':
                    kill_query.index_event(self.db, row[0], row[1], row[4])
        return {'ok': True, 'accepted': len(rows), 'inserted': count}


def make_handler(archive, token, query_token=None, incident_queries=False):
    if query_token is not None and (not query_token or hmac.compare_digest(token, query_token)):
        raise ValueError('distinct query token required')
    if not token:
        raise ValueError('empty token')

    class Handler(BaseHTTPRequestHandler):
        # No request headers, URL query strings, tokens or bodies in access logs.
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def reply(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def authorized(self, expected):
            auth = self.headers.get_all('Authorization', [])
            return expected is not None and len(auth) == 1 and hmac.compare_digest(auth[0].encode(), ('Bearer ' + expected).encode())

        def do_GET(self):
            route = urlsplit(self.path)
            query_paths = ['/wardogs/v1/kills', '/wardogs/v1/events']
            if incident_queries:
                query_paths.append('/wardogs/v1/helicopter-incidents')
            if route.path in query_paths:
                if not self.authorized(query_token):
                    return self.reply(401, {'error': {'code': 'unauthorized'}})
                try:
                    if len(route.query) > 4096:
                        raise kill_query.QueryError(400, 'invalid_query')
                    params = parse_qs(route.query, keep_blank_values=True, strict_parsing=True, max_num_fields=10, errors='strict')
                    result = event_export.query(archive.path, params) if route.path.endswith('/events') else archive.query_kills(params, incident_mode=route.path.endswith('/helicopter-incidents'))
                except kill_query.QueryError as error:
                    return self.reply(error.status, error.body)
                except (ValueError, UnicodeError):
                    return self.reply(400, {'error': {'code': 'invalid_query'}})
                except (sqlite3.Error, OSError):
                    return self.reply(503, {'error': {'code': 'query_unavailable'}})
                return self.reply(200, result)
            self.reply(200 if self.path == '/healthz' else 404, {'ok': self.path == '/healthz'})

        def do_POST(self):
            path = urlsplit(self.path).path.rstrip('/')
            if path not in PATHS:
                return self.reply(404, {'error': 'not_found'})
            if not self.authorized(token):
                return self.reply(401, {'error': 'unauthorized'})
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                return self.reply(400, {'error': 'invalid_length'})
            length = int(lengths[0])
            if length > MAX_BODY:
                return self.reply(413, {'error': 'too_large'})
            if self.headers.get_content_type() != 'application/json':
                return self.reply(415, {'error': 'json_required'})
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self.reply(400, {'error': 'truncated_body'})
                result = archive.ingest(raw, path)
            except (ValueError, UnicodeError, RecursionError):
                return self.reply(400, {'error': 'invalid_payload'})
            except (sqlite3.Error, OSError):
                return self.reply(503, {'error': 'storage_unavailable'})
            self.reply(200, result)
    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


if __name__ == '__main__':
    os.umask(0o077)
    token = Path(os.environ.get('EVENT_TOKEN_FILE', '/run/secrets/event_token')).read_text().strip()
    query_token = Path(os.environ.get('EVENT_QUERY_TOKEN_FILE', '/run/secrets/event_query_token')).read_text().strip()
    archive = Archive(os.environ.get('EVENT_DB', '/data/events.sqlite3'))
    Server((os.environ.get('API_HOST', '127.0.0.1'), int(os.environ.get('API_PORT', '8080'))), make_handler(archive, token, query_token)).serve_forever()
