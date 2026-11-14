#!/usr/bin/env bash
# Lane C: launch-chromium-cu.sh against a stub `chromium` on PATH that records argv/env/umask (no real browser, no X).
# Every guard test also asserts a positive run (rc 0 + recorded argv) so a do-nothing launcher cannot pass vacuously.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
L="$here/../launch-chromium-cu.sh"
SCR="$(mktemp -d "${TMPDIR:-/tmp}/oscu-lc.XXXXXX")"
trap 'rm -rf "$SCR"' EXIT
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1 ${2:-}"; fail=1; }

ROOT="$SCR/scenes/v2"
mkdir -p "$SCR/bin" "$ROOT/fonts" "$SCR/tmp"
echo '<html></html>' >"$ROOT/page_1.html"
ln -s /etc/hostname "$ROOT/evil.html"
cat >"$SCR/bin/chromium" <<'EOF'
#!/bin/sh
: >"$REC/argv"; for a in "$@"; do printf '%s\n' "$a" >>"$REC/argv"; done
printf 'umask=%s\nHOME=%s\nLANG=%s\nTZ=%s\nFONTCONFIG_FILE=%s\nXDG_CONFIG_HOME=%s\nXDG_CACHE_HOME=%s\nMESA=%s\n' \
  "$(umask)" "$HOME" "$LANG" "$TZ" "$FONTCONFIG_FILE" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$MESA_SHADER_CACHE_DISABLE" >"$REC/env"
for a in "$@"; do case "$a" in --user-data-dir=*) d="${a#--user-data-dir=}"; echo "$d" >"$REC/udd"; [ -d "$d" ] && echo yes >"$REC/udd_existed";; esac; done
exit "${STUB_RC:-0}"
EOF
chmod +x "$SCR/bin/chromium"

# run URL [extra args...] ; sets rc ; REC is fresh per call
run() {
  REC="$SCR/rec"; rm -rf "$REC"; mkdir -p "$REC"
  PATH="$SCR/bin:$PATH" REC="$REC" CU_SCENES_ROOT="$ROOT" CU_TMP="$SCR/tmp" GEOMETRY=1920x1080x24 \
    timeout 20 bash "$L" "$@" 2>"$SCR/err" >"$SCR/out"
  rc=$?
}
has() { grep -qxF -- "$1" "$SCR/rec/argv" 2>/dev/null; }
good="file://$ROOT/page_1.html"

# launcher_bash_n (+ positive run, so a stub cannot pass)
if bash -n "$L"; then run "$good"; if [ "$rc" -eq 0 ] && [ -s "$SCR/rec/argv" ]; then ok launcher_bash_n; else bad launcher_bash_n "rc=$rc argv empty"; fi; else bad launcher_bash_n syntax; fi

# launcher_refuses_http_url
run "http://example.com/page_1.html"; r1=$rc; inv1=$([ -e "$SCR/rec/argv" ] && echo y || echo n)
run "https://x/a.html"; r2=$rc
run "$good"; r3=$rc
if [ "$r1" -eq 2 ] && [ "$r2" -eq 2 ] && [ "$inv1" = n ] && [ "$r3" -eq 0 ]; then ok launcher_refuses_http_url; else bad launcher_refuses_http_url "rc=$r1,$r2,$r3 invoked=$inv1"; fi

# launcher_refuses_path_outside_scenes (traversal, subdir, uppercase, wrong ext, symlink escape, no arg)
allok=1
for u in "file:///etc/passwd" "file://$ROOT/../page_1.html" "file://$ROOT/fonts/../page_1.html" "file://$ROOT/sub/page_1.html" "file://$ROOT/Page.html" "file://$ROOT/page_1.txt" "file://$ROOT/evil.html" "file://$ROOT/missing.html" ""; do
  run "$u"; if [ "$rc" -ne 2 ] || [ -e "$SCR/rec/argv" ]; then allok=0; echo "     not refused: '$u' rc=$rc"; fi
done
run "$good"; if [ "$allok" -eq 1 ] && [ "$rc" -eq 0 ]; then ok launcher_refuses_path_outside_scenes; else bad launcher_refuses_path_outside_scenes "positive rc=$rc"; fi

# launcher_no_remote_debugging
run "$good"
if [ "$rc" -eq 0 ] && [ -s "$SCR/rec/argv" ] && ! grep -q -- 'remote-debugging' "$SCR/rec/argv" && ! grep -q -- 'remote-debugging' "$L" "$here/../profiles/chromium.flags"; then ok launcher_no_remote_debugging; else bad launcher_no_remote_debugging "rc=$rc"; fi

# launcher_profile_on_tmp_removed_on_exit (also when chromium fails; rc propagated, never 1)
run "$good"; udd="$(cat "$SCR/rec/udd" 2>/dev/null)"; existed="$(cat "$SCR/rec/udd_existed" 2>/dev/null)"
case "$udd" in "$SCR/tmp/chromium-"*) pre=1;; *) pre=0;; esac
gone=0; [ -n "$udd" ] && [ ! -e "$udd" ] && gone=1
STUB_RC=3 run "$good"; udd2="$(cat "$SCR/rec/udd" 2>/dev/null)"; rc2=$rc
STUB_RC=1 run "$good"; rc1=$rc
if [ "$rc" -ne 1 ] && [ "$pre" -eq 1 ] && [ "$existed" = yes ] && [ "$gone" -eq 1 ] && [ -n "$udd2" ] && [ ! -e "$udd2" ] && [ "$rc2" -eq 3 ] && [ "$rc1" -ne 1 ] && [ "$rc1" -ne 0 ] \
   && [ -z "$(ls -A "$SCR/tmp" | grep -vx chromium-raw.rc)" ]; then ok launcher_profile_on_tmp_removed_on_exit; else bad launcher_profile_on_tmp_removed_on_exit "udd=$udd existed=$existed gone=$gone rc2=$rc2 rc1=$rc1"; fi

# launcher_sets_umask_027
run "$good"
if [ "$rc" -eq 0 ] && grep -qx 'umask=0027' "$SCR/rec/env" && grep -q 'umask 027' "$L"; then ok launcher_sets_umask_027; else bad launcher_sets_umask_027 "$(grep umask "$SCR/rec/env" 2>/dev/null)"; fi

# launcher_det_flags_present (+ env contract)
run "$good"; det=1
for f in --force-device-scale-factor=1 --font-render-hinting=none --disable-lcd-text --disable-gpu --disable-smooth-scrolling \
  --disable-renderer-backgrounding --disable-background-timer-throttling --hide-scrollbars --window-position=0,0 --window-size=1920,1080 --kiosk \
  --no-first-run --disable-breakpad --disable-component-update --disable-background-networking --disable-sync \
  --disable-dev-shm-usage --no-default-browser-check "$good"; do has "$f" || { det=0; echo "     missing $f"; }; done
grep -q '^--user-data-dir=' "$SCR/rec/argv" || det=0
for e in "LANG=C.UTF-8" "TZ=UTC" "FONTCONFIG_FILE=$ROOT/fonts/fonts.conf" "MESA=true"; do grep -qxF -- "$e" "$SCR/rec/env" 2>/dev/null || { det=0; echo "     missing env $e"; }; done
udd="$(cat "$SCR/rec/udd" 2>/dev/null)"
grep -q "^HOME=$SCR/tmp/chromium-" "$SCR/rec/env" 2>/dev/null || det=0
if [ "$rc" -eq 0 ] && [ "$det" -eq 1 ]; then ok launcher_det_flags_present; else bad launcher_det_flags_present "rc=$rc"; fi

# launcher_sandbox_mode_closed_enum
run --sandbox-mode nosandbox "$good"; a=$rc; has --no-sandbox && ns=1 || ns=0
run "$good"; d=$rc; has --no-sandbox && dns=1 || dns=0
run --sandbox-mode namespace "$good"; b=$rc; has --no-sandbox && nn=1 || nn=0
run --sandbox-mode suid "$good"; c=$rc
run --sandbox-mode bogus "$good"; e=$rc; ei=$([ -e "$SCR/rec/argv" ] && echo y || echo n)
if [ "$a" -eq 0 ] && [ "$ns" -eq 1 ] && [ "$d" -eq 0 ] && [ "$dns" -eq 1 ] && [ "$b" -eq 0 ] && [ "$nn" -eq 0 ] && [ "$c" -eq 0 ] && [ "$e" -eq 2 ] && [ "$ei" = n ]; then
  ok launcher_sandbox_mode_closed_enum; else bad launcher_sandbox_mode_closed_enum "rc=$a,$d,$b,$c,$e"; fi

# launcher_geometry_required
GEOMETRY=bogus REC="$SCR/rec" PATH="$SCR/bin:$PATH" CU_SCENES_ROOT="$ROOT" CU_TMP="$SCR/tmp" timeout 20 bash "$L" "$good" 2>/dev/null; g=$?
run "$good"
if [ "$g" -eq 2 ] && [ "$rc" -eq 0 ]; then ok launcher_geometry_required; else bad launcher_geometry_required "g=$g rc=$rc"; fi

# launcher_v3_root_and_raw_rc (W4 R7, AW4-01): a v3 scenes root works, the raw chromium rc survives the 1 -> 5 map in $TMPBASE/chromium-raw.rc,
# and with no CU_SCENES_ROOT only the /opt/cu/scenes/{v2,v3} URL prefixes are derivable
R3="$SCR/scenes/v3"; mkdir -p "$R3/fonts"; echo '<html></html>' >"$R3/wl1l_large.html"
rm -f "$SCR/tmp/chromium-raw.rc"
REC="$SCR/rec"; rm -rf "$REC"; mkdir -p "$REC"
PATH="$SCR/bin:$PATH" REC="$REC" CU_SCENES_ROOT="$R3" CU_TMP="$SCR/tmp" GEOMETRY=1920x1080x24 STUB_RC=1 timeout 20 bash "$L" "file://$R3/wl1l_large.html" 2>/dev/null; r3=$?
raw1="$(cat "$SCR/tmp/chromium-raw.rc" 2>/dev/null)"
PATH="$SCR/bin:$PATH" REC="$REC" CU_SCENES_ROOT="$R3" CU_TMP="$SCR/tmp" GEOMETRY=1920x1080x24 STUB_RC=0 timeout 20 bash "$L" "file://$R3/wl1l_large.html" 2>/dev/null; r0=$?
raw0="$(cat "$SCR/tmp/chromium-raw.rc" 2>/dev/null)"
PATH="$SCR/bin:$PATH" REC="$REC" CU_TMP="$SCR/tmp" GEOMETRY=1920x1080x24 timeout 20 bash "$L" "file://$R3/wl1l_large.html" 2>/dev/null; rx=$?
PATH="$SCR/bin:$PATH" REC="$REC" CU_TMP="$SCR/tmp" GEOMETRY=1920x1080x24 timeout 20 bash "$L" "file:///opt/cu/scenes/v4/a.html" 2>/dev/null; rv4=$?
if [ "$r3" -eq 5 ] && [ "$raw1" = 1 ] && [ "$r0" -eq 0 ] && [ "$raw0" = 0 ] && [ "$rx" -eq 2 ] && [ "$rv4" -eq 2 ]; then ok launcher_v3_root_and_raw_rc; else bad launcher_v3_root_and_raw_rc "r3=$r3 raw1=$raw1 r0=$r0 raw0=$raw0 rx=$rx rv4=$rv4"; fi

[ "$fail" -eq 0 ] && exit 0
exit 5
