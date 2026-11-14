# PREREG: hb_eval router recall before/after the HB-VC-s1-desc migration (lane E)

Status: sealed by E-pre, before lane S1 writes anything. Query file: `queries_s1.jsonl`, sha256 in `queries_s1.sha256`.
Method per HB-v2 §7.11.1 (row format), §7.12 A12 and HB_DECISIONS §2.7. The numbers of the HB-v2 pilot (C1) are method only, never engine claims.

## Query set (DEV only)
51 rows, row format of §7.11.1: 25 FIND (gold = `visioncore/desc.py::<qualname>`, 1 sid each), 22 ABSENT (plausible, absent from desc.py),
4 CLAIM (receipt `file.json:key` under the book root, verdict supported/contradicted). `split` is always `DEV`; no TEST rows, no freeze.
Texts avoid symbol names (checked by `tests/test_hb_router_prereg.py`). Sources: git history (dd59992, 1c58b1a, e5fbc17), receipts
(R-s1-desc-unittest, R-s1-desc-bench), the pre-migration book text; recorded per row in `source_event`.
Scope declared: all FIND gold sids are in `visioncore/desc.py` (ten functions, so several queries share a gold sid); grid.py, layout.py and vnative/* are
outside the extracted source set (`HBV2_SRC_DIRS=visioncore/desc.py,tests/test_desc.py`), hence the narrower set. n >= 20 per class: `thin_dev` = false.

## Metrics
FIND: recall@1, recall@5, MRR (exact Fractions, 3-decimal strings, from `hb_eval.py`). ABSENT: abstention precision, recall, F1.
Systems: `v2` (book-aware BM25F + abstention) and `B1` (plain BM25F, no book).

## Runs
before: `v2,B1` over the index built from the current (pre-migration) `HB-VC-s1-desc/book.md`; after: same command over the migrated book, same env, same params.
Only the book changes. Indices are built in `build/room_E/{before,after}/`. Each receipt is stamped by `stamp.py` with the query sha256.

## Test and claim
Paired sign-flip permutation test on per-query RR of `v2` (before vs after), two-sided, exact for n <= 20 non-zero deltas, otherwise 100000 flips from Random(0)
(`compare.py`, the algorithm of `hb_eval.sign_flip_p`). An improvement is claimed only if p < 0.05; otherwise the wording is "did not move".
Claim wording: "in-sample DEV, not held-out". The query set was written by the same campaign that wrote the book, so no generalisation claim is made.
