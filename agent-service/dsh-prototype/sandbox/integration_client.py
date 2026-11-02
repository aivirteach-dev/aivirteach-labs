"""模拟平台客户端：显式提交/查进度/取消，不自动重试模型。

网络响应丢失时调用方保存并重用相同request_id及原始正文，再次submit可取回
原操作编号。页面轮询只是读状态，不代表再次提问。
"""
import json
from urllib.request import Request,urlopen
from urllib.error import HTTPError


class ClientError(RuntimeError):
    def __init__(self,status,body):self.status=status;self.body=body;super().__init__(body.get('error','http_error'))


class PlatformClient:
    def __init__(self,base_url,token):self.base=base_url;self.token=token
    def call(self,method,path,body=None):
        headers={'Authorization':'Bearer '+self.token}
        if body is not None:headers['Content-Type']='application/json'
        raw=None if body is None else json.dumps(body,ensure_ascii=False,allow_nan=False).encode()
        req=Request(self.base+path,data=raw,headers=headers,method=method)
        try:
            with urlopen(req,timeout=3) as response:return response.status,json.load(response)
        except HTTPError as exc:raise ClientError(exc.code,json.load(exc)) from None
    def submit(self,body):return self.call('POST','/v1/tutor/operations',body)
    def view(self,operation_id,after=0):return self.call('GET',f'/v1/tutor/operations/{operation_id}?after={after}')[1]
    def cancel(self,operation_id):return self.call('POST',f'/v1/tutor/operations/{operation_id}/cancel',{})[1]
