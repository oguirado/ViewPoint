#!/bin/sh
# Fixed commands for the offline image and chroot replay.
set -efu
export PATH=/bin LANG=C
unset CDPATH ENV BASH_ENV
umask 077

smoke() {
    group=/sys/fs/cgroup
    printf '+memory +pids\n' > "$group/cgroup.subtree_control"
    for lane in 0 1; do
        leaf=$group/$lane
        workspace=/work/lane$lane
        mkdir -m 700 "$leaf" "$workspace"
        printf '%s\n' 67108864 > "$leaf/memory.max"
        printf '0\n' > "$leaf/memory.swap.max"
        printf '24\n' > "$leaf/pids.max"
        GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git init -q --template=/etc/git-template "$workspace"
        /bin/busybox chown -R "1000$lane:1000$lane" "$workspace"
        alternate=$workspace/.git/objects/info/alternates
        printf '/objects\n' > "$alternate"
        chmod 444 "$alternate"
    done

    for lane in 0 1; do
        /sbin/lane-run "$lane" / "/work/lane$lane" "$group/$lane" /bin/sh -ec '
            printf "%s\n" "$(id -u)" > /work/ready
            for attempt in $(seq 1 100); do
                [ ! -e /work/go ] || exit 0
                sleep 0.05
            done
            exit 1
        ' &
        case "$lane" in 0) first=$! ;; 1) second=$! ;; esac
    done
    for attempt in $(seq 1 100); do
        [ -s /work/lane0/ready ] && [ -s /work/lane1/ready ] && break
        sleep 0.05
    done
    if [ -s /work/lane0/ready ] && [ -s /work/lane1/ready ] &&
       [ "$(cat /work/lane0/ready)" = 10000 ] &&
       [ "$(cat /work/lane1/ready)" = 10001 ] &&
       grep -qx 'populated 1' "$group/0/cgroup.events" &&
       grep -qx 'populated 1' "$group/1/cgroup.events"; then
        touch /work/lane0/go /work/lane1/go
        if wait "$first" && wait "$second"; then
            for attempt in $(seq 1 100); do
                if grep -qx 'populated 0' "$group/0/cgroup.events" &&
                   grep -qx 'populated 0' "$group/1/cgroup.events"; then
                    printf '%s\n' 'ATALAYA_OFFLINE_LANES_OK'
                    return 0
                fi
                sleep 0.05
            done
        fi
    fi
    wait "$first" || :
    wait "$second" || :
    return 1
}

case "${1-}" in
    --boot)
        [ "$$" = 1 ] && [ "$(id -u)" = 0 ] || exit 78
        trap 'rc=$?; if [ "$rc" != 0 ]; then printf "%s\n" ATALAYA_OFFLINE_LANES_FAILED >&2; fi; exec /bin/busybox poweroff -f' EXIT
        smoke
        ;;
    --replay) smoke ;;
    *) exit 78 ;;
esac
