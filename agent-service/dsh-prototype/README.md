# DSH 课程助教独立测试模块

本模块位于 Labs 的 `agent-service/dsh-prototype/`，用于验证 DeepSeek Harness、受控 MCP、课程问答、学习记忆、资源调度、异步交付和只读管理界面。它没有替换上级目录的现有 Agent，不修改仓库根 Compose，也不会因为下载代码而自动启动。

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

## 已有接口和兼容边界

接口版本 `dsh-tutor.v1`，严格字段 Schema 为 `docs/service-contract.schema.json`。可信宿主负责注入认证解析器和学生/任务/课程/VM 绑定；不能从学生正文构造授权。

| 方法与路径 | 作用 |
| --- | --- |
| `GET /v1/tutor/capabilities` | 已认证的能力查询 |
| `POST /v1/tutor/operations` | 提交；新操作 202，同编号同正文重发 200，不重复推理 |
| `GET /v1/tutor/operations/{operation_id}?after=0` | 有界进度、状态和已保存结果 |
| `POST /v1/tutor/operations/{operation_id}/cancel` | 正文 `{}`；请求取消，远端未知不伪报停止 |

HTTP 工厂仅监听本机回环；这是后端宿主接口，不是直接暴露给浏览器的学生登录接口。没有配置真实鉴权/数据绑定的可直接部署入口。`succeeded` 只确认 Agent 聊天及操作记录保存，平台保存仍未确认。用量未知是 null，不是 0。见 `docs/integration-stage3.md`。

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
- Docker、常驻启动装配、真实平台认证/Gateway、远端取消、多人容量和正式发布均待后续阶段。停止本地进程不能证明远端模型已停止。
- 本模块的源码版本由 Git 提交确定；升级在一个开发工作副本进行，服务器只运行已记录提交。回退优先 revert 或切回明确测试版本，不影响原运行目录。

目录：`dsh_tutor` 为复用业务模块；`sandbox` 为模拟工具、演示和测试；`tools` 为准备/检查/维护工具；`management_frontend` 为 Next/React/TypeScript 界面；`docs` 为契约与模块说明。验证清单见 `docs/public-release.md`。
