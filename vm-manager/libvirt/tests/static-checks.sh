#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
for script in "$ROOT"/scripts/*.sh; do
  echo "bash -n: $script"
  bash -n "$script"
done
for guest_tool in "$ROOT"/guest-tools/*.sh; do
  echo "bash -n: $guest_tool"
  bash -n "$guest_tool"
done
if "$ROOT/guest-tools/browser-smoke-test.sh" http://example.com \
  >/dev/null 2>&1; then
  echo "Browser smoke test accepted an insecure URL." >&2
  exit 1
fi
"$ROOT/../../tests/test_vm_logs.sh"
"$ROOT/../../tests/test_vm_control.sh"
grep -q 'qemu-kvm' "$ROOT/scripts/install-host.sh"
grep -q 'jq' "$ROOT/scripts/install-host.sh"
grep -q 'cloud-localds' "$ROOT/scripts/build-base-image.sh"
grep -q -- '--refresh-source' "$ROOT/scripts/build-base-image.sh"
grep -q 'Using cached, checksum-verified Ubuntu image' \
  "$ROOT/scripts/build-base-image.sh"
grep -q 'qemu-img create' "$ROOT/scripts/create-learner-vm.sh"
grep -q 'xrdp' "$ROOT/scripts/build-base-image.sh"
grep -q 'https://packages.mozilla.org/apt' "$ROOT/scripts/build-base-image.sh"
! grep -Fq "apt-cache policy firefox | grep -Fq" \
  "$ROOT/scripts/build-base-image.sh"
grep -q '35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3' \
  "$ROOT/scripts/build-base-image.sh"
grep -q '/etc/aivirteach/browser-ready' "$ROOT/scripts/build-base-image.sh"
grep -q 'image-style.*value="4"' "$ROOT/scripts/build-base-image.sh"
grep -q 'color-style.*value="0"' "$ROOT/scripts/build-base-image.sh"
grep -q 'property name="rgba1" type="array"' \
  "$ROOT/scripts/build-base-image.sh"
grep -q 'backdrop-cycle-enable.*value="false"' \
  "$ROOT/scripts/build-base-image.sh"
grep -q '/var/log/libvirt/qemu/' "$ROOT/scripts/vm-logs.sh"
"$ROOT/course-images/AI Daily Briefing/tests/test-checks.sh"
"$ROOT/scripts/build-base-image.sh" --validate-only \
  --course-image-dir "$ROOT/course-images/AI Daily Briefing"
echo "Static checks passed."
