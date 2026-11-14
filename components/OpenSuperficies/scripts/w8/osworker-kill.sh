#!/bin/sh
trap 'rc=$?; [ "$rc" -eq 1 ] && exit 5; exit "$rc"' EXIT
set -eu
trap 'rc=$?; trap - EXIT; case "$rc" in 0|2|3|4|5|6|7) ;; 1|124) rc=5;; *) rc=6;; esac; exit "$rc"' EXIT
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/osworker-lib.inc"
[ "$#" -eq 1 ] || fail 2 'usage: osworker-kill.sh off|on'
case $1 in off|on) :;; *) fail 2 'usage: osworker-kill.sh off|on';; esac
root_only; uid_lookup
case $1 in
 off)
    # No receipt writer runs here. Driver seat_lost is local; restart janitor is authoritative.
    user_run systemctl --user stop w8-seat.service || :
    loginctl disable-linger osworker
    systemctl stop "user@$OSW_UID.service"
    n=0
    while [ "$n" -lt 10 ]; do
        if pgrep -u osworker >/dev/null; then :; else
            rc=$?
            [ "$rc" -eq 1 ] || fail 5 'process query failed'
            printf 'killswitch=off\n'; exit 0
        fi
        sleep 1; n=$((n + 1))
    done
    pkill -KILL -u osworker || :
    if pgrep -u osworker >/dev/null; then fail 5 'osworker processes survived kill switch'; else
        rc=$?; [ "$rc" -eq 1 ] || fail 5 'process query failed'
    fi
    printf 'killswitch=off\n'
    ;;
 on)
    loginctl enable-linger osworker
    # logind does not always restart a manager that 'off' stopped explicitly; start it ourselves.
    systemctl start "user@$OSW_UID.service"
    wait_bus || fail 5 'user bus unavailable after bounded wait'
    user_run systemctl --user start docker
    user_run systemctl --user start w8-seat.service
    sh "$SCRIPT_DIR/osworker-verify.sh"
    ;;
esac
