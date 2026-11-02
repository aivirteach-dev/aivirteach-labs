"""资料准备的协作式时间/取消预算；SQLite可使用同一截止时间中断查询。

不通过超时丢弃仍在运行的线程。适配器不响应时继续占用准备和总请求名额，
返回后拒绝旧结果；这能限制资源扩散，但不能替代远端数据库/文件接口的硬超时。
"""
from contextlib import contextmanager
from contextvars import ContextVar
import time
import math

storage_deadline=ContextVar('dsh_storage_deadline',default=None)


class PreparationStopped(RuntimeError):
    def __init__(self,code):self.code=code;super().__init__(code)


class PreparationBudget:
    def __init__(self,seconds,*,cancel=None):
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0<seconds<=30:
            raise ValueError('invalid_preparation_budget')
        self.deadline=time.monotonic()+seconds;self.cancel=cancel

    def check(self):
        if self.cancel is not None and self.cancel.is_set():raise PreparationStopped('cancelled')
        if time.monotonic()>=self.deadline:raise PreparationStopped('preparation_timeout')

    @contextmanager
    def storage_scope(self):
        self.check();previous=storage_deadline.get()
        token=storage_deadline.set(min(previous,self.deadline) if previous is not None else self.deadline)
        try:yield
        finally:storage_deadline.reset(token)


class ExecutionAdmissionError(RuntimeError):
    """执行许可尚未取得；由ChatSession交还宿主做原子pending中止。"""
