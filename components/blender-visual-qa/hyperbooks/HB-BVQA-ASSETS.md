# HB-BVQA-ASSETS — Blender asset-script layer (`blender_visual_qa.assets`)

Status: draft · Waves AG1 (INGEST + TURNTABLE), AG2 (RENDER/LOD/HULL/ORM), AG3 (HEAL + MATERIAL) · Normative sources: `campaigns/visual-qa-0/v12g3/CARDS_AG.md` §2-§3, `v12ar/ASSETGEN_ROUTER_DESIGN.md`. Etags: CITED (code), TARGET (card). No receipts: no MEASURED claim.

## <a id="hb-s0"></a><a id="pm-s0"></a>0. Reading map

| Persona | Read | Question → section |
|---|---|---|
| implementer | hb-s1, hb-s5 | How is a stage invoked and chained? → hb-s1 |
| maintainer | hb-s2, hb-s6 | Where do asset limits live? → hb-s2 |
| verifier | hb-s4 | What binds one receipt to the next? → hb-s4 |

Prerequisites: HB-BVQA-REPO (gates), HB-BVQA-VISION (turntable vision stage). Host code is stdlib only; Blender work is a `blender -b --factory-startup --python` subprocess.

## <a id="hb-s1"></a><a id="pm-s1"></a>1. Contracts

`blender-visual-qa asset inspect|normalize|mesh-gate|turntable [--chain FILE] <stage flags>` → `bv.assets.cli.main(argv)` → stage module `main(argv) -> rc` (`inspect_gate`, `normalize`, `mesh_gate`, `turntable`, `render_sheet`, `lod_ladder`, `hull_gen`, `ortho4`, `orm_pack`, `heal`, `self_isect`, `bake_pbr`, `uv_gate`, `pbr_validate`, `delight`). The last JSON line the stage prints becomes `stage_receipt` inside a `bvqa.asset-receipt/v1` envelope: `{schema, stage, rc, input_sha256, prev_receipt_sha256, receipt_sha256, stage_receipt}`. A stage module that is not built yields rc 3 `stage_not_built`.

## <a id="hb-s2"></a><a id="pm-s2"></a>2. Types, weights and budgets (thresholds)

| id | value | unit | source | status |
|---|---|---|---|---|
| `ASSET_PIVOT_TOL_UM` | 10 | um | `limits.py`, `catalogue/thresholds.src` | TARGET (X3:uncalibrated) |
| `ASSET_TURN_FRAMES` | 72 | frames | same | TARGET (X3:uncalibrated) |
| `ASSET_TURN_STEP_MDEG` | 5000 | mdeg | same | TARGET (X3:uncalibrated) |
| `ASSET_TURN_RES` | 512 | px | same | TARGET (X3:uncalibrated) |
| `ASSET_TURN_SPP` | 64 | samples | same | TARGET (X3:uncalibrated) |
| `ASSET_ORM_ROUGH_MIN_BYTE` | 11 | byte | same (VG-0240) | TARGET (X3:uncalibrated) |
| `ASSET_LOD1_IOU_MIN_PPM` | 950000 | ppm | same | TARGET (X3:uncalibrated) |
| `ASSET_LOD2_IOU_MIN_PPM` | 900000 | ppm | same | TARGET (X3:uncalibrated) |
| `ASSET_HEAL_MAX_PASSES` | 4 | passes | same (VG-0238) | TARGET (X3:uncalibrated) |
| `ASSET_HEAL_IOU_MIN_PPM` | 990000 | ppm | same | TARGET (X3:uncalibrated) |
| `ASSET_UV_STRETCH_MAX_PERMILLE` | 4000 | permille | same (VG-0234) | TARGET (X3:uncalibrated) |
| `ASSET_PBR_F0_MIN_BYTE` | 170 | byte | same (VG-0037) | TARGET (X3:uncalibrated) |
| `ASSET_DELIGHT_FLOOR_Q16` | 4096 | q16 | same (VG-0252) | TARGET (X3:uncalibrated) |
| stage rc | 0, 2, 3, 4, 5, 6 | rc | `limits.RC_*` | CITED (code) |

Stage rc 1 is forbidden; any other rc (including a domain rc 1) maps to 5. New limits are requested on the bus as `REG-REQ: NAME value unit source` and added only here.

## <a id="hb-s3"></a><a id="pm-s3"></a>3. DAG and commands

Layer 4; `bv.cli.main` dispatches `asset` lazily to `bv.assets.cli.main`. Test command: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_ag1_e2e`. Blender legs need `BVQA_BLENDER=1`, the 72-frame turntable also `BVQA_SLOW=1`.

## <a id="hb-s4"></a><a id="pm-s4"></a>4. Triptych

**Contracts / claims**
- C-AST-RC (invariant, CITED): the envelope `rc` is always in {0,2,3,4,5,6}; a domain rc 1 becomes 5.
- C-AST-CHAIN (invariant, CITED): `receipt_sha256` is sha256 of the canonical envelope without that key; with `--chain FILE`, `prev_receipt_sha256` is the previous line's `receipt_sha256`.
- C-AST-ONEWAY (invariant, TARGET): vision may only downgrade a run, never clear a deterministic failure.

**Anti-uses** (blocked_use): do not compare against bare integers in asset code (g_threshold); do not call Blender from host modules.

**Oracle**: `tests/test_ag1_e2e.py` (chain, rc map, registry); full chain needs `BVQA_BLENDER=1`.

## <a id="hb-s5"></a><a id="pm-s5"></a>5. Subsystems

- Dispatcher — anchor `bv.assets.cli.main` (`blender_visual_qa/assets/cli.py`): stage lookup, rc normalisation, envelope, chain file.
- Limits — `blender_visual_qa/assets/limits.py`: named constants mirrored in `catalogue/thresholds.src`.
- Stages (peer lanes): `inspect_gate`, `normalize`, `mesh_gate`, `turntable`, `render_sheet`, `lod_ladder`, `hull_gen`, `ortho4`, `orm_pack`, `heal`, `self_isect`, `bake_pbr`, `uv_gate`, `pbr_validate`, `delight`.
- Vision calibration — `config/vision_asset_calib.json`: LOD IoU floors per fixture; `vision_mode` stays `shadow` until status is MEASURED on 3 fixtures (oracle `tests/test_ag2_e2e.py`).

## <a id="hb-s6"></a><a id="pm-s6"></a>6. Limits and search

| limit | blocked_use |
|---|---|
| asset thresholds are X3:uncalibrated | do not call a verdict calibrated before AG2 |
| dispatcher trusts the last JSON line of stdout | do not print receipts mid-stream from a stage |

Router entries: "asset command" → `bv.assets.cli.main`; "asset limits" → hb-s2.

## <a id="hb-s7"></a><a id="pm-s7"></a>7. Narrative

Trace: raw crate → `inspect` rc 5 (pivot) → `normalize` → `inspect` rc 0 → `mesh-gate` rc 0 → `turntable --with-truth --vision shadow` rc 0; each envelope carries the previous `receipt_sha256`. A non-manifold raw stops at `mesh-gate` rc 5 and no `render/` directory exists.

Wave AG1 lands `inspect`, `normalize`, `mesh-gate`, `turntable` behind the dispatcher above; e2e on a programmatic raw crate (offset pivot, shared instance, 1.7× scale).

Wave AG2 adds `render-sheet`, `lod-ladder`, `hull-gen`, `ortho4`, `orm-pack` to the same dispatcher; a stage whose module is absent returns rc 3 `stage_not_built`. Oracle: `tests/test_ag2_e2e.py` (registry, limits, calibration record, ladder→hull→ORM chain with `BVQA_BLENDER=1 BVQA_SLOW=1`).

Wave AG3 adds `heal`, `self-isect`, `bake-pbr`, `uv-gate`, `pbr-validate`, `delight` to the same dispatcher (same rc 3 `stage_not_built` rule). Oracle: `tests/test_ag3_e2e.py` (registry, limits, schema enum, noisy fixture with hole + duplicate face + sliver fan: `mesh-gate` rc 5 → `heal` → `mesh-gate` rc 0, sha-chained). Every `ASSET_*` constant in `limits.py` has a `BASSETS` DESIGN_CHOICE row in `catalogue/thresholds.src`.

## <a id="hb-s8"></a><a id="pm-s8"></a>8. Symbols (generated: see catalogue/symbols.jsonl)
<!-- hb-generated sha256=724daa58d6b4525c5553dd939e039075d170f6b3b17c6d9f41f08b2657b90624 -->

Anchor `bv.assets.cli.main` has path and line in `catalogue/symbols.jsonl`.

## <a id="hb-s9"></a><a id="pm-s9"></a>9. Edges
<!-- hb-generated sha256=4066aaf6aee08e34d8076af92142603ebb6b0a1eb3ec3e3fde042c7901b00b68 -->

assets.cli → `blender_visual_qa/assets/limits.py`, stage modules (lazy). Mathlinks (path#section): `v12g3/CARDS_AG.md#2`, `v12ar/ASSETGEN_ROUTER_DESIGN.md#0.3`.

## <a id="hb-s10"></a><a id="pm-s10"></a>10. Provenance
<!-- hb-generated sha256=7ddc8a368288167c9e8ed03b7c71bd6b38544afc276a15d62cac024589abcce9 -->

Files: `blender_visual_qa/assets/cli.py`, `blender_visual_qa/assets/limits.py`, `blender_visual_qa/schemas/asset-receipt.v1.schema.json`. Receipts: none (AUSENTE).


## <a id="hb-s11"></a><a id="pm-s11"></a>11. Intent: inspect a glb asset for pivot, scale and topology problems

Run `inspect_gate.run` on a GLB; `inspect_scene` evaluates the integer checks and `glb_core.parse_glb` reads the file. Nodes: `bv.assets.inspect_gate.run`, `bv.assets.inspect_gate.inspect_scene`, `bv.assets.glb_core.parse_glb`. Etag: CITED (code).

## <a id="hb-s12"></a><a id="pm-s12"></a>12. Intent: normalize an asset by baking transforms, grounding the pivot and fixing the basis

`normalize_scene` bakes the node TRS into world space, grounds the pivot and applies the scale; `run_bytes` returns the output bytes and receipt. Nodes: `bv.assets.normalize.normalize_scene`, `bv.assets.normalize.run_bytes`, `bv.assets.normalize.ground`. Etag: CITED (code).

## <a id="hb-s13"></a><a id="pm-s13"></a>13. Intent: mesh gate for manifold edges, genus and uv overlap of a mesh

`gate_scene` runs `gate_prim` over every primitive and returns the failed check ids. Nodes: `bv.assets.mesh_gate.gate_scene`, `bv.assets.mesh_gate.gate_prim`. Etag: CITED (code).

## <a id="hb-s14"></a><a id="pm-s14"></a>14. Intent: render a 72 frame turntable of an asset with truth passes

The turntable stage drives the Blender orbit worker, one subprocess per run. Nodes: `bv.assets.turntable.main`, `bpy.assets.orbit_worker.main`. Etag: CITED (code).

## <a id="hb-s15"></a><a id="pm-s15"></a>15. Intent: asset command line dispatcher to run an asset stage

`blender-visual-qa asset <stage>` dispatches to the stage `main`. Nodes: `bv.assets.cli.main`. Etag: CITED (code).
