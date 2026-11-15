# HB-BVQA-REPORT — report reconciliation and atomic output (`blender_visual_qa.report`)

Status: active · Subject commit `2390e8d` · Normative sources: `v12bs/BSPEC_BINT.md` R-B3 (stage rc), R-B4 (report), inherited `vqa.report/v1`. Etags: CITED (spec or code at the subject commit), TARGET. No receipts: no MEASURED claim.

## <a id="hb-s0"></a><a id="pm-s0"></a>0. Reading map

| Persona | Read | Question → section |
|---|---|---|
| implementer | hb-s1, hb-s5 | What does `reconcile` return? → hb-s1 |
| maintainer | hb-s4, hb-s7 | Why ERROR beats FAIL beats INCOMPLETE? → hb-s7 |
| verifier | hb-s4, hb-s6 | When is a run `qualified`? → hb-s4 |

Prerequisites: HB-BVQA-ORACLES (findings), HB-BVQA-JUDGE (judge results). Conventions: JSON canonical, sorted, integer-only; publish only after complete validation.

## <a id="hb-s1"></a><a id="pm-s1"></a>1. Contracts

`reconcile(config, capture_manifest, deterministic_results, judge_results, scope, command, stages, errors, incomplete, selftest, metrics, bindings, run_id)` → `vqa.report/v1` dict with `rc`, `ok`, `verdict`, `qualified`, `summary{checks,fails}`, `stages`, `errors`, `findings`. `emit(report_dict, run_dir_or_none)` → int rc: writes `report.json` atomically, canonical compact JSON on stdout, machine line `HB_ORACLE checks=<n> fails=<n>` on stderr. A filesystem failure turns the report into rc 2 `ERROR` with code `filesystem_error`. Helpers: `atomic_write_json`, `write_receipt`, `collect_run_artifacts`, `make_metric`, `default_metrics`.

## <a id="hb-s2"></a><a id="pm-s2"></a>2. Types, weights and budgets (thresholds)

| id | value | unit | source | status |
|---|---|---|---|---|
| verdict precedence | ERROR > FAIL > INCOMPLETE > PASS | order | `report.py` (reconcile) | CITED (code) |
| rc map | ERROR 2, FAIL 1, INCOMPLETE 3, PASS 0 | rc | `report.py` | CITED (code) |
| stage rc 1 | forbidden for stages | rc | `AGENTS.md` | CITED |
| rc 6 | budget exceeded | rc | `AGENTS.md` | CITED |

Note: report-level FAIL is rc 1; stage rc 1 is forbidden. The two tables are different levels (report verdict vs stage process rc) and must not be merged.

## <a id="hb-s3"></a><a id="pm-s3"></a>3. DAG and commands

Layer 4; imports `bv.contracts` only. Callers: `bv.cli.main` (`run`, `report`, `selftest`). Test command: `PYTHONDONTWRITEBYTECODE=1 timeout 300 python3 -B -m unittest discover -s tests`. A dedicated `tests/test_report*.py` is ABSENT at the subject commit.

## <a id="hb-s4"></a><a id="pm-s4"></a>4. Triptych

**Contracts / claims**
- C-REP-PREC (invariant, CITED): any error or ERROR stage → rc 2; else any failing check or surviving selftest mutant → rc 1; else zero checks, missing checks, CANNOT_SEE or incomplete → rc 3; else rc 0.
- C-REP-QUAL (postcondition, CITED): `qualified` only for scope `render`, rc 0, all expected checks evaluated, no fails, no CANNOT_SEE, no errors, nothing incomplete.
- C-REP-ATOMIC (invariant, CITED): `report.json` is written by temp file plus `os.replace` (`atomic_write_json`); a refusal preserves the caller's bytes.
- C-REP-ONEWAY (invariant, TARGET W15): a judge PASS never clears a deterministic FAIL.
- C-REP-CANON (determinism, TARGET): two runs on identical inputs give identical report bytes (T-BI determinism).

**Anti-uses** (blocked_use): do not set `qualified` from a judge PASS; do not map FAIL to a stage rc 1; do not write `report.json` in place.

**Oracle**: none dedicated today (ABSENT); selftest path covered indirectly by `tests/test_b0_skeleton.py`.

## <a id="hb-s5"></a><a id="pm-s5"></a>5. Subsystems

- Reconcile — anchor `bv.report.reconcile` (`blender_visual_qa/report.py`): merges capture errors, oracle findings, judge results, selftest into one verdict.
- Emit — anchor `bv.report.emit` (same file): atomic file plus stdout/stderr contract.
- Receipt — `write_receipt` (`bvqa.fault-receipt/v1` family) and `collect_run_artifacts` (sha256 of run artifacts).

## <a id="hb-s6"></a><a id="pm-s6"></a>6. Limits and search

| limit | blocked_use |
|---|---|
| `reconcile` is one ~400-line function | do not patch precedence without a mutant per branch |
| no dedicated report tests | do not call precedence verified |
| `qualified` only for scope `render` | do not claim qualification for `selftest` scope |

Router entries: "exit code precedence" → `bv.report.reconcile`; "report.json write" → `bv.report.emit`; "why ERROR beats FAIL" → hb-s7.

## <a id="hb-s7"></a><a id="pm-s7"></a>7. Narrative

Trace: a capture manifest with one error and one FAIL finding → `errors_list` non-empty → rc 2 `ERROR`; the FAIL stays in `findings` but does not set the rc. Decisions (ADR): D1 ERROR dominates so a broken harness is never read as a verdict · D2 INCOMPLETE ranks below FAIL so an observed defect is never masked by a missing check · D3 `emit` is the only writer of `report.json`.

## <a id="hb-s8"></a><a id="pm-s8"></a>8. Symbols (generated: see catalogue/symbols.jsonl)
<!-- hb-generated sha256=7634967a1b843c44e8f2a9b57c3f32a05d48e87c9fe8e9f318b056ac34845612 -->

Anchors `bv.report.reconcile` and `bv.report.emit` have path and line in `catalogue/symbols.jsonl`. Pending anchors (TARGET): `bv.report.write_receipt`.

## <a id="hb-s9"></a><a id="pm-s9"></a>9. Edges
<!-- hb-generated sha256=10f6dc8b51396458b9397b64cc4b671c808d401df0dd680530163f3387bd70cd -->

report → `bv.contracts`. Mathlinks (path#section, no copied text): `v12bs/BSPEC_BINT.md#R-B3` (stage rc), `v12s/SPEC_INT.md#R4` (exit codes, engine registry), `v12s/SPEC_INT.md#R3` (schema registry `vqa.<name>/v<N>`).

## <a id="hb-s10"></a><a id="pm-s10"></a>10. Provenance
<!-- hb-generated sha256=73508a4adc261e441d041b3e7c9abea6e27ccf4ef5e75ac5f645fed381ce4f90 -->

| path | sha256 | lines |
|---|---|---|
| `blender_visual_qa/report.py` | `910731e009606c6088561f85ee12dfd3deb458381c2abd01d679d99cf10970f8` | 603 |
| `blender_visual_qa/contracts.py` | `75976d1d86a3c4d98307750ca8fc584303dffde04db41621e887c11753774784` | 930 |

Receipts: none (AUSENTE). Anchors in code: `bv.report.reconcile`, `bv.report.emit`.
