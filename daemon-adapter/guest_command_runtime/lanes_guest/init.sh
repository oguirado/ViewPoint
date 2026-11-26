#!/bin/sh
# Dedicated offline image entry. A live relay needs an admitted host binding.
set -efu
export PATH=/bin
unset CDPATH ENV BASH_ENV
umask 077

[ "$$" = 1 ] && [ "$(id -u)" = 0 ] || exit 78
offline() {
    /bin/busybox mount -t proc -o nosuid,nodev,noexec proc /proc || return
    read -r cmdline </proc/cmdline || return
    case " $cmdline " in
        *" atalaya_offline=1 "*) ;;
        *) return 78 ;;
    esac
    /bin/busybox mount -t cgroup2 -o nosuid,nodev,noexec cgroup2 /sys/fs/cgroup || return
    /bin/busybox mount -t tmpfs -o nosuid,nodev,noexec,size=16M tmpfs /work || return
    exec /sbin/lane-smoke --boot
}

if ! offline; then
    printf '%s\n' 'ATALAYA_OFFLINE_REFUSED' >&2
fi
exec /bin/busybox poweroff -f
