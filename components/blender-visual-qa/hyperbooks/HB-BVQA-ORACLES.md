# HB-BVQA-ORACLES — scene/pixel oracles, verdict algebra, planted-fault mutants (`blender_visual_qa.oracles`, `bpy_side/{scene_checks,apply_mutant,mutate_ops}`)

Status: active · Subject: blender-visual-qa HEAD 2390e8d · Normative sources (CITED, not copied): `BSPEC_BORACLES` §2 (kernels), §4 (mutants), §5 J3 (one-way judge); `BSPEC_BINT` R-B3/R-B5/R-B7. The kill rule is v1.0 `qualify_mutant` carried unchanged (godot-visual-qa `oracles.py`, read-only). Etags: MEASURED, CITED, TARGET. `oracles/vision.py` belongs to HB-BVQA-VISION.

## <a id="hb-s0"></a>0. Reading map

| Persona | Read | Question → section |
|---|---|---|
| implementer | hb-s1, hb-s5 | What does an oracle row look like? → hb-s1 |
| mutant author | hb-s3, hb-s4, hb-s6 | How is a planted fault applied and killed? → hb-s3 |
| verifier | hb-s4, hb-s9 | Which claims have tests? → hb-s4 |

Prerequisites: HB-BVQA-DRIVE (scratch blend, journal), HB-BVQA-TRUTH (pass arrays for `bp.*`). Conventions: metrics are integers (q16 / ppm / um); thresholds come only from `catalogue/thresholds.tsv`.

## <a id="hb-s1"></a>1. Contracts

Row (`bvqa.oracle-result/v1`): keys `schema check_id oracle_id metrics thresholds verdict reason`; `validate_row` rejects a missing key, a verdict outside `PASS FAIL CANNOT_SEE ADVISORY`, and any non-integer metric (`OracleError`).
Verdict fold: precedence `ERROR > FAIL > CANNOT_SEE/INCOMPLETE > ADVISORY > PASS` (`max_precedence`; unknown string is an error, never PASS); rc map PASS/ADVISORY→0, FAIL→1 internal, ERROR→2, CANNOT_SEE→3 (CLI stage rc per `AGENTS.md`).
One-way judge: `final_verdict(sd, px, judge)` — the judge may add FAIL/INCOMPLETE, never clear an SD/PX FAIL; an unparsed judge verdict becomes CANNOT_SEE (J3, CITED).
Scene side: `scene_checks.py` runs in Blender, read-only (never saves the blend), writes `scene.jsonl` (`bvqa.oracle-sd/v1`); host `oracles/scene.py` folds it (`load`, `evaluate`, `failing_ids`, `write_result`). Pixel side: `oracles/pixel.py` on decoded frames and truth arrays, numpy allowed only there.

## <a id="hb-s2"></a>2. Types, weights and budgets

All `BO_*` rows of `catalogue/thresholds.tsv` (e.g. `BO_KERNEL_TRIS_MAX` 250000 → `CANNOT_SEE kernel_cap`, `BO_ZF_COS_PPM`, `BO_UV_TV_PPM`, `BO_VIS_MIN_PPM`, `BUDGET_TRIS_OBJECT`, `BUDGET_TRIS_SCENE`); a missing required row is rc 2 `E_SCHEMA`. `bp.*` thresholds not yet calibrated are rows with status `TARGET` (W11b). Mutant apply budget ≤ 10 s each (TARGET gate).

## <a id="hb-s3"></a>3. DAG and commands

`apply_mutant.py` (Blender, stage 2) loads a base blend, calls `mutate_ops.OPS[operation]`, compares `witness` before/after (equal → rc 5 `E_GATE mutant_noop`, output not written), saves a copy and a `bvqa.fault-receipt/v1`. Unknown operation/selector → rc 2 `E_SCHEMA`. The manifest `adapters/example_scene/mutant-manifest.json` (`bvqa.mutant-manifest/v1`, ids `bm.<family>.<nn>`) lists faults and expected oracle ids. A mutant is killed when the harness sees a FAIL on an expected oracle id that the clean scene does not produce (`qualify_mutant`, rc 5 on survival).
Commands: `cd components/blender-visual-qa && PYTHONDONTWRITEBYTECODE=1 timeout 300 python3 -m unittest tests.test_host_oracles tests.test_bpy_oracles tests.test_mutants tests.test_mutants_e2e`; Blender variants (`tests/test_bpy_oracles_blender.py`, `tests/test_mutants_blender.py`) need the engine and run only on scratch copies, CPU, pinned cores.

## <a id="hb-s4"></a>4. Triptych

**Claims**
- C-ORA-ALGEBRA (invariant, MEASURED `tests/test_host_oracles.py`): fold precedence and the one-way judge rule; a judge PASS never clears an SD FAIL.
- C-ORA-INT (postcondition, MEASURED): float or bool metric rejected by `validate_row`.
- C-ORA-KERNEL (determinism, MEASURED `tests/test_bpy_oracles.py`): the mesh kernel (`mesh_kernel`, `uv_stats`, `relative_sigma_fails`) is pure stdlib/integer and callable without bpy.
- C-ORA-MAGENTA (postcondition, MEASURED): `magenta_attrib` names exactly the mutated material.
- C-ORA-NOOP (precondition, MEASURED `tests/test_mutants*.py`): equal witnesses refuse with `mutant_noop`.
- C-ORA-KILL (TARGET at full scale): every non-equivalent `bm.*` is killed on 3 seeds; the 30-seed CP lower bound is a W10b/W11b outcome, not asserted here. At commit c1fd87d the repository records 17/20 non-equivalent killed end to end with 3 OPEN (history subject, CITED).
**Anti-uses** (blocked_use): do not edit thresholds inline in code (tsv only, `g_threshold`); do not let CANNOT_SEE certify equivalence; do not import bpy in `oracles/`; do not run Blender mutants on the source blend.
**Oracle**: `tests/test_host_oracles.py`, `tests/test_bpy_oracles.py`, `tests/test_mutants.py`, `tests/test_mutants_e2e.py`.

## <a id="hb-s5"></a>5. Subsystems

- Harness — `blender_visual_qa/oracles/harness.py`; anchors `bv.oracles.max_precedence`, `bv.oracles.final_verdict`, `bv.oracles.qualify_mutant`; also `validate_row`, `rc_of`, `sha256_canonical`.
- Scene fold — `blender_visual_qa/oracles/scene.py`; anchors `bv.oracles.scene.load`, `bv.oracles.scene.evaluate`.
- Pixel — `blender_visual_qa/oracles/pixel.py`; anchors `bv.oracles.pixel.frame_magenta`, `bv.oracles.pixel.magenta_attrib`, `bv.oracles.pixel.subject_visible`; also `frame_black`, `frame_uniform`, `frame_frozen`, `zfight_pixel`, `view_transform_ok`.
- In-Blender checks — `bpy_side/scene_checks.py` (anchors `bpy.scene_checks.mesh_kernel`, `bpy.scene_checks.uv_stats`, `bpy.scene_checks.zfight`; `check_meshes`, `check_materials`, `check_scale`, `check_budget`, `check_frames`).
- Mutants — `bpy_side/apply_mutant.py#entry`, `bpy_side/mutate_ops.py` (anchor `bpy.mutate_ops.witness`; ops `op_flip_normals`, `op_unlink_material`, `op_uv_scale`, `op_scale_object`, `op_camera_clip`, `op_kill_lights`, `op_freeze_anim`, …).

## <a id="hb-s6"></a>6. Limits and search

| limit | blocked_use |
|---|---|
| kernel cap `BO_KERNEL_TRIS_MAX` | do not read `CANNOT_SEE kernel_cap` as PASS |
| `zfight_pixel` is FAIL only on `workbench-det`, ADVISORY otherwise | do not gate Cycles runs on it |
| magenta checks valid only under the identity view transform | do not run `frame_magenta` under Filmic/AgX (`view_transform_ok`) |
| 3 OPEN non-equivalent mutants at c1fd87d | do not claim 20/20 until a receipt says so |
Router entries: "verdict precedence" → `bv.oracles.max_precedence`; "judge override" → `bv.oracles.final_verdict`; "mutant kill rule" → `bv.oracles.qualify_mutant`; "uv stretch kernel" → `bpy.scene_checks.uv_stats`.

## <a id="hb-s7"></a>7. Narrative

Clean scene → `scene_checks` emits rows → `scene.evaluate` folds them with pixel rows into a verdict. Mutant scene → `apply_mutant` plants one fault, proving it changed the witness → the same oracles must FAIL on the expected ids while the clean baseline stays PASS. Decisions: D1 oracle thresholds in one tsv so calibration is auditable; D2 integer q16 kernels so host tests need no Blender; D3 v1.0 kill rule reused unchanged so Godot and Blender results are comparable.

## <a id="hb-s8"></a>8. Symbols

Generated: `catalogue/symbols.jsonl` / TARGET `hyperbooks/anchors.jsonl`. No hand-kept line numbers.

## <a id="hb-s9"></a>9. Tests and gates

`tests/test_host_oracles.py`, `tests/test_bpy_oracles.py`, `tests/test_bpy_oracles_blender.py`, `tests/test_mutants.py`, `tests/test_mutants_e2e.py`, `tests/test_mutants_blender.py`; gates `g_threshold`, `g_bpy_boundary`, `g_numpy_scope`, `g_pure` via `python3 tools/gates.py --all`.

## <a id="hb-s10"></a>10. Provenance

Back-reference (code→book): modules `bv.oracles` (`harness`, `scene`, `pixel`), `bpy.scene_checks`, `bpy.apply_mutant`, `bpy.mutate_ops` name this book. The `# @module {..., "book":"HB-BVQA-ORACLES"}` header field is TARGET: code files are outside this lane; required edits are listed in `v12e/room_W13/REPORT_S2.md`.

Source sha256 table (MEASURED at write time, HEAD 2390e8d + working tree; re-seal when sources change):

| file | sha256 |
|---|---|
| `blender_visual_qa/oracles/harness.py` | ce53e0c672bda0566e7c588ae0e1599da069ab5c08555fc481a63da40d5043cf |
| `blender_visual_qa/oracles/scene.py` | 9bc3a7650b0cb30a4adfd5f3d714808700d68ba7b838a280f93d4c244331b941 |
| `blender_visual_qa/oracles/pixel.py` | 881fd1d739374a48e252297aef8af0ccea251fa868000d846fde86d10edf1d53 |
| `bpy_side/scene_checks.py` | fea4457d95714e1b1ffcde99b464965e4b23cc88f9f94262f2ed480daa7f9488 |
| `bpy_side/apply_mutant.py` | 2775ae6f3a34e90f619ffcf417ed6d106c04ba3d5c7cf473ad4caae25e7de40d |
| `bpy_side/mutate_ops.py` | c2065267c9be4172ecdcda86806e82264093058f18939cd71a4478734595803a |
| `adapters/example_scene/mutant-manifest.json` | 212609914d7c7112a07092aa4cc66dfb1d3c2aea20e584e8adc95b53c14ce5e3 |
