#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
set -eu

cd "$(dirname "$0")/.."
mkdir -p target/cargo-home target/tmp target/u-asan-bin
cat > target/u-asan-bin/cc <<'WRAPPER'
#!/bin/sh
exec clang-18 -fsanitize=address "$@"
WRAPPER
chmod +x target/u-asan-bin/cc

scope() {
    systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
        nice -n 19 taskset -c 12-15 "$@"
}

cargo_local() {
    scope env CARGO_HOME="$PWD/target/cargo-home" TMPDIR="$PWD/target/tmp" "$@"
}

target=$(rustc -vV | sed -n 's/^host: //p')
asan_runtime=$(clang-18 -print-file-name="libclang_rt.asan-${target%%-*}.so")
asan_dir="$PWD/target/u-asan/$target/release"

case "${1:-offline}" in
    offline)
        set -x
        cargo_local cargo fmt --check
        cargo_local cargo clippy --offline --locked --release --tests -- -D warnings
        cargo_local cargo test --offline --locked --release --lib -- --test-threads=1
        cargo_local cargo build --offline --locked --release
        scope cc -std=c11 -O2 -Wall -Wextra -Werror -pedantic tests/bulk_abi_probe.c \
            -Ltarget/release '-Wl,-rpath,$ORIGIN/release' -lvp_capture -o target/u_bulk_abi_probe
        scope target/u_bulk_abi_probe
        scope cc -std=c11 -O2 -Wall -Wextra -Werror -I. tests/abi_probe.c \
            -Ltarget/release '-Wl,-rpath,$ORIGIN/release' -lvp_capture -o target/abi_probe
        scope python3 -IB -m unittest discover -s tests -p test_bulk_capture.py -v

        cargo_local env CC="$PWD/target/u-asan-bin/cc" RUSTC_BOOTSTRAP=1 \
            RUSTFLAGS=-Zsanitizer=address ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 \
            cargo test --offline --locked --release --target "$target" --target-dir target/u-asan \
            --lib -- --test-threads=1
        cargo_local env CC="$PWD/target/u-asan-bin/cc" RUSTC_BOOTSTRAP=1 \
            RUSTFLAGS=-Zsanitizer=address cargo build --offline --locked --release \
            --target "$target" --target-dir target/u-asan
        scope clang-18 -std=c11 -O1 -g -fsanitize=address -shared-libasan \
            -Wall -Wextra -Werror -pedantic tests/bulk_abi_probe.c -L"$asan_dir" \
            -Wl,-rpath,"$asan_dir" -lvp_capture -o target/u-asan/bulk_abi_probe
        scope env LD_LIBRARY_PATH="$(dirname "$asan_runtime")" LD_PRELOAD="$asan_runtime" \
            ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 target/u-asan/bulk_abi_probe
        scope clang-18 -std=c11 -O1 -g -fsanitize=address -shared-libasan \
            -Wall -Wextra -Werror -I. tests/abi_probe.c -L"$asan_dir" \
            -Wl,-rpath,"$asan_dir" -lvp_capture -o target/u-asan/abi_probe
        scope env LD_PRELOAD="$asan_runtime" ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
            VP_CAPTURE_TEST_LIBRARY="$asan_dir/libvp_capture.so" \
            python3 -IB -m unittest discover -s tests -p test_bulk_capture.py -v
        ;;
    private-x)
        set -x
        scope timeout -k 3s 50s sh tests/private_x.sh \
            python3 -IB -m unittest discover -s tests -p 'test_*capture.py' -v
        scope timeout -k 3s 50s sh tests/private_x.sh env \
            LD_PRELOAD="$asan_runtime" ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 \
            VP_CAPTURE_TEST_LIBRARY="$asan_dir/libvp_capture.so" \
            VP_CAPTURE_TEST_ABI_PROBE="$PWD/target/u-asan/abi_probe" \
            python3 -IB -m unittest discover -s tests -p 'test_*capture.py' -v
        ;;
    *)
        exit 2
        ;;
esac
