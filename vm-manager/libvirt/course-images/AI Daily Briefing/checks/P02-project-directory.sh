#!/usr/bin/env bash
set -euo pipefail
CHECKPOINT_ID="P02"
# shellcheck disable=SC1091
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

project_dir="/home/learner/aivirteach/ai-daily-briefing"
if [[ ! -d "$project_dir" ]]; then
  failed "filesystem" "The fixed course project directory does not exist." \
    '{"directoryExists":false}'
fi

owner="$(stat -c '%U' "$project_dir" 2>/dev/null || true)"
facts="$(jq -cn --arg owner "$owner" '{directoryExists:true, owner:$owner}')"
passed "filesystem" "The fixed course project directory exists." "$facts"
