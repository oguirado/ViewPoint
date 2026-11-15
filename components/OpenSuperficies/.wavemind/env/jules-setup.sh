#!/bin/sh
trap 'rc=$?; [ "$rc" -eq 1 ] && exit 5; exit "$rc"' EXIT
set -eu
# Generated from pins.json; do not edit.
blender -b --version 2>&1 | grep -F -- '3.4.1' >/dev/null
dpkg-query -W build-essential 2>&1 | grep -F -- '12.9' >/dev/null
cmake --version 2>&1 | grep -F -- '3.25.1' >/dev/null
go version 2>&1 | grep -F -- '1.26.0' >/dev/null
godot --version 2>&1 | grep -F -- '4.4.1' >/dev/null
node --version 2>&1 | grep -F -- '22.16.0' >/dev/null
python3 -m pip --version 2>&1 | grep -F -- '23.0.1' >/dev/null
pkg-config --version 2>&1 | grep -F -- '1.8.1' >/dev/null
python3 --version 2>&1 | grep -F -- '3.11.2' >/dev/null
rustc --version 2>&1 | grep -F -- '1.85.1' >/dev/null
tini --version 2>&1 | grep -F -- '0.19.0' >/dev/null
