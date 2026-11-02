"""HTTP类别独立预算和有界鉴权等待；超时不丢弃仍执行的解析器占用。"""
from collections import deque
from threading import BoundedSemaphore, Event, Lock, Thread
import time
from .service_contracts import ServiceError,HttpBudgetSettings


class HttpBudget:
    """全局有限连接之外再分读/提交/取消；读请求不能耗尽控制请求的配额。"""
    def __init__(self,settings,*,clock=time.monotonic):
        self.settings=HttpBudgetSettings.model_validate(settings.model_dump() if isinstance(settings,HttpBudgetSettings) else settings)
        self.clock=clock;self._lock=Lock();self._windows={}
        self._limits={k:getattr(self.settings,k+'_per_second') for k in ('submit','query','cancel')}
        self._limits['other']=5
        self._resolvers={k:BoundedSemaphore(n) for k,n in [('query',4),('submit',2),('cancel',2),('other',1)]}

    def check(self,kind,*,authenticated=False):
        key=(kind,authenticated);limit=self._limits[kind]*(1 if authenticated else 3)
        with self._lock:
            window=self._windows.setdefault(key,deque(maxlen=limit));at=self.clock()
            while window and window[0]<=at-1:window.popleft()
            if len(window)>=limit:raise ServiceError('rate_limited',429)
            window.append(at)

    def resolve(self,resolver,token,kind):
        """鉴权适配器仍应设置自己的网络期限；本层限制宿主等待与残留线程数量。"""
        slot=self._resolvers[kind]
        if not slot.acquire(False):raise ServiceError('authentication_busy',503)
        finished=Event();box={}
        def work():
            try:box['binding']=resolver(token)
            except Exception:box['failed']=True
            finally:finished.set();slot.release()
        try:Thread(target=work,name='tutor-auth-'+kind,daemon=True).start()
        except BaseException:slot.release();raise
        if not finished.wait(self.settings.resolver_timeout_seconds):raise ServiceError('authentication_timeout',503)
        if box.get('failed'):raise ServiceError('unauthorized',401)
        return box.get('binding')


def request_kind(method,path):
    """仅用于选择资源预算，不能据此授权；真实路径仍经严格路由/身份检查。"""
    path=path.split('?',1)[0]
    if method=='POST' and path.endswith('/cancel'):return 'cancel'
    if method=='POST' and path=='/v1/tutor/operations':return 'submit'
    if method=='GET':return 'query'
    return 'other'
