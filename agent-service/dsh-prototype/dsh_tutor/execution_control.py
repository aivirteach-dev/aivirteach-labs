"""请求取消与模型名额所有权；单进程实现，接口为未来共享协调器保留。"""
from contextvars import ContextVar
from contextlib import contextmanager
from threading import RLock
from uuid import uuid4
import time

_current=ContextVar('tutor_execution_control',default=None)

class ExecutionControl:
    def __init__(self,cancel=None):self.cancel=cancel
    @property
    def requested(self):return bool(self.cancel is not None and self.cancel.is_set())

@contextmanager
def execution_control(cancel):
    token=_current.set(ExecutionControl(cancel))
    try:yield _current.get()
    finally:_current.reset(token)

def current_control():return _current.get()

class ModelOwnership:
    """唯一宿主共享；过期/取消只隔离，绝不能自动退还远端可能仍用的名额。

    进程重启的未知状态由AgentService操作日志和启动核实负责。这里不宣称
    跨进程全局限流；部署多个进程时必须注入同一外部协调器并验收。
    """
    def __init__(self,capacity=1,clock=time.monotonic):
        if type(capacity) is not int or not 1<=capacity<=64:raise ValueError('capacity')
        self.capacity=capacity;self.clock=clock;self.owner=uuid4().hex;self.lock=RLock()
        self.active={};self.quarantined=False;self.control_busy=False;self.last_activity=clock()
    def claim(self,request_ref,*,seconds):
        with self.lock:
            self.snapshot()
            if self.quarantined or self.control_busy:raise RuntimeError('model_verification_required')
            if not isinstance(request_ref,str) or not 1<=len(request_ref)<=128 or type(seconds) not in (int,float) or not 0<seconds<=3600:raise ValueError('model_lease')
            if request_ref in (v['request'] for v in self.active.values()):raise RuntimeError('model_duplicate_owner')
            if len(self.active)>=self.capacity:raise RuntimeError('model_capacity')
            key=uuid4().hex;self.active[key]={'request':request_ref,'deadline':self.clock()+seconds,'cancel':False}
            return key
    def renew(self,key,*,seconds):
        with self.lock:
            row=self.active[key]
            if self.quarantined or row['cancel'] or self.clock()>=row['deadline']:self.quarantined=True;raise RuntimeError('model_verification_required')
            if type(seconds) not in (int,float) or not 0<seconds<=3600:raise ValueError('model_lease')
            row['deadline']=self.clock()+seconds
    def cancel(self,key):
        with self.lock:self.active[key]['cancel']=True
    def settle(self,key,*,known_end):
        with self.lock:
            if key not in self.active:raise RuntimeError('model_unknown_lease')
            self.active.pop(key);self.last_activity=self.clock()
            if known_end is not True:self.quarantined=True
    def verify_idle(self,*,upstream_idle):
        with self.lock:
            if upstream_idle is not True or self.active or self.control_busy:raise RuntimeError('model_verification_required')
            self.quarantined=False;self.last_activity=self.clock()
    def snapshot(self):
        with self.lock:
            if any(self.clock()>=r['deadline'] for r in self.active.values()):self.quarantined=True
            return {'scope':'single_process','active':len(self.active),'capacity':self.capacity,
                    'quarantined':self.quarantined,'control_busy':self.control_busy,'runtime_policy':'per_request_isolated'}
    @contextmanager
    def maintenance(self,*,minimum_idle=0):
        """暖机/卸载和推理互斥；锁内只改状态，网络IO在锁外有限执行。"""
        with self.lock:
            self.snapshot()
            if self.active or self.quarantined or self.control_busy or self.clock()-self.last_activity<minimum_idle:raise RuntimeError('model_busy')
            self.control_busy=True
        try:yield
        except BaseException:
            with self.lock:self.quarantined=True
            raise
        finally:
            with self.lock:self.control_busy=False;self.last_activity=self.clock()
