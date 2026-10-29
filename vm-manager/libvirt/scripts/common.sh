#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
DEFAULTS_FILE="${MODULE_DIR}/config/defaults.env"

[[ -f "$DEFAULTS_FILE" ]] || {
  echo "ERROR: defaults file not found: $DEFAULTS_FILE" >&2
  exit 1
}

# shellcheck disable=SC1090
source "$DEFAULTS_FILE"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

require_root_or_sudo() {
  if [[ $EUID -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    die "Run as root or install sudo."
  fi
}

as_root() {
  if [[ $EUID -eq 0 ]]; then
    "$@"
  elif [[ "${AIVIRTEACH_NONINTERACTIVE:-false}" == "true" ]]; then
    sudo -n "$@"
  else
    sudo "$@"
  fi
}

# 所有 virsh 调用都走这里：固定 LC_ALL=C（sudo 会重置环境，所以放进命令里），
# 否则非英文 locale 会翻译 dominfo 的 "State"/"shut off"，也会让下面的错误匹配失效。
virsh_c() {
  as_root env LC_ALL=C virsh --connect qemu:///system "$@"
}

# domain 是否存在，三种结果：
#   返回 0 —— 存在；
#   返回 1 —— libvirt 明确回答"没有这个 domain"；
#   其它任何失败（libvirtd 挂了、socket 权限、sudo -n 要密码……）直接 die "libvirt unavailable"。
# 调用方会把"不存在"当成可以删文件、可以放弃管理的依据，所以绝不能把"查不到"说成"不存在"。
domain_exists() {
  local name="$1" output
  if output="$(virsh_c domstate "$name" 2>&1)"; then
    return 0
  fi
  if grep -qiE "failed to get domain|domain not found" <<<"$output"; then
    return 1
  fi
  die "libvirt unavailable while checking '$name': ${output}"
}

validate_lab_id() {
  local lab_id="$1"
  [[ "$lab_id" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$ ]] \
    || die "Invalid lab ID '$lab_id'. Use 1-63 letters, numbers, '.', '_' or '-'."
}

choose_osinfo() {
  local release="${1:-}"
  local preferred=""
  local candidate
  local osinfo_list

  case "$release" in
    24.04) preferred="ubuntu24.04" ;;
    22.04) preferred="ubuntu22.04" ;;
    20.04) preferred="ubuntu20.04" ;;
  esac
  osinfo_list="$(virt-install --osinfo list 2>/dev/null || true)"

  if [[ -n "$preferred" ]] \
      && awk -v candidate="$preferred" '
        {for (i=1; i<=NF; i++) {gsub(/,$/, "", $i); if ($i == candidate) found=1}}
        END {exit !found}
      ' \
        <<<"$osinfo_list"; then
    printf '%s\n' "$preferred"
    return 0
  fi
  if [[ -n "$preferred" ]]; then
    printf 'generic\n'
    return 0
  fi
  for candidate in ubuntu24.04 ubuntu22.04 ubuntu20.04 generic; do
    if awk -v candidate="$candidate" '
        {for (i=1; i<=NF; i++) {gsub(/,$/, "", $i); if ($i == candidate) found=1}}
        END {exit !found}
      ' \
        <<<"$osinfo_list"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  printf 'generic\n'
}

ensure_default_network() {
  local network_info=""
  if ! network_info="$(
    as_root virsh --connect qemu:///system net-info "$LIBVIRT_NETWORK" 2>&1
  )"; then
    if grep -qiE \
        'network not found|no network with matching name' \
        <<<"$network_info"; then
      die "Libvirt network '$LIBVIRT_NETWORK' does not exist. Run scripts/install-host.sh first."
    fi

    # A socket permission error, an unavailable daemon, or failed non-interactive
    # sudo is not the same thing as a missing network. Preserve the real virsh/sudo
    # error so callers know whether to use sudo, re-login, or restart libvirt.
    network_info="${network_info//$'\n'/; }"
    die "Unable to query libvirt network '$LIBVIRT_NETWORK': ${network_info:-unknown error}. Run this script with sudo, or verify libvirtd and /var/run/libvirt/libvirt-sock access."
  fi
  # Do not use `virsh ... | grep -q` under `set -o pipefail`: grep may close
  # the pipe after the match and make virsh report SIGPIPE, which incorrectly
  # sends an already-active network through `net-start`.
  if ! grep -q 'Active:.*yes' <<<"$network_info"; then
    as_root virsh --connect qemu:///system net-start "$LIBVIRT_NETWORK" >/dev/null
  fi
  as_root virsh --connect qemu:///system net-autostart "$LIBVIRT_NETWORK" >/dev/null
}

ensure_storage_layout() {
  as_root install -d -m 2770 -o root -g kvm \
    "$AIVIRTEACH_ROOT" "$BASE_DIR" "$LABS_DIR" "$SEEDS_DIR"
  as_root install -d -m 0750 -o root -g libvirt "$STATE_DIR"

  if command -v setfacl >/dev/null 2>&1 && id libvirt-qemu >/dev/null 2>&1; then
    as_root setfacl -m u:libvirt-qemu:rwx,m:rwx \
      "$AIVIRTEACH_ROOT" "$BASE_DIR" "$LABS_DIR" "$SEEDS_DIR"
    as_root setfacl -d -m u:libvirt-qemu:rwx,m:rwx \
      "$BASE_DIR" "$LABS_DIR" "$SEEDS_DIR"
  fi
}

get_vm_ip() {
  local vm_name="$1"
  local ip=""

  ip="$(as_root virsh --connect qemu:///system domifaddr "$vm_name" --source agent 2>/dev/null \
    | awk '$1 != "lo" && $3 == "ipv4" && $4 !~ /^127\./ {sub(/\/.*/, "", $4); print $4; exit}')" \
    || true

  if [[ -z "$ip" ]]; then
    ip="$(as_root virsh --connect qemu:///system domifaddr "$vm_name" --source lease 2>/dev/null \
      | awk '$3 == "ipv4" && $4 !~ /^127\./ {sub(/\/.*/, "", $4); print $4; exit}')" \
      || true
  fi

  printf '%s\n' "$ip"
}
