# RESULT: held-out router recall, 1 of 10 books hybrid (BEFORE) vs all 10 hb-hybrid/v1 (AFTER)

Scope: held-out TEST, blind author (Argon, blind to hyperbooks/). n = 70 FIND, 50 ABSENT, 0 CLAIM. Query file `queries_all_test.jsonl` sha256 cd9bc2dd880c38ddcba1a9d92012fe411fa408e6228fb35b5eee939b8950d80d (checked by the runbook, fail-closed rc 3). Rows run through the hb_eval DEV path with `split` relabelled (see RUNBOOK.md).
Receipts: `run_before.json` (corpus_rev 7199c81, book.md sha256 0dda8e8ddcfd1257ac65b24d59d8d19ef001fbbc458caac1aee403a43e2cc3e7), `run_after.json` (corpus_rev WORKTREE = f4579bc + wave C profiles, book.md sha256 8f0bba4931b7e48fbf719f4a1cbfc0a8731effb3add47e830de95d8e5f28f331, per-book shas in the receipt). Same query file, same `src_dirs`, same params; the 9 migrated books differ. Pinned `taskset -c 5-9`.

## FIND (raw, n=70)
| system | run | recall@1 | recall@5 | MRR | nDCG@5 |
|---|---|---|---|---|---|
| v2 | before | 0.386 | 0.529 | 0.453 | 0.462 |
| v2 | after | 0.414 | 0.586 | 0.483 | 0.500 |
| B1 | before | 0.429 | 0.657 | 0.517 | 0.543 |
| B1 | after | 0.429 | 0.671 | 0.516 | 0.547 |

## ABSENT (n=50)
| system | run | precision | recall | F1 | tp/fp/fn |
|---|---|---|---|---|---|
| v2 | before | 1.000 | 0.020 | 0.039 | 1/0/49 |
| v2 | after | 1.000 | 0.020 | 0.039 | 1/0/49 |
| B1 | both | null | 0.000 | 0.000 | 0/0/50 |

## Test (paired sign-flip on per-query RR, two-sided; `eval/hb_router/compare.py`)
- v2 before vs after: n=70, 22 non-zero deltas, sum dRR = +18817/9240 (about +2.04), p = 0.161 (22 > 20 non-zero, so the Monte-Carlo branch of compare.py: 100000 flips, seed 0, p_exact 16106/100001). p >= 0.05: **did not move**.
- B1 before vs after (control): n=70, 11 non-zero, sum dRR = -2663/32760, p = 479/512 (0.936): did not move.
- In-run v2 vs B1 (`perm_p` in the receipts): before p = 0.040, after p = 0.267. Not the before/after test.

## Reading
No improvement is claimed. v2 is numerically higher after (recall@5 0.529 -> 0.586, MRR 0.453 -> 0.483) but p = 0.161 >= 0.05. B1 (no book) is flat, as expected for a control. v2 ABSENT behaviour is unchanged (recall 0.020: the router almost never abstains on this set). B1 beats v2 on FIND in both runs (descriptive; the in-run v2-vs-B1 p was 0.040 before and 0.267 after).
Limits: the corpus is one assembled HB root of all 10 books (not per-book routing); `hb_build` reports E_BOOK_LINK rc 3 (files still written, runbook accepts 0|3); the sealed rows are evaluated via the DEV path (no freeze).
