# 效率优化第三阶段：分段保存与有界维护

日期：2026-11-01。Ubuntu /home/xf/projects/dsh为唯一业务开发位置；SDK/runtime仍0.2.1rc2，无新增依赖、无AIVirTeach仓库修改、无真实模型调用。本轮验收在本机假提供方、模拟课程和一次性目录中完成。

## 1. 学生如何减少等待

```text
总准入（默认5；每学生1）
       │
       ▼
资料准备（默认并发2）
       │
       ▼
等待并预留收尾空间（默认最多2） ← 保存积压时在这里背压
       │
       ├─固定动作：程序处理，不占DSH
       │
       └─开放问题：学生轮转 → DSH许可（仍为1）
                                  │
                        排队后复核范围/记忆版本
                                  │
                        DSH/模型/工具确认结束
                                  │
                        释放DSH许可，下一位可开始
                                  │
                                  ▼
                结果校验 → 聊天保存 → 交付记录保存
                                  │
                                  ▼
                 保存确认后返回，释放收尾和总名额
```

ResourceManagedTutor复用唯一ChatSession。execute接收执行范围回调，只有实际执行和排队后的复核持有DSH许可；结果校验、聊天保存、诊断与交付保存继续持有预留及总/每学生名额。AgentService通过on_complete复用原双保存逻辑，没有另建模型循环或另存一条回答。

原application_seconds字段保留兼容，现在表示执行许可占用，不含校验和保存；完整耗时看operation_total。预留包括还在执行的请求，不等于“同时写盘数”。收尾预留FIFO，取得许可的请求仍使用既有学生轮转；默认每学生1避免单人积压。有效并行上限受DSH和收尾两种限额共同约束，升级配置时分别考虑。

max_finishing默认2，finishing_queue_timeout_seconds默认10；和现有模型/准备限额一样支持CAS热调整。降低上限不会中断在途；数量降到新上限以下才继续放行。SQL复核预算从取得执行许可后开始，排队不消耗它。

运行回执必须确认正常完成；父进程退出但模型流仍有unconfirmed_calls时返回run_failed。未知运行先暂停准入，不能仅因本地许可计数归零就认定远端空闲；只有可信宿主确认后才能恢复。这仍是单进程单提供方控制，不是跨进程全局配额或真实取消确认。

同一阶段的重复进度不再重复写事件；接受/取消/结果/恢复等关键状态仍FULL同步。阶段变化保留，未引入丢最终结果的批量异步写。OperationView.feedback新增真实已用时、阶段和等待配置，estimated_remaining_seconds保持null；没有编造倒计时。没有额外模型用于分类/估算耗时，长短队列暂不增加。

## 2. 操作副本、去重凭据与有效期

| 对象 | 本轮行为 | 不代表什么 |
|---|---|---|
| 聊天/学习记忆 | 原有任务/范围/撤销/保存规则继续 | 本轮没有批量删除学生历史 |
| 操作里的完整回答副本 | 默认24小时后可在预览确认下逻辑清空，旧操作返回result_expired | 不能当成新请求重新推理，也不保证数据库文件立即变小 |
| 旧协议去重键 | 没有可信有效期就保留最后凭据，仍可能达到日志容量上限 | 没有把旧版1000条限制伪装为已无限解决 |
| 新窗口协议去重键 | 可信窗口过期且终态、满足回收宽限后可连同事件/窗口映射回收 | 学生自报时间/任意ID不构成可信授权 |
| 诊断样本 | 保持原有独立有界窗口 | 不当作全平台历史或失败率 |

启用新保留协议需可信控制面显式OperationJournal.enable_retention，将操作库v1事务迁移到v2；有queued/running/recovery_required时拒绝。默认旧库不自动迁移，聊天数据库版本不受影响；旧软件拒绝v2，不能用旧程序绕过去重。当前运行/旧验收库未批量迁移，仅新虚构验收库显式启用。

RequestWindow只由可信认证宿主建立，含随机nonce、issued_at、expires_at，窗口最长24小时。ID为owner/nonce/两端时间的SHA256；时间改变必须改变ID。新客户端提交request_window_id，request_id必须以“64位窗口ID.”开头，后接自己的唯一后缀。身份、授权和窗口对象不能由学生正文创建；窗口ID是标识，不是签名或登录凭据。

64位小写十六进制加点被保留为窗口命名空间；即使没有窗口绑定，也不能把该旧ID转为legacy请求。相同窗口/ID改变正文仍冲突。窗口过期后提交在查缓存前返回410，不重推理；已受理在途可以完成，已知operation_id的查询/取消继续受原范围检查。终态整条回收后旧操作查询可返回404，旧请求提交仍因窗口或命名空间被拒绝。

为避免刚完成就丢失查询结果，默认expiry_grace_seconds=3600：窗口过期后及最后状态更新时间均至少经过一小时才允许整条回收；测试的一次性样例显式设0。宽限不延长提交有效期。完整副本独立过期策略仍可清正文；需由宿主选择覆盖真实处理和取回时间的窗口/宽限，不保证任意配置都有相同体验。

每次retention最多100操作、短事务和SQLite预算；预览包含动作及记录版本/内容摘要，apply必须匹配token。非终态和待对账保留；过期窗口元数据没有剩余操作时才回收，避免墓碑无限累积。清理副本/凭据不触及ChatStore，不调用模型、不VACUUM。

时钟回退到已记录维护时间以前会拒绝窗口请求，待管理员核实时钟。备份恢复后仍检查真实有效期；生产恢复前必须从独立可信控制面设置request_issued_floor并废止旧批次，再核实服务恢复。单靠恢复库里的旧时间水位不能防止系统时钟与所有状态一起回滚；本轮没有完成异机灾备、正式认证发行器或生产保留政策。备份需SQLite一致性方式及平台对账，不得把活动主库裸拷贝当完整备份。

## 3. 运行目录回收

新的DSH与直接工具目录在父进程登记owned-run-v1标记，含根目录设备/inode、UID、运行ID和终态，并持有flock租约。原始请求/进程输出仍按原规则及时清除。状态active/unknown、进程中断、未知远端状态和旧未登记目录不自动接管；租约释放本身不等于运行已确认结束。

RunInventory只枚举配置根目录的一小页，默认最多64入口、10个目录、每目录256条文件/目录、8层、全批32MiB和2秒预算；默认终态至少24小时。树中任何符号链接/硬链接/特殊文件、外部所有者或超过界限均不进入可清理集合。预览不删除，大小是可清理样本下界，不是全部运行空间。

apply重新取得租约，复核目录身份、树结构、文件元数据及有界读取的内容指纹，再通过O_NOFOLLOW和目录FD删除；不把枚举出的路径传给另一shell。所有权标记和租约最后移除。文件清理中断后可重新预览剩余内容再执行；最后标记移除窗口发生故障可能留下无标记的小残余，继续按未知保留并人工核对，不能自动冒领。标记是受控目录中的所有权协议，不是抵御同UID恶意进程的密码学认证。

本轮只删除测试显式创建的一次性清理样例；真实模型运行资料、未知目录、旧报告和原教材保持。没有启用后台自动清理任务。

## 4. 维护与容量

ServiceMaintenance是可信本机控制面：每次只做一个轻量job（retention/run_cleanup/snapshot）。有在线工作立即deferred，不创建无界后台队列；维护期间暂停新准入，结束后保持原暂停/就绪状态。独立文件扫描/字节/时间/事务预算，不把备份或VACUUM塞进每轮问答。现有LocalMaintenance备份流程继续独立使用，未增加在线自动备份调度。

容量手动盘点聊天主库/WAL/SHM、操作主库/WAL/SHM、已知可清理运行下界以及接入的备份/验证目录。总运行目录和未接备份保持未知；合计仅为已知下界，空闲空间低于原阈值时提示压力。没有根据小样本预测硬盘够用或自动扩容。管理页面只读上次手动盘点缓存，刷新不会重新遍历/哈希磁盘或执行删除。

离线命令tools/maintain_service.py通过显式项目内配置读取已存在日志；在线服务的所有权锁、遗留在途和待恢复状态会阻止维护。它不创建Agent、聊天库或模型进程，输出计划采用独占创建和600权限，apply要求预览；配置样例不是已启用部署。

```bash
# 先由可信管理员核对实际项目内路径并备份；示例路径不存在时明确失败。
.venv/bin/python tools/maintain_service.py --config config/service-maintenance.example.json --action migrate-retention
.venv/bin/python tools/maintain_service.py --config config/service-maintenance.example.json --action preview-retention --out work/retention-plan.json
.venv/bin/python tools/maintain_service.py --config config/service-maintenance.example.json --action apply-retention --plan work/retention-plan.json
.venv/bin/python tools/maintain_service.py --config config/service-maintenance.example.json --action preview-runs --out work/run-plan.json
.venv/bin/python tools/maintain_service.py --config config/service-maintenance.example.json --action apply-runs --plan work/run-plan.json
```

## 5. 本地验证与后续

426项自动检查（比395新增31），其中保留7个JS子检查。覆盖慢聊天/交付保存重叠、收尾积压背压、范围变化不交付、预算从排队后开始、未知模型流不正常放行、重复进度去重、忙时延后、CLI实际预览/删除及所有权锁、1005条虚构操作回收、窗口/宽限/命名空间/时钟/恢复边界，以及目录租约/内容变更/越界链接/中断等。

sandbox/completion_acceptance.py有13项组合断言：两个实际DSH到假HTTP的请求，第一位慢交付期间第二位完成；过期提交410；一条过期样例回收；一个一次性目录清理、未知目录保留。早期效率1的13项和效率2的11项组合继续复测。tools/start_management.py --completion-demo仅一轮有限流量，管理页面只读。

本轮真实模型0、模型并发仍1、总在途5、准备2、收尾2；没有正式数据库迁移或真实用户数据清理。没有启用WAL、暖会话池、跨进程配额、真实远端取消或自动硬件调参。服务器容量、教学质量、真实Windows/Linux Gateway和正式保留/恢复政策仍待后续。阶段4将处理提供方适配与生命周期，阶段5服务器测试继续暂缓。
