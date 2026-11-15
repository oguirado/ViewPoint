# RUNBOOK: held-out router evaluation (lane E0 BEFORE, lane E1 AFTER)

Query set: `queries_all_test.jsonl` (120 rows: 70 FIND, 50 ABSENT), sealed, sha256 in `queries_all_test.sha256` (`cd9bc2dd...0d80d`). Never edited, never read by book writers.
Systems `v2` (book-aware BM25F + abstention) and `B1` (plain BM25F); recall@1/@5, MRR, ABSENT P/R/F1; paired sign-flip on RR to claim (p < 0.05).

## What the command does
1. Snapshots the repo into `$SCR/snap`: `REV` = a git rev (`7199c81` = BEFORE, 1 of 10 books migrated) or `WORKTREE` (tracked + untracked-not-ignored files; use for AFTER).
   All tools (`hbtools_v2/`), sources and books are read from the snapshot, so a rev run depends on nothing but git.
2. Fails closed (rc 3) if the query file sha differs from the `.sha256`.
3. Copies the sealed rows to a scratch bench with `split` relabelled `DEV` (hb_eval's TEST path needs a freeze; only the `split` field changes, texts untouched).
4. Corpus = ONE HB root: `book.md` is the concatenation (`LC_ALL=C` sort of the directory names, one blank line between) of every `hyperbooks/HB-V*/book.md` (10 books).
   `HBV2_BOOK_SUB=.`, `HBV2_SUBJECT=<snap>`, `HBV2_SRC_DIRS` = `visioncore/,vmem/,vadapt/,vnative/` + 20 curated stable unit-test modules (see script).
5. `hb_extract2` -> `hb_trace` -> `hb_build --render-derived` -> `hb_index` -> `hb_eval --split DEV --systems v2,B1 --jobs 4`, pinned `taskset -c 5-9`.
   `hb_build` exits rc 3 with `E_BOOK_LINK` issues for symbols no migrated section governs; the files are still written (the script accepts exactly rc 0|3).
6. Stamps the receipt: `queries_sha256`, `book_md_sha256` (assembled corpus book before `hb_build` re-renders it), `corpus_books_sha256` (per book), `corpus_rev`, `src_dirs`.

## Exact commands (repo root; quiet host; ~35 s per run)
Step 0, materialise the script from this file (`LANE` = `E0` or `E1`):
```
mkdir -p build/room_$LANE && python3 -I -c "import re,sys;t=open('eval/hb_router/heldout/RUNBOOK.md',encoding='utf-8').read();open(sys.argv[1],'w',encoding='utf-8').write(re.search(r'<!-- SCRIPT -->\n```bash\n(.*?)```',t,re.S).group(1))" build/room_$LANE/runbook.sh
```
BEFORE (lane E0; committed receipt, byte-identical on rerun, the scratch dir name does not matter):
```
bash build/room_E0/runbook.sh 7199c81 $PWD/eval/hb_router/heldout/run_before.json $PWD/build/room_E0/before
```
AFTER (lane E1, after M1..M4 and the admission lockstep are in the working tree):
```
bash build/room_E1/runbook.sh WORKTREE $PWD/eval/hb_router/heldout/run_after.json $PWD/build/room_E1/after
```
`OUT` and `SCR` must be absolute. `REV` = `WORKTREE` snapshots tracked + untracked-not-ignored files.

## Script
<!-- SCRIPT -->
```bash
set -euo pipefail
export LC_ALL=C
REV=$1; OUT=$2; SCR=$3
REPO=$(git rev-parse --show-toplevel)
rm -rf "$SCR"; mkdir -p "$SCR/snap" "$SCR/bench"
cd "$REPO"
if [ "$REV" = WORKTREE ]; then
  git ls-files -co --exclude-standard -z | tar --null --ignore-failed-read -T - -cf - | tar -xf - -C "$SCR/snap"
else
  git archive "$REV" | tar -xf - -C "$SCR/snap"
fi
cd "$SCR/snap"
Q=eval/hb_router/heldout/queries_all_test.jsonl
[ "$(sha256sum $Q | cut -d' ' -f1)" = "$(cut -d' ' -f1 eval/hb_router/heldout/queries_all_test.sha256)" ] || { echo "E_SEAL query sha mismatch" >&2; exit 3; }
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
$P/hb_build.py --render-derived >/dev/null 2>&1 || [ $? -eq 3 ]   # rc 3 = E_BOOK_LINK issues (unmigrated books), files are still written
$P/hb_index.py >/dev/null
$P/hb_eval.py --split DEV --systems v2,B1 --jobs 4 --out "$SCR/run_raw.json" >/dev/null
python3 -I - "$SCR/run_raw.json" "$Q" "$SCR/book.assembled.md" "$REV" "$OUT" <<'PY'
import hashlib, json, glob, os, sys
run, q, book, rev, out = sys.argv[1:]
sha = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
doc = json.load(open(run, encoding="utf-8"))
doc["queries_sha256"] = sha(q)
doc["book_md_sha256"] = sha(book)
doc["corpus_books_sha256"] = {os.path.dirname(p): sha(p) for p in sorted(glob.glob("hyperbooks/HB-V*/book.md"))}
doc["corpus_rev"] = rev
doc["src_dirs"] = os.environ["HBV2_SRC_DIRS"].split(",")
doc["queries_note"] = "sealed TEST rows run through hb_eval DEV path with split relabelled DEV in a scratch copy (hb_eval TEST path needs a freeze); texts untouched"
with open(out, "w", encoding="utf-8") as f:
    json.dump(doc, f, sort_keys=True, indent=1)
    f.write("\n")
PY
```

## Receipt (BEFORE)
`run_before.json`: sha256 recorded in REPORT_E0.md. Metrics per `tests/test_hb_router_heldout.py` (query sha, book shas, reproduction).
Declared: `perm_p.v2_vs_B1` in the receipt is v2 vs B1 on the same run (not the before/after test; E1 computes that).
