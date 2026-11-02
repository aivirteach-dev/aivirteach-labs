"""单个问答进程的只读状态桥；不是跨进程调度器，也不是持久任务队列。

宿主显式把正在处理问答的gate交给Publisher。管理进程读固定文件，有时效、
大小、字段和单写者限制；没有文件/心跳过期不能显示成空闲。无模型探测。
"""
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
import json
import os
import stat
import tempfile

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat
from typing import Literal


SOURCES={'local','simulation:scheduling-demo','simulation:scheduling-acceptance','simulation:preparation-acceptance'}


class _Activity(BaseModel):
    model_config=ConfigDict(extra='ignore',strict=True)
    run_ref:str=Field(pattern=r'^[0-9a-f]{32}$')
    request_ref:str | None=Field(default=None,pattern=r'^[0-9a-f]{24}$')
    stage:Literal['waiting','input','accept','history','prepare','preparation_wait','execution_wait','revalidate','model','result','save','replay','worker_start','dsh_startup','mcp_discovery','model_request','tool_execution','runtime_finish','direct_action','completion_wait','delivery_save']
    seconds:FiniteFloat=Field(ge=0)
    stage_seconds:FiniteFloat=Field(ge=0)
    cancel_requested:bool=False


class _Limits(BaseModel):
    model_config=ConfigDict(extra='ignore',strict=True)
    max_active:int=Field(ge=1,le=32)
    validated_max_active:int=Field(ge=1,le=32)
    max_waiting:int=Field(ge=0,le=128)
    max_per_student:int=Field(ge=1,le=8)
    queue_timeout_seconds:FiniteFloat=Field(gt=0,le=60)
    max_finishing:int=Field(default=2,ge=1,le=16)
    finishing_queue_timeout_seconds:FiniteFloat=Field(default=10,gt=0,le=60)
    max_preparing:int=Field(default=2,ge=1,le=8)
    preparation_queue_timeout_seconds:FiniteFloat=Field(default=5,gt=0,le=60)
    preparation_timeout_seconds:FiniteFloat=Field(default=3,gt=0,le=30)
    revalidation_timeout_seconds:FiniteFloat=Field(default=2,gt=0,le=30)


class _Recent(_Activity):
    outcome:Literal['finished','saved','replayed','in_progress','failed','not_saved','scope_changed','rejected','interrupted']
    error_code:str | None=Field(default=None,max_length=64)


class _Stats(BaseModel):
    model_config=ConfigDict(extra='ignore',strict=True)
    samples:int=Field(ge=0,le=64)
    mean:FiniteFloat | None=Field(default=None,ge=0)
    p95:FiniteFloat | None=Field(default=None,ge=0)


class _Execution(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    samples:int=Field(ge=0,le=64)
    model_calls:int=Field(ge=0,le=1024)
    budget_stops:int=Field(ge=0,le=64)
    input_estimated_tokens:int=Field(ge=0)
    output_reserved_tokens:int=Field(ge=0)
    actual_usage:Literal['unknown']
    hardware:Literal['unknown']
    timings:dict[str,_Stats]=Field(default_factory=dict,max_length=12)
    scope:str=Field(max_length=200)


class _Queue(BaseModel):
    model_config=ConfigDict(extra='ignore',strict=True)
    active:int=Field(ge=0,le=32)
    waiting:int=Field(ge=0,le=160)
    inflight:int | None=Field(default=None,ge=0,le=160)
    max_inflight:int | None=Field(default=None,ge=1,le=160)
    finishing_reserved:int=Field(default=0,ge=0,le=160)
    finishing_waiting:int=Field(default=0,ge=0,le=160)
    finishing_waiting_requests:list[_Activity]=Field(default_factory=list,max_length=160)
    finishing_requests:list[_Activity]=Field(default_factory=list,max_length=160)
    maintenance:bool=False
    preparing:int=Field(default=0,ge=0,le=8)
    preparation_waiting:int=Field(default=0,ge=0,le=160)
    paused:bool
    closed:bool
    instance_id:str | None=Field(default=None,pattern=r'^[0-9a-f]{32}$')
    policy_revision:int | None=Field(default=None,ge=1)
    policy:_Limits | None=None
    running:list[_Activity]=Field(default_factory=list,max_length=32)
    waiting_requests:list[_Activity]=Field(default_factory=list,max_length=160)
    preparing_requests:list[_Activity]=Field(default_factory=list,max_length=8)
    preparation_waiting_requests:list[_Activity]=Field(default_factory=list,max_length=160)
    recent_results:list[_Recent]=Field(default_factory=list,max_length=32)
    oldest_waiting_seconds:FiniteFloat=Field(default=0,ge=0)
    queue_seconds:_Stats | None=None
    application_seconds:_Stats | None=None
    preparation_seconds:_Stats | None=None
    preparation_queue_seconds:_Stats | None=None
    counts:dict[str,int]=Field(default_factory=dict,max_length=32)
    execution:_Execution | None=None


COUNTS={'finishing_queue_timeout','maintenance_busy','received','queued','admitted','finished','task_exception','policy_updates','closed','paused','preparation_queue_timeout',
        'cancelled','student_limit','queue_full','queue_timeout','invalid_input','invalid_identity',
        'result_saved','result_replayed','result_in_progress','result_failed','result_not_saved',
        'result_scope_changed','result_disk_low','result_disk_unavailable'}


def queue_projection(snapshot):
    """同进程和文件读取都使用同一白名单；不信任文件中的自报权限/模型健康。"""
    clean=_Queue.model_validate(snapshot).model_dump(exclude_none=True)
    if clean.get('execution'):
        from .execution_budget import MEASUREMENTS
        if set(clean['execution']['timings'])-MEASUREMENTS:raise ValueError('invalid_execution_timings')
    clean['counts']={k:v for k,v in clean['counts'].items() if k in COUNTS and type(v) is int and 0<=v<2**63}
    from .diagnostics import ERRORS
    for item in clean['recent_results']:
        code=item.get('error_code')
        if code and code not in ERRORS|COUNTS|{'disk_low','disk_unavailable'}:item['error_code']='unclassified'
    return clean


class SchedulerStatusPublisher:
    def __init__(self,gate,path,*,source='local',interval=.5):
        if source not in SOURCES or not .05<=interval<=2:raise ValueError('invalid_status_config')
        self.gate=gate;self.path=Path(path).absolute();self.source=source;self.interval=interval
        self._stop=Event();self._thread=None;self._owner=None

    def _write(self,state='running'):
        payload={'version':1,'publisher':state,'source':self.source,
                 'collected_at':datetime.now(timezone.utc).isoformat(),'queue':queue_projection(self.gate.snapshot())}
        raw=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()
        if len(raw)>65536:raise ValueError('status_too_large')
        name=None
        try:
            # 原子替换避免读到半截JSON；文件只含脱敏调度元数据。
            with tempfile.NamedTemporaryFile(dir=self.path.parent,prefix='.scheduler-',delete=False) as stream:
                name=stream.name;stream.write(raw)
            os.replace(name,self.path);name=None
        finally:
            if name is not None:Path(name).unlink(missing_ok=True)

    def start(self):
        if self._thread is not None or self._owner is not None:raise ValueError('publisher_already_started')
        import fcntl
        self.path.parent.mkdir(parents=True,exist_ok=True)
        # 同一个状态文件只允许一个发布者；第二个不能覆盖第一个的状态。
        fd=os.open(str(self.path)+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        owner=os.fdopen(fd,'a')
        try:
            fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB);self._owner=owner;self._write()
        except BaseException:
            self._owner=None;owner.close();raise
        def publish():
            while not self._stop.wait(self.interval):
                try:self._write()
                except (OSError,ValueError):pass  # 写失败后心跳自然过期，不能刷新虚假健康状态。
        self._thread=Thread(target=publish,name='scheduler-status',daemon=True);self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3)
            if self._thread.is_alive():raise RuntimeError('publisher_stop_pending')
        if self._owner is not None:
            try:self._write('stopped')
            finally:self._owner.close();self._owner=None

    def __enter__(self):return self.start()
    def __exit__(self,*args):self.close()


class SchedulerStatusReader:
    def __init__(self,path,*,max_age=5):
        if not .1<=max_age<=30:raise ValueError('invalid_max_age')
        self.path=Path(path).absolute();self.max_age=max_age

    def __call__(self):
        try:
            # 固定可信路径，不接受浏览器指定；拒绝链接和超大/不完整文件。
            with os.fdopen(os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise ValueError()
                raw=stream.read(65537)
            if len(raw)>65536:raise ValueError()
            data=json.loads(raw)
            if type(data.get('version')) is not int or data['version']!=1 or data.get('source') not in SOURCES:raise ValueError()
            at=datetime.fromisoformat(data['collected_at'])
            if at.tzinfo is None:raise ValueError()
            age=(datetime.now(timezone.utc)-at).total_seconds()
            if age< -2 or age>self.max_age or data.get('publisher')!='running':
                return {'state':'unknown','reason':'status_stale_or_stopped'}
            result=queue_projection(data['queue'])
            return {'state':'available',**result,'source':data['source'],'collected_at':at.isoformat(),
                    'age_seconds':round(max(0,age),3),'scope':'一个问答实例；跨进程只读采集，不是全局限流'}
        except FileNotFoundError:return {'state':'not_connected'}
        except (OSError,ValueError,TypeError,KeyError):return {'state':'unknown','reason':'status_unavailable'}
