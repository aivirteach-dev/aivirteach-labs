#!/usr/bin/env bash
set -euo pipefail

BUNDLE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CHECK_SOURCE="${BUNDLE_DIR}/checks"

[[ $EUID -eq 0 ]] || { echo "provision.sh must run as root" >&2; exit 1; }
[[ -f "${BUNDLE_DIR}/manifest.json" ]] || { echo "manifest.json is missing" >&2; exit 1; }
[[ -d "$CHECK_SOURCE" ]] || { echo "checks directory is missing" >&2; exit 1; }

# jq is used only to build bounded JSON evidence. Installing it does not
# pre-complete the learner's Docker/n8n course steps.
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends jq

COURSE_ID="$(jq -er '.courseId | strings' "${BUNDLE_DIR}/manifest.json")"
SOURCE_DOCUMENT="$(jq -er '.sourceDocument | strings' "${BUNDLE_DIR}/manifest.json")"
[[ "$COURSE_ID" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] \
  || { echo "manifest courseId is invalid" >&2; exit 1; }
[[ "$SOURCE_DOCUMENT" =~ ^[A-Za-z0-9][-A-Za-z0-9._\ ]*\.md$ ]] \
  || { echo "manifest sourceDocument is invalid" >&2; exit 1; }
[[ -f "${BUNDLE_DIR}/${SOURCE_DOCUMENT}" ]] \
  || { echo "manifest sourceDocument is missing" >&2; exit 1; }
[[ -f "${BUNDLE_DIR}/bin/aivirteach-check-progress" ]] \
  || { echo "course progress dispatcher is missing" >&2; exit 1; }
CHECK_TARGET="/usr/local/lib/aivirteach/checks/${COURSE_ID}"
COURSE_TARGET="/opt/aivirteach/courses/${COURSE_ID}"

install -d -m 0555 -o root -g root "$CHECK_TARGET"
for checker in "$CHECK_SOURCE"/*.sh; do
  bash -n "$checker"
  install -m 0555 -o root -g root "$checker" "$CHECK_TARGET/$(basename "$checker")"
done

install -d -m 0555 -o root -g root "$COURSE_TARGET"
install -m 0444 -o root -g root "${BUNDLE_DIR}/manifest.json" "$COURSE_TARGET/manifest.json"
install -m 0444 -o root -g root "${BUNDLE_DIR}/${SOURCE_DOCUMENT}" "$COURSE_TARGET/source.md"
install -m 0555 -o root -g root "${BUNDLE_DIR}/bin/aivirteach-check-step" \
  /usr/local/bin/aivirteach-check-step
install -m 0555 -o root -g root "${BUNDLE_DIR}/bin/aivirteach-check-progress" \
  /usr/local/bin/aivirteach-check-progress

printf '%s\n' "$COURSE_ID" > /etc/aivirteach/course-id.tmp
chown root:root /etc/aivirteach/course-id.tmp
chmod 0444 /etc/aivirteach/course-id.tmp
mv /etc/aivirteach/course-id.tmp /etc/aivirteach/course-id
