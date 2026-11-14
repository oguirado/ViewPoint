#!/usr/bin/env bash
# g_xshm_close.sh: runs the Xshm Close fixture (xshm_close_fixture_test.go.txt) against src/cubook/capture without writing into src/ (go test -overlay).
# rc 0 test passes, 5 test fails or build error. Never 1. Run from anywhere; uses the tree this script lives in.
export PATH="$PATH:/usr/local/go/bin" CGO_ENABLED=0
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || exit 5
T="$(mktemp -d "${TMPDIR:-/tmp}/xshmclose.XXXXXX")" || exit 5
trap 'rm -rf "$T"' EXIT
printf '{"Replace":{"%s":"%s"}}\n' "$REPO/src/cubook/capture/zz_xshm_close_test.go" "$REPO/scripts/cu/xshm_close_fixture_test.go.txt" > "$T/overlay.json"
env -u OS_CU_TEST go test -p 2 -count=1 -timeout 120s -overlay "$T/overlay.json" -run '^TestXshmCloseDropsGetterAndRefusesFrames$' ./src/cubook/capture >"$T/out.txt" 2>&1
rc=$?
cat "$T/out.txt"
grep -q "^ok" "$T/out.txt" && ! grep -q "no tests to run" "$T/out.txt" || rc=5
[ "$rc" -eq 0 ] || exit 5
exit 0
