"""用实际DSH运行时验证MCP发现；不发模型请求、不运行任意工具。"""
import json
import os
import pwd
import sys
import tempfile
import time
import unittest
from pathlib import Path
from deepseek_harness import DeepSeekHarness
from dsh_tutor.runtime import ROOT, load_config, render_patch
from dsh_tutor.tools import AuditLog
from sandbox.mcp_acceptance import bridge_for


class DshMcpStartupTests(unittest.TestCase):
    def test_installed_runtime_discovers_python_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            bridge=bridge_for('course','n8n','stopped',request_id='startup',instance_id='fake-vm')
            context=bridge['context']
            context.update(execution_user=pwd.getpwuid(os.geteuid()).pw_name,audit_path=str(base/'audit.jsonl'))
            context['policy'].update(run_id='startup',deadline_unix=time.time()+30)
            path=base/'context.json'
            path.write_text(json.dumps(context))
            plugin={'id':'mcp-course-tools','name':'@deepseek-ai/dsh-mcp-client',
                    'config':{'serverName':'course_tools','transport':'stdio','command':sys.executable,
                              'args':['-m','sandbox.mcp_server',str(path)],'cwd':str(ROOT),
                              'env':{'PYTHONPATH':str(ROOT),'PYTHONDONTWRITEBYTECODE':'1'},
                              'toolCallTimeoutMs':5000,'failOnStartupError':True,'reconnect':{'enabled':False}}}
            config=load_config(ROOT/'config.json')
            patch=base/'patch.yml'
            patch.write_text(render_patch(config,bridge_plugin=plugin))
            with DeepSeekHarness(dsh_home=str(base/'home'),cwd=str(base),profile='sdk-minimal',
                                 patches=(str(patch),),provider='ollama-local',model=config['model'],
                                 env={'OLLAMA_PLACEHOLDER_KEY':'local-ollama'},initialize_timeout_seconds=20) as harness:
                harness.start()
                rows=AuditLog(base/'audit.jsonl').read()
                self.assertTrue(any(x['type']=='discovery' for x in rows))
                self.assertFalse(any(x['type']=='execution' for x in rows))
