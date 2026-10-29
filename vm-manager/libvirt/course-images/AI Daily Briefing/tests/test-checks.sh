#!/usr/bin/env bash
set -euo pipefail

COURSE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

manifest_course_id="$(jq -r '.courseId' "$COURSE_DIR/manifest.json")"
[[ "$manifest_course_id" == "ai-daily-briefing-v2" ]]
jq -e '
  .checkpointPolicy.guestProbe == ["P01", "P02", "P03", "P04", "P05", "P06", "P07"] and
  .checkpointPolicy.platformOrBrowserConfirmationRequired == ["P07"] and
  (.checkpointPolicy.n8nObserverRequired | index("P21") != null) and
  (.checkpointPolicy.n8nObserverRequired | index("P24") != null) and
  .checkpointPolicy.externalConfirmationRequired == ["P21", "P24"] and
  .checkpointPolicy.requiredEvidenceByCheckpoint.P21 == ["n8n-execution", "receipt"] and
  .image.os == "Ubuntu 22.04" and
  .image.ubuntuRelease == "22.04" and
  .image.goldenImage == "ai-daily-briefing-v2-ubuntu-22.04-v1.qcow2"
' "$COURSE_DIR/manifest.json" >/dev/null
bash -n "$COURSE_DIR/provision.sh"
bash -n "$COURSE_DIR/bin/aivirteach-check-step"
bash -n "$COURSE_DIR/bin/aivirteach-check-progress"
for checker in "$COURSE_DIR"/checks/*.sh; do
  bash -n "$checker"
done
for executable in \
  "$COURSE_DIR/provision.sh" \
  "$COURSE_DIR/bin/aivirteach-check-step" \
  "$COURSE_DIR/bin/aivirteach-check-progress" \
  "$COURSE_DIR"/checks/*.sh; do
  [[ -x "$executable" ]]
done

set +e
empty_batch_output="$(AIVIRTEACH_COURSE_ID="$manifest_course_id" \
  "$COURSE_DIR/bin/aivirteach-check-progress" 2>/dev/null)"
empty_batch_rc=$?
invalid_batch_output="$(AIVIRTEACH_COURSE_ID="$manifest_course_id" \
  "$COURSE_DIR/bin/aivirteach-check-progress" P00 2>/dev/null)"
invalid_batch_rc=$?
duplicate_batch_output="$(AIVIRTEACH_COURSE_ID="$manifest_course_id" \
  "$COURSE_DIR/bin/aivirteach-check-progress" P01 P01 2>/dev/null)"
duplicate_batch_rc=$?
too_many_ids=()
for ((index = 0; index < 25; index += 1)); do
  too_many_ids+=(P01)
done
too_many_batch_output="$(AIVIRTEACH_COURSE_ID="$manifest_course_id" \
  "$COURSE_DIR/bin/aivirteach-check-progress" "${too_many_ids[@]}" 2>/dev/null)"
too_many_batch_rc=$?
set -e
[[ $empty_batch_rc -eq 2 && -z "$empty_batch_output" ]]
[[ $invalid_batch_rc -eq 2 && -z "$invalid_batch_output" ]]
[[ $duplicate_batch_rc -eq 2 && -z "$duplicate_batch_output" ]]
[[ $too_many_batch_rc -eq 2 && -z "$too_many_batch_output" ]]
grep -Fq 'CHECK_STEP="/usr/local/bin/aivirteach-check-step"' \
  "$COURSE_DIR/bin/aivirteach-check-progress"

set +e
sample_result="$(AIVIRTEACH_COURSE_ID="$manifest_course_id" CHECKPOINT_ID=P99 bash -c '
  source "$1"
  failed "unit-test" "bounded evidence" "{\"preserved\":true}"
' _ "$COURSE_DIR/checks/_common.sh")"
sample_rc=$?
set -e
[[ $sample_rc -eq 1 ]]
jq -e '
  .checkpointId == "P99" and .state == "failed" and
  .facts.preserved == true and .evidenceSummary == "bounded evidence"
' <<<"$sample_result" >/dev/null

mock_bin="$(mktemp -d)"
cleanup() { rm -rf -- "$mock_bin"; }
trap cleanup EXIT
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'exit "${MOCK_CURL_RC:-0}"' > "$mock_bin/curl"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  '[[ -z "${DOCKER_HOST:-}${DOCKER_CONTEXT:-}" ]] || exit 99' \
  'if [[ "${1:-}" == --host ]]; then shift 2; fi' \
  'case "${1:-}" in' \
  '  info)' \
  '    [[ "${MOCK_DOCKER_MODE:-present}" != unavailable ]] || exit 1' \
  '    printf "%s\\n" "27.0.0"' \
  '    ;;' \
  '  volume)' \
  '    [[ "${MOCK_DOCKER_MODE:-present}" == present ]] || exit 1' \
  '    printf "%s\\n" "ai_daily_briefing_n8n_data"' \
  '    ;;' \
  '  *) exit 1 ;;' \
  'esac' > "$mock_bin/docker"
chmod 0755 "$mock_bin/curl" "$mock_bin/docker"

set +e
reachable_result="$(PATH="$mock_bin:$PATH" AIVIRTEACH_COURSE_ID="$manifest_course_id" MOCK_CURL_RC=0 \
  "$COURSE_DIR/checks/P07-editor-reachable.sh")"
reachable_rc=$?
unreachable_result="$(PATH="$mock_bin:$PATH" AIVIRTEACH_COURSE_ID="$manifest_course_id" MOCK_CURL_RC=7 \
  "$COURSE_DIR/checks/P07-editor-reachable.sh")"
unreachable_rc=$?
volume_result="$(PATH="$mock_bin:$PATH" AIVIRTEACH_COURSE_ID="$manifest_course_id" \
  DOCKER_HOST=tcp://untrusted.example:2375 DOCKER_CONTEXT=untrusted \
  MOCK_DOCKER_MODE=present \
  "$COURSE_DIR/checks/P03-docker-volume.sh")"
volume_rc=$?
volume_unknown_result="$(PATH="$mock_bin:$PATH" AIVIRTEACH_COURSE_ID="$manifest_course_id" MOCK_DOCKER_MODE=unavailable \
  "$COURSE_DIR/checks/P03-docker-volume.sh")"
volume_unknown_rc=$?
set -e

[[ $reachable_rc -eq 2 && $unreachable_rc -eq 1 ]]
[[ $volume_rc -eq 0 && $volume_unknown_rc -eq 2 ]]
jq -e '.state == "unknown" and .facts.editorReachable == true' \
  <<<"$reachable_result" >/dev/null
jq -e '.state == "failed" and .facts.editorReachable == false' \
  <<<"$unreachable_result" >/dev/null
jq -e '.state == "passed" and .facts.volumeExists == true' \
  <<<"$volume_result" >/dev/null
jq -e '.state == "unknown" and .facts.daemonReachable == false' \
  <<<"$volume_unknown_result" >/dev/null

grep -q 'Ubuntu 22.04' "$COURSE_DIR/AI Daily Briefing.md"

echo "AI Daily Briefing course-image checks passed."
