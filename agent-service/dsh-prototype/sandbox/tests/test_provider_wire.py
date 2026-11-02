"""用本机假HTTP服务验证已安装DSH实际发出的参数；不调用Mac或真实模型。"""
import json
import os
import pwd
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

from dsh_tutor.runtime import ROOT,load_config,run_prompt


class ProviderWireTests(unittest.TestCase):
    def test_installed_dsh_sends_explicit_no_thinking_and_output_cap(self):
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append(request)
                self.send_response(200)
                if request.get('stream'):
                    self.send_header('Content-Type','text/event-stream');self.end_headers()
                    for delta,finish in [({'role':'assistant','content':'probe'},None),({},'stop')]:
                        event={'id':'probe','object':'chat.completion.chunk','created':0,'model':'test',
                               'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
                        self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode())
                    self.wfile.write(b'data: [DONE]\n\n')
                else:
                    self.send_header('Content-Type','application/json');self.end_headers()
                    self.wfile.write(json.dumps({'id':'probe','object':'chat.completion','created':0,'model':'test',
                        'choices':[{'index':0,'message':{'role':'assistant','content':'probe'},'finish_reason':'stop'}]}).encode())
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config=load_config(ROOT/'config.json')
                config.update(execution_user=pwd.getpwuid(os.geteuid()).pw_name,
                              ollama_base_url=f'http://127.0.0.1:{server.server_port}/v1',
                              reasoning_effort='off',reasoning_off_value='none',total_timeout_seconds=20)
                path=Path(directory)/'config.json';path.write_text(json.dumps(config))
                result=run_prompt('Only reply probe.',config_path=path,state_dir=Path(directory)/'runs')
                self.assertEqual(result['answer'],'probe')
                self.assertEqual(len(received),1)
                self.assertEqual(received[0].get('reasoning_effort'),'none')
                self.assertEqual(received[0].get('max_tokens'),config['max_output_tokens'])
                self.assertNotIn('developer',[x['role'] for x in received[0]['messages']])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)

    def test_too_small_context_fails_before_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            config=load_config(ROOT/'config.json')
            config['context_window']=4096
            path=Path(directory)/'config.json';path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError,'8192'):
                load_config(path)
