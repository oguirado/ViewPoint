#!/usr/bin/env bash
set -euo pipefail

evidence=components/vp-direct/accept/t2/qt2-evidence
label=$1
display=$2
apps=$3
first=$4
[[ $label =~ ^[a-z][-a-z0-9]*$ ]]
deadline=$(date -d '2026-11-18 06:10:00' +%s)
seconds_left=$((deadline - $(date +%s)))
(( seconds_left >= 615 ))
[[ ! -e "$evidence/$label" && ! -e "$evidence/$label.log" ]]
( set -o noclobber; date -Is > "$evidence/$label.log" )

qd_accept() {
    env -u DISPLAY PATH="$PWD/$evidence/bin:$PATH" QT2B_DEADLINE_EPOCH="$deadline" \
        QT2B_FIRST_TIMER="$first" \
        timeout --foreground --signal=TERM "${seconds_left}s" \
        systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p Delegate=yes -- \
        taskset -c 16-19 python3 -IB components/vp-direct/qa/live_driver/driver.py "$@"
}

rc=0
qd_accept --display "$display" accept/t2/LIVE_t2.py --backend cdp --apps "$apps" --arm baseline \
    --warmups 1 --wake-samples 5 --controls "$evidence/measurement-controls.json" \
    --receipt-dir "$evidence/$label" >> "$evidence/$label.log" 2>&1 || rc=$?
date -Is >> "$evidence/$label.log"
printf 'rc=%s\n' "$rc" >> "$evidence/$label.log"
exit "$rc"
