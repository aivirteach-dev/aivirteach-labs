"""容器存活探针；只读本机/health，不查询学生数据或调用模型。"""
import urllib.request

opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('http://127.0.0.1:18792/health',timeout=2) as response:
    if response.status!=200:raise SystemExit(1)
