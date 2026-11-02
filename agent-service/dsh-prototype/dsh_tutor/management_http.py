"""管理员只读HTTP适配；默认loopback，隔离容器绑定须宿主显式授权。"""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
from collections import deque
import secrets
import threading
import time
import json
from .management import ManagementError


def create_management_server(view,token,port=8788,*,bind_host='127.0.0.1',isolated_test=False,allowed_hosts=()):
    if not isinstance(token,str) or len(token)<32:raise ValueError('weak_token')
    if bind_host not in ('127.0.0.1','0.0.0.0') or ((bind_host!='127.0.0.1' or allowed_hosts) and not isolated_test):
        raise ValueError('isolated_bind_required')
    hosts=(f'127.0.0.1:{port}',f'localhost:{port}')+tuple(allowed_hosts)
    if any(not isinstance(x,str) or len(x)>128 or any(c in x for c in '/@?#\r\n') for x in hosts):
        raise ValueError('invalid_management_host')
    times=deque(maxlen=40);lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass  # 不记录Authorization、URL查询或原异常。
        def send(self,status,data):
            body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body)
        def do_GET(self):
            actual_hosts=(f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')+tuple(allowed_hosts)
            if self.headers.get_all('Host') not in ([h] for h in actual_hosts):
                return self.send(403,{'error':'host_denied'})
            auth=self.headers.get_all('Authorization') or []
            if self.headers.get('Origin') or len(auth)!=1 or not secrets.compare_digest(auth[0],'Bearer '+token):
                return self.send(401,{'error':'unauthorized'})
            with lock:
                at=time.monotonic()
                while times and times[0]<at-1:times.popleft()
                if len(times)>=20:return self.send(429,{'error':'rate_limited'})
                times.append(at)
            try:
                if len(self.path)>2048:raise ManagementError('invalid_query')
                url=urlsplit(self.path);raw=parse_qs(url.query,keep_blank_values=True)
                if any(len(v)!=1 for v in raw.values()):raise ManagementError('invalid_query')
                params={k:v[0] for k,v in raw.items()}
                if url.path=='/v1/management/events':result=view.query(params)
                elif url.path.startswith('/v1/management/events/'):
                    if params:raise ManagementError('invalid_query')
                    try:sequence=int(url.path.rsplit('/',1)[1])
                    except ValueError:raise ManagementError('invalid_query') from None
                    if not 1<=sequence<2**63-1:raise ManagementError('invalid_query')
                    page=view.query({'before':str(sequence+1),'limit':'1'})
                    if not page['items'] or page['items'][0]['sequence']!=sequence:return self.send(404,{'error':'event_unavailable'})
                    result={'item':page['items'][0],'source':page['source'],'collected_at':page['collected_at']}
                elif url.path in ('/v1/management/overview','/v1/management/config'):
                    if params:raise ManagementError('invalid_query')
                    result=view.overview() if url.path.endswith('overview') else view.configuration()
                else:return self.send(404,{'error':'not_found'})
                return self.send(200,result)
            except ManagementError as e:return self.send(400 if str(e)=='invalid_query' else 503,{'error':str(e)})
            except Exception:return self.send(503,{'error':'management_unavailable'})
        def do_POST(self):self.send(405,{'error':'read_only'})
        do_PUT=do_DELETE=do_PATCH=do_POST
    class Server(ThreadingHTTPServer):
        daemon_threads=True
        # 限制同时处理的连接，防止管理员刷新堆积无限线程。
        def __init__(self,*args):self.slots=threading.BoundedSemaphore(8);super().__init__(*args)
        def process_request(self,request,address):
            if not self.slots.acquire(False):self.shutdown_request(request);return
            try:super().process_request(request,address)
            except BaseException:self.slots.release();raise
        def process_request_thread(self,request,address):
            request.settimeout(3)
            try:super().process_request_thread(request,address)
            finally:self.slots.release()
    return Server((bind_host,port),Handler)
