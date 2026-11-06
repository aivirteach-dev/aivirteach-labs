#!/usr/bin/env bash
# 在服务器上部署监控和告警后运行，逐项检查是否按要求运行。只读，不打印任何密钥。
# 用法（在 agent-service/dsh-prototype/ 目录）：deploy/check_monitoring.sh   全部通过退出码为 0
set -uo pipefail

PROJECT="${COMPOSE_PROJECT_NAME:-dsh-platform}"
# shellcheck disable=SC2034  # ENV_FILE 和下面的端口变量都在 check 的 eval 字符串里使用
ENV_FILE="${MONITORING_ENV_FILE:-.env.monitoring}"
# shellcheck disable=SC2034
PHOENIX_PORT="${PHOENIX_UI_PORT:-6006}" PROM_PORT="${PROMETHEUS_UI_PORT:-9090}" AM_PORT="${ALERTMANAGER_UI_PORT:-9093}"
fail=0

container() {
  docker ps -q --filter "label=com.docker.compose.project=${PROJECT}" --filter "label=com.docker.compose.service=$1" | head -1
}
check() {
  if eval "$2" >/dev/null 2>&1; then echo "OK    $1"; else echo "FAIL  $1"; fail=1; fi
}
prom() {  # 查询 Prometheus API，用 python 取出需要的值
  curl -s "http://127.0.0.1:${PROM_PORT}$1" | python3 -c "import json, sys; d = json.load(sys.stdin)['data']; print($2)"
}

PHX="$(container phoenix)"; COL="$(container otel-collector)"; PROM="$(container prometheus)"
AM="$(container alertmanager)"; AGENT="$(container dsh-agent)"
for pair in "phoenix:$PHX" "otel-collector:$COL" "prometheus:$PROM" "alertmanager:$AM" "dsh-agent:$AGENT"; do
  [[ -n "${pair#*:}" ]] || { echo "FAIL  ${pair%%:*} 容器没有运行"; fail=1; }
done
[[ "$fail" = 0 ]] || exit 1

echo "== Phoenix"
check "容器健康" '[ "$(docker inspect -f "{{.State.Health.Status}}" "$PHX")" = healthy ]'
check "以非 root 用户运行" '[ "$(docker inspect -f "{{.Config.User}}" "$PHX")" = "65532:65532" ]'
check "6006 只发布在 127.0.0.1" '[ "$(docker port "$PHX" 6006/tcp)" = "127.0.0.1:${PHOENIX_PORT}" ]'
check "4317（gRPC）没有发布" '! docker port "$PHX" 4317/tcp'
check "未带 API key 写入被拒绝（401）" \
  '[ "$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "content-type: application/x-protobuf" "http://127.0.0.1:${PHOENIX_PORT}/v1/traces")" = 401 ]'
check "未登录读取被拒绝（401）" '[ "$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PHOENIX_PORT}/v1/projects")" = 401 ]'
check "保留期已设置" 'docker inspect -f "{{range .Config.Env}}{{println .}}{{end}}" "$PHX" | grep -q "^PHOENIX_DEFAULT_RETENTION_POLICY_DAYS=[0-9]"'
# 密钥从 .env.monitoring 读，经进程替换传给 curl，不出现在命令行参数里。
check "PHOENIX_API_KEY 有效" 'KEY="$(sed -n "s/^PHOENIX_API_KEY=//p" "$ENV_FILE")"; [ -n "$KEY" ] &&
  [ "$(curl -s -o /dev/null -w "%{http_code}" -H @<(printf "authorization: Bearer %s" "$KEY") "http://127.0.0.1:${PHOENIX_PORT}/v1/projects")" = 200 ]'

echo "== Collector / Prometheus / Alertmanager"
check "Collector 没有发布任何端口" '[ -z "$(docker port "$COL")" ]'
check "9090 只发布在 127.0.0.1" '[ "$(docker port "$PROM" 9090/tcp)" = "127.0.0.1:${PROM_PORT}" ]'
check "9093 只发布在 127.0.0.1" '[ "$(docker port "$AM" 9093/tcp)" = "127.0.0.1:${AM_PORT}" ]'
check "Alertmanager 集群端口 9094 已关闭" '! docker port "$AM" 9094/tcp && docker inspect -f "{{json .Args}}" "$AM" | grep -q "\"--cluster.listen-address=\""'
check "Prometheus 抓取目标全部正常" '[ "$(prom "/api/v1/targets?state=active" "sorted({t[\"health\"] for t in d[\"activeTargets\"]}) == [\"up\"] and len(d[\"activeTargets\"]) >= 2")" = True ]'
check "告警规则已加载（11 条）" '[ "$(prom /api/v1/rules "sum(len(g[\"rules\"]) for g in d[\"groups\"])")" -ge 11 ]'
check "Prometheus 连上了 Alertmanager" '[ "$(prom /api/v1/alertmanagers "len(d[\"activeAlertmanagers\"])")" -ge 1 ]'
check "Agent 健康探测正常" '[ "$(prom "/api/v1/query?query=max(httpcheck_status%7Bhttp_url%3D~%22.*/health%22,http_status_class%3D%222xx%22%7D)" "d[\"result\"][0][\"value\"][1]")" = 1 ]'
check "当前没有 PhoenixExportFailing 告警" '[ "$(prom /api/v1/alerts "[a for a in d[\"alerts\"] if a[\"labels\"][\"alertname\"] == \"PhoenixExportFailing\"]")" = "[]" ]'

echo "== Agent"
check "监控数据发给 Collector" '[ "$(docker exec "$AGENT" printenv OTEL_EXPORTER_OTLP_ENDPOINT)" = "http://otel-collector:4318" ]'
check "没有开启问答原文记录" '[ "$(docker exec "$AGENT" printenv AIVIRTEACH_OTEL_CAPTURE_CONTENT)" != true ]'

exit "$fail"
