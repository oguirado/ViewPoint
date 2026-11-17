#!/bin/sh
set -eu
journal_tests_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
    python3 -I -B -m unittest discover -s "$journal_tests_dir" -p 'test_*.py' -v
