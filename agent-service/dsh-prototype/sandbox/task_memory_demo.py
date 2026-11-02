"""虚构学生记忆管理演示：仅回环监听，独立SQLite，无模型/真实平台/系统工具。

python sandbox/task_memory_demo.py --port 8790
不提供生产登录功能；固定虚构身份在服务端绑定，不能从HTTP提交另一学生身份。
"""
import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dsh_tutor.conversation import ConversationScope
from dsh_tutor.storage import SQLiteChatStore,StorageError
from dsh_tutor.task_memory import TaskRepository,migrate_task_memory
from dsh_tutor.task_memory_api import TaskMemoryAPI
from dsh_tutor.lifecycle import migrate_lifecycle


def create_server(store,handle,*,port=8790):
    """测试可传port=0取得临时端口；令牌只进入本机HTML，不写入URL或日志。"""
    service=TaskMemoryAPI(TaskRepository(store));token=secrets.token_urlsafe(32)
    template=(ROOT/'sandbox/task_memory_demo.html').read_text(encoding='utf-8')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def send(self,status,body,content_type='application/json; charset=utf-8'):
            if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type',content_type)
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'nonce-"+token+"'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)

        def valid_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')

        def do_GET(self):
            if not self.valid_host():self.send(403,{'error':'host_denied'});return
            if self.path!='/':self.send(404,{'error':'not_found'});return
            page=template.replace('__TOKEN_JSON__',json.dumps(token)).replace('__NONCE__',token)
            self.send(200,page.encode(),'text/html; charset=utf-8')

        def do_POST(self):
            if self.path!='/api':self.send(404,{'error':'not_found'});return
            origin=self.headers.get('Origin')
            permitted=(f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}')
            supplied=self.headers.get('X-Demo-Token','')
            if not self.valid_host() or (origin is not None and origin not in permitted) or not secrets.compare_digest(supplied.encode('utf-8'),token.encode('ascii')):
                self.send(403,{'error':'request_denied'});return
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                self.send(415,{'error':'json_required'});return
            try:
                self.connection.settimeout(5)
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=8192:raise ValueError()
                payload=json.loads(self.rfile.read(size))
            except (ValueError,UnicodeError,TimeoutError):self.send(400,{'error':'invalid_input'});return
            try:self.send(200,service.execute(handle,payload))
            except StorageError as exc:
                status=409 if exc.code in ('revision_conflict','request_conflict','preview_changed','task_busy','operation_conflict') else 403 if exc.code in ('scope_denied','task_denied','stale_scope') else 400
                self.send(status,{'error':exc.code})

    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True
    return server


def create_demo_store():
    """只初始化固定sandbox虚构库，不接受任意数据库路径。旧正式库绝不在此迁移。"""
    directory=ROOT/'sandbox/results/task-memory-demo';directory.mkdir(parents=True,exist_ok=True)
    path=directory/'fictional.db'
    store=SQLiteChatStore.create(path);store.migrate_learning_memory();migrate_task_memory(store);migrate_lifecycle(store)
    scope=ConversationScope(student_id='fictional-student',learning_context_id='fictional-learning',
                            course_id='fictional-ai-course',course_version='demo-v1',lesson_id='demo-lesson')
    handle=store.activate(scope)
    return store,handle


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='虚构记忆管理页面，无模型请求')
    parser.add_argument('--port',type=int,default=8790)
    parser.add_argument('--enable-lifecycle',action='store_true',help='显式升级本演示虚构库至v4，非任意数据库迁移')
    args=parser.parse_args()
    # 首次建立学习范围；再次启动恢复原范围，不擅自创建新学生或迁移未知库。
    directory=ROOT/'sandbox/results/task-memory-demo';directory.mkdir(parents=True,exist_ok=True)
    path=directory/'fictional.db'
    if path.exists():
        store=SQLiteChatStore(path);handle=store.current(student_id='fictional-student',learning_context_id='fictional-learning')
    else:store,handle=create_demo_store()
    if args.enable_lifecycle:migrate_lifecycle(store)
    repo=TaskRepository(store)
    repo.create(handle,'news','搭建新闻简报');repo.create(handle,'mail','配置邮件发送')
    server=create_server(store,handle,port=args.port)
    print(f'虚构记忆管理：http://127.0.0.1:{server.server_port}；零模型调用；Ctrl+C停止。',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
