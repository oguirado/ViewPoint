#!/usr/bin/env bash
# docker exec spawns from containerd with umask 0022, not the entrypoint's: run every exec'd capsule process through
# this wrapper so its files carry no world bits either (AW3-07).  Usage: exec-umask.sh CMD [ARGS...]
set -euo pipefail
umask 027
[[ $# -gt 0 ]] || { echo "usage: exec-umask.sh CMD [ARGS...]" >&2; exit 2; }
# run as a child (not exec) so a bare rc 1 can be mapped to 5: SPEC_INT R4 forbids 1; every other rc is the child's own
rc=0
"$@" || rc=$?
[[ "$rc" -ne 1 ]] || rc=5
exit "$rc"
