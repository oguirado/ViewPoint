#!/usr/bin/env bash
# Lane P: the capsule entrypoint sets umask 027 before Xvfb, XVFB_CURSOR is a closed on|off switch, and
# exec-umask.sh gives docker-exec'd processes the same umask. Stubs only (no Xvfb, no docker).
set -u
here="$(cd "$(dirname "$0")" && pwd)"
EP="${EP_OVERRIDE:-$here/../entrypoint-cu.sh}"  # EP_OVERRIDE: failing-first run against the pre-W3 entrypoint
EX="$here/../exec-umask.sh"
SCR="$(mktemp -d "${TMPDIR:-/tmp}/oscu-um.XXXXXX")"
trap 'rm -rf "$SCR"' EXIT
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }

bash -n "$EP" && bash -n "$EX" && ok umask_scripts_bash_n || bad umask_scripts_bash_n

# static: `umask 027` is a top-level statement that precedes the Xvfb launch
ul="$(grep -nE '^umask 027\b' "$EP" | head -1 | cut -d: -f1)"
xl="$(grep -nE '^Xvfb ' "$EP" | head -1 | cut -d: -f1)"
if [ -n "$ul" ] && [ -n "$xl" ] && [ "$ul" -lt "$xl" ]; then ok entrypoint_sets_umask_027; else bad "entrypoint_sets_umask_027 (umask line=$ul Xvfb line=$xl)"; fi

# runtime: the first daemon the entrypoint starts inherits umask 0027 (id stubbed to the capsule uid)
mkdir -p "$SCR/bin" "$SCR/tenant" "$SCR/fb"
printf '#!/bin/sh\n[ "$1" = "-u" ] && echo 10001 || echo 0\n' >"$SCR/bin/id"
printf '#!/bin/sh\numask >"%s/umask.txt"\nexit 9\n' "$SCR" >"$SCR/bin/dbus-daemon"
chmod +x "$SCR/bin/id" "$SCR/bin/dbus-daemon"
PATH="$SCR/bin:$PATH" TENANT_DIR="$SCR/tenant" FB_DIR="$SCR/fb" VQA_ISO=00000000000000000000000000000000 timeout 20 bash "$EP" 2>"$SCR/err"
if [ "$(cat "$SCR/umask.txt" 2>/dev/null)" = "0027" ]; then ok entrypoint_runtime_umask_0027; else bad "entrypoint_runtime_umask_0027 got '$(cat "$SCR/umask.txt" 2>/dev/null)'"; fi

# XVFB_CURSOR: closed enum, refused before any daemon with exit 2
rm -f "$SCR/umask.txt"
PATH="$SCR/bin:$PATH" XVFB_CURSOR=maybe TENANT_DIR="$SCR/tenant" FB_DIR="$SCR/fb" VQA_ISO=00000000000000000000000000000000 timeout 20 bash "$EP" 2>"$SCR/err"; rc=$?
if [ "$rc" -eq 2 ] && grep -q xvfb_cursor_invalid "$SCR/err" && [ ! -e "$SCR/umask.txt" ]; then ok xvfb_cursor_closed_enum; else bad "xvfb_cursor_closed_enum rc=$rc"; fi
grep -qF 'CURSOR_ARGS=(-nocursor)' "$EP" && grep -qF 'CURSOR_ARGS=()' "$EP" && ok xvfb_cursor_default_nocursor || bad xvfb_cursor_default_nocursor

# exec wrapper: umask 0027 in the child, exit 2 without a command, the child's rc is preserved
[ "$(bash "$EX" sh -c umask)" = "0027" ] && ok exec_umask_child_0027 || bad exec_umask_child_0027
bash "$EX" >/dev/null 2>&1; [ $? -eq 2 ] && ok exec_umask_usage_rc2 || bad exec_umask_usage_rc2
bash "$EX" sh -c 'exit 5'; [ $? -eq 5 ] && ok exec_umask_preserves_rc || bad exec_umask_preserves_rc
( umask 000; f="$SCR/probe"; bash "$EX" sh -c ": >\"$f\""; [ "$(stat -c %a "$f")" = "640" ] ) && ok exec_umask_file_mode_640 || bad exec_umask_file_mode_640

[ "$fail" -eq 0 ] && exit 0
exit 5
