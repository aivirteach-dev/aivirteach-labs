"""单进程分段门控：总准入、资料准备与DSH执行；学生公平，有界等待和指标。"""
from collections import Counter,deque
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
import json
import re
from uuid import uuid4
import shutil
from pathlib import Path
from threading import Condition
import time

from pydantic import BaseModel,ConfigDict,Field,model_validator
from .conversation import ConversationLimits
from .chat_session import HistoryReadLimits,ChatReply,ChatSession
from .budgets import PreparationBudget,ExecutionAdmissionError
from .learning_memory import MemoryLimits
from .contracts import CourseQuestion
from .execution_budget import execution_projection
from .storage import ScopeHandle


class ResourcePolicy(BaseModel):
    """由管理员维护；validated_max_active是人工声明，必须另附实际容量证据。"""
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    max_active:int=Field(default=1,ge=1,le=32)
    validated_max_active:int=Field(default=1,ge=1,le=32)
    max_waiting:int=Field(default=4,ge=0,le=128)
    max_per_student:int=Field(default=1,ge=1,le=8)
    queue_timeout_seconds:float=Field(default=10.0,gt=0,le=60)
    max_finishing:int=Field(default=2,ge=1,le=16)
    finishing_queue_timeout_seconds:float=Field(default=10.0,gt=0,le=60)
    min_free_bytes:int=Field(default=268435456,ge=0)
    max_preparing:int=Field(default=2,ge=1,le=8)
    preparation_queue_timeout_seconds:float=Field(default=5.0,gt=0,le=60)
    preparation_timeout_seconds:float=Field(default=3.0,gt=0,le=30)
    revalidation_timeout_seconds:float=Field(default=2.0,gt=0,le=30)
    conversation:ConversationLimits=Field(default_factory=ConversationLimits)
    history:HistoryReadLimits=Field(default_factory=HistoryReadLimits)
    memory:MemoryLimits=Field(default_factory=MemoryLimits)
    @model_validator(mode='after')
    def check_capacity(self):
        if self.max_active>self.validated_max_active:
            raise ValueError('并发不能超过人工声明的已验证上限；未知容量默认串行。')
        return self


class AdmissionError(ExecutionAdmissionError):
    def __init__(self,code,waited=0.0):
        self.code,self.waited=code,waited
        super().__init__('resource:'+code)


@dataclass
class _Ticket:
    """身份只留在进程内部；管理快照只输出随机运行引用和请求哈希。"""
    student: str
    run_ref: str
    request_ref: str | None
    submitted: float
    deadline: float
    cancel: object = None
    started: float | None = None
    stage: str = 'waiting'
    changed: float = 0.0
    outcome: str = 'finished'
    error_code: str | None = None
    slot_attempted: bool = False


class ResourceGate:
    """应用启动时建一个实例并复用；不能每请求新建，也不能冒充跨进程全局限流。"""
    def __init__(self,policy):
        self.policy=ResourcePolicy.model_validate(policy.model_dump() if isinstance(policy,ResourcePolicy) else policy)
        self._cv=Condition();self._queues={};self._rotation=deque();self._students=Counter();self._active=0
        self._running={};self._revision=1;self.instance_id=uuid4().hex
        self._closed=False;self._paused=False;self._counts=Counter()
        self._waits=deque(maxlen=64);self._runs=deque(maxlen=64)
        self._recent=deque(maxlen=32)
        self._requests={};self._prep_queue=deque();self._preparing={}
        self._finishing={};self._finish_queue=deque();self._maintenance=False
        self._prep_times=deque(maxlen=64);self._prep_waits=deque(maxlen=64);self._runtime_samples=deque(maxlen=64)

    def _all_tickets(self):
        """总准入包含准备、排队和执行；兼容宿主直接占用slot的维护/测试调用。"""
        result=dict(self._requests);result.update(self._running)
        for q in self._queues.values():result.update((t.run_ref,t) for t in q)
        return result

    @contextmanager
    def request(self,student_id,*,run_ref,request_ref,cancel=None):
        """先限制总接收量，再读资料；整个请求只持有一个学生与系统配额。"""
        if type(student_id) is not str or not student_id.strip() or len(student_id)>128:raise AdmissionError('invalid_identity')
        if not re.fullmatch('[0-9a-f]{32}',run_ref) or not re.fullmatch('[0-9a-f]{24}',request_ref):raise AdmissionError('invalid_input')
        started=time.monotonic();ticket=_Ticket(student_id,run_ref,request_ref,started,0,cancel,stage='preparation_wait',changed=started)
        reserved=False
        try:
            with self._cv:
                if self._closed:self._reject('closed',started)
                if self._paused:self._reject('paused',started)
                if self._maintenance:self._reject('maintenance_busy',started)
                if cancel is not None and cancel.is_set():self._reject('cancelled',started)
                tickets=self._all_tickets()
                if run_ref in tickets:self._reject('invalid_input',started)
                if sum(t.student==student_id for t in tickets.values())>=self.policy.max_per_student:self._reject('student_limit',started)
                if len(tickets)>=self.policy.max_active+self.policy.max_waiting:self._reject('queue_full',started)
                self._requests[run_ref]=ticket;reserved=True
                self._counts['received']+=1
            yield ticket
        except AdmissionError as exc:
            ticket.outcome='rejected';ticket.error_code=exc.code;raise
        except BaseException:
            ticket.outcome='interrupted';ticket.error_code='unexpected_failure';raise
        finally:
            with self._cv:
                self._remember(ticket)
                if reserved:
                    del self._requests[run_ref]
                self._cv.notify_all()

    def _remember(self,ticket):
        at=time.monotonic()
        self._recent.append({'run_ref':ticket.run_ref,'request_ref':ticket.request_ref,'stage':ticket.stage,
            'seconds':round(at-ticket.submitted,4),'stage_seconds':round(at-ticket.changed,4),
            'cancel_requested':bool(ticket.cancel is not None and ticket.cancel.is_set()),
            'outcome':ticket.outcome,'error_code':ticket.error_code})

    @contextmanager
    def preparation(self,ticket):
        """只限制读取/组装阶段；阻塞调用未退出前不释放槽位，避免超时线程累积。"""
        started=time.monotonic();entered=False;queued=False
        try:
            with self._cv:
                if self._requests.get(ticket.run_ref) is not ticket:raise ValueError('unknown_request')
                deadline=started+self.policy.preparation_queue_timeout_seconds
                self._prep_queue.append(ticket);queued=True
                while True:
                    if self._closed:self._reject('closed',started)
                    if self._paused:self._reject('paused',started)
                    if ticket.cancel is not None and ticket.cancel.is_set():self._reject('cancelled',started)
                    remaining=deadline-time.monotonic()
                    if remaining<=0:self._reject('preparation_queue_timeout',started)
                    if self._prep_queue[0] is ticket and len(self._preparing)<self.policy.max_preparing:break
                    self._cv.wait(min(remaining,.05))
                self._prep_queue.popleft();queued=False;self._preparing[ticket.run_ref]=ticket;entered=True
                waited=round(time.monotonic()-started,4);self._prep_waits.append(waited);self._cv.notify_all()
            running=time.monotonic()
            try:yield waited
            finally:
                with self._cv:self._prep_times.append(round(time.monotonic()-running,4))
        finally:
            with self._cv:
                if queued:self._prep_queue.remove(ticket)
                if entered:del self._preparing[ticket.run_ref]
                self._cv.notify_all()

    @contextmanager
    def finishing(self,ticket):
        """先预留有限结果收尾空间，再运行模型；持有至交付保存完成。"""
        started=time.monotonic();entered=False
        with self._cv:self._finish_queue.append(ticket)
        try:
            with self._cv:
                while True:
                    if self._closed:self._reject('closed',started)
                    if self._paused:self._reject('paused',started)
                    if ticket.cancel is not None and ticket.cancel.is_set():self._reject('cancelled',started)
                    remaining=self.policy.finishing_queue_timeout_seconds-(time.monotonic()-started)
                    if remaining<=0:self._reject('finishing_queue_timeout',started)
                    if self._finish_queue[0] is ticket and len(self._finishing)<self.policy.max_finishing:break
                    self._cv.wait(min(.05,remaining))
                self._finish_queue.popleft();self._finishing[ticket.run_ref]=ticket;entered=True
            yield
        finally:
            with self._cv:
                if entered:self._finishing.pop(ticket.run_ref,None)
                elif ticket in self._finish_queue:self._finish_queue.remove(ticket)
                self._cv.notify_all()

    @contextmanager
    def maintenance(self):
        """无在途才占独立维护许可，不排无界维护队列。"""
        with self._cv:
            if self._maintenance or self._all_tickets():raise AdmissionError('maintenance_busy')
            self._maintenance=True
        try:yield
        finally:
            with self._cv:self._maintenance=False;self._cv.notify_all()

    def _reject(self,code,started):
        self._counts[code]+=1
        raise AdmissionError(code,round(time.monotonic()-started,4))

    @contextmanager
    def slot(self,student_id,*,cancel=None,run_ref=None,request_ref=None):
        if type(student_id) is not str or not student_id.strip() or len(student_id)>128:
            raise AdmissionError('invalid_identity')
        run_ref=uuid4().hex if run_ref is None else run_ref
        if not isinstance(run_ref,str) or not re.fullmatch('[0-9a-f]{32}',run_ref):
            raise AdmissionError('invalid_input')
        if request_ref is not None and (not isinstance(request_ref,str) or not re.fullmatch('[0-9a-f]{24}',request_ref)):
            raise AdmissionError('invalid_input')
        started=time.monotonic();queued=False;reserved=False;admitted=False
        ticket=_Ticket(student_id,run_ref,request_ref,started,0,cancel,changed=started)
        try:
            with self._cv:
                ticket.deadline=started+self.policy.queue_timeout_seconds
                if self._closed:self._reject('closed',started)
                if self._paused:self._reject('paused',started)
                if self._maintenance:self._reject('maintenance_busy',started)
                if cancel is not None and cancel.is_set():self._reject('cancelled',started)
                if run_ref not in self._requests:
                    tickets=self._all_tickets()
                    if sum(t.student==student_id for t in tickets.values())>=self.policy.max_per_student:self._reject('student_limit',started)
                    if len(tickets)>=self.policy.max_active+self.policy.max_waiting:self._reject('queue_full',started)
                if self._students[student_id]>=self.policy.max_per_student:self._reject('student_limit',started)
                if run_ref in self._running or any(t.run_ref==run_ref for q in self._queues.values() for t in q):
                    self._reject('invalid_input',started)
                immediate=not self._rotation and self._active<self.policy.max_active
                # 已通过总准入的请求保留位置，热调下限时也不丢弃已接受者。
                if not immediate and run_ref not in self._requests and self._waiting()>=self.policy.max_waiting:self._reject('queue_full',started)
                self._students[student_id]+=1;reserved=True
                if not immediate:
                    if student_id not in self._queues:
                        self._queues[student_id]=deque();self._rotation.append(student_id)
                    self._queues[student_id].append(ticket);queued=True;self._counts['queued']+=1
                    while True:
                        if self._closed:self._reject('closed',started)
                        if self._paused:self._reject('paused',started)
                        if cancel is not None and cancel.is_set():self._reject('cancelled',started)
                        remaining=ticket.deadline-time.monotonic()
                        if remaining<=0:self._reject('queue_timeout',started)
                        if self._rotation[0]==student_id and self._queues[student_id][0] is ticket and self._active<self.policy.max_active:
                            self._remove_waiter(ticket,rotate=True);queued=False;break
                        self._cv.wait(min(remaining,0.05) if cancel is not None else remaining)
                self._active+=1;admitted=True;self._counts['admitted']+=1
                ticket.started=ticket.changed=time.monotonic();ticket.stage='input';self._running[run_ref]=ticket
                waited=round(time.monotonic()-started,4);self._waits.append(waited)
                self._cv.notify_all()  # 一次扩容可同时唤醒多个学生，而不是等首位结束。
            running=time.monotonic()
            try:
                yield waited
            except BaseException:
                with self._cv:self._counts['task_exception']+=1
                raise
            finally:
                with self._cv:
                    self._runs.append(round(time.monotonic()-running,4))
                    self._counts['finished']+=1
        except AdmissionError as exc:
            ticket.outcome='rejected';ticket.error_code=exc.code
            raise
        except BaseException:
            ticket.outcome='interrupted';ticket.error_code='unexpected_failure'
            raise
        finally:
            with self._cv:
                owned=self._requests.get(run_ref)
                if owned is not None and ticket.outcome=='finished':ticket=owned
                if owned is None:self._remember(ticket)
                # 等待队列持有slot自己的ticket，不能用总准入ticket替换引用。
                if queued:
                    waiting_ticket=next(t for t in self._queues[student_id] if t.run_ref==run_ref)
                    self._remove_waiter(waiting_ticket)
                if admitted:self._active-=1;del self._running[run_ref]
                if reserved:
                    self._students[student_id]-=1
                    if self._students[student_id]==0:del self._students[student_id]
                self._cv.notify_all()

    def _waiting(self):
        return sum(len(q) for q in self._queues.values())

    def _remove_waiter(self,ticket,*,rotate=False):
        q=self._queues[ticket.student];q.remove(ticket)
        if not q:
            del self._queues[ticket.student];self._rotation.remove(ticket.student)
        elif rotate:
            self._rotation.remove(ticket.student);self._rotation.append(ticket.student)

    def mark(self,run_ref,stage):
        """由真实问答阶段回调，不能用预计时长伪造模型/工具进度。"""
        from .diagnostics import STAGES
        if stage not in STAGES:raise ValueError('invalid_stage')
        with self._cv:
            for tickets in (self._running,self._requests):
                ticket=tickets.get(run_ref)
                if ticket is not None:ticket.stage=stage;ticket.changed=time.monotonic()

    def reconfigure(self,changes,*,expected_revision):
        """可信管理脚本调用；CAS防覆盖。调低上限只限制后续放行，不杀在途请求。

        已排队请求保留原截止时间；减少队列/学生上限保留已接受者，拒绝新增。
        容量声明、上下文/存储预算不能通过热更新扩大。
        """
        allowed={'max_active','max_waiting','max_per_student','queue_timeout_seconds','max_preparing',
                 'preparation_queue_timeout_seconds','preparation_timeout_seconds','revalidation_timeout_seconds','max_finishing','finishing_queue_timeout_seconds'}
        if not isinstance(changes,dict) or not changes or set(changes)-allowed:
            raise ValueError('invalid_limits')
        with self._cv:
            if type(expected_revision) is not int or expected_revision!=self._revision:
                raise ValueError('stale_policy')
            if self._closed:raise ValueError('closed')
            candidate=ResourcePolicy.model_validate(self.policy.model_dump()|changes)
            self.policy=candidate;self._revision+=1;self._counts['policy_updates']+=1
            self._cv.notify_all()
            return self._revision

    def pause(self):
        """上游运行失败/取消未知后停止新准入，不因本机返回就继续堆模型请求。"""
        with self._cv:self._paused=True;self._cv.notify_all()

    def resume_after_verification(self):
        """仅可信管理员确认上游状态后显式恢复；本模块不自动猜测模型已空闲。"""
        with self._cv:self._paused=False;self._cv.notify_all()

    def close(self):
        """唤醒并拒绝等待者；运行者继续占槽直到结束，不声称已取消上游。"""
        with self._cv:self._closed=True;self._cv.notify_all()

    def snapshot(self):
        def stats(values):
            values=sorted(values)
            return {'samples':len(values),'mean':round(sum(values)/len(values),4) if values else None,
                    'p95':values[max(0,(len(values)*95+99)//100-1)] if values else None}
        with self._cv:
            at=time.monotonic()
            def item(ticket):
                return {'run_ref':ticket.run_ref,'request_ref':ticket.request_ref,'stage':ticket.stage,
                        'seconds':round(at-ticket.submitted,4),'stage_seconds':round(at-ticket.changed,4),
                        'cancel_requested':bool(ticket.cancel is not None and ticket.cancel.is_set())}
            waiting=[t for student in self._rotation for t in self._queues[student]]
            return {'instance_id':self.instance_id,'policy_revision':self._revision,
                    'policy':{k:getattr(self.policy,k) for k in ('max_active','validated_max_active','max_waiting','max_per_student','queue_timeout_seconds','max_preparing','preparation_queue_timeout_seconds','preparation_timeout_seconds','revalidation_timeout_seconds','max_finishing','finishing_queue_timeout_seconds')},
                    'inflight':len(self._all_tickets()),'max_inflight':self.policy.max_active+self.policy.max_waiting,
                    'preparing':len(self._preparing),'preparation_waiting':len(self._prep_queue),
                    'finishing_reserved':len(self._finishing),'finishing_waiting':len(self._finish_queue),'maintenance':self._maintenance,
                    'finishing_requests':[item(t) for t in self._finishing.values()],'finishing_waiting_requests':[item(t) for t in self._finish_queue],
                    'preparing_requests':[item(t) for t in self._preparing.values()],
                    'preparation_waiting_requests':[item(t) for t in self._prep_queue],
                    'preparation_seconds':stats(self._prep_times),'preparation_queue_seconds':stats(self._prep_waits),
                    'active':self._active,'waiting':len(waiting),'tracked_students':len({t.student for t in self._all_tickets().values()}),
                    'paused':self._paused,'closed':self._closed,'counts':dict(self._counts),
                    'running':[item(t) for t in self._running.values()],
                    'oldest_waiting_seconds':round(max((at-t.submitted for t in waiting),default=0),4),
                    'waiting_requests':[item(t) for t in waiting],
                    'recent_results':list(self._recent),
                    'scheduling':'学生轮转；同学生FIFO；总数和学生配额有界',
                    'queue_seconds':stats(self._waits),'application_seconds':stats(self._runs),
                    'execution':execution_projection(self._runtime_samples),
                    'metric_window':64,'scope':'单实例/单进程；无学生ID或消息正文，非模型吞吐容量证明。'}

    def record_runtime(self,metrics):
        from .execution_budget import RuntimeMetrics
        clean=RuntimeMetrics.model_validate(metrics).safe()
        with self._cv:self._runtime_samples.append(clean)

    def record_result(self,code,*,run_ref=None,error_code=None):
        allowed={'saved','replayed','in_progress','failed','not_saved','scope_changed','disk_low','disk_unavailable'}
        if code not in allowed:raise ValueError('未知的资源结果类型。')
        from .diagnostics import ERRORS
        with self._cv:
            self._counts['result_'+code]+=1
            for tickets in (self._running,self._requests):
                if run_ref in tickets:
                    tickets[run_ref].outcome=code
                    tickets[run_ref].error_code=error_code if error_code in ERRORS else 'unclassified' if error_code else None


def disk_check(path,minimum):
    """检查最近已存在父目录所在卷；不创建目录、不清理任何学生记录。"""
    target=Path(path).absolute()
    while not target.exists() and target!=target.parent:target=target.parent
    try:
        free=shutil.disk_usage(target).free
    except OSError:
        raise AdmissionError('disk_unavailable') from None
    if free<minimum:raise AdmissionError('disk_low')
    return free


class ManagedReply(BaseModel):
    admission:str
    message:str
    queue_seconds:float=0.0
    preparation_wait_seconds:float=0.0
    preparation_seconds:float=0.0
    chat:ChatReply | None=None


class ResourceManagedTutor:
    """同一个应用实例共用门控，调度ChatSession真实准备/执行，不重复模型循环。"""
    def __init__(self,*,policy,config_path,state_dir,diagnostics=None):
        self.gate=ResourceGate(policy);self.config_path=config_path;self.state_dir=state_dir;self.diagnostics=diagnostics
        from .execution_control import ModelOwnership
        self.model_owner=ModelOwnership(self.gate.policy.max_active);self.model_lifecycle=None

    def configure_model_lifecycle(self,config,*,idle_seconds=300,timeout=5):
        """可信宿主在启动时一次配置；不联网，控制命令由服务维护入口显式触发。"""
        from .model_lifecycle import OllamaLifecycle
        if self.model_lifecycle is not None:raise ValueError('model_control_already_configured')
        self.model_lifecycle=OllamaLifecycle(config,self.model_owner,idle_seconds=idle_seconds,timeout=timeout)

    def reload_limits(self,path,*,expected_revision):
        """配置文件仅由可信宿主读取；学生和模型不能指定路径。其余策略需重启核对。"""
        policy=load_resource_policy(path)
        before=self.gate.policy.model_dump();after=policy.model_dump()
        dynamic={'max_active','max_waiting','max_per_student','queue_timeout_seconds','max_preparing',
                 'preparation_queue_timeout_seconds','preparation_timeout_seconds','revalidation_timeout_seconds','max_finishing','finishing_queue_timeout_seconds'}
        if any(before[k]!=after[k] for k in before.keys()-dynamic):raise ValueError('restart_required')
        with self.model_owner.lock:
            result=self.gate.reconfigure({k:after[k] for k in dynamic},expected_revision=expected_revision)
            self.model_owner.capacity=policy.max_active
            return result

    def answer(self,request,*,store,handle,tool_bridge=None,learning_memory=None,cancel=None,history_mode='recent',history_before=None,progress=None,
               direct_action=None,material_repository=None,service_executor=None,on_complete=None):
        try:
            request=CourseQuestion.model_validate(request.model_dump());handle=ScopeHandle.model_validate(handle.model_dump())
        except (ValueError,AttributeError):return ManagedReply(admission='invalid_input',message='输入不符合约定，未准入。')
        run_ref=uuid4().hex
        from .diagnostics import reference
        session=None;prepared_seconds=0.0;preparation_wait=0.0;waited=0.0
        def report(stage):
            self.gate.mark(run_ref,stage)
            if progress is not None:progress(stage)
        def finish(chat):
            if chat.runtime_metrics is not None:self.gate.record_runtime(chat.runtime_metrics)
            self.gate.record_result(chat.state,run_ref=run_ref,error_code=chat.error_code)
            reply=ManagedReply(admission='accepted',message='业务结果及保存状态见chat。',queue_seconds=waited,
                 preparation_wait_seconds=preparation_wait,preparation_seconds=prepared_seconds,chat=chat)
            if on_complete is not None:on_complete(reply)
            return reply
        try:
            with self.gate.request(handle.scope.student_id,cancel=cancel,run_ref=run_ref,request_ref=reference(request.request_id)) as ticket:
                disk_check(self.state_dir,self.gate.policy.min_free_bytes);policy=self.gate.policy
                session=ChatSession(request,store=store,handle=handle,config_path=self.config_path,state_dir=self.state_dir,
                    tool_bridge=tool_bridge,learning_memory=learning_memory,conversation_limits=policy.conversation,
                    history_read_limits=policy.history,memory_limits=policy.memory.model_dump(),diagnostics=self.diagnostics,
                    progress=report,history_mode=history_mode,history_before=history_before,direct_action=direct_action,
                    material_repository=material_repository,service_executor=service_executor)
                session.trace.mark('preparation_wait')
                with self.gate.preparation(ticket) as preparation_wait:
                    started=time.monotonic();chat=session.prepare(budget=PreparationBudget(policy.preparation_timeout_seconds,cancel=cancel))
                    if chat is None and direct_action is not None:
                        session.trace.mark('completion_wait')
                        with self.gate.finishing(ticket):
                            chat=session.execute(revalidation_budget=PreparationBudget(policy.revalidation_timeout_seconds,cancel=cancel))
                            prepared_seconds=round(time.monotonic()-started,4);return finish(chat)
                    prepared_seconds=round(time.monotonic()-started,4)
                session.trace.mark('completion_wait')
                with self.gate.finishing(ticket):
                    if chat is not None:return finish(chat)
                    @contextmanager
                    def execution():
                        nonlocal waited
                        session.trace.mark('execution_wait');ticket.slot_attempted=True
                        receipt=SimpleNamespace(started=False,confirmed=False)
                        with self.gate.slot(handle.scope.student_id,cancel=cancel,run_ref=run_ref,request_ref=reference(request.request_id)) as waited:
                            from .execution_control import execution_control
                            try:lease=self.model_owner.claim(run_ref,seconds=3600)
                            except RuntimeError:
                                self.gate.pause();raise AdmissionError('paused') from None
                            try:
                                with execution_control(cancel):yield receipt
                            finally:
                                if cancel is not None and cancel.is_set():self.model_owner.cancel(lease)
                                known=not receipt.started or receipt.confirmed
                                self.model_owner.settle(lease,known_end=known)
                                if not known:self.gate.pause()
                    chat=session.execute(revalidation_budget=lambda:PreparationBudget(policy.revalidation_timeout_seconds,cancel=cancel),execution_scope=execution)
                    return finish(chat)
        except AdmissionError as exc:
            chat=session.abort(exc.code) if session is not None and session.can_abort else session.result if session is not None else None
            if chat is not None:self.gate.record_result(chat.state,run_ref=run_ref,error_code=chat.error_code)
            return self._rejection(exc,chat,preparation_wait,prepared_seconds)

    @staticmethod
    def _rejection(exc,chat,preparation_wait,prepared_seconds):
            messages={'student_limit':'该学生已有未结束的请求，未接受新请求。','queue_full':'总请求名额已满，未接受请求。',
                      'queue_timeout':'排队超过时限，未调用模型。','cancelled':'等待已取消，未调用模型。',
                      'preparation_queue_timeout':'等待资料准备超过时限，未调用模型。',
                      'disk_low':'可用磁盘低于阈值，未调用模型；未自动删除数据。','disk_unavailable':'磁盘状态不可读取，未调用模型。',
                      'paused':'上游运行状态待核实，暂不接受新请求。','closed':'服务正在停止，未接受请求。'}
            return ManagedReply(admission=exc.code,message=messages.get(exc.code,'未准入请求。'),queue_seconds=exc.waited,
                preparation_wait_seconds=preparation_wait,preparation_seconds=prepared_seconds,chat=chat)


def load_resource_policy(path):
    """读取已知的管理配置；限制文件大小，不创建或修改任何配置。"""
    with Path(path).open('rb') as stream:raw=stream.read(16385)
    if len(raw)>16384:raise ValueError('policy_too_large')
    data=json.loads(raw)
    if not isinstance(data,dict) or 'policy' not in data:raise ValueError('invalid_policy')
    return ResourcePolicy.model_validate(data['policy'])


def assess_capacity(*,expected_online=None,peak_requests_per_minute=None,measured_requests_per_minute=None):
    """只给需求/实测比，不根据人数或所谓服务器速度自动增加并发。"""
    for value in (expected_online,peak_requests_per_minute,measured_requests_per_minute):
        if value is not None and (type(value) not in (int,float) or value<0 or value!=value or value==float('inf')):
            raise ValueError('容量输入必须是有限非负数或未知。')
    ratio=None
    if peak_requests_per_minute is not None and measured_requests_per_minute:
        ratio=round(peak_requests_per_minute/measured_requests_per_minute,3)
    return {'expected_online':expected_online,'load_ratio':ratio,'automatic_changes':False,
            'advice':'缺少匹配模型/硬件/课程负载的实测，保持保守配置。' if ratio is None else
                     '平均需求达到或超过所填实测吞吐，需降低负载或评估扩容。' if ratio>=1 else
                     '平均需求低于所填实测吞吐，仍须验证突发、延迟、课程分布和余量，不能保证容量。'}
