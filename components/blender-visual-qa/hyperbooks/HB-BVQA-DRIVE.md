# HB-BVQA-DRIVE — deterministic staged Blender capture (`blender_visual_qa.drive`, `bpy_side/drive_*`)

Status: active · Subject: blender-visual-qa HEAD 2390e8d · Normative sources (CITED, not copied): `BSPEC_BDRIVE`, `BSPEC_BINT` §2 R-B1/R-B3/R-B9 (campaign `v12bs/`), vision-engine `SPEC_INT` R4 (error rc). Etags: MEASURED (test/receipt), CITED (spec), TARGET (not yet implemented). Books are host-written prose; line numbers live in `catalogue/symbols.jsonl`, never here.

## <a id="hb-s0"></a>0. Reading map

| Persona | Read | Question → section |
|---|---|---|
| implementer | hb-s1, hb-s5 | What does a drive run emit? → hb-s1. Which module owns launch/finish? → hb-s5 |
| maintainer | hb-s2, hb-s6 | Which budgets, why no float? → hb-s2, hb-s7 |
| verifier | hb-s4, hb-s9 | Which claims have tests? → hb-s4 |

Prerequisites: `AGENTS.md` (stage rc table, boundary rule), HB-BVQA-TRUTH for `cycles_seed` consumers. Conventions: integer-only canonical JSON, staged writes then `os.replace`, host never imports `bpy`.

## <a id="hb-s1"></a>1. Contracts

Inputs: a `.blend` (suffix checked), a `bvqa.journal/v1` integer journal, a render profile (`cycles-cpu-det` default, `cycles-cpu-look`, `workbench-det` under `blender_visual_qa/drive/profiles/`). Outputs: a run directory with canonicalised RGB8 PNGs, `bvqa.capture-receipt/v1` and adapter input (`finish`), optional judge-only MP4 (`encode`).
Errors: `DriveError(code, reason)` with codes `E_USAGE E_DRIVE E_PROFILE E_DIR E_SCHEMA E_BUDGET E_IO E_INCOMPLETE E_GATE`; stage rc follows the AGENTS.md table, rc 1 is forbidden (CITED R-B3).
Determinism: pure function of (source sha, journal, profile, threads); the env is declared (`declared_env`, `env_sha256`), seeds come from `cycles_seed(bench_no, k, ri)` (R-B9, single definition in `bpy_side/canon.py`, mirrored on host by `bv.journal.cycles_seed`).
Source immutability: the `.blend` sha and mtime are re-read after the run; a change aborts the run (`launch.drive`).

## <a id="hb-s2"></a>2. Types, weights and budgets

Thresholds are rows of `catalogue/thresholds.tsv` (append-only; `g_threshold`). Drive-relevant: `FRAME_CAP` 600, `MP4_MIB` 20, `ARTIFACT_MIB` 512 (all DESIGN_CHOICE until calibrated; status in the tsv). Timeouts: `--timeout-s` bounded, polled at 0.25 s granularity; no unbounded process (CITED rule in `AGENTS.md`).

## <a id="hb-s3"></a>3. DAG and commands

Host: `launch` → (Blender subprocess runs `bpy_side/drive_scene.py`, `drive_manifest.py`, helpers in `drive_common.py`) → `finish` → `encode`. `png.py` decodes strictly (colour type 2 only). `bpy_side` may import only `bpy_side.q` and `bpy_side.canon` across the boundary (`g_bpy_boundary`).
Commands (scratch only, CPU only):
`cd components/blender-visual-qa && PYTHONDONTWRITEBYTECODE=1 timeout 300 python3 -m unittest tests.test_drive tests.test_drive_mutants tests.test_seed_twins`
`python3 -m blender_visual_qa.drive.launch --help` (flags: `--profile --threads --timeout-s --still --keep-raw --strict --mutant`).

## <a id="hb-s4"></a>4. Triptych

**Claims**
- C-DRV-OWNED (invariant, MEASURED by `tests/test_drive.py`): every Blender child runs in an owned process group with argv admission (`validate_render_argv`) and is reaped on timeout.
- C-DRV-STAGED (postcondition, MEASURED): a refused run leaves caller output bytes untouched; publication is atomic.
- C-DRV-SEED (invariant, MEASURED by `tests/test_seed_twins.py`): host and Blender seed functions agree for the golden `(bench_no,k,ri)` grid.
- C-DRV-PNG (precondition, MEASURED): non-RGB8 or interlaced PNG is refused (`png.decode`).
- C-DRV-DET (determinism, MEASURED when Blender is available, `tests/test_drive_blender.py`): same inputs → same frame hashes on cycles-cpu-det; GPU is never used (TARGET: GB10 sm_121 unsupported, CITED CDESIGN_CBLD).
**Anti-uses** (blocked_use): do not call Blender without `launch`; do not read the live source blend after the run for identity; do not add float fields to the journal (`journal.nofloat`); do not treat the MP4 as evidence (PNG is).
**Oracle**: `tests/test_drive*.py`, `tests/test_seed_twins.py`; mutants `tests/test_drive_mutants.py`.

## <a id="hb-s5"></a>5. Subsystems

- Journal — `blender_visual_qa/drive/journal.py` (`validate`, `frame_list`, `canonical`, `sha`, `file_sha`, `cycles_seed`); anchor `bv.journal.cycles_seed`.
- Launch — `blender_visual_qa/drive/launch.py`; anchors `bv.launch.validate_render_argv`, `bv.launch.owned_process`; entry `drive`, `check` (golden compare), `main`.
- Finish — `blender_visual_qa/drive/finish.py`; anchor `bv.finish.finish` (`canonicalise`, adapter input, receipt).
- Encode — `blender_visual_qa/drive/encode.py#encode` (judge-only MP4).
- PNG — `blender_visual_qa/drive/png.py#decode`, `#encode`.
- In-Blender side — `bpy_side/drive_scene.py` (`apply_profile`, `verify_profile`, `apply_ops`), `bpy_side/drive_manifest.py#entry`, `bpy_side/drive_common.py` (anchor `bpy.drive.cycles_seed`), fixture `bpy_side/scene_example.py`.
- Shared seed — `bpy_side/canon.py` (anchors `bpy.canon.canonical_json`, `bpy.canon.cycles_seed`).

## <a id="hb-s6"></a>6. Limits and search

| limit | blocked_use |
|---|---|
| CPU Cycles only | do not request `cycles.device=GPU` (shipped profiles are CPU only) |
| cross-host byte identity is O-B1 (TARGET) | do not compare frame hashes across hosts/Blender builds |
| Blender noise ≠ Godot noise | do not reuse Godot thresholds |
Router entries: "launch blender render" → `bv.launch.owned_process`; "seed for bench frame" → `bv.journal.cycles_seed`; "finish capture receipt" → `bv.finish.finish`; "why integer journal" → hb-s7.

## <a id="hb-s7"></a>7. Narrative

A run: `launch.drive` validates journal and profile, hashes the source, spawns Blender with a declared empty env, `drive_scene` applies journal ops and renders raw PNGs, `finish.canonicalise` re-encodes them to the contract size, hashes decoded RGB (frame identity), writes the receipt, then publishes with `os.replace`. Decisions: D1 integers only so hashes are portable; D2 PNG not EXR for the beauty path; D3 seed formula lives once in `canon.py` to prevent drift (T-BI-SEED).

## <a id="hb-s8"></a>8. Symbols

Generated: see `catalogue/symbols.jsonl` and, when produced by `tools/hb_gates.py`, `hyperbooks/anchors.jsonl` (TARGET: absent at HEAD). Prose above carries anchor ids only.

## <a id="hb-s9"></a>9. Tests and gates

`tests/test_drive.py`, `tests/test_drive_blender.py`, `tests/test_drive_mutants.py`, `tests/test_seed_twins.py`, `tests/test_b0_skeleton.py`; gates `g_bpy_boundary`, `g_threshold`, `g_pure` via `python3 tools/gates.py --all`.

## <a id="hb-s10"></a>10. Provenance

Back-reference (code→book): modules `bv.drive`, `bv.launch`, `bv.finish`, `bv.journal`, `bpy.drive`, `bpy.canon` name this book. The `# @module {..., "book":"HB-BVQA-DRIVE"}` header field is TARGET: code files are outside this lane; the required edits are listed in `v12e/room_W13/REPORT_S2.md`. Source sha256 list: appended below at write time (MEASURED).

Source sha256 table (MEASURED at write time, HEAD 2390e8d + working tree; re-seal when sources change):

| file | sha256 |
|---|---|
| `blender_visual_qa/drive/launch.py` | 06c2dfa7197551ad4c8b3a6836f5e28136ed696e6af4d62b68bb2faf7c1ace99 |
| `blender_visual_qa/drive/finish.py` | 90f35087173185cbe1a1c71026f23195ab787b591b99f248d1245d895836b376 |
| `blender_visual_qa/drive/journal.py` | e3b3a4d4f5b5355dd86cefea9abbd55d3f09510d7c6b6e05430c3abe6614081d |
| `blender_visual_qa/drive/encode.py` | 40b26e2ed7ab2bcde8b302362d6c82a53a8eef3d1170401c00eef5cd7e33f821 |
| `blender_visual_qa/drive/png.py` | 3c3a38e5b59ba8d644af50550a8b888f32d71bbf6763e62929258b6a9a362865 |
| `bpy_side/drive_common.py` | cea34c3252f9eaaf7c164b05581332fecbeb27bcabcbdfb5bf04f4be546c877f |
| `bpy_side/drive_manifest.py` | 6ebd09fb295c1fe2ded6bf248f237b1ed078c0a58b4afe0eabb5ec855f1e249a |
| `bpy_side/drive_scene.py` | 4df47fac690627b66cf36db6ce47b456c08234c6552b244921ac2c17ed02b450 |
| `bpy_side/scene_example.py` | 65c5b031cc6118b52bcc8c7b7855d638f998d16260bea03a6179d0db62ca774a |
| `bpy_side/canon.py` | 5338a542b83662cdcf76f05882699afbd089ad5a738ec455b63e6bf3a5d50719 |
