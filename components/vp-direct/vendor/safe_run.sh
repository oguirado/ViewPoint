#!/bin/sh
set -eu
exec systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- "$@"
