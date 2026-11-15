#!/usr/bin/env bash
# gates.sh: INT gate bundle (wheel build + check first, then sweep, unit tests, W2/W3 vision/Go capture/entrypoint/launcher suites, W1 AST/engine/bench/WL suites, W4 V/B/M/R7 suites, W4 FX1 suites, g_disk_frames, g_disk_size, suite plan, mutants). rc 0 ok, 2 usage, 5 any failure. Never 1.
export PATH="$PATH:/usr/local/go/bin" PYTHONDONTWRITEBYTECODE=1
[ $# -eq 0 ] || { echo "usage: gates.sh" >&2; exit 2; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || exit 5
FAILS=0
g() { # g <name> <cmd...>
  local n="$1"; shift
  timeout "${GATE_TMO:-600}" taskset -c "${CU_CPUSET:-10-14}" "$@"; local rc=$?
  echo "GATE $n rc=$rc"
  [ $rc -eq 0 ] || FAILS=$((FAILS+1))
}
g wheel python3 -B vision/build_wheel.py
# JW2-05: compare against the wheel built from the ve-sealed-1-law4 tag (read-only git archive, scratch out dir), so the witness is equal:<sha8>
PEER_ARGS=()
mkdir -p build/gate_disk/peer
# W3b CI-in-VM: VE_PEER_WHL names a prebuilt peer wheel (regular file, no symlink) when the vision-engine checkout is
# absent; it is still judged by wheel-witness below, so a wrong wheel fails as before. Unset = unchanged host behaviour.
if [ -n "${VE_PEER_WHL:-}" ]; then
  if [ -f "$VE_PEER_WHL" ] && [ ! -L "$VE_PEER_WHL" ]; then PEER_WHL="$VE_PEER_WHL"; else PEER_WHL=""; fi
else
  PEER_LINE="$(timeout 300 taskset -c "${CU_CPUSET:-10-14}" python3 -B /home/user/Documentos/vision-engine/tools/build_wheel.py ve-sealed-1-law4 --out build/gate_disk/peer 2>/dev/null | tail -n 1)"
  PEER_WHL="${PEER_LINE#*  }"
fi
[ -f "$PEER_WHL" ] && PEER_ARGS=(--peer "$PEER_WHL")
g wheel-check python3 -B vision/build_wheel.py --check "${PEER_ARGS[@]}"
g sweep python3 -B vision/ramws_shim.py sweep
# RJ2-03: the witness must start equal:5e2395d5; an absent tag tool or a moved peer is peer_moved and FAILS this gate
g wheel-witness bash -c 'python3 -B vision/build_wheel.py --check "$@" | grep -q "\"witness\": \"equal:5e2395d5"' _ "${PEER_ARGS[@]}"
g bash-n bash -n scripts/cu/gates.sh scripts/cu/run_suite.sh docker/cu/entrypoint-cu.sh docker/cu/exec-umask.sh docker/cu/launch-chromium-cu.sh docker/cu/diag/harvest.sh docker/cu/diag/capsule-diag.sh docker/cu/tests/test_diag.sh docker/cu/oracle/launch-oracle-cu.sh scripts/cu/oracle_gates.sh docker/cu/tests/test_oracle_launch.sh
g suite-plan bash scripts/cu/run_suite.sh --plan
g unit python3 -B -m unittest discover -s scripts/cu/tests
g vision-tests python3 -B -m unittest discover -s vision/tests -t .
g go-vet go vet -p 5 ./src/cubook/... ./cmd/srf-cu-wlcap/... ./cmd/srf-cu-snap/... ./cmd/srf-cu-exec/... ./cmd/srf-cu-mcp/...
g go-capture env -u OS_CU_TEST go test -p 5 -count=1 -timeout 120s ./src/cubook/capture/... ./cmd/srf-cu-wlcap/...
g go-exec env -u OS_CU_TEST go test -p 5 -count=1 -timeout 120s ./src/cubook/exec/... ./cmd/srf-cu-exec/...
g go-snap env -u OS_CU_TEST go test -p 5 -count=1 -timeout 120s ./cmd/srf-cu-snap/...
g entrypoint-preflight bash docker/cu/tests/test_entrypoint_preflight.sh
g entrypoint-umask bash docker/cu/tests/test_entrypoint_umask.sh
g chromium-launcher bash docker/cu/tests/test_launch_chromium_cu.sh
g wl7-diag bash docker/cu/tests/test_diag.sh
g chromium-launcher-fx1 bash docker/cu/tests/test_w3_fx1_launcher.sh
g chromium-profile python3 -B -m unittest discover -s docker/cu/tests -p test_chromium_profile.py
g chunk-analyze python3 -B -m unittest vision.tests.test_chunk_analyze
g ast python3 -B -m unittest discover -s capsule/astcheck/tests -t .
g engine python3 -B -m unittest vision.tests.test_engine_path
g bench-tests python3 -B -m unittest discover -s bench/cu/tests -t .
g bench-dry python3 -B bench/cu/harness.py --dry-run bench/cu/prereg_w1.json
g wl-tests python3 -B -m unittest discover -s bench/wl/tests -t .
g wl-validate python3 -B bench/wl/recorder.py validate bench/wl/prereg_wl.json
g wl-validate-v2 python3 -B bench/wl/labels_v2.py validate bench/wl/prereg_wl_v2.json
g wl-validate-v3 python3 -B bench/wl/labels_v3.py validate bench/wl/prereg_wl_v3.json
g wl-prereg-labels-v3 python3 -B -m unittest bench.wl.tests.test_prereg_v3 bench.wl.tests.test_labels_v3
g wl-driver-markers python3 -B -m unittest bench.wl.tests.test_driver_markers
g wl-driver-fx1 python3 -B -m unittest bench.wl.tests.test_driver_fx1
g disk-frames-m python3 -B -m unittest scripts.cu.g_disk_frames_m_test
# W4 FX1 (JW4-05/06, ADC-W4-01): named so a renamed or dropped test fails here instead of passing vacuously inside ./...
g go-fx1-capture env -u OS_CU_TEST go test -p 5 -count=1 -timeout 120s -run '^(TestRunEmptyNonceRefused|TestWlcapNonceFailRc5)$' ./src/cubook/capture ./cmd/srf-cu-wlcap
g go-fx1-exec env -u OS_CU_TEST go test -p 5 -count=1 -timeout 120s -run '^(TestV3RectOffScreenRefused|TestV3DragRectOffScreenRefused|TestV3RectCheckBoundsStart)$' ./src/cubook/exec
# ADC-W3-10 / ADC2-W3-05: build/gate_disk also holds the peer wheel and mutant trees, so the per-episode 100 KB cap is checked on the smoke dir,
# which g_disk_frames fills with the real smoke evidence (no bin/); an empty smoke dir fails its own gate instead of passing vacuously
rm -rf build/gate_disk/smoke; mkdir -p build/gate_disk/smoke
g disk-frames python3 -B scripts/cu/g_disk_frames.py --smoke-out build/gate_disk/smoke
g disk-smoke-nonempty test -n "$(ls -A build/gate_disk/smoke)"
g disk-size python3 -B scripts/cu/g_disk_size.py --out build/gate_disk/smoke
g hb-os-tools python3 -B -m unittest discover -s hyperbooks/_tools/os/tests -t .
g go-cubook-rest env -u OS_CU_TEST go test -p 5 -count=1 -timeout 120s ./src/cubook/action/... ./src/cubook/ajson/... ./src/cubook/mcp/... ./src/cubook/prodgate/... ./src/cubook/rc/...
g hb-os-cubook bash hyperbooks/_tools/os/hb_os_gates.sh HB-OS-cubook
# JW5A-01: a gate run must leave the tracked book untouched (the book is rebuilt on purpose, never by the gate)
g hb-os-cubook-clean git diff --exit-code -- hyperbooks/HB-OS-cubook
# RD5A-02: the frozen eval protocol tests (RESULT.md wording, seals, measure.sh label allow-list) and the blind-split leak guard are gates
g hb-os-eval python3 -B -m unittest eval.hb_os.tests.test_result eval.hb_os.tests.test_measure eval.hb_os.tests.test_hb_os_prereg
g hb-os-leak python3 -B eval/hb_os/leak_check.py
g contracts-cubook bash -c 'set -e; mkdir -p build/contracts_bin; for c in exec mcp snap wlcap; do go build -p 5 -o build/contracts_bin/srf-cu-$c ./cmd/srf-cu-$c; done; python3 -B contracts/cubook/run_conformance.py --bin-dir build/contracts_bin; CUBOOK_BIN_DIR=build/contracts_bin python3 -B -m unittest discover -s contracts/cubook/tests -t .'
# W6a oracle (read-only Chromium labelling oracle): oracle_gates.sh = proxy + launch + labels/spec + crosscheck + det x2 + storage + gate tests; discover form because the pip package `docker` shadows docker.cu.tests (G-1)
g oracle bash scripts/cu/oracle_gates.sh
g oracle-shim python3 -B -c 'import ast,sys; [ast.parse(open(p).read(), p) for p in sys.argv[1:]]' docker/cu/oracle/bin/chromium docker/cu/oracle/cdp_proxy.py docker/cu/oracle/no_listener.py
GATE_TMO=1500 g mutants python3 -B scripts/cu/run_mutants.py
[ "$FAILS" -eq 0 ] && { echo "GATES OK"; exit 0; }
echo "GATES FAIL $FAILS"; exit 5
