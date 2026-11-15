# RESULT: hb_eval router recall before/after the HB-VC-s1-desc migration (lane E-post)

Scope: in-sample DEV, not held-out. n = 25 FIND, 22 ABSENT, 4 CLAIM (queries_s1.jsonl sha256 8d3dbc9bbe0c6487bc8b7bacae94258c6bed0be759b8abf3c6efc69a2835fe3d, sealed at 7831bc3 02:29:22, before the S1 migration ccfdbbd 02:46:15).
Receipts: `run_before.json` (book.md sha256 b08b2b1fad3ba43e82748edeafae7ce83360a3103f70e421209d18d134f90523), `run_after.json` (book.md sha256 400b9471ec0fb02e8dd826c9e8f956da6c65a5c677a3d002e58765465149585b). Same env, params and query file; only book.md differs. Pinned `taskset -c 5-9`.

## FIND (raw, n=25)
| system | run | recall@1 | recall@5 | MRR | nDCG@5 |
|---|---|---|---|---|---|
| v2 | before | 0.680 | 0.760 | 0.713 | 0.725 |
| v2 | after | 0.680 | 0.840 | 0.741 | 0.766 |
| B1 | before | 0.720 | 0.760 | 0.733 | 0.740 |
| B1 | after | 0.680 | 0.760 | 0.710 | 0.722 |

## ABSENT (n=22 absent rows)
| system | run | precision | recall | F1 | tp/fp/fn |
|---|---|---|---|---|---|
| v2 | before | 0.941 | 0.727 | 0.821 | 16/1/6 |
| v2 | after | 1.000 | 0.591 | 0.743 | 13/0/9 |
| B1 | before | null | 0.000 | 0.000 | 0/0/22 |
| B1 | after | null | 0.000 | 0.000 | 0/0/22 |

## Test (paired sign-flip on per-query RR, two-sided, exact; `compare.py`)
- v2 before vs after: n=25, 4 non-zero deltas, sum dRR = +7/10, p = 3/4 (0.750). p >= 0.05: **did not move**.
- B1 before vs after (control): n=25, 2 non-zero, sum dRR = -7/12, p = 1/2 (0.500): did not move.

## Reading
No improvement is claimed. v2 recall@5 and MRR are numerically higher after (0.760 -> 0.840, 0.713 -> 0.741) but the change is 4 queries and not significant. ABSENT recall fell (0.727 -> 0.591) while precision rose to 1.000 (the router abstains more; 3 more ABSENT rows are missed). CLAIM: accuracy 0.000 both runs (n=4), not interpreted.
Limit: all FIND gold sids are in `visioncore/desc.py`; the query set was written by the campaign that wrote the book.
