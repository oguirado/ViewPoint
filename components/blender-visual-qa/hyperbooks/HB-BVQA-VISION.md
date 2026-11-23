# HB-BVQA-VISION — engine vision stage, shadow/on (`vision_stage`, `oracles.vision`)

Status: active · Subject commit `2390e8d` · Engine pin `6b6679a925ff69249faf508aa510e25b2dd19500` (v1.1.0, wheel sha256 `73cd0cdd96544b6a06c9e8c3404761701bcaf71d42b50e510e90b2e5a7a52991`, `config/vision.json`). Normative sources are engine specs reached by mathlink only (hb-s9); this book copies none of their text. Etags: CITED, TARGET. No receipts: no MEASURED claim.

## <a id="hb-s0"></a><a id="pm-s0"></a>0. Reading map

| Persona | Read | Question → section |
|---|---|---|
| implementer | hb-s1, hb-s5 | How is the engine invoked? → hb-s1 |
| maintainer | hb-s4, hb-s6 | Why never patch visioncore? → hb-s7 |
| verifier | hb-s4 | What stops shadow from changing a verdict? → hb-s4 |

Prerequisites: HB-BVQA-TRUTH (profile thresholds), HB-BVQA-ORACLES (harness verdict constants). Engine algorithms stay in `vision-engine`; AGENTS.md forbids copying them.

## <a id="hb-s1"></a><a id="pm-s1"></a>1. Contracts

`vision_stage.run_stage(run_dir, vision='off'|'shadow'|'on', config=None, expected_commit=None, timeout=120)` → dict `{mode, advisory, status, engine_commit, wheel_sha256, proposals[]}`; `off` → `{'mode':'off','status':'SKIPPED'}` with no engine import. `pinned_wheel(config, expected_commit)` checks commit, wheel sha256 and that the wheel holds `vadapt/blender/from_render_dir.py`; failure → `StageError('E_PIN: ...')`. The worker runs in a fresh interpreter (`python -B`, `PYTHONPATH` removed, new session, process group killed on timeout) and adds only the wheel to `sys.path`; it checks that `from_render_dir` and `pipeline` load from the wheel.
Oracles: `oracles/vision.py` reads `<run>/vision/vision.json` (`vqa.vision/v1`, sidecar digests verified) and offers `vc_flicker`, `vc_frozen_subregion`, `vc_slow_drift` through `PREDICATES`; missing/tampered input → CANNOT_SEE `vision_json_missing`.

## <a id="hb-s2"></a><a id="pm-s2"></a>2. Types, weights and budgets (thresholds)

| id | value | unit | source | status |
|---|---|---|---|---|
| stage `timeout` | 120 | s | `vision_stage.run_stage` | CITED (code) |
| modes | `off`, `shadow`, `on` | enum | `vision_stage`; `oracles.vision.MODES` = shadow/on | CITED (code) |
| `min_tiles` default | 1 | tiles | `oracles/vision.py` | CITED (code) |
| `min_frames` defaults | placeholders until profile | frames | `oracles/vision.py` | TARGET (SEAL-2 profile supplies) |
| engine pin | 6b6679a / 1.1.0 | commit | `config/vision.json` | CITED |

## <a id="hb-s3"></a><a id="pm-s3"></a>3. DAG and commands

`vision_stage` (host, numpy allowed per AGENTS.md) → pinned wheel (`vadapt.blender.from_render_dir.adapt`, `visioncore.pipeline.analyze`, `visioncore.params.effective`) → `<run>/vision/`. `oracles.vision` is stdlib only. Commands: `python3 -m blender_visual_qa.vision_stage --run <dir> --vision shadow`; tests `PYTHONDONTWRITEBYTECODE=1 timeout 300 python3 -B -m unittest tests.test_vision_stage -v` (4 tests: pin/adapter fail-closed, off needs no engine, shadow sidecars and tamper, bool/invalid profile cannot trigger).

## <a id="hb-s4"></a><a id="pm-s4"></a>4. Triptych

**Contracts / claims**
- C-VIS-PIN (precondition, CITED): a wheel with wrong sha256 or a different commit is refused with `E_PIN` before any import.
- C-VIS-SHADOW (invariant, CITED): in `shadow` a FAIL surfaces as `ADVISORY` (`effective_verdict`) and never changes the v1.0 verdict.
- C-VIS-OFF (postcondition, CITED): `off` skips without loading the engine.
- C-VIS-NOPATCH (invariant, CITED): the stage never edits visioncore; engine fixes go to `proposals[]` (BINT Q4).
- C-VIS-DEFAULT (rule, TARGET): `--vision on` is not the default (H3, wave 16b).

**Anti-uses** (blocked_use): do not put an engine checkout on `PYTHONPATH`; do not copy engine algorithms here; do not count a shadow FAIL as a gate failure; do not use placeholder `min_frames` as calibrated thresholds.

**Oracle**: `tests/test_vision_stage.py` (4 tests). No mutant run receipt exists.

## <a id="hb-s5"></a><a id="pm-s5"></a>5. Subsystems

- Pin check — `pinned_wheel` (`blender_visual_qa/vision_stage.py`).
- Stage runner — `run_stage`, `_worker`, `main` (`vision_stage.py`); result keys `proposals` always `[]` today.
- Predicates — `vc_flicker`, `vc_frozen_subregion`, `vc_slow_drift`, helper `_cls` (`blender_visual_qa/oracles/vision.py`).
- Verdict mapping — `effective_verdict`, `load_vision`, `evaluate_vision`.

## <a id="hb-s6"></a><a id="pm-s6"></a>6. Limits and search

| limit | blocked_use |
|---|---|
| thresholds are placeholders until the SEAL-2 `vqa.profile/v1` | do not cite them as calibrated |
| B4 temporal rates were measured at 8 frames only (spec needs 32/60) | do not claim temporal recall |
| module headers carry no `@anchor` ids yet | do not route by anchor id; use `path#Symbol` |

Router entries: "pinned wheel check" → `vision_stage.py#pinned_wheel`; "shadow advisory" → `oracles/vision.py#effective_verdict`; "why never patch engine" → hb-s7.

## <a id="hb-s7"></a><a id="pm-s7"></a>7. Narrative

Trace: `run_stage(run, vision='shadow')` → verify wheel → spawn isolated worker → adapt `render/` to `vision_in/` → analyze to `vision/` → host oracles read `vision.json`. Decisions (ADR): D1 pinned hash-checked wheel, so results are reproducible and the engine is read-only · D2 fresh interpreter, so a caller's imported `visioncore` cannot shadow the pin · D3 shadow by default until calibration, so uncalibrated predicates cannot fail a run.

## <a id="hb-s8"></a><a id="pm-s8"></a>8. Symbols (generated: see catalogue/symbols.jsonl)
<!-- hb-generated sha256=5eb61d8f242cc67026c24724b3ac9a65c467655c1d885529e0b6058bc90d1129 -->

No `@anchor` tags exist in `vision_stage.py` or `oracles/vision.py` at the subject commit, so `symbols.jsonl` has no row for them. Pending anchors (TARGET, code edit outside this lane): `bv.vision.run_stage`, `bv.vision.pinned_wheel`, `bv.oraclesvision.effective_verdict`.

## <a id="hb-s9"></a><a id="pm-s9"></a>9. Edges
<!-- hb-generated sha256=8fd15d612af3f57b2026c02d180638b95d6303e14d87c947483ef8692726b914 -->

vision_stage → pinned wheel only. Mathlinks into `vision-engine` (path#section, no copied text): `v12s/SPEC_INT.md#R1` (grid/id grammar), `#R3` (schema registry), `#R4` (exit codes), `v12s/SPEC_ARCH.md#5` (adapters `vadapt`), `v12s/SPEC_ARCH.md#4.1` (`vqa.frame/v1`), `v12s/SPEC_TIME.md#2` (S5 temporal codec), `vision-engine:vadapt/blender/from_render_dir.py` (module `va.blender`).

## <a id="hb-s10"></a><a id="pm-s10"></a>10. Provenance
<!-- hb-generated sha256=29204f37fa66d5c0d0767fb81f2f06d6d336275562ccc77bb088fc77bd70f4db -->

| path | sha256 | lines |
|---|---|---|
| `blender_visual_qa/vision_stage.py` | `5234c733638ac80ba2d57bda3312b12de3a593f08519343676e95e61c2b07b9c` | 108 |
| `blender_visual_qa/oracles/vision.py` | `581556c409d0f4a0466f2551df0f3e348c98e7b902fc067ab7df33515b3eacbd` | 103 |
| `config/vision.json` | `e120287e57b20e0c86e072bfa5a564242571660d35dc6ddc63a4f98580b1361e` | 1 |
| `tests/test_vision_stage.py` | `11ac9bd253790357b7a1452e40b45c1c6bb6e2fdaecbffa9f58dd9faf1ec6081` | 50 |

Receipts: none (AUSENTE). Anchors in code: none (see hb-s8).
