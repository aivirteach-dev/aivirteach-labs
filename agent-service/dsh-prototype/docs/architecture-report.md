# 成员汇报页：平台与独立 DSH 助教

更新：2026-10-30。权威项目：`/home/xf/projects/dsh`。

## 查看与转发

构建 `python tools/dashboard.py build` 后，打开 `dashboard/index.html`，选择顶部“项目怎么运行”。也可使用 `index.html#team-report` 直接进入。页内有四张 SVG 流程图、技术与模块清单、完成度、术语、源码和验收依据。每张图可放大；打印仅输出当前页签，可保存 PDF。无 CDN，断网可查看。

Windows 固定只读导出入口：`D:\MAIC\DSH\dashboard\index.html`。转发 HTML 即可完整查看图文；不依赖本机教材或数据库。页面含项目源码链接，打开私有仓库链接仍需要访问权限。HTML 不含账户密钥、学生聊天或模型服务网络凭据。

## 内容维护

- 文字、节点、箭头、技术表和来源的唯一编辑入口是 `development/status.json` 的 `member_report`；原进度章节仍使用原字段。
- 渲染器 `tools/architecture_report.py` 只渲染图文，`tools/dashboard.py` 接入现有进度页。没有创建第二套业务实现或联网前端依赖。
- 图中蓝色是平台源码；绿色是原型已实现；黄色是模拟后端/验收；紫色虚线是未来接入；红色是约束/错误出口。混合图中各节点状态分别标记。
- 更新代码和模型后须复核报告中的数量、参数与边界。报告顶部的“自动检查通过”同时检查最新业务源码指纹，不用旧版模型样例冒充新版语义验收。

## 版本与来源

平台基于 `development/github-review.json` 的固定三仓库提交；本轮通过 GitHub 连接器比较固定基线与 main。Server、Client 相同；Labs main 领先5提交，改变集中在 `.gitignore`、VM 镜像构建/创建脚本、静态检查和 README。比较原始记录保存在 `development/github-main-check-20261030.json`；未改动参考源码或线上服务，没有全库或部署审计。

原型业务测试仍为252项自动检查、指纹匹配。最新检查证据的6组合场景使用替身模型和模拟后端，实际模型请求0；复核为 `development/inspection-20261030.json`。195项版本的三例 Qwen 任务样例保留为历史模型证据。检查历史管理面板的视觉操作复核尚未完成，报告页面的图文检查不能替代它。

## 本轮验证

见 `development/architecture-report-check-20261030.json`：离线HTML结构、图示XML、章节锚点、测试指纹以及实际页面渲染/切页/放大/打印等检查按实际结果记录。本轮只改看板、状态和说明，不触发模型、真实工具、生产部署、数据迁移或AIVirTeach仓库写入。

## 后续交接边界

正式接入需对齐可信请求上下文、完整 DiagnoseResponse/SSE/usage/错误/取消、精确课程版本、实例ID与代次、Server和Agent的持久化所有权。正式PostgreSQL、真实Windows/Linux Gateway、多人全局限流、压测、保留期限与生产恢复另行开发验收。独立原型通过不等于接入后一键可用。


2026-10-31可读性更新：默认打开通俗架构说明，以平台分工、一次提问、学习记忆、管理员查看四张图展开。普通文字解释用途；术语、文件和验证来源收起。页面不声称正式平台或真实检查已接入；原开发进度仍可切换查看。验证development/plain-architecture-check-20261031.json。本次不改业务源码，263项既有测试指纹一致，不额外重跑业务测试。


2026-10-31文字连线版：按用户截图改用深色背景、中文说明及分支/汇合连线，取代旧SVG卡片。六张图覆盖共享Agent、平台原分工、一次提问、学习记忆、资源调度、管理员查看；仍区分现状与未来接入。内容在status.json的member_report.diagrams[].text，固定中文字符宽度避免对齐偏移。验证development/text-flow-check-20261031.json；本轮没有改业务源码、重跑模型或变更平台仓库。
