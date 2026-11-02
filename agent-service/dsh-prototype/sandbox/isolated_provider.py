"""隔离网络内的固定假提供方；不保存提示词、不转发请求、不调用真实模型。"""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import signal
import threading


def create_provider(host='127.0.0.1',port=0):
    lock=threading.Lock();stats={'kind':'isolated_fixed_fake','requests':0,'active':0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path not in ('/health','/stats'):self.send_error(404);return
            with lock:body=json.dumps(dict(stats)).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_POST(self):
            self.request.settimeout(5)
            if self.path!='/v1/chat/completions' or self.headers.get('Transfer-Encoding'):
                self.send_error(400);return
            try:
                lengths=self.headers.get_all('Content-Length') or []
                if len(lengths)!=1 or not lengths[0].isdigit() or not 1<=int(lengths[0])<=65536:raise ValueError()
                raw=self.rfile.read(int(lengths[0]));value=json.loads(raw)
                if not isinstance(value,dict) or value.get('stream') is not True:raise ValueError()
            except (ValueError,RecursionError,OSError):self.send_error(400);return
            with lock:stats['requests']+=1;stats['active']+=1
            try:
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                for delta,reason in [({'role':'assistant','content':'这是隔离环境的固定测试回答，不是真实模型推理。'},None),({},'stop')]:
                    chunk={'id':'isolated','object':'chat.completion.chunk','created':0,'model':'isolated-fake',
                           'choices':[{'index':0,'delta':delta,'finish_reason':reason}]}
                    self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
            except (OSError,TimeoutError):pass
            finally:
                with lock:stats['active']-=1
    class Server(ThreadingHTTPServer):
        daemon_threads=True
        def __init__(self,*args):self.slots=threading.BoundedSemaphore(2);super().__init__(*args)
        def process_request(self,request,address):
            if not self.slots.acquire(False):self.shutdown_request(request);return
            try:super().process_request(request,address)
            except BaseException:self.slots.release();raise
        def process_request_thread(self,request,address):
            request.settimeout(5)
            try:super().process_request_thread(request,address)
            finally:self.slots.release()
    return Server((host,port),Handler)


def main():
    server=create_provider('0.0.0.0',18080);stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:stop.wait()
    finally:server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':main()
