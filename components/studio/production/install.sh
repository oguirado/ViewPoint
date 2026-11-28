#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
set -eu
export LC_ALL=C.UTF-8 LANG=C.UTF-8 TZ=UTC
if [ "$#" -ne 1 ]; then
    printf '%s\n' '{"reason":"invalid_install_prefix","retry_when":"an unused installation prefix is provided","next_command":["./install.sh","PREFIX"]}'
    exit 2
fi
source=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
parent=$(CDPATH='' cd -- "$(dirname -- "$1")" && pwd -P)
target=$parent/$(basename -- "$1")
if [ -e "$target" ] || [ -L "$target" ]; then
    printf '%s\n' '{"reason":"install_prefix_exists","retry_when":"an unused installation prefix is provided","next_command":["./install.sh","PREFIX"]}'
    exit 1
fi
stage=$(mktemp -d "$parent/.viewpoint-install.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
trap 'exit 2' HUP INT TERM
cp -R -- "$source/bin" "$source/lib" "$source/adapter" "$source/editorial"    "$source/library" "$source/review" "$source/series" "$source/short" "$source/LICENSE" "$source/NOTICE" "$source/USAGE.md" "$stage/"
"$stage/bin/viewpoint-studio" --list-capabilities >/dev/null
chmod 755 "$stage"
mv -T -n -- "$stage" "$target"
if [ -d "$stage" ]; then
    printf '%s\n' '{"reason":"install_prefix_exists","retry_when":"an unused installation prefix is provided","next_command":["./install.sh","PREFIX"]}'
    exit 1
fi
