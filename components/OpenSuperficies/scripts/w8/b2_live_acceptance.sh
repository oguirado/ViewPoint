#!/bin/sh
trap 'rc=$?; [ "$rc" -eq 1 ] && exit 5; exit "$rc"' EXIT
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/osworker-lib.inc"
usage() { printf '%s\n' 'usage: b2_live_acceptance.sh --plan | --run <1..11>'; }
[ "$#" -gt 0 ] || { usage; exit 2; }
if [ "$1" = --plan ] && [ "$#" -eq 1 ]; then
    cat <<'PLAN'
1. sudo osworker-verify.sh: VERIFY=PASS (requires the approved base for its probe).
2. Operator-approved tar context -> installed builder build -: equal reproducible OCI digests, valid record.
3. Hardened ephemeral id probe: uid/gid 10001; no network, binds, devices or mutable root.
4. Detached bounded probe + independent inspect: C1 subset, then removal even on failure.
5. Latest base_image_records.jsonl line -> record-check: rc0.
6. Unique os-worker-base image IDs: at most current plus rollback.
7. L1..L9 recipe lint rc5 + unapproved build rc7: image IDs unchanged.
8. Replayed approval rc7; held builder flock rc4 (no build performed).
9. Destructive rollback -> verify rc5; setup -> verify PASS (images retained).
10. Kill off -> rootless info fails, no osworker processes; on -> verify PASS; rootful remains active.
11. One verify probe -> exactly one new W8_CAPSULE_CREATED journal line.
The conductor runs individual steps after operator setup + per-build approval; agents never execute this list.
All Docker client commands use taskset -c 10-14; every probe has memory=memswap=4g and cpuset=10-14.
PLAN
    exit 0
fi
[ "$#" -eq 2 ] && [ "$1" = --run ] || { usage; exit 2; }
case $2 in 1|2|3|4|5|6|7|8|9|10|11) STEP=$2;; *) usage; exit 2;; esac
root_only; uid_lookup
REPO=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
BUILDER=${OSW_BUILDER:-/usr/local/libexec/opensuperficies/srf-w8-builder}
STATE="$OSW_HOME/.local/state/w8"
# No rootful Docker command, context, socket or daemon operation is used here.
docker_osw() { user_run taskset -c 10-14 docker --host "unix://$OSW_RUN_ROOT/user/$OSW_UID/docker.sock" "$@"; }
builder_osw() {
    sudo -u osworker env "XDG_RUNTIME_DIR=$OSW_RUN_ROOT/user/$OSW_UID" "DOCKER_HOST=unix://$OSW_RUN_ROOT/user/$OSW_UID/docker.sock" taskset -c 10-14 "$BUILDER" "$@"
}
build_context() { tar -c -C "$REPO/.wavemind/env" . | builder_osw build "$@" -; }
expect_rc() {
    want=$1; shift
    if "$@"; then got=0; else got=$?; fi
    [ "$got" -eq "$want" ] || fail 5 "expected rc=$want got=$got"
}
image_ids() {
    ids=$(docker_osw images os-worker-base --no-trunc -q) || return 5
    [ -n "$ids" ] || return 5
    printf '%s\n' "$ids" | sort -u
}
case $STEP in
 1|3) sh "$SCRIPT_DIR/osworker-verify.sh" ;;
 2) build_context ;;
 4)
    IMAGE=$(docker_osw image inspect os-worker-base:current --format '{{.Id}}')
    CID=$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')
    [ "${#CID}" -eq 12 ] || fail 5 'probe id unavailable'
    NAME="$CID"
    user_run systemd-cat -t w8-capsule <<EOF || fail 4 'journal unavailable'
W8_CAPSULE_CREATED kind=probe lease=- image=$IMAGE container=$CID caller_uid=$(id -u)
EOF
    # An EXIT cleanup is installed only after argument validation. Keep never-1 normalization.
    cleanup() { rc=$?; trap - EXIT; if ! docker_osw rm -f "$NAME" >/dev/null; then rc=6; fi; [ "$rc" -eq 1 ] && rc=5; exit "$rc"; }
    trap cleanup EXIT
    docker_osw run -d --name "$NAME" --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 64 --memory 4g --memory-swap 4g --cpuset-cpus 10-14 --oom-score-adj 800 --ulimit nofile=8192:8192 --ulimit core=0 "$IMAGE" sleep 60
    docker_osw inspect "$NAME" | python3 -c 'import json,sys
try:
 d=json.load(sys.stdin)[0];h=d["HostConfig"]
 ok=(h["ReadonlyRootfs"] and h["CapDrop"]==["ALL"] and "no-new-privileges:true" in h["SecurityOpt"] and not h["Privileged"] and h["NetworkMode"]=="none" and not h.get("Binds") and not h.get("Devices") and not d.get("Mounts") and h["Memory"]==h["MemorySwap"]==4294967296 and h["PidsLimit"]==64 and h["CpusetCpus"]=="10-14" and h["OomScoreAdj"]>=500 and d["Config"]["User"]=="10001:10001" and d["Config"]["Entrypoint"]==["/usr/bin/tini","-s","--"] and any(x["Name"]=="nofile" and x["Soft"]==8192 and x["Hard"]==8192 for x in h["Ulimits"]))
 print("C1_SUBSET="+("PASS" if ok else "FAIL"));sys.exit(0 if ok else 5)
except (ValueError,KeyError,IndexError,TypeError):sys.exit(5)'
    ;;
 5) tail -n 1 "$STATE/base_image_records.jsonl" | builder_osw record-check - ;;
 6)
    IDS=$(image_ids); [ -n "$IDS" ] || fail 5 'no base images'
    COUNT=$(printf '%s\n' "$IDS" | wc -l)
    [ "$COUNT" -le 2 ] || fail 5 'more than two retained image ids'
    printf 'retained=%s\n' "$COUNT"
    ;;
 7)
    BEFORE=$(image_ids)
    SCRATCH="$REPO/build/room_B2A/live"
    mkdir -p "$SCRATCH"
    for leak in 1 2 3 4 5 6 7 8 9; do
        DEST="$SCRATCH/L$leak"
        [ ! -e "$DEST" ] || fail 2 'live leak scratch already exists; inspect it before removal'
        mkdir "$DEST"; cp -R "$REPO/.wavemind/env/." "$DEST/"
        cp "$REPO/testdata/w8builder/leak/L$leak.Dockerfile" "$DEST/base/Dockerfile"
        expect_rc 5 taskset -c 10-14 "$BUILDER" lint --recipe-dir "$DEST"
    done
    expect_rc 7 build_context --approval-file "$REPO/testdata/w8builder/unapproved.json"
    [ "$(image_ids)" = "$BEFORE" ] || fail 5 'negative tests changed images'
    ;;
 8)
    expect_rc 7 build_context
    # flock holds the same per-user lock while a separate builder opens it.
    tar -c -C "$REPO/.wavemind/env" . | expect_rc 4 sudo -u osworker env "XDG_RUNTIME_DIR=$OSW_RUN_ROOT/user/$OSW_UID" "DOCKER_HOST=unix://$OSW_RUN_ROOT/user/$OSW_UID/docker.sock" flock -x "$STATE/builder.lock" taskset -c 10-14 "$BUILDER" build -
    ;;
 9)
    sh "$SCRIPT_DIR/osworker-setup.sh" --rollback
    expect_rc 5 sh "$SCRIPT_DIR/osworker-verify.sh"
    sh "$SCRIPT_DIR/osworker-setup.sh"
    sh "$SCRIPT_DIR/osworker-verify.sh"
    ;;
 10)
    sh "$SCRIPT_DIR/osworker-kill.sh" off
    # Probe from root only: systemd-run --machine=osworker@ --user would restart the user manager (and docker) itself.
    SOCK="$OSW_RUN_ROOT/user/$OSW_UID/docker.sock"
    if [ -S "$SOCK" ] && docker --host "unix://$SOCK" info >/dev/null 2>&1; then fail 5 'rootless daemon reachable while off'; fi
    if pgrep -u osworker >/dev/null; then fail 5 'osworker processes remain'; else rc=$?; [ "$rc" -eq 1 ] || fail 5 'process query error'; fi
    systemctl is-active --quiet docker || fail 5 'rootful daemon not active'
    sh "$SCRIPT_DIR/osworker-kill.sh" on
    ;;
 11)
    CURSOR=$(journalctl "_UID=$OSW_UID" -t w8-capsule --no-pager -n 0 --show-cursor | sed -n 's/^-- cursor: //p')
    [ -n "$CURSOR" ] || fail 4 'journal cursor unavailable'
    sh "$SCRIPT_DIR/osworker-verify.sh"
    LINES=$(journalctl "_UID=$OSW_UID" -t w8-capsule --after-cursor="$CURSOR" --no-pager -o cat | grep -c '^W8_CAPSULE_CREATED kind=probe ')
    [ "$LINES" -eq 1 ] || fail 5 'expected exactly one new probe journal line; serialize live acceptance'
    ;;
esac
printf 'LIVE_ITEM_%s=PASS\n' "$STEP"
