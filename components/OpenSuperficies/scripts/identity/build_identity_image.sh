#!/bin/sh
# Validate operator approval and print argv only. This script NEVER builds.
set -eu
if [ "$#" -ne 0 ]; then
    printf '%s\n' 'REFUSED: no arguments accepted' >&2
    exit 2
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 4
exec python3 -B -I "$script_dir/print_build.py"
