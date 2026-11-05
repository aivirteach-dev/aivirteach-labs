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

## 服务器部署（监控阶段 3）

- Phoenix 单独一个容器，只监听本机或内网，开启登录（`PHOENIX_ENABLE_AUTH=true`），设置保留期（`PHOENIX_DEFAULT_RETENTION_POLICY_DAYS=30`）。
- Agent 的 `OTEL_EXPORTER_OTLP_ENDPOINT` 指向它，`OTEL_EXPORTER_OTLP_HEADERS` 填 Phoenix 的 API key。
- 告警（超时率、失败率、延迟）在监控阶段 4 通过 OpenTelemetry Collector 接入。
