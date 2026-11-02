"""演示用 MCP 服务入口，正式桥接模块不导入本文件或模拟资料。"""
import json
import sys
from pathlib import Path
from dsh_tutor.runtime import require_execution_identity
from dsh_tutor.tools import ToolPolicy, AuditLog, ToolGateway
from dsh_tutor.mcp_bridge import run_stdio
from sandbox.mock_tools import MockExecutor


def main():
    context = json.loads(Path(sys.argv[1]).read_text())
    require_execution_identity(context)
    policy = ToolPolicy.model_validate(context['policy'])
    backend = MockExecutor(context['service_states'])
    run_stdio(ToolGateway(policy, AuditLog(Path(context['audit_path'])), backend))


if __name__ == '__main__':
    main()
