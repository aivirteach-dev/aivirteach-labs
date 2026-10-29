# AIVirTeach Libvirt Learner Image Module

This module installs KVM/QEMU and libvirt on an Ubuntu host, builds a reusable Ubuntu learner desktop image, creates per-learner qcow2 overlays, and manages learner VM lifecycle.

```text
Learner browser → Guacamole/RDP → learner VM:3389
AIVirTeach API → LibvirtLabDriver → Ubuntu host → QEMU/KVM
```

The golden image includes Ubuntu 24.04, XFCE, XRDP, xorgxrdp, QEMU guest agent, OpenSSH, Git, Python, and a locked `learner` account. Each learner receives a copy-on-write qcow2 overlay, so learner changes do not modify the base image.

## Files

```text
config/defaults.env
scripts/install-host.sh
scripts/build-base-image.sh
scripts/create-learner-vm.sh
scripts/vm-control.sh
course-images/<course>/manifest.json
course-images/<course>/provision.sh
course-images/<course>/checks/
templates/customize-image.example.sh
tests/static-checks.sh
```

## 1. Install the Ubuntu host

```bash
chmod +x scripts/*.sh tests/*.sh
./scripts/install-host.sh
```

This installs KVM/QEMU, libvirt, image tools, ACL support, and the default NAT network. Log out and back in after installation.

Verify:

```bash
kvm-ok
virsh --connect qemu:///system list --all
virsh --connect qemu:///system net-list --all
```

## 2. Configure the image

Edit:

```bash
nano config/defaults.env
```

Main settings:

```bash
GOLDEN_IMAGE_SIZE="40G"
BUILDER_MEMORY_MB="4096"
BUILDER_VCPUS="2"
LEARNER_MEMORY_MB="4096"
LEARNER_VCPUS="2"
```

Optional customization:

```bash
cp templates/customize-image.example.sh customize-image.sh

# Or create your own:
cat > customize-image.sh <<'CUSTOM'
#!/usr/bin/env bash
set -euo pipefail
install -d -m 0755 /opt/aivirteach/guest-agent
python3 -m venv /opt/aivirteach/venv
/opt/aivirteach/venv/bin/pip install --upgrade pip
# Install code-server, JupyterLab, course dependencies, or guest agent here.
CUSTOM
chmod +x customize-image.sh
```

Set its absolute path:

```bash
CUSTOMIZE_SCRIPT="/absolute/path/to/customize-image.sh"
```

Never embed production tokens or private keys in the golden image.

The general image can also install a project-owned JPG or PNG as both the XFCE
desktop wallpaper and LightDM background. Keep the asset under `general/` and
set, for example:

```bash
WALLPAPER_IMAGE="${MODULE_DIR}/general/logo.jpg"
GOLDEN_IMAGE_NAME="ubuntu-24.04-xfce-xrdp-v3.qcow2"
```

The image is shown with XFCE's **Scaled** style over a solid white background;
wallpaper cycling is disabled. Defaults for the common XRDP and virtio monitor
names are seeded before first login, then a one-time login helper updates every
runtime monitor/workspace property that XFCE creates. Learners may change the
wallpaper afterward.

## 3. Build the golden qcow2 image

```bash
./scripts/build-base-image.sh
```

The script downloads the current released Ubuntu 24.04 cloud image, verifies
`SHA256SUMS`, starts a builder VM, installs XFCE/XRDP and Mozilla's signed
Firefox DEB package, validates that Firefox is not a Snap, cleans clone-specific
identity, powers off, and publishes:

```text
/var/lib/libvirt/images/aivirteach/base/ubuntu-24.04-xfce-xrdp-v3.qcow2
```

The build refuses to publish unless `/etc/aivirteach/browser-ready` records a
Firefox DEB from `packages.mozilla.org`. The Mozilla signing key is also checked
against the expected fingerprint during the build.

Monitor:

```bash
sudo virsh list --all
sudo virsh vncdisplay avt-template-builder
```

A VNC display of `:0` maps to host port `5900` and listens on `127.0.0.1`.

Force rebuild:

```bash
./scripts/build-base-image.sh --force
```

The downloaded Ubuntu cloud image is cached under `BASE_DIR`. Every build
refreshes only the small codename-specific checksum file and verifies the local
image. If the checksum matches, `--force` reuses the cached image instead of
downloading it again. To intentionally fetch a fresh source image, use:

```bash
./scripts/build-base-image.sh --force --refresh-source
```

If checksum metadata cannot be refreshed, the normal mode may use previously
cached metadata; `--refresh-source` always fails closed instead of trusting a
stale checksum.

Do not force-replace a base image while learner overlays depend on it. Use a new
versioned filename instead.

### Browser acceptance test

After creating and starting a new learner VM from v3, run inside that VM:

```bash
cat /etc/aivirteach/browser-ready
/usr/local/bin/aivirteach-browser-smoke-test
```

The smoke test runs as `learner` even when invoked by the root QEMU Guest Agent,
rejects Snap Firefox, and uses headless Firefox to render `https://example.com/`.
A successful result starts with:

```text
browser_smoke_status=passed
```

From the host, confirm that the new overlay actually uses v3:

```bash
sudo qemu-img info /var/lib/libvirt/images/aivirteach/labs/LAB_ID.qcow2 \
  | grep 'backing file'
```

The build script refuses `--force` while any learner overlays exist. Publish versioned images instead of replacing an in-use base.

### Course-specific golden image example

`course-images/AI Daily Briefing/` demonstrates how a reviewed course bundle is embedded during the golden-image build. The Markdown remains source guidance; the builder executes only the reviewed `provision.sh` and installs the read-only checkpoint scripts.

Validate the bundle without creating a VM:

```bash
./course-images/AI\ Daily\ Briefing/tests/test-checks.sh
./scripts/build-base-image.sh --validate-only \
  --course-image-dir "$PWD/course-images/AI Daily Briefing"
```

Build its Ubuntu 22.04 course image:

```bash
sudo ./scripts/build-base-image.sh \
  --course-image-dir "$PWD/course-images/AI Daily Briefing"
```

The course bundle must stay under `course-images/`, contain no symlinks or likely secret files, use at most 128 files, and be at most 5 MiB uncompressed. Build settings are read non-executably from allowlisted fields in `manifest.json`. The image is published only after the powered-off builder disk contains markers matching the image name, course ID, and bundle SHA-256.

Inside a VM created from that image, a checkpoint can be queried with:

```bash
sudo /usr/local/bin/aivirteach-check-step P04
```

The host does not ask the learner to self-report this result. Progress Worker
calls the Diagnostic Gateway, which invokes the fixed batch dispatcher through
QEMU Guest Agent:

```text
/usr/local/bin/aivirteach-check-progress P01 P02
```

The Gateway requires the Server-owned VM UUID and accepts only checkpoint IDs
as tool parameters; it returns normalized JSON without raw stdout/stderr.

See `course-images/AI Daily Briefing/README.md` for the supported checkpoint boundary. The VM API does not yet map `course_id` to this golden image automatically.

## 4. Create a learner VM

```bash
./scripts/create-learner-vm.sh lab-001
```

Optional resources and SSH key:

```bash
./scripts/create-learner-vm.sh lab-001 \
  --memory 4096 \
  --vcpus 2 \
  --ssh-key "$HOME/.ssh/id_ed25519.pub"
```

When using an explicitly selected custom base image, pass its Ubuntu release as well so libosinfo metadata matches the guest:

```bash
./scripts/create-learner-vm.sh lab-course-demo \
  --base-image /var/lib/libvirt/images/aivirteach/base/CUSTOM.qcow2 \
  --os-release 22.04
```

The command prints the learner's generated RDP password and immutable libvirt
domain UUID (`VM instance ID`). The UUID is also returned as `vm_instance_id`
by `POST /v1/vms`; use it in the Server's progress assignment so a deleted and
recreated `lab-001` cannot inherit stale events. A protected copy is stored at:

```text
/var/lib/aivirteach-labs/lab-001/credentials.txt
```

The learner disk is:

```text
/var/lib/libvirt/images/aivirteach/labs/lab-001.qcow2
```

Inspect the backing chain:

```bash
sudo qemu-img info --backing-chain \
  /var/lib/libvirt/images/aivirteach/labs/lab-001.qcow2
```

For production, replace plaintext credential files with encrypted, short-lived secrets.

## 5. VM lifecycle

```bash
./scripts/vm-control.sh status lab-001
./scripts/vm-control.sh start lab-001
./scripts/vm-control.sh stop lab-001
./scripts/vm-control.sh force-stop lab-001
./scripts/vm-control.sh reboot lab-001
./scripts/vm-control.sh ip lab-001
./scripts/vm-control.sh vnc lab-001
sudo ./scripts/vm-control.sh credentials lab-001
./scripts/vm-control.sh delete lab-001 --yes
```

Deletion removes only the learner VM, overlay, seed, and learner credentials. It does not delete the golden image.

## 6. RDP access

Get the VM IP and test XRDP from the Ubuntu host:

```bash
VM_IP="$(./scripts/vm-control.sh ip lab-001)"
nc -vz "$VM_IP" 3389
```

The private libvirt network is normally `192.168.122.0/24`. During development, tunnel RDP:

```bash
ssh -N -L 13389:${VM_IP}:3389 your-user@UBUNTU_HOST_IP
```

Connect your RDP client to:

```text
127.0.0.1:13389
```

For AIVirTeach, use:

```text
Browser → HTTPS → Apache Guacamole → private learner VM:3389
```

Do not expose each VM's RDP port directly to the Internet.

## 7. VNC recovery

```bash
./scripts/vm-control.sh vnc lab-001
```

For `display=:0`, tunnel host port 5900:

```bash
ssh -N -L 15900:127.0.0.1:5900 your-user@UBUNTU_HOST_IP
```

Connect a VNC viewer to `127.0.0.1:15900`.

## 8. Troubleshooting

Permission denied:

```bash
namei -l /var/lib/libvirt/images/aivirteach/base/IMAGE.qcow2
./scripts/install-host.sh
sudo -u libvirt-qemu test -r /path/to/base && echo readable
sudo -u libvirt-qemu test -w /path/to/overlay && echo writable
```

Never use `chmod -R 777`.

Builder does not finish:

```bash
sudo virsh console avt-template-builder
sudo virsh vncdisplay avt-template-builder
```

Inside the builder VM:

```bash
sudo cloud-init status --long
sudo tail -n 200 /var/log/cloud-init-output.log
```

The temporary builder password exists during an active build at:

```text
/var/lib/libvirt/images/aivirteach/seeds/avt-template-builder/builder-password.txt
```

No guest IP:

```bash
sudo virsh net-dhcp-leases default
sudo virsh domifaddr lab-001 --source lease
sudo virsh domifaddr lab-001 --source agent
```

RDP failure inside the VM:

```bash
sudo systemctl status xrdp xrdp-sesman --no-pager
sudo ss -lntp | grep 3389
sudo tail -n 100 /var/log/xrdp.log
sudo tail -n 100 /var/log/xrdp-sesman.log
```

Use the `Xorg` session at the XRDP login screen.

## 9. Production integration

These scripts are for a manual prototype. In production, use a dedicated privileged VM-manager service with validated IDs, fixed storage roots, CPU/RAM quotas, audit logs, idempotent actions, and reconciliation. Do not let the public FastAPI process execute arbitrary shell input.

Suggested interface:

```python
class LibvirtLabDriver:
    async def create_lab(self, course_id: str, user_id: str): ...
    async def start_lab(self, lab_id: str): ...
    async def stop_lab(self, lab_id: str): ...
    async def reset_lab(self, lab_id: str): ...
    async def delete_lab(self, lab_id: str): ...
    async def get_status(self, lab_id: str): ...
    async def get_ip_address(self, lab_id: str): ...
```

## 10. Validate scripts

```bash
./templates/customize-image.example.sh
tests/static-checks.sh
```
