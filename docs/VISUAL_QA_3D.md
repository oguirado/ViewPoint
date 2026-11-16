# Visual QA for 3D environments

Status: branch `feat/visual-qa-3d`. Nothing here is on `main`.

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
| Round 2 | Feedback from the Gemini takes 1631, 1827, 0448 and the nightclub take, plus PapersHub pack, gave: world state and parenting, `set_frame` and batched sampling, tolerances, framing, camera provenance, frame noise and blank checks, render capability probe, typed errors, light state, `blender_live` adapter for the live GUI | done |
| Profiles | `vqa3d.profile/v1` with `film-animation`, `game-realtime`, `hyperreal-still` and `simulation` | first edition done |

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
| Blender (headless scratch copy) | implemented | Live runs logged in the campaign. Recorded traces cover take 1827 (98 objects, rig, bone-parented prop) and the nightclub take (159 objects). |
| Blender live GUI (`blender_live`) | implemented | Inbox/outbox bridge in the running GUI. A capture waits for the draw counter to advance, so a stale frame gives CANNOT_SEE. Checked on an owned Xvfb GUI; capture p50 217 ms over 10 samples, Workbench, software GL. |
| MuJoCo | implemented; always called a simulation | Two independent runs gave identical receipts |
| AutoCAD, Omniverse, Isaac Sim | `not_implemented` | Every call except `probe` raises `AdapterNotImplemented`; probes report CANNOT_SEE |

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

`film-animation` on the real scenes of take 1827 [MEASURED, campaign `live/F3_report.md`]:
- `motion.blend` FAILs. The feet go 36.1 mm and 6.9 mm below the floor and slide while planted.
- `motion_v2.blend` passes the feet, slide and bone checks. It fails only as a shot: it has no camera and renders with EEVEE.
- `final_scene_v2.blend` FAILs on one check. The right shoe drifts 267.8 mm over frames 9–18 while planted.

That last one is a real geometric defect. Whether it reads as a slide on screen has not been checked.

The film thresholds are targets bounded by measured anchors from approved scenes, not universal rules. PapersHub has no
film-production corpus. GPU and OIDN capture are not available on the GB10 host (CUDA sm_121 kernel fails to compile, no
OIDN in this Blender 4.0.2 build), so production-pixel quality is AUSENTE.

## Component boundary

`components/blender-visual-qa/` is declared in [OWNERSHIP.json](../OWNERSHIP.json), so the root scanners and the root router
leave it to its own suite and its own index. It is not in `deps.lock`. Run it from the component folder:

```bash
cd components/blender-visual-qa
python3 -B -m unittest discover -s tests -p "test_vqa3d*.py"
```

[tests/test_vqa3d_core.py](../tests/test_vqa3d_core.py) runs that suite in hosted CI, and
[tests/test_bvqa_route.py](../tests/test_bvqa_route.py) checks the route index freshness, abstention and bench floors.

Edits made while importing are listed in [PROVENANCE.md](../PROVENANCE.md#blender-visual-qa-import-15-nov-2026).

Licence: the upstream repository has no licence file, so the component falls under this repository's default
`AGPL-3.0-only` ([NOTICE-EVALUATION.md](../NOTICE-EVALUATION.md)). It has no row in that notice yet. The notice's component
table is pinned byte for byte by `route/study/agents.jsonl`, so adding the row needs a study re-pin, which is the
maintainer's call.

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
