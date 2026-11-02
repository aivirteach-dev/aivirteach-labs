# 真实双语教材接入

## 来源与身份

用户提供AI Daily Briefing英文与简体中文HTML。原名及原字节保存在inputs/courses/ai-daily-briefing/originals；原微信文件未改动。SHA256与源文件一致，见development/materials-import-20261029.json。

两个语言文件各有15个内容单元：课程概览加14个带编号课时；以原HTML的lesson-*模板ID配对，顺序相同，35个代码块逐一内容相同。没有把页面初始“0 / 9”当成课时数量或学生进度，也没有据此判定学生完成。正文翻译没有做逐句专业翻译验收。

本地course_id为ai-daily-briefing；平台正式ID及发布版本需接入时核对。文件名v5可能是文档设计修订，不能直接当成平台课程版本。导入版本为import-1036eafd8d8402ec，由两个原件指纹与提取器版本共同生成；official_course_version为空。

## 存储与接口

- inputs/courses/ai-daily-briefing/originals：真实原件，开发账号维护；runner不读取此目录。
- materials/courses/ai-daily-briefing/import-1036eafd8d8402ec：en.json、zh-CN.json及manifest.json。全量提取文本、代码和来源指纹，runner只读。
- config/materials.json：本次明确选择的导入版本，不按最新目录或标题猜测。
- dsh_tutor/materials.py：提取、导入、精确读取与有限摘录接口；不依赖sandbox、不运行HTML脚本或任何教材命令。
- sandbox/materials_acceptance.py：三个模型问答测试；结果只是开发验收报告，不是学生聊天历史系统。

```python
repo = MaterialRepository('/home/xf/projects/dsh/materials/courses')
snapshot = repo.prepare(
    'ai-daily-briefing', 'import-1036eafd8d8402ec', 'zh-CN',
    'lesson-1-2-install-and-start-n8n', '浏览器访问地址是什么？'
)
# 用snapshot构造已有CourseQuestion，再调用answer_course。
```

课程、版本、语言和课时显式传入；缺失时拒绝，不跨语言或版本兜底。文件内容指纹不匹配、课时ID不配对或路径越界均拒绝。教材本身不能授予工具权限，模型也没有任意教材文件读取工具。

CourseSnapshot新增locale（默认zh-CN）和可选material，goals允许空列表。HTML未独立列出规范化学习目标/过关标准时不编造，后续Server提供的权威目标不能被此导入覆盖。

material记录原文件名、SHA256、课时锚点，以及每条摘录对应块ID/字符范围。版本号留在CourseSnapshot.version。素材来源不等于用户授权。

## 控制上下文长度

完整代码保存在提取资料中；单次默认最多1400字符、12个摘录片段。长代码按行优先切片，记录原字符范围；先保留引导段落，再在当前课时内按关键词选取，同时关联相邻代码与说明，最后恢复原文顺序。没有引入向量数据库或额外检索模型。

这属于轻量摘录，不能保证每个问题都选中最佳片段。excerpt_only明确标注是否只给了部分内容；缺失时回答应说“当前资料未提供”，不能据此宣称整门课程没有。图片/截图/流程图保留在原HTML，但未进行视觉识别，images_interpreted=false。

普通课程问答遵循locale；受限工具事实模板也已支持中英文。英文工具文案有自动检查，本轮教材模型验收不调用工具。

## 验证与运行

52项自动检查通过，包含原有工具检查，以及提取完整性、课时配对、代码保真、精确版本/语言/课时、指纹篡改拒绝、路径边界、长代码与配置值邻近说明等。第一次实际摘录测试发现时区值遗漏，修正后回归通过。

```bash
cd /home/xf/projects/dsh
.venv/bin/python tools/dashboard.py test
sudo -u dsh-runner /home/xf/projects/dsh/.venv/bin/python /home/xf/projects/dsh/sandbox/materials_acceptance.py
```

问答验收：中文n8n访问地址、英文课程时区、未提供的实际VM内存。原始报告sandbox/results/runner-check/materials-report.json，关键词检查与人工复核分别记录；本轮三例核心事实复核通过，归档development/materials-acceptance-20261029.json；均0次工具调用，前后代码和教材指纹一致。格式遵循和摘录范围措辞仍需优化。

重新导入可以调用tools/import_materials.py，使用--course-id、重复--source参数和明确--root。相同版本不同内容拒绝覆盖；来源或提取器修订应生成新版本。正式发布版本映射不能由导入脚本猜测。

## 本轮未做

没有执行安装、调用Tavily/Gemini/Gmail、发送邮件或连接学生实验机；没有在线核验教材所述API当前可用性。没有单独收到n8n工作流JSON，当前知识来自HTML步骤和代码。未新增数据库、聊天历史或连续对话。

## 首轮失败与修正（2026-10-30）

首轮中文地址题超时，英文时区题没有可见回答，资料不足题正确，原始报告保存在development/materials-first-run-20261030.json。没有把失败标为完成。

导入器曾过滤所有button，误删inline-copy-link承载的正文地址。已修正为保留这类正文文字，普通复制控件仍忽略；提取器修订为lesson-templates-v2，新导入版本已选择，错误提取版本移到work保留失败证据，不作为正式资料读取。原始HTML未改动。

本地假HTTP检查另复现：当前pi-ai固定4096 token安全预留使4096窗口的max_tokens降到1。已创建复用Qwen权重的独立Ollama别名dsh-qwen35-9b:8k，服务端num_ctx=8192、num_predict=512；项目容量声明与之匹配。客户端显式发送reasoning_effort=none、max_tokens=512，并使用system角色。请求参数已由已安装DSH实际发出的协议验证。小于8192配置提前拒绝，不能只改客户端容量冒充服务端能力。

没有修改Ollama全局配置或原模型参数；沿用服务默认空闲卸载策略。此处不把超时的唯一原因归于某一项，真实效果由复测确认。模型层仍仅当前Ollama适配，换提供方需重新验证参数。

依据：
- pi-ai安全预留：https://raw.githubusercontent.com/earendil-works/pi/main/packages/ai/src/api/simple-options.ts
- DSH推理等级及协议映射：https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/providers.md
- Ollama兼容接口：https://docs.ollama.com/api/openai-compatibility
- Ollama配置别名：https://docs.ollama.com/api/create

## 本轮问答实测

- zh-port（zh-CN）：根据教材，应在浏览器中打开 http://localhost:5678 访问 n8n。当前资料未提供其他地址信息。；12.81秒，0次工具调用。
- en-timezone（en）：This course currently uses Asia/Kuala_Lumpur for n8n and the Code node.；6.24秒，0次工具调用。
- zh-missing（zh-CN）：教材中未提及当前学生虚拟机的实际内存容量，资料里没有提供此项信息。；7.27秒，0次工具调用。

以上为有限文本样例；未访问学生环境或识别图片，不外推为全部课程问答正确。
