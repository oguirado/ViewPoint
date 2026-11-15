#!/bin/sh
# OPERATOR-RUN NETWORK ACTION. This room only lints this script.
set -eu
trap 'fetch_rc=$?; case "$fetch_rc" in 0|2|3|4|5|6|7) exit "$fetch_rc";; *) exit 3;; esac' 0
if [ "$#" -ne 1 ] || [ "$1" != --operator-run ]; then
    printf '%s\n' 'AUSENTE — explicit --operator-run required' >&2
    exit 2
fi
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
vendor="$root/docker/vm/vendor"
if [ -e "$vendor/DIGEST" ] || [ -e "$vendor/SHA256SUMS" ]; then
    if [ -f "$vendor/DIGEST" ] && [ -f "$vendor/SHA256SUMS" ]; then
        python3 -I - "$root" <<'VERIFY'
import importlib.util
from pathlib import Path
import sys
sys.dont_write_bytecode = True
root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('vm_build', root / 'scripts/vm/print_build.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    module.vendor(root)
except (module.Refused, OSError):
    print('AUSENTE — existing vendor set is invalid', file=sys.stderr)
    raise SystemExit(3)
VERIFY
        printf '%s\n' 'Existing vendor set verified; no overwrite.'
        exit 0
    fi
    printf '%s\n' 'AUSENTE — incomplete existing vendor set; refusing overwrite' >&2
    exit 3
fi
for existing in "$vendor"/*.deb; do
    if [ -e "$existing" ]; then
        printf '%s\n' 'AUSENTE — existing package; refusing overwrite' >&2
        exit 3
    fi
done
printf '%s\n' 'Will pull arm64 ubuntu:24.04, resolve missing QEMU 8.2/e2fsprogs packages in a disposable container, download packages, write hashes and digest, and pin the Dockerfile. No image build or VM launch.'
mkdir -p "$root/build/room_CAPSULE/tmp" "$vendor"
stage=$(mktemp -d "$root/build/room_CAPSULE/tmp/vendor.XXXXXX")
container="srf-vm-vendor-fetch-$$"
cleanup() {
    fetch_rc=$?
    trap - EXIT
    timeout 20 docker rm -f "$container" >/dev/null 2>&1 || :
    rm -rf -- "$stage"
    case "$fetch_rc" in 0|2|3|4|5|6|7) exit "$fetch_rc";; *) exit 3;; esac
}
trap cleanup EXIT
trap 'exit 6' INT TERM HUP
timeout 600 docker pull --platform linux/arm64 ubuntu:24.04
digest=$(timeout 30 docker image inspect --format '{{index .RepoDigests 0}}' ubuntu:24.04)
digest=${digest##*@}
case "$digest" in sha256:*) ;; *) printf '%s\n' 'AUSENTE — base digest missing' >&2; exit 3 ;; esac
printf '%s\n' "$digest" > "$stage/DIGEST"
timeout 900 docker run -i --name "$container" --rm --platform linux/arm64 \
    --mount "type=bind,src=$stage,dst=/out" "ubuntu:24.04@$digest" \
    /bin/sh -es <<'FETCH'
      export DEBIAN_FRONTEND=noninteractive
      apt-get update >&2
      candidate=$(apt-cache policy qemu-system-arm | awk '/Candidate:/ {print $2}')
      case "$candidate" in 1:8.2.*) ;; *) echo "AUSENTE — QEMU 8.2 candidate missing" >&2; exit 3 ;; esac
      apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
          --no-breaks --no-replaces --no-enhances qemu-system-arm qemu-system-common \
          qemu-system-data e2fsprogs > /tmp/dependency-closure
      # APT resolves alternatives and excludes packages already installed in this
      # exact base. No recommendations (ipxe is unnecessary without a NIC).
      apt-get -s --no-install-recommends install \
          qemu-system-arm="$candidate" qemu-system-common qemu-system-data e2fsprogs > /tmp/install-plan
      cd /out
      # APT simulation emits only required changes, including versioned deps.
      # Extract each selected version, then download that exact package.
      awk '$1 == "Inst" { for (i=3; i<=NF; i++) if (substr($i,1,1)=="(") {v=substr($i,2); print $2 "=" v; break} }' /tmp/install-plan > /tmp/download-plan
      test -s /tmp/download-plan
      test "$(wc -l < /tmp/download-plan)" -le 256
      while IFS= read -r package; do apt-get download "$package" >&2; done < /tmp/download-plan
      sha256sum ./*.deb | sed "s@  ./@  @" > SHA256SUMS
      chmod 0644 ./*.deb SHA256SUMS DIGEST
FETCH
(cd "$stage" && sha256sum -c SHA256SUMS)
# Publish only into absent targets; partial failures are refused on the next run.
for artifact in "$stage"/*.deb "$stage/SHA256SUMS" "$stage/DIGEST"; do
    target="$vendor/$(basename "$artifact")"
    # The fetch container writes as root: copy (never hard-link another user's file), never overwrite.
    [ ! -e "$target" ] || exit 3
    cp -- "$artifact" "$target" || exit 3
    chmod 0644 "$target" || exit 3
done
python3 -I - "$root/docker/vm/Dockerfile" "$digest" <<'PY'
from pathlib import Path
import re
import sys
path = Path(sys.argv[1])
digest = sys.argv[2]
if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
    raise SystemExit(3)
text = path.read_text()
old = 'FROM ubuntu:24.04@sha256:OPERATOR_DIGEST'
new = 'FROM ubuntu:24.04@' + digest
if text.count(old) == 1:
    path.write_text(text.replace(old, new))
elif text.splitlines()[1] != new:
    raise SystemExit(3)
PY
printf '%s\n' 'Vendor set pinned. Build the static relay and guest artifacts, then run python3 -I scripts/vm/print_build.py --seal before requesting the per-recipe build approval.'
