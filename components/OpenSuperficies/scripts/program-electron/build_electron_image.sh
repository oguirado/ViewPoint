#!/bin/sh
set -eu
if [ "$#" -ne 0 ]; then
    printf '%s\n' 'REFUSED: arguments_not_allowed' >&2
    exit 2
fi
exec python3 -I "$(dirname "$0")/print_build.py"
