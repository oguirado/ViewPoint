# Visual QA for 3D environments

Status: The Blender Visual QA integration candidate is under review. The sections below record its implementation and trial evidence.

vqa3d is a visual-QA layer that lets AI agents work reliably in 3D environments. Each step is checked against a fresh frame
and the environment's integer state, and gets a typed verdict and a receipt.

It started from the maintainer's Blender bench, imported as
[components/blender-visual-qa](../components/blender-visual-qa/README.md). The core lives in
[vqa3d/](../components/blender-visual-qa/vqa3d/README.md) and its per-domain profiles in
[vqa3d/PROFILES.md](../components/blender-visual-qa/vqa3d/PROFILES.md).

## Phases

| Phase | Content | State |
|---|---|---|
| R | Import the Blender bench as a component; route index v2 over its code and documents | done |
| P | Cited context from PapersHub (PapersFast and PapersVideojuegos first) | done |
| C | Generic core: `EnvAdapter`, obligations, worst-wins verdicts, `vqa3d.receipt/v1`; Blender and MuJoCo simulation adapters | done |
| Round 2 | Feedback from takes 1631, 1827, 0448 and the nightclub take, plus PapersHub pack, gave: world state and parenting, `set_frame` and batched sampling, tolerances, framing, camera provenance, frame noise and blank checks, render capability probe, typed errors, light state, `blender_live` adapter for the live GUI | done |
| Profiles | `vqa3d.profile/v1` with `film-animation`, `game-realtime`, `hyperreal-still` and `simulation` | first edition done |
| Round 4 | Nightclub take (722k triangles, 4 rigs, 1112 frames): floor resolved under each foot by a 3x3 raycast footprint, `rigid_follow` vs `in_grip` props, one Blender process per profile with frame sampling plus contact gap filling, `--gates-only`, `--nice`/`--cpus`, declared Blender environment, `--capture-engine`, `allow_derived`, typed killed/timeout/dead-GUI reasons | done |

The loop runs per recording. The recording session posts a FEEDBACK file to the OpenSwitchboard room `vqa3d-loop`. A round
then goes through these steps:
- a PapersHub pack is built on the findings;
- writer lanes do the work;
- a cross-family reviewer checks it, then a fix lane;
- a commit lands on this branch;
- a `verdict` post closes the round, and the recording session re-tests its scenarios.

## Core

| Piece | What it does |
|---|---|
| `EnvAdapter` | `probe(render=False)`, `capture(tag)`, `scene_state()`, `act(action)`, optional `sample(frames, paths)`, `close()` |
| State | Canonical JSON, integers only (µm, millidegrees, ppm). Floats, NaN and bool-as-int are refused. |
| Obligations | `state_change`, `state_equals` (optional tolerance), `frame_change` (optional noise floor), `no_change`, `within_distance`, `range_over_frames`, `max_delta_over_frames`, `camera_is`, `frame_not_blank`, `frame_noise_below`, `capability` |
| Verdicts | PASS 0, ADVISORY 0, FAIL 1, ERROR 2, CANNOT_SEE 3; the worst wins. A model judge can only make a verdict worse. Stage return codes are a separate table. |
| Receipts | `vqa3d.receipt/v1`: adapter, obligation, decoded-RGB and state hashes before/after, camera provenance, profile identity, `receipt_sha256` |
| Replay | Recorded traces and replay adapters, so hosted CI tests run without Blender or MuJoCo |

## Adapters

| Environment | Status | Evidence |
|---|---|---|
| Blender (headless scratch copy) | implemented | Live runs, including nightclub runs, are logged in campaign benchmark reports. The shipped `vqa3d/traces/` include procedural Blender replay fixtures. The film goldens under `tests/golden/` include procedural fixtures and the operator-owned take 0448 AstroScout samples. |
| Blender live GUI (`blender_live`) | implemented | Inbox/outbox bridge in the running GUI. A capture waits for the draw counter to advance, so a stale frame gives CANNOT_SEE. Checked on an owned Xvfb GUI; capture p50 217 ms over 10 samples, Workbench, software GL. |
| MuJoCo | implemented; always called a simulation | Two independent runs gave identical receipts |
| AutoCAD, Omniverse, Isaac Sim | `not_implemented` | Every call except `probe` raises `AdapterNotImplemented`; probes report CANNOT_SEE |

The optional headless CUDA worker permits an enrolled worker to name its own threads. CPU is the routine mode. A separate private X11 run used a disposable fixture and establishes behavior for that fixture. Display-backed GPU qualification remains open.

## Profiles

The core is domain-neutral. A profile sets the following for a target domain:
- render and capture settings;
- budgets;
- default thresholds;
- required capabilities;
- reusable checks that select objects by type and name.

Every number in a profile declares where it comes from: `measured:<log>`, `cited:<evidence id>` or `target`.

| Profile | Optimises | Budget that rules |
|---|---|---|
| `film-animation` | Quality per shot: grounded feet, no foot sliding, per-bone rotation continuity, props attached to hands, scene camera, production capture | No triangle or texture budget |
| `game-realtime` | Cost per frame | Triangles per object and scene, texture size |
| `hyperreal-still` | Physical fidelity: scale, explicit lights, colour management, noise | Memory and render time |
| `simulation` | Reproducibility | Identical replay receipts |

The shipped `film-animation` replay fixtures cover ground penetration, stable planted feet, a slide above the floor, and a bone-parented prop moving with its grip. The procedural samples exercise the offline oracles. Historical take 1827 benchmarks are recorded separately in the campaign report `live/F3_report.md`.

On the nightclub take (`final_scene_v3.blend`, 722k triangles, 1112 frames) [MEASURED, campaign `live/C4_report.md`,
`live/F4_report.md`, re-run by the integrator]:
- film-animation used to take 4189 s and give 11 false FAILs; it chose a raised lobby floor and assumed a strap-hung speaker
  was held in a hand.
- It now gives 102 gates PASS and ADVISORY overall in 90 s at the default sampling, or 135 s over the full range.
- No verdict differs between default sampling and the full range on the four scenes measured. A one-frame bone pop can
  still fall between samples at the default stride; `--frames 1,N,1` checks every frame.

The film thresholds are targets bounded by measured anchors from approved scenes, not universal rules. PapersHub has no
film-production corpus. The GB10 film runs used a build that could not compile the CUDA sm_121 kernel and had no
OIDN in the Blender 4.0.2 build used for those measurements, so those runs do not establish production-pixel quality.

## Component boundary

`components/blender-visual-qa/` is declared in [OWNERSHIP.json](../OWNERSHIP.json), so the root scanners and the root router
leave it to its own suite and its own index. It is not in `deps.lock`. Run it from the component folder:

```bash
cd components/blender-visual-qa
python3 -B -m unittest discover -s tests -p "test_vqa3d*.py"
```

[tests/test_vqa3d_core.py](../tests/test_vqa3d_core.py) runs that suite in hosted CI, and
[tests/test_bvqa_route.py](../tests/test_bvqa_route.py) checks the route index freshness, abstention and bench floors.

For Vision-backed commands from a standalone Blender Visual QA wheel, set
`BVQA_VISION_WHEEL` to the absolute local path of the separately provisioned,
read-only `vision_engine-1.1.1-py3-none-any.whl`. Supply the exact artifact
required by the packaged Vision config. A pin mismatch refuses Vision admission
with `E_PIN` (reported in the error detail by the Vision stage).
Checkout and editable commands use the pinned wheel in the source tree.
Existing explicit config overrides take precedence over the standalone selector.

The offline package and read-only site checks cover W10 TRUTH dry-run, asset RAM
workspace admission and Vision parent/worker agreement on synthetic frames.
Live Blender rendering and capture require separate runtime qualification. The project reviews publication through its release process. See the [repository notice](../NOTICE-EVALUATION.md) for the component and wheel licence terms.

Edits made while importing are listed in [PROVENANCE.md](../PROVENANCE.md#blender-visual-qa-import-15-nov-2026).

The component uses [AGPL-3.0-only](../components/blender-visual-qa/LICENSE). The repository omits the upstream licence placeholder. It retains `tools/build_catalogue.py` as an exact copy and adapts `tools/route_code.py` and `tools/build_code_index.py` from `components/vision-engine` at [`8dec8e83768afef63445b03476c6ed673f04c5bc`](https://github.com/oguirado/ViewPoint/commit/8dec8e83768afef63445b03476c6ed673f04c5bc), an ancestor of the public ViewPoint baseline. The [vendoring manifest](../components/blender-visual-qa/tools/VENDORED.json) records their source and staged digests. The [repository notice](../NOTICE-EVALUATION.md#blender-visual-qa-and-its-pinned-vision-engine-wheel) records the scope of the pinned Vision Engine package and its embedded notices. These tools are present in the repository and excluded from the Blender Visual QA source distribution and wheel.

The [Vision wheel](../components/blender-visual-qa/tools/wheels/vision_engine-1.1.1-py3-none-any.whl) is a separate, pinned artifact. The wheel uses `AGPL-3.0-only` and includes its licence and notice. [The build recipe](../components/blender-visual-qa/tools/build_engine_wheel.py) builds it from the [pinned public source](https://github.com/oguirado/ViewPoint/tree/c3b0f53560dcb45e6c503d95e2e848cd3b8f16b7/components/vision-engine). The [repository notice](../NOTICE-EVALUATION.md#blender-visual-qa-and-its-pinned-vision-engine-wheel) records the wheel and embedded notice digests. See [DRIVE provenance](../components/blender-visual-qa/blender_visual_qa/drive/PROVENANCE.md) for the clean-room replacements of `png.py` and `launch.py`. The `examples/cube3.blend` fixture was regenerated via `bpy_side/scene_example.py`.

## Route index v2

[route/README.md](../components/blender-visual-qa/route/README.md) has the commands. `route/build.py` is deterministic and
`--check` prints `ROUTE_OK`. `route/query.py` returns `path:line` coordinates. It abstains with rc 3 when a question relies on
words absent from the product code, as for Unreal, SolidWorks or Houdini.

Recall@5 on the frozen bench, after round 2 and the profiles [MEASURED]:

| Split | Recall@5 | Floor |
|---|---|---|
| TEST, 18 positive | 888888 ppm | 888888 |
| HOLD, 38 positive | 605263 ppm | 552631 |
| Negatives (TEST 4, HOLD 6) | all abstained | all abstain |

HOLD negative `h39` asks where MuJoCo steps the simulation and reads contact forces. vqa3d now does part of that, so the
negative is one stemmed word away from being answered. Re-freezing that row is the maintainer's call.
