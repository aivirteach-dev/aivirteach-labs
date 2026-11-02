"""独立异步应用入口：有界工作量、双保存交付、重启对账；不另写模型循环。

真实身份、课程发布版本和工具绑定由宿主注入。单个操作日志只有一个服务
进程可写；跨服务器总模型配额仍需真实部署时统一协调。
"""
from concurrent.futures import ThreadPoolExecutor,wait
from contextlib import contextmanager
from collections import deque
from dataclasses import dataclass,field
from threading import Event,Thread,RLock
from uuid import uuid4
import hashlib,json,re,time
from .contracts import CourseQuestion
from .service_contracts import API_VERSION,REQUEST_ID,Submission,ServiceError,TokenUsage,OperationView
from .platform_adapter import adapt_platform
from .storage import StorageError


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def binding_refs(binding):
    h=binding.handle;task=getattr(binding.store,'task_id',None)
    owner=digest([h.scope.student_id,h.scope.learning_context_id,task])
    return owner,digest([h.model_dump(),task])


@dataclass
class _LiveOperation:
    operation_id:str
    student:str
    deadline:float
    cancel:Event=field(default_factory=Event)
    future:object=None
    stage:str='queued'
    model_started:bool=False
    deadline_exceeded:bool=False
    submitted:float=field(default_factory=time.monotonic)


class AgentService:
    def __init__(self,tutor,journal):
        self.tutor=tutor;self.journal=journal;self.settings=journal.settings
        self.instance=uuid4().hex;self._lock=RLock();self._active={};self._issues={}
        self._delivery_times=deque(maxlen=64);self._operation_times=deque(maxlen=64)
        self._direct_saved=0;self._direct_tool_calls=0
        self._ready=False;self._closing=False;self._closed=False;self._stop=Event();self._maintenance=False
        # 每次启动都先暂停，包括看似干净退出；不能假定远端模型已停止。
        self.tutor.gate.pause();self.tutor.model_owner.quarantined=True;self.journal.begin_session(self.instance)
        self._pool=ThreadPoolExecutor(max_workers=self.settings.max_inflight,thread_name_prefix='tutor-operation')
        self._watcher=Thread(target=self._watch,name='tutor-deadlines',daemon=True);self._watcher.start()

    def _check_binding(self,binding):
        try:
            h=binding.handle
            if binding.store.current(student_id=h.scope.student_id,learning_context_id=h.scope.learning_context_id)!=h:
                raise ServiceError('scope_changed')
            c=binding.course
            if (c.course_id,c.version,c.lesson_id)!=(h.scope.course_id,h.scope.course_version,h.scope.lesson_id):
                raise ServiceError('course_mismatch',422)
        except StorageError:raise ServiceError('source_unavailable',503) from None

    def _normalize(self,binding,raw):
        try:
            submission=Submission.model_validate(raw.model_dump() if isinstance(raw,Submission) else raw)
            if submission.profile=='aivirteach':request=adapt_platform(submission.payload,lab_id=binding.lab_id)
            elif submission.profile=='course_actions_v1':
                from .direct_actions import ActionRequest
                request=ActionRequest.model_validate(submission.payload).question()
                if submission.history_selection.mode!='none':raise ValueError('actions_require_no_history')
            else:request=CourseQuestion.model_validate(submission.payload)
            if not re.fullmatch(REQUEST_ID,request.request_id):raise ValueError()
        except (ValueError,TypeError,AttributeError):raise ServiceError('invalid_submission',422) from None
        if request.course!=binding.course:raise ServiceError('course_mismatch',422)
        owner,scope=binding_refs(binding)
        if binding.request_window is not None:
            if submission.request_window_id is None:raise ServiceError('invalid_request_window',422)
            self.journal.validate_window(binding.request_window,owner,request.request_id,submission.request_window_id)
        elif submission.request_window_id is not None or re.match(r'^[0-9a-f]{64}\.',request.request_id):raise ServiceError('invalid_request_window',422)
        checksum=digest({'question':request.model_dump(),'history':submission.history_selection.model_dump(),
            'profile':submission.profile,'window':submission.request_window_id,'scope':scope,'tool_bridge':binding.tool_bridge})
        return submission,request,owner,scope,checksum

    def verify_ready(self,*,upstream_idle):
        """仅可信管理员控制面调用，HTTP/模型无此入口；先核实上游，再逐条对账。"""
        with self._lock:
            if self._closing or self._closed:raise ServiceError('service_closed',503)
            if upstream_idle is not True or self._active:raise ServiceError('verification_required',503)
            counts=self.journal.counts()
            if self._issues or any(counts.get(s,0) for s in ('queued','running','recovery_required')):
                raise ServiceError('reconciliation_required',503)
            self.tutor.model_owner.verify_idle(upstream_idle=True)
            self.tutor.gate.resume_after_verification();self._ready=True

    def submit(self,binding,raw):
        self._check_binding(binding);submission,request,owner,scope,checksum=self._normalize(binding,raw)
        with self._lock:
            if self._closed or self._closing:raise ServiceError('service_closed',503)
            old=self.journal.find(owner,request.request_id)
            if old:
                if old['digest']!=checksum:raise ServiceError('request_conflict')
                return self.view(binding,old['operation_id']),False
            if self._maintenance:raise ServiceError('maintenance_busy',503)
            if not self._ready or self.tutor.gate.snapshot()['paused']:raise ServiceError('verification_required',503)
            policy=self.tutor.gate.policy
            if len(self._active)>=min(self.settings.max_inflight,policy.max_active+policy.max_waiting):raise ServiceError('overloaded',429)
            if sum(x.student==binding.handle.scope.student_id for x in self._active.values())>=policy.max_per_student:
                raise ServiceError('student_busy',429)
            op=uuid4().hex
            row,created=self.journal.claim(operation_id=op,owner_ref=owner,scope_ref=scope,request_id=request.request_id,
                digest=checksum,profile=submission.profile,instance=self.instance,window=binding.request_window)
            if not created:return self.view(binding,row['operation_id']),False
            live=_LiveOperation(op,binding.handle.scope.student_id,time.monotonic()+self.settings.operation_timeout_seconds)
            self._active[op]=live
            try:
                live.future=self._pool.submit(self._work,live,binding,submission,request)
                live.future.add_done_callback(lambda f:self._done(live))
            except Exception:
                self._active.pop(op,None);self._pause()
                try:self.journal.finish(op,self.instance,state='failed',code='dispatch_failed')
                except ServiceError:self._issues[op]='delivery_not_saved'
                raise ServiceError('dispatch_failed',503) from None
        return self.view(binding,op),True

    def _pause(self):
        self._ready=False;self.tutor.gate.pause()

    def _work(self,live,binding,submission,request):
        def progress(stage):
            live.stage=stage
            if stage=='model':live.model_started=True
            try:self.journal.progress(live.operation_id,self.instance,stage)
            except ServiceError:
                self._pause();raise  # RequestTrace记录progress_unavailable，不重复推理。
        delivered=False
        def deliver(reply):
            nonlocal delivered
            progress('delivery_save') if reply.chat is not None and reply.chat.state in ('saved','replayed') else None
            self._deliver(live,binding,request,action,reply);delivered=True
        try:
            from .direct_actions import ActionRequest
            action=ActionRequest.model_validate(submission.payload).action if submission.profile=='course_actions_v1' else None
            reply=self.tutor.answer(request,store=binding.store,handle=binding.handle,
                direct_action=action,material_repository=binding.material_repository,service_executor=binding.service_executor,
                tool_bridge=binding.tool_bridge,cancel=live.cancel,
                history_mode=submission.history_selection.mode,history_before=submission.history_selection.before,progress=progress,on_complete=deliver)
            if not delivered:deliver(reply)
        except BaseException:
            # 不能确定模型是否完成时不返回答案、不重跑。原异常文本不入操作日志。
            self._pause()
            code='run_state_unknown' if live.model_started else 'application_failure'
            try:self.journal.finish(live.operation_id,self.instance,state='recovery_required' if live.model_started else 'failed',code=code)
            except ServiceError:self._issues[live.operation_id]='delivery_not_saved'

    def _deliver(self,live,binding,request,action,reply):
        """在收尾预留仍持有时核对源聊天并保存交付；只在双保存确认后可返回。"""
        chat=reply.chat
        if chat is not None and chat.state in ('saved','replayed'):
            if action is not None and action.kind=='learning_goal' and chat.result is None:
                # 聊天原文不含完整修订回执，不能把旧目标恢复为当前目标。
                self.journal.finish(live.operation_id,self.instance,state='failed',code='source_changed');return
            result={'answer':chat.answer,'answer_status':chat.answer_status,'run_id':chat.run_id,
                'replayed':chat.state=='replayed','detail':chat.result.model_dump() if chat.result is not None else None,
                'usage':TokenUsage().model_dump(),'evidence_scope':'original_run_only',
                'persistence':{'agent_chat':'saved','operation_record':'saved','platform':'unconfirmed'}}
            # 不仅相信返回状态：源聊天必须已确认保存，操作日志再保存后才交付。
            source=binding.store.get_request(binding.handle,request.request_id)
            if source is None or source.status!='completed' or source.answer!=chat.answer:raise ServiceError('source_unavailable',503)
            saved_at=time.monotonic()
            self.journal.finish(live.operation_id,self.instance,state='succeeded',result=result)
            with self._lock:
                self._delivery_times.append(time.monotonic()-saved_at)
                if action is not None:
                    self._direct_saved+=1;self._direct_tool_calls+=chat.result.tool_calls if chat.result is not None else 0
        else:
            code=chat.error_code if chat is not None and chat.error_code else reply.admission
            from .diagnostics import ERRORS
            allowed=ERRORS|{'disk_low','disk_unavailable','student_limit','queue_full','invalid_input'}
            code=code if code in allowed else 'application_failure'
            recovery=bool(chat is not None and chat.recovery_required)
            if recovery or (live.model_started and code in ('run_failed','provider_outcome_unknown','cancel_unconfirmed')):self._pause()
            self.journal.finish(live.operation_id,self.instance,state='recovery_required' if recovery else 'failed',code=code)
    def _done(self,live):
        with self._lock:
            self._active.pop(live.operation_id,None)
            self._operation_times.append(time.monotonic()-live.submitted)

    def _watch(self):
        while not self._stop.wait(.05):
            with self._lock:jobs=list(self._active.values())
            for live in jobs:
                if time.monotonic()>=live.deadline and not live.deadline_exceeded:
                    live.deadline_exceeded=True;live.cancel.set()
                    if live.model_started:self._pause()
                    try:
                        # deadline与用户取消不同，事件里明确原因；占用仍待实际调用结束。
                        owner=None
                        with self.journal.connection() as db:
                            row=db.execute('SELECT owner_ref FROM operations WHERE operation_id=?',(live.operation_id,)).fetchone()
                            if row:owner=row[0]
                        if owner:self.journal.cancel(live.operation_id,owner,'operation_deadline')
                    except ServiceError:self._pause()

    def _row(self,binding,operation_id):
        self._check_binding(binding);owner,scope=binding_refs(binding)
        row=self.journal.get(operation_id,owner)
        if row['scope_ref']!=scope:raise ServiceError('scope_changed')
        return row

    def view(self,binding,operation_id,*,after=0):
        row=self._row(binding,operation_id)
        result=None;state=row['state'];code=row['error_code']
        if operation_id in self._issues:state='recovery_required';code=self._issues[operation_id]
        if state=='succeeded':
            try:source=binding.store.get_request(binding.handle,row['request_id'])
            except StorageError:raise ServiceError('source_unavailable',503) from None
            try:cached=json.loads(row['result_json'])
            except (ValueError,TypeError):raise ServiceError('journal_unavailable',503) from None
            if source is None or source.status!='completed' or source.answer!=cached['answer'] or source.run_id!=cached.get('run_id'):
                self.journal.redact(operation_id);state='failed';code='source_changed'
            else:
                detail=cached.get('detail') or {}
                if detail.get('direct_action_kind')=='learning_goal':
                    try:current_epoch=binding.store.learning_state(binding.handle)[1]
                    except (StorageError,AttributeError):raise ServiceError('source_unavailable',503) from None
                    if current_epoch!=detail.get('memory_revision'):
                        self.journal.redact(operation_id);state='failed';code='source_changed'
                    else:result=cached
                else:result=cached
        with self._lock:live=self._active.get(operation_id)
        return OperationView.model_validate({'api_version':API_VERSION,'operation_id':operation_id,'request_id':row['request_id'],
            'state':state,'stage':row['stage'],'error_code':code,'result':result,
            'cancel_requested':bool(row['cancel_requested']),
            'execution_cancellation':('not_requested' if not row['cancel_requested'] else 'late_success' if state=='succeeded' else 'not_dispatched' if state=='failed' and row['error_code'] in ('cancelled','cancelled_before_dispatch') else 'outcome_unknown' if state in ('failed','recovery_required') else 'requested'),
            'cancellation':'completed_after_cancel' if row['cancel_requested'] and state=='succeeded' else
                           'requested_unconfirmed' if row['cancel_requested'] and state not in ('failed','succeeded') else
                           'requested_unconfirmed' if row['cancel_requested'] and row['error_code'] in ('run_failed','cancel_unconfirmed','provider_outcome_unknown') else
                           'not_running' if row['cancel_requested'] else 'not_requested',
            'deadline_exceeded':bool(row['deadline_exceeded'] or live and live.deadline_exceeded),
            'feedback':{'elapsed_seconds':round(max(0,time.monotonic()-live.submitted),3) if live is not None else None,
                'stage':row['stage'],'queue_timeout_seconds':self.tutor.gate.policy.queue_timeout_seconds,'estimated_remaining_seconds':None,
                'note':'已等待/处理时间不是剩余时间；模型与工具未确认结束时不能据此释放许可。'},
            'events':self.journal.events(operation_id,after),'created_at':row['created_at'],'updated_at':row['updated_at'],
            'retry_policy':'reuse_same_request_id; no_automatic_inference_retry',
            'source':self.settings.profile}).model_dump()

    def cancel(self,binding,operation_id):
        row=self._row(binding,operation_id);owner,_=binding_refs(binding)
        self.journal.cancel(operation_id,owner)
        with self._lock:
            live=self._active.get(operation_id)
            if live is not None:live.cancel.set()
        return self.view(binding,operation_id)

    def reconcile(self,binding,operation_id,*,upstream_idle):
        """可信控制面显式对账；只读已存回答或结束遗留pending，不调用模型。"""
        if upstream_idle is not True:raise ServiceError('verification_required',503)
        owner,scope=binding_refs(binding);row=self.journal.get(operation_id,owner)
        with self._lock:
            if self._active:raise ServiceError('verification_required',503)
            # 对账期间关闭新准入；结束后仍需显式verify_ready确认。
            self._pause()
        if row['state'] in ('succeeded','failed') and operation_id not in self._issues:return
        if row['scope_ref']!=scope:raise ServiceError('scope_changed')
        try:
            current=binding.store.current(student_id=binding.handle.scope.student_id,learning_context_id=binding.handle.scope.learning_context_id)
            if current!=binding.handle:
                self.journal.finish(operation_id,self.instance,state='failed',code='scope_changed',reconcile=True)
            else:
                source=binding.store.get_request(binding.handle,row['request_id'])
                if row['profile']=='course_actions_v1':
                    if source is not None and source.status=='pending':binding.store.finish(binding.handle,row['request_id'],error_code='interrupted')
                    self.journal.finish(operation_id,self.instance,state='failed',code='source_changed',reconcile=True)
                elif source is not None and source.status=='completed':
                    # 原始usage/工具轨迹丢失时不重构伪证据；仅返回历史正文和原run_id。
                    result={'answer':source.answer,'answer_status':source.answer_status,'run_id':source.run_id,'replayed':True,
                        'detail':None,'usage':TokenUsage().model_dump(),'evidence_scope':'original_run_only',
                        'persistence':{'agent_chat':'saved','operation_record':'saved','platform':'unconfirmed'}}
                    self.journal.finish(operation_id,self.instance,state='succeeded',result=result,reconcile=True)
                else:
                    if source is not None and source.status=='pending':binding.store.finish(binding.handle,row['request_id'],error_code='interrupted')
                    self.journal.finish(operation_id,self.instance,state='failed',code='interrupted',reconcile=True)
        except StorageError:raise ServiceError('source_unavailable',503) from None
        self._issues.pop(operation_id,None)

    @staticmethod
    def _stats(values):
        values=sorted(values)
        return {'samples':len(values),'mean':sum(values)/len(values) if values else None,
                'p95':values[max(0,(len(values)*95+99)//100-1)] if values else None}

    @contextmanager
    def maintenance(self):
        """本机控制面在整体无在途时暂停准入；有工作立即延后，不堆积后台线程。"""
        with self._lock:
            if self._maintenance or self._active or self._closing:raise ServiceError('maintenance_busy',503)
            self._maintenance=True
        try:
            with self.tutor.gate.maintenance():yield
        finally:
            with self._lock:self._maintenance=False

    def maintain_model(self,action):
        """仅可信宿主的共享模型控制；不开放学生HTTP或MCP，忙时立即延后。"""
        if action not in ('warm','unload_if_idle'):raise ValueError('invalid_model_action')
        control=self.tutor.model_lifecycle
        if control is None:raise ServiceError('model_control_not_configured',503)
        try:
            with self.maintenance():
                if action=='warm':control.warm();return 'warm_confirmed'
                return control.unload_if_idle()
        except ServiceError:raise
        except RuntimeError as exc:
            if str(exc)=='model_busy':return 'deferred'
            self._pause();raise ServiceError('model_control_unconfirmed',503) from None

    def metrics(self):
        with self._lock:
            active=len(self._active)
            timings={'delivery_save':self._stats(self._delivery_times),'operation_total':self._stats(self._operation_times)}
        return {'state':'available','source':self.settings.profile,'ready':self._ready and not self.tutor.gate.snapshot()['paused'],
            'active_operations':active,'max_inflight':self.settings.max_inflight,'counts':self.journal.counts(),
            'model_lifecycle':self.tutor.model_lifecycle.snapshot() if self.tutor.model_lifecycle else {'enabled':False,'state':'not_configured'},'model_ownership':self.tutor.model_owner.snapshot(),'timings':timings,'direct_saved':self._direct_saved,'direct_tool_calls':self._direct_tool_calls,
            'delivery_issues':len(self._issues),'closing':self._closing,'usage':'unknown','scope':'单个操作日志/单进程'}

    def capabilities(self):
        return {'api_version':API_VERSION,'transport':'submit_and_poll','source':self.settings.profile,
            'native_aivirteach_drop_in':False,'authentication':'trusted_binding_resolver_required',
            'profiles':['course_snapshot','aivirteach','course_actions_v1'],'context_modes':['recent','earlier','none'],'direct_actions':['material_block','learning_goal','service_status'],'unsupported_platform_context':['nonempty history','nonempty learner_state','nonempty diagnostic_scope'],
            'running_cancel':'public_agent_cancel_bridge; remote_confirmation_required','automatic_retry':False,'startup_verification_required':True,'request_issued_floor':self.settings.request_issued_floor,'request_windows':'trusted_host_expiry_v1; explicit_journal_v2_migration','retention':'offline_preview_and_apply_only',
            'usage':'unknown','platform_persistence':'unconfirmed','events_per_operation':self.settings.events_per_operation,
            'http_budget':self.settings.http_budget.model_dump(),'polling':{'minimum_seconds':1.0,'maximum_seconds':8.0,'jitter':True,'pause_when_hidden':True},
            'max_body_bytes':self.settings.max_body_bytes,'max_inflight':self.settings.max_inflight,
            'operation_timeout_seconds':self.settings.operation_timeout_seconds}

    def close(self,timeout=3):
        """温和停止；未退出的线程仍保留所有权锁，不能让新实例与旧推理并行。"""
        with self._lock:
            if self._closed:return True
            self._closing=True;self._ready=False;self.tutor.gate.close();jobs=list(self._active.values())
            for live in jobs:live.cancel.set()
            futures=[x.future for x in jobs if x.future is not None]
        _,pending=wait(futures,timeout=timeout)
        if pending:return False
        self._stop.set();self._watcher.join(1);self._pool.shutdown(wait=True)
        self.journal.close();self._closed=True;return True
