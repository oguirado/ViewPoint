#!/usr/bin/env bash
# In-capsule diagnosis (staged to /opt/cu/diag/, run through exec-umask.sh).  Every probe is bounded by `timeout` and a byte cap;
# the whole output stays <= 32 KB.  `capsule-diag.sh --classify RC` prints the VG-0223 exit class of a chromium rc.  Exit 0 (2 on usage), never 1.
set -u
trap 'exit 5' ERR
TMO="${CU_DIAG_TMO:-5}"; TMPB="${CU_TMP:-/tmp}"; CAP=4096; MAXTOTAL=32768

# VG-0223 exit classification: SIGKILL first (uncatchable, no fault record), then fault signals, then other signals, then exit codes
classify_rc() {
  local rc="$1"
  [[ "$rc" =~ ^[0-9]+$ ]] || { echo Unknown; return 0; }
  if [ "$rc" -eq 0 ]; then echo Clean
  elif [ "$rc" -eq 137 ]; then echo SilentKill
  elif [ "$rc" -gt 128 ] && [ "$rc" -lt 193 ]; then
    case $((rc - 128)) in 4|6|7|8|11) echo Fault ;; *) echo OtherSignal ;; esac
  else echo ExitError; fi
}
if [ "${1:-}" = "--classify" ]; then [ $# -eq 2 ] || { echo "usage: capsule-diag.sh [--classify RC]" >&2; exit 2; }; classify_rc "$2"; exit 0; fi
[ $# -eq 0 ] || { echo "usage: capsule-diag.sh [--classify RC]" >&2; exit 2; }

section() { # TITLE LINES CMD...: bounded probe, output capped per section
  local title="$1" lines="$2"; shift 2
  echo "== $title"
  timeout "$TMO" "$@" 2>&1 | head -n "$lines" | head -c "$CAP" || true
  echo
}
run() {
  export DISPLAY="${DISPLAY:-:100}"
  section xdpyinfo_xfixes 20 sh -c 'xdpyinfo | grep -i -E "xfixes|XTEST|RENDER|version number"'
  section xwininfo_root_tree 60 xwininfo -root -tree
  section chromium_ps 20 sh -c 'ps -eo pid,ppid,stat,etimes,args | grep -i [c]hrom'
  section chromium_version 3 chromium --version
  section fc_list_count 3 sh -c 'fc-list | wc -l'
  section cursor_env 10 sh -c 'echo "HOME=${HOME:-} XCURSOR_PATH=${XCURSOR_PATH:-} XCURSOR_THEME=${XCURSOR_THEME:-} XCURSOR_SIZE=${XCURSOR_SIZE:-}"'
  section writable 10 sh -c 'for d in "${HOME:-/nonexistent}" /tmp; do if [ -w "$d" ]; then echo "$d writable"; else echo "$d NOT writable"; fi; done'
  section cursor_font 10 sh -c 'ls -d /usr/share/icons/*/cursors /usr/share/icons/default 2>/dev/null; fc-list 2>/dev/null | grep -ci cursor; find /usr/share/fonts /usr/share/X11/fonts -maxdepth 3 -iname "*cursor*" 2>/dev/null | head -n 5'
  echo "== chromium_death_class"
  local rcf="$TMPB/chromium-raw.rc" raw
  if [ -r "$rcf" ]; then raw="$(head -c 8 "$rcf" | tr -dc '0-9')"; echo "raw_rc=$raw class=$(classify_rc "$raw")"; else echo "raw_rc=absent"; fi
}
run 2>&1 | head -c "$MAXTOTAL"
exit 0
