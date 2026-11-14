#!/bin/sh
trap 'rc=$?; case "$rc" in 0|[2-7]) exit "$rc";; *) exit 5;; esac' EXIT
set -eu
# Behavioural identity/toolchain assertions; invoked as uid 10001.
[ "$(id -u)" = 10001 ]
[ "$(id -g)" = 10001 ]
for tool in tini git jq ps python3 taskset go node rustc cargo gcc g++ make cmake pkg-config godot blender; do
    command -v "$tool" >/dev/null
done
# Measured identical offline Go 1.26.0 arm64 builds; all multicall copies share this pin.
runner_sha256=90e5ea5ee9ad6cc28265e00dc6449257ba55d70e8ffbf9f4d272e3f1783f7cbb
[ "$runner_sha256" != AUSENTE ] || exit 4
[ "$(find /opt/w8 -mindepth 1 -maxdepth 1 | wc -l)" -eq 5 ] || exit 5
for verb in ingest gate status extract purge; do
    [ -f "/opt/w8/$verb" ] && [ ! -L "/opt/w8/$verb" ] || exit 5
    [ "$(stat -c '%u:%g:%a' "/opt/w8/$verb")" = '0:0:555' ] || exit 5
    printf '%s  %s\n' "$runner_sha256" "/opt/w8/$verb" | sha256sum -c - >/dev/null || exit 5
done
[ -d /workspace ] && [ -z "$(ls -A /workspace)" ]
[ -d /opt/cache ] && [ -z "$(ls -A /opt/cache)" ]
python3 -m venv --help >/dev/null
cargo --version | grep -F '1.85.1' >/dev/null
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
