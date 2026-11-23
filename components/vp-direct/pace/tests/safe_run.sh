#!/bin/sh
set -eu
tests=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
    python3 -I -B -m unittest discover -s "$tests" -p 'test_*.py' -v
