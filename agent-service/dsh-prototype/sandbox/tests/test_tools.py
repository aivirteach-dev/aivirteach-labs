"""不调用模型，验证执行前拒绝、预算、证据、超时及实例隔离。"""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from dsh_tutor.tools import AuditLog, ToolGateway, ToolPolicy, TOOL_NAME
from sandbox.mock_tools import MockExecutor


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy = ToolPolicy(request_id='r', run_id='run', student_id='student-a',
                                 instance_id='instance-a', course_id='course', course_version='v1',
                                 allowed_tools=[TOOL_NAME], allowed_services=['n8n'],
                                 max_attempts=2, timeout_seconds=0.03, deadline_unix=time.time()+10)
        self.backend = MockExecutor({'n8n':'stopped'})
        self.audit = AuditLog(Path(self.tmp.name)/'events.jsonl')
        self.gateway = ToolGateway(self.policy,self.audit,self.backend)

    async def test_unauthorized_zero_execution(self):
        policy=self.policy.model_copy(update={'allowed_tools':[]})
        row=await ToolGateway(policy,self.audit,self.backend).call(TOOL_NAME,{'service':'n8n'})
        self.assertEqual(row['status'],'denied')
        self.assertEqual(self.backend.executions,0)

    async def test_identity_in_arguments_is_rejected(self):
        row=await self.gateway.call(TOOL_NAME,{'service':'n8n','instance_id':'other'})
        self.assertEqual(row['status'],'invalid')
        self.assertEqual(self.backend.executions,0)

    async def test_other_resource_zero_execution(self):
        row=await self.gateway.call(TOOL_NAME,{'service':'other'})
        self.assertEqual(row['status'],'denied')
        self.assertEqual(self.backend.executions,0)

    async def test_invalid_attempts_consume_persistent_budget(self):
        await self.gateway.call('unknown',{})
        await self.gateway.call(TOOL_NAME,{'bad':'value'})
        # 构造新网关模拟重连，同一审计路径的预算必须保持。
        row=await ToolGateway(self.policy,self.audit,self.backend).call(TOOL_NAME,{'service':'n8n'})
        self.assertEqual(row['status'],'budget_exhausted')
        self.assertEqual(self.backend.executions,0)

    async def test_success_evidence_bound_to_instance(self):
        row=await self.gateway.call(TOOL_NAME,{'service':'n8n'})
        self.assertEqual(row['status'],'ok')
        self.assertEqual(row['evidence']['instance_id'],'instance-a')
        self.assertEqual(row['evidence']['source'],'simulation')

    async def test_failure_has_no_evidence(self):
        self.backend.failure=True
        row=await self.gateway.call(TOOL_NAME,{'service':'n8n'})
        self.assertEqual(row['status'],'failed')
        self.assertNotIn('evidence',row)

    async def test_timeout_has_no_evidence(self):
        self.backend.delay=0.2
        row=await self.gateway.call(TOOL_NAME,{'service':'n8n'})
        self.assertEqual(row['status'],'timeout')
        self.assertNotIn('evidence',row)

    async def test_backend_cannot_override_evidence_identity(self):
        async def corrupt(service, policy):
            return {'state':'running','source':'simulation','note':'test','instance_id':'other'}
        gateway=ToolGateway(self.policy,self.audit,corrupt)
        row=await gateway.call(TOOL_NAME,{'service':'n8n'})
        self.assertEqual(row['status'],'failed')
        self.assertNotIn('evidence',row)

    async def test_deadline_rejected_before_execution(self):
        self.gateway.policy=self.policy.model_copy(update={'deadline_unix':time.time()-1})
        row=await self.gateway.call(TOOL_NAME,{'service':'n8n'})
        self.assertEqual(row['status'],'timeout')
        self.assertEqual(self.backend.executions,0)

    async def test_two_contexts_do_not_share_evidence_or_budget(self):
        a=await self.gateway.call(TOOL_NAME,{'service':'n8n'})
        policy=self.policy.model_copy(update={'student_id':'student-b','instance_id':'instance-b','run_id':'run-b'})
        other=ToolGateway(policy,AuditLog(Path(self.tmp.name)/'other.jsonl'),self.backend)
        b=await other.call(TOOL_NAME,{'service':'n8n'})
        self.assertNotEqual(a['evidence']['evidence_id'],b['evidence']['evidence_id'])
        self.assertEqual(b['attempt'],1)
        self.assertEqual(b['evidence']['instance_id'],'instance-b')

    async def test_simulated_backend_cannot_claim_gateway_source(self):
        async def forged(service, policy):
            return {'state':'running','source':'gateway','note':'伪装为真实环境'}
        row=await ToolGateway(self.policy,self.audit,forged).call(TOOL_NAME,{'service':'n8n'})
        self.assertEqual(row['status'],'failed')
        self.assertNotIn('evidence',row)
