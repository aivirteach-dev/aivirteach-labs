#!/usr/bin/env bash
set -Eeuo pipefail

TEST_URL="${1:-https://example.com/}"
FIREFOX_BIN="/usr/bin/firefox"
LEARNER_USER="${AIVIRTEACH_LEARNER_USER:-learner}"

fail() {
  printf 'browser_smoke_status=failed\nreason=%s\n' "$1" >&2
  exit 1
}

[[ "$TEST_URL" =~ ^https:// ]] \
  || fail "test URL must use HTTPS"

if (( EUID == 0 )); then
  learner_home="$(getent passwd "$LEARNER_USER" | cut -d: -f6)"
  [[ -n "$learner_home" ]] || fail "learner user not found"
  exec runuser -u "$LEARNER_USER" -- env \
    HOME="$learner_home" \
    USER="$LEARNER_USER" \
    LOGNAME="$LEARNER_USER" \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    "$0" "$TEST_URL"
fi

[[ -x "$FIREFOX_BIN" ]] || fail "Firefox binary is missing"
FIREFOX_REALPATH="$(readlink -f "$FIREFOX_BIN")"
[[ "$FIREFOX_REALPATH" != /snap/* ]] \
  || fail "Firefox resolves to a Snap binary"
dpkg-query -W -f='${Status}\n' firefox 2>/dev/null \
  | grep -Fxq 'install ok installed' \
  || fail "Firefox DEB package is not installed"
if command -v snap >/dev/null 2>&1 && snap list firefox >/dev/null 2>&1; then
  fail "Firefox Snap is installed"
fi

WORK_DIR="$(mktemp -d)"
cleanup() {
  rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT

install -d -m 0700 "$WORK_DIR/profile"
timeout 90 "$FIREFOX_BIN" \
  --headless \
  --no-remote \
  --profile "$WORK_DIR/profile" \
  --screenshot "$WORK_DIR/screenshot.png" \
  "$TEST_URL" >/dev/null 2>"$WORK_DIR/firefox.stderr" \
  || {
    tail -n 20 "$WORK_DIR/firefox.stderr" >&2 || true
    fail "Firefox failed to render the test page"
  }

[[ -s "$WORK_DIR/screenshot.png" ]] \
  || fail "Firefox did not create a screenshot"

printf '%s\n' \
  'browser_smoke_status=passed' \
  'browser=firefox' \
  'packaging=deb' \
  "binary=${FIREFOX_REALPATH}" \
  "url=${TEST_URL}"
