#!/bin/sh
set -eu
surface=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
direct=$(realpath -- "$1")
shift
exec systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
    taskset -c 16-19 bwrap --ro-bind / / --tmpfs /dev/shm --unshare-net -- \
    env OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 VP_PERF_DIRECT="$direct" \
    python3 -I -B "$surface/benchmark.py" "$@"
