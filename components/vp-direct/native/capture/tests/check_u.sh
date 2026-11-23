#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
set -eu

cd "$(dirname "$0")/.."
test -z "${DISPLAY+x}"
cargo build --offline --locked --release
cc -std=c11 -O2 -Wall -Wextra -Werror -I. tests/abi_probe.c \
    -Ltarget/release '-Wl,-rpath,$ORIGIN/release' -lvp_capture -o target/abi_probe

case "${1:-offline}" in
    offline)
        target/abi_probe
        ;;
    private-x)
        sh tests/private_x.sh python3 -IB -m unittest discover -s tests -p test_capture.py -v
        ;;
    *)
        printf '%s\n' 'usage: sh tests/check_u.sh [offline|private-x]' >&2
        exit 2
        ;;
esac
