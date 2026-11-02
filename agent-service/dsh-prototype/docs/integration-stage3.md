# 服务器测试前第三阶段：接入与故障准备

2026-10-31，本地范围完成。接口版本 `dsh-tutor.v1`。本轮使用虚构平台、真实本地 HTTP/SQLite/线程及受控子进程，不调用 Mac 模型，也不连接正式 AIVirTeach。完整证据见 `development/integration-stage3-20261031.json` 和 `development/latest-tests.json`。

## 学生看到的流程

```text
平台确认学生身份、任务、课程版本和实验环境
                     │
                     ▼
        提交一次问题 → 立即取得操作编号
                     │                    │
                     ▼                    ▼
             原有资料准备与调度       按编号查看真实阶段
                     │                    │
                     ▼                    │
             DSH → 模型／受控工具          │
                     │                    │
                     ▼                    │
        校验 → 聊天保存 → 交付记录保存 ───┘
                     │
                     ▼
        平台接收回答 → 平台保存 → 展示完成
                       （最后两步待正式接入）
```

学生短暂断线不等于问题重新执行。重新提交**相同请求编号和相同正文**，会取得原操作；改变问题内容却继续用旧编号会被拒绝。一个操作可以多次查询进度，查询不会再调用模型。这里提供阶段轮询，不是逐字流式回答或现有平台 SSE 的直接替代。

`request_id` 是平台生成的稳定提问编号，建议 UUID；`operation_id` 是交付操作编号；`run_id` 是原模型运行编号。重发不创建新操作或新运行。`result.replayed` 指聊天层的历史重放/恢复，不统计 HTTP 重发次数。

## 模块和配置

| 位置 | 做什么 | 单独升级时保留什么 |
|---|---|---|
| `service_contracts.py` | 严格输入、响应、配置及 JSON Schema | 版本、字段及状态语义 |
| `agent_service.py` | 提交、查询、取消、可信恢复控制 | 复用 ResourceManagedTutor / ChatSession，不重写模型循环 |
| `operation_journal.py` | 单写进程的有界持久交付记录 | 幂等键、所有权、已完成与待核实状态 |
| `agent_http.py` | 回环地址 HTTP、鉴权绑定注入 | 不接受学生自报身份/数据库路径/工具授权 |
| `platform_adapter.py` | 固定 AIVirTeach 输入字段转换和有限响应投影 | 未支持字段拒绝，不静默丢弃 |
| `storage.py` / `task_memory.py` | 新增按请求编号只读查询 | 每次仍核对当前范围、任务与修订 |
| `management.py` / Next 页面 | 只读交付/恢复聚合 | 未接入和不可读必须显示未知 |

没有安装新依赖，也没有修改 DSH 核心、模型协议或 MCP 工具授权。运行平台仍为 Ubuntu / Python 3.10.12、DSH SDK+runtime 0.2.1rc2；本次不验证新模型能力。正式代码不导入 sandbox。

配置分三处：现有 `config.json` 管模型和运行身份；`config/resources.example.json` 管准备、调度和上下文预算；新增 `config/service.example.json` 管 HTTP 交付入口。部署宿主显式使用 `load_service_settings` 加载自己的配置。示例不包含密钥、地址或学生数据。

| 当前交付限制 | 默认值与含义 |
|---|---|
| 在途操作 | 最多5个；同时受原调度总上限和每学生1个约束。准备2、Mac模型1保持原值 |
| 完整操作期限 | 240秒软期限，包含准备/等待/推理/保存；不是强制杀远端模型 |
| 入口 | 只监听127.0.0.1；同时处理8连接，20次已认证请求/秒；连接读写超时3秒 |
| 输入 / 回答副本 | 请求32KiB，单份结果64KiB；超限拒绝，不截断后假装完整 |
| 操作日志 | 最多1000条、16MiB数据库页；不是整个磁盘/事务日志的硬配额 |
| 阶段事件 | 每操作最近24条；超出窗口标记 reset_required，客户端读当前状态重建 |
| 数据库等待 | 操作日志默认100ms，锁冲突返回稳定错误；源聊天库仍用自己的有界策略 |

记录满了拒绝新操作，保留旧请求的去重信息，不自动清理或重新执行。正式保留/归档、磁盘总预算和备份策略待服务器决定。HTTP socket 超时也不等于能强制打断宿主鉴权适配器；宿主的外部认证/数据库访问仍须设置自身期限。

## 认证与接入方式

HTTP 工厂 `create_agent_server(service, resolve_token, port=8792)` 接收可信宿主提供的解析器。解析器验证令牌后返回 `TrustedBinding(store, handle, course, lab_id, tool_bridge)`；handle 来自当前有效学习范围，store 必须已绑定当前任务，course 是服务端发布版本快照，工具绑定来自服务端授权。

不能从学生 JSON 构建绑定，不能把 UUID、课程编号、VM编号或任务编号当作身份凭据。每次提交、查询、取消均重新检查当前范围；正文里的完整课程快照也必须与权威快照相等。对其他学生的操作查询返回404。本地演示使用随机令牌，**不是**正式 JWT、Operator 权限或生产网关认证。

| 请求 | 结果 |
|---|---|
| GET `/v1/tutor/capabilities` | 实际协议版本、限制、已支持与未支持内容，要求认证 |
| POST `/v1/tutor/operations` | 新提交202；相同请求重发200。都返回 OperationView |
| GET `/v1/tutor/operations/{id}?after=0` | 当前阶段、有限新增事件及最终已保存结果 |
| POST `/v1/tutor/operations/{id}/cancel`，正文 `{}` | 请求取消，返回当前状态，不伪装上游已停止 |

提交示例（身份不在正文）：

```json
{
  "api_version": "dsh-tutor.v1",
  "profile": "course_snapshot",
  "payload": {
    "request_id": "example-1",
    "question": "本课的学习目标是什么？",
    "course": {"course_id": "course-a", "version": "v1", "lesson_id": "l1", "title": "虚构课程"}
  },
  "history_selection": {"mode": "recent"}
}
```

可信平台可以显式选择 earlier 和可选 before 序号，仍受第二阶段范围与读取预算限制；没有自动全历史扫描。JSON Schema 可离线导出，命令见下方。未知字段、重复 JSON 键、NaN、过大正文、跨站 Origin、错误 Host 和多重 Authorization 等被拒绝。生产 HTTPS、登录、令牌续期和反向代理仍待真实部署。

## 保存、取消和错误的含义

| 状态/错误 | 学生和管理员应如何理解 |
|---|---|
| queued / running | 仍在处理，不提前返回未经保存的正文；进度不是完成百分比 |
| succeeded | 聊天库和交付记录均已确认保存；answer_status仍可为partial，不能当完整诊断/语义全正确 |
| failed | 本操作已结束且没有可交付答案；不自动重试推理 |
| recovery_required | 运行或保存状态不明，需要核实；不自动继续或重跑 |
| request_conflict 409 | 同一学生/学习记录/任务内，旧请求编号对应不同内容 |
| student_busy / overloaded 429 | 个人占用或服务总量已满，未接受新操作 |
| verification_required 503 | 重启后或上游状态不明，暂停新提交 |
| journal_capacity / journal_unavailable 503 | 交付存储不足/不可用，不能把结果标成功 |
| unsupported_dynamic_context 422 | 平台动态字段尚未映射，不能丢弃后声称完整兼容 |

等待中的取消会终止 pending，不调用模型。运行中的取消只表示“已提出”；保留线程、请求和模型名额，直到底层实际返回。如果返回的回答通过版本检查并已保存，显示 `completed_after_cancel`，不会谎称已经取消。软期限同样不提前释放名额；若已进模型阶段则暂停新准入，待核实后恢复。远端模型真实取消仍未验证。

进度写入失败不另发一次模型请求；新提交暂停，已开始操作继续按原业务结果和保存状态收尾。聊天保存失败不交付；聊天已存而交付保存失败则待对账，不能拿内存正文冒充成功。每次取成功结果还会核对源聊天；源记录删除/范围变化/不可读时不交付缓存答案。交付副本不参与记忆检索，查询时逻辑清空失效副本；这不等于即时安全擦盘或备份删除，正式保留/删除策略仍待接入。

当前没有可靠的 Token 计量接口，`usage.state=unknown`，三个计数都是null；平台投影usage也是null。不能记作0或据此计费。工具失败可以产生“已保存的受限回答”，但事实状态仍unavailable，不伪造检查成功。

## 重启恢复

```text
启动 → 获得单写所有权 → 暂停新提交
                          │
                          ▼
            遗留未完成操作 → 待核实，不创建模型任务
                          │
                          ▼
           管理员确认旧进程和远端推理已结束
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
        源聊天已经保存             源聊天仍pending
              │                       │
        恢复原回答/编号            结束遗留记录
              └───────────┬───────────┘
                          ▼
             对账全部完成 → 再显式恢复准入
```

只有可信宿主可调用 `reconcile(binding, operation_id, upstream_idle=True)` 和 `verify_ready(upstream_idle=True)`。参数是管理员核实后的声明，不是程序已经检测到远端空闲。HTTP 和 Agent 都没有恢复准入/强杀接口。即使上次干净退出，下次启动也先暂停。Linux flock保证同一日志单写；旧线程未返回时close返回False并继续持有所有权，调用方不能忽略它后在另一日志启动第二份推理服务。

单进程门控与单日志所有权不能充当跨服务器全局配额。真实多进程部署前必须确定统一模型预算、请求路由、归属租约和恢复责任。SQLite源聊天与交付日志是两个库，靠“先保存、再交付、重启对账”处理间隙，不声称跨库原子事务。

## 与当前仓库的差异

固定核对 Server `b7d47a5cd4f6c916fe79fbe0d893272a8925dfba`、Client `f11d3ac2f6ce1959e37e35891070f51b18df10cd`、Labs `8ffb1c042b85affac5cf246b81aeb33b4fd079e6`。未声称核查线上或最新分支，也没有改动这些源码。

| 既有平台 | 本地第三阶段 | 真正接入还需做什么 |
|---|---|---|
| `/v1/agent/diagnose` 普通60秒 / SSE | 自有v1提交+轮询协议，完整操作软期限240秒 | 平台显式适配，协调请求期限、断线和complete；不能改URL直接替换 |
| course/current_step 的教学字段 | summary、摘录、顺序、说明、期望、标准和失败症状完整保留在受限JSON资料块 | 发布版本、教材选取及真实模型质量再验收 |
| question最大4000、history/learner_state/diagnostic_scope | 问题最大2000；非空动态字段明确422；本地历史由ChatStore恢复 | 正式动态字段逐项映射，不能关闭鉴权或截断冒充成功 |
| DiagnoseResponse完整诊断和平台工具枚举 | 可选课程问答投影固定partial，空诊断证据/建议、usage=null | 真实诊断结构和Gateway证据映射；有未映射工具轨迹时投影拒绝 |
| 平台先保存再发送complete | Agent仅确认自己的两次保存，platform=unconfirmed | Server最终保存、额度、消息归属与学生展示确认 |
| 平台认证、课程库、VM代次 | 注入式可信绑定，演示令牌和虚构环境 | 正式JWT/报名/权限/课程版本/Windows和Linux实例映射 |

教学块的新输入形式仅在本轮替身测试验证字段保留，不把它当真实模型教学质量通过。原有真实教材/记忆问答样例是第二阶段历史证据。

## 重现本轮与管理查看

在 Ubuntu 项目目录中，用开发账号运行以下纯模拟命令；它们不调用真实模型：

```bash
.venv/bin/python tools/check_service_contract.py
.venv/bin/python tools/check_service_contract.py --schema-out docs/service-contract.schema.json
.venv/bin/python tools/dashboard.py test
.venv/bin/python sandbox/integration_acceptance.py
.venv/bin/python tools/start_management.py --integration-demo
```

Schema命令只新建指定文件，已存在会拒绝覆盖。管理页仍为127.0.0.1:3002；来源明确 `simulation:integration-demo`。只发一轮有限虚构请求；刷新只读计数和日志，不继续调用模型。Ctrl+C清理本次创建的服务。默认启动不接交付服务时，显示“未接入”，不能推断零错误。

34项早期专项后继续补测，最终全量347项回归（本轮新增39）通过；11项组合断言、Next构建/TypeScript、实际页面核对分别记录在本阶段证据中。强制中断测试只终止自己创建的两个模拟子进程，不终止DSH、Ollama或其他Python进程。

## 隔离服务器的接入与回退

1. 确认独立目录、低权限运行身份、TLS/网络访问、模型提供方、数据库和Windows/Linux只读测试实例。按目标平台重建虚拟环境，不复制Windows .venv。
2. 实现可信绑定解析器、正式ChatStore适配、平台输入/输出及持久化确认。决定采用轮询桥或平台SSE适配；原平台最终保存规则保留。先单学生测试。
3. 使用当前保守并发，验证真实取消、进程清理、日志脱敏、故障恢复、双库保留及权限，再测少于10人展示，最终评估30～50人目标。人数不能直接换算模型并发。
4. 接入前保留旧服务路由和源代码版本；新增接口用独立版本/入口。回退时先停止新提交并处理在途/不明操作，核实上游结束后再切回。保留聊天和操作日志及其请求编号，不通过删库或恢复旧快照让旧请求重新执行。

本轮修改前源代码归档在 `work/integration-stage3-20261031/before/source.tgz`；这是代码回退材料，不是聊天数据库备份。正式备份应在受控停写或一致性快照下保存成套状态并验证恢复，不能边写边简单复制数据库文件。本轮没有迁移已有聊天库、没有上线、push或连接真实平台。
