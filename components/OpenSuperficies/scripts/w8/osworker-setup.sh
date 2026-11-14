#!/bin/sh
trap 'rc=$?; [ "$rc" -eq 1 ] && exit 5; exit "$rc"' EXIT
set -eu
trap 'rc=$?; trap - EXIT; if [ -n "${seat_tmp:-}" ]; then rm -f -- "$seat_tmp"; fi; case "$rc" in 0|2|3|4|5|6|7) ;; 1|124) rc=5;; *) rc=6;; esac; exit "$rc"' EXIT
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/osworker-lib.inc"
rollback=no; purge=no; seat_bin=; seat_sha=
while [ "$#" -gt 0 ]; do
    case $1 in
        --rollback) rollback=yes; shift ;;
        --purge-home) purge=yes; shift ;;
        --seat-bin) [ "$#" -ge 2 ] || fail 2 'missing seat binary'; seat_bin=$2; shift 2 ;;
        --seat-sha256) [ "$#" -ge 2 ] || fail 2 'missing seat sha256'; seat_sha=$2; shift 2 ;;
        *) fail 2 'usage: osworker-setup.sh --seat-bin P --seat-sha256 H | --rollback [--purge-home]' ;;
    esac
done
[ "$purge" = no ] || [ "$rollback" = yes ] || fail 2 'purge requires rollback'
if [ "$rollback" = no ]; then
    [ -n "$seat_bin" ] && [ -f "$seat_bin" ] && [ ! -L "$seat_bin" ] && [ -x "$seat_bin" ] || fail 2 'conductor-built regular seat binary required'
    printf '%s\n' "$seat_sha" | LC_ALL=C grep -Eq '^[0-9a-f]{64}$' || fail 2 'seat sha256 required'
    actual_sha=$(sha256sum -- "$seat_bin"); actual_sha=${actual_sha%% *}
    [ "$actual_sha" = "$seat_sha" ] || fail 5 'seat binary sha256 mismatch'
    [ -f "$SCRIPT_DIR/w8-seat.service" ] && [ ! -L "$SCRIPT_DIR/w8-seat.service" ] || fail 4 'seat unit absent'
    profile="$SCRIPT_DIR/../../src/w8seat/w8-seccomp.json"
    [ -f "$profile" ] && [ ! -L "$profile" ] || fail 4 'seccomp profile absent'
else
    [ -z "$seat_bin$seat_sha" ] || fail 2 'binary options do not apply to rollback'
fi
root_only
if [ "$rollback" = yes ]; then
    if getent passwd osworker >/dev/null; then
        uid_lookup
        user_run systemctl --user disable --now w8-seat.service
        user_run systemctl --user disable --now docker
        user_run dockerd-rootless-setuptool.sh uninstall
        loginctl disable-linger osworker
        systemctl stop "user@$OSW_UID.service"
        gpasswd -d osworker kvm
    fi
    for f in "$OSW_ETC/systemd/system/user@.service.d/delegate.conf" "$OSW_ETC/tmpfiles.d/opensuperficies-w8.conf" "$OSW_HOME/.config/systemd/user/docker.service.d/w8.conf"; do
        if [ -f "$f" ] && grep -qx '# w8-osworker' "$f"; then rm -- "$f"; fi
    done
    # Remove only the owned seat endpoint before the empty-directory assertion.
    seat_socket="$OSW_RUN_ROOT/opensuperficies/w8-seat.sock"
    [ ! -L "$seat_socket" ] || fail 5 'refusing socket symlink'
    if [ -S "$seat_socket" ]; then rm -- "$seat_socket"; fi
    if [ -f "$OSW_HOME/.config/systemd/user/w8-seat.service" ] && grep -qx '# w8-osworker' "$OSW_HOME/.config/systemd/user/w8-seat.service"; then
        rm -- "$OSW_HOME/.config/systemd/user/w8-seat.service"
    fi
    # Installed operator files are marked by their dedicated exact names.
    for installed in "${OSW_ROOT:-}/usr/local/libexec/opensuperficies/srf-w8-seat" "${OSW_ROOT:-}/usr/local/share/opensuperficies/w8-seccomp.json" "$OSW_ETC/opensuperficies/w8_cpus.json"; do
        [ ! -L "$installed" ] || fail 5 'refusing installed symlink'
        if [ -f "$installed" ]; then rm -- "$installed"; fi
    done
    # Only remove the B3 runtime directory if it is empty; never recurse into it.
    if [ -d "$OSW_RUN_ROOT/opensuperficies" ]; then rmdir -- "$OSW_RUN_ROOT/opensuperficies"; fi
    systemctl daemon-reload
    for db in subuid subgid; do
        if [ -f "$OSW_ETC/$db" ]; then
            sed '/^osworker:165536:65536$/d' "$OSW_ETC/$db" > "$OSW_ETC/$db.w8-tmp"
            cat "$OSW_ETC/$db.w8-tmp" > "$OSW_ETC/$db"; rm -- "$OSW_ETC/$db.w8-tmp"
        fi
    done
    if getent passwd osworker >/dev/null; then
        if [ "$purge" = yes ]; then userdel -r osworker; else userdel osworker; fi
    fi
    exit 0
fi
[ "$(uname -m)" = aarch64 ] || fail 2 'aarch64 required'
check_controllers "$OSW_CGROUP_ROOT/cgroup.controllers" || fail 2 'root cpuset memory pids required'
# Version match only: this script never upgrades the existing rootful daemon.
V=$(dpkg-query -W -f='${Version}' docker-ce) || fail 4 'docker-ce version unavailable'
[ -n "$V" ] || fail 4 'docker-ce version unavailable'
DOCKER_RELEASE=${V#*:}
case $DOCKER_RELEASE in 29.6.2|29.6.2-*) :;; *) fail 4 'operator must upgrade rootful docker-ce to 29.6.2 when idle before setup';; esac
AVAILABLE=$(apt-cache madison docker-ce-rootless-extras) || fail 4 'rootless-extras index unavailable'
if ! printf '%s\n' "$AVAILABLE" | awk -F'|' -v v="$V" '{gsub(/^[ \t]+|[ \t]+$/, "", $2); if ($2==v) ok=1} END {exit !ok}'; then
    printf 'Matching rootless-extras %s unavailable. Operator options:\n1. Supply the matching package from the Docker archive.\n2. Separately approve a rootful Docker upgrade, then rerun setup.\n' "$V" >&2
    exit 4
fi
# Validate preexisting ranges before any package/account mutation.
for db in subuid subgid; do
    if [ -f "$OSW_ETC/$db" ]; then
        awk -F: '$1=="osworker" {n++; if($2!=165536 || $3!=65536) bad=1} $1!="osworker" && $2<231072 && ($2+$3)>165536 {bad=1} END{exit (bad || n>1)}' "$OSW_ETC/$db" || fail 5 'subid conflict'
    fi
done
apt-get install -y --no-install-recommends uidmap dbus-user-session slirp4netns "docker-ce-rootless-extras=$V"
if ! getent passwd osworker >/dev/null; then
    useradd --system --create-home --home-dir /var/lib/osworker --shell /bin/sh --user-group osworker
fi
usermod -p '*' osworker
for forbidden in docker sudo adm; do
    if id -nG osworker | tr ' ' '\n' | grep -qx "$forbidden"; then fail 5 "osworker belongs to $forbidden"; fi
done
getent group kvm >/dev/null || fail 4 'kvm group unavailable'
getent group admin >/dev/null || fail 4 'admin group unavailable'
# The seat may chgrp its own socket to admin; no Docker socket ACL is granted.
need_group_refresh=no
if ! id -nG osworker | tr ' ' '\n' | grep -qx admin; then need_group_refresh=yes; fi
usermod -aG kvm,admin osworker
# Add only the missing half, avoiding duplicate allocation on repair/rerun.
for db in subuid subgid; do
    if ! grep -qx 'osworker:165536:65536' "$OSW_ETC/$db" 2>/dev/null; then
        case $db in subuid) usermod --add-subuids 165536-231071 osworker;; subgid) usermod --add-subgids 165536-231071 osworker;; esac
    fi
done
check_ranges || fail 5 'subuid/subgid verification failed'
uid_lookup
install -d -m 0755 "$OSW_ETC/systemd/system/user@.service.d" "$OSW_ETC/tmpfiles.d"
marker_write "$OSW_ETC/systemd/system/user@.service.d/delegate.conf" '[Service]
Delegate=cpu cpuset io memory pids'
systemctl daemon-reload
loginctl enable-linger osworker
# Operator setup is maintenance: refresh the dedicated manager after adding
# its socket group. Existing B2 processes cannot acquire new supplementary groups.
if [ "$need_group_refresh" = yes ]; then systemctl restart "user@$OSW_UID.service"; fi
wait_bus || fail 5 'user bus unavailable after bounded wait'
CG="$OSW_CGROUP_ROOT/user.slice/user-$OSW_UID.slice/user@$OSW_UID.service/cgroup.controllers"
if ! check_controllers "$CG"; then
    systemctl restart "user@$OSW_UID.service"
    check_controllers "$CG" || fail 5 'delegation unavailable; operator must qualify dedicated-user fallback (never root)'
fi
user_run dockerd-rootless-setuptool.sh install --skip-iptables
# install sets ownership even when repairing directories that already exist.
for d in .config .config/systemd .config/systemd/user .config/systemd/user/docker.service.d .local .local/state .local/state/w8; do
    [ ! -L "$OSW_HOME/$d" ] || fail 5 "refusing home symlink: $d"
    install -d -m 0700 -o osworker -g osworker "$OSW_HOME/$d"
done
marker_write "$OSW_HOME/.config/systemd/user/docker.service.d/w8.conf" '[Service]
CPUAffinity=10-14
Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_NET=slirp4netns
ExecStart=
ExecStart=/usr/bin/dockerd-rootless.sh --host=unix://%t/docker.sock --icc=false --no-new-privileges --userland-proxy=false'
chown osworker:osworker "$OSW_HOME/.config/systemd/user/docker.service.d/w8.conf"
chmod 0644 "$OSW_HOME/.config/systemd/user/docker.service.d/w8.conf"
marker_write "$OSW_ETC/tmpfiles.d/opensuperficies-w8.conf" 'd /run/opensuperficies 0750 osworker admin -'
systemd-tmpfiles --create "$OSW_ETC/tmpfiles.d/opensuperficies-w8.conf"
[ "$(stat -c %a "$OSW_RUN_ROOT/user/$OSW_UID")" = 700 ] || fail 5 'socket directory must be 0700'
# Refuse inherited access ACLs; no extra permissions are granted here.
if command -v getfacl >/dev/null 2>&1; then
    ACL=$(getfacl -cp "$OSW_RUN_ROOT/user/$OSW_UID")
    printf '%s\n' "$ACL" | grep -Eq '^(user:[^:]|group:[^:]|default:)' && fail 5 'socket directory has access ACLs'
else
    fail 4 'getfacl unavailable; operator must install acl for access verification'
fi
install -d -m 0755 "$OSW_ETC/opensuperficies" "${OSW_ROOT:-}/usr/local/libexec/opensuperficies" "${OSW_ROOT:-}/usr/local/share/opensuperficies"
for dst in "${OSW_ROOT:-}/usr/local/libexec/opensuperficies/srf-w8-seat" "${OSW_ROOT:-}/usr/local/share/opensuperficies/w8-seccomp.json" "$OSW_ETC/opensuperficies/w8_cpus.json"; do
    [ ! -L "$dst" ] || fail 5 'refusing seat installation symlink'
done
seat_tmp=$(mktemp "${OSW_ROOT:-}/usr/local/libexec/opensuperficies/.srf-w8-seat.XXXXXX")
install -m 0555 -o root -g root -- "$seat_bin" "$seat_tmp"
installed_sha=$(sha256sum -- "$seat_tmp"); installed_sha=${installed_sha%% *}
[ "$installed_sha" = "$seat_sha" ] || fail 5 'installed binary differs'
mv -f -- "$seat_tmp" "${OSW_ROOT:-}/usr/local/libexec/opensuperficies/srf-w8-seat"
seat_tmp=
install -m 0444 -o root -g root "$profile" "${OSW_ROOT:-}/usr/local/share/opensuperficies/w8-seccomp.json"
# Operator-owned partition; the seat also compares worker with daemon CPUAffinity.
python3 - "${OSW_ROOT:-}/sys/devices/system/cpu/online" "$OSW_ETC/opensuperficies/w8_cpus.json" <<'PYCPU'
import json,os,sys,tempfile
try:
    online=set()
    for item in open(sys.argv[1]).read().strip().split(','):
        lo,sep,hi=item.partition('-'); a=int(lo); b=int(hi) if sep else a
        if a<0 or b<a or b>65535: raise ValueError()
        online.update(range(a,b+1))
    worker=set(range(10,15)); interactive=online-worker
    if not worker<=online or not interactive: raise ValueError()
    dest=sys.argv[2]; fd,tmp=tempfile.mkstemp(prefix='.w8-cpus-',dir=os.path.dirname(dest))
    try:
        with os.fdopen(fd,'w') as f:
            json.dump({'worker':'10-14','interactive':','.join(map(str,sorted(interactive)))},f)
            f.write('\n'); f.flush(); os.fsync(f.fileno()); os.fchmod(f.fileno(),0o444)
        os.replace(tmp,dest)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
except (OSError,ValueError): sys.exit(4)
PYCPU
install -m 0644 -o osworker -g osworker "$SCRIPT_DIR/w8-seat.service" "$OSW_HOME/.config/systemd/user/w8-seat.service"
user_run systemctl --user daemon-reload
user_run systemctl --user enable --now docker
user_run systemctl --user enable w8-seat.service
user_run systemctl --user restart w8-seat.service
printf 'SETUP=PASS\n'
