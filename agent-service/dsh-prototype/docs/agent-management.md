# Agent 管理模块（本地只读第一版）

主要位置：Ubuntu `/home/xf/projects/dsh`。前端 `management_frontend` 使用 Next.js 16.3.8 / React 19.2.0 / TypeScript 5.7.3，与课程工作室采用相同框架；依赖已写入 package-lock.json。项目内 Node.js 22.13.0 的官方安装包已校验SHA256，不改系统Python或Node。

## 启动和查看

先构建（已构建时无需每次执行）：

```sh
PATH="$PWD/.runtime/node-v22.13.0-linux-x64/bin:$PATH" NEXT_TELEMETRY_DISABLED=1 .runtime/node-v22.13.0-linux-x64/bin/node management_frontend/node_modules/next/dist/bin/next build management_frontend --webpack
.venv/bin/python tools/start_management.py
```

浏览器打开 http://127.0.0.1:3002 ，点击“进入本机管理”。Windows需将本机3002经SSH转发到Ubuntu127.0.0.1:3002。服务没有系统自启动；终端Ctrl+C停止本次两个子进程组。Ubuntu关机或SSH转发关闭后页面会断连。

## 已实现的功能

- 状态总览、固定数据库可读状态与schema、主库/WAL/SHM字节统计；显示数据来源和采集时间。
- 诊断样本中相同错误按代码计数；不是全平台失败率。
- 日志每页20条、上限50条；状态、24位请求哈希引用、起止时间筛选；稳定sequence游标翻页和详情时间线。只保留最近200条可用诊断；被淘汰记录返回不可用，不假装从未发生。
- 只读配置摘要：模型名称、窗口和预算；不返回模型地址、内部目录、密钥或聊天正文。
- 浏览器3秒刷新，仅总览；页面后台暂停，单请求执行，退出后拒绝读取。错误保留旧数据并提示过期。
- Python采集结果5秒共享缓存，管理查询只读、锁等待100ms、SQL执行约500ms预算；不扫描全部聊天。HTTP每秒20次、最多8处理线程、连接超时3秒；Next上游2秒超时不重试。
- 管理接口只监听loopback，随机能力令牌只通过服务端子进程环境传递；浏览器取得HttpOnly/SameSite严格cookie。本机进入操作有Host/Origin校验。

## 数据来源与边界

默认启动只读 `sandbox/results/management-demo/fictional.db`。`sandbox/management_seed.py`显式生成三条模拟诊断用于展示成功、模型超时和保存未确认，模型调用0。这些不是实际模型请求，页面明确标注来源。

Agent推理进程在线状态、模型健康、真实Gateway、备份和CPU/GPU指标仍默认未知或未接入。队列已支持两种真实采集方式：`ManagementView(...,gate=actual_tutor.gate.snapshot)`注入同进程问答门控，或由实际问答宿主通过SchedulerStatusPublisher发布固定文件，再由SchedulerStatusReader读取。默认固定入口data/management/scheduler.json，无发布者仍显示未接入；不能创建空gate冒充。文件来源和采集时间单独显示，5秒过期或停止即未知。状态桥不是全局调度或模型健康检查。

调度区显示运行/等待、暂停/停止、策略版本/上限、最早等待、每条活跃请求的随机运行引用/请求哈希/阶段耗时，以及最近64个准入样本的等待与应用耗时。计数包括队列满、学生配额、超时、取消和结果。阶段来自实际answer_chat回调；DSH/模型/工具仍是合并阶段，不虚构GPU或工具细分进度。诊断日志仍在一轮结束后写一次。

需要查看有限动态演示时显式运行`.venv/bin/python tools/start_management.py --scheduler-demo`。它只产生一轮8名虚构学生的流量，使用实际应用、SQLite、调度和管理HTTP，但模型传输为替身，不连接Mac。新演示库位于sandbox/results/scheduling-demo/<run>/，不覆盖旧夹具。面板来源simulation:scheduling-demo；运行结束后队列显示本次进程真实的零运行/零等待。默认入口不会自动制造流量。

不开放配置修改、删除记忆、重跑模型、数据库恢复或备份操作。既有本地维护接口保留，后续按管理员权限接线。本地访问者被视为本机管理员：不是生产鉴权，也不是互联网部署方式。多进程全局限流、正式Operator授权、集中日志、正式保留政策、队列/硬件压测仍待集成。

## 接口与将来的并入

Python `dsh_tutor/management.py`负责采集和查询；`management_http.py`负责本地只读HTTP。Next的`app/api/manage/[...path]`固定代理上游地址，浏览器不能指定URL、数据库路径或身份。

| GET接口 | 含义 |
|---|---|
| `/v1/management/overview` | 来源/时间/状态/存储字节/错误聚合 |
| `/v1/management/events` | before、limit、state、request_ref、since、until筛选 |
| `/v1/management/events/{sequence}` | 当前保留的单条脱敏诊断 |
| `/v1/management/config` | 白名单配置摘要 |

未知字段、重复查询参数、非法时间/游标/页长拒绝；POST等写操作拒绝。未来前端并入Server仓库`apps/admin`，复用其UI与Operator会话；管理代理放到NestJS授权边界，Python只保留管理能力。平台TS契约与Python结构仍须正式对齐，目前没有修改AIVirTeach仓库或声称已经共享admin-contract。

## 验证

新增11项后端检查：脱敏、权限、只读、分页筛选、有限缓存、数据库失效、队列注入。完整回归结果见development/latest-tests.json，必须匹配当前业务/前端源码指纹。浏览器交互见development/management-acceptance-20261031.json，使用真实Ubuntu HTTP和模拟数据，断联提示由浏览器拦截模拟；没有实际模型或容量压测。


## 第二阶段更新（2026-10-31）

已将总准入、资料准备/等准备、DSH/等DSH分开显示，新增准备上限/耗时预算/执行前重核预算和准备时延。队列快照仍来自真实同进程实例或单实例文件桥，模型健康仍未知。问答诊断最多16段，包含等待准备、读取、等待DSH、排队后重核、模型与保存；超时请求也能查看失败原因和等待时间。

当前308项回归、11项第二阶段组合断言、12项第一阶段组合断言及最终页面核对通过。界面演示为虚构学生与模拟模型；另有dsh-runner真实Qwen3例核心行为通过，二者分开记录。详情docs/preparation-stage2.md；证据development/preparation-stage2-20261031.json。
