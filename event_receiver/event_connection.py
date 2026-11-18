"""Verified TLS to our event origin; preserve public Host/SNI and reuse connections."""
import http.client
import io
import json
import os
import socket
import ssl
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode

HOST=os.environ.get('EVENT_EXPORT_HOST', 'events.example.invalid')
ORIGIN=os.environ.get('EVENT_EXPORT_ORIGIN', HOST)


class OriginHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self,host,origin,timeout=5):
        self.origin=origin
        self.tls_context=ssl.create_default_context()
        super().__init__(host,timeout=timeout,context=self.tls_context)

    def connect(self):
        raw=socket.create_connection((self.origin,self.port),self.timeout,self.source_address)
        try:self.sock=self.tls_context.wrap_socket(raw,server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class EventClient:
    def __init__(self,key,connection_factory=OriginHTTPSConnection):
        self.key=key;self.connection_factory=connection_factory;self.connection=None;self.lock=threading.Lock()

    def close(self):
        if self.connection is not None:self.connection.close();self.connection=None

    def get(self,params):
        path='/wardogs/v1/events?'+urlencode(params)
        with self.lock:
            try:
                if self.connection is None:self.connection=self.connection_factory(HOST,ORIGIN,timeout=5)
                self.connection.request('GET',path,headers={'Authorization':'Bearer '+self.key,
                    'Accept':'application/json','User-Agent':'WardogsQueryClient/1.1'})
                response=self.connection.getresponse()
                raw=response.read(4194305)
                if len(raw)>4194304:raise ValueError('response_too_large')
                if not 200<=response.status<300:
                    raise HTTPError('https://'+HOST+path,response.status,response.reason,response.headers,io.BytesIO(raw))
                if response.will_close:self.close()
                return json.loads(raw)
            except Exception:
                self.close()
                raise
