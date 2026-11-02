# 工具证据与回答结果校验

本阶段只针对get_service_status的有限观察，不承诺任意课程、任意模型的自由回答都正确。

## 数据流与模块

course.answer_course → results.trusted_policy（启动前绑定请求/课程/版本）→ runtime.run_prompt（父进程生成run_id与开始时间）→ DSH/MCP/策略/模拟工具 → results.compose_tool_answer → CourseAnswer。

results.py校验本轮证据和成功轨迹逐一对应，核对请求、运行、实例、课程/版本、服务、来源及时间；重复编号、相互矛盾的状态、缺失轨迹、错误归属或来源均拒绝。当前要求证据生成时间不早于本轮开始，并核对可信策略中的instance_generation；连续对话的范围筛选由conversation.py处理。模块不自行探测VM重建，不持久管理代次。

工具策略新增allowed_sources，默认只有simulation。将来启用真实后端必须由可信调用方显式允许gateway，并单独实现和验收真实访问；增加这个枚举不代表Gateway已经接入。

## 学生可见内容

服务状态和模拟来源由程序按证据直接展示，即使模型漏写或给出相反文字，也不采用未经校验的改写。工具note不直接拼接到学生回答。

有工具时模型最终只输出JSON：evidence_ids、course_instruction_indexes、next_step。必须引用本轮全部有效证据；课程下标从0开始，引用后由程序展示课程原文，模型不能改写成新的课程要求。next_step只允许none、review_course、need_more_evidence；review_course必须有有效课程引用。

这会限制当前工具问答的自由解释能力：没有开放自由原因分析、任意建议或修复命令。可继续升级这个边界，但不能把未校验的模型草稿直接拼回answer以换取流畅。课程原文选择的相关性仍需样例复核；结构合法不等于相关性已证明。

无工具的普通课程问答保留原流程，quality为not_requested/not_checked，不因此获得工具事实校验保证。

## 结果与失败

保留answer、evidence、tool_trace等字段，新增run_id和quality。quality.facts为not_requested/verified/unavailable/rejected；quality.explanation为not_checked/validated_selection/omitted；issues为机器可读原因码。

- running、stopped分别展示正在运行、已停止；unknown展示暂时无法确定，status=partial。
- 没有成功检查，不能采用模型声称的状态；明确无法判断。
- 拒绝、参数错误、预算耗尽、超时和执行失败分别展示有限说明。
- 模型说明为空或格式错误时，保留有效事实，说明补充说明未通过校验，status=partial；不自动再次请求模型。
- 错绑、过期、冲突或畸形证据整组拒绝，不向调用方返回未通过校验的证据及轨迹；原始工具审计仍留在本次运行目录。
- 一次成功后另一次检查失败，成功事实和失败分别说明，不将失败说成服务停止。
- DSH本身连接失败、总超时等仍可能抛运行错误，不承诺所有传输中断都能返回已有事实；远端推理取消另行验收。

completed仅代表本次处理没有已识别的不完整项，不表示学生过关或任意语义已证明。

## 验证

35项自动检查通过（含原有检查）。有意义的新增场景包括漏答、相反自由草稿、空/畸形说明、伪造引用、三种状态、两类课程、错误归属、过期/未来时间、重复/冲突证据、失败不能转成服务状态、成功与失败共存，以及真实course入口组合与无工具兼容。

两课程真实模型验收命令：

```bash
sudo -u dsh-runner /home/xf/projects/dsh/.venv/bin/python /home/xf/projects/dsh/sandbox/mcp_acceptance.py
```

原始报告为sandbox/results/runner-check/quality-report.json。分别核对tool_chain_passed、facts_passed、explanation_selection_passed与源码指纹；报告仅反映实际cases，不代表真实Windows/Linux或多人环境验收。本轮两课程模型验收已通过，详见下一节。

## 本轮模型验收结果（2026-10-29）

同一冻结版本：n8n-demo明确展示已停止，49.31秒；codex-demo明确展示正在运行，37.83秒。各1次调用，均明确模拟来源，模型受限说明通过，Codex复核课程原文引用符合本例。前后源码指纹一致，35项自动检查指纹也一致。核查时未发现dsh-runner运行进程。

完整原始报告和复核保存于development/answer-quality-acceptance-20261029.json。该通过只覆盖当前两份虚构课程和服务状态工具，不能外推为真实Windows/Linux、多人或任意模型的质量保证。
