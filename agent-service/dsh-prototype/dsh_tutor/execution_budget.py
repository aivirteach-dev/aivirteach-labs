"""请求级执行预算与脱敏运行指标；计数上限与Token估算明确分开。"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class ExecutionLimits(BaseModel):
    """可信配置，不从学生输入或模型工具参数读取。输出按每次上限预留。"""
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    max_model_calls:int=Field(default=4,ge=1,le=16)
    max_total_output_tokens:int=Field(default=2048,ge=1,le=65536)
    max_input_estimated_tokens_per_call:int=Field(default=6000,ge=128,le=131072)
    max_total_input_estimated_tokens:int=Field(default=24000,ge=128,le=1048576)
    context_headroom_tokens:int=Field(default=512,ge=128,le=8192)
    max_request_bytes:int=Field(default=65536,ge=2048,le=262144)
    max_output_bytes:int=Field(default=32768,ge=128,le=262144)
    max_runtime_events:int=Field(default=128,ge=32,le=256)
    max_stream_chunks:int=Field(default=8192,ge=16,le=16384)


RUNTIME_STAGES={'worker_start','dsh_startup','mcp_discovery','model_request','tool_execution','runtime_finish'}
MEASUREMENTS={'worker_boot','dsh_startup','mcp_discovery','model_stream','tool_execution','first_visible','runtime_total','chat_save','result_validation'}


class RuntimeMetrics(BaseModel):
    """仅数值/有限枚举，不带提示词、学生、文件路径或远端原始异常。"""
    model_config=ConfigDict(extra='forbid',strict=True)
    version:Literal[1]=1
    source:Literal['installed_dsh_guard']='installed_dsh_guard'
    model_calls:int=Field(ge=0,le=16)
    input_estimated_tokens:int=Field(ge=0,le=2097152)
    output_reserved_tokens:int=Field(ge=0,le=65536)
    output_bytes:int=Field(ge=0,le=524288)
    budget_stop:Literal['model_calls','input_budget','output_reservation','output_bytes','request_bytes','deadline','route','missing_output_cap','tool_set','stream_chunks'] | None=None
    unconfirmed_calls:int=Field(default=0,ge=0,le=16)
    timings:dict[str,FiniteFloat]=Field(default_factory=dict,max_length=12)
    actual_usage:Literal['unknown']='unknown'
    estimation:Literal['ceil_utf8_bytes_div_2_not_tokenizer']='ceil_utf8_bytes_div_2_not_tokenizer'

    def safe(self):
        if set(self.timings)-MEASUREMENTS or any(not 0<=v<=7200 for v in self.timings.values()):
            raise ValueError('invalid_runtime_timings')
        return self.model_dump()


class RuntimeFailure(RuntimeError):
    """跨进程仅传稳定原因。预算后安全终态与上游不明故障不能混淆。"""
    def __init__(self,code='run_failed',metrics=None,*,known_end=False):
        self.code=code;self.metrics=metrics;self.known_end=known_end or code=='model_budget_exhausted'
        super().__init__('DSH 运行失败：'+code)


_observer=ContextVar('tutor_runtime_observer',default=None)


@contextmanager
def observe_runtime(callback):
    """线程/请求局部进度；应用同步调用链复用，不修改模型问答接口。"""
    token=_observer.set(callback)
    try:yield
    finally:_observer.reset(token)


def runtime_stage(stage):
    if stage not in RUNTIME_STAGES:raise ValueError('invalid_runtime_stage')
    callback=_observer.get()
    if callback is not None:callback(stage)


def execution_projection(samples):
    """最多64轮，统计窗口而非累计计费；不拿未知用量填0。"""
    rows=list(samples)[-64:]
    def stats(values):
        values=sorted(values)
        return {'samples':len(values),'mean':round(sum(values)/len(values),6) if values else None,
                'p95':values[max(0,(len(values)*95+99)//100-1)] if values else None}
    names=sorted({k for row in rows for k in row['timings']})
    return {'samples':len(rows),'model_calls':sum(r['model_calls'] for r in rows),
            'budget_stops':sum(r['budget_stop'] is not None for r in rows),
            'input_estimated_tokens':sum(r['input_estimated_tokens'] for r in rows),
            'output_reserved_tokens':sum(r['output_reserved_tokens'] for r in rows),
            'actual_usage':'unknown','hardware':'unknown',
            'timings':{k:stats([r['timings'][k] for r in rows if k in r['timings']]) for k in names},
            'scope':'最近至多64个有运行指标的请求；估算不是Token计量，应用耗时不是GPU耗时。'}
