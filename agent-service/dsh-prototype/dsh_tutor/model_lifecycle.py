"""模型服务的有限控制调用；由可信宿主显式调度，不在网页轮询时联网。"""
import json,urllib.request,urllib.error
from .providers import provider_for

class OllamaLifecycle:
    """单个共享Ollama模型的暖机/卸载；不创建每学生模型副本。"""
    def __init__(self,config,ownership,*,idle_seconds=300,timeout=5):
        self.provider=provider_for(config)
        if self.provider.kind!='ollama':raise ValueError('lifecycle_unsupported')
        if type(idle_seconds) is not int or not 1<=idle_seconds<=86400 or not 0<timeout<=30:raise ValueError('lifecycle_limits')
        self.owner=ownership;self.model=config['model'];self.idle_seconds=idle_seconds;self.timeout=timeout
        self.last='never';self.state='unknown'
    def _call(self,keep_alive):
        # 禁用代理与重定向，确保命令只到配置的同一提供方，不把控制发送到任意主机。
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args):return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        req=urllib.request.Request(self.provider.base_url[:-3]+'/api/generate',
            data=json.dumps({'model':self.model,'prompt':'','stream':False,'keep_alive':keep_alive}).encode(),headers={'Content-Type':'application/json'})
        try:
            with opener.open(req,timeout=self.timeout) as response:
                raw=response.read(16385)
                if len(raw)>16384:raise ValueError()
                result=json.loads(raw)
                if result.get('done') is not True or result.get('model')!=self.model:raise ValueError()
                # 卸载必须有Ollama原生unload回执，不能只凭HTTP200宣称内存已释放。
                if keep_alive==0 and result.get('done_reason')!='unload':raise ValueError()
        except Exception:raise RuntimeError('model_control_unconfirmed') from None
    def warm(self):
        try:
            with self.owner.maintenance():self._call(self.idle_seconds)
        except RuntimeError as exc:
            if str(exc)!='model_busy':self.last='unconfirmed';self.state='unknown'
            raise
        self.last='warm_confirmed';self.state='warm'
    def unload_if_idle(self):
        try:
            with self.owner.maintenance(minimum_idle=self.idle_seconds):self._call(0)
        except RuntimeError as exc:
            if str(exc)=='model_busy':return 'deferred'
            self.last='unconfirmed';self.state='unknown';raise
        self.last='unload_confirmed';self.state='unloaded';return self.last
    def snapshot(self):
        return {'enabled':'explicit_host_control','state':self.state,'last':self.last,'idle_seconds':self.idle_seconds,
                'hardware_memory':'not_measured','per_student_warm':False}
