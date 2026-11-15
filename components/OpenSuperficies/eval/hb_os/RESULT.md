# RESULT: HB-OS-cubook pilot (W5a), TEST FIND v2 reciprocal rank, before vs after

Recomputed from `eval/hb_os/runs/run_test_{before,after}.json` by `python3 -B eval/hb_os/compare.py` (test: `eval/hb_os/tests/test_result.py`). The receipts are not rewritten and no new TEST run was made in W5a. Pins: book @5a186d9, receipts @a6027ab.

## Wording (judge JW5A)
The pilot **met the pre-registered threshold narrowly (exact p = 23/512 = 0.0449); the effect is concentrated in 12/60 queries; it needs replication.** It is not a general claim that the book improves routing.

## Numbers [M]
| item | before | after |
|---|---|---|
| TEST FIND n | 60 | 60 |
| v2 MRR | 0.312 | 0.363 |
| v2 recall@1 / recall@5 | 0.233 / 0.433 | 0.267 / 0.483 |
| v2 nDCG@5 | 0.342 | 0.393 |
| v2 ABSENT F1 (guardrail, tolerated drop 0.05) | 0.584 | 0.593 |
| B1 MRR (secondary, not claimed) | 0.285 | 0.280 |

Primary endpoint: paired two-sided sign-flip over the per-query RR delta, exact because the non-zero deltas are 12 (<= 20): **10 wins / 2 losses**, 48 ties, mean delta RR +0.0508, **p = 23/512 = 0.0449** (< 0.05, mean > 0, ABSENT F1 not lower). Sensitivity: dropping the single smallest win (delta 1/20) gives p = 0.0596, so the result sits on the threshold. The v2-vs-B1 permutation p after is 0.006 (before 0.199); secondary, not claimed. DEV results are in-sample and not claimed.

## Errata (PREREG.md is sealed, sha256 264502f7... unchanged, not edited)
`PREREG.md:50` cites the frozen params as `sha256: be9da324...`. The stamps of both receipts carry `params_sha256` = `20c54c82...a168` (`git -C vision-engine show 8120aab:params.json`), identical before and after. The text value is wrong; the stamped value is the one used. Seal kept; this erratum lives here.

## Parity notes
1. `build_link_issues_test_files`: 11 (before) vs 0 (after). The skeleton `before` book emitted sections covering `*_test.go` files; `measure.sh` accepted those 11 `E_BOOK_LINK` issues (MEASURE.md D2) and stamped the count.
2. `after` ran at 5a186d9 with driver sha256 `bb0d9423...` (identical to before). INT then changed `hb_os_driver.py` (82b53cd, sha256 `363291ad...`), so HEAD cannot re-derive the `after` index byte-for-byte without measure.sh's own sub-anchor disable. The W5b replication (`before_681dc0b`, `after_681dc0b`, both re-run under the 681dc0b pin) is the clean comparison.
3. `measure.sh` accepts only the labels before|after|before_681dc0b|after_681dc0b, and refuses (E_TEST_ONCE / E_SEAL) any label whose committed TEST receipt is sealed (`run_test_before.json` 9921be0b..., `run_test_after.json` 245cc367...).

## Cost per book [M where a ledger exists, else AUSENTE]
Only the Opus cards room has a ledger with a price (`w5/a/room/cache_ledger.json`: CW5A USD 1.1303, 166 s). Sonnet and Opus-judge rooms have wall time only (`times.log` t0..end); price is `precio_ausente`, not guessed. Lane E (Gemini) has no times.log: AUSENTE.

| room / lane | seat | wall s | USD |
|---|---|---|---|
| CW5A cards | opus | 166 | 1.1303 |
| R1 T (gate entry) | sonnet | 1198 | precio_ausente |
| R1 E (query set) | gemini | AUSENTE | AUSENTE |
| MB (before measurement) | sonnet | 239 | precio_ausente |
| R2 B (book) | sonnet | 628 | precio_ausente |
| R2 K (contracts) | sonnet | 931 | precio_ausente |
| INT | sonnet | 760 | precio_ausente |
| JW5A judge | opus | 1210 | precio_ausente |

Known total: USD 1.13 priced (CW5A, 166 s) + 4966 s of wall without a price (T, MB, B, K, INT, judge). Book-only wall (lane B) 628 s. The W5_PLAN estimate was USD 8-14 [T]; it cannot be confirmed or refuted from the available ledgers.

## Replication under the language-neutral hbtools pin 681dc0b (W6, conductor, 2026-10-18)
Same sealed queries, same frozen measure.sh, same params; tool pin vision-engine rt 681dc0b with the OS bundle path (no monkeypatch);
book = HB-OS-cubook at 80f8de1 (post-FX prose, regenerated rows). One TEST run per label (before_681dc0b, after_681dc0b); stamps equal.
| system | MRR before | MRR after | wins / losses | exact sign-flip p |
|---|---|---|---|---|
| v2 | 0.314 | 0.363 | 9 / 2 | 61/1024 = 0.0596 |
| B1 (control) | 0.286 | 0.281 | 2 / 3 | 3/8 = 0.375 |
ABSENT F1 v2 0.584 -> 0.593. Reading: the effect direction and size reproduce (+0.049 MRR, B1 flat), but the replication does NOT
meet the pre-registered p < 0.05 (0.0596). Combined honest statement: "the book gives a small, consistent MRR gain (~+0.05) on 60
held-out FIND queries; one run passed the threshold narrowly (p 0.045), the replication did not (p 0.060); not yet established".
Not an independent replication (same TEST set); a larger held-out set is the next step if routing gains matter for W6/W7.
