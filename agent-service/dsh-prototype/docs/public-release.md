# 公开测试模块来源与验证

2026-11-02，第一阶段：独立原型代码入库，不部署、不替换现有服务。原候选 ZIP SHA256：`ccff6a85fa32a0ebece73c0fa16c9f64588d506ce35e4fe238cd34e17d396ebc`。原业务/配置指纹 `5951873d8de05314b0f0ce1eb6b527dca62504e392ce428ea95d41f8f1648dbb` 属于带私有教材的历史候选，不是本公开版本指纹。

## 本次变化

- 仅在 `agent-service/dsh-prototype/` 新增模块，DSH 业务和前端源码按候选保留，不修改原 Labs 服务/根配置。
- 不公开 36 个真实教材原件及提取文件；保留读取接口和非正文来源元数据。private_tests 保留 2 项原教材验收，公开基础回归为 465 项，不通过跳过冒充全部通过。
- README/工作手册按模块路径整理，明确阶段、接口与未完成边界；忽略私有教材目录，增加只读私有资料检查入口。
- 接口保持 dsh-tutor.v1，不改字段或另建模型循环。Schema 由现有模型生成并比对；模拟组合验证与真实平台验收区分。

## 可复现验证

先按 README 从干净目录安装固定依赖，然后：

```bash
.venv/bin/python -m unittest discover -s sandbox/tests -q
.venv/bin/python tools/check_service_contract.py
.venv/bin/python sandbox/integration_acceptance.py
.venv/bin/python sandbox/efficiency_acceptance.py
.venv/bin/python sandbox/completion_acceptance.py
.venv/bin/python sandbox/runtime_optimization_acceptance.py
.venv/bin/python tools/dashboard.py build
```

前端执行 `npm ci`、`npm run typecheck` 和 `npm run build`。以上不使用真实模型或正式数据。私有专项为独立命令，不在公开基础套件内；没有资料时应失败并明确原因。

原文档保留各历史阶段的设计和边界，其中指向未分发 development 报告的链接只作来源记录，不表示这些报告已在本模块重新生成。单进程所有权、真实取消、正式认证、平台同步/SSE转换、证据映射、完整计量、Docker 和容量验收仍待后续工作。

## 本轮核对结果

公开基础465项回归、依赖一致性、Schema逐项一致、前端全新安装/类型/生产构建、四组模拟组合（11/13/13/15条断言）、进度页构建通过。私有资料缺失分支明确拒绝；另行注入私有资料后2项原教材专项和11条教材组合断言通过。真实模型调用0；原Ubuntu业务源码指纹未变。原SDK文件流ResourceWarning仍可见，不声称已经修复。实际Git提交确定本源码版本。

本次另整理 task_memory.py、efficiency_fixture.py 文件尾空行及 dashboard.py 一处行尾空格；AST逐项相同，未改变运行逻辑。已复测公开回归。
