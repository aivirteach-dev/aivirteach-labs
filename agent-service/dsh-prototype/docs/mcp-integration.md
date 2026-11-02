# MCP模拟工具接入

## 当前结构

dsh_tutor/tools.py负责可信ToolPolicy、执行前授权、严格参数、持久尝试预算、异步超时和证据绑定；mcp_bridge.py使用官方SDK进行stdio通信，不监听网络端口；runtime.py负责DSH和本次MCP子进程；results.py校验证据并生成事实回答。

sandbox/mock_tools.py和mcp_server.py提供唯一虚构后端及演示入口，不读真实文件，不调用Docker、网络或真实VM。正式模块不导入模拟模块。

模型只提交service，不能设置学生、课程或实例。默认最多3次尝试，无效/拒绝/超限尝试同样计数，预算不因同一日志路径的MCP重连而归零。执行来源默认只允许simulation；工具错误不产生有效证据。

## 依赖与运行

Python3.10.12；DSH SDK/运行时0.2.1rc2；MCP SDK1.28.0维护线。全部依赖固定在requirements.txt，只安装到项目.venv。

```bash
cd /home/xf/projects/dsh
.venv/bin/python tools/dashboard.py test
sudo -u dsh-runner /home/xf/projects/dsh/.venv/bin/python /home/xf/projects/dsh/sandbox/mcp_acceptance.py
```

35项自动检查通过；模型验收单独运行，当前报告为sandbox/results/runner-check/quality-report.json。测试期间冻结运行代码，报告绑定指纹。不能只看模型声称调用过，需核对入口审计、证据及实际执行次数。

## 历史与范围

首轮两个课程有工具链路记录，但测试中代码更新，未绑定最终指纹，保留development/mcp-first-model-report-20261029.json。后续单例有匹配指纹，工具链通过但模型漏报stopped，见mcp-final-acceptance-20261029.json。现已针对该问题增加结果校验；新版本两课程结果校验验收通过，见development/answer-quality-acceptance-20261029.json；旧结果仍按历史版本保留。

当前只有模拟只读服务状态工具；没有真实Gateway、真实Windows/Linux检查、身份认证或生产多人支持。模型补充说明采用受限选择，尚未开放自由原因分析或修复命令，详见result-validation.md。

MCP不是系统沙箱。runner出站网络未单独限制；用户明确保留VMware剪贴板与拖放。DSH SDK关闭时仍有上游ResourceWarning；未隐藏或宣称修复。总模型轮次/总token和完整usage仍待实现，远端取消待验证。

## 官方依据

- DSH MCP配置：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md
- MCP Python维护线：https://github.com/modelcontextprotocol/python-sdk/tree/v1.x
