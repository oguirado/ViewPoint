#!/usr/bin/env bash
# run_suite.sh [--plan|--selftest-fail]  CU suite: janitor first, then Go (-p 2, -timeout 30s) minus pbx/grabador, manager -p 1,
# cmd/srf-cu-*, vision/tests, capsule/astcheck/tests, bench/cu+wl tests, scripts/cu/tests. rc: 0 ok, 2 usage, 5 any failure. Never 1.
export PATH="$PATH:/usr/local/go/bin" CGO_ENABLED=0 PYTHONDONTWRITEBYTECODE=1
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || exit 5
PLAN=0 SELF=0
case "${1:-}" in
  "") ;;
  --plan) PLAN=1 ;;
  --selftest-fail) SELF=1 ;;
  *) echo "usage: run_suite.sh [--plan|--selftest-fail]" >&2; exit 2 ;;
esac
[ $# -le 1 ] || { echo "usage: run_suite.sh [--plan|--selftest-fail]" >&2; exit 2; }
TS="taskset -c ${CU_CPUSET:-10-14}"
FAILS=0
step() { # step <name> <cmd...>
  local name="$1"; shift
  if [ "$PLAN" = 1 ]; then echo "STEP $name $*"; return 0; fi
  echo "STEP $name"
  "$@"; local rc=$?
  if [ $rc -ne 0 ]; then echo "FAIL $name rc=$rc"; FAILS=$((FAILS+1)); fi
  return 0
}
skip() { echo "SKIP $1: $2"; }

if [ "$SELF" = 1 ]; then step selftest false; [ "$FAILS" -eq 0 ] && exit 0; exit 5; fi

step sweep python3 -B vision/ramws_shim.py sweep
skip src/surfacebook/pbx "needs live OpenSwitchboard and SBD_OPERATOR_TOKEN (exit 77)"
skip src/surfacebook/grabador "host Xvfb :150 + /tmp/*.mp4 on ext4, >74s over the 30s cap"
skip "src/displaybook/desktop TestXTestWithLiveDisplay" "falls back to host display :1 and moves the operator pointer"
skip "src/surfacebook/manager TestServidorAdmiteSoloAlUIDConfigurado" "legacy uds broken-pipe race, intermittent on this host (RD1-01, KL-07)"
PKGS="$(go list ./src/... 2>/dev/null | grep -Ev '/surfacebook/(pbx|grabador|manager)(/|$)' | tr '\n' ' ')"
[ "$PLAN" = 1 ] && [ -z "$PKGS" ] && PKGS="./src/... (minus pbx grabador manager)"
step go-src $TS go test -p 2 -count=1 -timeout 30s -skip TestXTestWithLiveDisplay $PKGS
step go-manager $TS go test -p 1 -count=1 -timeout 30s -skip TestServidorAdmiteSoloAlUIDConfigurado ./src/surfacebook/manager/...
step go-cmd-cu $TS go test -p 2 -count=1 -timeout 30s ./cmd/srf-cu-snap/... ./cmd/srf-cu-exec/... ./cmd/srf-cu-mcp/... ./cmd/srf-cu-wlcap/...
step py-vision $TS python3 -B -m unittest discover -s vision/tests -t .
step py-astcheck $TS python3 -B -m unittest discover -s capsule/astcheck/tests -t .
step py-bench-cu $TS python3 -B -m unittest discover -s bench/cu/tests -t .
step py-bench-wl $TS python3 -B -m unittest discover -s bench/wl/tests -t .
step py-docker-cu $TS python3 -B -m unittest discover -s docker/cu/tests -p test_chromium_profile.py
step sh-docker-cu $TS bash -c 'for t in docker/cu/tests/test_*.sh; do bash "$t" || exit 5; done'
step py-scripts-cu $TS python3 -B -m unittest discover -s scripts/cu/tests
[ "$PLAN" = 1 ] && exit 0
if [ "$FAILS" -ne 0 ]; then echo "SUITE FAIL steps=$FAILS"; exit 5; fi
echo "SUITE OK"; exit 0
