#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

usage() { echo "Usage: $0 list | {start|stop|force-stop|reboot|status|ip|vnc|credentials|delete} LAB_ID [--yes]"; }
[[ $# -ge 1 ]] || { usage; exit 1; }
ACTION="$1"; shift

if [[ "$ACTION" == "list" ]]; then
  [[ $# -eq 0 ]] || { usage; exit 1; }
  require_root_or_sudo
  # 先完整拿到名单再遍历：写成 `done < <(virsh list ...)` 时 virsh 失败会被吞掉，
  # 变成"成功 + 空列表"，调用方会误以为所有 VM 都不存在。
  VM_NAMES="$(virsh_c list --all --name 2>&1)" || die "libvirt unavailable while listing domains: ${VM_NAMES}"
  while IFS= read -r VM_NAME; do
    [[ -n "$VM_NAME" ]] || continue
    if ! VM_UUID="$(virsh_c domuuid "$VM_NAME" 2>/dev/null)"; then
      # list 和 domuuid 之间被删掉的 domain 跳过；如果是 libvirt 本身出错，domain_exists 会 die。
      domain_exists "$VM_NAME" && die "libvirt unavailable while reading UUID of '$VM_NAME'"
      continue
    fi
    VM_STATE="$(virsh_c domstate "$VM_NAME" 2>/dev/null || printf 'unknown')"
    VM_STATE="${VM_STATE%%$'\n'*}"
    printf '%s\t%s\t%s\n' "$VM_NAME" "$VM_UUID" "$VM_STATE"
  done < <(sed '/^$/d' <<<"$VM_NAMES" | LC_ALL=C sort)
  exit 0
fi

[[ $# -ge 1 ]] || { usage; exit 1; }
LAB_ID="$1"; shift
validate_lab_id "$LAB_ID"
CONFIRM=false
[[ "${1:-}" == "--yes" ]] && CONFIRM=true
require_root_or_sudo

# 只有需要 domain 的动作才查；"VM not found" 只在 libvirt 明确说不存在时出现（见 domain_exists）。
case "$ACTION" in
  start|stop|force-stop|reboot|status|ip|vnc) domain_exists "$LAB_ID" || die "VM not found" ;;
esac

case "$ACTION" in
  start) virsh_c start "$LAB_ID" ;;
  stop) virsh_c shutdown "$LAB_ID" ;;
  force-stop) virsh_c destroy "$LAB_ID" ;;
  reboot) virsh_c reboot "$LAB_ID" ;;
  status) virsh_c dominfo "$LAB_ID" ;;
  ip)
    IP="$(get_vm_ip "$LAB_ID")"
    [[ -n "$IP" ]] || die "No IPv4 address reported yet."
    printf '%s\n' "$IP"
    ;;
  vnc)
    DISPLAY="$(virsh_c vncdisplay "$LAB_ID")"
    printf 'display=%s\n' "$DISPLAY"
    [[ "$DISPLAY" =~ ^:([0-9]+)$ ]] && printf 'host=127.0.0.1\nport=%s\n' "$((5900 + BASH_REMATCH[1]))"
    ;;
  credentials)
    CRED_FILE="${STATE_DIR}/${LAB_ID}/credentials.txt"
    [[ -f "$CRED_FILE" ]] || die "Credential file not found"
    as_root cat "$CRED_FILE"
    ;;
  delete)
    [[ "$CONFIRM" == true ]] || die "Deletion requires --yes"
    # libvirt 不可用时 domain_exists 会 die，不会在 domain 可能还在跑的情况下删掉它的磁盘。
    if domain_exists "$LAB_ID"; then
      # 已关机的 domain 上 destroy 会报 "domain is not running"，可以忽略；undefine 失败则必须中止。
      virsh_c destroy "$LAB_ID" >/dev/null 2>&1 || true
      virsh_c undefine "$LAB_ID" --nvram >/dev/null 2>&1 \
        || virsh_c undefine "$LAB_ID" >/dev/null \
        || die "Failed to undefine $LAB_ID; refusing to delete its disk"
    fi
    as_root rm -f "${LABS_DIR}/${LAB_ID}.qcow2"
    as_root rm -rf "${SEEDS_DIR}/${LAB_ID}" "${STATE_DIR}/${LAB_ID}"
    log "Deleted $LAB_ID"
    ;;
  *) usage; exit 1 ;;
esac
