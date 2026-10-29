#!/usr/bin/env bash
set -euo pipefail
CHECKPOINT_ID="P05"
# shellcheck disable=SC1091
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

command -v docker >/dev/null 2>&1 \
  || unknown "docker" "Docker is unavailable, so the container cannot be inspected." \
    '{"dockerAvailable":false}'
daemon_version="$(timeout 5 env -u DOCKER_HOST -u DOCKER_CONTEXT \
  docker --host unix:///var/run/docker.sock info \
  --format '{{.ServerVersion}}' 2>/dev/null || true)"
[[ -n "$daemon_version" ]] \
  || unknown "docker" "The Docker daemon is unavailable, so the container state is unknown." \
    '{"dockerAvailable":true,"daemonReachable":false}'

set +e
inspect="$(timeout 5 env -u DOCKER_HOST -u DOCKER_CONTEXT \
  docker --host unix:///var/run/docker.sock inspect \
  ai-daily-briefing-n8n 2>/dev/null)"
inspect_rc=$?
set -e
[[ $inspect_rc -ne 124 ]] \
  || unknown "docker" "Docker container inspection timed out." \
    '{"dockerAvailable":true,"daemonReachable":true}'
[[ $inspect_rc -eq 0 && -n "$inspect" ]] \
  || failed "docker" "The fixed n8n container does not exist." '{"containerExists":false}'

facts="$(jq -c '
  .[0] as $container |
  ($container.HostConfig.PortBindings // {}) as $portBindings |
  {
    containerExists:true,
    running:(
      ($container.State.Running == true) and
      ($container.State.Paused != true) and
      ($container.State.Restarting != true)
    ),
    imageMatches:($container.Config.Image == "docker.n8n.io/n8nio/n8n:2.31.7"),
    composeServiceMatches:(
      $container.Config.Labels["com.docker.compose.service"] == "n8n"
    ),
    loopbackPort:(
      (($portBindings | keys) == ["5678/tcp"]) and
      (($portBindings["5678/tcp"] | length) == 1) and
      (all($portBindings["5678/tcp"][]?;
        (.HostIp == "127.0.0.1") and (.HostPort == "5678")))
    ),
    volumeMounted:(any($container.Mounts[]?;
      (.Name == "ai_daily_briefing_n8n_data") and (.Destination == "/home/node/.n8n")))
  }
' <<<"$inspect")"

if jq -e \
  '.running and .imageMatches and .composeServiceMatches and .loopbackPort and .volumeMounted' \
  >/dev/null <<<"$facts"; then
  passed "docker" "The Compose n8n service is running with the expected image, port, and volume." "$facts"
fi
failed "docker" "The n8n container exists but does not match the running contract." "$facts"
