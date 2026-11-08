# 已移除的原型功能

2026-11-06 精简时，平台接口用不到的原型功能从分支中删除。完整原型保留在标签 **`dsh-prototype-full-20261102`**（提交 `a7af3a7`），取回方式：

```bash
git fetch origin tag dsh-prototype-full-20261102
git show dsh-prototype-full-20261102:agent-service/dsh-prototype/<路径>        # 查看单个文件
git worktree add ../dsh-full dsh-prototype-full-20261102                      # 取出完整原型
```

删除原则：平台（Server → Agent → Gateway）当前不调用；或者 Server / main 已经负责同一件事。下表的“什么时候值得加回来”是判断依据，不是计划。

| 功能 | 原来的代码 | 为什么删 | 什么时候值得加回来 |
|---|---|---|---|
| 异步提交和轮询接口（`/v1/tutor/operations`、取消、重放） | `agent_http.py`、`agent_service.py`、`http_budget.py`、`polling.py`、`service_contracts.py`、`platform_adapter.py` | 平台用 SSE 长连接（`/v1/agent/diagnose/stream`），断开即取消；接口已改为 `platform_app.py` | 需要“学生关掉页面后答案仍继续生成并稍后取回”时 |
| 交付库、请求去重、重启对账、保留期 | `operation_journal.py`、`operation_retention.py`、`results.py` | Server 已按 requestId 去重并保存回答；Agent 不再持有交付状态 | Agent 需要独立于 Server 保证“只交付一次”时 |
| 聊天库和对话上下文 | `storage.py`、`conversation.py`、`chat_session.py` | 聊天记录归 Server（`Conversation` 表），历史经请求的 `history` 传入 | 不会：违背“学习数据归 Server”的原则 |
| 学习记忆、任务记忆、检查摘要、数据库迁移 | `learning_memory.py`、`task_memory.py`、`task_memory_api.py`、`inspection_memory.py`、`lifecycle.py` | 同上；且需要学生写固定前缀（如“学习目标：”）才生效，实际难以使用 | 平台决定做长期学习记忆时，应在 Server 侧设计，可参考这里的更正、撤销、来源校验做法 |
| 三段式限流、热调整、学生轮转 | `resources.py`、`scheduler_status.py`、`budgets.py` | 平台接口用一个信号量加总超时即可；模型并发由提供方决定 | 单个 Agent 进程要服务大量并发、需要按学生公平排队时 |
| 共享模型暖机、空闲卸载 | `model_lifecycle.py` | 只对本机 Ollama 有意义；DeepSeek 等托管模型不需要 | 改为自托管模型且显存紧张时 |
| DSH 自己的教材流水线和公共缓存、固定原文动作 | `course.py`、`materials.py`、`material_publication.py`、`direct_actions.py`、`contracts.py`、`tools/import_materials.py`、`tools/publish_materials.py`、`config/materials.json` | 平台用 main 的 `CourseRepository`（`platform_course.py`）和 `process_course.py` | 需要双语教材按版本精确选取、代码块完整保留时，可合并进 main 的课程处理 |
| 管理接口和 Next 管理前端、容量面板 | `management.py`、`management_http.py`、`management_frontend/`、`tools/dashboard.py`、`tools/start_management.py`、`tools/serve_management.py`、`dashboard/` | 监控改用开源方案（OpenTelemetry + Phoenix，见 README）；平台管理后台是 Server 的 `apps/admin` | 不会：由开源监控界面替代 |
| 维护子系统（目录登记、预览删除、备份验证） | `maintenance.py`、`service_maintenance.py`、`diagnostics.py`、`tools/maintain_service.py` | 只用于维护上面这些已删除的数据 | Agent 重新持有长期数据时 |
| 隔离测试环境（虚构身份、假模型容器、nginx 入口） | `isolated_host.py`、`compose.isolated.yaml`、`deploy/nginx-isolated.conf`、`deploy/frontend-entry.cjs`、`sandbox/isolated_provider.py`、`tools/isolated_*.py`、`tools/build_isolated.py` | 部署统一为 `compose.platform.yaml`；本地测试用 `sandbox/fake_gateway.py` 和 `sandbox/fake_model.py` | 不会 |
| 旧运行方式（每请求一个 Python 子进程）、模拟工具 | `runtime.py` 中的 `run_prompt`/`worker`、`run_registry.py`、`execution_control.mjs`、`mcp_bridge.py`、`tools.py`（模拟 `get_service_status`）、`sandbox/mcp_server.py`、`sandbox/mock_tools.py` | 平台在线程中直接运行 DSH；工具改为 `platform_mcp.py` 调真实 Gateway | 不会 |
| 预算插件里的监控字段：按字节估算输入 token、数据块计数、进度事件、“未确认调用”统计 | `plugins/model_budget.mjs`、`execution_budget.py` 中的 `RuntimeMetrics` | 改由 OpenTelemetry 记录真实 token、首字延迟和每次调用的结束状态（监控阶段 2） | 不会：被标准监控替代 |
| 环境变量 `AIVIRTEACH_DSH_REASONING_OFF_VALUE`、`AIVIRTEACH_DSH_EXECUTION_USER`（及运行账号检查函数） | `platform_app.py`、`platform_runtime.py`、`dsh_patch.py`、`deploy/Dockerfile`、`compose.platform.yaml` | main 没有这两个变量；DeepSeek 和 Ollama 都接受 `none`；运行账号已由容器的 `USER 1000` 保证 | 接入不接受 `none` 的模型提供方时，再把关闭思考的值做成配置 |
| 原型阶段的文档、报告、进度记录、演示和验收脚本 | `docs/` 下除平台文档外的全部、`development/`、`sandbox/*_acceptance.py`、`sandbox/*_demo.py`、`tools/architecture_report.py`、`tools/target_architecture.py` | 描述的是已删除的模块，留着会误导 | 不会 |
| 看图的问题分类、界面类问题“评估最多 2 次”、`AIVIRTEACH_SCREEN_REQUESTS` 提示开关、`screen_analyzing` / `screen_analyzed` / `screen_requested` 事件（2026-11-08） | `platform_screen_flow.py`（`classify_question`、关键词表、末尾提示）、`platform_app.py`、`platform_screen.py` | 关键词表要随课程维护，曾误判；调用上限疑似导致服务器测试里的评估失败；提示和事件要 Server、Client 配合，而 Client 自己知道有没有带截图、可以一直显示“附上画面”按钮 | 有数据表明某类问题少调用一次模型能明显提速且不降质量时 |
