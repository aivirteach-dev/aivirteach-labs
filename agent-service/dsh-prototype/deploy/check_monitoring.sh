#!/usr/bin/env bash
# 在服务器上部署监控后运行，逐项检查 Phoenix 是否按要求运行。只读，不打印任何密钥。
# 用法：deploy/check_monitoring.sh   （全部通过退出码为 0）
set -uo pipefail

PROJECT="${COMPOSE_PROJECT_NAME:-dsh-platform}"
# shellcheck disable=SC2034  # 在下面 check 的 eval 字符串里使用
PORT="${PHOENIX_UI_PORT:-6006}"
fail=0

container() {
  docker ps -q --filter "label=com.docker.compose.project=${PROJECT}" --filter "label=com.docker.compose.service=$1" | head -1
}
check() {
  if eval "$2" >/dev/null 2>&1; then echo "OK    $1"; else echo "FAIL  $1"; fail=1; fi
}

PHX="$(container phoenix)"
AGENT="$(container dsh-agent)"
[[ -n "$PHX" ]] || { echo "FAIL  phoenix 容器没有运行"; exit 1; }

check "Phoenix 容器健康" '[ "$(docker inspect -f "{{.State.Health.Status}}" "$PHX")" = healthy ]'
check "Phoenix 以非 root 用户运行" '[ "$(docker inspect -f "{{.Config.User}}" "$PHX")" = "65532:65532" ]'
check "6006 只发布在 127.0.0.1" '[ "$(docker port "$PHX" 6006/tcp)" = "127.0.0.1:${PORT}" ]'
check "4317（gRPC）没有发布到宿主机" '! docker port "$PHX" 4317/tcp'
check "未带 API key 写入被拒绝（401）" \
  '[ "$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "content-type: application/x-protobuf" "http://127.0.0.1:${PORT}/v1/traces")" = 401 ]'
check "未登录读取被拒绝（401）" '[ "$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/v1/projects")" = 401 ]'
check "保留期已设置" '[ -n "$(docker inspect -f "{{range .Config.Env}}{{println .}}{{end}}" "$PHX" | grep "^PHOENIX_DEFAULT_RETENTION_POLICY_DAYS=[0-9]")" ]'

if [[ -z "$AGENT" ]]; then
  echo "FAIL  dsh-agent 容器没有运行"; fail=1
else
  # 在 Agent 容器里用它自己的密钥访问 Phoenix：既检查网络通，也检查密钥有效。
  check "Agent 能连到 Phoenix，且 API key 有效" 'docker exec "$AGENT" python -c "
import os, urllib.request
headers = dict(h.split(\"=\", 1) for h in os.environ[\"OTEL_EXPORTER_OTLP_HEADERS\"].split(\",\") if \"=\" in h)
req = urllib.request.Request(os.environ[\"OTEL_EXPORTER_OTLP_ENDPOINT\"] + \"/v1/projects\", headers=headers)
assert urllib.request.urlopen(req, timeout=5).status == 200
"'
  check "Agent 没有记录问答原文" '[ "$(docker exec "$AGENT" printenv AIVIRTEACH_OTEL_CAPTURE_CONTENT)" != true ]'
fi

exit "$fail"
