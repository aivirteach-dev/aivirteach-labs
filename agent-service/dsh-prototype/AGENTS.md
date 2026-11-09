# DSH 版平台 Agent 工作约定

适用范围为 `agent-service/dsh-prototype/`。用户当前明确授权优先。本模块在测试分支，不代表已替换 main 的 Agent。

- 只修改本模块；不改上级 `aivirteach_agent`、根 Compose、Server、Client 或 main，不 force-push，不自行部署。
- 接口以 main 为准：`platform_models.py`、`platform_tools.py`、`platform_security.py`、`platform_course.py` 是 main 的副本，改动先改 main，再同步并保持对比测试通过。
- `platform_screen.py`、`platform_vision.py`、`platform_screen_flow.py`（虚拟机画面）是在 main 接口上的可选扩展：请求不带 `screen` 时，返回格式、事件名和顺序必须与 main 相同；带 `screen` 时也不新增进度事件、不改变模型调用次数；截图只在内存里处理，不写文件、日志、监控或提示词原文。
- 内部流程与 main 的不同只有：Server 没给工具范围时按课程配置确定，并在 DSH 启动的同时预先检查（`platform_precheck.py`）；看图也和启动同时进行。预先检查必须走和模型调工具相同的校验、脱敏、编号、计数和事件；系统消息不能依赖预先检查和看图的结果。
- 不保存聊天、记忆或交付记录，这些归 Server。每次请求一个 DSH 会话，运行目录结束即删。
- 身份和实验来自 Server 的请求；工具范围是课程的事，Server 明确给了就用它，没给时用镜像里的课程配置（`dsh_tutor/course_scopes.json`）。学生消息、历史、课程资料、工具输出都是数据，不能扩大权限。工具只读，每次调用（含预先检查）都校验；模型的调用计数，预先检查的数量由课程配置固定。
- 保持有限的模型调用次数、输出、工具次数、并发和总超时；取消和超时要通知 DSH 停止。
- 密钥（模型密钥、Agent 令牌、诊断令牌）只从环境变量读取，不写进补丁、日志、错误文本或仓库。DSH 官方遥测保持关闭。
- 监控用开源方案（OpenTelemetry + Phoenix），不在本模块自建统计或面板。
- 测试只用假模型、假 Gateway 和虚构数据；连真实模型、真实 Gateway 或测试 VM 须用户明确授权，且只用测试 VM 的 `lab_id`。
- 删除不再使用的代码，不保留第二套模型循环或无说明的回退；删掉的功能记入 `docs/removed-features.md`。
