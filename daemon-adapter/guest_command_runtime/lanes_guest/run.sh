#!/bin/sh
# Trusted guest broker entry. Arguments before COMMAND come from its own grant.
set -efu
export PATH=/bin:/usr/bin
unset CDPATH ENV BASH_ENV
umask 077

[ "$#" -ge 5 ] && [ "$(id -u)" = 0 ] || exit 126
lane=$1
root=$2
workspace=$3
leaf=$4
shift 4
case "$lane" in 0|1) ;; *) exit 126 ;; esac
case "$1" in /bin/*) ;; *) exit 126 ;; esac
for dir in "$root" "$workspace" "$leaf"; do
    [ -d "$dir" ] && [ "$(readlink -f -- "$dir")" = "$dir" ] || exit 126
done
[ "${leaf##*/}" = "$lane" ] || exit 126
[ "$(stat -c %u:%a -- "$leaf")" = 0:700 ] || exit 126
[ "$(stat -f -c %t -- "$leaf")" = 63677270 ] || exit 126
[ "$(cat "$leaf/memory.max")" = 67108864 ] || exit 126
[ "$(cat "$leaf/pids.max")" = 24 ] || exit 126
[ "$(cat "$leaf/memory.swap.max")" = 0 ] || exit 126
[ "$(stat -c %u:%g:%a -- "$workspace")" = "1000$lane:1000$lane:700" ] || exit 126
alternate=$workspace/.git/objects/info/alternates
[ -f "$alternate" ] && [ "$(readlink -f -- "$alternate")" = "$alternate" ] || exit 126
[ "$(stat -c %u:%g:%a -- "$alternate")" = 0:0:444 ] || exit 126
[ "$(cat "$alternate")" = /objects ] || exit 126
printf '%s\n' "$$" > "$leaf/cgroup.procs"

case "$root" in /) root= ;; esac
exec "$root/bin/bwrap" \
    --unshare-pid --unshare-ipc --unshare-net --unshare-uts --unshare-cgroup \
    --new-session --die-with-parent --clearenv \
    --cap-add CAP_SETUID --cap-add CAP_SETGID \
    --ro-bind "$root/bin" /bin --ro-bind "$root/lib" /lib \
    --ro-bind "$root/etc" /etc --ro-bind "$root/objects" /objects \
    --bind "$workspace" /work \
    --ro-bind "$alternate" /work/.git/objects/info/alternates --dir /proc --dir /sys \
    --perms 01777 --size 8388608 --tmpfs /tmp \
    --tmpfs /credentials --remount-ro /credentials --dev /dev --remount-ro / \
    --setenv PATH /bin --setenv HOME /work --setenv LANG C \
    --setenv GIT_CONFIG_NOSYSTEM 1 --setenv GIT_TERMINAL_PROMPT 0 \
    --setenv GIT_EXEC_PATH /bin --setenv GIT_TEMPLATE_DIR /etc/git-template \
    --chdir /work -- /bin/lane-enter "$lane" "$@"
