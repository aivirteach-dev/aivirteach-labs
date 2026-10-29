#!/usr/bin/env bash
set -euo pipefail
CHECKPOINT_ID="P03"
# shellcheck disable=SC1091
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

command -v docker >/dev/null 2>&1 \
  || unknown "docker" "Docker is unavailable, so the volume cannot be inspected." \
    '{"dockerAvailable":false}'
daemon_version="$(timeout 5 env -u DOCKER_HOST -u DOCKER_CONTEXT \
  docker --host unix:///var/run/docker.sock info \
  --format '{{.ServerVersion}}' 2>/dev/null || true)"
[[ -n "$daemon_version" ]] \
  || unknown "docker" "The Docker daemon is unavailable, so the volume state is unknown." \
    '{"dockerAvailable":true,"daemonReachable":false}'

set +e
volume_name="$(timeout 5 env -u DOCKER_HOST -u DOCKER_CONTEXT \
  docker --host unix:///var/run/docker.sock volume inspect ai_daily_briefing_n8n_data \
  --format '{{.Name}}' 2>/dev/null)"
inspect_rc=$?
set -e
[[ $inspect_rc -ne 124 ]] \
  || unknown "docker" "Docker volume inspection timed out." \
    '{"dockerAvailable":true,"daemonReachable":true}'
facts="$(jq -cn --arg name "$volume_name" \
  '{volumeExists:($name == "ai_daily_briefing_n8n_data")}')"
[[ $inspect_rc -eq 0 && "$volume_name" == "ai_daily_briefing_n8n_data" ]] \
  && passed "docker" "The fixed n8n data volume exists." "$facts"
failed "docker" "The fixed n8n data volume is missing." "$facts"
