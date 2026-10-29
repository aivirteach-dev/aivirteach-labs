#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh"

FORCE=false
REFRESH_SOURCE=false
COURSE_IMAGE_DIR=""
VALIDATE_ONLY=false
usage() {
  echo "Usage: $0 [--force] [--refresh-source] [--course-image-dir DIR] [--validate-only]"
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=true ;;
    --refresh-source) REFRESH_SOURCE=true ;;
    --validate-only) VALIDATE_ONLY=true ;;
    --course-image-dir)
      [[ $# -ge 2 ]] || die "--course-image-dir requires a directory."
      COURSE_IMAGE_DIR="$2"
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

COURSE_ID=""
COURSE_BUNDLE_B64=""
COURSE_BUNDLE_SHA256=""
BROWSER_SMOKE_SOURCE="${SCRIPT_DIR}/../guest-tools/browser-smoke-test.sh"
BROWSER_SMOKE_B64=""
TEMP_FILES=()
cleanup_temp_files() {
  local path
  for path in "${TEMP_FILES[@]}"; do
    rm -f -- "$path"
  done
}
trap cleanup_temp_files EXIT

pack_course_bundle() {
  COURSE_ARCHIVE="$(mktemp)"
  TEMP_FILES+=("$COURSE_ARCHIVE")
  tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
    -C "$COURSE_IMAGE_DIR" -czf "$COURSE_ARCHIVE" .
  COURSE_ARCHIVE_BYTES="$(stat -c '%s' "$COURSE_ARCHIVE")"
  (( COURSE_ARCHIVE_BYTES <= 6 * 1024 * 1024 )) \
    || die "Compressed course bundle exceeds the 6 MiB cloud-init limit."
  COURSE_BUNDLE_SHA256="$(sha256sum "$COURSE_ARCHIVE" | awk '{print $1}')"
  COURSE_BUNDLE_B64="$(base64 -w0 "$COURSE_ARCHIVE")"
  (( ${#COURSE_BUNDLE_B64} <= 8 * 1024 * 1024 )) \
    || die "Encoded course bundle exceeds the 8 MiB cloud-init limit."
}

if [[ -n "$COURSE_IMAGE_DIR" ]]; then
  require_command jq
  COURSE_IMAGES_ROOT="$(realpath -e -- "${SCRIPT_DIR}/../course-images")"
  COURSE_IMAGE_DIR="$(realpath -e -- "$COURSE_IMAGE_DIR")"
  case "${COURSE_IMAGE_DIR}/" in
    "${COURSE_IMAGES_ROOT}/"*) ;;
    *) die "Course image directory must be below ${COURSE_IMAGES_ROOT}." ;;
  esac

  UNSAFE_PATH="$(find "$COURSE_IMAGE_DIR" -mindepth 1 \( -type l -o \( ! -type d ! -type f \) \) -print -quit)"
  [[ -z "$UNSAFE_PATH" ]] || die "Course bundle contains a symlink or special file: $UNSAFE_PATH"
  SECRET_PATH="$(find "$COURSE_IMAGE_DIR" -type f \( \
    -iname '.env*' -o -iname '*.pem' -o -iname '*.key' -o -iname '*.p12' -o \
    -iname '*.pfx' -o -iname 'id_rsa*' -o -iname 'id_ed25519*' -o \
    -iname '*credential*.json' -o -iname '*secret*.json' \
    \) -print -quit)"
  [[ -z "$SECRET_PATH" ]] || die "Refusing to embed a possible secret file: $SECRET_PATH"
  COURSE_BUNDLE_FILE_COUNT="$(find "$COURSE_IMAGE_DIR" -type f -printf '.\n' | wc -l)"
  (( COURSE_BUNDLE_FILE_COUNT <= 128 )) \
    || die "Course bundle exceeds the 128-file limit."
  COURSE_BUNDLE_BYTES="$(find "$COURSE_IMAGE_DIR" -type f -printf '%s\n' | awk '{total += $1} END {print total + 0}')"
  (( COURSE_BUNDLE_BYTES <= 5 * 1024 * 1024 )) \
    || die "Course bundle exceeds the 5 MiB uncompressed limit."

  COURSE_MANIFEST="${COURSE_IMAGE_DIR}/manifest.json"
  [[ -f "$COURSE_MANIFEST" ]] || die "Course manifest.json is required."
  [[ -f "${COURSE_IMAGE_DIR}/provision.sh" ]] || die "Course provision.sh is required."
  [[ -f "${COURSE_IMAGE_DIR}/bin/aivirteach-check-step" ]] \
    || die "Course checkpoint dispatcher is required."
  [[ -f "${COURSE_IMAGE_DIR}/bin/aivirteach-check-progress" ]] \
    || die "Course progress dispatcher is required."
  [[ -d "${COURSE_IMAGE_DIR}/checks" ]] || die "Course checks directory is required."
  compgen -G "${COURSE_IMAGE_DIR}/checks/*.sh" >/dev/null \
    || die "Course checks directory contains no shell checkers."

  COURSE_ID="$(jq -er '.courseId | strings' "$COURSE_MANIFEST")" \
    || die "manifest courseId is required."
  COURSE_SOURCE_DOCUMENT="$(jq -er '.sourceDocument | strings' "$COURSE_MANIFEST")" \
    || die "manifest sourceDocument is required."
  UBUNTU_RELEASE="$(jq -er '.image.ubuntuRelease | strings' "$COURSE_MANIFEST")" \
    || die "manifest image.ubuntuRelease is required."
  UBUNTU_CODENAME="$(jq -er '.image.ubuntuCodename | strings' "$COURSE_MANIFEST")" \
    || die "manifest image.ubuntuCodename is required."
  UBUNTU_IMAGE_NAME="$(jq -er '.image.ubuntuImageName | strings' "$COURSE_MANIFEST")" \
    || die "manifest image.ubuntuImageName is required."
  UBUNTU_IMAGE_BASE_URL="$(jq -er '.image.ubuntuImageBaseUrl | strings' "$COURSE_MANIFEST")" \
    || die "manifest image.ubuntuImageBaseUrl is required."
  GOLDEN_IMAGE_NAME="$(jq -er '.image.goldenImage | strings' "$COURSE_MANIFEST")" \
    || die "manifest image.goldenImage is required."
  GOLDEN_IMAGE_SIZE="$(jq -er '.image.goldenImageSize | strings' "$COURSE_MANIFEST")" \
    || die "manifest image.goldenImageSize is required."
  BUILDER_VM_NAME="$(jq -er '.image.builderVmName | strings' "$COURSE_MANIFEST")" \
    || die "manifest image.builderVmName is required."
  BUILDER_MEMORY_MB="$(jq -er '.image.builderMemoryMb | numbers' "$COURSE_MANIFEST")" \
    || die "manifest image.builderMemoryMb is required."
  BUILDER_VCPUS="$(jq -er '.image.builderVcpus | numbers' "$COURSE_MANIFEST")" \
    || die "manifest image.builderVcpus is required."
  BUILD_TIMEOUT_SECONDS="$(jq -er '.image.buildTimeoutSeconds | numbers' "$COURSE_MANIFEST")" \
    || die "manifest image.buildTimeoutSeconds is required."

  [[ "$COURSE_ID" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] \
    || die "manifest courseId is invalid."
  [[ "$COURSE_SOURCE_DOCUMENT" =~ ^[A-Za-z0-9][-A-Za-z0-9._\ ]*\.md$ ]] \
    || die "manifest sourceDocument must be a safe Markdown filename."
  [[ -f "${COURSE_IMAGE_DIR}/${COURSE_SOURCE_DOCUMENT}" ]] \
    || die "manifest sourceDocument is missing from the course bundle."
  [[ "$UBUNTU_RELEASE" =~ ^[0-9]{2}\.[0-9]{2}$ ]] \
    || die "manifest Ubuntu release is invalid."
  [[ "$UBUNTU_CODENAME" =~ ^[a-z][a-z0-9-]*$ ]] \
    || die "manifest Ubuntu codename is invalid."
  [[ "$UBUNTU_IMAGE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.(img|qcow2)$ ]] \
    || die "manifest Ubuntu image filename is invalid."
  [[ "$UBUNTU_IMAGE_BASE_URL" =~ ^https://cloud-images\.ubuntu\.com/[A-Za-z0-9._/-]+$ ]] \
    || die "manifest Ubuntu image URL must use the official HTTPS host."
  [[ "$GOLDEN_IMAGE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.qcow2$ ]] \
    || die "manifest golden image name must be a safe qcow2 filename."
  [[ "$GOLDEN_IMAGE_SIZE" =~ ^[1-9][0-9]{0,2}G$ ]] \
    || die "manifest golden image size must be 1G-999G."
  validate_lab_id "$BUILDER_VM_NAME"
  [[ "$BUILDER_MEMORY_MB" =~ ^[0-9]+$ ]] \
    && (( BUILDER_MEMORY_MB >= 1024 && BUILDER_MEMORY_MB <= 65536 )) \
    || die "manifest builder memory must be 1024-65536 MiB."
  [[ "$BUILDER_VCPUS" =~ ^[0-9]+$ ]] \
    && (( BUILDER_VCPUS >= 1 && BUILDER_VCPUS <= 32 )) \
    || die "manifest builder vCPUs must be 1-32."
  [[ "$BUILD_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] \
    && (( BUILD_TIMEOUT_SECONDS >= 300 && BUILD_TIMEOUT_SECONDS <= 7200 )) \
    || die "manifest build timeout must be 300-7200 seconds."
  [[ "$(jq -r '.image.os' "$COURSE_MANIFEST")" == "Ubuntu ${UBUNTU_RELEASE}" ]] \
    || die "manifest image.os and image.ubuntuRelease do not match."
  MISSING_ASSET_COUNT="$(jq -er '.missingRequiredCourseAssets | arrays | length' \
    "$COURSE_MANIFEST")" || die "manifest missingRequiredCourseAssets must be an array."
  COURSE_BUNDLE_STATUS="complete"
  if (( MISSING_ASSET_COUNT > 0 )); then
    COURSE_BUNDLE_STATUS="incomplete"
    MISSING_ASSETS="$(jq -r '.missingRequiredCourseAssets | join(", ")' "$COURSE_MANIFEST")"
    warn "Course manifest declares missing assets: ${MISSING_ASSETS}"
  fi
  [[ -z "$CUSTOMIZE_SCRIPT" ]] \
    || die "CUSTOMIZE_SCRIPT and --course-image-dir cannot be used together."
fi

if [[ "$VALIDATE_ONLY" == true ]]; then
  [[ -n "$COURSE_IMAGE_DIR" ]] \
    || die "--validate-only requires --course-image-dir."
  for cmd in tar stat sha256sum base64; do
    require_command "$cmd"
  done
  bash -n "${COURSE_IMAGE_DIR}/provision.sh"
  bash -n "${COURSE_IMAGE_DIR}/bin/aivirteach-check-step"
  bash -n "${COURSE_IMAGE_DIR}/bin/aivirteach-check-progress"
  while IFS= read -r checker; do
    bash -n "$checker"
  done < <(find "${COURSE_IMAGE_DIR}/checks" -type f -name '*.sh' -print)
  pack_course_bundle
  printf '%s\n' \
    "Course image bundle structure is valid." \
    "bundle_status=${COURSE_BUNDLE_STATUS}" \
    "course_id=${COURSE_ID}" \
    "golden_image=${GOLDEN_IMAGE_NAME}" \
    "files=${COURSE_BUNDLE_FILE_COUNT}" \
    "uncompressed_bytes=${COURSE_BUNDLE_BYTES}" \
    "declared_missing_assets=${MISSING_ASSET_COUNT}" \
    "bundle_sha256=${COURSE_BUNDLE_SHA256}"
  exit 0
fi

require_root_or_sudo
for cmd in curl qemu-img cloud-localds virt-install virsh openssl sha256sum base64 tar virt-cat; do
  require_command "$cmd"
done
[[ -f "$BROWSER_SMOKE_SOURCE" && ! -L "$BROWSER_SMOKE_SOURCE" ]] \
  || die "Browser smoke test must be a regular project-owned file: $BROWSER_SMOKE_SOURCE"
bash -n "$BROWSER_SMOKE_SOURCE"
BROWSER_SMOKE_B64="$(base64 -w0 "$BROWSER_SMOKE_SOURCE")"
ensure_storage_layout
ensure_default_network

WALLPAPER_B64=""
if [[ -n "${WALLPAPER_IMAGE:-}" ]]; then
  GENERAL_ASSETS_ROOT="$(realpath -e -- "${SCRIPT_DIR}/../general")"
  WALLPAPER_IMAGE="$(realpath -e -- "$WALLPAPER_IMAGE")"
  case "${WALLPAPER_IMAGE}" in
    "${GENERAL_ASSETS_ROOT}/"*) ;;
    *) die "Wallpaper must be below ${GENERAL_ASSETS_ROOT}." ;;
  esac
  [[ -f "$WALLPAPER_IMAGE" && ! -L "$WALLPAPER_IMAGE" ]] \
    || die "Wallpaper must be a regular file, not a symlink."
  [[ "$WALLPAPER_IMAGE" =~ \.(jpg|jpeg|png)$ ]] \
    || die "Wallpaper must use a .jpg, .jpeg, or .png extension."
  WALLPAPER_BYTES="$(stat -c '%s' "$WALLPAPER_IMAGE")"
  (( WALLPAPER_BYTES <= 6 * 1024 * 1024 )) \
    || die "Wallpaper exceeds the 6 MiB cloud-init limit."
  WALLPAPER_B64="$(base64 -w0 "$WALLPAPER_IMAGE")"
  (( ${#WALLPAPER_B64} <= 8 * 1024 * 1024 )) \
    || die "Encoded wallpaper exceeds the 8 MiB cloud-init limit."
fi

SOURCE_IMAGE="${BASE_DIR}/${UBUNTU_IMAGE_NAME}"
CHECKSUM_FILE="${BASE_DIR}/SHA256SUMS-${UBUNTU_CODENAME}"
BUILDER_DISK="${BASE_DIR}/${BUILDER_VM_NAME}.qcow2"
FINAL_IMAGE="${BASE_DIR}/${GOLDEN_IMAGE_NAME}"
BUILD_DIR="${SEEDS_DIR}/${BUILDER_VM_NAME}"
SEED_ISO="${BUILD_DIR}/seed.iso"
USER_DATA="${BUILD_DIR}/user-data.yaml"
META_DATA="${BUILD_DIR}/meta-data.yaml"

if [[ -e "$FINAL_IMAGE" && "$FORCE" != true ]]; then
  die "Golden image exists: $FINAL_IMAGE. Use --force to replace it."
fi

if [[ -e "$FINAL_IMAGE" && "$FORCE" == true ]] \
    && compgen -G "${LABS_DIR}/*.qcow2" >/dev/null; then
  die "Refusing to replace the golden image while learner overlays exist in $LABS_DIR."
fi

if as_root virsh --connect qemu:///system dominfo "$BUILDER_VM_NAME" >/dev/null 2>&1; then
  [[ "$FORCE" == true ]] || die "Builder domain already exists: $BUILDER_VM_NAME"
  as_root virsh --connect qemu:///system destroy "$BUILDER_VM_NAME" >/dev/null 2>&1 || true
  as_root virsh --connect qemu:///system undefine "$BUILDER_VM_NAME" --nvram >/dev/null 2>&1 \
    || as_root virsh --connect qemu:///system undefine "$BUILDER_VM_NAME" >/dev/null 2>&1 \
    || true
fi

if [[ "$FORCE" == true ]]; then
  as_root rm -f "$FINAL_IMAGE" "$BUILDER_DISK"
  as_root rm -rf "$BUILD_DIR"
fi

log "Refreshing Ubuntu ${UBUNTU_RELEASE} checksum metadata..."
if as_root curl -fL --retry 4 --retry-delay 3 -o "${CHECKSUM_FILE}.tmp" \
  "${UBUNTU_IMAGE_BASE_URL}/SHA256SUMS"; then
  as_root mv "${CHECKSUM_FILE}.tmp" "$CHECKSUM_FILE"
else
  as_root rm -f "${CHECKSUM_FILE}.tmp"
  if [[ "$REFRESH_SOURCE" == true || ! -f "$CHECKSUM_FILE" ]]; then
    die "Unable to refresh Ubuntu checksums and no usable cached checksum file exists."
  fi
  warn "Checksum refresh failed; using cached metadata: ${CHECKSUM_FILE}"
fi

EXPECTED_LINE="$(grep -m1 -E "[ *]${UBUNTU_IMAGE_NAME}$" "$CHECKSUM_FILE" || true)"
[[ -n "$EXPECTED_LINE" ]] || die "Image not found in SHA256SUMS."
EXPECTED_HASH="$(awk '{print $1}' <<< "$EXPECTED_LINE")"

SOURCE_IMAGE_VALID=false
if [[ "$REFRESH_SOURCE" != true && -f "$SOURCE_IMAGE" ]]; then
  CACHED_HASH="$(as_root sha256sum "$SOURCE_IMAGE" | awk '{print $1}')"
  if [[ "$CACHED_HASH" == "$EXPECTED_HASH" ]]; then
    SOURCE_IMAGE_VALID=true
    log "Using cached, checksum-verified Ubuntu image: ${SOURCE_IMAGE}"
  else
    warn "Cached Ubuntu image checksum changed or is invalid; downloading a fresh copy."
  fi
fi

if [[ "$SOURCE_IMAGE_VALID" != true ]]; then
  log "Downloading Ubuntu ${UBUNTU_RELEASE} released cloud image..."
  as_root curl -fL --retry 4 --retry-delay 3 -o "${SOURCE_IMAGE}.tmp" \
    "${UBUNTU_IMAGE_BASE_URL}/${UBUNTU_IMAGE_NAME}"
  DOWNLOADED_HASH="$(as_root sha256sum "${SOURCE_IMAGE}.tmp" | awk '{print $1}')"
  if [[ "$DOWNLOADED_HASH" != "$EXPECTED_HASH" ]]; then
    as_root rm -f "${SOURCE_IMAGE}.tmp"
    die "Downloaded Ubuntu image checksum mismatch."
  fi
  as_root mv "${SOURCE_IMAGE}.tmp" "$SOURCE_IMAGE"
fi

as_root qemu-img convert -p -O qcow2 "$SOURCE_IMAGE" "$BUILDER_DISK"
as_root qemu-img resize "$BUILDER_DISK" "$GOLDEN_IMAGE_SIZE"
as_root chown libvirt-qemu:kvm "$BUILDER_DISK"
as_root chmod 0660 "$BUILDER_DISK"

as_root install -d -m 0770 -o root -g kvm "$BUILD_DIR"
command -v setfacl >/dev/null 2>&1 && as_root setfacl -m u:libvirt-qemu:rwx,m:rwx "$BUILD_DIR"

BUILDER_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 20)"
BUILDER_PASSWORD_HASH="$(openssl passwd -6 "$BUILDER_PASSWORD")"
as_root bash -c "umask 077; printf '%s\n' '$BUILDER_PASSWORD' > '$BUILD_DIR/builder-password.txt'"

CUSTOM_B64=""
if [[ -n "$CUSTOMIZE_SCRIPT" ]]; then
  [[ -f "$CUSTOMIZE_SCRIPT" ]] || die "CUSTOMIZE_SCRIPT not found: $CUSTOMIZE_SCRIPT"
  CUSTOM_B64="$(base64 -w0 "$CUSTOMIZE_SCRIPT")"
fi

if [[ -n "$COURSE_IMAGE_DIR" ]]; then
  pack_course_bundle
fi

BUILD_SCRIPT_TMP="$(mktemp)"
TEMP_FILES+=("$BUILD_SCRIPT_TMP")
cat > "$BUILD_SCRIPT_TMP" <<'BUILDSTART'
#!/usr/bin/env bash
set -Eeuo pipefail

remove_publish_markers() {
  rm -f \
    /etc/aivirteach/browser-ready \
    /etc/aivirteach/browser-ready.tmp \
    /etc/aivirteach/image-ready \
    /etc/aivirteach/image-ready.tmp \
    /etc/aivirteach/course-image-ready \
    /etc/aivirteach/course-image-ready.tmp
}
trap remove_publish_markers ERR
remove_publish_markers

echo 'lightdm shared/default-x-display-manager select lightdm' \
  | debconf-set-selections || true
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  xfce4 xfce4-goodies xrdp xorgxrdp dbus-x11 qemu-guest-agent \
  openssh-server ca-certificates curl git gnupg vim wget \
  python3 python3-venv python3-pip

install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg \
  -o /etc/apt/keyrings/packages.mozilla.org.asc
chmod 0644 /etc/apt/keyrings/packages.mozilla.org.asc
mozilla_key_fingerprint="$(
  gpg --batch --quiet --show-keys --with-colons \
    /etc/apt/keyrings/packages.mozilla.org.asc \
    | awk -F: '$1 == "fpr" {print toupper($10); exit}'
)"
[[ "$mozilla_key_fingerprint" == "35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3" ]] \
  || {
    echo "Mozilla repository signing-key fingerprint mismatch." >&2
    exit 1
  }

cat > /etc/apt/sources.list.d/mozilla.list <<'MOZILLAREPO'
deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main
MOZILLAREPO
cat > /etc/apt/preferences.d/mozilla <<'MOZILLAPIN'
Package: *
Pin: origin packages.mozilla.org
Pin-Priority: 1000

Package: firefox
Pin: release o=Ubuntu
Pin-Priority: -1
MOZILLAPIN

apt-get update
if command -v snap >/dev/null 2>&1 && snap list firefox >/dev/null 2>&1; then
  snap remove firefox
fi
DEBIAN_FRONTEND=noninteractive apt-get install -y firefox
update-alternatives --install \
  /usr/bin/x-www-browser x-www-browser /usr/bin/firefox 200
update-alternatives --set x-www-browser /usr/bin/firefox

install -d -m 0755 /etc/xdg/xfce4
touch /etc/xdg/xfce4/helpers.rc
sed -i '/^WebBrowser=/d' /etc/xdg/xfce4/helpers.rc
printf '%s\n' 'WebBrowser=firefox' >> /etc/xdg/xfce4/helpers.rc
cat > /etc/xdg/mimeapps.list <<'MIMEAPPS'
[Default Applications]
text/html=firefox.desktop
x-scheme-handler/http=firefox.desktop
x-scheme-handler/https=firefox.desktop
MIMEAPPS

install -d -m 0755 /etc/aivirteach /opt/aivirteach /home/learner/course
printf '%s\n' \
  'unset DBUS_SESSION_BUS_ADDRESS' \
  'unset XDG_RUNTIME_DIR' \
  'exec startxfce4' > /home/learner/.xsession
chown -R learner:learner /home/learner
adduser xrdp ssl-cert || true
systemctl enable qemu-guest-agent ssh xrdp aivirteach-firstboot.service
BUILDSTART

if [[ -n "$COURSE_BUNDLE_B64" ]]; then
  cat >> "$BUILD_SCRIPT_TMP" <<COURSEBUILD
printf '%s  %s\n' \
  '${COURSE_BUNDLE_SHA256}' '/var/tmp/aivirteach-course-image.tar.gz' \
  | sha256sum --check -
rm -rf /opt/aivirteach/course-image
install -d -m 0755 /opt/aivirteach/course-image
tar --no-same-owner -xzf /var/tmp/aivirteach-course-image.tar.gz \
  -C /opt/aivirteach/course-image
bash /opt/aivirteach/course-image/provision.sh
[[ "\$(cat /etc/aivirteach/course-id)" == '${COURSE_ID}' ]]
rm -rf /opt/aivirteach/course-image
COURSEBUILD
fi

if [[ -n "$WALLPAPER_B64" ]]; then
  cat >> "$BUILD_SCRIPT_TMP" <<'WALLPAPERBUILD'
install -d -m 0755 /usr/share/backgrounds/aivirteach /usr/local/lib/aivirteach
install -m 0644 /var/tmp/aivirteach-wallpaper \
  /usr/share/backgrounds/aivirteach/wallpaper.jpg

cat > /usr/local/lib/aivirteach/set-wallpaper.sh <<'SETWALLPAPER'
#!/usr/bin/env bash
set -euo pipefail

wallpaper="/usr/share/backgrounds/aivirteach/wallpaper.jpg"
marker="${HOME}/.config/aivirteach/wallpaper-v3-prefixes"
[[ -f "$wallpaper" ]] || exit 0

set_scalar_property() {
  local property="$1"
  local type="$2"
  local value="$3"
  if xfconf-query -c xfce4-desktop -p "$property" >/dev/null 2>&1; then
    xfconf-query -c xfce4-desktop -p "$property" -s "$value"
  else
    xfconf-query -c xfce4-desktop -p "$property" -n -t "$type" -s "$value"
  fi
}

set_white_rgba() {
  local property="$1"
  xfconf-query -c xfce4-desktop -p "$property" -r >/dev/null 2>&1 || true
  xfconf-query -c xfce4-desktop -p "$property" -n -a \
    -t double -s 1 \
    -t double -s 1 \
    -t double -s 1 \
    -t double -s 1
}

for _attempt in 1 2 3 4 5; do
  image_properties="$(
    xfconf-query -c xfce4-desktop -l 2>/dev/null \
      | grep -E '/(last-image|image-path)$' || true
  )"
  if [[ -n "$image_properties" ]]; then
    declare -A configured_backdrops=()
    while IFS= read -r property; do
      [[ -n "$property" ]] || continue
      backdrop="${property%/*}"
      [[ -z "${configured_backdrops[$backdrop]+configured}" ]] || continue
      configured_backdrops["$backdrop"]=1
      grep -Fxq -- "$backdrop" "$marker" 2>/dev/null && continue
      set_scalar_property "$property" string "$wallpaper"
      set_scalar_property "${backdrop}/image-style" int 4
      set_scalar_property "${backdrop}/color-style" int 0
      set_white_rgba "${backdrop}/rgba1"
      set_white_rgba "${backdrop}/rgba2"
      set_scalar_property "${backdrop}/backdrop-cycle-enable" bool false
      mkdir -p "$(dirname "$marker")"
      printf '%s\n' "$backdrop" >> "$marker"
    done <<< "$image_properties"
    [[ ! -f "$marker" ]] || sort -u -o "$marker" "$marker"
    xfdesktop --reload >/dev/null 2>&1 || true
    exit 0
  fi
  sleep 2
done
SETWALLPAPER
chmod 0755 /usr/local/lib/aivirteach/set-wallpaper.sh

# Seed the common XRDP and virtio monitor names so the first desktop frame
# already uses the branded background. The autostart script above handles any
# runtime monitor/workspace names that XFCE creates later.
install -d -m 0755 /etc/xdg/xfce4/xfconf/xfce-perchannel-xml
cat > /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml <<'XFCEDEFAULTS'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-desktop" version="1.0">
  <property name="backdrop" type="empty">
    <property name="screen0" type="empty">
      <property name="monitorrdp0" type="empty">
        <property name="workspace0" type="empty">
          <property name="color-style" type="int" value="0"/>
          <property name="image-style" type="int" value="4"/>
          <property name="last-image" type="string" value="/usr/share/backgrounds/aivirteach/wallpaper.jpg"/>
          <property name="rgba1" type="array">
            <value type="double" value="1"/>
            <value type="double" value="1"/>
            <value type="double" value="1"/>
            <value type="double" value="1"/>
          </property>
          <property name="rgba2" type="array">
            <value type="double" value="1"/>
            <value type="double" value="1"/>
            <value type="double" value="1"/>
            <value type="double" value="1"/>
          </property>
          <property name="backdrop-cycle-enable" type="bool" value="false"/>
        </property>
      </property>
      <property name="monitorVirtual-1" type="empty">
        <property name="workspace0" type="empty">
          <property name="color-style" type="int" value="0"/>
          <property name="image-style" type="int" value="4"/>
          <property name="last-image" type="string" value="/usr/share/backgrounds/aivirteach/wallpaper.jpg"/>
          <property name="rgba1" type="array">
            <value type="double" value="1"/>
            <value type="double" value="1"/>
            <value type="double" value="1"/>
            <value type="double" value="1"/>
          </property>
          <property name="rgba2" type="array">
            <value type="double" value="1"/>
            <value type="double" value="1"/>
            <value type="double" value="1"/>
            <value type="double" value="1"/>
          </property>
          <property name="backdrop-cycle-enable" type="bool" value="false"/>
        </property>
      </property>
    </property>
  </property>
</channel>
XFCEDEFAULTS
chmod 0644 /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml

install -d -m 0755 /etc/xdg/autostart
cat > /etc/xdg/autostart/aivirteach-wallpaper.desktop <<'WALLPAPERDESKTOP'
[Desktop Entry]
Type=Application
Name=AIVirTeach Wallpaper
Exec=/usr/local/lib/aivirteach/set-wallpaper.sh
OnlyShowIn=XFCE;
X-GNOME-Autostart-enabled=true
Terminal=false
WALLPAPERDESKTOP
chmod 0644 /etc/xdg/autostart/aivirteach-wallpaper.desktop

install -d -m 0755 /etc/lightdm/lightdm-gtk-greeter.conf.d
cat > /etc/lightdm/lightdm-gtk-greeter.conf.d/50-aivirteach-wallpaper.conf <<'LIGHTDMBACKGROUND'
[greeter]
background=/usr/share/backgrounds/aivirteach/wallpaper.jpg
LIGHTDMBACKGROUND
chmod 0644 /etc/lightdm/lightdm-gtk-greeter.conf.d/50-aivirteach-wallpaper.conf
WALLPAPERBUILD
fi

cat >> "$BUILD_SCRIPT_TMP" <<'BUILDCLEAN'
if [[ -x /usr/local/sbin/aivirteach-customize ]]; then
  /usr/local/sbin/aivirteach-customize
fi

browser_binary="$(command -v firefox || true)"
[[ "$browser_binary" == "/usr/bin/firefox" ]]
browser_realpath="$(readlink -f "$browser_binary")"
[[ "$browser_realpath" != /snap/* ]]
[[ "$(dpkg-query -W -f='${Status}' firefox 2>/dev/null)" == "install ok installed" ]]
if command -v snap >/dev/null 2>&1; then
  ! snap list firefox >/dev/null 2>&1
fi
firefox_policy="$(apt-cache policy firefox)"
grep -Fq 'https://packages.mozilla.org/apt' <<< "$firefox_policy"
[[ "$(readlink -f /etc/alternatives/x-www-browser)" == "$browser_realpath" ]]
[[ -x /usr/local/bin/aivirteach-browser-smoke-test ]]
bash -n /usr/local/bin/aivirteach-browser-smoke-test
browser_version="$(dpkg-query -W -f='${Version}' firefox)"
/usr/bin/firefox --version
printf '%s\n' \
  'browser=firefox' \
  'packaging=deb' \
  "version=${browser_version}" \
  'binary=/usr/bin/firefox' \
  'source=packages.mozilla.org' \
  > /etc/aivirteach/browser-ready.tmp
chmod 0444 /etc/aivirteach/browser-ready.tmp
mv /etc/aivirteach/browser-ready.tmp /etc/aivirteach/browser-ready

passwd -l learner || true
apt-get clean
rm -rf /tmp/* /var/tmp/* /var/lib/apt/lists/*
rm -f /etc/ssh/ssh_host_*
userdel -r builder || true
rm -f /var/lib/aivirteach/firstboot-complete
cloud-init clean --logs --machine-id
rm -f /usr/local/sbin/aivirteach-customize /usr/local/sbin/aivirteach-build-image
BUILDCLEAN

if [[ -n "$COURSE_BUNDLE_B64" ]]; then
  cat >> "$BUILD_SCRIPT_TMP" <<COURSEMARKER
printf '%s %s\n' '${COURSE_ID}' '${COURSE_BUNDLE_SHA256}' \
  > /etc/aivirteach/course-image-ready.tmp
chmod 0444 /etc/aivirteach/course-image-ready.tmp
mv /etc/aivirteach/course-image-ready.tmp /etc/aivirteach/course-image-ready
COURSEMARKER
fi

cat >> "$BUILD_SCRIPT_TMP" <<BUILDMARKER
printf '%s\n' '${GOLDEN_IMAGE_NAME}' > /etc/aivirteach/image-ready.tmp
chmod 0444 /etc/aivirteach/image-ready.tmp
mv /etc/aivirteach/image-ready.tmp /etc/aivirteach/image-ready
trap - ERR
BUILDMARKER

bash -n "$BUILD_SCRIPT_TMP"
BUILD_SCRIPT_B64="$(base64 -w0 "$BUILD_SCRIPT_TMP")"

USER_DATA_TMP="$(mktemp)"
TEMP_FILES+=("$USER_DATA_TMP")
cat > "$USER_DATA_TMP" <<CLOUDCFG
#cloud-config
hostname: ${BUILDER_VM_NAME}
manage_etc_hosts: true
ssh_pwauth: true
disable_root: true

users:
  - default
  - name: builder
    shell: /bin/bash
    groups: [adm, sudo]
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    lock_passwd: false
    hashed_passwd: "${BUILDER_PASSWORD_HASH}"
  - name: learner
    shell: /bin/bash
    groups: [adm, sudo, video, render]
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    lock_passwd: true

write_files:
  - path: /etc/aivirteach/image-version
    owner: root:root
    permissions: "0644"
    content: |
      ${GOLDEN_IMAGE_NAME}
  - path: /usr/local/sbin/aivirteach-firstboot
    owner: root:root
    permissions: "0755"
    content: |
      #!/usr/bin/env bash
      set -e
      ssh-keygen -A
      install -d -m 0755 /run/aivirteach
      systemctl restart ssh || true
      systemctl restart qemu-guest-agent || true
      systemctl restart xrdp || true
  - path: /etc/systemd/system/aivirteach-firstboot.service
    owner: root:root
    permissions: "0644"
    content: |
      [Unit]
      Description=AIVirTeach cloned VM first-boot preparation
      After=network.target
      ConditionPathExists=!/var/lib/aivirteach/firstboot-complete

      [Service]
      Type=oneshot
      ExecStart=/usr/local/sbin/aivirteach-firstboot
      ExecStartPost=/usr/bin/install -D -m 0644 /dev/null /var/lib/aivirteach/firstboot-complete
      RemainAfterExit=yes

      [Install]
      WantedBy=multi-user.target
  - path: /usr/local/sbin/aivirteach-build-image
    owner: root:root
    permissions: "0700"
    encoding: b64
    content: ${BUILD_SCRIPT_B64}
  - path: /usr/local/bin/aivirteach-browser-smoke-test
    owner: root:root
    permissions: "0755"
    encoding: b64
    content: ${BROWSER_SMOKE_B64}
CLOUDCFG

if [[ -n "$CUSTOM_B64" ]]; then
  cat >> "$USER_DATA_TMP" <<CUSTOMCFG
  - path: /usr/local/sbin/aivirteach-customize
    owner: root:root
    permissions: "0755"
    encoding: b64
    content: ${CUSTOM_B64}
CUSTOMCFG
fi

if [[ -n "$WALLPAPER_B64" ]]; then
  cat >> "$USER_DATA_TMP" <<WALLPAPERCFG
  - path: /var/tmp/aivirteach-wallpaper
    owner: root:root
    permissions: "0600"
    encoding: b64
    content: ${WALLPAPER_B64}
WALLPAPERCFG
fi

if [[ -n "$COURSE_BUNDLE_B64" ]]; then
  cat >> "$USER_DATA_TMP" <<COURSECFG
  - path: /var/tmp/aivirteach-course-image.tar.gz
    owner: root:root
    permissions: "0600"
    encoding: b64
    content: ${COURSE_BUNDLE_B64}
COURSECFG
fi

cat >> "$USER_DATA_TMP" <<'CLOUDEND'

runcmd:
  - [bash, /usr/local/sbin/aivirteach-build-image]

power_state:
  delay: now
  mode: poweroff
  message: AIVirTeach golden image build completed
  timeout: 60
  condition: true
CLOUDEND

as_root mv "$USER_DATA_TMP" "$USER_DATA"
as_root chown root:kvm "$USER_DATA"
as_root chmod 0640 "$USER_DATA"

META_DATA_TMP="$(mktemp)"
TEMP_FILES+=("$META_DATA_TMP")
cat > "$META_DATA_TMP" <<METACFG
instance-id: ${BUILDER_VM_NAME}-$(date +%s)
local-hostname: ${BUILDER_VM_NAME}
METACFG
as_root mv "$META_DATA_TMP" "$META_DATA"
as_root chown root:kvm "$META_DATA"
as_root chmod 0640 "$META_DATA"

as_root cloud-localds "$SEED_ISO" "$USER_DATA" "$META_DATA"
as_root chown libvirt-qemu:kvm "$SEED_ISO"
as_root chmod 0640 "$SEED_ISO"

OSINFO="$(choose_osinfo "$UBUNTU_RELEASE")"
as_root virt-install \
  --connect qemu:///system \
  --name "$BUILDER_VM_NAME" \
  --memory "$BUILDER_MEMORY_MB" \
  --vcpus "$BUILDER_VCPUS" \
  --cpu host-passthrough \
  --osinfo "$OSINFO" \
  --import \
  --disk "path=${BUILDER_DISK},format=qcow2,bus=virtio" \
  --disk "path=${SEED_ISO},device=cdrom,readonly=on" \
  --network "network=${LIBVIRT_NETWORK},model=virtio" \
  --graphics vnc,listen=127.0.0.1 \
  --video virtio \
  --channel unix,target.type=virtio,target.name=org.qemu.guest_agent.0 \
  --noautoconsole

cat <<BUILDINFO
Builder started. Temporary login: builder / $BUILDER_PASSWORD
VNC: sudo virsh vncdisplay $BUILDER_VM_NAME
BUILDINFO

START_TIME="$(date +%s)"
while true; do
  STATE="$(as_root virsh --connect qemu:///system domstate "$BUILDER_VM_NAME" 2>/dev/null | tr -d '\r' || true)"
  [[ "$STATE" == "shut off" ]] && break
  NOW="$(date +%s)"
  if (( NOW - START_TIME > BUILD_TIMEOUT_SECONDS )); then
    die "Builder timed out. Inspect with sudo virsh console $BUILDER_VM_NAME"
  fi
  sleep 10
done

IMAGE_READY="$(as_root virt-cat -c qemu:///system -d "$BUILDER_VM_NAME" \
  /etc/aivirteach/image-ready 2>/dev/null || true)"
[[ "$IMAGE_READY" == "$GOLDEN_IMAGE_NAME" ]] \
  || die "Golden image completion marker is missing or invalid; refusing to publish it."
BROWSER_READY="$(as_root virt-cat -c qemu:///system -d "$BUILDER_VM_NAME" \
  /etc/aivirteach/browser-ready 2>/dev/null || true)"
grep -Fxq 'browser=firefox' <<< "$BROWSER_READY" \
  && grep -Fxq 'packaging=deb' <<< "$BROWSER_READY" \
  && grep -Fxq 'binary=/usr/bin/firefox' <<< "$BROWSER_READY" \
  && grep -Fxq 'source=packages.mozilla.org' <<< "$BROWSER_READY" \
  || die "Browser completion marker is missing or invalid; refusing to publish the image."
if [[ -n "$COURSE_ID" ]]; then
  COURSE_READY="$(as_root virt-cat -c qemu:///system -d "$BUILDER_VM_NAME" \
    /etc/aivirteach/course-image-ready 2>/dev/null || true)"
  [[ "$COURSE_READY" == "$COURSE_ID $COURSE_BUNDLE_SHA256" ]] \
    || die "Course provisioning marker is missing or invalid; refusing to publish the image."
fi

as_root virsh --connect qemu:///system undefine "$BUILDER_VM_NAME" --nvram >/dev/null 2>&1 \
  || as_root virsh --connect qemu:///system undefine "$BUILDER_VM_NAME" >/dev/null
as_root qemu-img check "$BUILDER_DISK"
as_root mv "$BUILDER_DISK" "$FINAL_IMAGE"
as_root chown root:root "$FINAL_IMAGE"
as_root chmod 0444 "$FINAL_IMAGE"
command -v setfacl >/dev/null 2>&1 && as_root setfacl -m u:libvirt-qemu:r-- "$FINAL_IMAGE"
as_root rm -rf "$BUILD_DIR"

printf '\nGolden image created: %s\n' "$FINAL_IMAGE"
as_root qemu-img info "$FINAL_IMAGE"
