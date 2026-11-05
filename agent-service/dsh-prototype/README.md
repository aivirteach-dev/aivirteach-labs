# DSH 版平台 Agent

用 DeepSeek Harness（DSH）实现的 AIVirTeach Agent，接口与 main 上的 `agent-service/aivirteach_agent` 完全相同，Server 和 Client 不需要改动。目前在测试分支，未替换 main 的 Agent。

原型阶段的异步接口、聊天库、学习记忆、调度、管理界面等已删除，记录和取回方式见 [docs/removed-features.md](docs/removed-features.md)。

## 接口

`GET /health`、`GET /ready`、`POST /v1/agent/diagnose`、`POST /v1/agent/diagnose/stream`（SSE）、`/openapi.json`，端口 8770，Bearer `AIVIRTEACH_AGENT_TOKEN`。不保存聊天或学习记录，这些由 Server 负责。

与 main 逐字对应、由测试自动对比的部分：请求/返回格式（`platform_models.py`）、20 个只读工具的参数和授权校验（`platform_tools.py`）、脱敏（`platform_security.py`）、课程资料补充（`platform_course.py`）。SSE 事件名和帧格式、Gateway 错误码、证据编号（`obs-001`…）、工具调用次数上限也与 main 一致。

## 一次请求怎么跑

1. `platform_app.py`：鉴权、排队（并发上限）、组提示词，在线程里调用 `run_once`，把过程事件转成 SSE。
2. `platform_runtime.py`：建临时运行目录，启动一个 DSH 会话，读取事件文件，结束后删除目录。
3. `dsh_patch.py`：生成本次请求的 DSH 补丁——模型提供方、预算插件、工具桥。DSH 自带的文件、Shell、摘要、重试等插件在 `ollama.patch.yml` 里关闭。
4. `plugins/model_budget.mjs`（预算插件）：每次模型调用前检查次数、单次输出上限、工具集、总时限、取消；把文字片段、用量、结束原因写进事件文件。
5. `platform_mcp.py`（工具桥，MCP stdio）：按 Server 给的 `diagnostic_scope` 校验后调用 Gateway `POST /v1/diagnostics/{lab_id}/tools/{tool}`，写证据和调用记录。Server 不传范围时，与 main 一样只有 `get_vm_status`、`get_guest_agent_status`。

事件文件每行带毫秒时间戳 `t`；`RunOutcome.calls` 给出每次模型调用的开始、首字、结束时间、结束原因和用量，供监控使用。DSH 官方遥测会把会话发往 DeepSeek，已用 `DSH_TELEMETRY_DISABLED=1` 关闭。

## 启动

- 宿主机：`./start_platform.sh`，读取与 main 相同的 `agent-service/config/agent.env`（可用 `AIVIRTEACH_AGENT_ENV_FILE` 指定）。
- 容器：`docker compose -f compose.platform.yaml --env-file ../../config.env up -d --build`。

两种方式都占用 8770，与 main 的 agent-service 二选一。用真实 Gateway 验收见 [docs/platform-gateway-test.md](docs/platform-gateway-test.md)。

## 配置

沿用 main 的变量：`AIVIRTEACH_AGENT_TOKEN`、`AIVIRTEACH_AGENT_HOST`/`PORT`、`AIVIRTEACH_AGENT_CORS_ORIGINS`、`AIVIRTEACH_COURSE_DIR`、`AIVIRTEACH_GATEWAY_URL`、`AIVIRTEACH_DIAGNOSTIC_TOKEN`、`AIVIRTEACH_MODEL_PROVIDER`（`fake`、`ollama`、`openai_compatible`，默认 `fake`）、`AIVIRTEACH_MODEL_BASE_URL`（根地址如 `https://api.deepseek.com/` 会自动补 `/v1`）、`AIVIRTEACH_MODEL_API_KEY`、`AIVIRTEACH_MODEL_NAME`、`AIVIRTEACH_MODEL_THINKING`（只有 `enabled` 才开启思考）、`AIVIRTEACH_AGENT_TOTAL_TIMEOUT`（默认 40 秒，排队时间也计入）、`AIVIRTEACH_TOOL_TIMEOUT`（8）、`AIVIRTEACH_MAX_REASONING_TURNS`（4）、`AIVIRTEACH_MAX_TOOL_CALLS`（6，含被拒绝的调用）、`AIVIRTEACH_MAX_TOOL_OUTPUT_CHARS`（32768）、`AIVIRTEACH_MAX_CONCURRENT_REQUESTS`（4）。`AIVIRTEACH_MODEL_TIMEOUT` 不使用：DSH 由总超时和预算插件约束。

DSH 专用：`AIVIRTEACH_DSH_CONTEXT_WINDOW`（32768）、`AIVIRTEACH_DSH_MAX_OUTPUT_TOKENS`（4096）、`AIVIRTEACH_DSH_REASONING_OFF_VALUE`（关闭思考时发给提供方的值，默认 `none`）、`AIVIRTEACH_DSH_STATE_DIR`（每次请求的临时目录，结束即删）、`AIVIRTEACH_DSH_EXECUTION_USER`（设置后核对运行账号，镜像内为 `dsh-runner`）。

## 开发和测试

需要 Python 3.10 及以上（镜像用 3.10.12）。DSH 运行时是 `deepseek-harness-runtime-bin` 里的单个可执行文件，运行时不需要 Node；Node 只用来跑预算插件的单元测试，不在 PATH 时该项跳过。

```bash
cd agent-service/dsh-prototype
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s sandbox/tests
```

- `sandbox/tests/test_platform_api.py`：接口契约、鉴权、SSE、工具桥、用量、取消（不启动 DSH）。Python 3.11+ 且能导入 main 的 Agent 时，自动对比复制过来的模型、工具和脱敏代码。
- `sandbox/tests/test_dsh_runtime.py`：真实 DSH + 假模型（`sandbox/fake_model.py`）+ 假 Gateway（`sandbox/fake_gateway.py`），检查文字片段、用量、工具调用、预算停止、截断、取消和时间戳。
- `sandbox/tests/test_model_budget.mjs`：预算插件单元测试。

## 实测（本地，假 Gateway）

- DeepSeek `deepseek-v4-flash`：按授权依次调用容器、端口、日志工具并引用证据回答；`usage` 与提供方一致。
- Ollama `qwen3.5:9b`：工具调用和证据正常，`usage` 为 null（不按缓存计量，与 main 一致）。
- 每个并发请求 1 个 DSH 运行时进程，约 11 个线程、190MB 独占内存；首字 0.35～3 秒。调用方断开或总超时后通知 DSH 停止，实测 0.3 秒内退出。

## 当前边界

- `diagnosis`、`course_alignment`、`suggested_actions` 留空：回答一次生成并逐段推送（main 先单独生成结构化 JSON）。Server 只要求这些字段存在，Client 只显示 `answer`。
- 回答超过输出上限时保留已生成内容，标记 `partial` 和 `MODEL_STREAM_OUTPUT_TRUNCATED`。
- 尚未在真实 Gateway / 真实 VM 上验收；镜像未在 Docker 中实际构建。
- 监控（OpenTelemetry + Phoenix）在后续阶段接入，数据来源是 `RunOutcome.calls` 和工具调用记录。
