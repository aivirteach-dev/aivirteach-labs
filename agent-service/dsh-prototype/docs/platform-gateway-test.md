# 用真实 Gateway 验收 DSH 平台接口

目的：确认 DSH 版 Agent 能通过真实 Diagnostic Gateway 查看一台**测试用**虚拟机，并按 main 的格式返回。整个过程只读，不改 Gateway、不重启任何现有服务。

## 安全前提

1. 只用专门的测试虚拟机，记下它的 `lab_id`。填错会读到学生虚拟机的日志和文件。
2. 诊断令牌（`AIVIRTEACH_DIAGNOSTIC_TOKEN`）是密钥：放在只有自己能读的文件里，不要写进仓库、命令行或聊天。
3. 测试用的 Agent 只监听 `127.0.0.1`，不占用 main 正在用的 8770（下面用 18770）。

## 方式一（推荐）：在 Labs 主机的测试目录里跑

Gateway 就在本机，不需要隧道。

```bash
git clone --branch fix/dsh-platform-interface https://github.com/aivirteach-dev/aivirteach-labs.git ~/dsh-gateway-test
cd ~/dsh-gateway-test/agent-service/dsh-prototype
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## 方式二：在别的机器上跑，经 SSH 隧道访问 Gateway

在能 SSH 到 Labs 主机的机器（例如 xf-legion）上开隧道，只转发到本机回环地址：

```bash
ssh -N -L 127.0.0.1:18765:127.0.0.1:8765 用户@labs主机
```

之后下面的 `AIVIRTEACH_GATEWAY_URL` 改成 `http://127.0.0.1:18765`。测完 Ctrl+C 关闭隧道。

## 启动测试用 Agent

```bash
# 密钥文件示例：~/.config/aivirteach/test.env（chmod 600），内容为
#   AIVIRTEACH_DIAGNOSTIC_TOKEN=...
#   AIVIRTEACH_MODEL_API_KEY=...
set -a; . ~/.config/aivirteach/test.env; set +a
export AIVIRTEACH_AGENT_TOKEN=$(openssl rand -hex 24)
export AIVIRTEACH_AGENT_HOST=127.0.0.1 AIVIRTEACH_AGENT_PORT=18770 AIVIRTEACH_AGENT_ENV_FILE=/nonexistent
export AIVIRTEACH_GATEWAY_URL=http://127.0.0.1:8765
export AIVIRTEACH_MODEL_PROVIDER=openai_compatible AIVIRTEACH_MODEL_BASE_URL=https://api.deepseek.com/
export AIVIRTEACH_MODEL_NAME=deepseek-v4-flash AIVIRTEACH_MODEL_THINKING=disabled
./start_platform.sh &
curl -s http://127.0.0.1:18770/ready
```

`/ready` 应返回 `"status":"ready"` 和 `"diagnostic_gateway_configured":true`。

## 发一次请求

把 `lab_id` 换成测试虚拟机的 ID：

```bash
cat > /tmp/req.json <<'EOF'
{"request_id":"c30beac8-d1db-4b1a-8df0-79aa8208e275","lab_id":"填测试虚拟机的lab_id",
 "question":"帮我看看虚拟机现在是什么状态？","response_language":"zh-CN",
 "course":{"course_id":"n8n-agent-builder","version":1,"title":"Build an AI Daily Briefing with n8n"},
 "current_step":{"module_id":"runtime-environment","lesson_id":"install-n8n","sequence":4,"title":"Install and Start n8n"}}
EOF
curl -sN -H "Authorization: Bearer $AIVIRTEACH_AGENT_TOKEN" -H "Content-Type: application/json" \
  -d @/tmp/req.json http://127.0.0.1:18770/v1/agent/diagnose/stream | tee /tmp/out.sse
```

这个请求不带 `diagnostic_scope`，与 Server 现在的请求一样，只允许 `get_vm_status`、`get_guest_agent_status`。要测更多工具，加上 `diagnostic_scope`（格式见仓库根 `AGENT.md` 的示例）。

## 检查清单

| 检查项 | 期望 |
|---|---|
| 事件顺序 | `accepted → started → context_ready → reasoning_started → tool_started/tool_finished … → answer_started → assistant_delta … → answer_finished → result → done` |
| `tool_finished` 的 `status` | `ok`；若为 `error`，看 `error_code`（`VM_NOT_FOUND`、`GATEWAY_AUTH_FAILED`、`GUEST_AGENT_UNAVAILABLE` 等，与 main 相同） |
| `result.response.evidence` | 每个成功的工具有一条 `obs-00N`，`summary` 来自真实 Gateway |
| `result.response.answer` | 引用的状态与 `evidence` 一致，没有声称修改过虚拟机 |
| `result.response.usage` | DeepSeek 下三项都是整数 |
| 日志 | Agent 日志里没有出现两个令牌 |

对比时可以把同一个请求发给 main 的 Agent（`/v1/agent/diagnose`），两边的 `evidence`、`tool_trace` 结构应相同，回答措辞会不同。

## 收尾

```bash
kill %1                      # 停掉测试用 Agent
rm -rf ~/dsh-gateway-test    # 方式一的测试目录
```
