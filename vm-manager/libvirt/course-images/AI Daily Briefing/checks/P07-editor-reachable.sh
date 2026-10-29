#!/usr/bin/env bash
set -euo pipefail
CHECKPOINT_ID="P07"
# shellcheck disable=SC1091
source "$(dirname -- "${BASH_SOURCE[0]}")/_common.sh"

command -v curl >/dev/null 2>&1 \
  || unknown "http" "curl is unavailable, so editor reachability cannot be observed." \
    '{"curlAvailable":false}'
reachable=false
timeout 5 curl --noproxy '*' -fsS -o /dev/null http://127.0.0.1:5678/ 2>/dev/null \
  && reachable=true
facts="$(jq -cn --argjson reachable "$reachable" '{editorReachable:$reachable, ownerSetupVerified:false}')"
if [[ "$reachable" != true ]]; then
  failed "http" "The local n8n editor is not reachable." "$facts"
fi
unknown "http" \
  "HTTP can prove editor reachability, but owner setup needs browser or platform evidence." \
  "$facts"
