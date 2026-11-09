# DSH 版平台 Agent

用 DeepSeek Harness（DSH）实现的 AIVirTeach Agent，接口与 main 上的 `agent-service/aivirteach_agent` 完全相同，Server 和 Client 不需要改动。目前在测试分支，未替换 main 的 Agent。

原型阶段的异步接口、聊天库、学习记忆、调度、管理界面等已删除，记录和取回方式见 [docs/removed-features.md](docs/removed-features.md)。

## 接口

`GET /health`、`GET /ready`、`POST /v1/agent/diagnose`、`POST /v1/agent/diagnose/stream`（SSE）、`/openapi.json`，端口 8770，Bearer `AIVIRTEACH_AGENT_TOKEN`。不保存聊天或学习记录，这些由 Server 负责。

与 main 逐字对应、由测试自动对比的部分：请求/返回格式（`platform_models.py`）、20 个只读工具的参数和授权校验（`platform_tools.py`）、脱敏（`platform_security.py`）、课程资料补充（`platform_course.py`）。SSE 事件名和帧格式、Gateway 错误码、证据编号（`obs-001`…）、工具调用次数上限也与 main 一致。

### 扩展：虚拟机画面（开发中）

学生同意后，Client 可以把 Guacamole 当前显示的 VM 桌面截图放进请求，Agent 据此判断学生停在哪个窗口、找不到哪个按钮。代码在 `platform_screen.py`，`platform_models.py` 仍是 main 的原样副本。

- 请求可选字段 `screen`：`source`（固定 `vm_console`）、`mime_type`（`image/png`、`image/jpeg`、`image/webp`）、`data`（标准 base64，不带 `data:` 前缀）、`width`、`height`、`captured_at`。
- 返回可选字段 `screen_findings`：`summary` 和最多 5 个 `highlights`（按比例的坐标 `x`、`y`、`width`、`height`，0～1）。没有结果时返回里不出现这个字段。
- 不新增 SSE 事件：带不带截图，进度事件都与 main 相同。截图没用上的原因只写进 `limitations`，回答末尾有一句说明；Client 自己知道带了截图，可以自己显示“正在查看画面”。
- 兼容性：Server 不发 `screen` 时，返回格式和事件与 main 相同；发了 `screen` 时，模型调用次数和事件也不变，只是评估多了画面描述、返回多了 `screen_findings`。Server 用 zod 校验返回，不认识的字段会被去掉，不会出错。
- 检查：先按文件头判断格式和尺寸，再完整解码。解码后最大 3 MB、单边最大 2560、总像素最多 2560×1600、不接受动图、声明的宽高必须与实际一致。不合格时不影响回答，只在 `limitations` 里记原因码（`SCREEN_INVALID_BASE64`、`SCREEN_TOO_LARGE`、`SCREEN_FORMAT_MISMATCH`、`SCREEN_DIMENSIONS_TOO_LARGE`、`SCREEN_DIMENSIONS_MISMATCH`、`SCREEN_ANIMATED`、`SCREEN_UNREADABLE`）。
- 隐私：截图只在内存里处理，不写文件、日志和监控，也不进任何提示词；`screen` 下的 422 错误不回显输入；超过 6 MB 的请求体在解析前返回 413。
- 看图（`platform_vision.py`）：在评估之前，把截图交给单独配置的看图模型（OpenAI 兼容的 `/chat/completions`，不经过 DSH），得到固定格式的界面描述：窗口类型、程序、页面、一两句描述、可见的报错、最多 5 个相关元素的位置，以及是否含密钥等敏感内容。
  - 先看长边缩到 1280 的整屏；模型说相关区域看不清时，从原图裁出那块再看一次。每个请求最多 2 次调用，坐标换回整张图。
  - 让模型给所看图片的像素坐标（请求里带上图片尺寸），再换成 0～1 的比例。实测 DeepSeek 用像素坐标准得多：4 张模拟截图各 3 次，像素 9/9 命中目标，比例只有 6/9，而且越靠下偏得越多。
  - 看图结果用 main 的脱敏规则过滤，并加上课程会出现的 Tavily、Google API Key、Google OAuth 密钥格式；画面上有敏感内容时不使用这张截图（`SCREEN_SENSITIVE_CONTENT`）。
  - 限时：默认 12 秒，并保证看图之后还剩至少 20 秒给评估和回答，否则不看（`SCREEN_ANALYSIS_TIMEOUT`）。连接失败、超时、5xx 重试一次；其他失败记 `SCREEN_ANALYSIS_FAILED`，输出格式不对记 `SCREEN_ANALYSIS_INVALID`。看图失败不影响回答。
  - 减压：同一张图、同一个问题在 2 分钟内复用结果（不再调用模型）；每台 VM 每小时最多看 10 张（`SCREEN_RATE_LIMITED`），复用的结果不计。
  - 看图的 token 并入返回的 `usage`，Server 现有计费会算上；监控里是单独的 `chat <看图模型>` span（`aivirteach.phase` 为 `screen`），只有耗时和 token，没有图片和看图原文。
  - 结果放进返回的 `screen_findings`（描述和标注位置）。
- 接入诊断（`platform_screen_flow.py`）：
  - 看图和 DSH 启动同时进行（见下文“工具范围和预先检查”）。有画面时，画面描述（窗口、页面、报错、相关元素的粗略位置如“右上”“底部中间”，不含坐标和图片）加在评估和收尾的用户消息末尾；使用规则写成“有画面描述时……”，一直在系统消息里，所以系统消息不等看图结果。回答阶段仍只看校验过的评估（与 main 相同）。画面描述没有观察编号，评估 JSON 里引用了也会在 main 的校验之前去掉。
  - 回答末尾的说明：带了画面但没用上时说明原因（含密钥、次数用完、其他原因），说明也逐段推送。
  - 2026-11-08 简化：删掉了按关键词的问题分类、“界面类问题评估最多 2 次”、`AIVIRTEACH_SCREEN_REQUESTS` 提示开关和 3 个看图进度事件（见 `docs/removed-features.md`）。
- 实测（DeepSeek 对话 + 看图、真实 DSH、假 Gateway、仓库教材、模拟截图）：界面问题带画面约 11 秒，回答直接指出“画布底部中间的橙色 Execute workflow 按钮”，标注位置落在按钮上；环境问题带画面约 12.5 秒，结合画面上的 Unable to connect 和教材里的目录、容器名、就绪检查给出步骤。评测（阶段 4）尚未做。

## 一次请求怎么跑

1. `platform_app.py`：鉴权、排队（并发上限）、确定工具范围（`platform_precheck.py`，见下一节），在工作线程里按 main 的流程依次运行各阶段，把过程事件转成 SSE：
   - **评估**：DSH 启动的同时预先检查和看图；模型拿到检查结果（还可以自己再调工具）后只输出结构化评估 JSON（诊断、课程对应、证据编号、建议、限制），不推送给学生；
   - **收尾**：只在检查次数用完、还没给出评估时，不带工具再问一次；
   - **校验**：JSON 结构和证据编号不合规就不写回答（`ASSESSMENT_INVALID`）。比 main 多两步容错（实测 DeepSeek 约 5～8% 的评估有这类问题）：校验前先去掉规定以外的字段（模型偶尔多写 `course_alignment_note: ""` 这类空字段，main 会因此作废整个评估）；JSON 括号不配对、不完整时，剩余时间够 15 秒就不带工具按已有证据再要一次评估（发 main 已有的 `finalization_started` 事件，`reason` 为 `assessment_unstructured`）。规定字段的类型、长度和证据编号仍按 main 的规则严格校验；
   - **回答**：另起一个不带工具的 DSH 会话，只根据校验过的评估写回答，逐段推送。
   提示词和校验在 `platform_assessment.py`（解析和兜底照抄 main，测试对比两边结果）；每个阶段的规则作为系统消息（经 DSH 的 `DSH_SYSTEM_PROMPT`），数据作为用户消息。
2. `platform_runtime.py`：运行一个阶段：建临时运行目录，启动一个 DSH 会话，读取事件文件，结束后删除目录。提问可以是 `LatePrompt`：DSH 启动（起进程、加载插件、连工具桥）的同时准备提问，两边都好了再发出。
3. `dsh_patch.py`：生成本次请求的 DSH 补丁——模型提供方、预算插件、工具桥。DSH 自带的文件、Shell、摘要、重试等插件在 `dsh-base.patch.yml` 里关闭。
4. `plugins/model_budget.mjs`（预算插件）：每次模型调用前检查次数、单次输出上限、工具集、总时限、取消；把文字片段、用量、结束原因写进事件文件。连接被切断、请求超时，或 15 秒内一个字都没输出（首字超时），且模型还没输出任何内容时，同一次调用重试一次（不会出现重复内容，也不算新的模型调用）。重试后仍失败的调用报告为 `MODEL_PROVIDER_ERROR`（已输出一部分时为 `MODEL_STREAM_INCOMPLETE`），不会把半截输出当成模型的正常回答。
5. `platform_mcp.py`（工具桥，MCP stdio）：按本次请求的 `diagnostic_scope` 校验后调用 Gateway `POST /v1/diagnostics/{lab_id}/tools/{tool}`，写证据和调用记录。预先检查用同一个类在平台接口进程里调用，做完后把次数、证据编号和结果交给 DSH 里的工具桥接着用。

## 工具范围和预先检查

按分工，课程相关的事归 labs：工具范围不再等 Server 发，由 `platform_precheck.py` 按课程配置 `dsh_tutor/course_scopes.json` 决定（课程和课时按教材里的正式编号或别名匹配）。

- **范围**：Server 在请求里明确给了 `diagnostic_scope` 时仍以它为准（main 的接口）；没给时用课程配置里的范围（AI Daily Briefing：工作目录 `/home/learner/aivirteach/ai-daily-briefing`、容器 `ai-daily-briefing-n8n`、端口 5678、`docker.service`、只读 `compose.yaml`，共 9 个工具）；配置里没有这门课时与 main 一样只有 `get_vm_status`、`get_guest_agent_status`。范围来自部署在镜像里的配置，不来自学生。读文件要 Gateway 的 `AIVIRTEACH_GUEST_ALLOWED_ROOTS` 包含这个工作目录。
- **预先检查**：每个课时都要查的几项（默认：虚拟机状态、课程容器状态、5678 端口；“安装 Docker”这一课查虚拟机和 `docker.service`）不等模型提出，在 DSH 启动的同时并行调 Gateway。做法参考 k8sgpt（固定检查器先查，模型负责解释）和 HolmesGPT（每类问题配排查手册，限定该查什么）。
  - 和模型调工具完全一样：同一套范围校验、脱敏、证据编号（`obs-001`…）、调用记录和 `tool_started` / `tool_finished` 事件，事件仍在 `reasoning_started` 和 `reasoning_finished` 之间。预先检查不占模型的工具次数（`AIVIRTEACH_MAX_TOOL_CALLS` 仍是模型能调几次，和 main 相同），它的数量由课程配置固定；实测让它占用次数时，模型在“没有创建容器”这类问题上会把剩下的次数用完。
  - 结果（每项最多 1,500 字符）作为 `prechecked` 加在评估和收尾的用户消息末尾；规则告诉模型不要重复、信息够了就直接输出评估。模型仍可以调范围内的其他工具（例如日志、`compose.yaml`）；重复相同的检查直接用已有结果，不再访问 Gateway。
  - 系统消息不依赖检查结果：DSH 可以先启动，系统消息也总能命中缓存。
  - Server 明确给了空的工具范围时不做预先检查，与 main 相同（`LIVE_DIAGNOSTICS_NOT_RUN`）。预先检查出意外时照常诊断，由模型自己调工具。

事件文件每行带毫秒时间戳 `t`；`RunOutcome.calls` 给出每次模型调用的开始、首字、结束时间、结束原因和用量，监控据此按 OpenTelemetry 发送到 Phoenix，见 [docs/monitoring.md](docs/monitoring.md)。DSH 官方遥测会把会话发往 DeepSeek，已用 `DSH_TELEMETRY_DISABLED=1` 关闭。

## 启动

- 宿主机：`./start_platform.sh`，读取与 main 相同的 `agent-service/config/agent.env`（可用 `AIVIRTEACH_AGENT_ENV_FILE` 指定）。
- 容器：`docker compose -f compose.platform.yaml --env-file ../../config.env up -d --build`。
- 容器加监控和告警（Phoenix、OpenTelemetry Collector、Prometheus、Alertmanager）：再叠加 `-f compose.monitoring.yaml --env-file .env.monitoring`，步骤见 [docs/monitoring.md](docs/monitoring.md#服务器部署)。

两种方式都占用 8770，与 main 的 agent-service 二选一。用真实 Gateway 验收见 [docs/platform-gateway-test.md](docs/platform-gateway-test.md)。

## 配置

沿用 main 的变量：`AIVIRTEACH_AGENT_TOKEN`、`AIVIRTEACH_AGENT_HOST`/`PORT`、`AIVIRTEACH_AGENT_CORS_ORIGINS`、`AIVIRTEACH_COURSE_DIR`、`AIVIRTEACH_GATEWAY_URL`、`AIVIRTEACH_DIAGNOSTIC_TOKEN`、`AIVIRTEACH_MODEL_PROVIDER`（`fake`、`ollama`、`openai_compatible`，默认 `fake`）、`AIVIRTEACH_MODEL_BASE_URL`（根地址如 `https://api.deepseek.com/` 会自动补 `/v1`）、`AIVIRTEACH_MODEL_API_KEY`、`AIVIRTEACH_MODEL_NAME`、`AIVIRTEACH_MODEL_THINKING`（只有 `enabled` 才开启思考）、`AIVIRTEACH_AGENT_TOTAL_TIMEOUT`（默认 40 秒，排队时间也计入）、`AIVIRTEACH_TOOL_TIMEOUT`（8）、`AIVIRTEACH_MAX_REASONING_TURNS`（4）、`AIVIRTEACH_MAX_TOOL_CALLS`（6，含被拒绝的调用）、`AIVIRTEACH_MAX_TOOL_OUTPUT_CHARS`（32768）、`AIVIRTEACH_MAX_CONCURRENT_REQUESTS`（4）。`AIVIRTEACH_MODEL_TIMEOUT` 不使用：DSH 由总超时和预算插件约束。

看图（可选，不设模型名就不看图）：`AIVIRTEACH_VISION_MODEL_NAME`（例如 DeepSeek 的 `deepseek-flash`）、`AIVIRTEACH_VISION_MODEL_BASE_URL`（默认沿用 `AIVIRTEACH_MODEL_BASE_URL`）、`AIVIRTEACH_VISION_MODEL_API_KEY`（默认沿用 `AIVIRTEACH_MODEL_API_KEY`；本地 Ollama 等不需要密钥的可以都不设）、`AIVIRTEACH_VISION_TIMEOUT`（默认 12 秒，范围 3～30）、`AIVIRTEACH_SCREEN_MAX_PER_HOUR`（每台 VM 每小时最多看图次数，默认 10，0 表示不限）。地址不是根地址或 `/v1` 结尾时不看图，并在启动日志里提示。

DSH 专用：`AIVIRTEACH_DSH_CONTEXT_WINDOW`（32768）、`AIVIRTEACH_DSH_MAX_OUTPUT_TOKENS`（4096）、`AIVIRTEACH_DSH_STATE_DIR`（每次请求的临时目录，结束即删；启动时删除上次进程被杀时留下的目录）。关闭思考时固定发送 `reasoning_effort: none`（DeepSeek、Ollama 实测都接受）；运行账号由镜像的 `USER 1000`（`dsh-runner`）和 compose 的 `user` 保证。

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
- `sandbox/screen_eval.py`：用真实看图模型评测截图（服务器测试 T11 使用）：`python -m sandbox.screen_eval sandbox/screens/cases.json --runs 3`。`sandbox/screens/` 是 4 张模拟的课程桌面截图（假密钥）和期望值，`make_mock_screens.py` 可以重新生成（需要 Playwright）。
- `sandbox/tests/test_screen_flow.py`：提示词里的画面描述、带截图时预算和事件与不带时相同、画面引用不影响 main 的校验、收尾阶段、回答末尾的说明。
- `sandbox/tests/test_precheck.py`：课程配置有效且每项预先检查都在范围内、范围的确定（课程配置、别名、课时、Server 给的范围优先、未知课程用 main 的默认值、配置文件损坏）、并行检查和事件、Gateway 出错、数据截短、工具桥接着用编号和结果、接进诊断流程后的事件顺序和提示词。
- `sandbox/tests/test_diagnostics.py`：评估没通过校验时记录是哪一类问题（不含原文）、慢请求和失败请求的调用摘要日志、监控只记原因码、提示词里稳定的部分在前（利于缓存）、启动时清理遗留目录。
- `sandbox/pipeline_eval.py`：用真实模型把整条诊断流程重复跑多次（假 Gateway 按 `lab_id` 模拟容器停止、端口冲突、没有创建容器、一切正常 4 种环境；模拟截图），统计是否答对、完成率、耗时、评估从第几秒开始、token、缓存命中率和失败类别：`python -m sandbox.pipeline_eval --runs 5`；`--mode model` 是对照组（同样的工具范围、不预先检查）。
- `sandbox/tests/test_vision.py`：看图的请求格式、整屏 → 放大两次看图和坐标换算、密钥过滤、输出格式错误、HTTP 错误和重试、超时和取消、结果复用、每台 VM 的次数限制、接入请求流程、用量合并、监控里不含图片和看图原文（看图模型用 httpx 的模拟传输，不连真实模型）。

## 实测（本地，假 Gateway）

- DeepSeek `deepseek-v4-flash`：按授权依次调用容器、端口、日志工具并引用证据回答；`usage` 与提供方一致。
- Ollama `qwen3.5:9b`：工具调用和证据正常，`usage` 为 null（不按缓存计量，与 main 一致）。
- 每个并发请求 1 个 DSH 运行时进程，约 11 个线程、190MB 独占内存；首字 0.35～3 秒。调用方断开或总超时后通知 DSH 停止，实测 0.3 秒内退出。
- 固定开销（假模型、立即回答）：每个 DSH 阶段启动约 0.5 秒、退出约 0.3 秒；带工具时连接 MCP 工具桥再多约 1 秒。一个带工具的请求约 2.9 秒后才出第一个字，与模型无关。
- 2026-11-09 整条流程重复评测（`sandbox/pipeline_eval.py`，DeepSeek `deepseek-v4-flash` + 看图 `deepseek-flash`、假 Gateway、模拟截图，4 种请求各 10 次）：40/40 completed；3 次评估 JSON 不完整，都由再要一次评估救回（每次多约 4.5 秒）。平均总耗时 / 首字：带工具范围 12.6 / 10.8 秒，界面问题带截图 14.6 / 13.2 秒，环境问题带截图 15.6 / 13.9 秒，坏截图 10.2 / 8.7 秒；平均 token 1.0 万～1.9 万，其中输入缓存命中约 80%，输出约 1,000～1,500。
- 2026-11-09 预先检查对比（同上的模型和环境；假 Gateway 模拟容器停止、端口冲突、没有创建容器、一切正常；5 种请求各 5 次）：

  | | 答对 | 平均总耗时 | 平均首字 | 平均 token | 未命中缓存的输入 | 输出 |
  |---|---|---|---|---|---|---|
  | 预先检查（上线做法） | 25/25 | 10.8 秒 | 9.1 秒 | 1.12 万 | 2,245 | 1,030 |
  | 对照组：同样的工具范围，模型自己查 | 21/25 | 13.4 秒 | 11.6 秒 | 1.65 万 | 2,406 | 1,229 |

  - 评估阶段的模型调用从平均 2.7 次降到 1.6 次；界面问题从 2 次降到 1 次。环境问题模型通常还会再查一次日志或 `compose.yaml`。
  - 对照组“没有创建容器”只答对 1/5：模型把 6 次工具调用用完（`TOOL_CALL_BUDGET_EXHAUSTED`），结果是 partial。预先检查最初也占用这 6 次，同样的问题 3/5，改成不占用后 5/5。
  - 评估从第 1.1 秒开始（带截图时约 2.5 秒，等看图）：预先检查和看图都藏在 DSH 启动的时间里。用假模型测，带工具的启动（含连工具桥）约 1.9 秒，在准备提问期间全部被覆盖（准备完 0.02 秒后就发出第一次模型调用）。
  - 界面问题带截图：总耗时 10.9 秒、首字 9.5 秒、7,480 token；改之前（提交 3d01c38，同一张模拟截图、同样的问题）是 14.6 秒、13.2 秒、1.36 万。

## 当前边界

- 每个请求至少 2 次 DSH 运行（评估、回答），比一次生成多约 3 秒、多约 40% token；学生在评估完成后才开始看到文字。
- 评估里的 `limitations` 是模型写的说明文字，与 main 一样并入返回的 `limitations`。
- 回答超过输出上限时保留已生成内容，标记 `partial` 和 `MODEL_STREAM_OUTPUT_TRUNCATED`。
- 2026-11-08 第 1 轮服务器测试（真实 Gateway、真实 VM、镜像构建、监控）已做；之后的改动（含工具范围和预先检查）要在下一轮服务器测试里确认。
