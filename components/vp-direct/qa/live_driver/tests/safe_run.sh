#!/bin/sh
set -eu
tests_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
    python3 -IB -m unittest discover -v -s "$tests_dir" "$@"
