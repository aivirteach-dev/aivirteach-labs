"""独立Agent v1本地传输：宿主鉴权绑定、短提交与有界轮询。

只监听回环地址；生产TLS、登录、平台JWT验证须由部署宿主接入。
认证解析器返回TrustedBinding，绝不信任学生正文中的身份或文件路径。
"""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
import json,re,threading,time
from .service_contracts import ServiceError,TrustedBinding,API_VERSION
from .http_budget import HttpBudget,request_kind


def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate_key')
        result[key]=value
    return result


def create_agent_server(service,resolve_token,port=8792):
    """resolve_token由可信宿主注入；失败关闭，异常文本和令牌不入响应。"""
    budget=HttpBudget(service.settings.http_budget)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,data):
            body=json.dumps(data,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('Connection','close')
            if status==429:self.send_header('Retry-After','1')
            self.end_headers();self.wfile.write(body);self.close_connection=True

        def binding(self):
            if self.headers.get_all('Host') not in ([f'127.0.0.1:{self.server.server_port}'],[f'localhost:{self.server.server_port}']):
                raise ServiceError('host_denied',403)
            values=self.headers.get_all('Authorization') or []
            if self.headers.get('Origin') or len(values)!=1 or not values[0].startswith('Bearer '):
                raise ServiceError('unauthorized',401)
            token=values[0][7:]
            if not 32<=len(token)<=1024:raise ServiceError('unauthorized',401)
            kind=request_kind(self.command,self.path)
            budget.check(kind)
            binding=budget.resolve(resolve_token,token,kind)
            if not isinstance(binding,TrustedBinding):raise ServiceError('unauthorized',401)
            budget.check(kind,authenticated=True)
            service._check_binding(binding)
            return binding

        def body(self):
            if self.headers.get('Transfer-Encoding'):raise ServiceError('invalid_body',400)
            lengths=self.headers.get_all('Content-Length') or []
            if len(lengths)!=1 or not re.fullmatch(r'[0-9]{1,8}',lengths[0]):raise ServiceError('invalid_body',400)
            count=int(lengths[0])
            if count>service.settings.max_body_bytes:raise ServiceError('body_too_large',413)
            if self.headers.get('Content-Type','').split(';',1)[0].strip().lower()!='application/json':
                raise ServiceError('invalid_content_type',415)
            raw=self.rfile.read(count)
            if len(raw)!=count:raise ServiceError('invalid_body',400)
            try:
                value=json.loads(raw.decode('utf-8'),object_pairs_hook=_object,
                    parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite')))
                if not isinstance(value,dict):raise ValueError()
                return value
            except (ValueError,RecursionError):raise ServiceError('invalid_json',400) from None

        def route(self):
            binding=self.binding()
            if len(self.path)>512:raise ServiceError('invalid_query',400)
            url=urlsplit(self.path);params=parse_qs(url.query,keep_blank_values=True)
            if any(len(v)!=1 for v in params.values()):raise ServiceError('invalid_query',400)
            if self.command=='GET' and url.path=='/v1/tutor/capabilities' and not params:
                return 200,service.capabilities()
            if self.command=='POST' and url.path=='/v1/tutor/operations' and not params:
                result,created=service.submit(binding,self.body());return (202 if created else 200),result
            match=re.fullmatch(r'/v1/tutor/operations/([a-f0-9]{32})(/cancel)?',url.path)
            if match:
                operation_id,cancel=match.groups()
                if self.command=='POST' and cancel and not params:
                    if self.body():raise ServiceError('invalid_body',400)
                    return 200,service.cancel(binding,operation_id)
                if self.command=='GET' and not cancel and set(params)<= {'after'}:
                    cursor=params.get('after',['0'])[0]
                    if not re.fullmatch(r'[0-9]{1,19}',cursor):raise ServiceError('invalid_cursor',400)
                    return 200,service.view(binding,operation_id,after=int(cursor))
            raise ServiceError('not_found',404)

        def handle_api(self):
            try:status,data=self.route()
            except ServiceError as exc:status,data=exc.status,{'api_version':API_VERSION,'error':exc.code}
            except (TimeoutError,OSError):return
            except Exception:status,data=503,{'api_version':API_VERSION,'error':'service_unavailable'}
            try:self.send(status,data)
            except (OSError,TimeoutError):pass  # 客户端断线不取消/重跑后台操作。
        do_GET=do_POST=handle_api
        def do_PUT(self):self.send(405,{'error':'method_not_allowed'})
        do_DELETE=do_PATCH=do_PUT

    class Server(ThreadingHTTPServer):
        daemon_threads=True;request_queue_size=8
        def __init__(self,*args):
            self.slots=threading.BoundedSemaphore(8)
            # 同步读取有限请求行后分配类别，读连接最多4，保留提交/取消连接。
            self.category_slots={k:threading.BoundedSemaphore(n) for k,n in [('query',4),('submit',2),('cancel',2),('other',1)]}
            super().__init__(*args)
        def process_request(self,request,address):
            if not self.slots.acquire(False):self.shutdown_request(request);return
            try:super().process_request(request,address)
            except BaseException:self.slots.release();raise
        def process_request_thread(self,request,address):
            request.settimeout(service.settings.endpoint_timeout_seconds)
            category=None
            try:
                # MSG_PEEK不消耗HTTP请求，超长/未完成行归入有限other类别。
                import socket
                first=request.recv(1024,socket.MSG_PEEK).split(b'\r\n',1)[0].split(b' ')
                kind=request_kind(first[0].decode('ascii'),first[1].decode('ascii')) if len(first)>=2 else 'other'
                category=self.category_slots[kind]
                if not category.acquire(False):category=None;self.shutdown_request(request);return
                super().process_request_thread(request,address)
            except (OSError,UnicodeError):self.shutdown_request(request)
            finally:
                if category is not None:category.release()
                self.slots.release()
    return Server(('127.0.0.1',port),Handler)
