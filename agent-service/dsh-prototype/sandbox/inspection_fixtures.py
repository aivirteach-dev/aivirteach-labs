"""共享虚构传输夹具：真实ToolGateway授权/预算/审计，返回预设观察，不调用模型。"""
import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from uuid import uuid4
from dsh_tutor.tools import ToolPolicy,ToolGateway,AuditLog,TOOL_NAME


def simulated_transport(bridge,root,*,state='running',failure=False,denied=False,mutate=None):
    def call(prompt,**kwargs):
        run_id=uuid4().hex;started=datetime.now(timezone.utc).isoformat()
        policy=ToolPolicy.model_validate(bridge['context']['policy']|{'run_id':run_id,'deadline_unix':time.time()+30})
        async def observe(service,policy):
            if failure:raise RuntimeError('PRIVATE_BACKEND_ERROR')
            return {'state':state,'source':'simulation','note':'PRIVATE_BACKEND_NOTE'}
        audit=AuditLog(Path(root)/(run_id+'.jsonl'));gateway=ToolGateway(policy,audit,observe)
        row=asyncio.run(gateway.call(TOOL_NAME if not denied else 'PRIVATE_UNAUTHORIZED_TOOL',{'service':policy.allowed_services[0]}))
        evidence=[row['evidence']] if row['status']=='ok' else []
        result={'answer':json.dumps({'evidence_ids':[x['evidence_id'] for x in evidence],'course_instruction_indexes':[],'next_step':'none'}),
                'model':'transport-substitute','elapsed_seconds':0.01,'run_id':run_id,'run_started_at':started,
                'tool_calls':sum(x['type']=='execution' for x in audit.read()),'tool_attempts':1,'tool_trace':[row],'evidence':evidence}
        if mutate:mutate(result)
        return result
    return call
