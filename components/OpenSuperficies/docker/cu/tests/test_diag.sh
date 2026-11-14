#!/usr/bin/env bash
# Lane R7 (W4): harvest.sh / capsule-diag.sh against a stub `docker` and stub X tools on PATH (no docker daemon, no X, no browser).
# Every negative assertion is paired with a positive one so a no-op script cannot pass.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
H="$here/../diag/harvest.sh"; D="$here/../diag/capsule-diag.sh"
SCR="$(mktemp -d "${TMPDIR:-/tmp}/oscu-diag.XXXXXX")"
trap 'rm -rf "$SCR"' EXIT
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1 ${2:-}"; fail=1; }
mkdir -p "$SCR/bin" "$SCR/state"

# stub docker: container "alive" iff $SCR/state/alive exists; every call is logged; after `rm` logs vanish like the real thing
cat >"$SCR/bin/docker" <<'EOF2'
#!/bin/sh
S="$STUB_STATE"; echo "docker $*" >>"$S/calls"
sub="$1"; shift
alive() { [ -e "$S/alive" ]; }
case "$sub" in
  logs) alive || { echo "Error: No such container: $1" >&2; exit 1; }
        if [ -n "${STUB_BIG:-}" ]; then head -c 200000 /dev/zero | tr '\0' 'L'; else echo "LOGLINE xvfb ready"; fi ;;
  inspect) alive || { echo "Error: No such object: $*" >&2; exit 1; }
        echo "ExitCode=137 OOMKilled=false" ;;
  exec) alive || { echo "Error: No such container" >&2; exit 1; }
        case "$*" in
          *capsule-diag.sh*) if [ -n "${STUB_BIG:-}" ]; then head -c 200000 /dev/zero | tr '\0' 'D'; else echo "DIAGLINE xfixes"; fi ;;
          *chromium-raw.rc*) echo 137 ;;
          *cl.log*) if [ -n "${STUB_BIG:-}" ]; then head -c 200000 /dev/zero | tr '\0' 'C'; else echo "CLLINE launcher"; fi ;;
        esac ;;
  rm) rm -f "$S/alive" ;;
esac
exit 0
EOF2
chmod +x "$SCR/bin/docker"; cp "$SCR/bin/docker" "$SCR/docker.stub"   # harvest_no_rc1 swaps the stub for a failing one; FX1 cases restore it
runh() { # name outdir ; sets rc
  PATH="$SCR/bin:$PATH" STUB_STATE="$SCR/state" timeout 30 bash "$H" "$@" >"$SCR/hout" 2>"$SCR/herr"; rc=$?
}

# harvest_before_rm: harvest while alive captures data and issues no rm/stop/kill; a later rm then leaves the harvest intact
: >"$SCR/state/alive"; : >"$SCR/state/calls"
runh vqa-x "$SCR/o1"
PATH="$SCR/bin:$PATH" STUB_STATE="$SCR/state" docker rm -f vqa-x
if [ "$rc" -eq 0 ] && grep -q LOGLINE "$SCR/o1"/*logs* 2>/dev/null && grep -q DIAGLINE "$SCR/o1"/* 2>/dev/null \
   && grep -q 137 "$SCR/o1"/*inspect* 2>/dev/null && ! grep -E '^docker (rm|stop|kill)' <(head -n -1 "$SCR/state/calls") >/dev/null \
   && [ -e "$SCR/o1/.harvested" ]; then ok harvest_before_rm; else bad harvest_before_rm "rc=$rc"; fi

# harvest_absent_rc0: container gone -> rc 0, container_absent recorded; a second call after a live harvest does not overwrite it (idempotent)
runh vqa-x "$SCR/o2"
if [ "$rc" -eq 0 ] && [ -e "$SCR/o2/container_absent" ]; then a=1; else a=0; fi
before="$(cat "$SCR/o1"/*logs* 2>/dev/null | md5sum)"
runh vqa-x "$SCR/o1"
after="$(cat "$SCR/o1"/*logs* 2>/dev/null | md5sum)"
if [ "$a" = 1 ] && [ "$rc" -eq 0 ] && [ "$before" = "$after" ] && grep -q LOGLINE "$SCR/o1"/*logs* && [ ! -e "$SCR/o1/container_absent" ]; then ok harvest_absent_rc0; else bad harvest_absent_rc0 "a=$a rc=$rc"; fi

# harvest_cap_100k: oversized artifacts are tail-capped, total <= 100 KB, each <= 32 KB
: >"$SCR/state/alive"
STUB_BIG=1 runh vqa-big "$SCR/o3"
tot=$(cat "$SCR/o3"/* 2>/dev/null | wc -c); mx=$(for f in "$SCR/o3"/*; do wc -c <"$f"; done | sort -n | tail -1)
if [ "$rc" -eq 0 ] && [ "$tot" -gt 1000 ] && [ "$tot" -le 102400 ] && [ "${mx:-0}" -le 32768 ]; then ok harvest_cap_100k; else bad harvest_cap_100k "rc=$rc tot=$tot max=$mx"; fi

# harvest_no_rc1: usage errors are rc 2, a failing docker never gives rc 1
runh; r1=$rc; runh onlyname; r2=$rc
printf '#!/bin/sh\nexit 1\n' >"$SCR/bin/docker"
runh vqa-y "$SCR/o4"; r3=$rc
if [ "$r1" -eq 2 ] && [ "$r2" -eq 2 ] && [ "$r3" -ne 1 ] && [ -e "$SCR/o4/container_absent" ]; then ok harvest_no_rc1; else bad harvest_no_rc1 "$r1 $r2 $r3"; fi

# diag_bounded: stub X tools flood 1 MB or hang; output stays <= 32 KB and the script ends within the per-command timeout
for t in xdpyinfo xwininfo chromium fc-list; do
  printf '#!/bin/sh\nif [ -n "$STUB_HANG" ]; then sleep 30; fi\nhead -c 1000000 /dev/zero | tr "\\0" "X"\n' >"$SCR/bin/$t"; chmod +x "$SCR/bin/$t"
done
PATH="$SCR/bin:$PATH" CU_TMP="$SCR" DISPLAY=:100 timeout 60 bash "$D" >"$SCR/d1" 2>/dev/null; r1=$?
s1=$(wc -c <"$SCR/d1")
t0=$(date +%s)
PATH="$SCR/bin:$PATH" CU_TMP="$SCR" DISPLAY=:100 STUB_HANG=1 CU_DIAG_TMO=1 timeout 60 bash "$D" >"$SCR/d2" 2>/dev/null; r2=$?
t1=$(date +%s)
if [ "$r1" -eq 0 ] && [ "$s1" -gt 100 ] && [ "$s1" -le 32768 ] && [ "$r2" -eq 0 ] && [ $((t1-t0)) -le 25 ] && [ "$(wc -c <"$SCR/d2")" -le 32768 ]; then ok diag_bounded; else bad diag_bounded "r1=$r1 s1=$s1 r2=$r2 dt=$((t1-t0))"; fi

# diag_bash_n: both scripts parse and define the required functions
if bash -n "$H" && bash -n "$D" && grep -q 'harvest_one\|^harvest()' "$H" && grep -q 'classify_rc' "$D" && grep -q 'section()' "$D"; then ok diag_bash_n; else bad diag_bash_n; fi

# diag_sigkill_class: VG-0223 exit classification
c() { bash "$D" --classify "$1" 2>/dev/null; }
if [ "$(c 0)" = Clean ] && [ "$(c 2)" = ExitError ] && [ "$(c 5)" = ExitError ] && [ "$(c 137)" = SilentKill ] \
   && [ "$(c 139)" = Fault ] && [ "$(c 143)" = OtherSignal ]; then ok diag_sigkill_class; else bad diag_sigkill_class "$(c 0)/$(c 2)/$(c 137)/$(c 139)/$(c 143)"; fi

# ---- FX1 (X1): JW4-02 budget counts bytes already in OUTDIR, ADC-W4-03 idempotence before docker, JW4-01 runner prereg refusal, JW4-04 cpuset
cp "$SCR/docker.stub" "$SCR/bin/docker"; chmod +x "$SCR/bin/docker"
fsum() { find "$1" -type f -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}'; }

# harvest_respects_existing: OUTDIR pre-filled with 60 KB; 200 KB per artifact on the stub -> episode total (files) stays <= 100 KB and harvest still wrote something
: >"$SCR/state/alive"; mkdir -p "$SCR/o5"; head -c 61440 /dev/zero | tr '\0' 'R' >"$SCR/o5/receipt.bin"
STUB_BIG=1 runh vqa-pre "$SCR/o5"
tot=$(fsum "$SCR/o5")
if [ "$rc" -eq 0 ] && [ "$tot" -gt 62000 ] && [ "$tot" -le 102400 ] && [ -e "$SCR/o5/.harvested" ] && [ -s "$SCR/o5/docker_logs.txt" ]; then ok harvest_respects_existing; else bad harvest_respects_existing "rc=$rc total=$tot"; fi

# harvest_full_outdir: OUTDIR already at the ceiling -> harvest adds no payload bytes beyond the cap, rc 0
mkdir -p "$SCR/o6"; head -c 100352 /dev/zero | tr '\0' 'R' >"$SCR/o6/receipt.bin"
STUB_BIG=1 runh vqa-pre "$SCR/o6"
tot=$(fsum "$SCR/o6")
if [ "$rc" -eq 0 ] && [ "$tot" -le 102400 ] && [ -e "$SCR/o6/.harvested" ]; then ok harvest_full_outdir; else bad harvest_full_outdir "rc=$rc total=$tot"; fi

# harvest_sub_budget: CU_HARVEST_SUB bounds harvest's own files
STUB_BIG=1 CU_HARVEST_SUB=10000 runh vqa-sub "$SCR/o7"
tot=$(fsum "$SCR/o7")
if [ "$rc" -eq 0 ] && [ "$tot" -gt 1000 ] && [ "$tot" -le 10000 ]; then ok harvest_sub_budget; else bad harvest_sub_budget "rc=$rc total=$tot"; fi

# harvest_idempotent_first: container ALIVE and .harvested present -> no docker call at all, files untouched (ADC-W4-03)
: >"$SCR/state/alive"; : >"$SCR/state/calls"; mkdir -p "$SCR/o8"; echo SENTINEL >"$SCR/o8/docker_logs.txt"; : >"$SCR/o8/.harvested"
runh vqa-idem "$SCR/o8"
if [ "$rc" -eq 0 ] && [ ! -s "$SCR/state/calls" ] && [ "$(cat "$SCR/o8/docker_logs.txt")" = SENTINEL ]; then ok harvest_idempotent_first; else bad harvest_idempotent_first "rc=$rc calls=$(wc -c <"$SCR/state/calls")"; fi

# runner (conductor-owned, in C): copy with C pointed at a scratch dir; skipped when absent
RUN=/home/user/Música/TeamWork_Antigravity2/campaigns/os-cu-0/w4/slive/run_slive4.sh
if [ -f "$RUN" ]; then
  OSR="$(cd "$here/../../.." && pwd)"
  sed "s#^OS=[^;]*; C=.*#OS=$OSR; C=$SCR/c#" "$RUN" >"$SCR/run_slive4.sh"
  mkdir -p "$SCR/c/w4"
  prun() { STEPS="$1" CPUSET="${2:-10-14}" timeout 60 bash "$SCR/run_slive4.sh" >"$SCR/rout" 2>&1; rc=$?; }
  CAN=$(python3 -B "$OSR/bench/wl/labels_v3.py" validate "$OSR/bench/wl/prereg_wl_v3.json" | tail -1)
  FSHA=$(sha256sum "$OSR/bench/wl/prereg_wl_v3.json" | cut -d' ' -f1)
  # JW4-01: no sha journalled -> rc 5 "prereg v3" on every v3 item; WL-7 (v2) is not subject
  : >"$SCR/c/SYNC_VQA.md"; r=0
  for st in wl1s wl1c wl1L wl1h wl4 wl3; do prun $st; if [ "$rc" -ne 5 ] || ! grep -q 'prereg v3' "$SCR/rout"; then r=1; fi; done
  prun wl7; if grep -q 'prereg v3' "$SCR/rout"; then r=1; fi
  if [ "$r" = 0 ]; then ok runner_prereg_unjournalled_rc5; else bad runner_prereg_unjournalled_rc5; fi
  # canonical sha present but the C copy is missing / altered -> rc 5
  printf '%s\n%s\n' "$CAN" "$FSHA" >"$SCR/c/SYNC_VQA.md"; prun wl4; a=$rc; grep -q 'prereg v3' "$SCR/rout" && ma=1 || ma=0
  sed 's/WL-4/WL-X/' "$OSR/bench/wl/prereg_wl_v3.json" >"$SCR/c/w4/PREREG_WL_V3.json"; prun wl4; b=$rc; grep -q 'prereg v3' "$SCR/rout" && mb=1 || mb=0
  if [ "$a" -eq 5 ] && [ "$ma" = 1 ] && [ "$b" -eq 5 ] && [ "$mb" = 1 ]; then ok runner_prereg_copy_mismatch_rc5; else bad runner_prereg_copy_mismatch_rc5 "$a$ma $b$mb"; fi
  # all journalled and matching -> the prereg check passes (the next abort is the RESOURCE_ACK one)
  cp "$OSR/bench/wl/prereg_wl_v3.json" "$SCR/c/w4/PREREG_WL_V3.json"; prun wl4
  if [ "$rc" -eq 5 ] && ! grep -q 'prereg v3' "$SCR/rout" && grep -q 'RESOURCE_ACK' "$SCR/rout"; then ok runner_prereg_journalled_passes; else bad runner_prereg_journalled_passes "rc=$rc"; fi
  # JW4-04: cpuset 10-14 only
  prun wl7 0,1
  if [ "$rc" -eq 5 ] && grep -q 'CPUSET' "$SCR/rout" && ! grep -qE '10-14\|0,1\)' "$RUN"; then ok runner_cpuset_10_14_only; else bad runner_cpuset_10_14_only "rc=$rc"; fi
  # JW4-02 runner side: wl7diag diag files are capped at 8192 B each, harvest gets the sub-budget env
  if grep -q 'head -c 8192' "$RUN" && ! grep -q 'head -c 32768' "$RUN" && grep -q 'CU_HARVEST_SUB=' "$RUN"; then ok runner_harvest_subbudget; else bad runner_harvest_subbudget; fi
  bash -n "$RUN" && ok runner_bash_n || bad runner_bash_n
else
  echo "skip runner tests (run_slive4.sh absent)"
fi

[ "$fail" -eq 0 ] || exit 5
exit 0
