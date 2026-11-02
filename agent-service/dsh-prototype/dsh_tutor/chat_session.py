"""应用层持久问答：原子接受请求、有限恢复前文、调用既有Agent、保存结果。

不提供登录认证或隐式重试，不让DSH直接读数据库；不同存储实现遵循ChatStore。
"""
from typing import Literal
from dataclasses import dataclass
from threading import Lock
from contextlib import nullcontext
from copy import deepcopy
from .budgets import PreparationStopped,ExecutionAdmissionError
from .execution_budget import RuntimeMetrics
import time
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import CourseQuestion, CourseAnswer
from .conversation import ConversationContext, ConversationLimits, ConversationTurn, prepare_context
from .course import answer_course, build_prompt
from .results import trusted_policy
from .storage import ChatStore, ScopeHandle, StorageError, TurnPage


class HistoryReadLimits(BaseModel):
    """最多一次有界查询；不因查不到完整对话就扫描整个历史库。"""
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    max_scan_turns: int=Field(default=16,ge=1,le=16)
    recent_turns: int=Field(default=4,ge=1,le=4)


class HistoryReadInfo(BaseModel):
    mode: Literal['recent','earlier','none']='recent'
    rows_limit: int=0
    rows_seen: int=0
    skipped_inspection: int=0
    skipped_incomplete: int=0
    skipped_oversized: int=0
    older_available: bool=False
    memory_items: int=0
    memory_omitted: int=0
    skipped_before_memory_edit: int=0


class ChatReply(BaseModel):
    """保存状态与模型结果分开；重放只有原文，不伪造本次工具检查或模型用量。"""
    model_config=ConfigDict(extra='forbid',strict=True)
    request_id: str
    task_id: str | None=Field(default=None,min_length=1,max_length=128)
    state: Literal['saved','replayed','in_progress','failed','not_saved','scope_changed']
    stage: Literal['input','accept','history','prepare','preparation_wait','execution_wait','revalidate','model','result','save','replay','direct_action','completion_wait','delivery_save']
    message: str
    error_code: str | None=None
    answer: str | None=None
    answer_status: Literal['completed','partial'] | None=None
    run_id: str | None=None
    result: CourseAnswer | None=None
    history: HistoryReadInfo=Field(default_factory=HistoryReadInfo)
    recovery_required: bool=False
    diagnostic_issue: str | None=None
    runtime_metrics: RuntimeMetrics | None=None


def _failed_record(store,handle,request_id,code):
    """只尽力记录安全失败码；不重试、不掩盖记录失败，也不暴露原异常。"""
    try:
        store.finish(handle,request_id,error_code=code)
        return None
    except StorageError as exc:
        return exc.code


def _load_history(store,handle,sequence,limits,*,minimum_sequence=0,history_mode='recent',history_before=None):
    capacity=store.page_capacity
    if type(capacity) is not int or not 1<=capacity<=100:
        raise StorageError('invalid_history_page')
    limit=min(capacity,limits.max_scan_turns,limits.recent_turns if history_mode=='recent' else 16)
    before=sequence if history_before is None else history_before
    if type(before) is not int or not 0<before<=sequence:raise StorageError('invalid_history_query')
    page=store.read_page(handle,before=before,limit=limit)
    page=TurnPage.model_validate(page.model_dump())
    rows=page.turns
    ids=[x.sequence for x in rows]
    if (len(rows)>limit or ids!=sorted(set(ids))
            or any(x.scope_revision!=handle.revision or not 0<x.sequence<before for x in rows)):
        raise StorageError('invalid_history_page')
    info=HistoryReadInfo(mode=history_mode,rows_limit=limit,rows_seen=len(rows),older_available=page.next_before is not None)
    if history_mode=='earlier' and history_before is None:
        # 明确查询上一段历史时省略最近窗口；不能读16轮后又只保留同样的最近4轮。
        rows=rows[:-limits.recent_turns] if len(rows)>limits.recent_turns else []
    turns=[]
    for row in rows:
        if row.sequence<minimum_sequence:
            info.skipped_before_memory_edit+=1
            continue
        # partial可能包含不完整说明，先保守省略；正式存储保留原文，不截断改写。
        if row.status!='completed' or row.answer_status!='completed':
            info.skipped_incomplete+=1
            continue
        if row.history_excluded:
            info.skipped_inspection+=1
            continue
        if (not row.answer or len(row.answer)>2000 or not row.question or len(row.question)>2000):
            info.skipped_oversized+=1
            continue
        turns.append(ConversationTurn(scope=handle.scope,user=row.question,assistant=row.answer))
    incomplete=bool(history_mode=='earlier' or info.skipped_inspection or info.skipped_incomplete or info.skipped_oversized or info.older_available or info.skipped_before_memory_edit)
    return ConversationContext(scope=handle.scope,turns=turns,history_incomplete=incomplete),info


def _prepare_chat(request: CourseQuestion, *, store: ChatStore, handle: ScopeHandle,
                config_path, state_dir, tool_bridge=None, conversation_limits=None,
                history_read_limits=None, learning_memory=None, memory_limits=None, _trace=None,
                history_mode='recent', history_before=None,checkpoint=None, owner=None,
                direct_action=None,material_repository=None,service_executor=None):
    """可信应用入口：调用方核实身份/当前范围，范围切换须显式activate。

    本函数只准备有界快照，不调用模型；ChatSession执行段复用answer_course。
    数据库连接不跨越推理。中途切换或删除时不把旧资料交给新范围。
    """
    candidate=getattr(request,'request_id',None)
    rid=candidate if type(candidate) is str and 1<=len(candidate)<=128 else 'invalid'
    info=HistoryReadInfo()
    if getattr(store,'learning_memory_required',False) and learning_memory is None:
        learning_memory=store
    task_id=getattr(store,'task_id',None)
    english=getattr(getattr(request,'course',None),'locale',None)=='en'
    def reply(state,stage,zh,en,**values):
        return ChatReply(request_id=rid,task_id=task_id,state=state,stage=stage,message=en if english else zh,
                         history=info,**values)
    try:
        if learning_memory is not None and learning_memory is not store:
            raise ValueError('memory_coordinator_required')
        if history_mode not in ('recent','earlier','none'):raise ValueError('invalid_history_mode')
        if history_before is not None and (history_mode!='earlier' or type(history_before) is not int or history_before<1):raise ValueError('invalid_history_query')
        # 对学生问题、教材与工具配置取独立快照，排队期间不引用调用方可变对象。
        tool_bridge=deepcopy(tool_bridge)
        request=CourseQuestion.model_validate(request.model_dump())
        handle=ScopeHandle.model_validate(handle.model_dump())
        limits=ConversationLimits.model_validate(
            conversation_limits.model_dump() if isinstance(conversation_limits,ConversationLimits)
            else ({} if conversation_limits is None else conversation_limits))
        read_limits=HistoryReadLimits.model_validate(
            history_read_limits.model_dump() if isinstance(history_read_limits,HistoryReadLimits)
            else ({} if history_read_limits is None else history_read_limits))
        from .direct_actions import DirectAction
        direct_action=DirectAction.model_validate(direct_action.model_dump()) if direct_action is not None else None
        if direct_action is not None:
            from .direct_actions import ActionRequest
            if history_mode!='none' or request.question!=ActionRequest(request_id=request.request_id,course=request.course,action=direct_action).question().question:raise ValueError('invalid_action_envelope')
        policy=trusted_policy(request,tool_bridge) if tool_bridge is not None and (direct_action is None or direct_action.kind=='service_status') else None
        empty=ConversationContext(scope=handle.scope)
        prepare_context(request,empty,limits,policy=policy)
        build_prompt(request,tool_available=tool_bridge is not None,conversation=empty,conversation_limits=limits)
    except (ValueError,TypeError,AttributeError):
        return reply('failed','input','输入或可信范围不符合约定，未接受问题、未调用模型。',
                     'Invalid input or trusted scope; no question was accepted and no model was called.',error_code='invalid_input')
    if checkpoint is not None:checkpoint()
    if _trace is not None:_trace.mark('accept')
    try:
        accepted=store.accept(handle,rid,request.question)
    except StorageError as exc:
        state='scope_changed' if exc.code=='stale_scope' else 'failed'
        return reply(state,'accept','未能接受本次问题，未调用模型；请核对范围或存储状态。',
                     'The question could not be accepted; check the scope or storage. No model was called.',error_code=exc.code)
    row=accepted.turn
    if owner is not None:owner.created=accepted.created
    if not accepted.created:
        if _trace is not None:_trace.mark('replay')
        if row.status=='completed':
            return reply('replayed','replay','返回该请求已保存的原回答；本次没有重新调用模型或检查环境。',
                         'Returning the saved answer; no new model call or environment check occurred.',
                         answer=row.answer,answer_status=row.answer_status,run_id=row.run_id)
        if row.status=='pending':
            return reply('in_progress','replay','该请求尚未完成，未重复调用模型；如已确认中断，请由应用恢复状态。',
                         'This request is pending; no duplicate model call was made. The application must confirm interruption before recovery.',error_code='request_pending')
        return reply('failed','replay','该请求已有失败或取消记录，未自动重试模型。',
                     'This request already failed or was cancelled; the model was not retried.',error_code=row.error_code)
    if checkpoint is not None:checkpoint()
    if _trace is not None:_trace.mark('history')
    try:
        if history_mode=='none':
            # 不读取历史、不提取新记忆；仍绑定修订，防止删除/更正期间交付旧回答。
            conversation=ConversationContext(scope=handle.scope,history_incomplete=True,history_loading="course_only")
            info=HistoryReadInfo(mode='none')
            memory_snapshot=None
            memory_epoch=learning_memory.learning_state(handle)[1] if learning_memory is not None else None
        elif learning_memory is not None:
            from .learning_memory import MemoryLimits, MemoryPromptItem
            import json
            ml=MemoryLimits.model_validate({} if memory_limits is None else memory_limits)
            items=learning_memory.remember_question(handle,rid,request.question,limits=ml)
            memory_floor,memory_epoch=learning_memory.learning_state(handle)
            conversation,info=_load_history(store,handle,row.sequence,read_limits,minimum_sequence=memory_floor,history_mode=history_mode,history_before=history_before)
            from .learning_memory import PREFIXES
            kept=[turn for turn in conversation.turns if not any(line.startswith(prefix) for line in turn.user.splitlines() for prefix in PREFIXES)]
            if len(kept)!=len(conversation.turns):
                conversation.history_incomplete=True
            conversation.turns=kept
            memory_snapshot=[x.model_dump() for x in items]
            selected=[]
            for item in items:
                candidate=MemoryPromptItem(kind=item.kind,text=item.text)
                if len(json.dumps([x.model_dump() for x in selected+[candidate]],ensure_ascii=False))<=ml.max_prompt_chars:
                    selected.append(candidate)
                else:
                    info.memory_omitted+=1
            info.memory_items=len(selected)
            conversation.learning_memory=selected
            conversation.history_incomplete=conversation.history_incomplete or bool(info.memory_omitted)
        else:
            conversation,info=_load_history(store,handle,row.sequence,read_limits,history_mode=history_mode,history_before=history_before)
    except (StorageError,ValidationError,AttributeError,TypeError):
        if checkpoint is not None:checkpoint()
        cleanup=_failed_record(store,handle,rid,'application_failed')
        return reply('scope_changed' if cleanup=='stale_scope' else 'failed','history',
                     '无法恢复前文，未调用模型；未将缺失历史当作空白会话继续回答。',
                     'History could not be restored; no model was called and no stateless fallback was used.',
                     error_code='history_unavailable',recovery_required=cleanup is not None)
    if checkpoint is not None:checkpoint()
    if _trace is not None:_trace.mark('prepare')
    try:
        build_prompt(request,tool_available=tool_bridge is not None,conversation=conversation,conversation_limits=limits)
        current=store.current(student_id=handle.scope.student_id,learning_context_id=handle.scope.learning_context_id)
        if current!=handle:
            raise StorageError('stale_scope')
    except (ValueError,StorageError):
        if checkpoint is not None:checkpoint()
        cleanup=_failed_record(store,handle,rid,'application_failed')
        return reply('scope_changed' if cleanup=='stale_scope' else 'failed','prepare',
                     '前文预算或当前范围检查未通过，未调用模型。',
                     'History budget or current scope validation failed; no model was called.',
                     error_code='context_unavailable',recovery_required=cleanup is not None)
    if checkpoint is not None:checkpoint()
    ready=_PreparedChat(request,store,handle,conversation,limits,policy,row,learning_memory,
                        memory_snapshot if learning_memory is not None else None,
                        memory_epoch if learning_memory is not None else None,info,reply,
                        tool_bridge,config_path,state_dir,direct_action,material_repository,service_executor)
    # 读历史期间发生更正/删除也必须拒绝；等待模型后还会再次核对。
    try:
        _check_prepared(ready)
    except StorageError as exc:
        if checkpoint is not None:checkpoint()
        cleanup=_failed_record(store,handle,rid,'application_failed')
        return reply('scope_changed','prepare','资料准备期间范围或记忆已变化，未调用模型。',
                     'Scope or memory changed while preparing; no model was called.',
                     error_code=exc.code,recovery_required=cleanup not in (None,'stale_scope','terminal_conflict'))
    return ready


@dataclass
class _PreparedChat:
    """只存在于单次ChatSession中的有界快照；不能从HTTP/模型反序列化。"""
    request: object
    store: object
    handle: object
    conversation: object
    limits: object
    policy: object
    row: object
    learning_memory: object
    memory_snapshot: object
    memory_epoch: object
    info: object
    reply: object
    tool_bridge: object
    config_path: object
    state_dir: object
    direct_action: object
    material_repository: object
    service_executor: object


def _check_prepared(p):
    p.store.check_pending(p.handle,p.request.request_id,p.row.sequence,p.request.question)
    if p.learning_memory is not None:
        epoch=p.learning_memory.learning_state(p.handle)[1]
        if epoch!=p.memory_epoch:raise StorageError('memory_changed')
        if p.memory_snapshot is not None and [x.model_dump() for x in p.learning_memory.learning_items(p.handle)]!=p.memory_snapshot:raise StorageError('memory_changed')


def _execute_chat(p,*,_trace=None,revalidation_budget=None,execution_scope=None):
    """等待之后重新验证，再调用唯一既有模型循环；保存仍使用最终事务版本检查。"""
    request,store,handle=p.request,p.store,p.handle
    conversation,limits,policy,row=p.conversation,p.limits,p.policy,p.row
    learning_memory,memory_snapshot,memory_epoch=p.learning_memory,p.memory_snapshot,p.memory_epoch
    info,reply,rid=p.info,p.reply,request.request_id
    tool_bridge,config_path,state_dir=p.tool_bridge,p.config_path,p.state_dir
    with execution_scope() if execution_scope is not None else nullcontext() as receipt:
        # 校验预算从取得执行许可之后开始，不能让排队时间耗尽SQL复核预算。
        if callable(revalidation_budget):revalidation_budget=revalidation_budget()
        if _trace is not None:_trace.mark('revalidate')
        try:
            with revalidation_budget.storage_scope() if revalidation_budget is not None else nullcontext():
                _check_prepared(p)
                if revalidation_budget is not None:revalidation_budget.check()
        except (StorageError,PreparationStopped) as exc:
            cleanup=_failed_record(store,handle,rid,'cancelled' if exc.code=='cancelled' else 'application_failed')
            changed=exc.code in ('stale_scope','stale_request','memory_changed','task_archived','task_denied','scope_denied')
            return reply('scope_changed' if changed else 'failed','revalidate',
                '排队后资料或范围检查未通过，未调用模型。','Post-queue context validation failed; no model was called.',
                error_code=exc.code,recovery_required=cleanup not in (None,'stale_scope','terminal_conflict'))
        if receipt is not None:receipt.started=True
        if _trace is not None:_trace.mark('direct_action' if p.direct_action is not None else 'model')
        try:
            from .execution_budget import observe_runtime
            with observe_runtime(_trace.mark if _trace is not None else None):
                if p.direct_action is not None:
                    from .direct_actions import execute_action
                    raw=execute_action(p,p.direct_action,repository=p.material_repository,service_executor=p.service_executor)
                else:
                    raw=answer_course(request,config_path=config_path,state_dir=state_dir,tool_bridge=tool_bridge,
                                      conversation=conversation,conversation_limits=limits)
            if receipt is not None:receipt.confirmed=True
        except Exception as exc:
            from .execution_budget import RuntimeFailure
            from .direct_actions import DirectActionError
            code=exc.code if isinstance(exc,DirectActionError) else exc.code if isinstance(exc,RuntimeFailure) and exc.code in ('model_budget_exhausted','cancelled_before_dispatch','provider_config_rejected','provider_request_rejected','provider_outcome_unknown','cancel_unconfirmed') else 'direct_action_failed' if p.direct_action is not None else 'run_failed'
            if receipt is not None and isinstance(exc,RuntimeFailure) and exc.known_end:receipt.confirmed=True
            cleanup=_failed_record(store,handle,rid,'model_failed')
            return reply('scope_changed' if cleanup=='stale_scope' else 'failed','direct_action' if p.direct_action is not None else 'model',
                         '本轮固定动作未完成，未调用模型或自动重试。' if p.direct_action is not None else '本轮运行失败，未自动重试；本机结束不代表远端模型已经停止。',
                         'The direct action did not complete; no model was called or automatically retried.' if p.direct_action is not None else 'The run failed and was not retried; local termination does not confirm remote cancellation.',
                         error_code=code,recovery_required=cleanup is not None,
                         runtime_metrics=exc.metrics if isinstance(exc,RuntimeFailure) else None)
    if _trace is not None:_trace.mark('result')
    validation_started=time.monotonic()
    try:
        result=CourseAnswer.model_validate(raw)
        if result.request_id!=rid or not result.answer.strip() or not result.run_id:
            raise ValueError('invalid_result')
        inspection=None
        if getattr(store,'inspection_required',False):
            from .inspection_memory import compile_snapshot
            inspection=compile_snapshot(request,result,handle,policy,row.created_at)
    except (ValueError,TypeError,StorageError):
        cleanup=_failed_record(store,handle,rid,'application_failed')
        return reply('failed','result','本轮结果格式不正确，未保存为正式回答。',
                     'The result format was invalid and was not saved as a final answer.',
                     error_code='invalid_result',recovery_required=cleanup is not None)
    if result.runtime_metrics is not None:
        result.runtime_metrics.timings['result_validation']=time.monotonic()-validation_started
    if _trace is not None:_trace.mark('save')
    save_started=time.monotonic()
    try:
        if learning_memory is not None and (learning_memory.learning_state(handle)[1]!=memory_epoch or memory_snapshot is not None and [x.model_dump() for x in learning_memory.learning_items(handle)]!=memory_snapshot):
            cleanup=_failed_record(store,handle,rid,'application_failed')
            return reply('scope_changed','save','学习记忆已更正或删除，未交付旧上下文的回答。','Learning memory changed; the stale answer was withheld.',error_code='memory_changed',recovery_required=cleanup is not None)
        extra={'inspection':inspection} if inspection is not None else {}
        store.finish(handle,rid,answer=result.answer,answer_status=result.status,run_id=result.run_id,expected_memory_epoch=memory_epoch if learning_memory is not None else None,**extra)
    except StorageError as exc:
        changed=exc.code in ('stale_scope','scope_denied','terminal_conflict','memory_changed')
        cleanup_failed=False
        if exc.code=='memory_changed':
            cleanup_failed=_failed_record(store,handle,rid,'application_failed') is not None
        return reply('scope_changed' if changed else 'not_saved','save',
                     '范围或记录已变化，未交付旧回答。' if changed else '模型已返回，但最终保存未确认，未交付回答；请先恢复存储状态，不要重复执行模型。',
                     'The scope or record changed; the old answer was withheld.' if changed else
                     'The model returned, but final persistence was not confirmed. The answer was withheld; recover storage before retrying.',
                     error_code=exc.code,recovery_required=not changed or cleanup_failed,run_id=result.run_id)
    if result.runtime_metrics is not None:result.runtime_metrics.timings['chat_save']=time.monotonic()-save_started
    return reply('saved','save','回答已保存。','The answer was saved.',answer=result.answer,
                 answer_status=result.status,run_id=result.run_id,result=result,
                 runtime_metrics=result.runtime_metrics.model_dump() if result.runtime_metrics is not None else None)


class ChatSession:
    """单次请求分成准备/执行两段；只持有必要快照，拒绝重复执行同一对象。

    宿主负责在两段之间排队。每段和异常共用同一诊断轨迹，只写一次；
    明确取消时终止已接受pending，不自动重新执行模型。
    """
    def __init__(self,request,*,store,handle,config_path,state_dir,tool_bridge=None,
                 conversation_limits=None,history_read_limits=None,learning_memory=None,
                 memory_limits=None,diagnostics=None,progress=None,history_mode='recent',history_before=None,
                 direct_action=None,material_repository=None,service_executor=None):
        from .diagnostics import RequestTrace
        self.request=deepcopy(request);self.store=store;self.handle=handle;self.diagnostics=diagnostics
        self.trace=RequestTrace(progress);self.created=False;self._ready=None
        self._state='new';self._lock=Lock();self.result=None
        self.arguments=dict(store=store,handle=handle,config_path=config_path,state_dir=state_dir,
            tool_bridge=deepcopy(tool_bridge),conversation_limits=deepcopy(conversation_limits),history_read_limits=deepcopy(history_read_limits),
            learning_memory=learning_memory,memory_limits=deepcopy(memory_limits),history_mode=history_mode,history_before=history_before,
            direct_action=direct_action,material_repository=material_repository,service_executor=service_executor)

    @property
    def can_abort(self):return self._state=='ready'

    def _claim(self,expected,target):
        with self._lock:
            if self._state!=expected:raise ValueError('invalid_session_state')
            self._state=target

    def _finish(self,result):
        self.result=result;self._state='done';self._ready=None
        if self.diagnostics is not None:
            try:self.diagnostics.record(self.handle,result,self.trace)
            except (StorageError,ValueError,TypeError):result.diagnostic_issue='diagnostic_unavailable'
        if self.trace.progress_failed and result.diagnostic_issue is None:result.diagnostic_issue='progress_unavailable'
        return result

    def _failure(self,code,*,stage=None):
        cleanup=_failed_record(self.store,self.handle,self.request.request_id,
            'cancelled' if code=='cancelled' else 'timeout' if code.endswith('_timeout') else 'application_failed') if self.created else None
        english=getattr(getattr(self.request,'course',None),'locale',None)=='en'
        return ChatReply(request_id=getattr(self.request,'request_id','invalid'),task_id=getattr(self.store,'task_id',None),
            state='failed',stage=stage or self.trace.stage,error_code=code,
            message='Preparation or waiting did not complete; no model was called. Review the reason before submitting a new request.' if english else
                    '准备或等待未完成，未调用模型；请核对原因后使用新的请求编号。',
            recovery_required=cleanup not in (None,'stale_scope','terminal_conflict'))

    def prepare(self,*,budget=None):
        self._claim('new','preparing')
        try:
            with budget.storage_scope() if budget is not None else nullcontext():
                result=_prepare_chat(self.request,**self.arguments,_trace=self.trace,
                                     checkpoint=budget.check if budget is not None else None,owner=self)
                if budget is not None:budget.check()
        except PreparationStopped as exc:
            return self._finish(self._failure(exc.code,stage='prepare'))
        except Exception:
            self._finish(self._failure('unexpected_failure'));raise
        if isinstance(result,ChatReply):return self._finish(result)
        self._ready=result;self._state='ready';return None

    def execute(self,*,revalidation_budget=None,execution_scope=None):
        self._claim('ready','executing')
        try:result=_execute_chat(self._ready,_trace=self.trace,revalidation_budget=revalidation_budget,execution_scope=execution_scope)
        except ExecutionAdmissionError:
            self._state='ready';raise
        except Exception:
            # 未预期异常记录失败后继续传播，调度宿主决定是否暂停；不重跑模型。
            self._finish(self._failure('unexpected_failure'));raise
        return self._finish(result)

    def abort(self,code):
        """仅允许中止尚未执行的快照；不能用此方法假称远端模型已取消。"""
        self._claim('ready','aborting')
        return self._finish(self._failure(code,stage='execution_wait'))


def answer_chat(request: CourseQuestion, *, store: ChatStore, handle: ScopeHandle,
                config_path, state_dir, tool_bridge=None, conversation_limits=None,
                history_read_limits=None, learning_memory=None, memory_limits=None, diagnostics=None,
                progress=None,history_mode='recent',history_before=None,
                direct_action=None,material_repository=None,service_executor=None) -> ChatReply:
    """原同步接口沿用同一准备/执行实现；资源宿主在两段之间加入有界调度。"""
    session=ChatSession(request,store=store,handle=handle,config_path=config_path,state_dir=state_dir,
        tool_bridge=tool_bridge,conversation_limits=conversation_limits,history_read_limits=history_read_limits,
        learning_memory=learning_memory,memory_limits=memory_limits,diagnostics=diagnostics,
        progress=progress,history_mode=history_mode,history_before=history_before,
        direct_action=direct_action,material_repository=material_repository,service_executor=service_executor)
    result=session.prepare()
    return result if result is not None else session.execute()
