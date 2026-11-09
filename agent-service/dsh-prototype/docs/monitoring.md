# 监控：OpenTelemetry + Phoenix

Agent 按 OpenTelemetry 标准发送监控数据（`dsh_tutor/telemetry.py`），用开源的 [Arize Phoenix](https://github.com/Arize-ai/phoenix) 接收和查看。本模块不自建统计或面板。

## 记录什么

每个请求一条 trace：

| span | 类型 | 内容 |
|---|---|---|
| `invoke_agent aivirteach-tutor` | AGENT | 整个请求：request_id、lab_id、课程和课时、是否流式、返回状态、限制码（只记原因码，不记模型写的说明句子）、评估没通过校验时的类别（`aivirteach.assessment_error`，如 `schema:diagnosis.confidence:enum`，不含原文）、模型调用次数、工具调用次数、排队时间、首字延迟、总耗时 |
| `chat <模型>` | LLM | 每次模型调用：所属阶段（`aivirteach.phase`：screen（看图，span 名用看图模型的名字）/ assessment / finalization / answer）、连接类错误后的重试次数（`aivirteach.retries`）、耗时、首字延迟、输入/输出/缓存命中 token、结束原因、失败原因（含提供方返回的 error / aborted） |
| `execute_tool <工具>` | TOOL | 每次工具调用（含 DSH 启动时的预先检查，见 README“工具范围和预先检查”）：耗时、结果状态（ok/cached/denied/error）、错误码、证据编号 |

- 时间来自事件文件里的毫秒时间戳，请求结束后一次性补记，不拖慢回答。超时或调用方断开时，等 DSH 真正停下再记，所以这些情况也能看到每次调用的情况。
- 属性同时写 OpenTelemetry GenAI 约定（`gen_ai.*`）和 OpenInference 约定（`llm.token_count.*` 等）。Phoenix 直接识别后者，包括缓存命中 token。输入 token 按两套约定的要求包含缓存命中部分。
- 失败（`AGENT_BUSY`、`AGENT_TOTAL_TIMEOUT`、`MODEL_PROVIDER_ERROR`、`MODEL_BUDGET_EXHAUSTED`、`CANCELLED`、`MODEL_STREAM_INCOMPLETE`、`MODEL_STREAM_OUTPUT_TRUNCATED`、调用方断开）标为 ERROR，`error.type` 是对应的码。
- **默认不记录学生问题、回答原文和工具返回内容。** `AIVIRTEACH_OTEL_CAPTURE_CONTENT=true` 时记录问题和回答，只建议在测试环境打开。
- 不记录任何密钥和令牌。
- 监控出错（Phoenix 没开、网络不通）只写一条警告日志，不影响请求。
- 不开监控时也有线索：超过 25 秒、失败或被取消的请求，在 Agent 日志里写一行各次调用的摘要（阶段、耗时、首字、结束原因、工具次数、原因码、评估失败类别），不含任何原文。

## 配置

只用 OpenTelemetry 的标准环境变量，外加一个内容开关：

| 变量 | 说明 |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Phoenix 地址，例如 `http://127.0.0.1:6006`（自动加 `/v1/traces`）。**不设就完全不发送。** |
| `OTEL_EXPORTER_OTLP_HEADERS` | Phoenix 开启登录后填 `authorization=Bearer <Phoenix API key>` |
| `OTEL_RESOURCE_ATTRIBUTES` | 可选。默认服务名 `aivirteach-dsh-agent`，Phoenix 项目名 `aivirteach-agent`；例如 `openinference.project.name=staging` 改项目名 |
| `OTEL_SDK_DISABLED` | `true` 时关闭 |
| `AIVIRTEACH_OTEL_CAPTURE_CONTENT` | `true` 时记录问题和回答原文，默认 `false` |

## 本机试用

```bash
python3 -m venv ~/phoenix-venv && ~/phoenix-venv/bin/pip install arize-phoenix
PHOENIX_HOST=127.0.0.1 ~/phoenix-venv/bin/phoenix serve        # 界面和接收地址都是 http://127.0.0.1:6006
# 另一个终端：
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:6006 ./start_platform.sh
```

浏览器打开 http://127.0.0.1:6006，进入 `aivirteach-agent` 项目。

Phoenix 不内置 DeepSeek 的价格：要看费用，在 Settings → Models 里按当前价目添加模型名和每百万 token 的输入、缓存命中输入、输出价格。

## 服务器部署

`compose.monitoring.yaml` 叠加在 `compose.platform.yaml` 上，和 DSH Agent 在同一个 compose 项目里运行：

```
Agent ──trace──> otel-collector ──trace──> phoenix       查看每次请求（6006）
                       └──指标──> prometheus ──> alertmanager   告警规则（9090）→ 通知（9093）
```

- **Agent** 只把数据发给 Collector（容器网络内，不需要密钥）。
- **Collector**（OpenTelemetry Collector）把 trace 原样转给 Phoenix（带 Phoenix API key），同时从 trace 生成告警用的指标：每类请求、模型调用、工具调用的次数、失败数和耗时（按 `error.type` 细分），以及 token 累计；每 30 秒探测 Agent 和 Phoenix 的健康接口。它不发布任何端口。
- **Prometheus** 抓取这些指标，按 `deploy/alerts.yml` 判断告警。
- **Alertmanager** 负责通知、合并重复告警。默认不发通知（告警只显示在它的界面里），配置方法见下面的“告警通知”。

全部是开源组件，镜像都固定了版本和 digest。

### 安全设置

| 设置 | 作用 |
|---|---|
| 界面只发布在 `127.0.0.1`（Phoenix 6006、Prometheus 9090、Alertmanager 9093） | 公网和内网都访问不到，只能从服务器本机或 SSH 隧道访问；Collector、Phoenix gRPC 4317、Alertmanager 集群端口 9094 都不开放 |
| Phoenix 开启登录 | 界面要登录；REST 读取和 OTLP 写入都要 API key，否则返回 401 |
| Phoenix 强密码策略 | 至少 12 位，含大小写、数字和特殊字符 |
| Phoenix 保留 30 天 | 超过 30 天的 trace 每周日 0 点清理，所以最长约保留 37 天 |
| Phoenix 磁盘上限 10 GiB，用到 90% 停止写入 | 防止监控数据写满磁盘；删除不受影响 |
| Prometheus 保留 15 天、最多 2 GB | 告警只需要近期数据 |
| Phoenix 关闭统计像素、外部字体、AI 助手（含服务器端 Shell、联网、GitHub）、MCP 接口、代码沙箱 | 不向外部发送任何东西，减少攻击面 |
| 所有容器非 root、只读根文件系统、去掉全部 capabilities、`no-new-privileges`、限内存和进程数 | 与 Agent 容器同样的加固 |

### 第一次部署

在服务器上 `agent-service/dsh-prototype/` 目录里：

```bash
# 1. 准备密钥文件（不提交，权限 600）
cp deploy/monitoring.env.example .env.monitoring && chmod 600 .env.monitoring
#    填 PHOENIX_SECRET（openssl rand -hex 32）和 PHOENIX_ADMIN_INITIAL_PASSWORD；PHOENIX_API_KEY 先留空

# 2. 启动
docker compose -f compose.platform.yaml -f compose.monitoring.yaml \
  --env-file ../../config.env --env-file .env.monitoring up -d --build

# 3. 在自己的电脑上开 SSH 隧道（只转发到服务器本机地址）
ssh -N -L 127.0.0.1:6006:127.0.0.1:6006 -L 127.0.0.1:9090:127.0.0.1:9090 -L 127.0.0.1:9093:127.0.0.1:9093 用户@服务器
#    Phoenix http://localhost:6006   Prometheus http://localhost:9090   Alertmanager http://localhost:9093

# 4. 用 admin@localhost 和初始密码登录 Phoenix → Settings → API Keys → System Keys，新建 otel-collector，
#    把显示的 key 填进 .env.monitoring 的 PHOENIX_API_KEY，然后只重启 Collector：
docker compose -f compose.platform.yaml -f compose.monitoring.yaml \
  --env-file ../../config.env --env-file .env.monitoring up -d otel-collector

# 5. 检查
deploy/check_monitoring.sh
```

第 4 步之前 Phoenix 会拒收数据，`PhoenixExportFailing` 告警会触发；告警指标和学生的回答都不受影响。

`check_monitoring.sh` 检查 20 项：各容器在运行；Phoenix 健康、非 root、端口只在 127.0.0.1、4317 没开放、不带 key 读写都被拒绝、保留期已设置、`PHOENIX_API_KEY` 有效；Collector 没开放端口；Prometheus 和 Alertmanager 只在 127.0.0.1、集群端口已关闭；抓取目标都正常、11 条规则已加载、Prometheus 连上了 Alertmanager、Agent 健康探测正常、没有 `PhoenixExportFailing`；Agent 发往 Collector、没有开启问答原文记录。只读，不打印密钥。

### 告警规则

规则在 `deploy/alerts.yml`，单元测试在 `deploy/alerts.test.yml`（`promtool test rules deploy/alerts.test.yml`）。改阈值后要重新跑测试。

| 告警 | 级别 | 条件 |
|---|---|---|
| `AgentDown` | critical | Agent `/health` 连续 2 分钟不是 2xx |
| `AgentErrorRateHigh` | critical | 10 分钟内请求失败率超过 20%，且至少 5 个请求，持续 2 分钟 |
| `AgentTimeouts` | warning | 15 分钟内至少 3 个请求总超时（`AGENT_TOTAL_TIMEOUT`） |
| `AgentBusy` | warning | 10 分钟内出现排队超时（`AGENT_BUSY`），说明并发名额用满 |
| `AgentSlow` | warning | 15 分钟内 95% 分位耗时超过 30 秒，且至少 5 个请求，持续 5 分钟 |
| `ModelCallErrorRateHigh` | warning | 10 分钟内模型调用失败率超过 20%，且至少 5 次调用，持续 2 分钟 |
| `GatewayErrors` | warning | 15 分钟内至少 5 次工具调用因 Gateway 或虚拟机问题失败 |
| `TokenUsageHigh` | warning | 1 小时内 token（输入含缓存命中 + 输出）超过 200 万 |
| `MonitoringPipelineDown` | critical | Prometheus 连续 2 分钟抓不到 Collector（此时其他 Agent 告警都会失效） |
| `PhoenixDown` | warning | Phoenix `/healthz` 连续 5 分钟不可用 |
| `PhoenixExportFailing` | warning | 10 分钟内有 span 写不进 Phoenix（常见是 API key 错误或磁盘到上限） |

`AgentDown` 触发时，失败率、超时、变慢、模型失败这几条告警不再重复通知。

一个细节：计数器刚出现时（例如某种错误第一次发生），Prometheus 默认算不到这第一次。这里用 protobuf 格式抓取并开启 `created-timestamp-zero-ingestion`，让新计数器从 0 开始算；Collector 自身的计数器不带创建时间，`PhoenixExportFailing` 另加了一个条件补上。

### 告警通知

默认的 `deploy/alertmanager.yml` 不发通知。要发邮件或企业微信：

```bash
cp deploy/alertmanager.example.yml .alertmanager.yml       # 已被 .gitignore 忽略
#   填好收件人、SMTP 或企业微信应用的信息，只保留要用的渠道
chgrp 65534 .alertmanager.yml && chmod 640 .alertmanager.yml   # 容器里的 nobody 用户要能读，其他用户不能读
echo "ALERTMANAGER_CONFIG=./.alertmanager.yml" >> .env.monitoring
docker compose -f compose.platform.yaml -f compose.monitoring.yaml \
  --env-file ../../config.env --env-file .env.monitoring up -d alertmanager
```

钉钉、飞书没有内置支持，需要另加一个 webhook 转换服务，暂不提供。

### 部署后

- 首次登录 Phoenix 后在 Settings → Models 添加 DeepSeek 的价格，才能看到费用。
- 改了 `PHOENIX_ADMIN_INITIAL_PASSWORD` 不会影响已有的管理员；改密码在界面里做。
- 数据在命名卷 `dsh-platform_phoenix-data`、`dsh-platform_prometheus-data`、`dsh-platform_alertmanager-data` 里，删除会清空对应数据。
- 升级任一组件：先在测试环境验证新版本（告警规则跑一遍 `promtool test rules`），再改 `compose.monitoring.yaml` 里的版本和 digest。
