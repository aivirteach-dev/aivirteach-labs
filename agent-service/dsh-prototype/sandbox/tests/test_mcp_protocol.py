"""真实stdio协议测试：不请求模型；开发账号仅运行固定模拟后端。"""
import json
import os
import pwd
import sys
import tempfile
import time
import unittest
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dsh_tutor.tools import TOOL_NAME, AuditLog
from dsh_tutor.runtime import ROOT


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_discovery_validation_and_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            audit=base/'audit.jsonl'
            context={'execution_user':pwd.getpwuid(os.geteuid()).pw_name,'audit_path':str(audit),
                     'policy':{'request_id':'test','run_id':'protocol','student_id':'fake','instance_id':'fake-vm',
                               'course_id':'course','course_version':'v1','allowed_tools':[TOOL_NAME],
                               'allowed_services':['n8n'],'max_attempts':2,'timeout_seconds':2.0,
                               'deadline_unix':time.time()+20},'service_states':{'n8n':'stopped'}}
            path=base/'context.json'
            path.write_text(json.dumps(context))
            server=StdioServerParameters(command=sys.executable,args=['-m','sandbox.mcp_server',str(path)],
                                         cwd=str(ROOT),env={'PYTHONPATH':str(ROOT)})
            async with stdio_client(server) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tools=await session.list_tools()
                    self.assertEqual([x.name for x in tools.tools],[TOOL_NAME])
                    good=await session.call_tool(TOOL_NAME,{'service':'n8n'})
                    self.assertFalse(good.isError)
                    self.assertEqual(json.loads(good.content[0].text)['evidence']['source'],'simulation')
                    invalid=await session.call_tool(TOOL_NAME,{'service':'n8n','instance_id':'other'})
                    self.assertTrue(invalid.isError)
                    self.assertEqual(json.loads(invalid.content[0].text)['status'],'invalid')
                    over=await session.call_tool(TOOL_NAME,{'service':'n8n'})
                    self.assertTrue(over.isError)
                    self.assertEqual(json.loads(over.content[0].text)['status'],'budget_exhausted')
            self.assertEqual(sum(x['type']=='execution' for x in AuditLog(audit).read()),1)
