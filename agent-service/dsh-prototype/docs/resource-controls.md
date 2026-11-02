# 当前资源控制：总准入、资料准备与DSH执行

Ubuntu权威项目：/home/xf/projects/dsh。第一、二阶段本地范围已完成；见docs/preparation-stage2.md。原来整个问答占槽的实现已替换为同一ChatSession的准备/执行两段，没有并行旧实现。

```python
from dsh_tutor.resources import ResourceManagedTutor, load_resource_policy
service = ResourceManagedTutor(policy=load_resource_policy(resource_config),
                               config_path=model_config, state_dir=private_state_root)
reply = service.answer(request, store=task_store, handle=verified_handle,
                       tool_bridge=trusted_bridge)
snapshot = service.gate.snapshot()
```

宿主启动时创建并复用；不能每请求新建门控。身份来自可信应用。ManagedReply.admission说明准入，chat.state说明业务结果；accepted不等于保存成功或语义正确。

## 默认配置

| 配置 | 值 | 作用 |
|---|---|---|
| max_active / validated_max_active | 1 / 1 | DSH并发/人工容量声明，Mac仍串行 |
| max_waiting | 4 | 额外总准入名额，总上限=并发+该值，覆盖准备与等待 |
| max_per_student | 1 | 每学生跨学习记录未结束请求数 |
| queue_timeout_seconds | 10 | 准备完成后等DSH时限 |
| max_preparing | 2 | 读取/组装资料并发 |
| preparation_queue_timeout_seconds | 5 | 等准备名额时限 |
| preparation_timeout_seconds | 3 | 准备预算，SQLite锁等待与长查询共用截止时间 |
| revalidation_timeout_seconds | 2 | 等DSH后重核 |
| min_free_bytes | 256MiB | 运行卷空闲阈值，不自动删学生数据 |

config/resources.example.json是可信样例。容量声明需匹配模型/硬件/课程实测，填大数字不能证明机器更快。在线人数不等于活跃推理，不根据人数自动加并发。

## 动态调整

```python
revision = service.gate.snapshot()['policy_revision']
service.gate.reconfigure({'max_active': 1, 'max_preparing': 2}, expected_revision=revision)
service.reload_limits(resource_config, expected_revision=service.gate.snapshot()['policy_revision'])
```

可信宿主持有原实例才能调用。允许热调并发、额外准入、学生配额及等待/准备/复核时限；容量声明、上下文/存储预算变更需重启核对。CAS冲突/非法值整次拒绝。降低上限保留在途与已接受者，新放行按新上限；原等待者保留截止时间。页面仍只读；文件修改须宿主显式重载，不自动生效。

等DSH按学生轮转、同学生FIFO；准备使用有界FIFO。等待取消不调用模型，已接受pending尽力结束；失败标recovery_required，不重试。已执行取消不代表远端停止。未知模型失败/中断先暂停再释放槽，核实后显式恢复；暂停和配额不跨重启持久化。关闭唤醒等待者，不杀在途。

## 观测与范围

snapshot包括总占用、准备/等准备、DSH执行/等待、策略版本、32条脱敏结果和64样本时延；不出学生ID/正文。应用耗时指取得DSH名额到校验/保存，不含提前准备；各准备/等待分开计量，不冒充GPU或首token计时。

同进程ManagementView注入真实gate；独立进程用SchedulerStatusPublisher/Reader读取固定单实例心跳文件。0.5秒发布、64KiB上限、单写者、5秒过期即未知，缺失显示未接入；不是全局限流。不同运行账号共享时只授予指定文件必要读取权限，不开放数据库、模型目录或sudo。模型验收报告交付在sandbox/results/runner-check/preparation-report.json，development目录仍私有。

当前308项回归、11项第二阶段组合断言、12项第一阶段组合断言、前端构建及管理页面通过；真实Qwen3例核心行为通过，保留文案问题。模拟并发2不是Mac/服务器容量证明。多个进程或其他客户端共用模型时必须统一总预算。普通平台60秒与原型180秒预算未对齐，第三阶段补接入/故障契约；正式身份/数据库/真实Gateway/VM、取消、usage与容量另验收。assess_capacity只基于输入的需求和实测给建议，不自动扩缩容或清理。
