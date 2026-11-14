#!/usr/bin/env bash
# hb_os_gates.sh <book> [outdir]: OS gate entry. goextract -> build -> index -> live + OS gates -> admission_os. rc 0, 2 usage, 3 book-link, 5 gate failure or E_BOOK_DRIFT. Never 1.
# Env overrides (tests): HBV2_ROOT HBV2_SUBJECT HBV2_SRC_DIRS HBV2_BENCH OS_FORMAT OS_PARAMS_REF.
[ $# -ge 1 ] && [ $# -le 2 ] || { echo "usage: hb_os_gates.sh <book> [outdir]" >&2; exit 2; }
BOOK="$1"
export PATH="$PATH:/usr/local/go/bin" PYTHONDONTWRITEBYTECODE=1
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO" || exit 5
OUT="${2:-build/hb_os}"
export HBV2_ROOT="${HBV2_ROOT:-hyperbooks}" HBV2_SUBJECT="${HBV2_SUBJECT:-.}" HBV2_BOOK_SUB="$BOOK" HBV2_BENCH="${HBV2_BENCH:-eval/hb_os/dev}"
export HBV2_SRC_DIRS="${HBV2_SRC_DIRS:-src/cubook,cmd/srf-cu-exec,cmd/srf-cu-mcp,cmd/srf-cu-snap,cmd/srf-cu-wlcap}"
D="hyperbooks/_tools/os/hb_os_driver.py"
mkdir -p "$OUT"
# frozen tuned params (vision-engine 8120aab, read-only git show) staged when absent; the sha is printed for the receipt
if [ ! -f "$HBV2_ROOT/params.json" ]; then
  VE="/home/user/Documentos/vision-engine"
  case "${VE_REPO:-}" in /*) [ -d "$VE_REPO" ] && VE="$VE_REPO" ;; esac  # W3b CI-in-VM
  git -C "$VE" show "${OS_PARAMS_REF:-8120aab:params.json}" > "$OUT/params.json" 2>/dev/null || { echo "params.json unavailable" >&2; exit 5; }
  cp "$OUT/params.json" "$HBV2_ROOT/params.json" || exit 5
  trap 'rm -f "$HBV2_ROOT/params.json"' EXIT
fi
echo "params_sha256=$(sha256sum "$HBV2_ROOT/params.json" | cut -d' ' -f1)" >&2
# JW5A-01 / ADC-W5A-02(b): a stored book must match its committed manifest BEFORE any rebuild; a build must not dirty the tracked book (no self-heal)
BD="$HBV2_ROOT/$BOOK"
snap() { (cd "$BD" && find . -type f -print0 | sort -z | xargs -0 -r sha256sum) 2>/dev/null; }
if [ -f "$BD/manifest.json" ]; then
  stored="$(python3 -B -c 'import json,sys; print(json.load(open(sys.argv[1])).get("files",{}).get("book.md",""))' "$BD/manifest.json" 2>/dev/null)"
  actual="$(sha256sum "$BD/book.md" 2>/dev/null | cut -d' ' -f1)"
  [ -n "$stored" ] && [ "$stored" = "$actual" ] || { echo "E_BOOK_DRIFT: book.md sha ${actual:-absent} != manifest ${stored:-absent} (rebuild the book on purpose, then gate)" >&2; exit 5; }
  PRE="$(snap)"
elif [ -f "$BD/book.md" ]; then
  echo "E_BOOK_DRIFT: manifest.json absent for $BD (restore it or rebuild the book on purpose, then gate)" >&2; exit 5
fi
# hb_trace is a Python pytest tracer: declared skip (no TESTS edges for Go), reported in gates.json skipped[]
python3 -B "$D" extract --out "$OUT" || exit 5
python3 -B "$D" build; rc=$?; [ $rc -eq 0 ] || exit $([ $rc -eq 3 ] && echo 3 || echo 5)
python3 -B "$D" index || exit 5
if [ -n "${PRE+x}" ] && [ "$PRE" != "$(snap)" ]; then echo "E_BOOK_DRIFT: build changed the tracked book dir $BD (code and book are out of step)" >&2; exit 5; fi
python3 -B "$D" gates --out "$OUT/gates.json"; rc=$?
python3 -B hyperbooks/_tools/os/admission_os.py --format "${OS_FORMAT:-hyperbooks/_tools/os/OS_FORMAT.json}" --root "$HBV2_ROOT"; arc=$?
[ $rc -eq 0 ] && [ $arc -eq 0 ] && exit 0
exit 5
