#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

require_root_or_sudo
[[ -r /etc/os-release ]] || die "Cannot read /etc/os-release."
# shellcheck disable=SC1091
source /etc/os-release

if [[ "${ID:-}" != "ubuntu" && "${ID:-}" != "debian" ]]; then
  die "Unsupported host OS: ${ID:-unknown}. Use Ubuntu or Debian."
fi

TARGET_USER="${SUDO_USER:-${USER}}"
[[ "$TARGET_USER" != "root" ]] || warn "No non-root administrator detected."

log "Installing KVM, QEMU, libvirt and image-building tools..."
as_root apt-get update
as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y \
  qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients \
  virtinst virt-viewer cloud-image-utils libguestfs-tools bridge-utils \
  cpu-checker curl wget openssl ca-certificates acl netcat-openbsd \
  libosinfo-bin osinfo-db jq

log "Enabling libvirt..."
as_root systemctl enable --now libvirtd

if [[ "$TARGET_USER" != "root" ]]; then
  as_root usermod -aG libvirt,kvm "$TARGET_USER"
fi

ensure_storage_layout

NETWORK_INFO=""
if ! NETWORK_INFO="$(
  as_root virsh --connect qemu:///system net-info "$LIBVIRT_NETWORK" 2>&1
)"; then
  if ! grep -qiE \
      'network not found|no network with matching name' \
      <<<"$NETWORK_INFO"; then
    NETWORK_INFO="${NETWORK_INFO//$'\n'/; }"
    die "Unable to query libvirt network '$LIBVIRT_NETWORK': ${NETWORK_INFO:-unknown error}. Verify libvirtd and sudo access."
  fi

  DEFAULT_NET_XML="$(mktemp)"
  cat > "$DEFAULT_NET_XML" <<'NETXML'
<network>
  <name>default</name>
  <forward mode='nat'/>
  <bridge name='virbr0' stp='on' delay='0'/>
  <ip address='192.168.122.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='192.168.122.2' end='192.168.122.254'/>
    </dhcp>
  </ip>
</network>
NETXML
  as_root virsh --connect qemu:///system net-define "$DEFAULT_NET_XML"
  rm -f "$DEFAULT_NET_XML"
fi

ensure_default_network

if command -v kvm-ok >/dev/null 2>&1; then
  as_root kvm-ok || warn "KVM acceleration is unavailable. Check BIOS/UEFI virtualization."
fi

as_root virsh --connect qemu:///system list --all
as_root virsh --connect qemu:///system net-list --all

cat <<HOSTDONE

Host installation is complete.
Storage root: $AIVIRTEACH_ROOT
Administrator: $TARGET_USER

Log out and back in before using virsh without sudo.

Next:
  ./scripts/build-base-image.sh
HOSTDONE
