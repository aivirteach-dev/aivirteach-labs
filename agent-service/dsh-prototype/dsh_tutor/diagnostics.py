"""有界请求诊断：每轮最多64段内存计时，结束后一次写入；不保存聊天或异常正文。"""
import hashlib
import json
import time
from pydantic import BaseModel,ConfigDict,Field
from .storage import StorageError,_handle,_now
from .lifecycle import require_lifecycle
from .execution_budget import RUNTIME_STAGES

STAGES=RUNTIME_STAGES | {'input','accept','history','prepare','preparation_wait','execution_wait','revalidate','model','result','save','replay','direct_action','completion_wait','delivery_save'}
STATES={'saved','replayed','in_progress','failed','not_saved','scope_changed'}
ERRORS={'cancelled_before_dispatch','provider_config_rejected','provider_request_rejected','provider_outcome_unknown','cancel_unconfirmed','finishing_queue_timeout','maintenance_busy','invalid_input','stale_scope','scope_denied','task_denied','task_archived','task_binding_required',
        'unexpected_failure','request_conflict','request_in_progress','capacity','busy','unavailable','request_pending','history_unavailable',
        'context_unavailable','run_failed','invalid_result','memory_changed','terminal_conflict','interrupted',
        'preparation_timeout','preparation_queue_timeout','queue_timeout','paused','closed','stale_request','invalid_history_query',
        'model_failed','cancelled','application_failed','history_deleted','timeout','model_budget_exhausted','direct_action_failed','action_locale_unsupported','material_repository_unavailable','material_block_unavailable',
        'material_source_mismatch','learning_memory_unavailable','service_executor_unavailable'}


class DiagnosticLimits(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    max_events:int=Field(default=200,ge=1,le=10000)
    page_limit:int=Field(default=20,ge=1,le=100)


def reference(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24]


class RequestTrace:
    def __init__(self,progress=None):
        self.started=self.changed=time.monotonic();self.stage='input';self.timeline=[]
        self.progress=progress;self.progress_failed=False
        self._notify()

    def _notify(self):
        # 监测故障只标记，不改变问答结果，不因显示失败重跑模型。
        if self.progress is not None:
            try:self.progress(self.stage)
            except Exception:self.progress_failed=True

    def mark(self,stage):
        if stage not in STAGES:raise ValueError('invalid_stage')
        now=time.monotonic()
        if len(self.timeline)<63:self.timeline.append({'stage':self.stage,'seconds':round(now-self.changed,6)})
        self.stage=stage;self.changed=now
        self._notify()

    def finish(self):
        now=time.monotonic()
        return (self.timeline+[{'stage':self.stage,'seconds':round(now-self.changed,6)}])[:64],round(now-self.started,6)


class DiagnosticStore:
    """可信应用注入到问答入口；查询仅同一学习记录。管理全局仅提供聚合容量。"""
    def __init__(self,store,limits=None):
        self.store=store;self.limits=DiagnosticLimits.model_validate({} if limits is None else limits)

    def record(self,handle,reply,trace):
        handle=_handle(handle);timeline,elapsed=trace.finish()
        if reply.stage not in STAGES or reply.state not in STATES:raise StorageError('invalid_input')
        code=reply.error_code if reply.error_code in ERRORS else 'unclassified' if reply.error_code else None
        with self.store._connection(write=True) as db:
            require_lifecycle(db);self.store._check(db,handle,active=True)
            # 先删有限滚动记录再写入，达到上限也不会无限累积正文/堆积队列。
            db.execute('DELETE FROM diagnostic_events WHERE sequence NOT IN '
                       '(SELECT sequence FROM diagnostic_events ORDER BY sequence DESC LIMIT ?)',(self.limits.max_events-1,))
            db.execute('INSERT INTO diagnostic_events(context_id,request_ref,task_ref,state,stage,error_code,elapsed_seconds,timeline_json,created_at) '
                       'VALUES(?,?,?,?,?,?,?,?,?)',(handle.scope.learning_context_id,reference(reply.request_id),
                       reference(reply.task_id) if reply.task_id else None,reply.state,reply.stage,code,elapsed,json.dumps(timeline),_now()))

    def read_page(self,handle,*,before=9223372036854775807,limit=None):
        handle=_handle(handle);limit=self.limits.page_limit if limit is None else limit
        if type(before) is not int or before<1 or type(limit) is not int or not 1<=limit<=self.limits.page_limit:raise StorageError('invalid_input')
        with self.store._connection() as db:
            require_lifecycle(db);self.store._check(db,handle,active=True)
            rows=db.execute('SELECT * FROM diagnostic_events WHERE context_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?',
                            (handle.scope.learning_context_id,before,limit+1)).fetchall()
            fields=('sequence','request_ref','task_ref','state','stage','error_code','elapsed_seconds','created_at')
            return {'events':[dict((k,r[k]) for k in fields)|{'timeline':json.loads(r['timeline_json'])} for r in rows[:limit]],
                    'next_before':rows[limit-1]['sequence'] if len(rows)>limit else None,
                    'note':'有限滚动诊断，可能淘汰旧项；耗时是应用各环节，不等于远端GPU/模型内部耗时。'}
