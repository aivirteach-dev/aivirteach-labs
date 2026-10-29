#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

usage() {
  cat <<USAGE
Usage: $0 LAB_ID [--password VALUE] [--ssh-key FILE] [--memory MB] [--vcpus N] [--base-image PATH] [--os-release VERSION] [--autostart]
USAGE
}

[[ $# -ge 1 ]] || { usage; exit 1; }
LAB_ID="$1"; shift
validate_lab_id "$LAB_ID"

PASSWORD=""
SSH_KEY_FILE=""
MEMORY_MB="$LEARNER_MEMORY_MB"
VCPUS="$LEARNER_VCPUS"
BASE_IMAGE="${BASE_DIR}/${GOLDEN_IMAGE_NAME}"
OS_RELEASE="$UBUNTU_RELEASE"
AUTOSTART=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --password) PASSWORD="${2:?Missing password}"; shift ;;
    --ssh-key) SSH_KEY_FILE="${2:?Missing SSH key}"; shift ;;
    --memory) MEMORY_MB="${2:?Missing memory}"; shift ;;
    --vcpus) VCPUS="${2:?Missing vCPUs}"; shift ;;
    --base-image) BASE_IMAGE="${2:?Missing image path}"; shift ;;
    --os-release) OS_RELEASE="${2:?Missing OS release}"; shift ;;
    --autostart) AUTOSTART=true ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

[[ "$OS_RELEASE" =~ ^[0-9]{2}\.[0-9]{2}$ ]] \
  || die "OS release must look like 22.04 or 24.04."

require_root_or_sudo
for cmd in qemu-img cloud-localds virt-install virsh openssl; do require_command "$cmd"; done
[[ -f "$BASE_IMAGE" ]] || die "Golden image not found: $BASE_IMAGE"
[[ -z "$SSH_KEY_FILE" || -f "$SSH_KEY_FILE" ]] || die "SSH key not found: $SSH_KEY_FILE"
if domain_exists "$LAB_ID"; then die "VM already exists: $LAB_ID"; fi

ensure_storage_layout
ensure_default_network

OVERLAY="${LABS_DIR}/${LAB_ID}.qcow2"
SEED_DIR="${SEEDS_DIR}/${LAB_ID}"
SEED_ISO="${SEED_DIR}/seed.iso"
LAB_STATE_DIR="${STATE_DIR}/${LAB_ID}"
[[ ! -e "$OVERLAY" ]] || die "Overlay exists: $OVERLAY"

cleanup_failed_create() {
  warn "Learner VM creation failed; removing partial resources for $LAB_ID."
  as_root virsh --connect qemu:///system destroy "$LAB_ID" >/dev/null 2>&1 || true
  as_root virsh --connect qemu:///system undefine "$LAB_ID" --nvram >/dev/null 2>&1 \
    || as_root virsh --connect qemu:///system undefine "$LAB_ID" >/dev/null 2>&1 \
    || true
  as_root rm -f "$OVERLAY"
  as_root rm -rf "$SEED_DIR" "$LAB_STATE_DIR"
}
trap cleanup_failed_create ERR

as_root install -d -m 0770 -o root -g kvm "$SEED_DIR"
as_root install -d -m 0750 -o root -g libvirt "$LAB_STATE_DIR"
command -v setfacl >/dev/null 2>&1 && as_root setfacl -m u:libvirt-qemu:rwx,m:rwx "$SEED_DIR"

[[ -n "$PASSWORD" ]] || PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 20)"
PASSWORD_HASH="$(openssl passwd -6 "$PASSWORD")"
if [[ -n "$SSH_KEY_FILE" ]]; then
  SSH_KEY="$(tr -d '\r\n' < "$SSH_KEY_FILE")"
fi

as_root qemu-img create -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$OVERLAY"
as_root chown libvirt-qemu:kvm "$OVERLAY"
as_root chmod 0660 "$OVERLAY"

USER_DATA_TMP="$(mktemp)"
cat > "$USER_DATA_TMP" <<CLOUDCFG
#cloud-config
hostname: ${LAB_ID}
manage_etc_hosts: true
ssh_pwauth: false
disable_root: true
users:
  - name: learner
    shell: /bin/bash
    groups: [adm, sudo, video, render]
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    lock_passwd: false
    hashed_passwd: "${PASSWORD_HASH}"
CLOUDCFG

if [[ -n "$SSH_KEY_FILE" ]]; then
  printf '    ssh_authorized_keys:\n      - %s\n' "$SSH_KEY" >> "$USER_DATA_TMP"
fi

cat >> "$USER_DATA_TMP" <<CLOUDREST
write_files:
  - path: /etc/aivirteach/session-id
    owner: root:root
    permissions: "0600"
    content: |
      ${LAB_ID}
runcmd:
  - [bash, -lc, "ssh-keygen -A"]
  - [bash, -lc, "usermod -U learner || true"]
  - [bash, -lc, "systemctl restart ssh qemu-guest-agent xrdp || true"]
CLOUDREST

META_DATA_TMP="$(mktemp)"
cat > "$META_DATA_TMP" <<METACFG
instance-id: ${LAB_ID}-$(date +%s)-$(openssl rand -hex 4)
local-hostname: ${LAB_ID}
METACFG

as_root mv "$USER_DATA_TMP" "${SEED_DIR}/user-data.yaml"
as_root mv "$META_DATA_TMP" "${SEED_DIR}/meta-data.yaml"
as_root chown root:kvm "${SEED_DIR}/user-data.yaml" "${SEED_DIR}/meta-data.yaml"
as_root chmod 0640 "${SEED_DIR}/user-data.yaml" "${SEED_DIR}/meta-data.yaml"
as_root cloud-localds "$SEED_ISO" "${SEED_DIR}/user-data.yaml" "${SEED_DIR}/meta-data.yaml"
as_root chown libvirt-qemu:kvm "$SEED_ISO"
as_root chmod 0640 "$SEED_ISO"

as_root bash -c "umask 077; cat > '${LAB_STATE_DIR}/credentials.txt' <<CREDS
lab_id=${LAB_ID}
username=learner
password=${PASSWORD}
rdp_port=${RDP_PORT}
CREDS"

OSINFO="$(choose_osinfo "$OS_RELEASE")"
as_root virt-install \
  --connect qemu:///system \
  --name "$LAB_ID" \
  --memory "$MEMORY_MB" \
  --vcpus "$VCPUS" \
  --cpu host-passthrough \
  --osinfo "$OSINFO" \
  --import \
  --disk "path=${OVERLAY},format=qcow2,bus=virtio" \
  --disk "path=${SEED_ISO},device=cdrom,readonly=on" \
  --network "network=${LIBVIRT_NETWORK},model=virtio" \
  --graphics vnc,listen=127.0.0.1 \
  --video virtio \
  --channel unix,target.type=virtio,target.name=org.qemu.guest_agent.0 \
  --noautoconsole

[[ "$AUTOSTART" == true ]] && as_root virsh --connect qemu:///system autostart "$LAB_ID"
VM_INSTANCE_ID="$(as_root virsh --connect qemu:///system domuuid "$LAB_ID")"
[[ "$VM_INSTANCE_ID" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] \
  || die "libvirt returned an invalid domain UUID for $LAB_ID."
as_root bash -c "printf 'vm_instance_id=%s\\n' '$VM_INSTANCE_ID' >> '${LAB_STATE_DIR}/credentials.txt'"
trap - ERR

cat <<RESULT
Learner VM created.
VM: $LAB_ID
VM instance ID: $VM_INSTANCE_ID
Username: learner
RDP password: $PASSWORD
Credentials: ${LAB_STATE_DIR}/credentials.txt
Get IP: ./scripts/vm-control.sh ip $LAB_ID
RESULT
