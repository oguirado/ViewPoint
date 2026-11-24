#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 4 ]]; then
    echo "usage: $0 LINUX_SOURCE FIRECRACKER_ARM64_BASE_CONFIG NEW_OUTPUT_DIR CROSS_COMPILE_PREFIX" >&2
    echo "Use an empty string as CROSS_COMPILE_PREFIX for a native AArch64 build." >&2
    exit 2
fi
src=$(realpath -e -- "$1")
base=$(realpath -e -- "$2")
out=$(realpath -m -- "$3")
fragment=$(realpath -e -- "$(dirname -- "$0")/firecracker-arm64.config")
cross=$4
[[ -x "$src/scripts/kconfig/merge_config.sh" && -f "$base" && ! -e "$out" ]]
mkdir -- "$out"
export ARCH=arm64 CROSS_COMPILE="$cross"
export KCONFIG_CONFIG="$out/.config"
"$src/scripts/kconfig/merge_config.sh" -m -O "$out" "$base" "$fragment"
make -C "$src" O="$out" olddefconfig
while IFS= read -r setting; do
    if [[ "$setting" == '# CONFIG_'* ]]; then
        symbol=${setting#\# }
        symbol=${symbol%% *}
        if grep -q "^$symbol=" "$KCONFIG_CONFIG"; then
            echo "Forbidden kernel option enabled: $symbol" >&2
            exit 1
        fi
    elif ! grep -Fxq -- "$setting" "$KCONFIG_CONFIG"; then
        echo "Required kernel option missing after olddefconfig: $setting" >&2
        exit 1
    fi
done < "$fragment"
make -C "$src" O="$out" Image
sha256sum -- "$base" "$fragment" "$KCONFIG_CONFIG" "$out/arch/arm64/boot/Image"
