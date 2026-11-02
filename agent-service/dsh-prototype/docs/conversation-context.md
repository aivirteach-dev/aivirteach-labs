# 连续对话与动态环境

本轮只实现每次请求的有限前文输入、范围检查和当前证据处理；不建立聊天数据库、不接真实学生认证或VM。学生及课程身份必须由未来Server适配器核实，不能直接采用浏览器/模型声称的身份。

## 职责和接口

`dsh_tutor.conversation`负责契约、前文筛选和预算；`course.answer_course`组织输入；`tools`在执行前授权；`results`只展示本轮校验过的工具事实。正式模块不导入sandbox，无全局历史列表或会话缓存。

```python
result = answer_course(
    request,
    config_path=model_config,
    state_dir=isolated_run_root,
    tool_bridge=trusted_bridge,
    conversation={"scope": trusted_scope, "turns": completed_turns},
    conversation_limits={"max_turns": 4, "max_history_chars": 2400},
)
```

`conversation`和`conversation_limits`是可信应用参数，不放入学生question。现有无前文调用继续可用；不能据此把旧接口称为完整DiagnoseRequest适配。

scope包括student_id、learning_context_id、course_id、course_version、lesson_id、instance_id、instance_generation、environment_os（linux/windows/unknown）。没有实验实例时instance_id和instance_generation必须同时为null；有实例必须同时提供。系统类型未知就使用unknown；不能根据教材或模型猜测。代次来自可信应用，不是此模块自行探测；同名实例重建时必须更换代次。

每个已结束问答对格式为`{"scope": old_scope, "user": "问题", "assistant": "回答"}`。只接收完整问答对，按时间从旧到新排列；不接system/tool角色或可执行命令配置。候选最多16轮，每条消息最多2000字符；超出硬上限拒绝，不先接收无限内容再处理。

对照固定AIVirTeach源码：Labs已有history的role/content结构，最多8条；本实现最多4问答对，在送往模型时也转为8条role/content。Server/Client已有enrollment与generation范围。未来映射learning_context_id到已授权enrollment，generation到可信当前代次，并核对lab/instance归属；平台的数字代次须显式转为字符串。平面消息应由适配器校验配对后传入，不能自动编造缺失的回答。当前没有实现HTTP转换或修改平台源码。

## 先拒绝，再筛选，再裁剪

1. 当前scope的课程/版本/课时必须与CourseQuestion一致。
2. 任何候选前文属于其他学生或learning_context时，整次拒绝；即使它本来会因超长而被删掉，也不能绕过归属检查。错误仅返回机器可读原因，不包含聊天原文或身份。
3. 同一学生同一学习上下文中，课程、版本、课时、实例、代次或系统类型不同的旧轮次全部省略，不送给模型。不同学生/不同学习上下文不能依靠这条省略规则静默混用。
4. 默认保留最近最多4轮、序列化后2400字符的历史，始终整轮删除；可信调用方可调低，硬上限4轮/4000字符。没有历史时计数为0。
5. 当前课程/问题优先，全部JSON数据最多8000字符；必要时继续删旧轮。即使删空仍超限，启动模型前报错。完整提示最多10000字符。字符不是token，当前8K模型仍需要真实协议与样本验证；不能保证换模型后同等质量。
6. 输入发生省略时带history_incomplete标记；指代无法确定时要求补充，不猜测被省略内容。当前无自动摘要、长期记忆或检查点恢复。

## 历史不能证明现在

同一VM中“之前停止”与“学生说已启动”都不能直接变成当前状态。需要状态事实时调用本轮工具；旧证据ID不可选作本轮证据。有工具但本轮没拿到有效结果时，明确无法判断，不能回退引用上轮成功结果。

开启conversation并同时启用工具时，工具可信策略必须另外绑定相同student_id、learning_context_id、lesson_id、instance_id和instance_generation。后端返回的状态由工具入口包装证据编号、当前运行及代次，模型不能覆盖。results增加代次匹配检查，保留原有请求/运行/时间/来源/轨迹校验。

这些是输入绑定和本轮证据规则，不是检测VM重建的系统服务。真实Gateway还必须验证当前实例归属和代次。Windows/Linux目前只是范围元数据与模拟状态，不代表真实系统检查已适配。没有工具的自由教材回答仍需样例/人工复核，提示规则不能证明任意模型都不会幻觉。

## 结果和定位

CourseAnswer新增可空context_usage：received_turns、used_turns、discarded_scope、discarded_budget、history_chars、issues和current_state_policy。只记录计数及原因，不回显身份或历史原文。省略原因有scope_changed_history_omitted和history_budget_trimmed；不把合理裁剪自动当作工具事实错误。

ConversationError.code可为request_scope_mismatch、instance_generation_required、tool_scope_mismatch、history_owner_mismatch、current_input_too_large。格式/长度错误沿用Pydantic ValidationError。应用应记录阶段和原因码，不把ValidationError原始输入完整写入生产日志或学生界面。

本轮仍不提供身份认证、聊天持久化、并发队列、去重、取消上游推理或完整usage；这些职责按原集成方案另行完成。运行中临时请求文件由runtime清理；开发验收只使用虚构对话，报告保存的是测试证据。

## 验证

自动检查覆盖归属拒绝、各维度变更省略、整轮预算、当前问题保留、无共享前文、同VM重新观察、代次错绑拒绝、旧ID不能当新证据、当前检查失败不能回用旧事实、历史不能授权Shell。自动测试包含模拟模型返回，不等于实际模型理解已通过。

模型验收脚本`sandbox/conversation_acceptance.py`以dsh-runner运行，仅调用Mac Ollama和模拟工具。4例：文本追问、首次停止、同VM启动后再查、换成Windows模拟实例后旧前文省略。失败停止，不自动重试。原始报告在sandbox/results/runner-check/conversation-report.json，复核在development/conversation-acceptance-20261030.json。测试运行期间冻结代码，并核对前后指纹；不把该小样本称作多人或真实环境验收。

## 2026-10-30 本轮复核结果

70项自动检查通过。四例串行实际模型核心行为复核通过，源码与自动测试指纹均一致：端口追问4.77秒，无工具；首次停止12.53秒、同VM重查运行11.02秒、新VM运行10.21秒，各1次模拟工具执行。换VM旧轮省略，当前证据绑定新实例和g2；报告内证据与轨迹一致，复核时无dsh-runner运行进程。runner运行目录为私有，开发账号未独立读取原始审计或核实本次临时文件清理。

端口例多输出了说明句，未严格只答数字；当前课程也包含端口，该例不能单独证明答案依赖历史。动态状态例验证本轮重查和旧状态不回用。此结果不外推到任意语义、真实Windows/Linux或多人。长期聊天保存未开发；原始报告不改写，完整复核见development/conversation-acceptance-20261030.json。
