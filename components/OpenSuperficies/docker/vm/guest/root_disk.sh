#!/bin/sh
# Sourced by assembly and offline tests. Requires util-linux sfdisk and coreutils.
# The filesystem size must be sector-aligned; GPT backup metadata follows it.
create_root_disk() (
    set -eu
    trap 'rc=$?; case "$rc" in 0|2|3|4|5|6|7) exit "$rc";; *) exit 3;; esac' 0
    [ "$#" -eq 2 ] || { printf '%s\n' 'AUSENTE — expected filesystem and disk paths' >&2; exit 2; }
    fs=$1
    disk=$2
    [ -f "$fs" ] && [ ! -e "$disk" ] || {
        printf '%s\n' 'AUSENTE — filesystem missing or disk already exists' >&2; exit 3;
    }
    bytes=$(stat -c %s "$fs")
    [ "$bytes" -gt 0 ] && [ "$((bytes % 512))" -eq 0 ] || {
        printf '%s\n' 'AUSENTE — filesystem size must be positive and sector aligned' >&2; exit 3;
    }
    sectors=$((bytes / 512))
    truncate -s "$(((2048 + sectors + 33) * 512))" "$disk"
    # Explicit sector units prevent dependence on sfdisk's automatic alignment.
    sfdisk "$disk" <<GPT
label: gpt
unit: sectors

start=2048, size=$sectors, type=0fc63daf-8483-4772-8e79-3d69d8477de4, uuid=c0ffee00-5a1e-4d0e-9e00-0e5f0a70c001
GPT
    dd if="$fs" of="$disk" bs=1048576 seek=1 conv=notrunc,sparse status=none
)
