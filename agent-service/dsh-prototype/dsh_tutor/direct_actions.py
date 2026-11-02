"""显式course-actions-v1：只有白名单动作；不从自然语言猜命令、不启动模型。"""
import asyncio,json,time
from datetime import datetime,timezone
from uuid import uuid4
from pathlib import Path
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
from .contracts import CourseSnapshot,CourseQuestion,CourseAnswer

class DirectAction(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    kind:Literal['material_block','learning_goal','service_status']
    block_id:str | None=Field(default=None,pattern=r'^b[0-9]{3,6}$')
    service:str | None=Field(default=None,min_length=1,max_length=64)
    @model_validator(mode='after')
    def coherent(self):
        if (self.kind=='material_block')!=(self.block_id is not None):raise ValueError('block_argument_mismatch')
        if (self.kind=='service_status')!=(self.service is not None):raise ValueError('service_argument_mismatch')
        return self

class ActionRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    request_id:str=Field(min_length=1,max_length=128)
    course:CourseSnapshot
    action:DirectAction
    def question(self):
        # 原ChatStore以完整question做去重；规范动作正文避免不同动作共用同一保存键。
        return CourseQuestion(request_id=self.request_id,course=self.course,
            question='[course-actions-v1] '+json.dumps(self.action.model_dump(),sort_keys=True,ensure_ascii=False,separators=(',',':')))

class DirectActionError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)

def execute_action(prepared,action,*,repository=None,service_executor=None):
    """返回结果仍由ChatSession统一校验/保存；不在本模块另开持久化路径。"""
    from .tools import ToolGateway,AuditLog,TOOL_NAME
    from .results import compose_tool_answer
    p=prepared;request=p.request;course=request.course
    en=course.locale=='en';run_id='direct-'+uuid4().hex;started=time.monotonic()
    if course.locale not in ('zh-CN','en'):raise DirectActionError('action_locale_unsupported')
    began=datetime.now(timezone.utc).isoformat()
    values=dict(request_id=request.request_id,execution_path='direct',direct_action_kind=action.kind,
                memory_revision=p.memory_epoch if action.kind=='learning_goal' else None,run_id=run_id,
                model='deterministic:course-actions-v1',elapsed_seconds=0.0)
    if action.kind=='material_block':
        if repository is None:raise DirectActionError('material_repository_unavailable')
        try:block=repository.block(course.course_id,course.version,course.locale,course.lesson_id,action.block_id)
        except (OSError,ValueError,KeyError):raise DirectActionError('material_block_unavailable') from None
        if course.material is None or (block['source_sha256'],block['source_filename'],block['lesson_id'])!=(course.material.source_sha256,course.material.source_filename,course.material.anchor):raise DirectActionError('material_source_mismatch')
        label='Material verbatim' if en else '教材原文'
        answer=label+':\n'+block['text']+'\n'+('Source: ' if en else '出处：')+block['source_filename']+' #'+course.lesson_id+'/'+action.block_id+' SHA256:'+block['source_sha256']
        values.update(answer=answer,limitations=['原文完整展示，未执行命令；尚未检查实验环境。' if not en else 'Complete source text; no commands executed or environment checked.'])
    elif action.kind=='learning_goal':
        if p.learning_memory is None:raise DirectActionError('learning_memory_unavailable')
        items=p.learning_memory.learning_items(p.handle);goal=next((x for x in items if x.kind=='goal'),None)
        values.update(answer=('Recorded learning goal: ' if en else '已记录的学习目标：')+goal.text+('\nStudent statement; source request: ' if en else '\n来源为学生声明，请求编号：')+goal.source_request_id if goal else ('No learning goal is recorded for this task.' if en else '当前任务尚未记录学习目标。'),limitations=['目标是学生声明，不代表已掌握或通过课程。' if not en else 'A goal is a student statement, not proof of mastery.'])
    else:
        if p.policy is None or service_executor is None:raise DirectActionError('service_executor_unavailable')
        from .execution_budget import runtime_stage
        policy=p.policy.model_copy(update={'run_id':run_id,'deadline_unix':time.time()+min(10.0,p.policy.timeout_seconds)})
        directory=Path(p.state_dir)/run_id;directory.mkdir(parents=True,exist_ok=False)
        from .run_registry import RunLease
        registration=RunLease(directory,'direct_tool');outcome='unknown'
        try:
            gateway=ToolGateway(policy,AuditLog(directory/'tool-audit.jsonl'),service_executor)
            runtime_stage('tool_execution')
            asyncio.run(gateway.call(TOOL_NAME,{'service':action.service}))
            rows=gateway.audit.read();evidence=[x['evidence'] for x in rows if x['type']=='result' and x.get('evidence')]
            selection={'evidence_ids':[x['evidence_id'] for x in evidence],'course_instruction_indexes':[],'next_step':'none'}
            raw=dict(answer=json.dumps(selection),run_id=run_id,model=values['model'],elapsed_seconds=time.monotonic()-started,
              run_started_at=datetime.now(timezone.utc).isoformat(),tool_calls=sum(x['type']=='execution' for x in rows),
              tool_attempts=sum(x['type']=='attempt' for x in rows),tool_trace=[x for x in rows if x['type']=='result'],evidence=evidence)
            # 校验要求观察时间在本轮开始之后；工具调用前的真实开始时间单独保存。
            raw['run_started_at']=began
            result=compose_tool_answer(request,raw,policy)
            result.update(request_id=request.request_id,execution_path='direct',direct_action_kind=action.kind)
            response=CourseAnswer.model_validate(result).model_dump()
            outcome='completed' if all(x['status'] not in ('failed','timeout') for x in rows if x['type']=='result') else 'unknown'
            return response
        finally:registration.close(outcome)
    values['elapsed_seconds']=time.monotonic()-started
    return CourseAnswer(**values).model_dump()
