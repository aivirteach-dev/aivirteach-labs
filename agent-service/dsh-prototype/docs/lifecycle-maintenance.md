# 生命周期、诊断与本地维护

本轮只在Ubuntu独立原型和sandbox虚构数据完成，不修改AIVirTeach或正式数据。任务归档/恢复不调用模型；诊断不新增推理；备份与恢复验证由可信管理员应用或演示CLI执行，不能交给Agent。

## 模块与配置

- `dsh_tutor/lifecycle.py`：显式schema v3→v4迁移、预览、版本核对、归档/恢复与幂等操作。发现pending拒绝迁移。
- `dsh_tutor/diagnostics.py`：可选问答诊断，每轮最多8段计时、结束后一次写入，默认滚动200条、分页20条。只记录请求/任务哈希引用、阶段、状态、有限错误码与耗时，不复制问题、答案、异常正文。
- `dsh_tutor/maintenance.py`：管理员本地SQLite适配，容量快照、一致性备份与隔离恢复验证。
- `config/lifecycle.example.json`：三类可信限制示例。无定时器、自动归档/备份/删学生记录、空间整理或自动扩容功能。

旧v1/v2/v3接口仍按各自范围兼容，不为使用新代码而强制升级旧验收数据库。仅固定虚构演示库以`--enable-lifecycle`显式升级。恢复验证专属application_id以及目录标记会让普通SQLiteChatStore拒绝打开副本。

## 归档与恢复

TaskLifecycle.preview返回任务状态、版本、相关记录数量、pending数量和preview_token；apply必须带token、operation_id。token用于检测预览后的任务/范围/记忆变化，不是授权凭据；执行仍核对可信学生、学习记录、课程版本与课时。

有未结束请求则拒绝归档，不取消已有回答、不声称远端模型停止。事务内核对与请求接受串行，避免检查后穿插新请求。相同操作编号只重放原结果，不能改成其他目标；重放是历史操作结果，界面须刷新显示当前状态。操作记录默认每学习记录1000条，到上限明确拒绝，不无限增长。

归档是任务独立状态，非记忆有效性状态：goal/attempt等原状态不被改写。归档任务仍可查看，但问答/编辑拒绝，模型记忆返回空；共享偏好在其他活动任务保留。恢复不复活已撤销、待确认、旧环境信息。归档不删除原文，释放磁盘字节报告为0。

学生管理API新增preview_lifecycle/apply_lifecycle。备份、数据库恢复、容量和诊断查询未开放到学生HTTP动作；该演示固定虚构身份，不替代正式平台认证和管理员授权。

## 诊断接入

```python
from dsh_tutor.lifecycle import migrate_lifecycle
from dsh_tutor.diagnostics import DiagnosticStore
from dsh_tutor.resources import ResourceManagedTutor

# 仅可信应用在旧请求结束后显式执行；本轮未迁移真实库。
migrate_lifecycle(sqlite_store)
diagnostics = DiagnosticStore(sqlite_store, limits={"max_events": 200, "page_limit": 20})
service = ResourceManagedTutor(policy={}, config_path=config_path,
                               state_dir=private_runtime_dir, diagnostics=diagnostics)
reply = service.answer(question, store=task_binding, handle=trusted_handle)
```

问答原模型循环不变。input/accept/history/prepare/model/result/save按实际经过的环节计时；重复请求走replay。一次批量写入有限滚动表，不新增后台队列。旧诊断会淘汰，不能当完整审计或正式聊天备份。

诊断写入失败不重跑模型、不掩盖已保存的业务结果，返回diagnostic_issue=diagnostic_unavailable。未预期程序异常尽力留下脱敏阶段码后继续向应用传播，不吞掉故障；若库不可写/范围已变，诊断也可能无法保存。哈希请求引用供定位，不声称匿名化；保留文件权限。

耗时是应用环节，model段包含本地等待/运行等成本，不等于远端GPU内部时间；模型token/usage和生产监控仍待平台适配。

## 管理员维护边界

LocalMaintenance只接受可信应用配置的源store和维护根目录，不是学生可提交的任意路径。一个共享维护目录使用排他锁，默认同时一个任务；崩溃遗留锁不自动抢占，需管理员确认没有在运行后处理。多个独立目录或服务器没有全局锁。

默认最多5份备份、每份64MiB，最多3次独立恢复验证；满了拒绝，不自动删旧副本。最低剩余256MiB另加预计目标空间；可调整但不证明生产容量。时限默认30秒、每步64页，备份使用SQLite在线backup API并有限等待。时限是合作式检查，不能硬中断所有操作系统文件读写；最终磁盘配额和系统IO调度仍需部署配置。

备份清单记录时间、schema、文件大小、SHA256和记录数量；SHA256用于完整性，不是签名。数据与清单位于权限受限的维护目录，备份包含整库，不能给普通学生下载。本地同盘副本用于恢复验证，不是异机灾备。

恢复只写入新建的validations目录，不覆盖源库；先标记目录为仅验证，再改数据库application_id。校验结构、外键及记录数量，pending保留原状态，不重新执行模型。即使旧备份包含后来撤销的内容，副本也不能直接用于聊天；latest_revocations_reconciled始终false。没有生产晋升、授权/撤销对账或在线覆盖恢复接口。

失败的新备份只清理本次尚未交付文件；已有备份不删。失败的恢复目录保留在隔离区域，占用验证次数，管理员检查后处理。完整自动保留/清理策略尚未实现。

## 虚构演示操作

Ubuntu项目内启动学生管理演示：

```text
.venv/bin/python sandbox/task_memory_demo.py --port 8790 --enable-lifecycle
```

CLI固定源为sandbox/results/task-memory-demo/fictional.db，维护位置为sandbox/results/lifecycle-maintenance：

```text
.venv/bin/python sandbox/lifecycle_admin.py status --save-snapshot
.venv/bin/python sandbox/lifecycle_admin.py backup --save-snapshot
.venv/bin/python sandbox/lifecycle_admin.py validate --backup-id <上一步backup_id> --save-snapshot
```

CLI无需运行DSH，因此可使用xf；真实模型仍沿用dsh-runner约定。--save-snapshot仅更新development/lifecycle-admin-snapshot.json，不自动启动生产监控。看板显示采集时间及范围，不把一次采样当增长预测。

组合验收`sandbox/lifecycle_acceptance.py`使用独立虚构库和模型传输替身，覆盖归档/恢复、诊断失败定位、真实SQLite备份与隔离恢复。本轮真实模型调用0；此前任务版本195项/3例模型作为历史证据保留，新版本不冒称重新通过模型语义或多人容量测试。
