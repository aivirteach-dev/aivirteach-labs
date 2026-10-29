#!/usr/bin/env bash
set -euo pipefail
umask 077

SERVICE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SERVICE_DIR}/.." && pwd)"
PYTHON_BIN="${AIVIRTEACH_PROGRESS_PYTHON:-${PROJECT_DIR}/.venv/bin/python}"

: "${AIVIRTEACH_PROGRESS_WORKER_ID:?Set AIVIRTEACH_PROGRESS_WORKER_ID}"
: "${AIVIRTEACH_PROGRESS_SERVER_TOKEN:?Set AIVIRTEACH_PROGRESS_SERVER_TOKEN}"
: "${AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN:?Set AIVIRTEACH_PROGRESS_DIAGNOSTIC_TOKEN}"
[[ -x "$PYTHON_BIN" ]] || {
  echo "Python environment not found: $PYTHON_BIN" >&2
  exit 1
}

cd "$SERVICE_DIR"
exec "$PYTHON_BIN" -m progress_worker.worker "$@"
