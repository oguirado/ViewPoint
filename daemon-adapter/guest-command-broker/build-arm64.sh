#!/usr/bin/env bash
# Build the normal guest PID1 broker without executing the result.
set -euo pipefail

if [[ $# != 1 ]]; then
    printf 'Usage: %s TARGET_DIR\n' "${0##*/}" >&2
    exit 2
fi

broker_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
target=aarch64-unknown-linux-gnu
env -u CARGO_ENCODED_RUSTFLAGS \
    RUSTFLAGS='-C target-feature=+crt-static -C relocation-model=static' \
    cargo build --offline --locked --release --manifest-path "$broker_dir/Cargo.toml" \
    --target-dir "$1" --target "$target" --bin guest-command-broker

# Cargo may hard-link the output to its cache. Local registration requires a
# protected, single-link asset, so publish an independent copy.
broker="$1/$target/release/guest-command-broker"
staged=$(mktemp -- "$broker.XXXXXXXX")
trap 'rm -f -- "$staged"' EXIT
install -m 0755 -- "$broker" "$staged"
mv -fT -- "$staged" "$broker"
