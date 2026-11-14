# Shared operator-plane helpers. No operation occurs when sourced.
OSW_ETC=${OSW_ETC:-${OSW_ROOT:-}/etc}
OSW_HOME=${OSW_HOME:-${OSW_ROOT:-}/var/lib/osworker}
OSW_RUN_ROOT=${OSW_RUN_ROOT:-${OSW_ROOT:-}/run}
OSW_CGROUP_ROOT=${OSW_CGROUP_ROOT:-${OSW_ROOT:-}/sys/fs/cgroup}
fail() { printf '%s\n' "$2" >&2; exit "$1"; }
root_only() { [ "$(id -u)" = 0 ] || fail 2 'operator root required'; }
uid_lookup() { OSW_UID=$(id -u osworker); [ "$OSW_UID" -gt 0 ] || fail 5 'invalid osworker uid'; }
user_run() { systemd-run --machine=osworker@ --user --wait --pipe --quiet "$@"; }
user_docker() { user_run docker --host "unix://$OSW_RUN_ROOT/user/$OSW_UID/docker.sock" "$@"; }
wait_bus() {
    n=0
    while [ "$n" -lt 10 ]; do
        [ -S "$OSW_RUN_ROOT/user/$OSW_UID/bus" ] && return 0
        sleep 1; n=$((n + 1))
    done
    return 5
}
# Missing/duplicate/foreign/overlapping ranges never count as valid.
check_ranges() {
    for db in subuid subgid; do
        [ -f "$OSW_ETC/$db" ] || return 5
        awk -F: '
          $1=="osworker" { n++; if ($2!=165536 || $3!=65536) bad=1 }
          $1!="osworker" && $2<231072 && ($2+$3)>165536 {bad=1}
          END {exit !(n==1 && !bad)}' "$OSW_ETC/$db" || return 5
    done
}
check_controllers() {
    [ -f "$1" ] || return 5
    for c in cpuset memory pids; do
        tr ' ' '\n' < "$1" | grep -qx "$c" || return 5
    done
}
marker_write() {
    dest=$1; content=$2
    [ ! -L "$dest" ] || fail 5 "refusing symlink: $dest"
    if [ -e "$dest" ] && ! grep -qx '# w8-osworker' "$dest"; then
        fail 5 "refusing unmarked file: $dest"
    fi
    printf '# w8-osworker\n%s\n' "$content" > "$dest"
}
