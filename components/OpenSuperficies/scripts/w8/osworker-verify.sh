#!/bin/sh
trap 'rc=$?; [ "$rc" -eq 1 ] && exit 5; exit "$rc"' EXIT
set -eu
trap 'rc=$?; trap - EXIT; case "$rc" in 0|2|3|4|5|6|7) ;; 1|124) rc=5;; *) rc=6;; esac; exit "$rc"' EXIT
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/osworker-lib.inc"
[ "$#" -eq 0 ] || fail 2 'usage: osworker-verify.sh'
root_only
BAD=0
emit() { printf '%s=%s\n' "$1" "$2"; }
check() { emit "$1" "$2"; [ "$2" = "$3" ] || BAD=1; }
if ! getent passwd osworker >/dev/null; then emit killswitch off; emit VERIFY FAIL; exit 5; fi
uid_lookup; emit osworker_uid "$OSW_UID"
LOCKED=$(passwd -S osworker | awk '{print $2}')
if [ "$LOCKED" = L ] || [ "$LOCKED" = LK ]; then check passwd locked locked; else check passwd unlocked locked; fi
for db in subuid subgid; do
    v=$(awk -F: '$1=="osworker" {print $2 ":" $3}' "$OSW_ETC/$db" 2>/dev/null) || v=AUSENTE
    check "${db}_range" "$v" '165536:65536'
done
check_ranges || BAD=1
check kvm_group "$(id -nG osworker | tr ' ' '\n' | awk '$0=="kvm"{f=1}END{print f?"yes":"no"}')" yes
for forbidden in docker sudo adm; do
    if id -nG osworker | tr ' ' '\n' | grep -qx "$forbidden"; then BAD=1; fi
done
LINGER=$(loginctl show-user osworker --property=Linger --value) || LINGER=no
check linger "$LINGER" yes
if systemctl is-active --quiet "user@$OSW_UID.service"; then check killswitch on on; else check killswitch off on; fi
if check_controllers "$OSW_CGROUP_ROOT/user.slice/user-$OSW_UID.slice/user@$OSW_UID.service/cgroup.controllers"; then check delegated yes yes; else check delegated no yes; fi
ROOTFILES=$(find "$OSW_HOME" -xdev -ignore_readdir_race -path "$OSW_HOME/.local/share/docker" -prune -o -uid 0 -type f -print) || fail 5 'home ownership scan unavailable'
if [ -z "$ROOTFILES" ]; then ROOTCOUNT=0; else ROOTCOUNT=$(printf '%s\n' "$ROOTFILES" | wc -l | tr -d ' '); fi
check home_root_owned "$ROOTCOUNT" 0
MODE=$(stat -c %a "$OSW_RUN_ROOT/user/$OSW_UID") || MODE=AUSENTE
check sock_dir_mode "$MODE" 700
if command -v getfacl >/dev/null 2>&1; then
    ACL=$(getfacl -cp "$OSW_RUN_ROOT/user/$OSW_UID")
    if printf '%s\n' "$ACL" | grep -Eq '^(user:[^:]|group:[^:]|default:)'; then BAD=1; fi
else
    emit socket_acl AUSENTE; BAD=1
fi
if runuser -u admin -- python3 -c 'import socket,sys
s=socket.socket(socket.AF_UNIX)
s.settimeout(2)
try: s.connect(sys.argv[1])
except PermissionError: sys.exit(5)
except OSError: sys.exit(4)
else: sys.exit(0)' "$OSW_RUN_ROOT/user/$OSW_UID/docker.sock"; then
    check admin_can_reach yes no
else
    reach_rc=$?
    # Only EACCES proves the boundary. Missing socket is unavailable, not safe.
    if [ "$reach_rc" -eq 5 ]; then check admin_can_reach no no; else check admin_can_reach AUSENTE no; fi
fi
RUNMODE=$(stat -c '%a:%U:%G' "$OSW_RUN_ROOT/opensuperficies") || RUNMODE=AUSENTE
check run_dir "$RUNMODE" '750:osworker:admin'
if user_run systemctl --user is-active --quiet w8-seat.service; then check seat_service active active; else check seat_service inactive active; fi
SEATMODE=$(stat -c '%a:%U:%G' "$OSW_RUN_ROOT/opensuperficies/w8-seat.sock") || SEATMODE=AUSENTE
check seat_socket "$SEATMODE" '660:osworker:admin'
[ -S "$OSW_RUN_ROOT/opensuperficies/w8-seat.sock" ] || BAD=1
BINARYMODE=$(stat -c '%a:%U:%G' "${OSW_ROOT:-}/usr/local/libexec/opensuperficies/srf-w8-seat") || BINARYMODE=AUSENTE
check seat_binary "$BINARYMODE" '555:root:root'
PROFILEMODE=$(stat -c '%a:%U:%G' "${OSW_ROOT:-}/usr/local/share/opensuperficies/w8-seccomp.json") || PROFILEMODE=AUSENTE
check seccomp_profile "$PROFILEMODE" '444:root:root'
CPUMODE=$(stat -c '%a:%U:%G' "$OSW_ETC/opensuperficies/w8_cpus.json") || CPUMODE=AUSENTE
check cpu_file "$CPUMODE" '444:root:root'

if systemctl is-active --quiet docker; then check rootful_untouched active active; else check rootful_untouched inactive active; fi
CPUS=$(user_run systemctl --user show docker --property=CPUAffinity --value) || CPUS=AUSENTE
case $CPUS in '10 11 12 13 14'|10-14) check daemon_cpus 10-14 10-14;; *) check daemon_cpus "$CPUS" 10-14;; esac
if python3 - "$OSW_ETC/opensuperficies/w8_cpus.json" "$CPUS" <<'PYCPU'
import json,sys
try:
 def cpus(s):
  out=set()
  for part in s.replace(' ',',').split(','):
   if not part: continue
   a,sep,b=part.partition('-'); lo=int(a); hi=int(b) if sep else lo
   if lo<0 or hi<lo or hi>65535: raise ValueError()
   out.update(range(lo,hi+1))
  return out
 d=json.load(open(sys.argv[1])); w=cpus(d['worker']); i=cpus(d['interactive'])
 if set(d)!=set(('worker','interactive')) or not i or w!=cpus(sys.argv[2]) or w&i: sys.exit(5)
except (OSError,ValueError,KeyError,TypeError): sys.exit(5)
PYCPU
then check cpu_partition valid valid; else check cpu_partition invalid valid; fi

INFO=$(user_docker info --format '{{json .}}') || { emit daemon unavailable; emit VERIFY FAIL; exit 5; }
FIELDS=$(printf '%s\n' "$INFO" | python3 -c 'import json,sys
try:
 d=json.load(sys.stdin)
 print("rootless="+("yes" if any("rootless" in x for x in d.get("SecurityOptions",[])) else "no"))
 print("image_store="+("containerd" if any("containerd" in str(x) for x in d.get("DriverStatus",[])) else "other"))
 print("cgroup_version="+str(d.get("CgroupVersion","AUSENTE")))
except (ValueError,TypeError): sys.exit(5)') || fail 5 'invalid daemon info'
for pair in $FIELDS; do
    key=${pair%%=*}; value=${pair#*=}
    case $key in rootless) check "$key" "$value" yes;; image_store) check "$key" "$value" containerd;; cgroup_version) check "$key" "$value" 2;; esac
done
# The behavioural probe creates an ephemeral container. Journal first, fail closed.
if [ "$BAD" -eq 0 ]; then
    IMAGE=$(user_docker image inspect os-worker-base:current --format '{{.Id}}') || fail 5 'base image unavailable for verification probe'
    ENTRY=$(user_docker image inspect "$IMAGE" --format '{{json .Config.Entrypoint}}') || fail 5 'image entrypoint unavailable'
    check entrypoint "$ENTRY" '["/usr/bin/tini","-s","--"]'
    [ "$BAD" -eq 0 ] || fail 5 'unexpected entrypoint'
    CID=$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')
    [ "${#CID}" -eq 12 ] || fail 5 'probe identifier unavailable'
    user_run systemd-cat -t w8-capsule <<EOF || fail 4 'probe journal unavailable'
W8_CAPSULE_CREATED kind=probe lease=- image=$IMAGE container=$CID caller_uid=$(id -u)
EOF
    PROBE=$(user_docker run --rm --name "$CID" --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 64 --memory 4g --memory-swap 4g --cpuset-cpus 10-14 "$IMAGE" id) || { emit probe_run 5; emit VERIFY FAIL; exit 5; }
    case $PROBE in uid=10001*gid=10001*) emit probe_run 0;; *) emit probe_run 5; BAD=1;; esac
else
    emit probe_run AUSENTE
fi
if [ "$BAD" -eq 0 ]; then emit VERIFY PASS; else emit VERIFY FAIL; exit 5; fi
