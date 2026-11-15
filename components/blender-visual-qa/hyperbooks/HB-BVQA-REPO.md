# HB-BVQA-REPO — repository layout, catalogue, gates and migration (`tools/*`, `catalogue/*`, `migration/*`)

Status: active · Subject commit `2390e8d` · Normative sources: `v12bs/BSPEC_BINT.md` §1/§6 (registry, waves), `v12s/SPEC_ARCH.md` §1/§6/§10 (layout, AI-navigable repository, gates) reached by mathlink only. Etags: CITED, TARGET. No receipts: no MEASURED claim.

## <a id="hb-s0"></a><a id="pm-s0"></a>0. Reading map

| Persona | Read | Question → section |
|---|---|---|
| implementer | hb-s1, hb-s3 | Where does code go and which gates apply? → hb-s1, hb-s3 |
| maintainer | hb-s5, hb-s6 | How is the catalogue regenerated? → hb-s5 |
| verifier | hb-s4 | Which gate guards which rule? → hb-s4 |

Prerequisites: `llms.txt` then `AGENTS.md` (entry order). Conventions: hand edits only in `catalogue/*.src`; generated files come from `tools/build_catalogue.py`.

## <a id="hb-s1"></a><a id="pm-s1"></a>1. Contracts

Host package `blender_visual_qa` must not import `bpy`, `bmesh`, `mathutils` or `gpu`; `bpy_side` must not import `blender_visual_qa`; only pure-stdlib `bpy_side.q` and `bpy_side.canon` cross. Numpy only in `truth/`, `oracles/pixel.py`, `vision_stage.py`. Schema ids: `bvqa.<name>/vN` for Blender, inherited `vqa.<name>/vN` for engine contracts. Stage rc table (0, 2, 3, 4, 5, 6; rc 1 forbidden) lives in `AGENTS.md`. Engine pin: `config/vision.json`; vendored tooling provenance: `tools/VENDORED.json`. Subjects: drive, truth, oracles, judge, report, vision, repo, mutants, bpy (`catalogue/subjects.json`).
Code router: `python3 tools/route_code.py --root catalogue/router --q "task"`; abstention is rc 3.

## <a id="hb-s2"></a><a id="pm-s2"></a>2. Types, weights and budgets (thresholds)

| id | value | unit | source | status |
|---|---|---|---|---|
| `tools/gates.py --all` wall | ≤ 60 | s | `CARDS_WBLD.md` W9 gate | TARGET |
| unit test timeout | 300 | s | W9 G-T0 | CITED |
| thresholds catalogue | `catalogue/thresholds.src` → `thresholds.tsv` | rows | `g_threshold` | CITED |
| router TEST recall@5 / MRR | ≥ 0.70 / ≥ 0.50 | ratio | W13 S4 | TARGET |
| code→book header coverage | ≥ 18/20 sampled | ratio | W13 S2 | TARGET |

## <a id="hb-s3"></a><a id="pm-s3"></a>3. DAG and commands

Gates in `tools/gates.py` (anchor `tools.gates.main`): `g_migration`, `g_vendor`, `g_pin`, `g_tier1`, `g_no_engine_leak`, `g_bpy_boundary`, `g_numpy_scope`, `g_schema_prefix`, `g_wave_dag`, `g_threshold`, `g_pure`; the `g_hb_*` family lives in `tools/hb_gates.py` (W13 S1). Commands: `python3 tools/gates.py --all`; `PYTHONDONTWRITEBYTECODE=1 timeout 300 python3 -B -m unittest discover -s tests`; catalogue rebuild `python3 tools/build_catalogue.py` and `python3 tools/refresh_catalogue.py` (anchors `tools.buildcatalogue.main`, `tools.refreshcatalogue.main`). Never run `make`. Never launch Blender for documentation waves.

## <a id="hb-s4"></a><a id="pm-s4"></a>4. Triptych

**Contracts / claims**
- C-REPO-BOUNDARY (invariant, CITED): host/bpy import separation enforced by `g_bpy_boundary`.
- C-REPO-PIN (invariant, CITED): `config/vision.json` pin matches the vendored source commit and the wheel bytes (`g_pin`).
- C-REPO-NOLEAK (invariant, CITED): no engine algorithm copied into this repo (`g_no_engine_leak`).
- C-REPO-WAVE (invariant, CITED): `migration/wave_plan.json` is a DAG with B2 ‖ B3 (`g_wave_dag`, T-BI-WAVE).
- C-REPO-THRESH (rule, CITED): every threshold has a catalogue row; no `CITED` without a check (`g_threshold`).
- C-REPO-ANCHOR (rule, TARGET W13): every code anchor has one owning book and every book anchor exists in code (`g_hb_*`).

**Anti-uses** (blocked_use): do not edit generated catalogue files by hand; do not edit `tools/gates.py` outside a gate wave; do not add a `.blend` binary to git (benches are procedural); do not read credential or token files.

**Oracle**: `tests/test_b0_skeleton.py`, `tests/test_gates_w9.py`, `tests/test_seed_twins.py`; gate mutants are planted per SPEC_ARCH §10.

## <a id="hb-s5"></a><a id="pm-s5"></a>5. Subsystems

- Catalogue — `catalogue/{layers,subjects,routes,symbols,thresholds,failures,measurements}.*` (`.src` are inputs, `.json/.tsv/.jsonl` generated).
- Anchor grammar — `# @anchor:<prefix>.<unit>.<symbol>` and `# @module {json}` read by `tools/build_catalogue.py`; build accepts prefixes `vc|vm|va|tools`, refresh accepts `bv|bpy|tools`. DEVIATION: the two regexes disagree on `bv.*`; flagged for the router lane (S4).
- Router — `tools/route_code.py`, vendored `catalogue/router/`.
- Migration — `migration/copy_manifest.tsv`, `migration/wave_plan.json`, `tools/MIGRATION_POLICY.json`, `tools/MIGRATION_SOURCE.json`.
- Entry docs — `llms.txt`, `AGENTS.md`, `README.md`, `QUICKSTART.md`, `docs/negative-knowledge/`.

## <a id="hb-s6"></a><a id="pm-s6"></a>6. Limits and search

| limit | blocked_use |
|---|---|
| `@module` headers exist only in `cli.py`, `selftest.py`, `tools/gates.py`, `tools/refresh_catalogue.py` | do not assume module ids for judge/report/vision/truth |
| no `book` field in any header | do not claim code→book coverage |
| `symbols.jsonl` carries 18 rows (several belong to other books) | do not route by anchor for unanchored modules |

Router entries: "which gate checks X" → hb-s3; "where do pins live" → `config/vision.json`; "anchor grammar" → hb-s5.

## <a id="hb-s7"></a><a id="pm-s7"></a>7. Narrative

The repo is a copy-and-adapt of `godot-visual-qa` (`copy_manifest.tsv`) with Blender-specific stages. Decisions (ADR): D1 pin the engine by wheel hash so every run is reproducible · D2 gates are stdlib and fail closed, each with a planted mutant · D3 generated catalogue, never hand-kept, so books and code cannot drift silently · D4 stage rc 1 is reserved for report-level FAIL.

## <a id="hb-s8"></a><a id="pm-s8"></a>8. Symbols (generated: see catalogue/symbols.jsonl)
<!-- hb-generated sha256=9b10cbd74412792a6bf6915ad3d567dc3a8c1326b2ec50eb01049e81abddb70f -->

Anchors with rows: `bv.cli.main`, `bv.contracts.{canonical,validate,load}`, `bv.judge.{png,validate,run}`, `bv.report.{reconcile,emit}`, `tools.{buildcatalogue,gates,refreshcatalogue,routecode}.main`. Lines are in `symbols.jsonl`, not here.

## <a id="hb-s9"></a><a id="pm-s9"></a>9. Edges
<!-- hb-generated sha256=a3b3cc38475762278c9449a2cbd0c05a8b4b463420e3589aebe158c5429d90c6 -->

Mathlinks (path#section, no copied text) into the `vision-engine` specs: `v12s/SPEC_ARCH.md#1` (layout), `#2` (layer DAG), `#6` (AI-navigable repository, anchors and module header), `#10` (gates); `v12s/SPEC_INT.md#R3`, `#R4`; `v12bs/BSPEC_BINT.md#1`, `#6`.

## <a id="hb-s10"></a><a id="pm-s10"></a>10. Provenance
<!-- hb-generated sha256=bbcd32442317a8a549461c91b660636897695f547b3cf5409b7976f7b1924eda -->

| path | sha256 | lines |
|---|---|---|
| `tools/gates.py` | `73fdea07fdef2d2b0a174836cbc74cd0457f6b90577bed59e38f9102b2974394` | 331 |
| `tools/route_code.py` | `e93ac70af29d29d8a03fe2383aaba3c278dd109fc0672331b39749199dc84a98` | 171 |
| `tools/build_catalogue.py` | `29f19c4ea16e045f3019f39b9b7513a0c2dd50a74f67d60a8fd18b187d5f3ea7` | 187 |
| `migration/wave_plan.json` | `75e4f8ac1c502b360cf2fb670fe2562a82cac1489013a5bc75b9e1e8ab82393c` | 1 |
| `AGENTS.md` | `c34645a88c10bf8c388ca5d8c077fa608d77bfecb5cbade3d94dc3a10ff8ac63` | 34 |

Receipts: none (AUSENTE). Anchors in code: `tools.gates.main`, `tools.buildcatalogue.main`, `tools.refreshcatalogue.main`, `tools.routecode.main`.
