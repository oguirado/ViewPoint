#!/bin/sh
set -eu

# Isolate both pathname and abstract X11 sockets. Xvfb's XWD fixture lives
# on this private tmpfs and cannot reach the operator's Xauthority or display.
exec unshare --map-root-user --mount --net --ipc --propagation private sh -eu -c '
    mount -n -t tmpfs -o mode=1777,nosuid,nodev,size=64m tmpfs /tmp
    mkdir -m 1777 /tmp/.X11-unix
    private_dev=/tmp/vp-dev
    mkdir "$private_dev"
    mount -n -t tmpfs -o nosuid,mode=755 tmpfs "$private_dev"
    for device in null zero random urandom; do
        touch "$private_dev/$device"
        mount -n --bind "/dev/$device" "$private_dev/$device"
    done
    mkdir -m 1777 "$private_dev/shm"
    mount -n --move "$private_dev" /dev
    test ! -e /dev/dri && test ! -e /dev/nvidia0
    export VP_CAPTURE_PRIVATE_X=1
    exec "$@"
' vp-private-x "$@"
