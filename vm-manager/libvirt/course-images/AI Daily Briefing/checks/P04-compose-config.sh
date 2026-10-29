#!/usr/bin/env bash
set -euo pipefail
CHECKPOINT_ID="P04"
# shellcheck disable=SC1091
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

project_dir="/home/learner/aivirteach/ai-daily-briefing"
[[ -f "$project_dir/compose.yaml" ]] \
  || failed "docker" "compose.yaml is missing from the fixed project directory." \
    '{"composeFileExists":false}'

command -v docker >/dev/null 2>&1 \
  || unknown "docker" "Docker is unavailable, so compose.yaml cannot be normalized." \
    '{"composeFileExists":true,"dockerAvailable":false}'
timeout 5 env -u DOCKER_HOST -u DOCKER_CONTEXT -u COMPOSE_FILE \
  -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
  docker compose version >/dev/null 2>&1 \
  || unknown "docker" "Docker Compose is unavailable, so compose.yaml cannot be normalized." \
    '{"composeFileExists":true,"dockerAvailable":true,"composeAvailable":false}'

set +e
config="$(timeout 10 env -u DOCKER_HOST -u DOCKER_CONTEXT -u COMPOSE_FILE \
  -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
  docker --host unix:///var/run/docker.sock compose \
  --project-directory "$project_dir" -f "$project_dir/compose.yaml" \
  config --format json 2>/dev/null)"
config_rc=$?
set -e
[[ $config_rc -ne 124 ]] \
  || unknown "docker" "Docker Compose normalization timed out." \
    '{"composeFileExists":true,"composeAvailable":true}'
[[ $config_rc -eq 0 ]] \
  || failed "docker" "Docker Compose rejected compose.yaml." \
    '{"composeFileExists":true,"configValid":false}'

if jq -e '
  .services.n8n as $service |
  ($service.image == "docker.n8n.io/n8nio/n8n:2.31.7") and
  ($service.container_name == "ai-daily-briefing-n8n") and
  ($service.environment.TZ == "Asia/Kuala_Lumpur") and
  ($service.environment.GENERIC_TIMEZONE == "Asia/Kuala_Lumpur") and
  (($service.ports | length) == 1) and
  (all($service.ports[]?;
    ((.target | tostring) == "5678") and
    ((.published | tostring) == "5678") and
    (.host_ip == "127.0.0.1"))) and
  (($service.volumes | length) == 1) and
  (all($service.volumes[]?;
    (.source == "ai_daily_briefing_n8n_data") and
    (.target == "/home/node/.n8n"))) and
  (.volumes.ai_daily_briefing_n8n_data.external == true)
' >/dev/null <<<"$config"; then
  passed "docker" "compose.yaml matches the fixed image, container, port, timezone, and volume contract." \
    '{"composeFileExists":true,"configValid":true,"fixedContractMatches":true}'
fi

failed "docker" "compose.yaml is valid but does not match the fixed course contract." \
  '{"composeFileExists":true,"configValid":true,"fixedContractMatches":false}'
