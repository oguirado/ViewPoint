#!/usr/bin/env bash
# Lane U: entrypoint-cu.sh preflight runs before dbus/Xvfb and exits 4 with a distinct token (stubbed id, scratch dirs).
set -u
here="$(cd "$(dirname "$0")" && pwd)"
EP="$here/../entrypoint-cu.sh"
SCR="$(mktemp -d "${TMPDIR:-/tmp}/oscu-ep.XXXXXX")"
trap 'rm -rf "$SCR"' EXIT
fail=0
bash -n "$EP" || { echo "FAIL syntax"; exit 5; }

mkstub() { # $1 = uid printed by `id -u`; dbus-daemon stub aborts the run if the preflight did not stop earlier
  mkdir -p "$SCR/bin"
  printf '#!/bin/sh\n[ "$1" = "-u" ] && echo %s || echo 0\n' "$1" >"$SCR/bin/id"
  printf '#!/bin/sh\necho REACHED_DBUS >&2\nexit 9\n' >"$SCR/bin/dbus-daemon"
  chmod +x "$SCR/bin/id" "$SCR/bin/dbus-daemon"
}
run() { # $1 tenant dir, $2 fb dir ; prints stderr, returns the rc
  PATH="$SCR/bin:$PATH" TENANT_DIR="$1" FB_DIR="$2" VQA_ISO=00000000000000000000000000000000 timeout 20 bash "$EP" 2>"$SCR/err"
}
expect() { # name want_rc want_token
  local rc=$?
  if [ "$rc" -eq "$2" ] && grep -q "$3" "$SCR/err" && ! grep -q REACHED_DBUS "$SCR/err"; then echo "ok   $1"; else echo "FAIL $1 rc=$rc"; sed 's/^/     /' "$SCR/err"; fail=1; fi
}

mkdir -p "$SCR/tenant" "$SCR/fb"

mkstub 1000; run "$SCR/tenant" "$SCR/fb"; expect entrypoint_refuses_uid_1000 4 uid_mismatch
mkstub 0; run "$SCR/tenant" "$SCR/fb"; expect entrypoint_refuses_uid_0 4 uid_mismatch
mkstub 10001; run /usr "$SCR/fb"; expect entrypoint_refuses_root_tenant 4 tenant_not_owned
mkstub 10001; chmod 0500 "$SCR/fb"; run "$SCR/tenant" "$SCR/fb"; r=$?; chmod 0700 "$SCR/fb"; (exit $r); expect entrypoint_refuses_unwritable_fb 4 fb_not_writable
mkstub 10001; run "$SCR/tenant" "$SCR/nofb"; expect entrypoint_refuses_missing_fb 4 fb_not_writable
# preflight passed: the run proceeds to dbus (stub exits 9, which is the proof it got past the preflight)
mkstub 10001; run "$SCR/tenant" "$SCR/fb"; rc=$?
if grep -q REACHED_DBUS "$SCR/err"; then echo "ok   entrypoint_preflight_passes_for_10001"; else echo "FAIL preflight_passes rc=$rc"; sed 's/^/     /' "$SCR/err"; fail=1; fi
# no chmod of the tenant dir, no `|| true` swallow, no docker user flag in the entrypoint
if grep -nE 'chmod[^#]*\|\| *true' "$EP" || grep -nE -- '-{2}user' "$EP"; then echo "FAIL forbidden construct"; fail=1; else echo "ok   entrypoint_no_chmod_swallow"; fi
[ "$fail" -eq 0 ] && exit 0
exit 5
