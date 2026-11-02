# AIVirTeach 接入接口说明

主要开发位置为Ubuntu /home/xf/projects/dsh；本项目独立于AIVirTeach，尚不是可直接上线的替换包。2026-10-31已完成第三阶段本地接入/故障准备：自有dsh-tutor.v1提交+进度查询、持久交付、取消与显式重启对账；完整当前说明见docs/integration-stage3.md。下方按日期列出的早期步骤均为历史记录，不能用来推断最新状态。

## Python入口

调用dsh_tutor.course.answer_course(request, config_path=..., state_dir=..., tool_bridge=None, conversation=None, conversation_limits=None)。连续对话可信输入及范围规则见conversation-context.md；持久问答使用chat_session.answer_chat，通过ChatStore自动恢复前文并保存，详见persistent-chat.md；本底层answer_course保持无存储调用。

- CourseQuestion：request_id、course、question；CourseSnapshot：course_id、version、lesson_id、title、locale、goals、instructions及可选material来源。locale默认zh-CN；HTML未规范化的目标可为空，不能编造。
- config_path集中管理模型和运行限制；当前只实现Ollama配置路径，其他提供方需明确适配和验收。
- state_dir由可信调用方指定，运行数据按独立run_id存放；生产还需定义学生归属、并发及保留规则。
- tool_bridge只接受可信应用配置，指定受信入口模块及策略上下文，不能来自学生问题或模型。当前演示使用sandbox.mcp_server；正式代码不导入sandbox。
- 有工具时启动前检查请求/课程/版本绑定；实例、学生、允许服务与来源由可信上下文提供。这不等于真实身份认证已完成。

返回CourseAnswer字典：request_id、run_id、status、answer、model、elapsed_seconds、tool_calls、tool_attempts、tool_trace、evidence、quality、context_usage、limitations。

quality区分事实与受限说明，详见result-validation.md。有工具时关键事实由程序生成，模型只选本轮证据、课程原文和有限建议；自由草稿不直接展示。无工具问答保留原行为。

completed不表示语义全正确或学生过关；unknown、工具失败、缺证据、证据拒绝、补充说明无效均返回partial。底层配置/输入错误抛ValueError/Pydantic错误，运行失败和总超时仍可能抛RuntimeError；新AgentService/agent_http负责交付状态及稳定错误码，底层入口不另写传输逻辑。

## 与AIVirTeach既有契约的关系

platform_adapter已显式转换course/current_step/question/request_id并核对可信lab_id，完整保留受限教学字段。平台非空history/learner_state/diagnostic_scope尚未映射，明确拒绝而非静默丢弃。本地ChatStore恢复有限前文；自有HTTP不是完整平台替换。可选DiagnoseResponse投影仅课程问答partial/usage=null；未映射工具证据拒绝投影，真实诊断/Gateway/平台保存/SSE仍待适配。

真实学生授权、VM归属与生产服务未实现。DSH底层仍有高权限宿主策略；MCP及虚拟环境均不等于系统沙箱。

## 部署路线

Ubuntu开发和模拟回归 → 明确隔离的服务器测试环境 → 一名学生一台只读测试VM → Windows/Linux分别验收 → 少于10人展示 → 面向30～50人目标进行容量与隔离验收。在线人数不等于同时推理数，Mac目前串行。

服务器主机、系统/架构、独立目录/端口/数据及回退需确认后部署。模型服务可与Agent分开。不能跨系统复制.venv；依赖和DSH运行时在目标平台重新安装。POSIX本次进程组清理有测试，远端模型取消未验证。原型180秒总时限与现有Server普通请求60秒仍需对齐。

未登录或部署任何真实测试服务器，未改动AIVirTeach仓库；密钥和真实学生数据不进HTML或Git。

## 教材适配

MaterialRepository按精确课程/版本/语言/课时生成CourseSnapshot；详见course-materials.md。当前是本地内容导入版，不能静默覆盖Server权威课程或冒充平台发布版本。material来源范围不授予工具权限；只收到摘录时不能将未检索到当成整门教材不存在。answer_chat自动恢复当前范围有界前文；底层answer_course仍只接收显式前文。平台存储和权限由Server适配，不让DSH直接连接数据库。

## 学习记忆接入

answer_chat可选learning_memory=store和memory_limits，由同一存储实例事务协调。先显式migrate_learning_memory升级v1到v2，不安装时自动迁移。四种显式前缀、查看/撤销、学习摘要与资源边界见learning-memory.md；实际模型验收单独记录，不代表平台学生画像已上线。

## 资源门控与平台准备

本地持久问答由ResourceManagedTutor统一限制，配置与限制见resource-controls.md、preparation-stage2.md。第三阶段AgentService复用该实例，自有提交+轮询接口避免把整次推理阻塞在一次普通请求里。固定平台普通60秒与本地180秒模型时限/240秒操作软期限仍需实际接线；不是将正式Server改为新协议。用户确认服务器未准备，本轮仅建立本地模拟适配器，未访问真实平台。

## 任务记忆接入边界

本地新增TaskRepository/TaskMemoryStore和TaskMemoryAPI，见task-memory.md。正式Server未来按可信学习范围绑定任务；TaskMemoryAPI不接收客户端身份，TaskMemoryStore自动启用学习记忆与最终版本保护，ChatReply.task_id供页面按归属展示。课时默认任务不等于平台已有task字段，正式exercise/lesson映射仍待源码与服务器契约确认。不得用学习任务ID作登录凭据。正式存储实现沿用行为契约，不要求复制SQLite表。

## 生命周期与管理员接口

新增TaskLifecycle和可选DiagnosticStore，本地SQLite显式v4迁移；平台实现相同行为契约，不要求复用本地表。学生API只新增任务状态预览/应用，管理员容量、整库备份、验证恢复不开放给Agent或普通学生。LocalMaintenance是本地SQLite适配，正式PostgreSQL应结合平台现有备份/授权，不能把演示CLI当生产恢复服务。详见lifecycle-maintenance.md。
