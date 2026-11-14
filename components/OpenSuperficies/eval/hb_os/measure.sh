#!/usr/bin/env bash
# measure.sh <label> <book_dir>: frozen HB-OS measurement protocol (same for 'before' and 'after'; see MEASURE.md).
# Seals -> stage root -> goextract/build/index (hb_os_driver) -> DEV run -> freeze -> ONE locked TEST run -> stamped receipts.
# rc 0 ok, 2 usage, 5 refusal/failure. Never 1. Env (tests): MEASURE_EVAL_DIR MEASURE_RUNS_DIR MEASURE_BUILD_DIR OS_PARAMS_REF.
[ $# -eq 2 ] || { echo "usage: measure.sh <label> <book_dir>" >&2; exit 2; }
LABEL="$1"; BOOKIN="$2"
# explicit allow-list: the pilot pair and the W5b replication pair under the 681dc0b pin; nothing else may run a TEST
case "$LABEL" in before|after|before_681dc0b|after_681dc0b) ;; *) echo "usage: label must be one of before|after|before_681dc0b|after_681dc0b" >&2; exit 2;; esac
[ -f "$BOOKIN/book.md" ] || { echo "usage: $BOOKIN/book.md missing" >&2; exit 2; }
export PATH="$PATH:/usr/local/go/bin" PYTHONDONTWRITEBYTECODE=1
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SELF="$REPO/eval/hb_os/measure.sh"
EVAL="${MEASURE_EVAL_DIR:-$REPO/eval/hb_os}"
RUNS="${MEASURE_RUNS_DIR:-$EVAL/runs}"
BD="${MEASURE_BUILD_DIR:-$REPO/build}/hb_os_$LABEL"
HBT="$REPO/hyperbooks/_tools/hbtools_v2"; DRV="$REPO/hyperbooks/_tools/os/hb_os_driver.py"
TMO="${MEASURE_TMO:-1200}"
die() { echo "$2" >&2; exit "$1"; }
sha1() { sha256sum "$1" | cut -d' ' -f1; }
run() { timeout "$TMO" taskset -c 10-14 "$@"; }

# 1. TEST-once guard (before any work) and seals
# Committed pilot TEST receipts are sealed by sha256 (measured at a6027ab); a label whose receipt is committed can never re-run,
# even if the working-tree file and build/hb_os_<label>/TEST.lock were deleted, and an altered receipt is refused first.
declare -A TSEAL=([before]=9921be0be4f97f809009c1b0f3ba479f834af56eb75538620f0c1bce35a41799 [after]=245cc3677750f8b2a22db3b246765bc2ba095e8d40843d25463bab1546a1063d)
if [ -n "${TSEAL[$LABEL]:-}" ]; then
  [ ! -e "$RUNS/run_test_$LABEL.json" ] || [ "$(sha1 "$RUNS/run_test_$LABEL.json")" = "${TSEAL[$LABEL]}" ] || die 5 "E_SEAL run_test_$LABEL.json differs from its committed seal"
  CB="$(git -C "$REPO" show "HEAD:eval/hb_os/runs/run_test_$LABEL.json" 2>/dev/null | sha256sum | cut -d' ' -f1)"
  [ "$CB" = "${TSEAL[$LABEL]}" ] && die 5 "E_TEST_ONCE label '$LABEL' has a committed sealed TEST receipt"
fi
[ ! -e "$RUNS/run_test_$LABEL.json" ] && [ ! -e "$BD/TEST.lock" ] || die 5 "E_TEST_ONCE label '$LABEL' already has a TEST run"
for s in dev/hb_dev heldout/hb_test; do
  [ -f "$EVAL/$s.jsonl" ] && [ -f "$EVAL/$s.sha256" ] || die 5 "E_SEAL missing $s.jsonl or $s.sha256"
  [ "$(sha1 "$EVAL/$s.jsonl")" = "$(cut -d' ' -f1 "$EVAL/$s.sha256")" ] || die 5 "E_SEAL $s sha mismatch"
done
DEVSHA="$(sha1 "$EVAL/dev/hb_dev.jsonl")"; TESTSHA="$(sha1 "$EVAL/heldout/hb_test.jsonl")"

# 2. stage root (HBV2_ROOT) in the build dir
ROOT="$BD/root"
rm -rf "$ROOT" "$BD/extract"; mkdir -p "$ROOT/eval/bench" "$RUNS" "$BD/extract" || die 5 "mkdir failed"
cp -r "$BOOKIN" "$ROOT/HB-OS-cubook" || die 5 "copy book failed"
cp "$EVAL/dev/hb_dev.jsonl" "$EVAL/heldout/hb_test.jsonl" "$ROOT/eval/bench/" || die 5 "stage queries failed"
PREF="${OS_PARAMS_REF:-8120aab:params.json}"
git -C /home/user/Documentos/vision-engine show "$PREF" > "$ROOT/params.json" 2>/dev/null || die 5 "params.json unavailable ($PREF)"
export HBV2_ROOT="$ROOT" HBV2_SUBJECT="$REPO" HBV2_BOOK_SUB=HB-OS-cubook HBV2_BENCH="$ROOT/eval/bench"
export HBV2_SRC_DIRS="src/cubook,cmd/srf-cu-exec,cmd/srf-cu-mcp,cmd/srf-cu-snap,cmd/srf-cu-wlcap"
cd "$REPO" || exit 5

# 3. build + index (same driver, same params for every label)
run python3 -B "$DRV" extract --out "$BD/extract" >/dev/null || die 5 "extract failed"
# hb_build.derive sub-anchors symbols > SYM_MAX with the Python-AST hb_pack, which cannot parse Go (driver gap, see MEASURE.md D1):
# the build runs the same driver with hb_pack.subanchor disabled, identically for every label.
run python3 -B - "$DRV" <<'PY' >/dev/null 2>"$BD/build.err"; BRC=$?
import sys, importlib.util
spec = importlib.util.spec_from_file_location("hb_os_driver", sys.argv[1]); D = importlib.util.module_from_spec(spec); spec.loader.exec_module(D)
D.install(); D.B.P.subanchor = lambda *a, **k: None
sys.exit(D.main(["build"]))
PY
# rc 3 (E_BOOK_LINK) is accepted only when every issue is a section covering a *_test.go file (tests are outside the module set); count is stamped.
if [ $BRC -ne 0 ]; then
  [ $BRC -eq 3 ] && ! grep -v '^E_BOOK_LINK covers unknown module .*_test\.go$' "$BD/build.err" | grep -q . || { cat "$BD/build.err" >&2; die 5 "build failed"; }
fi
BISS=$(grep -c '^E_BOOK_LINK' "$BD/build.err")
run python3 -B "$DRV" index >/dev/null || die 5 "index failed"

stamp() { # stamp <receipt> <split>
  python3 -B - "$1" "$2" "$LABEL" "$DEVSHA" "$TESTSHA" "$ROOT" "$PREF" "$SELF" "$DRV" "$BOOKIN/book.md" "$REPO" "$BD/freeze_sha256" "$BISS" <<'PY'
import hashlib, json, subprocess, sys, os
rc, split, label, dev, test, root, pref, selfp, drv, bookmd, repo, frz, biss = sys.argv[1:]
h = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
idx = hashlib.sha256()
for n in ("cols.json", "post.json", "vocab.json", "ID"):
    idx.update(n.encode() + b"\0" + open(os.path.join(root, "HB-OS-cubook", "index", n), "rb").read())
d = json.load(open(rc))
d["stamp"] = {"label": label, "split": split, "systems": ["v2", "B1"], "os_head": subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
              "dev_queries_sha256": dev, "test_queries_sha256": test, "index_sha256": idx.hexdigest(), "params_ref": pref, "params_sha256": h(os.path.join(root, "params.json")),
              "protocol_sha256": h(selfp), "driver_sha256": h(drv),
              "driver_head_sha256": hashlib.sha256(subprocess.run(["git", "-C", repo, "show", "HEAD:hyperbooks/_tools/os/hb_os_driver.py"], capture_output=True).stdout).hexdigest(), "book_md_sha256": h(bookmd), "build_link_issues_test_files": int(biss),
              "freeze_sha256": open(frz).read().strip() if os.path.exists(frz) else None}
json.dump(d, open(rc, "w"), indent=1, sort_keys=True)
PY
}

# 4. DEV run (in-sample), then freeze (book copy under the name hb_freeze hashes, params, queries)
run python3 -B "$HBT/hb_eval.py" --split DEV --systems v2,B1 --jobs 5 --out "$RUNS/run_dev_$LABEL.json" >/dev/null || die 5 "DEV run failed"
cp -r "$ROOT/HB-OS-cubook" "$ROOT/book" || die 5 "freeze copy failed"
run python3 -B "$HBT/hb_freeze.py" --write --root "$ROOT" >/dev/null || die 5 "freeze write failed"
sha1 "$ROOT/out/freeze.json" > "$BD/freeze_sha256"
stamp "$RUNS/run_dev_$LABEL.json" DEV || die 5 "stamp DEV failed"
run python3 -B "$HBT/hb_freeze.py" --verify --root "$ROOT" >/dev/null || die 5 "freeze verify failed"

# 5. the ONE locked TEST run
echo "locked" > "$BD/TEST.lock"
run python3 -B "$HBT/hb_eval.py" --split TEST --lock --systems v2,B1 --jobs 5 --out "$RUNS/run_test_$LABEL.json" >/dev/null || die 5 "TEST run failed"
stamp "$RUNS/run_test_$LABEL.json" TEST || die 5 "stamp TEST failed"
echo "measure $LABEL ok: dev=$RUNS/run_dev_$LABEL.json test=$RUNS/run_test_$LABEL.json" >&2
exit 0
