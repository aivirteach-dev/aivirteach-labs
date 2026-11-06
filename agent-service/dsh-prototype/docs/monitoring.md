# 监控：OpenTelemetry + Phoenix

Agent 按 OpenTelemetry 标准发送监控数据（`dsh_tutor/telemetry.py`），用开源的 [Arize Phoenix](https://github.com/Arize-ai/phoenix) 接收和查看。本模块不自建统计或面板。

## 记录什么

每个请求一条 trace：

| span | 类型 | 内容 |
|---|---|---|
| `invoke_agent aivirteach-tutor` | AGENT | 整个请求：request_id、lab_id、课程和课时、是否流式、返回状态、限制码、模型调用次数、工具调用次数、排队时间、首字延迟、总耗时 |
| `chat <模型>` | LLM | 每次模型调用：耗时、首字延迟、输入/输出/缓存命中 token、结束原因、失败原因 |
| `execute_tool <工具>` | TOOL | 每次工具调用：耗时、结果状态（ok/cached/denied/error）、错误码、证据编号 |

- 时间来自事件文件里的毫秒时间戳，请求结束后一次性补记，不拖慢回答。超时或调用方断开时，等 DSH 真正停下再记，所以这些情况也能看到每次调用的情况。
- 属性同时写 OpenTelemetry GenAI 约定（`gen_ai.*`）和 OpenInference 约定（`llm.token_count.*` 等）。Phoenix 直接识别后者，包括缓存命中 token。输入 token 按两套约定的要求包含缓存命中部分。
- 失败（`AGENT_BUSY`、`AGENT_TOTAL_TIMEOUT`、`MODEL_PROVIDER_ERROR`、`MODEL_BUDGET_EXHAUSTED`、`CANCELLED`、`MODEL_STREAM_INCOMPLETE`、`MODEL_STREAM_OUTPUT_TRUNCATED`、调用方断开）标为 ERROR，`error.type` 是对应的码。
- **默认不记录学生问题、回答原文和工具返回内容。** `AIVIRTEACH_OTEL_CAPTURE_CONTENT=true` 时记录问题和回答，只建议在测试环境打开。
- 不记录任何密钥和令牌。
- 监控出错（Phoenix 没开、网络不通）只写一条警告日志，不影响请求。

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

`compose.monitoring.yaml` 叠加在 `compose.platform.yaml` 上，Phoenix 和 DSH Agent 在同一个 compose 项目里，Agent 经容器网络把数据发给 `http://phoenix:6006`。

### Phoenix 的安全设置

| 设置 | 作用 |
|---|---|
| 端口只发布在 `127.0.0.1:6006` | 公网和内网都访问不到，只能从服务器本机或 SSH 隧道访问；gRPC 4317 不发布 |
| `PHOENIX_ENABLE_AUTH=true` | 界面要登录；REST 读取和 OTLP 写入都要 API key，否则返回 401 |
| 强密码策略 | 至少 12 位，含大小写、数字和特殊字符 |
| 保留 30 天 | 超过 30 天的 trace 每周日 0 点清理，所以最长约保留 37 天 |
| 磁盘上限 10 GiB，用到 90% 停止写入 | 防止监控数据写满磁盘；删除不受影响 |
| 关闭统计像素、外部字体 | Phoenix 不向外部发送任何东西 |
| 关闭 AI 助手（含服务器端 Shell、联网、GitHub）、MCP 接口、代码沙箱 | 这些功能用不到，关掉减少攻击面 |
| 非 root（uid 65532）、只读根文件系统、去掉全部 capabilities、`no-new-privileges`、内存 1 GiB、进程数 256 | 容器加固，与 Agent 容器同样的做法 |
| 镜像固定版本和 digest | `version-20.16.0-nonroot`，与本地验证的版本相同 |

### 第一次部署

在服务器上 `agent-service/dsh-prototype/` 目录里：

```bash
# 1. 准备密钥文件（不提交，权限 600）
cp deploy/monitoring.env.example .env.monitoring && chmod 600 .env.monitoring
#    填 PHOENIX_SECRET（openssl rand -hex 32）和 PHOENIX_ADMIN_INITIAL_PASSWORD；PHOENIX_API_KEY 先留空

# 2. 启动 Agent 和 Phoenix
docker compose -f compose.platform.yaml -f compose.monitoring.yaml \
  --env-file ../../config.env --env-file .env.monitoring up -d --build

# 3. 在自己的电脑上开 SSH 隧道（只转发到服务器本机地址），浏览器打开 http://localhost:6006
ssh -N -L 127.0.0.1:6006:127.0.0.1:6006 用户@服务器

# 4. 用 admin@localhost 和初始密码登录 → Settings → API Keys → System Keys，新建 dsh-agent，
#    把显示的 key 填进 .env.monitoring 的 PHOENIX_API_KEY，然后只重启 Agent：
docker compose -f compose.platform.yaml -f compose.monitoring.yaml \
  --env-file ../../config.env --env-file .env.monitoring up -d dsh-agent

# 5. 检查
deploy/check_monitoring.sh
```

第 4 步之前 Agent 的监控数据会被 Phoenix 拒收（日志里有 `Failed to export spans batch code: 401`），但回答学生不受影响。

`check_monitoring.sh` 检查：容器健康、非 root、6006 只在 127.0.0.1、4317 没发布、不带 key 写入和读取都被拒绝、保留期已设置、Agent 能连到 Phoenix 且 key 有效、没有开启问答原文记录。只读，不打印密钥。

### 部署后

- 首次登录后在 Settings → Models 添加 DeepSeek 的价格，才能看到费用。
- 改了 `PHOENIX_ADMIN_INITIAL_PASSWORD` 不会影响已有的管理员；改密码在界面里做。
- 数据在命名卷 `dsh-platform_phoenix-data` 里。删除它会清空所有监控数据。
- 升级 Phoenix：先在测试环境验证新版本，再改 `compose.monitoring.yaml` 里的版本和 digest。
- 告警（超时率、失败率、延迟）在监控阶段 4 接入。
