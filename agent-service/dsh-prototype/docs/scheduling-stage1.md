# 第一阶段：请求调度与管理状态

状态：本地开发及模拟验收完成。Ubuntu是唯一开发位置：`/home/xf/projects/dsh`。

## 解决什么问题

学生分别保留课程、任务、记忆和权限，共用Agent与模型。请求多时，程序按配置分配处理名额，防止无限排队；管理员可以看见正在运行、正在等待和失败的原因。

```text
学生提交
   │
   ▼
核对输入与学生配额
   │
   ▼
等待学生轮流放行 ──→ 队列满／超时／取消：明确拒绝
   │
   ▼
读取必要教材与少量记忆
   │
   ▼
DSH／模型与受控工具
   │
   ▼
校验 → 保存 → 返回 → 释放名额

真实阶段、耗时、脱敏原因 → 只读管理面板
```

当前一个处理名额覆盖资料读取至保存。把资料准备和DSH执行分开控制属于第二阶段，本轮没有提前实现。

## 现有功能

- 学生轮转等待、同学生FIFO；默认每学生最多1个未结束请求。
- 默认1运行、4等待、10秒等待超时；Mac真实模型继续使用这一保守配置。
- 可信宿主脚本CAS热调并发、等待数、学生配额和新请求等待时限。调低并发只限制后续放行；不杀在途请求。已排队者保留原截止时间。配置文件修改须宿主显式重载，不自动生效；页面当前只读。
- 等待取消不调用模型；已经运行的取消不代表上游停止。未知中断或模型失败先暂停，再释放当前名额；管理员核实后显式恢复。
- 实际问答阶段回调，以及运行/等待明细、最近32条脱敏调度结果、最近64个已准入样本的时延统计。请求哈希可关联问答诊断，原始学生身份和消息不进入状态快照。
- 管理面板可读同进程真实gate，或读取实际问答进程发布的单实例文件心跳；文件最多64KiB，默认每0.5秒发布，5秒过期/停止时未知，缺失未接入。不创建空gate来伪装队列。
- 浏览器前台3秒刷新，数据库仍5秒共享缓存；刷新不会调用模型。

阶段目前是输入、接收、历史/记忆、准备、DSH模型与工具、结果校验、保存/重放。DSH/模型/工具仍合并，不能声称已展示GPU或工具内部进度。

## 在哪里修改

| 职责 | 文件 |
|---|---|
| 队列、限额、调限、暂停、时延 | dsh_tutor/resources.py |
| 阶段回调与原问答流程 | dsh_tutor/chat_session.py、diagnostics.py |
| 单实例心跳发布/读取与字段校验 | dsh_tutor/scheduler_status.py |
| 只读采集、HTTP | dsh_tutor/management.py、management_http.py |
| 管理页面 | management_frontend/app/page.tsx |
| 配置样例 | config/resources.example.json |
| 长期回归、组合验收、有限演示 | sandbox/tests/test_scheduling.py、sandbox/scheduling_acceptance.py、sandbox/scheduling_demo.py |

正式模块不依赖sandbox。后续删除演示前，需保留关键回归与验收证据。

## 如何查看和验证

```sh
cd /home/xf/projects/dsh
.venv/bin/python tools/dashboard.py test
.venv/bin/python sandbox/scheduling_acceptance.py
.venv/bin/python tools/start_management.py --scheduler-demo
```

前两个命令全部使用虚构数据和替身模型；不需要sudo，不调用Mac。最后一条启动一轮8名虚构学生的有限演示，通过本机3002查看。默认不带`--scheduler-demo`不会制造流量；问答进程没有发布状态时显示未接入。运行时使用同一个服务实例，不能每请求新建调度器。真实DSH模型调用仍必须使用dsh-runner。

第一阶段最终284项回归（新增21）和12项组合断言通过，Next生产构建及TypeScript检查通过，最终页面已核对。组合场景用模拟并发2验证：8名学生中2运行/4等待/2拒绝；取消一名、排队期间更换一名环境、降低并发后保持在途请求；最终只发生4次模拟推理，并验证可靠保存和重放。真实DSH/Mac请求0。

记录：development/scheduling-stage1-20261031.json、scheduling-stage1-acceptance-20261031.json、scheduling-browser-20261031.json、latest-tests.json。组合报告原件保存在sandbox/results/scheduling-stage1/<run>/，不改写旧报告。

## 下一阶段与限制

第二阶段再拆分有界资料准备与DSH执行，补充等待期间版本变化与质量/时延对比。第三阶段完成服务器接入与故障准备。

当前是单进程调度；状态文件只是观测桥，不能作为多进程全局限流。生产鉴权、正式数据库/真实Gateway、Windows/Linux真实VM、远端取消、真实模型多人容量和硬件压力尚未验收。不根据在线人数自动提高并发，不安装DeepInfer或其他大型调度框架。
