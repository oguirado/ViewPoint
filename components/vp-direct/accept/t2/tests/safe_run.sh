#!/bin/sh
set -eu
tests_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
case "${1:-}" in
    --live)
        shift
        set -- "$tests_dir/../LIVE_t2.py" "$@"
        ;;
    --score)
        shift
        set -- "$tests_dir/../score.py" "$@"
        ;;
    calibrate)
        shift
        set -- "$tests_dir/calibrate.py" "$@"
        ;;
    *)
        set -- -m unittest discover -s "$tests_dir" -p 'test_*.py' -v
        ;;
esac
exec systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
    taskset -c 16-19 python3 -I -B "$@"
