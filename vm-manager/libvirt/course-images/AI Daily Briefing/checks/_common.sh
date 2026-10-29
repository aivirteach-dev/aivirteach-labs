#!/usr/bin/env bash
set -euo pipefail

COURSE_ID="${AIVIRTEACH_COURSE_ID:-}"
if [[ -z "$COURSE_ID" && -r /etc/aivirteach/course-id ]]; then
  COURSE_ID="$(</etc/aivirteach/course-id)"
fi
[[ "$COURSE_ID" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] \
  || { echo "Course identity is unavailable or invalid" >&2; exit 2; }
CHECKPOINT_ID="${CHECKPOINT_ID:-unknown}"

emit_result() {
  local state="$1"
  local evidence_type="$2"
  local summary="$3"
  local facts_json="${4-}"
  local exit_code="$5"

  [[ -n "$facts_json" ]] || facts_json='{}'
  jq -e . >/dev/null 2>&1 <<<"$facts_json" || facts_json='{}'
  jq -cn \
    --arg courseId "$COURSE_ID" \
    --arg checkpointId "$CHECKPOINT_ID" \
    --arg state "$state" \
    --arg observedAt "$(date --utc +'%Y-%m-%dT%H:%M:%SZ')" \
    --arg evidenceType "$evidence_type" \
    --arg evidenceSummary "$summary" \
    --argjson facts "$facts_json" \
    '{schemaVersion:1, courseId:$courseId, checkpointId:$checkpointId,
      state:$state, observedAt:$observedAt, evidenceType:$evidenceType,
      evidenceSummary:$evidenceSummary, facts:$facts}'
  exit "$exit_code"
}

passed() { emit_result "passed" "$1" "$2" "${3-}" 0; }
failed() { emit_result "failed" "$1" "$2" "${3-}" 1; }
unknown() { emit_result "unknown" "$1" "$2" "${3-}" 2; }
