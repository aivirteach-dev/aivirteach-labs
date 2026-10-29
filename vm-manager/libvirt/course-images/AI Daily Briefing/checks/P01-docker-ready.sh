#!/usr/bin/env bash
set -euo pipefail
CHECKPOINT_ID="P01"
# shellcheck disable=SC1091
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

docker_version=""
compose_version=""
service_state="$(systemctl is-active docker 2>/dev/null || true)"
daemon_version=""

command -v docker >/dev/null 2>&1 \
  && docker_version="$(docker --version 2>/dev/null | head -n 1 || true)"
[[ -n "$docker_version" ]] \
  && compose_version="$(env -u DOCKER_HOST -u DOCKER_CONTEXT \
    docker compose version 2>/dev/null | head -n 1 || true)"
[[ "$service_state" == "active" ]] \
  && daemon_version="$(timeout 5 env -u DOCKER_HOST -u DOCKER_CONTEXT \
    docker --host unix:///var/run/docker.sock info \
    --format '{{.ServerVersion}}' 2>/dev/null || true)"

facts="$(jq -cn \
  --arg dockerVersion "$docker_version" \
  --arg composeVersion "$compose_version" \
  --arg serviceState "$service_state" \
  --arg daemonVersion "$daemon_version" \
  '{dockerAvailable:($dockerVersion != ""), composeAvailable:($composeVersion != ""),
    serviceState:$serviceState, daemonReachable:($daemonVersion != "")}')"

if [[ -n "$docker_version" && -n "$compose_version" \
  && "$service_state" == "active" ]]; then
  passed "docker" "Docker Engine and Compose are available and the service is active." "$facts"
fi
failed "docker" "Docker Engine, Compose, or the service is not ready." "$facts"
