#!/usr/bin/env bash
set -euo pipefail
CHECKPOINT_ID="P06"
# shellcheck disable=SC1091
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

command -v curl >/dev/null 2>&1 \
  || unknown "http" "curl is unavailable, so n8n readiness cannot be observed." \
    '{"curlAvailable":false}'
if timeout 5 curl --noproxy '*' -fsS -o /dev/null \
  http://127.0.0.1:5678/healthz/readiness 2>/dev/null; then
  passed "http" "The local n8n readiness endpoint is healthy." '{"ready":true}'
fi
failed "http" "The local n8n readiness endpoint is not healthy." '{"ready":false}'
