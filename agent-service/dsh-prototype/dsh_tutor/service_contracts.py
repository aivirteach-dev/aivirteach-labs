"""独立Agent API v1契约；身份/教材由可信宿主解析，不从学生正文取得。

协议为异步提交+有界进度查询；原平台同步diagnose不是本接口的直接替换品。
"""
from dataclasses import dataclass
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
from .contracts import CourseSnapshot,CourseAnswer
from .storage import ScopeHandle

API_VERSION='dsh-tutor.v1'
REQUEST_ID=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'


class ServiceError(RuntimeError):
    def __init__(self,code,status=409):self.code=code;self.status=status;super().__init__(code)


class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)


class HttpBudgetSettings(StrictModel):
    """每类已认证请求的固定窗口限额；有界鉴权线程另保留类别配额。"""
    submit_per_second:int=Field(default=10,ge=1,le=100)
    query_per_second:int=Field(default=40,ge=1,le=200)
    cancel_per_second:int=Field(default=10,ge=1,le=100)
    resolver_timeout_seconds:float=Field(default=.5,gt=0,le=2)


class ServiceSettings(StrictModel):
    request_issued_floor:int=Field(default=0,ge=0)
    http_budget:HttpBudgetSettings=Field(default_factory=HttpBudgetSettings)
    max_operations:int=Field(default=1000,ge=1,le=10000)
    max_journal_bytes:int=Field(default=16*1024*1024,ge=65536,le=64*1024*1024)
    max_result_bytes:int=Field(default=65536,ge=1024,le=65536)
    max_inflight:int=Field(default=5,ge=1,le=32)
    operation_timeout_seconds:float=Field(default=240.0,gt=0,le=600)
    database_timeout_seconds:float=Field(default=.1,gt=0,le=1)
    max_body_bytes:int=Field(default=32768,ge=1024,le=65536)
    events_per_operation:int=Field(default=24,ge=16,le=32)
    endpoint_timeout_seconds:float=Field(default=3.0,gt=0,le=10)
    profile:Literal['simulation','isolated_test']='simulation'


class HistorySelection(StrictModel):
    mode:Literal['recent','earlier','none']='recent'
    before:int | None=Field(default=None,ge=1)
    @model_validator(mode='after')
    def check_mode(self):
        if self.before is not None and self.mode!='earlier':raise ValueError('earlier_required')
        return self


class Submission(StrictModel):
    api_version:Literal['dsh-tutor.v1']
    profile:Literal['course_snapshot','aivirteach','course_actions_v1'] = 'course_snapshot'
    payload:dict
    request_window_id:str | None=Field(default=None,pattern=r'^[0-9a-f]{64}$')
    history_selection:HistorySelection=Field(default_factory=HistorySelection)


@dataclass(frozen=True)
class TrustedBinding:
    """由平台宿主/认证适配器建立；不能反序列化HTTP请求创建它。

    store必须是当前任务的ChatStore，course是服务端权威快照；每次API调用
    都重查当前handle，课程文本篡改也会拒绝。lab_id不等同VM实例UUID。
    """
    store:object
    handle:ScopeHandle
    course:CourseSnapshot
    lab_id:str | None=None
    tool_bridge:dict | None=None
    material_repository:object | None=None
    service_executor:object | None=None
    request_window:object | None=None


class TokenUsage(StrictModel):
    state:Literal['known','unknown']='unknown'
    input_cache_hit_tokens:int | None=Field(default=None,ge=0)
    input_cache_miss_tokens:int | None=Field(default=None,ge=0)
    output_tokens:int | None=Field(default=None,ge=0)
    @model_validator(mode='after')
    def coherent(self):
        values=(self.input_cache_hit_tokens,self.input_cache_miss_tokens,self.output_tokens)
        if self.state=='known' and any(x is None for x in values):raise ValueError('incomplete_usage')
        if self.state=='unknown' and any(x is not None for x in values):raise ValueError('unknown_not_zero')
        return self


class PersistenceState(StrictModel):
    agent_chat:Literal['saved']='saved'
    operation_record:Literal['saved']='saved'
    platform:Literal['unconfirmed']='unconfirmed'


class OperationResult(StrictModel):
    answer:str=Field(min_length=1)
    answer_status:Literal['completed','partial']
    run_id:str | None=Field(default=None,max_length=128)
    replayed:bool
    detail:CourseAnswer | None
    usage:TokenUsage
    evidence_scope:Literal['original_run_only']='original_run_only'
    persistence:PersistenceState


class OperationEvent(StrictModel):
    sequence:int=Field(ge=1,lt=2**63)
    kind:Literal['accepted','stage','cancel_requested','succeeded','failed','recovery_required','interrupted','reconciled','redacted']
    stage:str=Field(min_length=1,max_length=64)
    code:str | None=Field(default=None,max_length=128)
    created_at:str=Field(max_length=64)


class EventPage(StrictModel):
    items:list[OperationEvent]=Field(max_length=32)
    next_after:int=Field(ge=0,lt=2**63)
    reset_required:bool


class OperationView(StrictModel):
    """完成是交付状态；answer_status单独说明教学/证据结果是否受限。"""
    api_version:Literal['dsh-tutor.v1']
    operation_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    request_id:str=Field(pattern=REQUEST_ID)
    state:Literal['queued','running','succeeded','failed','recovery_required']
    stage:str=Field(min_length=1,max_length=64)
    error_code:str | None=Field(default=None,max_length=128)
    result:OperationResult | None
    cancel_requested:bool
    execution_cancellation:Literal['not_requested','requested','not_dispatched','late_success','outcome_unknown']='not_requested'
    cancellation:Literal['completed_after_cancel','requested_unconfirmed','not_running','not_requested']
    deadline_exceeded:bool
    feedback:dict=Field(default_factory=dict)
    events:EventPage
    created_at:str=Field(max_length=64)
    updated_at:str=Field(max_length=64)
    retry_policy:Literal['reuse_same_request_id; no_automatic_inference_retry']
    source:Literal['simulation','isolated_test']
    @model_validator(mode='after')
    def consistent(self):
        if (self.state=='succeeded')!=(self.result is not None):raise ValueError('inconsistent_delivery')
        return self


def load_service_settings(path):
    """读取已选定的配置文件；不探测网络、不创建数据库或安装依赖。"""
    import json
    from pathlib import Path
    with Path(path).open('rb') as stream:raw=stream.read(16385)
    if len(raw)>16384:raise ValueError('settings_too_large')
    return ServiceSettings.model_validate(json.loads(raw))
