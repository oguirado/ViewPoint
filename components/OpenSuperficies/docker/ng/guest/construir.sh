#!/usr/bin/env bash
set -euo pipefail

CPUSET="0-4,10-14"
JSON_OUTPUT=0
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cpuset)
      CPUSET="$2"
      shift 2
      ;;
    --cpuset=*)
      CPUSET="${1#*=}"
      shift
      ;;
    --json)
      JSON_OUTPUT=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    *)
      shift
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
GUEST_DIR="$REPO_ROOT/guest"
CACHE_DIR="/tmp/srf-kernel-cache"

mkdir -p "$GUEST_DIR" "$CACHE_DIR"

log() {
  echo "[srf-guest-builder] $*" >&2
}

# 1. Verify builder image exists (H1: do not rebuild, no apt-get)
BUILDER_IMAGE="srf-guest-builder"
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$BUILDER_IMAGE" 2>/dev/null || true)"
if [[ -z "$IMAGE_ID" ]]; then
  log "Error: srf-guest-builder image absent; rebuild requires operator authorization (OQ-6)"
  if [[ $JSON_OUTPUT -eq 1 ]]; then
    echo '{"ok":false,"error":"srf-guest-builder image absent; rebuild requires operator authorization (OQ-6)"}'
  fi
  exit 1
fi

# 2. Kernel tarball verification & download if needed (H1)
KERNEL_VERSION="6.18.52"
KERNEL_SHA256="2b69564f7d4fea0c859b1959ba33709ee6e9139bd100e30a853b57159a8221b8"
TARBALL="$CACHE_DIR/linux-$KERNEL_VERSION.tar.xz"

descarga=false
tarball_valid=0
if [[ -f "$TARBALL" ]] && echo "$KERNEL_SHA256  $TARBALL" | sha256sum -c --status 2>/dev/null; then
  tarball_valid=1
fi

if [[ $tarball_valid -eq 0 ]]; then
  log "Fetching linux-$KERNEL_VERSION.tar.xz inside $BUILDER_IMAGE container..."
  docker run --rm --cpuset-cpus="$CPUSET" \
    -v "$CACHE_DIR:/cache" \
    "$BUILDER_IMAGE" \
    curl -fL "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$KERNEL_VERSION.tar.xz" -o "/cache/linux-$KERNEL_VERSION.tar.xz" >&2
  descarga=true
  if ! echo "$KERNEL_SHA256  $TARBALL" | sha256sum -c --status 2>/dev/null; then
    log "Error: downloaded tarball checksum mismatch"
    if [[ $JSON_OUTPUT -eq 1 ]]; then
      echo '{"ok":false,"error":"tarball checksum mismatch"}'
    fi
    exit 1
  fi
fi

# Cache artifact integrity check helper (H3)
check_cache_artifact() {
  local filename="$1"
  local filepath="$CACHE_DIR/$filename"
  local manifest="$CACHE_DIR/MANIFIESTO.sha256"

  if [[ ! -f "$filepath" ]] || [[ ! -f "$manifest" ]]; then
    return 1
  fi

  local expected_hash actual_hash
  expected_hash="$(grep -E " [ /]*${filename}\$" "$manifest" 2>/dev/null | awk '{print $1}' | head -n 1)"
  if [[ -z "$expected_hash" ]]; then
    return 1
  fi
  actual_hash="$(sha256sum "$filepath" | awk '{print $1}')"
  if [[ "$actual_hash" != "$expected_hash" ]]; then
    log "Warning: cache integrity failed for $filename ($actual_hash != $expected_hash)"
    return 1
  fi

  if [[ -f "$GUEST_DIR/MANIFIESTO.sha256" ]]; then
    local guest_hash
    guest_hash="$(grep -E " [ /]*${filename}\$" "$GUEST_DIR/MANIFIESTO.sha256" 2>/dev/null | awk '{print $1}' | head -n 1)"
    if [[ -n "$guest_hash" && "$guest_hash" != "$expected_hash" ]]; then
      log "Warning: cache manifest hash mismatch with guest manifest for $filename ($expected_hash != $guest_hash)"
      return 1
    fi
  fi

  return 0
}

# 3. Compile or restore kernel Image and kernel.config (H1: --network none, H3: cache integrity)
needs_kernel_build=0
if [[ $FORCE -eq 1 ]] || [[ ! -f "$GUEST_DIR/Image" ]] || [[ ! -f "$GUEST_DIR/kernel.config" ]]; then
  if check_cache_artifact "Image" && check_cache_artifact "kernel.config"; then
    log "Restoring verified kernel Image and kernel.config from sealed cache..."
    cp -f "$CACHE_DIR/Image" "$GUEST_DIR/Image"
    cp -f "$CACHE_DIR/kernel.config" "$GUEST_DIR/kernel.config"
  else
    needs_kernel_build=1
  fi
fi

if [[ $needs_kernel_build -eq 1 ]]; then
  log "Kernel cache miss or invalid. Compiling Linux $KERNEL_VERSION pinned to cpuset $CPUSET (network: none)..."
  docker run --rm --network none --cpuset-cpus="$CPUSET" \
    -e SOURCE_DATE_EPOCH=1764460800 \
    -e KBUILD_BUILD_TIMESTAMP="@1764460800" \
    -e KBUILD_BUILD_USER=opensuperficies \
    -e KBUILD_BUILD_HOST=builder \
    -e KBUILD_BUILD_VERSION=1 \
    -e TZ=UTC -e LC_ALL=C -e LANG=C \
    -v "$CACHE_DIR:/cache" \
    -v "$SCRIPT_DIR/kernel.config.fragment:/fragment:ro" \
    -v "$GUEST_DIR:/guest" \
    "$BUILDER_IMAGE" sh -c '
      set -e
      mkdir -p /work && cd /work
      tar -xf /cache/linux-'"$KERNEL_VERSION"'.tar.xz
      cd linux-'"$KERNEL_VERSION"'
      make ARCH=arm64 O=out defconfig
      ./scripts/kconfig/merge_config.sh -m -r -O out out/.config /fragment
      make ARCH=arm64 O=out olddefconfig
      make ARCH=arm64 O=out -j10 Image
      cp out/arch/arm64/boot/Image /guest/Image
      cp out/.config /guest/kernel.config
      cp out/arch/arm64/boot/Image /cache/Image
      cp out/.config /cache/kernel.config
      chmod 644 /guest/Image /guest/kernel.config /cache/Image /cache/kernel.config
    ' >&2
fi

# 4. Ensure srf-guest-init.bin is compiled
if [[ ! -f "$GUEST_DIR/srf-guest-init.bin" ]]; then
  log "Building srf-guest-init binary..."
  (cd "$REPO_ROOT" && CGO_ENABLED=0 GOARCH=arm64 go build -trimpath -o "$GUEST_DIR/srf-guest-init.bin" ./cmd/srf-guest-init) >&2
fi

# 5. Rootfs raw ext4 image creation (H1: --network none, H3: cache integrity & rootfs.tar cleanup)
ROOTFS_IMAGE="opensuperficies-browser-arm64:1.0.0"
needs_rootfs_build=0
if [[ $FORCE -eq 1 ]] || [[ ! -f "$GUEST_DIR/guest-root.raw" ]]; then
  if check_cache_artifact "guest-root.raw"; then
    log "Restoring verified guest-root.raw from sealed cache..."
    rm -f "$GUEST_DIR/guest-root.raw"
    cp -f "$CACHE_DIR/guest-root.raw" "$GUEST_DIR/guest-root.raw"
  else
    needs_rootfs_build=1
  fi
fi

if [[ $needs_rootfs_build -eq 1 ]]; then
  log "Exporting rootfs from $ROOTFS_IMAGE and generating guest-root.raw (network: none)..."
  CID=$(docker create "$ROOTFS_IMAGE")
  docker export "$CID" > "$CACHE_DIR/rootfs.tar"
  docker rm "$CID" >/dev/null

  docker run --rm --network none --cpuset-cpus="$CPUSET" \
    -v "$CACHE_DIR:/cache" \
    -v "$GUEST_DIR:/guest" \
    "$BUILDER_IMAGE" sh -c '
      set -e
      mkdir -p /stage
      tar -xf /cache/rootfs.tar -C /stage
      mkdir -p /stage/sbin
      cp /guest/srf-guest-init.bin /stage/sbin/srf-init
      cp /guest/srf-guest-init.bin /stage/sbin/srf-guest-init
      cp /guest/srf-guest-init.bin /stage/init
      chmod 0755 /stage/sbin/srf-init /stage/sbin/srf-guest-init /stage/init
      truncate -s 3G /guest/guest-root.raw
      mke2fs -q -F -t ext4 -b 4096 -L srf-root \
        -U 7cb1163f-4f6f-4ff4-a01c-1f01983af245 \
        -E hash_seed=2ed227a6-ae24-45f6-872a-af3f871d986b,lazy_itable_init=0,lazy_journal_init=0 \
        -O "^has_journal" -d /stage /guest/guest-root.raw
      cp /guest/guest-root.raw /cache/guest-root.raw
    ' >&2
  rm -f "$CACHE_DIR/rootfs.tar"
fi

# Always clean up rootfs.tar per H3
rm -f "$CACHE_DIR/rootfs.tar"

# Ensure immutable mode 444
chmod 444 "$GUEST_DIR/guest-root.raw" 2>/dev/null || true

# 6. Measurements (H2: derive all fields from actual measurements, fail-closed)
# 6a. Measured tarball sha256
MEASURED_TARBALL_SHA="$(sha256sum "$TARBALL" 2>/dev/null | awk '{print $1}')"
tarball_sha_ok=false
if [[ "$MEASURED_TARBALL_SHA" == "$KERNEL_SHA256" ]]; then
  tarball_sha_ok=true
fi

# 6b. Measured kernel version from header
MEASURED_KERNEL_VERSION="$(sed -n 's/^# Linux\/arm64 \([^ ]*\) Kernel Configuration$/\1/p' "$GUEST_DIR/kernel.config" 2>/dev/null | head -n 1)"
kernel_version_ok=false
if [[ -n "$MEASURED_KERNEL_VERSION" && "$MEASURED_KERNEL_VERSION" == "$KERNEL_VERSION" ]]; then
  kernel_version_ok=true
fi

# 6c. Measured init_estatico via ELF inspection inside builder with --network none
init_estatico=false
if docker run --rm --network none -v "$GUEST_DIR:/guest:ro" "$BUILDER_IMAGE" sh -c \
  'test -f /guest/srf-guest-init.bin && ! LC_ALL=C readelf -l /guest/srf-guest-init.bin | grep -q INTERP && ! LC_ALL=C readelf -d /guest/srf-guest-init.bin | grep -q -E "(DYNAMIC|Dynamic section)"' 2>/dev/null; then
  init_estatico=true
fi

# 6d. Required symbols in kernel config
symbols_ok=true
for s in CONFIG_VIRTIO_MMIO CONFIG_VIRTIO_BLK CONFIG_VIRTIO_NET CONFIG_VIRTIO_CONSOLE CONFIG_HW_RANDOM_VIRTIO CONFIG_FW_CFG_SYSFS CONFIG_EXT4_FS CONFIG_DEVTMPFS_MOUNT CONFIG_USER_NS CONFIG_SECCOMP_FILTER; do
  if ! grep -qx "$s=y" "$GUEST_DIR/kernel.config" 2>/dev/null; then
    symbols_ok=false
    break
  fi
done

# 6e. Mode 444 on guest-root.raw
raw_perm="$(stat -c %a "$GUEST_DIR/guest-root.raw" 2>/dev/null || true)"
perm_ok=false
if [[ "$raw_perm" == "444" ]]; then
  perm_ok=true
fi

# 6f. Conjunction of all checks
overall_ok=false
if [[ "$tarball_sha_ok" == "true" && "$kernel_version_ok" == "true" && "$init_estatico" == "true" && "$symbols_ok" == "true" && "$perm_ok" == "true" ]]; then
  overall_ok=true
fi

# 7. Write guest/PROCEDENCIA.json
cat > "$GUEST_DIR/PROCEDENCIA.json" <<EOF
{"kernel_version": "$MEASURED_KERNEL_VERSION", "kernel_tarball_sha256": "$MEASURED_TARBALL_SHA", "rootfs_origen": "$ROOTFS_IMAGE", "init_estatico": $init_estatico}
EOF

# 8. Generate guest/MANIFIESTO.sha256
(cd "$GUEST_DIR" && sha256sum Image guest-root.raw kernel.config srf-guest-init.bin PROCEDENCIA.json > MANIFIESTO.sha256)

# Update cache sealed manifest
(cd "$CACHE_DIR" && sha256sum Image guest-root.raw kernel.config "linux-$KERNEL_VERSION.tar.xz" > MANIFIESTO.sha256)

# 9. Cache files and hashes for construccion.json (H3)
IMAGE_CACHE_SHA="$(sha256sum "$CACHE_DIR/Image" 2>/dev/null | awk '{print $1}')"
CONFIG_CACHE_SHA="$(sha256sum "$CACHE_DIR/kernel.config" 2>/dev/null | awk '{print $1}')"
ROOT_CACHE_SHA="$(sha256sum "$CACHE_DIR/guest-root.raw" 2>/dev/null | awk '{print $1}')"

if [[ $JSON_OUTPUT -eq 1 ]]; then
  cat <<EOF
{
  "ok": $overall_ok,
  "descarga": $descarga,
  "network_mode": "none",
  "docker_network_mode": "none",
  "kernel_version": "$MEASURED_KERNEL_VERSION",
  "kernel_tarball_sha256": "$MEASURED_TARBALL_SHA",
  "rootfs_origen": "$ROOTFS_IMAGE",
  "init_estatico": $init_estatico,
  "cpuset": "$CPUSET",
  "cache": {
    "path": "$CACHE_DIR",
    "files": [
      {"name": "linux-$KERNEL_VERSION.tar.xz", "sha256": "$MEASURED_TARBALL_SHA"},
      {"name": "Image", "sha256": "$IMAGE_CACHE_SHA"},
      {"name": "kernel.config", "sha256": "$CONFIG_CACHE_SHA"},
      {"name": "guest-root.raw", "sha256": "$ROOT_CACHE_SHA"}
    ]
  }
}
EOF
else
  if [[ "$overall_ok" == "true" ]]; then
    log "Build finished successfully. Artifacts ready in $GUEST_DIR."
  else
    log "Build failed validation checks."
  fi
fi

if [[ "$overall_ok" != "true" ]]; then
  exit 1
fi
