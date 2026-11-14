# RESULT_TUNED: router v2 tuned on blind DEV, measured once on the sealed TEST set

Scope: held-out TEST, blind author, tuned on blind DEV. TEST `queries_all_test.jsonl` sha256 cd9bc2dd880c38ddcba1a9d92012fe411fa408e6228fb35b5eee939b8950d80d (n = 70 FIND, 50 ABSENT, 0 CLAIM; fail-closed rc 3 on mismatch). DEV `queries_all_dev.jsonl` sha256 b6bce6f82944eb1dced8254bdab04383a97eb992bd0b39ba94ff032ce846809b (70 FIND, 50 ABSENT, blind author Argon, never saw TEST). Tuner: `hbtools_v2/hb_tune.py` through `tune_dev.py`, DEV only (the tuner refuses TEST paths and TEST-labelled rows, rc 4). Same corpus as `heldout/run_after.json` (10 books, `book.md` sha256 8f0bba4931b7e48fbf719f4a1cbfc0a8731effb3add47e830de95d8e5f28f331, corpus_rev 956e1bb), same `src_dirs`, `taskset -c 5-9`. Commands: `RUNBOOK_TUNED.md`. Receipts here: `run_dev_before.json`, `tune_report.json`, `../../../params.json`, `run_dev_after.json`, `run_test_tuned.json` (stamped with `queries_sha256`/`test_sha256`, `dev_sha256`, `params_sha256`). The TEST run was executed once, after params.json was frozen.

## Tuner choices (DEV only)
- Stage 2 FIND (36 configs): default J 0.675437, best 0.749821; parsimony + promotion accepted `profile S2, lcp5 1, w_body 500, w_card 0` (dJ +0.074385).
- Stage 3 abstention fit: `m_min 2, tau_cov 0, tau_oov 360389` (DEV fit F1 0.800, 40 tp, 10 abstained answerable).
- Skipped honestly: stage 1 tau_rel (the drift suite targets the original HB-v2 subject, absent here; default 250000 kept), stage 4 IMPACT (0 DEV IMPACT rows; default k_e 1 / d_max 1 / S0), stage 5 visual (no `visual_DEV.json`). The grid is vendored as `grid_hb.json` (sha256 83cfcb81...baa8b; `standards/` is not in this repo).

## DEV (tuned on it, descriptive only)
| system | run | FIND R@1 | R@5 | MRR | ABSENT P | ABSENT R |
|---|---|---|---|---|---|---|
| v2 | default (before) | 0.486 | 0.757 | 0.594 | 1.000 | 0.020 |
| v2 | tuned (after) | 0.600 | 0.800 | 0.682 | 0.800 | 0.800 |
| B1 | both | 0.486 | 0.714 | 0.579 | null | 0.000 |

## TEST (held-out, n = 70 FIND, 50 ABSENT)
| system | run | FIND R@1 | R@5 | MRR | nDCG@5 | ABSENT P | ABSENT R | ABSENT F1 | tp/fp/fn |
|---|---|---|---|---|---|---|---|---|---|
| v2 | untuned (heldout/run_after) | 0.414 | 0.586 | 0.483 | 0.500 | 1.000 | 0.020 | 0.039 | 1/0/49 |
| v2 | tuned (run_test_tuned) | 0.500 | 0.729 | 0.592 | 0.617 | 0.857 | 0.600 | 0.706 | 30/5/20 |
| B1 | both | 0.429 | 0.671 | 0.516 | 0.547 | null | 0.000 | 0.000 | 0/0/50 |

## Test (paired sign-flip on per-query RR, two-sided; `eval/hb_router/compare.py`)
- v2 untuned vs tuned: n = 70, 29 non-zero deltas, sum dRR = +23633/3080 (about +7.67), p = 0.0002 (29 > 20 non-zero: Monte-Carlo branch, 100000 flips, seed 0, p_exact 29/100001). p < 0.05.
- B1 control untuned vs tuned run: identical (0 non-zero deltas, p = 1), as expected: B1 does not use params.
- In-run v2 vs B1 (`perm_p`, tuned run): p = 0.009, v2 above B1 on MRR (0.592 vs 0.516). Not the before/after test.

## Claim
On the held-out TEST, blind author, tuned on blind DEV: tuning router v2 on DEV moves FIND MRR 0.483 -> 0.592 (R@5 0.586 -> 0.729) and ABSENT recall 0.02 -> 0.60 (precision 1.00 -> 0.857), paired sign-flip p = 0.0002 < 0.05. The root cause named in the card (no params.json, hence no abstention and default ranking) is consistent with this result: params.json adds the calibrated abstention and the S2/lcp5/w_body FIND profile.
Limits: one assembled 10-book corpus (not per-book routing); the sealed TEST rows run through the hb_eval DEV path (no freeze); IMPACT, drift and visual stages not tuned; the p value uses the Monte-Carlo branch; ABSENT precision fell from 1.00 to 0.857 (5 abstentions, fp, on answerable rows; whether they cost FIND recall is already inside the FIND numbers above).
