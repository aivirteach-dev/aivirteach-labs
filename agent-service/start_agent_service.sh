#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"

set -a
source $SCRIPT_DIR/config/agent.env
set +a

SERVICE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SERVICE_DIR}/.." && pwd)"

: "${AIVIRTEACH_AGENT_TOKEN:?Set AIVIRTEACH_AGENT_TOKEN before starting the agent}"
: "${AIVIRTEACH_DIAGNOSTIC_TOKEN:?Set AIVIRTEACH_DIAGNOSTIC_TOKEN before starting the agent}"

AGENT_HOST="${AIVIRTEACH_AGENT_HOST:-127.0.0.1}"
AGENT_PORT="${AIVIRTEACH_AGENT_PORT:-8770}"
PYTHON_BIN="${AIVIRTEACH_PYTHON:-${PROJECT_DIR}/.venv/bin/python}"

[[ -x "$PYTHON_BIN" ]] || {
  echo "ERROR: Python environment not found: $PYTHON_BIN" >&2
  exit 1
}
[[ "$AGENT_PORT" =~ ^[0-9]+$ ]] && (( AGENT_PORT >= 1 && AGENT_PORT <= 65535 )) || {
  echo "ERROR: AIVIRTEACH_AGENT_PORT must be between 1 and 65535." >&2
  exit 1
}
if [[ "${AIVIRTEACH_MODEL_PROVIDER:-fake}" == "openai_compatible" ]]; then
  : "${AIVIRTEACH_MODEL_BASE_URL:?Set AIVIRTEACH_MODEL_BASE_URL}"
  : "${AIVIRTEACH_MODEL_API_KEY:?Set AIVIRTEACH_MODEL_API_KEY}"
  : "${AIVIRTEACH_MODEL_NAME:?Set AIVIRTEACH_MODEL_NAME}"
fi

export PYTHONPATH="${SERVICE_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
cd "$SERVICE_DIR"
exec "$PYTHON_BIN" -m uvicorn agent_service:app --host "$AGENT_HOST" --port "$AGENT_PORT"
