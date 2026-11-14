#!/usr/bin/env bash
# Chromium launcher for the hardened CU capsule (lane C). Usage: launch-chromium-cu.sh [--sandbox-mode namespace|suid|nosandbox] URL
# URL must be file:///opt/cu/scenes/{v2,v3}/<[a-z0-9_]+>.html (a regular file whose realpath stays there). No CDP: actuation is XTEST/xdotool.
# Exit: 0 ok, 2 usage/refused input, 5 chromium failed (its rc 1 is mapped to 5), other chromium rc propagated.
# The raw chromium rc (before the 1 -> 5 map) is written to $TMPBASE/chromium-raw.rc for the WL-7 diagnosis (W4 R7, KL-26b).
set -euo pipefail
set -o errtrace
umask 027
# any unplanned failure (mkdir/mktemp on a full or read-only /tmp, ...) is rc 5 + token, never the shell default 1 (ADC-W3-05)
trap 'echo "launch-chromium-cu: E_LAUNCH failed at line $LINENO" >&2; exit 5' ERR

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLAGS="${CU_FLAGS_FILE:-$HERE/profiles/chromium.flags}"
ROOT="${CU_SCENES_ROOT:-}"   # empty: derived from the URL (v2 or v3 only) below; CU_* overrides exist for the stub tests only; the RunArgs allow-list cannot set them
TMPBASE="${CU_TMP:-/tmp}"
die() { echo "launch-chromium-cu: $*" >&2; exit 2; }

mode=""
while [ $# -gt 0 ]; do
  case "$1" in
    --sandbox-mode) [ $# -ge 2 ] || die "usage: --sandbox-mode needs a value"; mode="$2"; shift 2 ;;
    --) shift; break ;;
    -*) die "usage: unknown option $1" ;;
    *) break ;;
  esac
done
[ $# -eq 1 ] || die "usage: exactly one URL"
URL="$1"

[ -r "$FLAGS" ] || die "flags file missing"
declare -a base=() det=() sbflags=()
default_mode=""; mode_known=0
while read -r kind a b extra; do
  case "$kind" in
    ""|"#"*) continue ;;
  esac
  [ -z "${extra:-}" ] || die "flags file: extra token in record"
  case "$kind $a $b" in *remote''-debugging*) die "flags file: CDP flag forbidden" ;; esac  # split literal: the static no-CDP test greps this file
  case "$kind" in
    default_sandbox) default_mode="$a" ;;
    sandbox) [ -n "${mode:-}" ] || continue
             if [ "$a" = "$mode" ]; then mode_known=1; [ -z "${b:-}" ] || sbflags+=("$b"); fi ;;
    flag) case "$a" in base) base+=("$b") ;; det) det+=("$b") ;; *) die "flags file: unknown group $a" ;; esac ;;
    *) die "flags file: unknown record $kind" ;;
  esac
done <"$FLAGS"
[ -n "$default_mode" ] || die "flags file: no default_sandbox"
if [ -z "$mode" ]; then
  mode="$default_mode"
  # second pass for the default mode's flags
  while read -r kind a b extra; do
    [ "$kind" = sandbox ] && [ "$a" = "$mode" ] && { mode_known=1; [ -z "${b:-}" ] || sbflags+=("$b"); }
  done <"$FLAGS"
fi
[ "$mode_known" -eq 1 ] || die "sandbox mode not in the closed enum"

if [ -z "$ROOT" ]; then
  case "$URL" in
    file:///opt/cu/scenes/v2/*) ROOT=/opt/cu/scenes/v2 ;;
    file:///opt/cu/scenes/v3/*) ROOT=/opt/cu/scenes/v3 ;;
    *) die "url outside the scenes root" ;;
  esac
fi
# URL: exact prefix, one path segment, lowercase name; then realpath -e must be a regular file directly under ROOT
prefix="file://$ROOT/"
case "$URL" in "$prefix"*) ;; *) die "url outside the scenes root" ;; esac
name="${URL#"$prefix"}"
[[ "$name" =~ ^[a-z0-9_]+\.html$ ]] || die "url name not allowed"
rroot="$(realpath -e -- "$ROOT")" || die "scenes root missing"
target="$(realpath -e -- "$ROOT/$name")" || die "scene missing"
[ -f "$target" ] && [ "$(dirname -- "$target")" = "$rroot" ] || die "scene escapes the scenes root"

[[ "${GEOMETRY:-}" =~ ^([0-9]{3,5})x([0-9]{3,5})x[0-9]{1,2}$ ]] || die "GEOMETRY must be WxHxD"
SIZE="${BASH_REMATCH[1]},${BASH_REMATCH[2]}"

PROFILE=""
cleanup() { [ -z "$PROFILE" ] || rm -rf -- "$PROFILE"; }
trap cleanup EXIT
PROFILE="$(mktemp -d "$TMPBASE/chromium-$$.XXXXXX")"
mkdir -m 700 -- "$PROFILE/cfg" "$PROFILE/cache"
trap 'exit 143' INT TERM

export HOME="$PROFILE" XDG_CONFIG_HOME="$PROFILE/cfg" XDG_CACHE_HOME="$PROFILE/cache" MESA_SHADER_CACHE_DISABLE=true
export LANG=C.UTF-8 TZ=UTC FONTCONFIG_FILE="$ROOT/fonts/fonts.conf"

# receipt line: sandbox "none" means the capsule is the only boundary
rcpt="$mode"; [ "$mode" = nosandbox ] && rcpt=none
echo "{\"sandbox\":\"$rcpt\",\"mode\":\"$mode\",\"geometry\":\"$SIZE\"}" >&2

rc=0
chromium "${base[@]}" "${det[@]}" ${sbflags[@]+"${sbflags[@]}"} \
  --user-data-dir="$PROFILE" --disk-cache-dir="$PROFILE/cache" --window-size="$SIZE" "$URL" || rc=$?
printf '%s\n' "$rc" >"$TMPBASE/chromium-raw.rc" 2>/dev/null || true
[ "$rc" -ne 1 ] || rc=5
exit "$rc"
