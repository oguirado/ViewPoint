# vision-engine

Base: components/vision-engine

`vision-engine` is the deterministic, integer-only visual QA engine used by
MetaViewer to turn rendered frames into tile classes, region records, a
temporal GOP, a token-bounded judge pack, citation verdicts and a queryable
visual memory. It is evidence machinery: it describes change and exposes
coordinates. It is not an admission authority or a semantic vision model. Most
adapters read existing captures; the explicit Xvfb helper captures into a RAM
ring rather than providing a general screen-capture service.

## What the component is

Answers: purpose; pipeline; guarantees; what it deliberately does not do.

Unless a citation gives a repository path, component file names below are
relative to `components/vision-engine/`.

The pipeline is an adapter input (`vadapt`) -> `vqa.frame/v1` manifest -> S0 tile grid ->
S1 descriptors -> S2 change classes -> S3 pyramid -> S4 regions -> S5 GOP ->
S6 pack and citation verification -> S7 `vmem` ingest. The public entry points
are the `vcore` CLI, the `vmem` CLI, the persistent `vadapt.vserve` server and
the adapter module CLIs.

The engine's stated guarantees are part of its contract:

- identical decoded frame bytes and parameters produce byte-identical outputs;
  `vcore selftest` checks each built-in scenario three times;
- no float reaches a stored L0-L3 field, and ratios are integer ppm or num/den;
- every handled `vcore` command writes exactly one canonical JSON object on
  stdout, including handled errors, and uses exit codes `0` OK, `2`
  usage/schema, `3` reject, `4` stale, `5` gate, `6` budget, never `1`. The
  adapter and server entry points have their own contracts: C2 writes one
  JSON payload on success, caught input errors go to stderr and exit `2`, and
  argparse exits before that handler; `vserve` emits a JSON-lines reply
  stream, request error codes live in the replies, startup errors go to
  stderr and a clean EOF exits `0`;
- a refusal leaves caller-owned outputs byte-identical; writes stage to a
  temporary path and publish by atomic rename, with `os.rename` for the run
  directory after per-file staging;
- engine execution keeps frame data in RAM. `g_disk_frames` runs a one-repeat
  determinism smoke, then checks for newly modified frame-like files under the
  component `build` directory, checks that the engine RAM directory is empty
  and checks for new Blender/Godot processes. It is not a host-wide frame-leak
  detector; adapter outputs are caller-owned and may include their declared
  frame files, and `vmem` stores are a separate persistence surface.

The core does not capture the screen, decide whether MetaViewer may act, or
interpret a visual element semantically. Adapters read existing capture
directories, screenshots or RAM rings; the explicit Xvfb path captures into a
ring. The engine reports coordinates and classes; MetaViewer's core remains the
admission and verification authority.

Source identity: the parent `deps.lock` pins commit
`3bb54835db8a2e545311cfcc45185a0dfbd9313a` and labels the dependency `v0.2.0`.
The imported tree reports package and API version `1.1.0` in `pyproject.toml`,
`README.md` and `visioncore/__init__.py`. This document uses the commit as the
source identity and does not reconcile the two version labels.

`vision-engine` is a separate program; this tree is the pinned import used for
local integration work, published under `AGPL-3.0-only` (see the parent notice).

[CITED: `components/vision-engine/README.md`,
`components/vision-engine/AGENTS.md`,
`components/vision-engine/visioncore/cli.py`,
`components/vision-engine/visioncore/errors.py`,
`components/vision-engine/visioncore/pipeline.py`,
`components/vision-engine/visioncore/__init__.py`,
`components/vision-engine/vadapt/x11_ramcap.py`,
`components/vision-engine/tools/gate_checks.py`,
`components/vision-engine/pyproject.toml`, `deps.lock`,
`NOTICE-EVALUATION.md`].

## How MetaViewer reaches it

Answers: which process boundary; how the checkout is pinned; live versus
offline paths.

MetaViewer does not import the engine. `agents/metaviewer_agents/perception/`
can resolve the pinned checkout from `deps.lock`; the adapter accepts an
explicit `VQA_VE_ROOT` checkout and verifies it against the lock only when
`verify_lock=True`. It drives the engine as a subprocess. Live ticks prefer
the persistent `vadapt.vserve` JSON-lines server when it is available.
Eligible persistent failures fall back to one-shot `vadapt.cu_describe` when
fallback is enabled; an `identity_mismatch` propagates and does not fall back
(`agents/metaviewer_agents/perception/vcore.py:2102`). Offline QA uses the
`vcore` verbs. The adapter validates C2 output against the caller's frame
digest and projects it into `vqa.element-table/v1` on the MetaViewer side.

The live C2 contracts are `ve.cu_describe/v1`, `ve.vserve.reply/v1` and
`ve.vserve.hello/v1`. They are produced inline by `vadapt` and are not JSON
Schema files in `visioncore/schemas`; the MetaViewer adapter checks their
fields. The `vqa.*` family is the engine's record contract family.

[CITED: `agents/metaviewer_agents/perception/vcore.py`,
`agents/metaviewer_agents/perception/deps_lock.py`,
`components/vision-engine/vadapt/cu_describe.py`,
`components/vision-engine/vadapt/vserve.py`].

## Entry points and commands

Answers: which executable to run; which command performs each stage.

The `vcore` console script is declared in `pyproject.toml`. From a checkout that
is not installed, use `python3 -m visioncore.cli` with the same arguments.

| Entry point | Command | Purpose |
| --- | --- | --- |
| Engine self-test | `vcore selftest` | Deterministic built-in scenarios; fails on drift |
| Analyze | `vcore analyze <manifest> --out <dir>` | S0-S4, with S5 when the gop module is present; default out is a vision directory beside the manifest |
| Pack | `vcore pack <dir> --manifest <manifest> --out <pack-dir>` | S6 judge pack; requires the GOP output of analyze |
| Cite check | `vcore cite-check <dir> --pack <pack> --judge-output <judge>` | Verify judge pointers against stored evidence; `verify-citations` is an alias |
| Rejudge | `vcore rejudge --from-run <dir> --pack <pack> --judge-output <judge> [--out <file>]` | Re-verify stored evidence without capture or inference |
| Persistent server | `python3 -m vadapt.vserve [--max-control-line-bytes N] [--max-frame-bytes M] [--max-sessions K]` | C2 JSON-lines server on stdin/stdout |
| One-shot C2 | `python3 -m vadapt.cu_describe --frame <png> --json [--prev <png>] [--marks-out <path> --ocr-marks]` | Describe one frame or a frame pair |
| GUI adapter | `python3 -m vadapt.gui.from_screens --in <run-dir> --out <out-dir>` | Screenshots, step JSON-lines, accessibility hints or raw RGB24 -> `vqa.frame/v1` |
| Godot adapter | `python3 -m vadapt.godot.from_capture_dir --in <run-dir> --out <out-dir>` | Godot capture directory -> `vqa.frame/v1` plus region hints |
| Blender adapter | `adapt(in_dir, out_dir)` in `vadapt/blender/from_render_dir.py` | Render directory -> manifest; no module CLI at this commit |
| Visual memory | `vmem` module CLI in `vmem/cli.py` | Route, answer, inspect, ingest, compact, verify and roll back stores |
| RAM workspace | `python3 -m vadapt.ramws {sweep,sweep-kept,status,check-disk}` | Inspect or clean the RAM workspace; rc `5` on a law breach |
| Gates | `python3 tools/gates.py --all` | Static and dynamic repository gates; rc `5` on failure |
| Gate mutants | `python3 tools/gates.py --mutants` | Prove the gates kill planted defects |
| Catalogue | `python3 tools/build_catalogue.py [--check]` | Rebuild generated catalogue files; `--check` returns `4` when stale |
| Code router | `python3 tools/route_code.py --q "<task>"` | Return at most three code anchors; rc `3` means abstain |
| Reproducible wheel | `python3 tools/build_wheel.py [<rev>] [--out <dir>]` | Build from `git archive`; prints the wheel sha256 |
| Native build | `python3 tools/build_native.py [--out <dir>]` | Offline `visioncore_native` build; rc `3` when cargo or crates are absent |
| Per-file test health | `bash tools/ci_health.sh [outdir]` | Run one fresh unittest process per top-level test module |

`vmem` verbs are `route`, `answer`, `get`, `evidence`, `last-good`,
`diff-region`, `seen-defect`, `check-answer`, `verify`, `rollback`, `ingest`,
`compact`, `gc` and `pin`. The `vserve` operations are `describe`, `hello`,
`ping`, `capabilities`, `open_session`, `close_session`, `reap` and `status`.
A missing `op` with a frame key defaults to `describe`.

[CITED: `components/vision-engine/pyproject.toml`,
`components/vision-engine/visioncore/cli.py`,
`components/vision-engine/vadapt/vserve.py`,
`components/vision-engine/vadapt/cu_describe.py`,
`components/vision-engine/vadapt/gui/from_screens.py`,
`components/vision-engine/vadapt/godot/from_capture_dir.py`,
`components/vision-engine/vadapt/blender/from_render_dir.py`,
`components/vision-engine/vmem/cli.py`,
`components/vision-engine/vadapt/ramws.py`,
`components/vision-engine/tools/gates.py`,
`components/vision-engine/tools/build_catalogue.py`,
`components/vision-engine/tools/route_code.py`,
`components/vision-engine/tools/build_wheel.py`,
`components/vision-engine/tools/build_native.py`,
`components/vision-engine/tools/ci_health.sh`].

## Packages and responsibilities

Answers: which tree owns each stage; which imports are allowed.

| Path | Responsibility | Boundary |
| --- | --- | --- |
| `visioncore/` | Sealed core, stages S0-S6 and layers L0-L4. Modules `grid`, `desc`, `change`, `pyramid`, `regions`, `gop`, `pack`, `cite`; `pipeline` orchestrates and `cli` exposes `vcore` | Never imports `vmem` or `vadapt`; numpy only from L1 upward |
| `vmem/` | S7 visual memory: content-addressed blobs, generations, single-writer store, receipts, compaction, query ladder and verification gates | May import only `visioncore.{ids,layout,errors,canon}` plus the standard library |
| `vadapt/` | Engine adapters, C2 description, `vserve`, marks and OCR evidence, RAM workspace and frame ring, Godot/Blender/GUI input, Xvfb RAM capture | Layer gate allows `visioncore` imports `frame`, `ids`, `errors`, `canon`, `params` and the canonical PNG `decode`; C2 helpers also reuse local `eval` kernels and never import `vmem` |
| `vnative/` | Opt-in backend switch for native kernels (`VISIONCORE_BACKEND`) and dual verification | Outside the sealed `visioncore/` tree |
| `rust/` | `visioncore_native` cdylib and native kernel experiments | Built by `tools/build_native.py`; not required by the Python path |
| `tools/` | Gates, catalogue builder, code router, reproducible wheel, native build, brief checker and test-health runner | Standard library only |
| `eval/` | Evaluation and measurement scripts (determinism, citations, storage, briefs) and retained results | Outside `visioncore`; C2 helpers import selected local `eval` kernels |
| `langx/` | Independent cross-language conformance: X1 for two pixel kernels, X2 for browser UI steps | Not imported by `visioncore`, `vmem` or `vadapt` |
| `tests/` | Unit, property, adversarial, end-to-end and packaging suites; 362 top-level `tests/test_*.py` modules at this commit | Runs against the checkout |
| `hyperbooks/`, `hbtools_v2/` | HyperBook documentation, build and audit tooling | Out of the runtime path |

The ownership registry assigns CORE to `grid`, `desc`, `change`, `pyramid`
and `regions`; TIME to `gop`, `pack` and `cite`; and VMEM to `vmem/`. ARCH owns
the shared registries, `params`, errors, canonical JSON, frame/decode,
pipeline, CLI and adapters. The shared registry wins ties.

[CITED: `components/vision-engine/AGENTS.md`,
`components/vision-engine/visioncore/`,
`components/vision-engine/vmem/`,
`components/vision-engine/vadapt/`,
`components/vision-engine/vnative/backend.py`,
`components/vision-engine/rust/visioncore_native/Cargo.toml`,
`components/vision-engine/tools/`,
`components/vision-engine/langx/`].

## Perception schemas

Answers: which records cross the boundary; what each carrier is.

The schema files live in `visioncore/schemas/`. They use the slash form
`vqa.<name>/v<N>` (and the `vmem.unit/v1` family); the filename replaces the
slash with a dot and appends `.schema.json` (for example
`visioncore/schemas/vqa.frame.v1.schema.json`).

Carrier names below are runtime artifacts the engine writes or reads, not files
tracked in this repository; a tracked manifest example is
`tests/golden/clean_static/frames.manifest.json`.

| Schema | Carrier | Role |
| --- | --- | --- |
| `vqa.frame/v1` | frames.manifest.json | Input manifest: dimensions, RGB24 format, frame timeline and digests, `png_dir` or `rgb24_raw` storage |
| `vqa.profile/v1` | profile.json | Adapter, grid, renderer and source digest declaration |
| `vqa.region-hint/v1` | region_hints.jsonl | Optional UI, actor, object, gizmo or accessibility rectangles |
| `vqa.vision/v1` | vision.json | Run identity: API version, parameter and frame-list digests |
| `vqa.tile/v1` | tiles.bin header | Tile-grid geometry and 32-byte packed tile records |
| `vqa.core-frame/v1` | frames.jsonl | Per-frame class counts, coverage and motion summary |
| `vqa.core-region/v1` | regions.jsonl | Region island: class, kind, tile and pixel boxes, motion, residual |
| `vqa.core-link/v1` | links.jsonl | Previous-to-current region correspondence, splits and merges |
| `vqa.gop/v1` | gops.jsonl | Temporal GOP boundaries, frame IDs and blob digests |
| `vqa.pack/v2` | pack.json | Judge-pack frame map, byte cap and payload digest |
| `vqa.judge-input/v2` | Judge input | At most eight pointers into the pack |
| `vqa.judge-output/v2` | Judge output | Model ID, digest quote and at most eight pointers |
| `vqa.cite-verdict/v1` | Verifier output | Per-pointer acceptance and final status |
| `vqa.episode/v1` | episodes.jsonl | Compact episode record for visual-memory ingest |
| `vmem.unit/v1` | Unit records | Memory unit: layer, pin, scope, track key, predicate and anchors |
| `vmem.manifest/v1` | Generation manifest | Generation identity for a store snapshot |
| `vmem.catalogue/v1` | Catalogue | Foreground unit rows for a generation |
| `vmem.answer/v1` | Answer record | Query-ladder answer envelope |

The tree also carries test/evaluation schemas (`vqa.px-mutant/v1`,
`vqa.mutant-manifest/v3`, `vqa.eval-row/v1`); they do not cross the runtime
perception boundary.

A derived consumer contract, `vqa.vision-brief/v1`, is defined in
`components/vision-engine/docs/spec/VISION_BRIEF_V1_SPEC.md`, encoded by
`components/vision-engine/eval/vision_brief.py` and checked by
`components/vision-engine/tools/vision_brief_check.py`. It summarizes frame
and region rows for a multimodal model; `vcore` does not emit it.

Limits worth checking before calling the engine:

- frame width and height are `1..8192`; the grid uses the smallest power-of-two
  tile of at least 16 px that keeps cells at most 4096 and both axes at most
  255;
- tile records are packed little-endian at 32 bytes; class values are
  `UNCHANGED`, `JITTER`, `CHANGED`, `MOVED`, `FLICKER`, `FROZEN`, `DRIFT`;
- region kinds are `EDGE`, `TEXTURE`, `SMOOTH`, `MIXED`; verdicts are `PASS`,
  `FAIL`, `CANNOT_SEE`;
- the judge contract allows at most 8 pointers, and `visioncore/pack.py` uses at
  most 4 heatmaps and 6 crop pairs per pack;
- `Z1_MAX_B` is 6000 bytes; `PACK_CALL_TOK`, `PACK_RUN_TOK` and
  `PACK_RETRY_TOK` are 15500, 62000 and 93000 tokens;
- `vserve` defaults are 65536 bytes per control line, 33554432 bytes per frame
  and 16 sessions; each limit refuses with `E_BUDGET`, rc `6`.

[CITED: `components/vision-engine/visioncore/schemas/`,
`components/vision-engine/visioncore/layout.py`,
`components/vision-engine/visioncore/grid.py`,
`components/vision-engine/visioncore/params.py`,
`components/vision-engine/visioncore/pack.py`,
`components/vision-engine/visioncore/cite.py`,
`components/vision-engine/vadapt/vserve.py`,
`components/vision-engine/docs/spec/VISION_BRIEF_V1_SPEC.md`,
`components/vision-engine/eval/vision_brief.py`,
`components/vision-engine/tools/vision_brief_check.py`].

## C2, marks and OCR

Answers: what a live description contains; how marks are bounded; what OCR can
and cannot establish.

`vadapt.cu_describe` returns `ve.cu_describe/v1`: `frame_sha256`, width and
height, `changed`, at most 16 `changed_regions`, at most 8 `crops` and
`timing_us`. Optional fields add S4 adjacency, crop images, marks and the marks
PNG. A region `score` is its share of dirty tiles, not a confidence that an
action will succeed. `vserve` returns the same `ve.cu_describe/v1` payload
shape inside `ve.vserve.reply/v1`; `hello` and `capabilities` return
`ve.vserve.hello/v1`.

`vadapt.cu_marks` produces visual candidates, not semantic or authoritative
evidence of interactivity. It keeps at most 24 boxes, drops boxes smaller than
12x10, wider than three quarters of the frame or taller than one third, and
suppresses glyph islands enclosed by a control outline. A mark `score` is rim
edge density, not click confidence. `write_marks` accepts only the fixed
RAM-only marks directory and replaces the destination atomically. This commit
does not emit a `mark_cap_reached` flag; the MetaViewer adapter derives
`len(marks) == 24` when the field is absent.

Call contract for C2 and its helpers: exactly one current-frame source
(`--frame`, `--frame-raw` or `--ring`) and at most one previous-frame source.
Raw inputs must name an absolute path inside the verified shared-memory
namespace and refuse traversal and symlinks; ring paths are resolved and
refused when they escape that namespace. Marks output requires an existing
parent and a crop destination must be an existing directory, both under the
fixed RAM namespace; the caller creates them, and the component stages inside
that location before replacing the final path atomically. Every `vserve`
request requires a valid `request_id`, and a missing `op` defaults to
`describe` only when a frame-source key is present.
The path custody is in `vadapt/cu_describe.py` (`_check_shm_path` and the ring
read) and `vadapt/cu_marks.py` (`write_marks`, `validate_ram_path`); the
request gate is `vadapt/vserve.py` (`handle`).

OCR is opt-in and external:

- `--ocr-marks` is refused without `--marks-out`; OCR is off by default.
- A readiness probe requires an executable `tesseract` and a readable
  traineddata file for the requested language; the language must appear in
  `--list-langs`.
- One full-frame OCR process runs per call when at least one mark is eligible,
  with `--psm 6` and a per-process timeout capped at 5 seconds; the readiness
  probe is a separate process and no overall call cap applies. It parses at
  most 4096 TSV rows and drops negative-sentinel confidence rows.
- Joined labels are sanitized and bounded to 64 UTF-8 bytes; confidence is
  stored as integer promille.
- The OCR helper can skip caller-declared accessibility names and mark them
  `named_by_ax`. The current C2 CLI does not pass a named-mark set, so all
  returned marks are OCR candidates and a mark with no matching word keeps
  `missing_reason: ocr_empty`.
- Missing data or a failed run does not fail C2: marks carry
  `tessdata_absent`, `ocr_unavailable` or `ocr_empty`, and the rest of the
  description is still returned.
- OCR requires the clean pre-annotation RGB buffer and verifies its digest; it
  never reads the annotated marks PNG.

The deterministic `fallback_mark_lines` path classifies `visual_kind` and counts
text lines from pixels. It does not read text. That fallback is a bounded
substitute for absent OCR evidence, not a fix for a missing OCR stack.

[CITED: `components/vision-engine/vadapt/cu_describe.py`,
`components/vision-engine/vadapt/cu_marks.py`,
`components/vision-engine/vadapt/cu_ocr.py`,
`components/vision-engine/vadapt/vserve.py`,
`agents/metaviewer_agents/perception/vcore.py`].

## Known failure modes and open limits

Answers: what refuses; what is expected to degrade; which limits remain.

Refusals are typed and deliberate:

| Condition | Behavior |
| --- | --- |
| Dimensions outside `1..8192`, or no grid satisfies the cell and axis caps | `E_GRID`, rc `2` |
| Malformed manifest, frame count, timeline or digest fields | `E_VC_FRAME_SCHEMA`, rc `2` |
| Decoded frame size differs from the manifest | `E_VC_FRAME_SIZE`, rc `3` |
| Frame digest differs from the recorded digest | `E_VC_FRAME_SHA`, rc `3` |
| Output directory is occupied | `E_VC_OUTPUT_EXISTS`, rc `3`; nothing is published |
| Pack exceeds a byte or token cap | `E_BUDGET`, rc `6`; no pack is written |
| No pointer of a `FAIL` result is accepted after syntax, existence, window, class, relevance and quote checks | Status becomes `CANNOT_SEE`, rc `3` |
| Some pointers of a `FAIL` result are accepted while siblings are rejected | Result stays `FAIL`; rejected siblings remain in diagnostics and can set a failure receipt while the overall rc is `0` |
| Supplied quote is unbound | Every pointer of that result is rejected with `E_CITE_QUOTE` |
| Ring frame becomes stale while being read | `E_STALE`; a torn frame is not accepted |
| New frame-like files under `build`, a non-empty engine RAM directory or a new Blender/Godot process after the smoke | `g_disk_frames` gate failure, rc `5` |
| `vmem` lock is held or the current generation changes during a read | `E_STALE` |
| OCR is absent or fails | C2 still succeeds; marks carry a `missing_reason` |
| Native `dual_verify` is requested while kernels are shims | `E_VN_NATIVE_MISSING`; `native` with `workers > 1` refuses with `E_VN_WORKERS` |

Citation acceptance is existential per judge result, not universal.
`visioncore/cite.py:410` sets `accepted = bool(out["accepted_pointers"])`;
only an empty accepted set adds `E_CITE_NONE` and changes the status to
`CANNOT_SEE`. Reproduced with reason `frozen` and check ID `review`: a single
accepted pointer gives rc `0`, status `FAIL` and receipt `CV:PASS`; one
accepted plus one invented pointer gives rc `0`, status `FAIL` and receipt
`CV:FAIL:SY`; an invented-only list gives rc `3`, status `CANNOT_SEE` and
receipt `CV:FAIL:SY,NO`. A zero exit therefore does not mean every pointer was
accepted, and an unbound quote rejects every pointer of its result.

Open limits, stated as limits rather than fixed behavior:

- the heavy-flicker storage row can exceed the clean-run L2 budget; it is kept
  as an advisory row, not a PASS/FAIL gate;
- CPU budget excludes PNG decode by definition, and the optional GPU path is
  not gated;
- tmpfs may spill to swap under host memory pressure; the workspace records
  peak RAM and swap growth but cannot bound host-wide swap;
- `vmem verify` reports `g_determinism` and `g_cache_stable` as `absent`
  because they require an ingest run; `g_privacy` is a no-op hook;
- the kernel table in `vnative/backend.py` used by `vnative.run` is still
  Python identity shims (`KERNELS_REAL = False`); the switch and
  dual-comparison scaffolding exist, but `native` mode is not an acceleration
  claim;
- Blender EXR frames are not decoded; render PNG upstream.
- `KNOWN_LIMITATIONS.md` also records KL-4 and KL-5 as resolved historical
  findings; their old numbers are not current failures.

[CITED: `components/vision-engine/visioncore/errors.py`,
`components/vision-engine/visioncore/cite.py`,
`components/vision-engine/visioncore/pipeline.py`,
`components/vision-engine/visioncore/params.py`,
`components/vision-engine/vadapt/rtring.py`,
`components/vision-engine/vadapt/ramws.py`,
`components/vision-engine/vmem/gates.py`,
`components/vision-engine/vmem/store.py`,
`components/vision-engine/vnative/backend.py`,
`components/vision-engine/vnative/run.py`,
`components/vision-engine/vadapt/blender/from_render_dir.py`,
`components/vision-engine/KNOWN_LIMITATIONS.md`].

## How it is tested

Answers: which suites exist; which command runs them; what a skip means.

The test tree is broad. At this commit, `tests/` contains 362 top-level
`tests/test_*.py` modules plus fixtures, golden data and mutation drivers. The
main families are:

| Family | Representative suites | What it covers |
| --- | --- | --- |
| Core stages | `tests/test_grid.py`, `tests/test_desc.py`, `tests/test_change.py`, `tests/test_pyramid.py`, `tests/test_regions.py`, `tests/test_gop.py`, `tests/test_pack.py`, `tests/test_cite.py` | Stage contracts, integer properties and determinism |
| CLI and end-to-end | `tests/test_pipeline.py`, `tests/test_pipeline_e2e.py`, `tests/test_pipeline_shm.py` | Analyze, pack, cite-check, refusal, budget and shared-memory input |
| C2 and adapters | `tests/test_cu_describe_unit.py`, `tests/test_cu_describe_raw.py`, `tests/test_vserve_unit.py`, `tests/test_vadapt_godot.py`, `tests/test_vadapt_blender.py`, `tests/test_vadapt_gui.py` | Live description, server protocol and input adapters |
| Adversarial and storage | `tests/test_adv_ramws_escape.py`, `tests/test_adv_rtring_stale_seq.py`, `tests/test_adv_png_chunk_families.py`, `tests/test_decode_malformed_fuzz.py`, `tests/test_e15_law_gates.py` | Escapes, stale rings, malformed PNGs and the storage law |
| Visual memory | `tests/test_vmem_store.py`, `tests/test_vmem_router.py`, `tests/test_vmem_compact.py`, `tests/test_vmem_gates_negative.py`, `tests/test_vmem_ingest_spool.py` | Store, query ladder, compaction, gates and spool ingest |
| Gates and packaging | `tests/test_gates.py`, `tests/test_gates_fix.py`, `tests/test_e16_build_wheel.py`, `tests/test_e4_vadapt_packaging.py` | Gate mutants, fail-closed behavior, reproducible wheel and subpackages |

Commands, from `components/vision-engine`. Every command runs inside the
mandated memory scope; the repository health script is Bash, not Python.

```sh
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- python3 tools/gates.py --all
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- python3 tools/gates.py --mutants
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t .
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- bash tools/ci_health.sh
```

`tools/ci_health.sh` runs one fresh `python3 -m unittest <module>` per top-level
test file with a 300-second cap. It reports one known heavy module as
`SKIPPED_HEAVY`; a skip is not a pass.

The `langx/` tree is a separate cross-language experiment, not a runtime port.
X1 has 15 seeds (5 Go, 5 Python, 5 Rust) implementing the K1 BGRX32-to-RGB24
repack and the K2 damage-rectangle-to-tile-bitset kernel;
`langx/x1/host/conformance.py` checks them against 38 K1 and 41 K2 fixtures.
X2 has 6 browser seeds (3 Python, 3 TypeScript) over shared HTML page fixtures
and uses `langx/x2/host/compare.py` for determinism, majority conformance and
timing.

```sh
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- python3 langx/x1/host/conformance.py
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- python3 -m unittest discover -s langx/x2/host
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- env CHROME_BIN=<browser> python3 langx/x2/host/compare.py --seeds py_w1,py_w2,py_w3,ts_w1,ts_w2,ts_w3 --reps 5 --out <path>
```

X1 needs Python with numpy, Go, Cargo and `taskset`; its README documents a
fixture defect in which the committed `langx/x1/fixtures/repack_*.bin` files
contained escaped text. The harness can rebuild the small buffers in RAM from
`langx/x1/fixtures/gen_fixtures.py` and exposes `--use-committed-bins` to
reproduce the defect. That is a harness workaround for a fixture defect, not a
seed or kernel fix. In this checkout the two bins are 2216 and 1682 bytes, and
`langx/x1/host/RESULTS.md` records that they were regenerated.

X2 prints `SKIP` and exits `0` when `CHROME_BIN` is unset. That skip is not
evidence of conformance. The committed X1 `langx/x1/host/RESULTS.md` is a
historical run at its stated branch and host; this document does not present
those timings as a new measurement.

[CITED: `components/vision-engine/tests/`,
`components/vision-engine/tools/ci_health.sh`,
`components/vision-engine/langx/x1/host/README.md`,
`components/vision-engine/langx/x1/host/conformance.py`,
`components/vision-engine/langx/x1/host/RESULTS.md`,
`components/vision-engine/langx/x1/fixtures/check_fixtures.py`,
`components/vision-engine/langx/x2/host/README.md`,
`components/vision-engine/langx/x2/host/compare.py`].

## Improvement candidates

Answers: which register entries concern this component.

These are campaign improvement-register entries, listed by ID and title, with
the register status summarised below the table. They are proposals, not claims
about current behavior.

| ID | Title |
| --- | --- |
| IMP-05 | Mark ceiling and regional paging |
| IMP-06 | Goal-aware mark selection |
| IMP-07 | Marks for blank editable regions |
| IMP-08 | OCR data discovery under scrubbed environments |

Register status: IMP-05, IMP-06 and IMP-07 are `open`; IMP-08 is
`worked around in MetaViewer` and depends on IMP-31. None is an implemented
fix at this commit.

## Open questions

Answers: what this document does not settle.

- The parent lock's `v0.2.0` label and the imported tree's `1.1.0` version are
  not reconciled here; the commit is the reliable identity.
- C2 does not emit `mark_cap_reached`, so each consumer must derive the cap
  condition. The MetaViewer adapter currently does so.
- Native kernels are shims at this commit; whether a real native build becomes
  a gated path is outside this document.
- OCR discovery under scrubbed environments is an improvement entry (IMP-08)
  whose register status is `worked around in MetaViewer`, not implemented
  behavior.
- No live timing, GPU or full-host qualification is claimed here. The committed
  evaluation results are historical evidence, not a new run.
