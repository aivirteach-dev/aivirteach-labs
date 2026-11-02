"""隔离环境装配：仅虚构学生与固定假提供方；复用现有Agent，不接平台或真实工具。

启动保持暂停。可信操作者通过本机Unix socket显式核对假提供方空闲后放行；
健康检查只读本地状态，学生HTTP没有恢复/迁移/任意文件或Shell入口。
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import socketserver
import threading
import urllib.request

from .agent_service import AgentService
from .agent_http import create_agent_server
from .contracts import CourseSnapshot
from .conversation import ConversationScope
from .management import ManagementView
from .management_http import create_management_server
from .operation_journal import OperationJournal
from .resources import ResourceManagedTutor
from .runtime import load_config,require_execution_identity
from .service_contracts import TrustedBinding,ServiceError
from .storage import SQLiteChatStore,StorageError
from .task_memory import migrate_task_memory,TaskRepository
from .lifecycle import migrate_lifecycle
from .inspection_memory import migrate_inspections
from .diagnostics import DiagnosticStore

ROOT=Path(__file__).resolve().parents[1]
FAKE_URL='http://fake-provider:18080/v1'
COURSE=CourseSnapshot(course_id='isolated-course',version='v1',lesson_id='lesson-1',
                     title='隔离测试虚构课程',instructions=['这是一份虚构资料，只用于验证服务接口和保存流程。'])


def read_json(path,maximum=16384):
    """仅读取可信操作者选定的有限本地配置；符号链接不作为配置入口。"""
    path=Path(path)
    if path.is_symlink():raise ValueError('unsafe_config')
    with path.open('rb') as stream:data=stream.read(maximum+1)
    if len(data)>maximum:raise ValueError('config_too_large')
    return json.loads(data)


def settings(path):
    value=read_json(path)
    if not isinstance(value,dict) or set(value)!={'version','students'} or type(value['version']) is not int or value['version']!=1 or not isinstance(value['students'],list) or len(value['students'])!=2:
        raise ValueError('invalid_isolated_settings')
    for index,row in enumerate(value['students']):
        if not isinstance(row,dict) or set(row)!={'id','token_sha256'} or row['id']!=('test-a','test-b')[index]:raise ValueError('invalid_test_student')
        if not isinstance(row['token_sha256'],str) or len(row['token_sha256'])!=64 or any(c not in '0123456789abcdef' for c in row['token_sha256']):
            raise ValueError('invalid_test_token_hash')
    if value['students'][0]['token_sha256']==value['students'][1]['token_sha256']:raise ValueError('duplicate_test_token')
    return value


def runtime_config():
    value=read_json(ROOT/'config/runtime.example.json')
    value.update(ollama_base_url=FAKE_URL,model='isolated-fake',execution_user='dsh-runner')
    value['total_timeout_seconds']=60;value['sdk_request_timeout_seconds']=45
    return value


def initialize(state,config):
    """只由显式init命令新建私有虚构库；拒绝覆盖/自动迁移既有数据。"""
    state=Path(state).absolute();rows=settings(config)['students']
    if state.is_symlink() or not state.is_dir() or any(state.iterdir()):raise ValueError('state_must_be_empty_private_directory')
    if state.stat().st_uid!=os.geteuid() or state.stat().st_mode&0o077:raise ValueError('state_permissions')
    store=SQLiteChatStore.create(state/'chat.db')
    # 仅对本命令刚创建的新虚构库显式建最新结构，常规serve绝不执行迁移。
    store.migrate_learning_memory();migrate_task_memory(store);migrate_lifecycle(store);migrate_inspections(store)
    for row in rows:
        handle=store.activate(ConversationScope(student_id=row['id'],learning_context_id='isolated-'+row['id'],
            course_id=COURSE.course_id,course_version=COURSE.version,lesson_id=COURSE.lesson_id))
        TaskRepository(store).ensure_lesson_task(handle)
    journal=OperationJournal.create(state/'operations.db',{'profile':'isolated_test'});journal.close()
    (state/'runs').mkdir(mode=0o700)
    for name,value in [('runtime.json',runtime_config()),('initialized.json',{'version':1,'config_sha256':hashlib.sha256(Path(config).read_bytes()).hexdigest()})]:
        fd=os.open(state/name,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as out:json.dump(value,out,ensure_ascii=False)


class IsolatedHost:
    def __init__(self,state,config,management_token,*,upstream_check=None):
        self.state=Path(state).absolute();rows=settings(config)['students']
        marker=read_json(self.state/'initialized.json')
        if marker!={'version':1,'config_sha256':hashlib.sha256(Path(config).read_bytes()).hexdigest()}:
            raise ValueError('initialized_config_changed')
        if self.state.is_symlink() or self.state.stat().st_uid!=os.geteuid() or self.state.stat().st_mode&0o077:
            raise ValueError('state_permissions')
        cfg=load_config(self.state/'runtime.json');require_execution_identity(cfg)
        if cfg!=load_config_value():raise ValueError('isolated_runtime_changed')
        self.store=SQLiteChatStore(self.state/'chat.db');self.bindings=[]
        with self.store._connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0]!=5:raise ValueError('isolated_schema_requires_explicit_setup')
        for row in rows:
            h=self.store.current(student_id=row['id'],learning_context_id='isolated-'+row['id'])
            if h is None:raise ValueError('test_scope_missing')
            tasks=TaskRepository(self.store).list(h)['tasks']
            if len(tasks)!=1:raise ValueError('isolated_task_missing')
            bound=TaskRepository(self.store).bind(h,tasks[0]['task_id'])
            self.bindings.append((row['token_sha256'],TrustedBinding(bound,h,COURSE)))
        self.tutor=ResourceManagedTutor(policy={'max_active':1,'max_waiting':4,'max_per_student':1,'min_free_bytes':64*1024*1024},
            config_path=self.state/'runtime.json',state_dir=self.state/'runs',diagnostics=DiagnosticStore(self.store))
        self.journal=OperationJournal(self.state/'operations.db',{'profile':'isolated_test'})
        self.service=AgentService(self.tutor,self.journal)
        self.management_token=management_token;self.upstream_check=upstream_check or self.fake_idle

    def resolve(self,token):
        digest=hashlib.sha256(token.encode()).hexdigest()
        for expected,binding in self.bindings:
            if secrets.compare_digest(expected,digest):return binding
        return None

    def readiness(self):
        # 不调用metrics()，避免健康轮询查询操作库；只投影内存状态。
        return {'ready':self.service._ready and not self.tutor.gate.snapshot()['paused'] and not self.service._closing}

    @staticmethod
    def fake_idle():
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(FAKE_URL.removesuffix('/v1')+'/stats',timeout=2) as response:
            raw=response.read(2049)
        if len(raw)>2048:raise ValueError('fake_probe_too_large')
        value=json.loads(raw)
        return value.get('kind')=='isolated_fixed_fake' and value.get('active')==0

    def verify(self):
        if self.upstream_check() is not True:raise ServiceError('upstream_not_idle',503)
        # 既有服务会继续拒绝未对账操作；不会自动重试或丢弃pending。
        self.service.verify_ready(upstream_idle=True)
        return {'status':'ready','scope':'isolated_fake_only'}

    def view(self):
        return ManagementView(self.state/'chat.db',self.state/'runtime.json',gate=self.tutor.gate.snapshot,
            operations=self.service.metrics,source='isolated_test:fixed-fake')


def load_config_value():
    """与load_config规范化后的预算一致，用于拒绝偷偷换成真实提供方。"""
    from .execution_budget import ExecutionLimits
    value=runtime_config();value['execution_limits']=ExecutionLimits.model_validate(value.get('execution_limits',{})).model_dump()
    return value


def control_server(host):
    path=host.state/'control.sock'
    if path.exists():
        if path.is_symlink() or not path.is_socket():raise ValueError('unsafe_control_path')
        # 操作库独占锁已由AgentService取得，旧控制服务不能与本实例共存。
        path.unlink()
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(3)
            try:
                # Docker exec操作者必须与容器运行身份相同，不向学生HTTP开放。
                import struct
                _,uid,_=struct.unpack('3i',self.request.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                if uid!=os.geteuid():raise ValueError('control_identity')
                line=self.rfile.readline(1025)
                if line!=b'{"command":"verify"}\n':raise ValueError('invalid_control')
                answer=host.verify()
            except Exception:answer={'status':'paused','error':'verification_or_reconciliation_required'}
            self.wfile.write(json.dumps(answer).encode()+b'\n')
    server=socketserver.UnixStreamServer(str(path),Handler);os.chmod(path,0o600)
    return server


def verify_command(state):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
        client.settimeout(5);client.connect(str(Path(state)/'control.sock'))
        client.sendall(b'{"command":"verify"}\n')
        with client.makefile('rb') as reader:data=reader.readline(2049)
        if len(data)>2048 or not data.endswith(b'\n'):raise ValueError('invalid_control_response')
    value=json.loads(data);print(json.dumps(value))
    return 0 if value.get('status')=='ready' else 2


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['initialize','serve','verify'])
    parser.add_argument('--state',type=Path,default=Path('/state'))
    parser.add_argument('--config',type=Path,default=Path('/run/config/host.json'))
    args=parser.parse_args()
    if args.command=='initialize':initialize(args.state,args.config);print('isolated_fake_state_initialized');return
    if args.command=='verify':raise SystemExit(verify_command(args.state))
    token_path=Path('/run/secrets/management_token')
    with token_path.open('rb') as stream:token=stream.read(130).decode('ascii').strip()
    if not 32<=len(token)<=128 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for c in token):
        raise ValueError('invalid_management_token')
    host=IsolatedHost(args.state,args.config,token)
    api=create_agent_server(host.service,host.resolve,18792,bind_host='0.0.0.0',health=host.readiness)
    manage=create_management_server(host.view(),token,18788,bind_host='0.0.0.0',isolated_test=True,allowed_hosts=('agent:18788',))
    control=control_server(host);servers=[api,manage,control];stop=threading.Event()
    for sig in [signal.SIGTERM,signal.SIGINT]:signal.signal(sig,lambda *_:stop.set())
    threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in servers]
    for t in threads:t.start()
    print('isolated_host_started_paused; explicit_verify_required',flush=True)
    try:stop.wait()
    finally:
        for s in servers:s.shutdown();s.server_close()
        for t in threads:t.join(2)
        if not host.service.close(timeout=10):
            print('shutdown_unconfirmed; reconcile_before_reuse',flush=True)
            raise SystemExit(2)


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,ServiceError,StorageError):
        print('isolated_startup_or_control_rejected; inspect_private_configuration',flush=True)
        raise SystemExit(2)
