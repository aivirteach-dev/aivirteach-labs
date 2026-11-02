# 效率优化第四阶段：模型接入、取消与模型生命周期

Ubuntu `/home/xf/projects/dsh` 是唯一业务开发位置。本轮不改 AIVirTeach，不连接 Mac，不启动正式服务。最终检查数量与指纹以 `development/efficiency-stage4-20261101.json` 和 `latest-tests.json` 为准。

## 现在怎样运行

```text
学生各自的课程 / 任务 / 少量记忆
                   │
总准入 → 资料准备 → 收尾预留 → 公平等待模型名额
                                     │
                       取得本请求模型所有权
                                     │
                       独立 Python + DSH 运行时
                           ├─ 统一模型预算出口
                           ├─ 当前提供方适配器 → 共享模型服务
                           ├─ 已授权 MCP 工具
                           └─ 公开 Agent.cancel ← 本请求取消信号
                                     │
                       明确结束？ ─否→ 暂停新推理，等待核实
                           │是
                           ▼
                  释放执行名额 → 校验 / 双保存 → 返回

可信后端维护入口 → 同一模型所有权 → 统一暖机 / 空闲卸载
                                 ↑
                 有推理、状态未知或其他控制正在运行，不能卸载
```

保留模型并发1、总在途5、准备2、收尾预留2。每名学生拥有自己的记录，计算服务共享；不会创建一个永久驻留的学生 Agent 进程。

## 1. 换模型改哪里

`dsh_tutor/providers.py` 定义 ProviderSpec 和 Capabilities。现有配置没有 `provider` 时仍走已验证的 `ollama-local`。需要第二提供方时，由可信宿主把 `config/provider.example.json` 作为完整 `provider` 对象放进运行配置，另外填写实际 model/context_window/max_output_tokens 和已验证的 reasoning_off_value。

第二路径只实现 OpenAI Chat Completions 兼容协议，不等于支持所有模型或全部厂商接口；需要别的协议时新增适配器。开发者角色、输出上限字段通过显式配置选择。当前固定 DSH 的 pi-ai 要求上下文至少8192，reasoningEfforts 表需要非 off 等级；保留原来的 low 映射但正式入口仍仅允许已验证的 off/None，不开放新推理等级。真实模型能力、上下文和默认推理参数仍需验收。

密钥只从 auth_env 引用的宿主环境变量读取，不写入 JSON、临时 patch、错误或看板。配置拒绝地址中的账号密码、query、fragment 和未知字段；模型端点不能由学生提供。通用适配器使用 SDK 的现有鉴权路径。宿主应只传所需的运行环境；本轮未更改SDK继承环境的既有行为。

课程、学习目标、工具定义和对话语义输入不依赖品牌。组合验收比较两条路径完整消息与工具定义，只归一化 DSH 自动附加的本次临时工作目录，不能删除目标或历史来制造“一致”。这不保证不同模型回答相同或质量相同。用量、准确Token、硬件、生产并发能力未知时保持 unknown/null，不把估算当计费。声明 tools=unsupported 时发送前拒绝工具请求；unknown 不被伪装为已验证支持。

## 2. 取消的真实含义

| 状态 | 行为 |
|---|---|
| 排队或发送前取消 | 不启动提供方请求，明确记录 not_dispatched |
| 提供方请求已经发送 | 父进程创建本次私有取消标记；插件只定位本次session，调用公开 Agent.cancel({kind:'user'}) |
| DSH本地空闲 | 仅说明本地循环停止；不是远端服务器/GPU停止回执 |
| 取消后成功结果晚到 | 原校验和双保存成功才交付，标记 late_success，不再推理 |
| 断线、强制终止或取消未确认 | 保留 outcome_unknown，暂停新模型请求；管理员核实远端并对账后恢复 |

SDK 0.2.1rc2 没有公开逐提问取消/逐会话关闭 RPC。新增 `execution_control.mjs` 只桥接公开核心 Agent 接口，不修改 vendor 源码、不发明 SDK 方法。标记与状态文件固定在请求运行目录，模型无法指定其他学生；轮询25ms，有界小文件，无提示词或原始异常。

统一模型预算插件不能把适配器合成的 error/abort/未知 finish 当成远端终态。只有已识别的完成原因结束本次流；缺失终态保留 unconfirmed_calls。预算耗尽只有在此前请求均已明确结束时才成为安全拒绝。父进程还检查正常退出时 unconfirmed_calls 必须为0。

`OperationView.execution_cancellation` 是新增向后兼容字段：not_requested/requested/not_dispatched/late_success/outcome_unknown。旧 cancellation 字段保留；未知取消不得显示 not_running。通用HTTP适配器没有可信远端取消回执，因此能力表不能声明 remote_cancel=supported。未来须按实际服务的任务ID和停止回执另加适配并测试。

## 3. 模型名额的所有权

`ModelOwnership` 由同一个 ResourceManagedTutor 复用，记录随机租约、请求引用、有限deadline和取消申请。到期或收到取消不自动释放远端计算容量；未知终态进入隔离，必须显式核实。续期不能复活已到期/取消租约。正常结束或明确未发送才退回，取消本身不是释放凭证。并发调低保留在途租约，只阻挡新增。

当前实现仍是单进程。一个操作日志的排他锁和启动暂停继续生效；进程崩溃/重启通过已有持久日志保留故障并要求核实。内存租约不冒充持久分布式租约，多个不同日志/服务进程不能共用本实现宣称全局限流。未来多进程部署应把 claim/renew/cancel/settle/verify_idle/maintenance 连接到唯一协调者，验收故障接管后才增加进程，不先装 Redis 或队列制造复杂度。

## 4. 暖机和空闲卸载

`OllamaLifecycle` 使用原生 `/api/generate` 空提示和 keep_alive，而不是给模型发送聊天指令。配置地址严格限制为原提供方同一主机；不跟随重定向、不走代理。响应最多16KiB、超时最多30秒、无重试；卸载必须收到 done=true、匹配model和 done_reason=unload。只确认服务回执，不声称已测量释放多少Mac内存。

可信宿主在启动时可调用 `tutor.configure_model_lifecycle(config, idle_seconds=300, timeout=5)`，不联网。控制面再显式调用 `service.maintain_model('warm')` 或 `service.maintain_model('unload_if_idle')`。有在线操作立即 maintenance_busy；未到空闲阈值返回 deferred；控制不明确则隔离并暂停。推理和控制共用同一所有权，互斥。没有学生HTTP/MCP暖机或卸载入口。

本轮只在固定假服务上验证此路径。没有配置自动定时器、没有改变 Mac 现有空闲释放设置；服务器部署时由唯一可信宿主按实际需求触发，不能多个定时器竞争控制同一模型。其他应用直接调用同一模型不受本服务名额约束，正式部署必须统一入口或另行协调。

## 5. 暖DSH进程实验的决定

测试只复用一个实验运行时两次，固定假响应、两个独立session、预算有上限，结束必关闭。公开Agent注册表从1增加到2，未逐会话回收；SDK没有公开逐会话关闭，当前 MCP 授权及模型预算又是请求绑定。第二次可减少启动开销，但这不足以证明长期池的权限隔离、内存回收或真实收益。

因此正式运行保留每请求独立进程，不交付未验证的“关闭开关的暖池”。实验代码集中 sandbox，可删除；正式模块不导入 sandbox。报告记录实际启动/提问时间、直接子进程RSS及会话数；不是完整进程树RAM、Mac内存或服务器吞吐。以后要启用暖池，须有明确会话/工具卸载与预算重置、最大会话数/寿命/RSS和异常回收证据，再单独升级。

## 6. 验证与边界

回归覆盖提供方配置脱敏、未知能力、预算、取消隔离、租约到期/续期、控制互斥、空闲阈值、异常隔离、晚到保存/去重、重启暂停和管理只读等。组合入口 `sandbox/runtime_optimization_acceptance.py` 用实际安装DSH对本机假提供方发7次请求（含暖进程实验2次），验证普通与工具输入适配、取消和生命周期决定。并复测前三阶段组合，真实模型调用0。

管理前端继续用 Next/React/TypeScript，新增模型名额/隔离/控制状态说明；配置摘要显示能力，不探测模型。正式平台认证、真实学生、真实Gateway、Windows/Linux真实诊断、多服务器共享预算、模型质量及容量仍留在服务器阶段。本轮没有部署、迁移正式数据库或push仓库。

## 官方依据

- [固定版本 Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/python/sdk/README.md)：公开Python生命周期接口。
- [固定版本核心 Agent](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/packages/core/agent/README.md)：核心取消与作用域。
- [取消原因类型](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/packages/core/session/src/types.ts)：user取消原因。
- [Ollama原生生成API](https://docs.ollama.com/api/generate)与[空闲加载说明](https://docs.ollama.com/faq)：空请求预加载、keep_alive和卸载。

接口能力从官方固定版本及实际组合核对；暖池取舍是本项目根据实验和授权边界作出的工程决定，不是DSH官方要求。
