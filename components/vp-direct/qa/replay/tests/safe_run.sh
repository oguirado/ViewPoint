#!/bin/sh
set -eu

tests=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repo=$(git -C "$tests" rev-parse --show-toplevel)
mode=${1:-suite}
case "$mode" in
    suite|baseline|findings) ;;
    *) echo "usage: $0 [suite|baseline|findings] [commit]" >&2; exit 2 ;;
esac
if [ "$#" -gt 2 ]; then
    echo "usage: $0 [suite|baseline|findings] [commit]" >&2
    exit 2
fi
revision=$(git -C "$repo" rev-parse --verify --end-of-options "${2:-HEAD}^{commit}")
scratch=$(mktemp -d "$tests/../.snapshot.XXXXXXXX")
trap 'rm -rf -- "$scratch"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
scratch=$(CDPATH= cd -- "$scratch" && pwd -P)
export TMPDIR="$scratch"

git -C "$repo" archive -o "$scratch/source.tar" "$revision" -- \
    components/vp-direct components/vision-engine components/wender-agent \
    components/blender-visual-qa agents tools contracts memoryweb-builtin \
    daemon-adapter/rewind_runtime daemon-adapter/guest_command_runtime
mkdir "$scratch/source"
tar -xf "$scratch/source.tar" -C "$scratch/source"
rm -- "$scratch/source.tar"
printf 'Pinned commit %s\n' "$revision" >&2

if [ "$mode" = baseline ]; then
    cd "$scratch/source"
    result=0
    for surface in perception/demand perception/stream perception/lifecycle perception/receipts \
        bus/broker bus/journal rewind/machine rewind/blender_rw1 rewind/microvm loop; do
        wrapper="components/vp-direct/$surface/tests/safe_run.sh"
        rc=0
        taskset -c 16-19 sh "$wrapper" >"$scratch/baseline.log" 2>&1 || rc=$?
        printf '\ncommand: taskset -c 16-19 sh %s (rc=%s)\n' "$wrapper" "$rc"
        if [ "$rc" -eq 0 ]; then
            tail -n 5 "$scratch/baseline.log"
        else
            tail -n 55 "$scratch/baseline.log"
            result=1
        fi
    done
    exit "$result"
fi

if [ "$mode" = findings ]; then
    set -- "$tests/probe_findings.py"
else
    set -- -m unittest discover -s "$tests" -p 'test_*.py' -v
fi

systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
    taskset -c 16-19 bwrap --ro-bind / / --tmpfs /dev/shm \
    --setenv REP_SOURCE "$scratch/source" --setenv REP_REVISION "$revision" -- \
    python3 -I -B "$@"
