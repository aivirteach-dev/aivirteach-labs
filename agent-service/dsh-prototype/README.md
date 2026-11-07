# DSH 版平台 Agent

用 DeepSeek Harness（DSH）实现的 AIVirTeach Agent，接口与 main 上的 `agent-service/aivirteach_agent` 完全相同，Server 和 Client 不需要改动。目前在测试分支，未替换 main 的 Agent。

原型阶段的异步接口、聊天库、学习记忆、调度、管理界面等已删除，记录和取回方式见 [docs/removed-features.md](docs/removed-features.md)。

## 接口

`GET /health`、`GET /ready`、`POST /v1/agent/diagnose`、`POST /v1/agent/diagnose/stream`（SSE）、`/openapi.json`，端口 8770，Bearer `AIVIRTEACH_AGENT_TOKEN`。不保存聊天或学习记录，这些由 Server 负责。

与 main 逐字对应、由测试自动对比的部分：请求/返回格式（`platform_models.py`）、20 个只读工具的参数和授权校验（`platform_tools.py`）、脱敏（`platform_security.py`）、课程资料补充（`platform_course.py`）。SSE 事件名和帧格式、Gateway 错误码、证据编号（`obs-001`…）、工具调用次数上限也与 main 一致。

### 扩展：虚拟机画面（开发中，已完成阶段 1、2）

学生同意后，Client 可以把 Guacamole 当前显示的 VM 桌面截图放进请求，Agent 据此判断学生停在哪个窗口、找不到哪个按钮。代码在 `platform_screen.py`，`platform_models.py` 仍是 main 的原样副本。

- 请求可选字段 `screen`：`source`（固定 `vm_console`）、`mime_type`（`image/png`、`image/jpeg`、`image/webp`）、`data`（标准 base64，不带 `data:` 前缀）、`width`、`height`、`captured_at`。
- 返回可选字段 `screen_findings`：`summary` 和最多 5 个 `highlights`（按比例的坐标 `x`、`y`、`width`、`height`，0～1）。没有结果时返回里不出现这个字段。
- 新增 SSE 事件 `screen_analyzing`、`screen_analyzed`（`status` 为 `ok` / `skipped` / `rejected` / `failed`，带原因码）、`screen_requested`。现在的 Client 不认识这些事件，会直接忽略。
- 兼容性：Server 不发 `screen` 时，请求处理、返回内容和事件与 main 完全相同。Server 用 zod 校验返回，不认识的字段会被去掉，不会出错。
- 检查：先按文件头判断格式和尺寸，再完整解码。解码后最大 3 MB、单边最大 2560、总像素最多 2560×1600、不接受动图、声明的宽高必须与实际一致。不合格时不影响回答，只在 `limitations` 里记原因码（`SCREEN_INVALID_BASE64`、`SCREEN_TOO_LARGE`、`SCREEN_FORMAT_MISMATCH`、`SCREEN_DIMENSIONS_TOO_LARGE`、`SCREEN_DIMENSIONS_MISMATCH`、`SCREEN_ANIMATED`、`SCREEN_UNREADABLE`）。
- 隐私：截图只在内存里处理，不写文件、日志和监控，也不进任何提示词；`screen` 下的 422 错误不回显输入；超过 6 MB 的请求体在解析前返回 413。
- 看图（`platform_vision.py`）：在评估之前，把截图交给单独配置的看图模型（OpenAI 兼容的 `/chat/completions`，不经过 DSH），得到固定格式的界面描述：窗口类型、程序、页面、一两句描述、可见的报错、最多 5 个相关元素的位置，以及是否含密钥等敏感内容。
  - 先看长边缩到 1280 的整屏；模型说相关区域看不清时，从原图裁出那块再看一次。每个请求最多 2 次调用，坐标换回整张图。
  - 看图结果用 main 的脱敏规则过滤，并加上课程会出现的 Tavily、Google API Key、Google OAuth 密钥格式；画面上有敏感内容时不使用这张截图（`SCREEN_SENSITIVE_CONTENT`）。
  - 限时：默认 12 秒，并保证看图之后还剩至少 20 秒给评估和回答，否则不看（`SCREEN_ANALYSIS_TIMEOUT`）。连接失败、超时、5xx 重试一次；其他失败记 `SCREEN_ANALYSIS_FAILED`，输出格式不对记 `SCREEN_ANALYSIS_INVALID`。看图失败不影响回答。
  - 减压：同一张图、同一个问题在 2 分钟内复用结果（不再调用模型）；每台 VM 每小时最多看 10 张（`SCREEN_RATE_LIMITED`），复用的结果不计。
  - 看图的 token 并入返回的 `usage`，Server 现有计费会算上；监控里是单独的 `chat <看图模型>` span（`aivirteach.phase` 为 `screen`），只有耗时和 token，没有图片和看图原文。
  - 结果放进返回的 `screen_findings`（描述和标注位置）。阶段 2 还没有把看图结果交给评估和回答，这是阶段 3 的内容；评测是阶段 4。

## 一次请求怎么跑

1. `platform_app.py`：鉴权、排队（并发上限），在工作线程里按 main 的流程依次运行各阶段，把过程事件转成 SSE：
   - **评估**：DSH + 工具，模型查证后只输出结构化评估 JSON（诊断、课程对应、证据编号、建议、限制），不推送给学生；
   - **收尾**：只在检查次数用完、还没给出评估时，不带工具再问一次；
   - **校验**：JSON 结构和证据编号不合规就不写回答（`ASSESSMENT_INVALID`）；
   - **回答**：另起一个不带工具的 DSH 会话，只根据校验过的评估写回答，逐段推送。
   提示词和校验在 `platform_assessment.py`（解析和兜底照抄 main，测试对比两边结果）；每个阶段的规则作为系统消息（经 DSH 的 `DSH_SYSTEM_PROMPT`），数据作为用户消息。
2. `platform_runtime.py`：运行一个阶段：建临时运行目录，启动一个 DSH 会话，读取事件文件，结束后删除目录。
3. `dsh_patch.py`：生成本次请求的 DSH 补丁——模型提供方、预算插件、工具桥。DSH 自带的文件、Shell、摘要、重试等插件在 `dsh-base.patch.yml` 里关闭。
4. `plugins/model_budget.mjs`（预算插件）：每次模型调用前检查次数、单次输出上限、工具集、总时限、取消；把文字片段、用量、结束原因写进事件文件。连接被切断或请求超时、且模型还没输出任何内容时，同一次调用重试一次（不会出现重复内容，也不算新的模型调用）。
5. `platform_mcp.py`（工具桥，MCP stdio）：按 Server 给的 `diagnostic_scope` 校验后调用 Gateway `POST /v1/diagnostics/{lab_id}/tools/{tool}`，写证据和调用记录。Server 不传范围时，与 main 一样只有 `get_vm_status`、`get_guest_agent_status`。

事件文件每行带毫秒时间戳 `t`；`RunOutcome.calls` 给出每次模型调用的开始、首字、结束时间、结束原因和用量，监控据此按 OpenTelemetry 发送到 Phoenix，见 [docs/monitoring.md](docs/monitoring.md)。DSH 官方遥测会把会话发往 DeepSeek，已用 `DSH_TELEMETRY_DISABLED=1` 关闭。

## 启动

- 宿主机：`./start_platform.sh`，读取与 main 相同的 `agent-service/config/agent.env`（可用 `AIVIRTEACH_AGENT_ENV_FILE` 指定）。
- 容器：`docker compose -f compose.platform.yaml --env-file ../../config.env up -d --build`。
- 容器加监控和告警（Phoenix、OpenTelemetry Collector、Prometheus、Alertmanager）：再叠加 `-f compose.monitoring.yaml --env-file .env.monitoring`，步骤见 [docs/monitoring.md](docs/monitoring.md#服务器部署)。

两种方式都占用 8770，与 main 的 agent-service 二选一。用真实 Gateway 验收见 [docs/platform-gateway-test.md](docs/platform-gateway-test.md)。

## 配置

沿用 main 的变量：`AIVIRTEACH_AGENT_TOKEN`、`AIVIRTEACH_AGENT_HOST`/`PORT`、`AIVIRTEACH_AGENT_CORS_ORIGINS`、`AIVIRTEACH_COURSE_DIR`、`AIVIRTEACH_GATEWAY_URL`、`AIVIRTEACH_DIAGNOSTIC_TOKEN`、`AIVIRTEACH_MODEL_PROVIDER`（`fake`、`ollama`、`openai_compatible`，默认 `fake`）、`AIVIRTEACH_MODEL_BASE_URL`（根地址如 `https://api.deepseek.com/` 会自动补 `/v1`）、`AIVIRTEACH_MODEL_API_KEY`、`AIVIRTEACH_MODEL_NAME`、`AIVIRTEACH_MODEL_THINKING`（只有 `enabled` 才开启思考）、`AIVIRTEACH_AGENT_TOTAL_TIMEOUT`（默认 40 秒，排队时间也计入）、`AIVIRTEACH_TOOL_TIMEOUT`（8）、`AIVIRTEACH_MAX_REASONING_TURNS`（4）、`AIVIRTEACH_MAX_TOOL_CALLS`（6，含被拒绝的调用）、`AIVIRTEACH_MAX_TOOL_OUTPUT_CHARS`（32768）、`AIVIRTEACH_MAX_CONCURRENT_REQUESTS`（4）。`AIVIRTEACH_MODEL_TIMEOUT` 不使用：DSH 由总超时和预算插件约束。

看图（可选，不设模型名就不看图）：`AIVIRTEACH_VISION_MODEL_NAME`（例如 DeepSeek 的 `deepseek-flash`）、`AIVIRTEACH_VISION_MODEL_BASE_URL`（默认沿用 `AIVIRTEACH_MODEL_BASE_URL`）、`AIVIRTEACH_VISION_MODEL_API_KEY`（默认沿用 `AIVIRTEACH_MODEL_API_KEY`；本地 Ollama 等不需要密钥的可以都不设）、`AIVIRTEACH_VISION_TIMEOUT`（默认 12 秒，范围 3～30）、`AIVIRTEACH_SCREEN_MAX_PER_HOUR`（每台 VM 每小时最多看图次数，默认 10，0 表示不限）。地址不是根地址或 `/v1` 结尾时不看图，并在启动日志里提示。

DSH 专用：`AIVIRTEACH_DSH_CONTEXT_WINDOW`（32768）、`AIVIRTEACH_DSH_MAX_OUTPUT_TOKENS`（4096）、`AIVIRTEACH_DSH_STATE_DIR`（每次请求的临时目录，结束即删）。关闭思考时固定发送 `reasoning_effort: none`（DeepSeek、Ollama 实测都接受）；运行账号由镜像的 `USER 1000`（`dsh-runner`）和 compose 的 `user` 保证。

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
- `sandbox/tests/test_deploy_config.py`：部署文件的安全设置；`promtool`、`amtool`、`otelcol-contrib` 在 PATH 时还检查告警规则（含 `deploy/alerts.test.yml`）和配置。
- `sandbox/tests/test_telemetry.py`：监控 span 的结构、属性、token 换算、失败标记和隐私默认值（内存导出，不需要 Phoenix）。
- `sandbox/tests/test_screen.py`：虚拟机画面的请求和返回格式、图片检查（每种不合格情况的原因码、特制的大尺寸文件头）、缩略和裁剪、没有截图时返回与 main 相同、422 不回显截图、请求体上限。
- `sandbox/tests/test_vision.py`：看图的请求格式、整屏 → 放大两次看图和坐标换算、密钥过滤、输出格式错误、HTTP 错误和重试、超时和取消、结果复用、每台 VM 的次数限制、接入请求流程、用量合并、监控里不含图片和看图原文（看图模型用 httpx 的模拟传输，不连真实模型）。

## 实测（本地，假 Gateway）

- DeepSeek `deepseek-v4-flash`：按授权依次调用容器、端口、日志工具并引用证据回答；`usage` 与提供方一致。
- Ollama `qwen3.5:9b`：工具调用和证据正常，`usage` 为 null（不按缓存计量，与 main 一致）。
- 每个并发请求 1 个 DSH 运行时进程，约 11 个线程、190MB 独占内存；首字 0.35～3 秒。调用方断开或总超时后通知 DSH 停止，实测 0.3 秒内退出。

## 当前边界

- 每个请求至少 2 次 DSH 运行（评估、回答），比一次生成多约 3 秒、多约 40% token；学生在评估完成后才开始看到文字。
- 评估里的 `limitations` 是模型写的说明文字，与 main 一样并入返回的 `limitations`。
- 回答超过输出上限时保留已生成内容，标记 `partial` 和 `MODEL_STREAM_OUTPUT_TRUNCATED`。
- 尚未在真实 Gateway / 真实 VM 上验收；镜像未在 Docker 中实际构建。
- 监控和告警：各组件已在本机用相同配置验证（告警规则另有单元测试），尚未在服务器的 Docker 里实际运行。
