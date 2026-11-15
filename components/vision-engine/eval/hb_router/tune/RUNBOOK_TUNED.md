# RUNBOOK_TUNED: tune router v2 on the sealed blind DEV set, measure once on the sealed blind TEST set

Corpus logic is the one of `eval/hb_router/heldout/RUNBOOK.md` (snapshot, ONE assembled HB root of 10 books, same `HBV2_SRC_DIRS`, `taskset -c 5-9`).
One script, three modes:
- `eval`: `hb_eval --split DEV --systems v2,B1` over `QFILE` (rows relabelled `split=DEV` in a scratch copy). `EXTRA` = a params.json copied into the scratch root (v2 loads `<root>/params.json`) or `NONE` (v2 default, uncalibrated).
- `tune`: `tune_dev.py` (wrapper of `hb_tune.py`: grid vendored as `grid_hb.json` because `standards/` is not in this repo; drift stage 1 skipped (`--skip-drift`, the suite targets the original HB-v2 subject), IMPACT stage skipped honestly (DEV has no IMPACT rows); visual stage skipped, no `visual_DEV.json`) over the scratch root. Only the sealed DEV file is accepted (rc 4 otherwise); `OUT` = params.json, `EXTRA` = tune_report.json.
Fail closed: rc 3 when `QFILE` sha differs from its sibling `.sha256`.

Receipt fields added over the heldout runbook: `queries_sha256`, `params_sha256` (or null), `dev_sha256` (from the tuner receipt inside params.json), `test_sha256` (TEST runs only), `corpus_rev`.

## Exact commands (repo root, quiet host)
```
mkdir -p build/room_R && python3 -I -c "import re,sys;t=open('eval/hb_router/tune/RUNBOOK_TUNED.md',encoding='utf-8').read();open(sys.argv[1],'w',encoding='utf-8').write(re.search(r'<!-- SCRIPT -->\n```bash\n(.*?)```',t,re.S).group(1))" build/room_R/runbook_tuned.sh
D=eval/hb_router/dev/queries_all_dev.jsonl; T=eval/hb_router/heldout/queries_all_test.jsonl; X=eval/hb_router/tune
bash build/room_R/runbook_tuned.sh eval WORKTREE $PWD/$D $PWD/$X/run_dev_before.json $PWD/build/room_R/dev_before NONE
bash build/room_R/runbook_tuned.sh tune WORKTREE $PWD/$D $PWD/params.json $PWD/build/room_R/tune $PWD/$X/tune_report.json
bash build/room_R/runbook_tuned.sh eval WORKTREE $PWD/$D $PWD/$X/run_dev_after.json $PWD/build/room_R/dev_after $PWD/params.json
bash build/room_R/runbook_tuned.sh eval WORKTREE $PWD/$T $PWD/$X/run_test_tuned.json $PWD/build/room_R/test_tuned $PWD/params.json   # the ONE TEST run
python3 eval/hb_router/compare.py eval/hb_router/heldout/run_after.json $X/run_test_tuned.json v2
```
The WORKTREE snapshot excludes nothing of the corpus that params.json or the tune files change (books, sources and tools are unchanged by this lane), so reruns are byte-identical.

## Script
<!-- SCRIPT -->
```bash
set -euo pipefail
export LC_ALL=C
MODE=$1; REV=$2; Q=$3; OUT=$4; SCR=$5; EXTRA=$6
REPO=$(git rev-parse --show-toplevel)
DEVQ="$REPO/eval/hb_router/dev/queries_all_dev.jsonl"
if [ "$MODE" = tune ] && [ "$(readlink -f "$Q")" != "$DEVQ" ]; then echo "E_BLIND tune accepts only the sealed DEV file" >&2; exit 4; fi
[ "$(sha256sum "$Q" | cut -d' ' -f1)" = "$(cut -d' ' -f1 "${Q%.jsonl}.sha256")" ] || { echo "E_SEAL query sha mismatch" >&2; exit 3; }
rm -rf "$SCR"; mkdir -p "$SCR/snap" "$SCR/bench"
cd "$REPO"
if [ "$REV" = WORKTREE ]; then
  git ls-files -co --exclude-standard -z | tar --null --ignore-failed-read -T - -cf - | tar -xf - -C "$SCR/snap"
else
  git archive "$REV" | tar -xf - -C "$SCR/snap"
fi
cd "$SCR/snap"
rm -f params.json
python3 -I -c '
import json,sys
with open(sys.argv[2],"w",encoding="utf-8") as o:
    for l in open(sys.argv[1],encoding="utf-8"):
        if l.strip():
            r=json.loads(l); r["split"]="DEV"; o.write(json.dumps(r,sort_keys=True,ensure_ascii=False)+"\n")
' "$Q" "$SCR/bench/hb_dev.jsonl"
mkdir -p "$SCR/root/out"
for b in $(ls -d hyperbooks/HB-V*/ | sort); do cat "${b}book.md"; echo; done > "$SCR/root/book.md"
cp "$SCR/root/book.md" "$SCR/book.assembled.md"
export HBV2_ROOT="$SCR/root" HBV2_BOOK_SUB=. HBV2_SUBJECT="$SCR/snap" HBV2_BENCH="$SCR/bench" PYTHONDONTWRITEBYTECODE=1
export HBV2_SRC_DIRS=visioncore/,vmem/,vadapt/,vnative/,tests/test_change.py,tests/test_cite.py,tests/test_decode.py,tests/test_desc.py,tests/test_gop.py,tests/test_grid.py,tests/test_pack.py,tests/test_pyramid.py,tests/test_regions.py,tests/test_topology.py,tests/test_parallel.py,tests/test_pipeline.py,tests/test_ramws.py,tests/test_rt1_backend.py,tests/test_rt1_dual.py,tests/test_vmem_store.py,tests/test_vmem_units.py,tests/test_vmem_compact.py,tests/test_vmem_router.py,tests/test_registry.py
P="taskset -c 5-9 python3 hbtools_v2"
$P/hb_extract2.py --out "$SCR/root/out" >/dev/null
$P/hb_trace.py --out "$SCR/root/out/trace.json" >/dev/null
$P/hb_build.py --render-derived >/dev/null 2>&1 || [ $? -eq 3 ]
$P/hb_index.py >/dev/null
if [ "$MODE" = tune ]; then
  python3 "$REPO/eval/hb_router/tune/tune_dev.py" --dev "$SCR/bench/hb_dev.jsonl" --out "$OUT" --report "$EXTRA" --root "$SCR/root" >"$SCR/tune_stdout.json"
  exit 0
fi
if [ "$EXTRA" != NONE ]; then cp "$EXTRA" "$SCR/root/params.json"; fi
$P/hb_eval.py --split DEV --systems v2,B1 --jobs 4 --out "$SCR/run_raw.json" >/dev/null
python3 -I - "$SCR/run_raw.json" "$Q" "$SCR/book.assembled.md" "$REV" "$OUT" "$EXTRA" <<'PY'
import hashlib, json, glob, os, sys
run, q, book, rev, out, extra = sys.argv[1:]
sha = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
doc = json.load(open(run, encoding="utf-8"))
doc["queries_sha256"] = sha(q)
doc["book_md_sha256"] = sha(book)
doc["corpus_books_sha256"] = {os.path.dirname(p): sha(p) for p in sorted(glob.glob("hyperbooks/HB-V*/book.md"))}
doc["corpus_rev"] = rev
doc["src_dirs"] = os.environ["HBV2_SRC_DIRS"].split(",")
doc["params_sha256"] = None if extra == "NONE" else sha(extra)
doc["dev_sha256"] = None if extra == "NONE" else json.load(open(extra, encoding="utf-8")).get("dev_sha256")
if "queries_all_test" in os.path.basename(q):
    doc["test_sha256"] = doc["queries_sha256"]
doc["queries_note"] = "sealed rows run through hb_eval DEV path with split relabelled DEV in a scratch copy (hb_eval TEST path needs a freeze); texts untouched"
with open(out, "w", encoding="utf-8") as f:
    json.dump(doc, f, sort_keys=True, indent=1)
    f.write("\n")
PY
```
