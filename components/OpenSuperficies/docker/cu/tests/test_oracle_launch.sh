#!/usr/bin/env bash
# Lane P: launch-oracle-cu.sh -> UNCHANGED launch-chromium-cu.sh -> oracle shim -> stub "real chromium". No browser, no X, no docker.
# Every guard also asserts a positive run (rc 0 + recorded argv/fds) so a do-nothing script cannot pass.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
O="$here/../oracle/launch-oracle-cu.sh"
L="$here/../launch-chromium-cu.sh"
FX="$here/fixtures/cdp"
SCR="$(mktemp -d "${TMPDIR:-/tmp}/oscu-lo.XXXXXX")"
trap 'rm -rf "$SCR"' EXIT
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1 ${2:-}"; fail=1; }

ROOT="$SCR/scenes/v2"
mkdir -p "$ROOT/fonts" "$SCR/tmp" "$SCR/fifo"
echo '<html></html>' >"$ROOT/page_1.html"
cat >"$SCR/stub" <<'EOS'
#!/usr/bin/env python3
import json, os, sys
fds = sorted(int(x) for x in os.listdir("/proc/self/fd") if os.path.exists("/proc/self/fd/" + x))
json.dump({"argv": sys.argv[1:], "fds": fds}, open(os.environ["REC"], "w"))
sys.exit(int(os.environ.get("STUB_RC", "0")))
EOS
chmod +x "$SCR/stub"
run() {
  rm -f "$SCR/rec.json"
  REC="$SCR/rec.json" OS_ORACLE_REAL_CHROMIUM="$SCR/stub" OS_ORACLE_FIFO_DIR="$SCR/fifo" OS_ORACLE_TTL=20 \
    CU_SCENES_ROOT="$ROOT" CU_TMP="$SCR/tmp" GEOMETRY=1920x1080x24 \
    timeout 40 bash "$O" "$@" 2>"$SCR/err" >"$SCR/out"
  rc=$?
}
jq_() { python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));'"$2" "$1"; }
good="file://$ROOT/page_1.html"

[ -x "$O" ] && bash -n "$O" && ok oracle_launcher_bash_n || bad oracle_launcher_bash_n

run "$good"
if [ "$rc" -eq 0 ] && [ -s "$SCR/rec.json" ]; then ok oracle_launch_runs_stub; else bad oracle_launch_runs_stub "rc=$rc $(cat "$SCR/err" | head -3)"; fi

if [ -s "$SCR/rec.json" ] && [ "$(jq_ "$SCR/rec.json" 'print(d["argv"].count("--remote-debugging-pipe"))')" = 1 ]; then ok exactly_one_pipe_flag; else bad exactly_one_pipe_flag; fi
if [ -s "$SCR/rec.json" ] && [ "$(jq_ "$SCR/rec.json" 'print(sum(("remote-debugging-port" in a or "remote-debugging-address" in a) for a in d["argv"]))')" = 0 ]; then ok no_port_or_address_flag; else bad no_port_or_address_flag; fi
if [ -s "$SCR/rec.json" ] && [ "$(jq_ "$SCR/rec.json" 'print(d["fds"])')" = "[0, 1, 2, 3, 4]" ]; then ok child_fd_table_0_to_4; else bad child_fd_table_0_to_4; fi
if [ -s "$SCR/rec.json" ] && [ "$(jq_ "$SCR/rec.json" 'print("--no-first-run" in d["argv"] and "--kiosk" in d["argv"] and d["argv"][-2]==d["argv"][-2] and "--user-data-dir=" in "".join(a[:16] for a in d["argv"]))')" = True ]; then ok launcher_flags_and_profile_kept; else bad launcher_flags_and_profile_kept; fi
if [ -z "$(ls -A "$SCR/fifo")" ]; then ok fifos_removed_at_exit; else bad fifos_removed_at_exit "$(ls -A "$SCR/fifo")"; fi

run "http://example.com/page_1.html"; r1=$rc; inv=$([ -s "$SCR/rec.json" ] && echo y || echo n)
run "file:///etc/passwd"; r2=$rc
if [ "$r1" -eq 2 ] && [ "$r2" -eq 2 ] && [ "$inv" = n ]; then ok url_jail_still_refuses_rc2; else bad url_jail_still_refuses_rc2 "r1=$r1 r2=$r2 inv=$inv"; fi

# ADC-W6A-07: any spelling of a remote-debugging switch before the URL is refused rc 2 and the stub is never run
for sp in -remote-debugging-port=9222 --REMOTE-DEBUGGING-PORT=9222 --remote_debugging_address=0.0.0.0 --remote-debugging-pipe --remote-debugging-pipe=json; do
  run "$sp" "$good"
  if [ "$rc" -eq 2 ] && [ ! -s "$SCR/rec.json" ]; then ok "argv_spelling_refused_rc2 $sp"; else bad "argv_spelling_refused_rc2 $sp" "rc=$rc"; fi
done

STUB_RC=1 run "$good"
if [ "$rc" -ne 1 ] && [ "$rc" -ne 0 ]; then ok chromium_rc1_never_leaks_as_1; else bad chromium_rc1_never_leaks_as_1 "rc=$rc"; fi

run; if [ "$rc" -eq 2 ]; then ok usage_rc2; else bad usage_rc2 "rc=$rc"; fi

# R3: the non-oracle path stays byte-unchanged
s1=$(sha256sum "$L" | cut -c1-8); s2=$(sha256sum "$here/../profiles/chromium.flags" | cut -c1-8)
if [ "$s1" = 854c560f ] && [ "$s2" = 2e3b876e ]; then ok launcher_and_flags_sha_unchanged; else bad launcher_and_flags_sha_unchanged "$s1 $s2"; fi
# the oracle dir must not hold the launcher's forbidden token outside the shim/proxy sources it is meant for
if ! grep -q 'remote-debugging-port' "$O"; then ok oracle_launcher_has_no_port_flag; else bad oracle_launcher_has_no_port_flag; fi

[ "$fail" -eq 0 ] && echo "ALL OK" || { echo "FAILED"; exit 5; }
