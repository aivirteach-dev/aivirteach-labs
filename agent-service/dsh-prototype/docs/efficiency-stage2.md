# 效率优化第二阶段：教材预处理与低成本请求

日期：2026-11-01。主要开发位置仍为Ubuntu /home/xf/projects/dsh；SDK/runtime保持0.2.1rc2，无新Python/npm依赖、数据库迁移或AIVirTeach仓库修改。本轮真实模型0；本地实际DSH只访问固定假HTTP提供方。

## 请求如何分工

```text
可信宿主绑定学生／课程版本／任务／实例
                   │
                   ▼
         原有身份、去重、总准入与准备名额
                   │
       ┌───────────┴─────────────┐
       ▼                         ▼
显式course_actions_v1       原course_snapshot/aivirteach
       │                         │
       ├─教材完整块              ├─none：不读历史与学习记忆
       ├─当前任务目标            ├─recent：当前任务少量前文/记忆
       └─获准服务状态            └─earlier：显式早期页
       │                         │
程序直接处理，共用ToolGateway     公平排队 → DSH → 统一模型预算
       │                         │
       └───────────┬─────────────┘
                   ▼
      共用范围/修订复核、证据校验、聊天保存
                   │
                   ▼
             交付记录保存后返回
```

固定动作不从自然语言猜测。学生问“为什么报错”仍交给DSH；没有额外分类模型，也不会把句子里的Shell命令直接执行。固定动作不占DSH许可，仍占总请求、单学生和准备名额；准备/工具调用阻塞时继续占用，不丢弃线程伪装取消。模型路径执行与保存名额拆分仍留待第三阶段。

## 教材发布与缓存

- 原HTML不变。新导入采用lesson-templates-v3；只从明确“学习目标 / Learning goals”等小节保留目标原文，不从标题猜目标。旧版本继续可读，版本号不偷偷更换。
- 整批语言文件在同盘暂存目录验证后原子rename，避免读到一半语言；已存在内容版本必须逐字一致。
- tools/publish_materials.py对明确课程版本生成material-preparation-v1课时块、完整代码、来源、轻量字词索引。最多4096索引词；没有向量模型。所有课时编制完后才发布整个目录，已有不同内容不覆盖。宿主应在导入和预处理均成功后才启用该版本；本工具不自动切换平台配置。
- 本轮已为既有真实双语版本import-1036eafd8d8402ec生成30个课时文件，未重写原教材或伪造平台正式版本。旧提取器没有声明目标的课时仍返回空目标。
- MaterialRepository.load保留全教材校验接口；prepare/block改为按课时读取预处理产物。未预处理的历史版本有明确兼容读取，首次按课时编制，之后缓存；新发布建议提前编制。
- 缓存键包括课程版本、内容指纹、提取器/预处理器版本、语言、课时和文件元数据。初读核对SHA256，重用时检查文件元数据；以公共教材目录由可信发布者维护且内容不可变为前提，不是对拥有系统权限攻击者的内容认证。
- 默认最多64条、4MiB序列化内容，LRU淘汰；可按config/material-cache.example.json注入其他上限，0可关闭。Python对象/临时解析和文件系统缓存另有开销，4MiB不是进程总内存保证。每个repository实例一份缓存，应由宿主按资料库复用实例，避免每次请求新建或多份实例总量失控。
- 缓存只有公共不可变课时；不缓存问题、回答、任务目标或私人记忆。返回新对象避免调用方污染共享内容；未知语言、版本、课时不自动回退。
- 代码块在模型摘录中完整容纳或整体省略；omitted_code_blocks明确记录省略数量，规则要求请求具体块，不能补写缺失命令。显式原文动作最多6000 UTF8字节，超限明确失败，不截断。长文本仍有原文字符范围，不把片段说成完整课时。
- 公共发布目录755、材料文件644；暂存目录在发布前为700。仅处理公共教材，不改development权限、dsh-runner身份或系统隔离。

```bash
.venv/bin/python tools/import_materials.py --course-id YOUR_COURSE --source INPUT_EN --source INPUT_ZH
.venv/bin/python tools/publish_materials.py --course YOUR_COURSE --version EXPLICIT_IMPORT_VERSION
```

## 显式动作协议

保持外层dsh-tutor.v1和原有提交/查询/取消接口；新增显式profile course_actions_v1，并在capabilities中声明。旧profile的payload仍严格拒绝action或任意附加字段，现有客户端不需要改动。新客户端先检查capabilities再使用。

```json
{
  "api_version":"dsh-tutor.v1",
  "profile":"course_actions_v1",
  "history_selection":{"mode":"none"},
  "payload":{
    "request_id":"unique-action-id",
    "course":"由可信宿主绑定的完整CourseSnapshot对象，此处仅示意",
    "action":{"kind":"material_block","block_id":"b002"}
  }
}
```

白名单仅三种：material_block(block_id)、learning_goal(无参数)、service_status(service)。参数互斥，不能附加用户、路径、任意Python/Shell；未知操作系统不自动运行命令。

TrustedBinding由可信宿主注入material_repository或异步service_executor；HTTP、学生、模型均不能创建这些对象。直接服务检查使用原ToolPolicy/ToolGateway，保留尝试计数、授权范围、来源、超时、审计和results事实校验，不复制一套权限规则。没有真实后端时返回未接入；本轮executor为模拟。

结果detail新增execution_path/direct_action_kind/memory_revision。直接路径用deterministic:course-actions-v1标识，绝不假称用了Qwen；usage仍未知，动作次数另列。动作规范正文进入原ChatStore去重，外层原请求摘要还覆盖动作/profile/历史选择；同编号改动作冲突，重发不重做工具或模型。

learning_goal始终来自当前任务有效目标且标注学生声明，不是成绩。交付记录保留目标读取时的修订，后续更正/删除后旧目标操作会失效；新编号重新读取当前目标。服务检查重放永远是原检查结果，不是重新观察。

完整聊天与交付均确认保存后才返回。中断对账时，若固定动作的完整交付回执丢失，只凭聊天原文不足以重建修订/证据，就保守结束为source_changed，不重做检查或模型；原聊天保留供授权历史查看，须用新编号发起新动作。不能把这一限制说成自动无损恢复。

## 记忆读取模式

- recent：保持原默认，读取当前任务少量有效记忆和近期有限前文。
- earlier：显式早期页，保留原有范围、删除下界和页预算。
- none：主动选择教材独立问答；不读历史、不读学习条目、不提取新记忆。仍读取当前范围和修订状态以保证撤销/更正一致性，仍保存本次问答。模型明确收到course_only标记，依赖前文时要求补充；不保证任意模型一定遵守。

none不是“无权限模式”，不能跳过认证/任务/VM代次与保存。正常历史读取失败仍拒绝，不自动切none。公共课程块先于私有前文和问题进入提示，给未来后端复用稳定前缀留机会；当前没有宣称缓存GPU计算或加速比例。

## 验证与管理

sandbox/low_cost_acceptance.py以真实双语教材做两个原文动作，另测目标和模拟服务动作及一个开放问题。只有开放问题进入实际DSH并访问本机假提供方，四个动作模型调用0；模拟HTTP和SQLite保存是真实执行。tools/start_management.py --low-cost-demo运行有限一轮，刷新只读状态，不继续推理。

管理页新增公共缓存条目/序列化字节/命中/淘汰、直接动作保存与工具执行数、固定动作阶段标签；缺接线保持未知。日志不扩散学生正文。

关键回归包含发布失败/越界符号链接/篡改、缓存边界、两课两语言和Linux/Windows/unknown、原文保真、目标跨学生跨任务隔离/更正删除、none不读写私人条目但保留修订复核、未授权工具零执行、保存失败不交付、恢复缺回执保守停止，以及已有DSH预算回归。准确数量/指纹和组合报告见development/efficiency-stage2-20261101.json。

本轮不调用Mac或真实LLM，不证明真实解释质量、Windows/Linux Gateway、学生等待改善比例、GPU/内存容量或30～50人吞吐；服务器验证继续暂缓。
