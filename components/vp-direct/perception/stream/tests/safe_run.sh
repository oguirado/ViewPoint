#!/bin/sh
set -eu
tests_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
scratch=$(mktemp -d "$tests_dir/.ram.XXXXXXXX")
trap 'rmdir "$scratch"' EXIT
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
    bwrap --ro-bind / / --tmpfs "$scratch" --setenv P3_TEST_RAM "$scratch" -- \
    python3 -I -B -m unittest discover -s "$tests_dir" -p 'test_*.py' -v
