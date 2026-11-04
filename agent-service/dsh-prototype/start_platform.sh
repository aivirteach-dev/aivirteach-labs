#!/usr/bin/env bash
# 在宿主机上启动 DSH 版平台 Agent（端口 8770），读取与 main 相同的 agent-service/config/agent.env。
# 与 main 的 start_agent_service.sh 二选一，不能同时占用 8770。
set -euo pipefail

MODULE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${AIVIRTEACH_AGENT_ENV_FILE:-${MODULE_DIR}/../config/agent.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${AIVIRTEACH_AGENT_TOKEN:?Set AIVIRTEACH_AGENT_TOKEN before starting the agent}"

AGENT_HOST="${AIVIRTEACH_AGENT_HOST:-127.0.0.1}"
AGENT_PORT="${AIVIRTEACH_AGENT_PORT:-8770}"
PYTHON_BIN="${AIVIRTEACH_PYTHON:-${MODULE_DIR}/.venv/bin/python}"

[[ -x "$PYTHON_BIN" ]] || { echo "ERROR: Python environment not found: $PYTHON_BIN" >&2; exit 1; }
[[ "$AGENT_PORT" =~ ^[0-9]+$ ]] && (( AGENT_PORT >= 1 && AGENT_PORT <= 65535 )) || {
  echo "ERROR: AIVIRTEACH_AGENT_PORT must be between 1 and 65535." >&2
  exit 1
}
if [[ "${AIVIRTEACH_MODEL_PROVIDER:-fake}" != "fake" ]]; then
  : "${AIVIRTEACH_MODEL_BASE_URL:?Set AIVIRTEACH_MODEL_BASE_URL}"
  : "${AIVIRTEACH_MODEL_NAME:?Set AIVIRTEACH_MODEL_NAME}"
fi
if [[ "${AIVIRTEACH_MODEL_PROVIDER:-fake}" == "openai_compatible" ]]; then
  : "${AIVIRTEACH_MODEL_API_KEY:?Set AIVIRTEACH_MODEL_API_KEY}"
fi

cd "$MODULE_DIR"
export PYTHONDONTWRITEBYTECODE=1
exec "$PYTHON_BIN" -m uvicorn dsh_tutor.platform_app:app \
  --host "$AGENT_HOST" --port "$AGENT_PORT" --no-access-log
