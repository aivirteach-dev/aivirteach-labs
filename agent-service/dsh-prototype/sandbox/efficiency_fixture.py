"""本机固定假模型协议服务；只回固定文本/工具请求，不转发任何外部地址。"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json,time,threading

@contextmanager
def fake_provider(mode='text',delay=0):
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            selected=mode.get('value','text') if isinstance(mode,dict) else mode
            if selected=='failure':self.send_response(500);self.end_headers();return
            if isinstance(mode,dict) and mode.get('hold') is not None:
                mode['entered'].set()
                if not mode['hold'].wait(10):
                    self.send_response(503);self.end_headers();return
            time.sleep(delay);self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            if selected=='tool':
                delta={'role':'assistant','tool_calls':[{'index':0,'id':'call-'+str(len(received)),'type':'function',
                     'function':{'name':received[-1]['tools'][0]['function']['name'],'arguments':'{"service":"n8n"}'}}]}
                finish='tool_calls'
            else:delta={'role':'assistant','content':'x'*10000 if selected=='large' else 'probe'};finish='stop'
            for value,reason in [(delta,None),({},finish)]:
                chunk={'id':'probe','object':'chat.completion.chunk','created':0,'model':'test',
                    'choices':[{'index':0,'delta':value,'finish_reason':reason}]}
                try:self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
                except (BrokenPipeError,ConnectionResetError):return
            try:self.wfile.write(b'data: [DONE]\n\n')
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/v1',received
    finally:server.shutdown();server.server_close();thread.join(2)
