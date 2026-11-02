# 第二步：自动衔接连续问答

新入口dsh_tutor.chat_session.answer_chat复用course.answer_course，不重复实现DSH循环、模型协议或工具授权。存储仍在应用层，DSH不直接读数据库。原answer_course保留为无存储的底层入口。

## 调用

```python
store = SQLiteChatStore(private_database_path)
handle = store.current(student_id=verified_student_id,
                       learning_context_id=verified_learning_context_id)
reply = answer_chat(request, store=store, handle=handle,
                    config_path=model_config_path, state_dir=isolated_run_root,
                    tool_bridge=trusted_bridge,
                    history_read_limits={"max_scan_turns": 16},
                    conversation_limits={"max_turns": 4, "max_history_chars": 2400})
```

首次建库/绑定及范围切换仍由可信应用显式create/activate，比较修订号；不会根据学生文字猜测身份或自动切换。调用方负责真实认证、实例归属、授权及界面对账。config/storage.example.json新增session配置样例，应用显式传参，不读取学生提供的路径。

流程：验证输入和工具范围 → 原子接受问题 → 一次有限历史查询 → 既有上下文筛选与预算 → 再查当前范围 → 既有Agent → 结果校验 → 核对范围并保存 → 返回。

## 历史与资源

最多一次read_page，读取当前修订、当前问题序号之前的最多16条候选，尊重适配器page_capacity；SQL额外最多取一条判断是否还有旧页。不反复扫描历史，不跨学生/修订加载。只使用completed且answer_status=completed的完整问答。pending/failed/cancelled、partial和单条超过2000字符的历史整轮省略；原始存储不截断。

partial不表示所有事实错误，本阶段先保守省略。history_incomplete和ContextUsage.issues的upstream_history_omitted明确上游省略；不能假装提供了完整历史。继续沿用默认4轮/2400字符、JSON总量8000字符、完整提示10000字符等限制；字符不是token。允许复述学生前文提供的练习代号，不把它升级为课程要求或环境事实。旧观察不能替代本轮工具检查。

## 幂等、并发与故障

ChatStore新增accept，返回AcceptedTurn(turn,created)。只有created=true的原子新建调用获得一次模型任务的执行资格。第一步begin委托同一实现保持兼容；不能以begin返回pending推断自己有执行权。page_capacity公开适配器分页能力。SQLite schema仍为v1，无表结构迁移。

重复已完成请求返回原回答，明确标记replayed，不伪造本次工具事实、耗时或usage。pending返回in_progress；失败/取消不自动重试。进程在接受后退出仍可能留下pending，须由应用确认中断后恢复，不以“重新打开数据库”判定任务死亡；不承诺跨崩溃的模型推理恰好一次。

ChatReply将保存状态与CourseAnswer分开：

| state | 行为 |
| --- | --- |
| saved | 已保存并交付，含本次result、answer_status、run_id |
| replayed | 历史回答原文，result为空，界面必须展示重放提示 |
| in_progress | 不重复调用模型 |
| failed | 输入、读取、运行或格式失败，返回安全阶段/原因码 |
| not_saved | 模型返回但保存未确认，暂不交付答案，recovery_required=true |
| scope_changed | 范围变化或记录被清理，拒绝旧结果交付 |

stage为input/accept/history/prepare/model/result/save/replay，history含rows_seen、skipped_incomplete、skipped_oversized、older_available。历史读取失败时阻止模型调用，不降级成无前文会话；尽力记录application_failed。模型失败记录model_failed；失败状态也无法写入时标记需恢复。异常原文不回显。

最终保存失败时不能确认持久结果与当前范围，因此暂不交付。应恢复存储并查询原请求：若提交已成功但确认丢失，可重放；若仍pending，先确认任务状态。不能重试产生重复推理。数据库事务不跨模型运行；期间切换范围或删除记录会拒绝晚到保存。外部VM变化仍须真实Gateway校验，平台须对返回/显示进行对账。

## 验证与边界

21项新增测试，与原有合计121项，通过模拟推理传输验证跨进程恢复、唯一执行权、隔离、预算、失败、保存未确认和范围/删除竞态。自动测试不等于模型语义验收。

sandbox/chat_session_acceptance.py以dsh-runner串行测试：首次随机代号、全新子进程恢复追问、相同请求重放、换Windows模拟实例后重查；3个新模型请求＋1次重放，失败停止。resume不接收代号参数，当前课程和问题也没有答案，验证来源确实在数据库前文。冻结源码，核对前后指纹。

原始报告sandbox/results/runner-check/chat-session-report.json；复核development/chat-session-step2-20261030.json。虚构数据库在runner私有目录，不导出到HTML。无真实学生、平台HTTP/SSE、摘要、偏好提取、后台任务或多人生产容量验收。本机进程结束仍不证明远端模型取消；SDK流关闭ResourceWarning仍存在。

## 第二步首轮实际验收（2026-10-30）

第二步首轮实际验收未通过：首次保存成功，但模型插入代号空格，首例停止，恢复/重放/换实例尚未执行。已补原始字符复述规则，分离首次登记与后续严格恢复检查；121项回归再通过，修正后实际模型待复测。

首轮归档development/chat-session-step2-first-attempt-20261030.json。首次问题现在仅要求确认收到，后续恢复仍用原始字符串严格匹配；重放必须等于已保存回答。新增保持标识原始字符的通用规则，未对答案作空格归一化或替换。测试库为独立虚构fixture，第二步尚未完成。

## 第二步复测结果（2026-10-30）

长期记忆第二步核心流程复测通过：121项自动检查，3个实际模型请求及1次原文重放验证；新进程从DB恢复追问、换实例隔离正确，源码/测试指纹一致。首次确认句仍有代号格式错误，追问未严格仅输出代号；文案质量未全部解决。

首次14.67秒、恢复5.39秒、新实例模拟工具12.83秒；重放没有新的模型结果。恢复在新进程读取1轮前文，准确输出原代号；相同请求返回完全相同的已存回答，记录未增加。新实例无旧前文、证据为vm-new/g2。

首次确认仍插入代号空格，追问也未严格仅输出代号，不宣称复述质量已解决；不得以助手确认句覆盖学生原始输入。原始失败和复测报告均保留。runner私有DB未由开发账号另行打开，复核仅报告、脚本与指纹。第三步、真实平台和生产容量未开发验收。
