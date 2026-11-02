"""只读进度查询节奏；不提交、重试或取消模型工作，调用方控制是否在前台。"""
import random
from pydantic import BaseModel,ConfigDict,Field


class PollingSettings(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    minimum_seconds:float=Field(default=1.0,ge=.5,le=10)
    maximum_seconds:float=Field(default=8.0,ge=1,le=30)


class ProgressPoller:
    """无变化时退避，进度变化后重置；后台/终态返回None停止计时器。"""
    def __init__(self,settings=None,*,random_value=random.random):
        self.settings=PollingSettings.model_validate(settings or {})
        if self.settings.maximum_seconds<self.settings.minimum_seconds:raise ValueError('invalid_polling_settings')
        self._random=random_value;self._last=None;self._unchanged=0

    def delay(self,*,cursor=None,visible=True,terminal=False,retry_after=None):
        if not visible or terminal:return None
        self._unchanged=min(self._unchanged+1,5) if cursor==self._last else 0;self._last=cursor
        base=min(self.settings.maximum_seconds,self.settings.minimum_seconds*1.5**self._unchanged)
        delay=min(self.settings.maximum_seconds,base*(.85+.3*self._random()))
        if retry_after is not None:
            if type(retry_after) not in (int,float) or not 0<=retry_after<=60:raise ValueError('invalid_retry_after')
            delay=max(delay,retry_after)
        return round(delay,3)
