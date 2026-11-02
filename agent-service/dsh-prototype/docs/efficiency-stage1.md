# 效率优化第一阶段：计时、进度和总预算

2026-11-01。本阶段属于新效率路线，不重开此前已完成的接入三阶段。证据以development/efficiency-stage1-20261101.json及最新指纹为准；未调用Mac/真实LLM，没有服务器人数容量和教学质量验收。

## 运行链路

```text
提交/查询/取消 → 各类HTTP限流与有界鉴权 → 原有身份/范围/去重
                                     ↓
                           原有准备/公平排队
                                     ↓
     Python工作进程 → 当前DSH/sdk-minimal → 统一llm/stream预算
                                     ↓
                      模型请求／现有受控MCP工具
                                     ↓
                  原有证据校验/版本重核/聊天与交付保存

白名单阶段、有限运行指标 → 原有诊断/管理面板；未知用量和硬件保持未知
```

未拆执行和保存名额（第三阶段）、未增加暖会话池/真实远端取消（第四阶段）。现有总5/准备2/DSH1保持，不自动增加并发。

## 预算与接口

- config.json.execution_limits仅由可信宿主维护。默认最多4次模型尝试；每次仍512输出，整个请求最多2048输出Token上限预留。失败尝试不退款，不因HTTP重发重跑模型。
- 输入按ceil(模型可见messages与tools的UTF8字节/2)启发式估算：单次6000、总24000，另有512上下文余量。该估算不是tokenizer，不保证真实Token上界，更不是费用统计；真实输入/缓存/输出usage继续unknown/null。未来替换精确计数要做模型适配回归。
- 64KiB模型请求块、32KiB累计文本/推理/工具参数输出、每个流8192个chunk、有限进度文件64KiB/256行、进程stdout2MiB/stderr128KiB。到限停止本轮；上游未确认结束则保留未知并暂停原有准入。
- dsh_tutor/plugins/model_budget.mjs挂在公开llm/stream，不只pre-step；根请求、工具后续和辅助调用共用请求独占账本。只允许本轮provider/model和MCP工具schema；不修改冻结请求，不调用另一套Agent循环。
- render_patch显式关闭llm-retry、compaction-basic、image-offload及原先关闭的文件/Shell工具。没有自动摘要和隐藏重试；预算插件必须在harness.start后确认就绪才发送第一问。
- 工具尝试预算和证据授权继续在既有ToolGateway执行；DSH插件不能代替身份授权或真实Gateway。
- 模型预算发送前拒绝且此前调用已结束，返回model_budget_exhausted；生成流中断等仍按run_failed/状态未知处理，不以超时冒充远端停止。完整请求期限是软期限。

## 实际profile与本地验收

```bash
cd /home/xf/projects/dsh
.venv/bin/python tools/check_execution_profile.py
.venv/bin/python tools/check_service_contract.py
.venv/bin/python tools/dashboard.py test
.venv/bin/python sandbox/efficiency_acceptance.py
.venv/bin/python tools/start_management.py --efficiency-demo
```

check_execution_profile用当前安装运行时--dump-config检查真实组合、禁用能力及预算插件，不读默认用户DSH home，不请求模型。正式运行仍要求dsh-runner。

--efficiency-demo仅在sandbox临时配置中允许xf访问本机假HTTP服务，不修改正式运行身份。有限4操作：首次保存并重发去重、一次工具后续被预算停止、另一学生成功、等待取消；最多3个假提供方HTTP请求，实际DSH/SQLite/HTTP参与。刷新面板不再发推理请求。停止后只清理本次启动的服务。

## 计时、进度和管理展示

- worker_start、dsh_startup、mcp_discovery、model_request、tool_execution、runtime_finish为白名单；父进程观察进度间隔约50ms，因此不是逐字流或精确远端时间。
- dsh_startup包含MCP实际启动/发现；mcp_discovery单列的是初始化后确认工具发现成功的核对耗时，不能宣传为纯MCP启动耗时。
- model_stream是模型流累计墙钟时间，包含网络；first_visible从预算插件就绪起算到首次可见文本；runtime_total含子进程启动、DSH及清理。worker_boot为父进程观察到首次工作事件的时间。
- 结果校验、聊天保存、交付日志保存与整个操作另行计时；其中存在包含关系，不能相加冒充GPU时间。未有可见文本时first_visible不填0。
- 每请求诊断最多64段；队列运行样本最多64，交付/操作测量各最多64。只存计数、有限枚举、时间和哈希引用，不存模型正文或原始异常。
- CourseAnswer新增可空runtime_metrics；历史重放仍不伪造新指标和usage。配置schema同步导出；模型验收指纹覆盖正式MJS插件。

## HTTP与客户端节奏

ServiceSettings.http_budget默认每秒提交10、查询40、取消10，各类窗口独立，另有限认证前尝试数及8个HTTP连接。查询最多4个在处理连接/鉴权，提交和取消各保留类别限额。

宿主鉴权等待默认0.5秒，超时后后台解析器仍持有自身类别的有限名额直到实际返回，不无限创建线程或假装已经取消。宿主外部网络/数据库仍须配置自己的真正期限。

dsh_tutor.polling.ProgressPoller是可复用节奏策略：前台进度不变时退避、变化时重置、随机抖动、终态/后台暂停、遵守Retry-After；不自动提交或取消模型任务。服务capabilities公开轮询建议。正式学生前端/SSE尚未接入，不声称已修改AIVirTeach学生页面。

现有管理前端只读刷新改为有限失败退避和抖动，后台暂停；显示实际接入来源、细分指标和未知硬件。50次虚构查询不等于50个真实学生并发模型测试。

## 版本与限制

SDK与runtime仍0.2.1rc2，无新增Python/npm依赖，无DSH核心修改、无新数据库schema迁移、无正式平台连接。原有保存、取消、权限、任务记忆与恢复规则继续回归。

本机假提供方验证协议和资源规则，不证明Qwen或其他模型能在相同预算下完成所有教材问答；跨模型质量、真实Token、远端取消、硬件压力及30～50人容量留待后续阶段。原来的真实模型小样本属于历史证据，不算新版本质量复核。
