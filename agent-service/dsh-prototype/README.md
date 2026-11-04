# DSH 课程助教独立测试模块

本模块位于 Labs 的 `agent-service/dsh-prototype/`，用于验证 DeepSeek Harness、受控 MCP、课程问答、学习记忆、资源调度、异步交付和只读管理界面。它没有替换上级目录的现有 Agent，不修改仓库根 Compose，也不会因为下载代码而自动启动。

第二阶段新增独立容器环境，详见 [隔离环境说明](docs/isolated-environment.md)：仅固定假提供方和虚构身份，默认暂停，显式准备/启动/放行，不接真实平台或VM。

2026-11-02 从独立原型的已验证候选包整理。业务模块保留；真实教材原件和完整提取资料不公开。公开源码、私有资料和实际模型验收分别记录。历史文档中的机器路径、阶段数量和模型结果属于原开发环境，不表示本分支已接通真实平台。

## 从干净目录准备

已验证目标：Ubuntu/Linux x86_64、Python 3.10.12、DSH SDK/运行时 0.2.1rc2、MCP 1.28.0、Node 22.13.0。Python 和前端依赖分别锁定在 `requirements.txt`、`management_frontend/package-lock.json`。Windows/ARM 和其他版本未验收。

在独立 clone 的仓库中进入本模块，再执行：

```bash
cd agent-service/dsh-prototype
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# 先安装并选用 Node 22.13.0；此命令不下载软件、不调用模型。
.venv/bin/python tools/prepare_local.py --node-path "$(command -v node)"
.venv/bin/python tools/check_service_contract.py
.venv/bin/python -m unittest discover -s sandbox/tests -q
cd management_frontend
npm ci
npm run typecheck
npm run build
```

缺少 venv/ensurepip 时由环境管理员安装匹配的 `python3-venv`，不要 sudo pip 修改系统 Python。准备脚本不覆盖既有配置，只创建被忽略的 `config.json`、空结果目录和固定 Node 路径链接。默认模型地址 `http://127.0.0.1:1/v1` 不可用，正式运行身份仍是 `dsh-runner`。真实模型入口不可混入默认回归；模拟测试会在临时配置中显式使用当前测试身份。

## 平台接口（与 main 现有 Agent 相同）

`dsh_tutor/platform_app.py` 提供与 `agent-service/aivirteach_agent` 完全相同的接口，Server 和 Client 不需要改动：`GET /health`、`GET /ready`、`POST /v1/agent/diagnose`、`POST /v1/agent/diagnose/stream`（SSE）、`/openapi.json`。请求/返回格式和课程资料补充逐字复制自 main（`platform_models.py`、`platform_course.py`），`sandbox/tests/test_platform_api.py` 自动对比两边的 JSON Schema；事件名和帧格式（`schema_version`、`request_id`、`sequence`）与 main 一致，`assistant_delta` 由预算插件转发模型的逐段输出。不保存聊天或学习记录，这些由 Server 负责。

### 启动

- 宿主机：`./start_platform.sh`，读取与 main 相同的 `agent-service/config/agent.env`（可用 `AIVIRTEACH_AGENT_ENV_FILE` 指定）。
- 容器：`docker compose -f compose.platform.yaml --env-file ../../config.env up -d --build`（`deploy/Dockerfile` 的 `platform` 阶段）。

两种方式都占用 8770，与 main 的 agent-service 二选一。

### 配置

沿用 main 的变量：`AIVIRTEACH_AGENT_TOKEN`、`AIVIRTEACH_AGENT_HOST`/`PORT`、`AIVIRTEACH_AGENT_CORS_ORIGINS`、`AIVIRTEACH_COURSE_DIR`、`AIVIRTEACH_MODEL_PROVIDER`（`fake`、`ollama`、`openai_compatible`，默认 `fake`）、`AIVIRTEACH_MODEL_BASE_URL`（根地址如 `https://api.deepseek.com/` 会自动补 `/v1`）、`AIVIRTEACH_MODEL_API_KEY`、`AIVIRTEACH_MODEL_NAME`、`AIVIRTEACH_MODEL_THINKING`（只有 `enabled` 才开启思考）、`AIVIRTEACH_AGENT_TOTAL_TIMEOUT`（默认 40 秒，排队时间也计入）、`AIVIRTEACH_MAX_CONCURRENT_REQUESTS`（默认 4）。

DSH 专用：`AIVIRTEACH_DSH_CONTEXT_WINDOW`（默认 32768）、`AIVIRTEACH_DSH_MAX_OUTPUT_TOKENS`（默认 4096）、`AIVIRTEACH_DSH_REASONING_OFF_VALUE`（关闭思考时发给提供方的值，默认 `none`）、`AIVIRTEACH_DSH_STATE_DIR`（每次请求的临时目录，结束即删）、`AIVIRTEACH_DSH_EXECUTION_USER`（设置后核对运行账号，镜像内为 `dsh-runner`）。

### 资源（实测，qwen3.5:9b / Ollama）

每个并发请求起 1 个 DSH 运行时进程，约 11 个线程、190MB 独占内存（另有约 180MB 可共享的程序文件页）；首字 0.35～0.93 秒。调用方断开或总超时后会通知 DSH 停止生成，实测 0.3 秒内进程退出，名额在进程真正结束后才归还。

### 当前边界

- 尚未接入 Diagnostic Gateway 工具：`limitations` 含 `LIVE_DIAGNOSTICS_NOT_RUN`，`evidence`/`tool_trace` 为空。
- `usage`：DeepSeek（`deepseek-v4-flash`）已实测，命中/未命中/输出三项与提供方一致；总数对不上或使用 Ollama 时为 null，不猜测（与 main 规则一致）。
- 关闭思考：DeepSeek 已实测接受 `reasoning_effort: none`；不关闭时默认思考会占满输出预算，因此除非设为 `enabled`，否则一律关闭。
- 回答超过输出上限时保留已生成内容，标记 `partial` 和 `MODEL_STREAM_OUTPUT_TRUNCATED`。
- 容器部署文件未在 Docker 中实际构建验证。

## 已有接口和兼容边界

接口版本 `dsh-tutor.v1`，严格字段 Schema 为 `docs/service-contract.schema.json`。可信宿主负责注入认证解析器和学生/任务/课程/VM 绑定；不能从学生正文构造授权。

| 方法与路径 | 作用 |
| --- | --- |
| `GET /v1/tutor/capabilities` | 已认证的能力查询 |
| `POST /v1/tutor/operations` | 提交；新操作 202，同编号同正文重发 200，不重复推理 |
| `GET /v1/tutor/operations/{operation_id}?after=0` | 有界进度、状态和已保存结果 |
| `POST /v1/tutor/operations/{operation_id}/cancel` | 正文 `{}`；请求取消，远端未知不伪报停止 |

HTTP 工厂仅监听本机回环；这是后端宿主接口，不是直接暴露给浏览器的学生登录接口。现有隔离宿主只绑定固定虚构身份，不是带正式平台鉴权的部署入口。`succeeded` 只确认 Agent 聊天及操作记录保存，平台保存仍未确认。用量未知是 null，不是 0。见 `docs/integration-stage3.md`。

`platform_adapter.py` 只实现固定版本的有限课程问答转换：非空 history/learner_state/diagnostic_scope 拒绝，未映射诊断工具证据拒绝，有限平台响应必须 partial。它不等于完整兼容现有同步 diagnose/SSE 接口。真实 Server 请求、可信身份、完整证据和平台最终保存必须另验收，不新建第二套模型循环。

## 私有教材与测试分类

默认 `sandbox/tests` 包含通用教材解析的虚构 HTML 样例，不依赖真实教材。原候选包的 467 项拆分为公开基础 465 项和私有教材专项 2 项；这是分类变化，不是删除校验。

获准的教材保管者可在本模块内提供被忽略的 `inputs/courses/` 和 `materials/courses/`，保持来源版本与指纹一致；原件路径为 `inputs/courses/ai-daily-briefing/originals/`，提取资料保留原版本子目录。然后执行：

```bash
.venv/bin/python tools/check_private_materials.py
.venv/bin/python -m unittest discover -s sandbox/private_tests -q
```

校验器不下载、不执行教材命令、不创建库；缺失或校验失败明确非零退出。`--low-cost-demo`、真实教材模型验收也要求该资料。`sandbox/resource_acceptance.py` 是依赖外部固定参考源码的历史检查，不是本模块自包含入口。

## 模拟组合验证与管理界面

准备完成后，可以分别运行 `sandbox/integration_acceptance.py`、`sandbox/efficiency_acceptance.py`、`sandbox/completion_acceptance.py` 和 `sandbox/runtime_optimization_acceptance.py`。它们使用虚构数据与本机假提供方，不验证模型质量、真实 VM 或硬件容量。

前端构建后运行 `.venv/bin/python tools/start_management.py --completion-demo`，页面监听 `127.0.0.1:3002`，后端 `127.0.0.1:8788`；虚构数据、随机本机能力令牌，不是平台管理员认证。不要把它直接公开。进度快照可用 `tools/dashboard.py build` 生成；历史结果与本轮结果分开解释。

## 测试分支的操作边界

- 不合并 main、不覆盖原 Agent、不启动根 Compose，不在已有服务器目录 pull/checkout/reset/clean/stash。
- 服务器单独 clone 并固定 commit；模型、端口、数据库和测试 VM 必须显式配置，私有资料不进 Git。
- 调度与模型所有权当前是单进程；不得增加 worker/副本并声称全局并发仍受限。
- 已有隔离容器与常驻测试装配；真实平台认证/Gateway、真实模型、远端取消、多人容量和正式发布均待后续阶段。停止本地进程不能证明远端模型已停止。
- 本模块的源码版本由 Git 提交确定；升级在一个开发工作副本进行，服务器只运行已记录提交。回退优先 revert 或切回明确测试版本，不影响原运行目录。

目录：`dsh_tutor` 为复用业务模块；`sandbox` 为模拟工具、演示和测试；`tools` 为准备/检查/维护工具；`management_frontend` 为 Next/React/TypeScript 界面；`docs` 为契约与模块说明。验证清单见 `docs/public-release.md`。
