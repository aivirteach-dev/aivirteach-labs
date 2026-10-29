#!/usr/bin/env bash
# vm-control.sh 的契约测试：只有 libvirt 明确回答"没有这个 domain"时才能报 VM not found，
# libvirt 本身不可用（守护进程挂了、sudo 失败……）时必须报错，绝不能伪装成"不存在"或"空列表"。
# 用 PATH 里的假 virsh / sudo 模拟三种 libvirt 状态，不需要真实的 libvirt。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/vm-manager/libvirt/scripts/vm-control.sh"

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR"' EXIT
export CALL_LOG="$STUB_DIR/calls.log"

# 假 sudo：去掉 -n 后执行；rm 只记录不执行，用来断言删除有没有发生。
cat >"$STUB_DIR/sudo" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "-n" ]] && shift
printf '%s\n' "$*" >>"$CALL_LOG"
[[ "${1:-}" == "rm" ]] && exit 0
exec "$@"
EOF

# 假 virsh：FAKE_VIRSH_MODE=exists|missing|down|vanished 决定行为；记录收到的 LC_ALL。
cat >"$STUB_DIR/virsh" <<'EOF'
#!/usr/bin/env bash
printf 'virsh LC_ALL=%s %s\n' "${LC_ALL:-}" "$*" >>"$CALL_LOG"
[[ "$1" == "--connect" ]] && shift 2
cmd="$1"; name="${2:-}"
not_found() { echo "error: failed to get domain '$name'" >&2; exit 1; }
case "$FAKE_VIRSH_MODE" in
  down) echo "error: failed to connect to the hypervisor" >&2; exit 1 ;;
  missing)
    [[ "$cmd" == "list" ]] && exit 0
    not_found ;;
  vanished)
    [[ "$cmd" == "list" ]] && { printf 'lab-1\nlab-2\n'; exit 0; }
    [[ "$name" == "lab-2" ]] && not_found ;;
esac
case "$cmd" in
  list) echo "lab-1" ;;
  domstate) echo "running" ;;
  dominfo) printf 'Name:           %s\nState:          running\n' "$name" ;;
  domuuid) echo "11111111-2222-3333-4444-555555555555" ;;
  shutdown) echo "Domain '$name' is being shutdown" ;;
esac
EOF
chmod +x "$STUB_DIR/sudo" "$STUB_DIR/virsh"

run() {
  local mode="$1"; shift
  : >"$CALL_LOG"
  PATH="$STUB_DIR:$PATH" FAKE_VIRSH_MODE="$mode" AIVIRTEACH_NONINTERACTIVE=true \
    "$SCRIPT" "$@" >"$STUB_DIR/out" 2>"$STUB_DIR/err"
}

fail() { echo "FAIL: $*" >&2; echo "--- stderr:" >&2; cat "$STUB_DIR/err" >&2; exit 1; }

# 1. VM 存在：status 正常返回 dominfo，且 virsh 以 LC_ALL=C 运行（输出不被翻译，service 能解析 State）。
run exists status lab-1 || fail "status on existing VM should succeed"
grep -q 'State:          running' "$STUB_DIR/out" || fail "status should print dominfo"
grep -q 'virsh LC_ALL=C .*dominfo lab-1' "$CALL_LOG" || fail "virsh should run with LC_ALL=C"

# 2. libvirt 明确说不存在：报 VM not found。
for action in status stop start; do
  if run missing "$action" lab-1; then fail "$action on missing VM should fail"; fi
  grep -q 'VM not found' "$STUB_DIR/err" || fail "$action on missing VM should say VM not found"
done

# 3. libvirt 不可用：必须报错，而且不能说 VM not found。
for action in status stop start force-stop; do
  if run down "$action" lab-1; then fail "$action with libvirt down should fail"; fi
  if grep -q 'VM not found' "$STUB_DIR/err"; then fail "$action with libvirt down must not claim VM not found"; fi
  grep -q 'libvirt unavailable' "$STUB_DIR/err" || fail "$action with libvirt down should say libvirt unavailable"
done

# 4. libvirt 不可用时 delete 必须中止，不能在查不到 domain 的情况下删掉磁盘。
if run down delete lab-1 --yes; then fail "delete with libvirt down should fail"; fi
if grep -q '^rm ' "$CALL_LOG"; then fail "delete with libvirt down must not remove files"; fi

# 5. VM 确实不存在时 delete 仍然清理残留文件（幂等）。
run missing delete lab-1 --yes || fail "delete of a missing VM should succeed"
grep -q '^rm ' "$CALL_LOG" || fail "delete of a missing VM should remove leftover files"

# 6. list：libvirt 不可用时必须失败，而不是返回"成功 + 空列表"。
if run down list; then fail "list with libvirt down should fail"; fi
grep -q 'libvirt unavailable' "$STUB_DIR/err" || fail "list with libvirt down should say libvirt unavailable"

# 7. list：list 和 domuuid 之间被删掉的 domain 照常跳过。
run vanished list || fail "list should tolerate a domain that vanished mid-listing"
grep -q '^lab-1' "$STUB_DIR/out" || fail "list should include lab-1"
if grep -q '^lab-2' "$STUB_DIR/out"; then fail "list should skip vanished lab-2"; fi

# 8. credentials 只读本地文件，不依赖 libvirt。
run down credentials lab-1 || true  # 本地没有凭证文件，失败是预期的；只关心失败原因
if grep -q 'libvirt unavailable' "$STUB_DIR/err"; then fail "credentials must not depend on libvirt"; fi

echo "vm-control.sh contract checks passed."
