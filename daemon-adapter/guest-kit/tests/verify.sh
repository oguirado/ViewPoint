#!/usr/bin/env bash
set -euo pipefail
kit=$(realpath -e -- "$(dirname -- "$0")/..")
baseline=$(realpath -e -- "${1:?pass the H1-ENTRY baseline worktree}")
output="$kit/.build"
[[ $(uname -m) == aarch64 ]]
[[ $(git -C "$baseline" rev-parse HEAD) == a5000a6a236ae32b12188d16313db74994966af2 ]]
scope=$(awk -F/ '/^0::/ {print $NF}' /proc/self/cgroup)
systemctl --user show "$scope" -p Id -p ControlGroup -p MemoryHigh -p MemoryMax
[[ $(systemctl --user show "$scope" -p MemoryHigh --value) == 6442450944 ]]
[[ $(systemctl --user show "$scope" -p MemoryMax --value) == 8589934592 ]]
umask 022
mkdir -p "$output"
export LIVE_KIT_BUILD="$output"
baseline_kit="$baseline/daemon-adapter/guest-kit"

echo "Fail-before $baseline_kit"
before_rc=0
LIVE_KIT_ROOT="$baseline_kit" python3 -B "$kit/tests/test_live_kit.py" > "$output/before-python.log" 2>&1 || before_rc=$?
[[ $before_rc == 1 ]]
missing=$(grep -cF 'AssertionError: False is not true : required kit source missing:' "$output/before-python.log")
failures=$(grep -cE '^FAIL: test_' "$output/before-python.log")
tests=$(awk '/^Ran [0-9]+ tests? in / {print $2}' "$output/before-python.log")
[[ "$missing" -gt 0 && "$missing" == "$failures" && "$missing" == "$tests" ]]
grep -E '^FAIL: test_|^Ran |^FAILED ' "$output/before-python.log"

before_rc=0
aarch64-linux-gnu-gcc -std=c17 -O2 -Wall -Wextra -Werror -pthread -I "$baseline_kit/guest" \
    "$kit/tests/test_guest.c" -o "$output/test-guest-baseline" > "$output/before-c.log" 2>&1 || before_rc=$?
[[ $before_rc == 1 ]]
grep -F 'fatal error: init.c: No such file or directory' "$output/before-c.log"

echo "Pass-after $kit"
aarch64-linux-gnu-gcc -dumpfullversion
aarch64-linux-gnu-gcc -std=c17 -Os -Wall -Wextra -Werror -static -no-pie \
    -Wl,--build-id=none -s "$kit/guest/init.c" -o "$output/init-aarch64"
aarch64-linux-gnu-gcc -std=c17 -Os -Wall -Wextra -Werror -static -no-pie \
    -Wl,--build-id=none -s "$kit/guest/init.c" -o "$output/init-aarch64-repeat"
cmp -- "$output/init-aarch64" "$output/init-aarch64-repeat"
readelf -W -h -l "$output/init-aarch64" > "$output/init-elf.txt"
grep -E 'Class:.*ELF64|Machine:.*AArch64|Type:.*EXEC' "$output/init-elf.txt"
if grep -Eq '(^|[[:space:]])(INTERP|DYNAMIC)([[:space:]]|$)' "$output/init-elf.txt"; then
    echo "init must be static" >&2
    exit 1
fi
sha256sum -- "$output/init-aarch64" "$output/init-aarch64-repeat"
aarch64-linux-gnu-gcc -std=c17 -O2 -Wall -Wextra -Werror -pthread -I "$kit/guest" \
    "$kit/tests/test_guest.c" -o "$output/test-guest"
"$output/test-guest"
LIVE_KIT_ROOT="$kit" python3 -B "$kit/tests/test_live_kit.py"
