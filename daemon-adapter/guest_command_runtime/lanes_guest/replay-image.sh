#!/bin/bash
# Run the pinned archive on the host kernel within a delegated test scope.
set -euo pipefail
[ "$#" = 1 ] || exit 2
image=$(realpath "$1")
lock=$(dirname "$(realpath "$0")")/inputs.json
expected=$(python3 -B -c 'import json, sys; print(json.load(open(sys.argv[1]))["image_sha256"])' "$lock")
printf '%s  %s\n' "$expected" "$image" | sha256sum -c -

IFS=: read -r hierarchy controllers relative </proc/self/cgroup
scope=/sys/fs/cgroup$relative
[ "$hierarchy:$controllers" = 0: ] && [[ "$scope" = *.scope ]]
[ "$(cat "$scope/memory.max")" = 8589934592 ]
mkdir "$scope/replay-manager" "$scope/replay"
printf '%s\n' "$$" > "$scope/replay-manager/cgroup.procs"
printf '+memory +pids\n' > "$scope/cgroup.subtree_control"

root=$(mktemp -d)
trap 'rmdir "$root"' EXIT
unshare -Urpf --map-auto --mount-proc --kill-child bash -ceu '
    root=$1
    mount -t tmpfs -o mode=0700,nosuid,size=96M tmpfs "$root"
    cd "$root"
    set -o pipefail
    # A user namespace cannot create these nodes. Archive metadata is checked
    # separately; bind only the devices used by bubblewrap for this replay.
    gzip -dc "$2" | cpio -id --quiet --no-absolute-filenames -f "dev/*"
    for name in null zero full random urandom tty; do
        : > "dev/$name"
        mount --bind "/dev/$name" "dev/$name"
    done
    mount --rbind /proc proc
    mount --bind "$3" sys/fs/cgroup
    env -i PATH=/bin /usr/sbin/chroot "$root" /sbin/lane-smoke --replay
    for test in openat201 openat203 ptrace01 unshare02 mount03; do
        rc=0
        env -i PATH=/sbin/ltp:/bin LANG=C TMPDIR=/tmp LTP_SINGLE_FS_TYPE=tmpfs \
            /usr/sbin/chroot "$root" "/sbin/ltp/$test" || rc=$?
        printf "LTP %s rc=%s\n" "$test" "$rc"
    done
' replay "$root" "$image" "$scope/replay"
rmdir "$scope/replay/"{0,1} "$scope/replay"
