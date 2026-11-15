# MEASURE: frozen before/after protocol (`eval/hb_os/measure.sh`)

`bash eval/hb_os/measure.sh <label> <book_dir>` (use `taskset -c 10-14`). One protocol for both `before` and `after` (AW5-03 parity). rc: 0 ok, 2 usage, 5 refusal or failure; never 1.

## Steps
1. **Guards (before any work)**: refuse (rc 5, `E_TEST_ONCE`) if `eval/hb_os/runs/run_test_<label>.json` or `build/hb_os_<label>/TEST.lock` exists. Refuse (rc 5, `E_SEAL`) if `dev/hb_dev.{jsonl,sha256}` or `heldout/hb_test.{jsonl,sha256}` is missing or the sha differs.
2. **Stage** `build/hb_os_<label>/root/`: copy of `<book_dir>` as `HB-OS-cubook/`, the two query files under `eval/bench/`, `params.json` from `git -C vision-engine show 8120aab:params.json` (pin, sha stamped).
3. **Build + index**: `hyperbooks/_tools/os/hb_os_driver.py` extract, build, index (env `HBV2_ROOT/SUBJECT/BOOK_SUB/BENCH/SRC_DIRS`, same as `hb_os_gates.sh`).
4. **DEV run**: `hb_eval.py --split DEV --systems v2,B1` -> `runs/run_dev_<label>.json` (in-sample).
5. **Freeze**: book copied to `root/book/`, `hb_freeze.py --write` (hashes book+index, params, queries), then `--verify`.
6. **ONE locked TEST run**: `TEST.lock` written, `hb_eval.py --split TEST --lock --systems v2,B1` -> `runs/run_test_<label>.json`. No retry: a second run for the label is refused.
7. **Stamp** (key `stamp` in both receipts): label, os_head, dev/test query sha256, index_sha256 (cols+post+vocab+ID), params_ref/sha256, freeze_sha256, protocol_sha256 (measure.sh), driver_sha256, book_md_sha256, build_link_issues_test_files.

`after` must carry identical `params_sha256`, `dev/test_queries_sha256`, `driver_sha256`, `protocol_sha256`; only `index_sha256` and `book_md_sha256` differ.

## Deviations of the protocol (declared, identical for every label)
- **D1**: `hb_build.derive` sub-anchors symbols > `SYM_MAX` with the Python-AST `hb_pack`, which cannot parse Go (gap of hb_os_driver.py, lane T). measure.sh runs the same driver with `hb_pack.subanchor` disabled (no `#bN` sub-anchors).
- **D2**: `scaffold-before` emits sections for `*_test.go` files that the module set excludes; build rc 3 (`E_BOOK_LINK covers unknown module *_test.go`) is accepted only when every issue is of that form (count stamped; 11 for the skeleton).

## Metrics (from `hb_eval.py`, exact fractions as 3-decimal strings)
FIND: recall@1, recall@5, MRR, nDCG@5. ABSENT: abstention precision/recall/F1 (rows with no gold; a system abstains when it answers nothing). CLAIM: verdict accuracy (DEV only). Systems: `v2` (book-aware), `B1` (plain BM25F). Per-query RR is in `rr` of each receipt.

## After-vs-before comparison (primary endpoint: v2 RR on TEST FIND)
Paired two-sided sign-flip (`hb_eval.sign_flip_p`: exact for n<=20 non-zero deltas, else 100000 flips, Random(0)); success = p<0.05 and mean delta > 0.
```
python3 -B -c 'import sys,json;sys.path.insert(0,"hyperbooks/_tools/hbtools_v2");from fractions import Fraction as F;import hb_eval as H
a,b=[json.load(open("eval/hb_os/runs/run_test_%s.json"%l)) for l in ("after","before")]
for k in ("params_sha256","test_queries_sha256","driver_sha256","protocol_sha256"): assert a["stamp"][k]==b["stamp"][k],k
ra,rb=a["rr"]["v2"],b["rr"]["v2"];d=[F(ra[q])-F(rb[q]) for q in sorted(ra) if q in rb]
print("n",len(d),"mean_dRR",float(sum(d)/len(d)),"p",float(H.sign_flip_p(d)))'
```
ABSENT guardrail: `metrics.v2.ABSENT.f1` after must not be lower than before by more than 0.05. Claim only on success; otherwise the wording is "did not move".
