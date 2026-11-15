#!/usr/bin/env bash
# W3 FX1 lane FC (ADC-W3-05): launcher/exec-umask failures map to rc 5 + token (never 1), flags file grammar is enforced at
# runtime (extra tokens, remote-debugging), the chromium profile dir is unpredictable. Stub chromium, no browser, no X.
# L_OVERRIDE / X_OVERRIDE: failing-first against the pre-fix scripts.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
L="${L_OVERRIDE:-$here/../launch-chromium-cu.sh}"
EX="${X_OVERRIDE:-$here/../exec-umask.sh}"
SCR="$(mktemp -d "${TMPDIR:-/tmp}/oscu-fc.XXXXXX")"
trap 'rm -rf "$SCR"' EXIT
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1 ${2:-}"; fail=1; }

ROOT="$SCR/scenes/v2"
mkdir -p "$SCR/bin" "$SCR/rbin" "$ROOT/fonts" "$SCR/tmp"
echo '<html></html>' >"$ROOT/page_1.html"
cat >"$SCR/bin/chromium" <<'STUB'
#!/bin/sh
: >"$REC/argv"; for a in "$@"; do printf '%s\n' "$a" >>"$REC/argv"; done
echo "$PPID" >"$REC/ppid"
for a in "$@"; do case "$a" in --user-data-dir=*) echo "${a#--user-data-dir=}" >"$REC/udd";; esac; done
exit "${STUB_RC:-0}"
STUB
chmod +x "$SCR/bin/chromium"
for c in mkdir mktemp; do printf '#!/bin/sh\nexit 1\n' >"$SCR/rbin/$c"; chmod +x "$SCR/rbin/$c"; done
good="file://$ROOT/page_1.html"

# run [flags-file] : rc in $rc, stderr in $SCR/err
run() {
  REC="$SCR/rec"; rm -rf "$REC"; mkdir -p "$REC"
  PATH="${EXTRA_PATH:+$EXTRA_PATH:}$SCR/bin:$PATH" REC="$REC" CU_SCENES_ROOT="$ROOT" CU_TMP="$SCR/tmp" GEOMETRY=1920x1080x24 \
    CU_FLAGS_FILE="${FLAGS_FILE:-$here/../profiles/chromium.flags}" timeout 20 bash "$L" "$good" 2>"$SCR/err" >/dev/null
  rc=$?
}

# positive control: the launcher runs and chromium is invoked
run; if [ "$rc" -eq 0 ] && [ -s "$SCR/rec/argv" ]; then ok w3fx1_launcher_positive; else bad w3fx1_launcher_positive "rc=$rc"; fi

# ADC-W3-05(1): a pre-exec failure (profile creation) is rc 5 + token, never 1, chromium never invoked
EXTRA_PATH="$SCR/rbin" run; r=$rc; inv=$([ -e "$SCR/rec/argv" ] && echo y || echo n)
if [ "$r" -eq 5 ] && grep -q 'E_LAUNCH' "$SCR/err" && [ "$inv" = n ]; then ok launcher_preexec_failure_rc5_token; else bad launcher_preexec_failure_rc5_token "rc=$r invoked=$inv err=$(head -c 120 "$SCR/err")"; fi

# ADC-W3-05(2): a flags-file record with an extra token, or any remote-debugging flag, is refused rc 2 before chromium
F="$SCR/f_extra"; { cat "$here/../profiles/chromium.flags"; echo "flag base --no-zygote --enable-logging"; } >"$F"
FLAGS_FILE="$F" run; re=$rc; ie=$([ -e "$SCR/rec/argv" ] && echo y || echo n)
F="$SCR/f_cdp"; { cat "$here/../profiles/chromium.flags"; echo "flag base --remote-debugging-port=9222"; } >"$F"
FLAGS_FILE="$F" run; rd=$rc; id=$([ -e "$SCR/rec/argv" ] && echo y || echo n)
F="$SCR/f_ok"; { cat "$here/../profiles/chromium.flags"; echo "flag base --no-zygote"; } >"$F"
FLAGS_FILE="$F" run; rp=$rc; ip=$(grep -cxF -- --no-zygote "$SCR/rec/argv" 2>/dev/null)
if [ "$re" -eq 2 ] && [ "$ie" = n ] && [ "$rd" -eq 2 ] && [ "$id" = n ] && [ "$rp" -eq 0 ] && [ "$ip" -eq 1 ]; then ok launcher_flags_extra_token_and_cdp_refused; else bad launcher_flags_extra_token_and_cdp_refused "extra=$re/$ie cdp=$rd/$id pos=$rp/$ip"; fi

# ADC-W3-05(3): the profile dir is not the predictable chromium-<pid>
run; udd="$(cat "$SCR/rec/udd" 2>/dev/null)"; pp="$(cat "$SCR/rec/ppid" 2>/dev/null)"; b="$(basename -- "$udd")"
if [ "$rc" -eq 0 ] && [ -n "$pp" ] && [[ "$b" =~ ^chromium-${pp}\.[A-Za-z0-9]{6}$ ]]; then ok launcher_profile_name_random_suffix; else bad launcher_profile_name_random_suffix "name=$b ppid=$pp"; fi

# exec-umask.sh: a child rc 1 is mapped to 5; other rcs preserved; positive: rc 0 stays 0
bash "$EX" sh -c 'exit 1' 2>"$SCR/err"; x1=$?
bash "$EX" sh -c 'exit 3' 2>/dev/null; x3=$?
bash "$EX" true 2>/dev/null; x0=$?
if [ "$x1" -eq 5 ] && [ "$x3" -eq 3 ] && [ "$x0" -eq 0 ]; then ok exec_umask_child_rc1_mapped_to_5; else bad exec_umask_child_rc1_mapped_to_5 "rc1->$x1 rc3->$x3 rc0->$x0"; fi

[ "$fail" -eq 0 ] && exit 0
exit 5
