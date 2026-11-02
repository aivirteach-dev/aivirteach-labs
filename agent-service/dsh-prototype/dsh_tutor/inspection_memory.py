"""检查证据持久化：可信应用编制有限摘要，回答同事务写入，历史不可当当前观察。

复用results的本轮证据验证，不保存工具原始message/note或任意输出。没有模型
数据库工具；此接口不是认证、签名或任意工具的通用证据协议。
"""
from datetime import datetime
import hashlib
import json
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
from .storage import StorageError,_handle,_text,_now
from .contracts import CourseAnswer
from .results import _validated_evidence
from .task_memory import TaskRepository
from .tools import TOOL_NAME


class ObservationSummary(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    evidence_id:str=Field(min_length=1,max_length=128)
    service:str=Field(min_length=1,max_length=64)
    state:Literal['running','stopped','unknown']
    source:Literal['simulation','gateway']
    observed_at:str=Field(min_length=1,max_length=64)


class AttemptSummary(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    attempt:int=Field(ge=1,le=32)
    tool:Literal['get_service_status','unsupported_tool']
    status:Literal['ok','denied','invalid','budget_exhausted','timeout','failed']
    evidence_id:str | None=Field(default=None,max_length=128)


class InspectionSnapshot(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    format_version:Literal['inspection-v1']='inspection-v1'
    tool_contract_version:Literal['service-status-v1']='service-status-v1'
    backend_version:None=None  # 当前模拟协议没有后端版本声明，不伪造部署版本。
    request_id:str=Field(min_length=1,max_length=128)
    run_id:str=Field(min_length=1,max_length=128)
    model:str=Field(min_length=1,max_length=128)
    status:Literal['no_tools','verified','no_observation','rejected']
    accepted_at:str=Field(max_length=64)
    recorded_at:str=Field(max_length=64)
    answer_sha256:str=Field(pattern='^[0-9a-f]{64}$')
    reported_attempts:int | None=Field(default=None,ge=0,le=32)
    reported_executions:int | None=Field(default=None,ge=0,le=10)
    observations:list[ObservationSummary]=Field(default_factory=list,max_length=10)
    attempts:list[AttemptSummary]=Field(default_factory=list,max_length=32)

    @model_validator(mode='after')
    def consistent(self):
        accepted=datetime.fromisoformat(self.accepted_at);recorded=datetime.fromisoformat(self.recorded_at)
        if accepted.tzinfo is None or recorded.tzinfo is None or recorded<accepted:raise ValueError('invalid_time')
        ids=[x.evidence_id for x in self.observations]
        if len(ids)!=len(set(ids)):raise ValueError('duplicate_evidence')
        for item in self.observations:
            at=datetime.fromisoformat(item.observed_at)
            if at.tzinfo is None or not accepted<=at<=recorded:raise ValueError('invalid_time')
        if self.status in ('no_tools','rejected'):
            if self.observations or self.attempts:raise ValueError('unexpected_observation')
            if self.status=='no_tools' and (self.reported_attempts!=0 or self.reported_executions!=0):raise ValueError('invalid_counts')
        else:
            if self.reported_attempts!=len(self.attempts) or self.reported_executions is None or not len(ids)<=self.reported_executions<=min(len(self.attempts),10):raise ValueError('invalid_counts')
            success=[]
            for i,row in enumerate(self.attempts,1):
                if row.attempt!=i:raise ValueError('invalid_sequence')
                if row.status=='ok':
                    if row.tool!=TOOL_NAME or row.evidence_id not in ids:raise ValueError('invalid_success')
                    success.append(row.evidence_id)
                elif row.evidence_id is not None:raise ValueError('invalid_failure')
            if sorted(success)!=sorted(ids) or (self.status=='verified')!=bool(ids):raise ValueError('invalid_observations')
        return self


def compile_snapshot(request,result,handle,policy,accepted_at):
    """只能由应用在模型结果返回后调用；接受时刻作为保守下界，再核对一次本轮证据。"""
    result=CourseAnswer.model_validate(result.model_dump());handle=_handle(handle)
    if (result.request_id,request.course.course_id,request.course.version,request.course.lesson_id)!=(
            request.request_id,handle.scope.course_id,handle.scope.course_version,handle.scope.lesson_id):
        raise StorageError('invalid_inspection')
    values=dict(request_id=request.request_id,run_id=result.run_id,model=result.model,accepted_at=accepted_at,
                recorded_at=_now(),answer_sha256=hashlib.sha256(result.answer.encode()).hexdigest())
    if policy is None:
        if result.tool_calls or result.tool_attempts or result.evidence or result.tool_trace or result.quality.facts!='not_requested':
            raise StorageError('invalid_inspection')
        return InspectionSnapshot(status='no_tools',reported_attempts=0,reported_executions=0,**values)
    if (policy.student_id,policy.learning_context_id,policy.lesson_id,policy.instance_id,policy.instance_generation)!=(
            handle.scope.student_id,handle.scope.learning_context_id,handle.scope.lesson_id,handle.scope.instance_id,handle.scope.instance_generation):
        raise StorageError('invalid_inspection')
    if result.quality.facts=='rejected':
        # 上游已清空被拒绝的原始证据；仅记拒绝状态，不把错绑内容复制进库。
        if result.evidence or result.tool_trace:raise StorageError('invalid_inspection')
        return InspectionSnapshot(status='rejected',**values)
    raw=result.model_dump();raw['run_started_at']=accepted_at
    try:evidence,_=_validated_evidence(request,raw,policy)
    except (ValueError,TypeError,KeyError):raise StorageError('invalid_inspection') from None
    if (bool(evidence) and result.quality.facts!='verified') or (not evidence and result.quality.facts!='unavailable'):
        raise StorageError('invalid_inspection')
    observations=[ObservationSummary(**{key:item.model_dump()[key] for key in ObservationSummary.model_fields}) for item in evidence]
    attempts=[AttemptSummary(attempt=row['attempt'],tool=TOOL_NAME if row.get('tool')==TOOL_NAME else 'unsupported_tool',
                             status=row['status'],evidence_id=(row.get('evidence') or {}).get('evidence_id')) for row in result.tool_trace]
    return InspectionSnapshot(status='verified' if evidence else 'no_observation',observations=observations,attempts=attempts,
                              reported_attempts=result.tool_attempts,reported_executions=result.tool_calls,**values)


def migrate_inspections(store):
    """显式v4→v5；旧回答不补造工具证据，保留原记录与旧验收库。"""
    with store._connection(write=True) as db:
        version=db.execute('PRAGMA user_version').fetchone()[0]
        if version==5:return
        if version!=4:raise StorageError('lifecycle_migration_required')
        if db.execute("SELECT 1 FROM turns WHERE status='pending' LIMIT 1").fetchone():raise StorageError('requests_pending')
        db.execute('''CREATE TABLE inspection_records (
            context_id TEXT NOT NULL,request_id TEXT NOT NULL,task_id TEXT NOT NULL,scope_revision INTEGER NOT NULL,
            run_id TEXT NOT NULL,kind TEXT NOT NULL,body_json TEXT NOT NULL,body_sha256 TEXT NOT NULL,
            PRIMARY KEY(context_id,request_id),
            FOREIGN KEY(context_id,request_id) REFERENCES turns(context_id,request_id),
            FOREIGN KEY(context_id,task_id) REFERENCES learning_tasks(context_id,task_id))''')
        db.execute('CREATE INDEX inspection_task_page ON inspection_records(context_id,task_id,scope_revision)')
        db.execute('PRAGMA user_version=5')


def save_in_transaction(db,handle,row,snapshot,*,answer,run_id,maximum):
    """在finish同一事务内检查绑定/来源范围，任何失败回滚回答和证据。"""
    if snapshot is None:raise StorageError('inspection_required')
    try:item=InspectionSnapshot.model_validate(snapshot.model_dump() if isinstance(snapshot,InspectionSnapshot) else snapshot)
    except (ValueError,TypeError,AttributeError):raise StorageError('invalid_inspection') from None
    if (item.request_id,item.run_id,item.accepted_at,item.answer_sha256)!=(row['request_id'],run_id,row['created_at'],hashlib.sha256(answer.encode()).hexdigest()):
        raise StorageError('invalid_inspection')
    task=db.execute('SELECT task_id FROM turn_tasks WHERE context_id=? AND request_id=?',
                    (handle.scope.learning_context_id,row['request_id'])).fetchone()
    if task is None:raise StorageError('task_binding_required')
    encoded=item.model_dump_json();digest=hashlib.sha256(encoded.encode()).hexdigest()
    if len(encoded.encode())>maximum:raise StorageError('inspection_capacity')
    key=handle.scope.learning_context_id
    old=db.execute('SELECT body_json FROM inspection_records WHERE context_id=? AND request_id=?',(key,row['request_id'])).fetchone()
    if old:
        if old[0]!=encoded:raise StorageError('terminal_conflict')
        return
    db.execute('INSERT INTO inspection_records VALUES(?,?,?,?,?,?,?,?)',
               (key,row['request_id'],task[0],handle.revision,run_id,item.status,encoded,digest))


def check_history_marker(row):
    """无工具轮次进入模型前核对摘要完整性；已含工具事实的轮次无需加载正文。"""
    try:
        body=row['inspection_body']
        if body is None or hashlib.sha256(body.encode()).hexdigest()!=row['inspection_hash']:raise ValueError()
        item=InspectionSnapshot.model_validate_json(body)
        if (item.status,item.request_id,item.run_id,item.answer_sha256)!=(
                'no_tools',row['request_id'],row['run_id'],hashlib.sha256((row['answer'] or '').encode()).hexdigest()):raise ValueError()
    except (ValueError,TypeError):raise StorageError('inspection_corrupt') from None


class InspectionRepository:
    """历史查看入口，只读且分页；永不作为自动模型上下文或本轮授权来源。"""
    def __init__(self,store):self.store=store

    def _check(self,db,handle,task_id):
        self.store._check(db,handle,active=True)
        if db.execute('PRAGMA user_version').fetchone()[0]!=5:raise StorageError('inspection_migration_required')
        row=db.execute('SELECT * FROM learning_tasks WHERE context_id=? AND task_id=?',(handle.scope.learning_context_id,task_id)).fetchone()
        TaskRepository._matches(row,handle)

    def _read(self,db,handle,task_id,rid):
        row=db.execute('SELECT t.*,r.body_json,r.body_sha256,r.run_id AS stored_run_id,r.kind AS record_kind,'
                       'r.task_id AS record_task,r.scope_revision AS record_scope,s.scope_json '
                       'FROM turns t JOIN turn_tasks a ON a.context_id=t.context_id AND a.request_id=t.request_id '
                       'JOIN scopes s ON s.context_id=t.context_id AND s.revision=t.scope_revision '
                       'LEFT JOIN inspection_records r ON r.context_id=t.context_id AND r.request_id=t.request_id '
                       'WHERE t.context_id=? AND a.task_id=? AND t.request_id=?',
                       (handle.scope.learning_context_id,task_id,rid)).fetchone()
        if row is None:raise StorageError('request_not_found')
        output={'request_id':rid,'run_id':row['run_id'],'task_id':task_id,'turn_state':row['status'],
                'always_historical':True,'usable_as_current':False,'tool_contract_version':None,
                'failure_code':row['error_code'],
                'note':'仅为历史记录；判断现在的环境必须重新检查，不能据此获得工具权限。'}
        if row['error_code']=='history_deleted':return output|{'record_status':'history_deleted'}
        if row['body_json'] is None:return output|{'record_status':'not_recorded' if row['status']=='completed' else 'no_final_result'}
        try:
            if hashlib.sha256(row['body_json'].encode()).hexdigest()!=row['body_sha256']:raise ValueError()
            item=InspectionSnapshot.model_validate_json(row['body_json'])
            if (item.request_id,item.run_id,item.answer_sha256)!=(rid,row['run_id'],hashlib.sha256((row['answer'] or '').encode()).hexdigest()):raise ValueError()
            if (item.status,item.run_id,task_id,row['scope_revision'])!=(row['record_kind'],row['stored_run_id'],row['record_task'],row['record_scope']):raise ValueError()
            scope=json.loads(row['scope_json'])
        except (ValueError,TypeError):raise StorageError('inspection_corrupt') from None
        environment=['instance_id','instance_generation','environment_os']
        return output|{'record_status':item.status,'snapshot':item.model_dump(),
                       'observed_environment':{key:scope[key] for key in environment},
                       'current_environment_matches':all(scope[key]==getattr(handle.scope,key) for key in environment),
                       'tool_contract_version':item.tool_contract_version}

    def get(self,handle,task_id,request_id):
        handle=_handle(handle);_text(task_id,128);_text(request_id,128)
        with self.store._connection() as db:
            self._check(db,handle,task_id);return self._read(db,handle,task_id,request_id)

    def list(self,handle,task_id,*,before=9223372036854775807,limit=10):
        handle=_handle(handle);_text(task_id,128)
        if type(before) is not int or not 1<=before<=9223372036854775807 or type(limit) is not int or not 1<=limit<=20:raise StorageError('invalid_input')
        with self.store._connection() as db:
            self._check(db,handle,task_id)
            rows=db.execute("SELECT t.sequence,t.request_id FROM turn_tasks a JOIN turns t ON t.sequence=a.sequence "
                            "WHERE a.context_id=? AND a.task_id=? AND a.sequence<? AND (t.error_code IS NULL OR t.error_code NOT IN ('history_deleted','memory_edit')) "
                            'ORDER BY a.sequence DESC LIMIT ?',
                            (handle.scope.learning_context_id,task_id,before,limit+1)).fetchall()
            return {'records':[self._read(db,handle,task_id,r['request_id'])|{'sequence':r['sequence']} for r in rows[:limit]],
                    'next_before':rows[limit-1]['sequence'] if len(rows)>limit else None,'always_historical':True,'usable_as_current':False}
