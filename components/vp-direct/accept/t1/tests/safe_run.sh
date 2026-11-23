#!/bin/sh
set -eu
tests_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
case "$#:${1-}" in
    0:) set -- -m unittest discover -s "$tests_dir" -p 'test_*.py' -v ;;
    1:calibrate) set -- "$tests_dir/calibrate.py" ;;
    *) printf '%s\n' 'Usage: safe_run.sh [calibrate]' >&2; exit 2 ;;
esac
scratch=$(mktemp -d "$tests_dir/.ram.XXXXXXXX")
trap 'rmdir "$scratch"' EXIT
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
    taskset -c 16-19 bwrap --ro-bind / / --dev /dev --tmpfs "$scratch" --setenv P7_TEST_RAM "$scratch" -- \
    python3 -I -B "$@"
