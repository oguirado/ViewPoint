#!/bin/sh
# OPERATOR-RUN ONLY: offline guest assembly; no pulls, apt or kernel builds.
set -eu
trap 'rc=$?; case "$rc" in 0|2|3|4|5|6|7) exit "$rc";; *) exit 3;; esac' 0
if [ "$#" -ne 0 ]; then
    printf '%s\n' 'AUSENTE — guest builder accepts no arguments' >&2
    exit 2
fi
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$repo"
out="$repo/build/vm_guest"
work="$repo/build/vm_guest_work"
base=opensuperficies-program-gtk-arm64:0.1.0
builder=srf-guest-builder
if [ -e "$out" ]; then
    printf '%s\n' 'AUSENTE — build/vm_guest exists; move it aside explicitly before rebuilding' >&2
    exit 3
fi
# Resolve every immutable local image ID before creating any container.
base_id=$(docker image inspect --format '{{.Id}}' "$base")
builder_id=$(docker image inspect --format '{{.Id}}' "$builder")
[ -n "$base_id" ] && [ -n "$builder_id" ] && [ -f guest/Image ]
[ "$(docker image inspect --format '{{.Architecture}}' "$base_id")" = arm64 ]
[ "$(docker image inspect --format '{{.Architecture}}' "$builder_id")" = arm64 ]
[ ! -e "$work" ] || { printf '%s\n' 'AUSENTE — guest builder scratch already exists' >&2; exit 3; }
mkdir -p "$work/stage-out" "$work/tmp"
work=$(CDPATH='' cd -- "$work" && pwd)
export TMPDIR="$work/tmp"
export GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local CGO_ENABLED=0 GOOS=linux GOARCH=arm64
if command -v go >/dev/null 2>&1; then guest_go=$(command -v go); else guest_go=/usr/local/go/bin/go; fi
printf '%s\n' 'OFFLINE: export pinned local program image, install static init/agent, assemble read-only ext4 root.'
taskset -c 10-14 "$guest_go" test ./src/vmsurface/helpers
taskset -c 10-14 "$guest_go" build -trimpath -buildvcs=false -o "$work/srf-guest-init" ./cmd/srf-guest-init
taskset -c 10-14 "$guest_go" build -trimpath -buildvcs=false -o "$work/srf-guest-agent" ./cmd/srf-guest-agent
cid=''
cleanup() {
    rc=$?
    trap - 0
    if [ -n "$cid" ]; then docker rm -f "$cid" >/dev/null 2>&1 || :; fi
    if [ "$rc" -ne 0 ]; then
        printf '%s\n' 'AUSENTE — offline guest rootfs build failed; scratch retained for inspection' >&2
        exit 3
    fi
}
trap cleanup 0
trap 'exit 6' HUP INT TERM
# Prepare guest-specific copies on the host; the builder image need not contain Python.
python3 -I - "$repo" "$work" <<'ADAPT'
from pathlib import Path
import sys
repo, work = map(Path, sys.argv[1:])
entry = (repo / 'docker/program/entrypoint.sh').read_text()
if entry.count("PROGRAMS = {'notepad':") != 1 or '/srfview' not in entry:
    print('AUSENTE — program entrypoint does not match guest recipe', file=sys.stderr)
    raise SystemExit(3)
entry = entry.replace('/srfview', '/run/srfview')
anchor = "\nclass Refused(Exception):"
if entry.count(anchor) != 1:
    print('AUSENTE — program entrypoint alias anchor missing', file=sys.stderr)
    raise SystemExit(3)
entry = entry.replace(anchor, "\nPROGRAMS['notepad-vm'] = PROGRAMS['notepad']\n" + anchor)
(work / 'identity-entrypoint.sh').write_text(entry)
xpra = (repo / 'docker/identity/xpra-layer.sh').read_text()
if 'XPRA_SOCKET=/srfview/xpra.sock' not in xpra:
    print('AUSENTE — xpra socket anchor missing', file=sys.stderr)
    raise SystemExit(3)
(work / 'identity-xpra-layer.sh').write_text(xpra.replace('/srfview', '/run/srfview'))
ADAPT
cid=$(docker create --network none "$base_id")
docker export "$cid" > "$work/rootfs.tar"
docker rm "$cid" >/dev/null
cid=''
# run.sh belongs to the guest recipe, not to the exported image.
docker run --rm --network none --cpuset-cpus 10-14 \
    --memory 8192m --memory-swap 8192m --pids-limit 256 \
    -v "$work:/input" -v "$repo/docker/vm/guest:/recipe:ro" \
    "$builder_id" sh /recipe/assemble.sh
# The builder writes root.raw as root; hand it back to the invoking user before the host-side chmod/seal.
docker run --rm --network none --cpuset-cpus 10-14 \
    --memory 8192m --memory-swap 8192m --pids-limit 256 \
    -v "$work:/input" "$builder_id" \
    chown "$(id -u):$(id -g)" /input/root.ext4
# Host side: wrap the filesystem in a GPT disk with the fixed root PARTUUID.
. "$repo/docker/vm/guest/root_disk.sh"
create_root_disk "$work/root.ext4" "$work/stage-out/root.raw"
rm -f "$work/root.ext4"
cp guest/Image "$work/stage-out/Image"
python3 -I - "$repo" "$work" "$base" "$base_id" "$builder_id" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
repo, work = map(Path, sys.argv[1:3])
stage = work / 'stage-out'
def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()
sources = ['docker/program/entrypoint.sh', 'docker/identity/xpra-layer.sh',
           'docker/vm/guest/assemble.sh', 'docker/vm/guest/root_disk.sh',
           'scripts/vm/build_guest_rootfs.sh']
# Include all init/agent/proto/helper Go sources, including generated allow-list bytes.
for directory in ('cmd/srf-guest-init', 'cmd/srf-guest-agent', 'src/vmsurface/proto', 'src/vmsurface/helpers'):
    sources.extend(str(p.relative_to(repo)) for p in sorted((repo / directory).rglob('*.go')))
provenance = {
    'schema': 'opensuperficies.vm-guest-provenance/v1',
    'base_image': sys.argv[3], 'base_image_id': sys.argv[4],
    'builder_image_id': sys.argv[5], 'network': 'none',
    'arch': 'arm64', 'cgo_enabled': False, 'root_readonly': True,
    'root_bytes': (stage / 'root.raw').stat().st_size,
    'root_fs_bytes': 3 * 1024**3,
    'root_partuuid': 'c0ffee00-5a1e-4d0e-9e00-0e5f0a70c001',
    'kernel_sha256': digest(stage / 'Image'),
    'init_sha256': digest(work / 'srf-guest-init'),
    'agent_sha256': digest(work / 'srf-guest-agent'),
    'sources': {p: digest(repo / p) for p in sorted(set(sources))},
}
(stage / 'PROVENANCE.json').write_text(json.dumps(provenance, sort_keys=True, indent=2) + '\n')
(stage / 'MANIFEST.sha256').write_text(''.join(
    digest(stage / name) + '  ' + name + '\n'
    for name in ('Image', 'root.raw')))
PY
chmod 0444 "$work/stage-out/Image" "$work/stage-out/root.raw" "$work/stage-out/PROVENANCE.json" "$work/stage-out/MANIFEST.sha256"
# Publish only a complete artifact directory; never overwrite an existing approval input.
[ ! -e "$out" ]
mv -T "$work/stage-out" "$out"
printf '%s\n' 'Guest artifacts: build/vm_guest/{Image,root.raw,MANIFEST.sha256,PROVENANCE.json}'
