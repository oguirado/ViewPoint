#!/bin/sh
# Printer only: the operator executes the approved command separately.
set -eu
if [ "$#" -ne 0 ]; then
    printf '%s\n' 'AUSENTE — arguments not allowed' >&2
    exit 2
fi
exec python3 -I "$(dirname "$0")/print_build.py"
