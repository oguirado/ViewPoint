# HB-BVQA-JUDGE — multimodal judge stage (`blender_visual_qa.judge`)

Status: active · Subject commit `2390e8d` · Normative sources: `v12bs/BSPEC_BORACLES.md` §5 (checklist, judge), `v12bs/BSPEC_BINT.md` R-B3/R-B5 (stage rc, verdict algebra). Etags: CITED (spec or code read at the subject commit), TARGET (not implemented/measured). No receipts exist for this book: it makes no MEASURED claim (see hb-s10).

## <a id="hb-s0"></a><a id="pm-s0"></a>0. Reading map

| Persona | Read | Question → section |
|---|---|---|
| implementer | hb-s1, hb-s2, hb-s5 | What does `judge_run` take and return? → hb-s1. Which caps? → hb-s2 |
| maintainer | hb-s2, hb-s6, hb-s7 | Why can a judge PASS not clear an oracle FAIL? → hb-s7 |
| verifier | hb-s4, hb-s6 | Which claims are tested today? → hb-s4 (none MEASURED) |

Prerequisites: HB-BVQA-REPORT (verdict algebra consumes judge results), HB-BVQA-ORACLES (the deterministic findings the judge never overrides). Conventions: the judge sees media and a rubric only; telemetry, oracle verdicts, mutant labels and canary expectations are withheld.

## <a id="hb-s1"></a><a id="pm-s1"></a>1. Contracts

Entry points (`blender_visual_qa/judge/__init__.py`): `build_batches(capture, checklist, run_dir)` → list of batches; `validate_response(input_obj, output_obj, expectations)` → normalised verdicts; `judge_run(...)` → stage result. Schemas: `vqa.judge-stage/v1`, `vqa.judge-input/v1`, `vqa.judge-output/v1`, `vqa.judge-expectations/v1`, `vqa.judge-policy/v1`; JSON schemas in `blender_visual_qa/schemas/{judge-input,judge-output,judge-policy,checklist}.v1.schema.json`. Prompt: `blender_visual_qa/judge/judge_prompt_template_v1.txt` (echoes `run_id`, `batch_id`, `attempt_id`).
Verdict per check: PASS, FAIL or CANNOT_SEE. A PASS/FAIL below `CONFIDENCE_MIN` becomes CANNOT_SEE. Errors: `JudgeValidationError`, `JudgeTransportError`, `JudgeBudgetError` (subclasses of `JudgeError`); stage rc follows the table in `AGENTS.md` (rc 1 forbidden for stages).
Canaries: `render_canary_frame` / `derive_canary_token` (alphabet `CANARY_ALPHABET`) embed a seeded token in a synthetic frame; the expected token never reaches the worker.

## <a id="hb-s2"></a><a id="pm-s2"></a>2. Types, weights and budgets (thresholds)

| id | value | unit | source | status |
|---|---|---|---|---|
| `DEFAULT_MODEL_ID` | `gemini-3.8-flash` | model | `judge/__init__.py` | CITED (code) |
| `CONFIDENCE_MIN` | 0.85 | prob | D-S3-6 | CITED (code) |
| `MAX_CALLS` / `MAX_RETRY_CALLS` / `MAX_RETRIES_PER_BATCH` | 6 / 2 / 1 | calls | code | CITED (code) |
| `CALL_TIMEOUT_S` / `WALL_BUDGET_S` | 480 / 2400 | s | code | CITED (code) |
| `INPUT_BUDGET_TOKENS` / `INPUT_PER_CALL_TOKENS` | 1500000 / 300000 | tokens | code | CITED (code) |
| `MAX_GAMEPLAY_MEDIA_PER_BATCH` | 3 | files | code | CITED (code) |
| per-call / per-run token cap W15 | 14.4 k / 58 k | tokens | `CARDS_WBLD.md` W15 | TARGET |

DEVIATION (declared, not fixed here): the module docstring says 180k total, 35k/call, 180 s call, 900 s wall (D-S3-9) while the constants above are larger; the constants are what runs. The W15 card tightens them; reconcile in W15 S1.

## <a id="hb-s3"></a><a id="pm-s3"></a>3. DAG and commands

Layer 4; consumer of `bv.contracts`; producer for `bv.report.reconcile`. Command: `blender-visual-qa judge ...` (dispatch in `bv.cli.main`). Host test commands: `PYTHONDONTWRITEBYTECODE=1 timeout 300 python3 -B -m unittest discover -s tests`. Test double: `tools/fake_judge_worker.py`. Worker transport: `scr-obrero-gemini` (path in `DEFAULT_WORKER_PATH`), direct argv, never a shell string.

## <a id="hb-s4"></a><a id="pm-s4"></a>4. Triptych

**Contracts / claims**
- C-JUDGE-BLIND (invariant, CITED): the prompt and inputs carry no oracle verdicts, telemetry or expected canary token (D-S3-3).
- C-JUDGE-CONF (postcondition, CITED): PASS/FAIL accepted only at confidence ≥ `CONFIDENCE_MIN`, else CANNOT_SEE (`bv.judge.validate`).
- C-JUDGE-ONEWAY (invariant, TARGET W15): a judge PASS never clears a deterministic FAIL (T-J-ONEWAY); enforced in `bv.report.reconcile`.
- C-JUDGE-BUDGET (bound, CITED): at most `MAX_CALLS` worker calls and `WALL_BUDGET_S` wall; excess → `JudgeBudgetError`, rc 6.
- C-JUDGE-ESC (rule, TARGET W15): Flash first, Argon after ≥ 3 consecutive CANNOT_SEE or canary fails, counter reset on pass.

**Anti-uses** (blocked_use): do not feed oracle results or `bm.*` labels to the judge; do not treat judge PASS as qualification (qualification needs deterministic PASS, see HB-BVQA-REPORT); do not use `--vision on` as default (H3); do not call the worker through a shell string.

**Oracle**: none today. `tests/test_judge*.py` is ABSENT at the subject commit (W15 S1 owns it); the only judge-adjacent double is `tools/fake_judge_worker.py`.

## <a id="hb-s5"></a><a id="pm-s5"></a>5. Subsystems

- Validation — anchor `bv.judge.validate` (`blender_visual_qa/judge/__init__.py`): schema, identity echo, confidence rule, expectation match.
- Run — anchor `bv.judge.run` (same file): batches, attempts, retry within caps, usage accounting, atomic outputs.
- PNG helpers — anchor `bv.judge.png`: canonical RGB PNG encoder used by canaries and attention images.
- Batching — `build_batches`, `_build_attempt_for_batch`: media selection (PNG for HUD/cards, MP4 for motion), companion attention PNG/MP4.
- Media gate — `validate_media_pre_dispatch`, `validate_mp4_duration`, `probe_mp4_duration`.

## <a id="hb-s6"></a><a id="pm-s6"></a>6. Limits and search

| limit | blocked_use |
|---|---|
| judge sees pixels only; no scene truth | do not use it for geometry, ids or counts (use `bo.*`) |
| checklist `bvqa.checklist/v1` not yet authored | do not claim checklist coverage before W15 |
| no `test_judge*.py` | do not call the module verified |
| cost caps in code differ from docstring | do not quote docstring caps |

Router entries: "judge confidence rule" → `bv.judge.validate`; "judge budget caps" → hb-s2; "why judge cannot clear fail" → hb-s7.

## <a id="hb-s7"></a><a id="pm-s7"></a>7. Narrative

Flow: capture manifest + checklist → `build_batches` → per-batch prompt from the template → worker call → `validate_response` → accepted verdicts or one retry on an abstaining batch with alternative evidence → results to reconcile.
Decisions (ADR): D1 finite Python judge, no service · D2 pixels and rubric only, to keep the judge independent of the oracles · D3 low confidence is CANNOT_SEE, not FAIL, so uncertainty never produces a false defect · D4 an accepted FAIL is never erased by a retry · D5 dual reconciliation: deterministic FAIL dominates (judge is advisory upward only).

## <a id="hb-s8"></a><a id="pm-s8"></a>8. Symbols (generated: see catalogue/symbols.jsonl)
<!-- hb-generated sha256=b3b7e0805b53c0fa5f9668b5eb4165f49702d83741d33996ef49744835b87fca -->

Rows are not repeated here; `catalogue/symbols.jsonl` holds path and line for `bv.judge.png`, `bv.judge.validate`, `bv.judge.run`. Pending anchors (TARGET, to add in code): `bv.judge.build_batches`, `bv.judge.attention`.

## <a id="hb-s9"></a><a id="pm-s9"></a>9. Edges
<!-- hb-generated sha256=88f3c6d5b5d786bfe6fdb8ede2b2d6f4fb1dac43f4f2e27a9f5feb8fed23db93 -->

judge → `bv.contracts`; judge → worker transport; report.reconcile ← judge results. Mathlinks (path#section, no copied text): `v12s/SPEC_TIME.md#4` (judge output v2, additive to `vqa.judge-output/v1`), `v12s/SPEC_TIME.md#6` (token budget per judge call), `v12bs/BSPEC_BORACLES.md#5` (checklist), `v12bs/BSPEC_BINT.md#R-B3` (stage rc).

## <a id="hb-s10"></a><a id="pm-s10"></a>10. Provenance
<!-- hb-generated sha256=a9b6af652e9260a45458d40c663c083ac4e25e62b45997247175860b3e6ff8af -->

| path | sha256 | lines |
|---|---|---|
| `blender_visual_qa/judge/__init__.py` | `0d52be758282b4d0d1028e41a8c2718e927b0887baa5e24a63109de48f14130f` | 2231 |
| `blender_visual_qa/judge/judge_prompt_template_v1.txt` | `61b14d269f30333b1565f7511c235199e56347edb99b2cdc16189e0031e796b8` | 43 |

Receipts: none (AUSENTE). Anchors in code: `bv.judge.png`, `bv.judge.validate`, `bv.judge.run`.
