# Visual QA for 3D environments

Status: branch `feat/visual-qa-3d`, phase R done (component import and router). Phases P
and C are planned and not built. Nothing here is on `main`.

The goal is a visual-QA layer that lets AI agents work reliably in any 3D environment
(Blender, MuJoCo, AutoCAD, NVIDIA Omniverse or Isaac Sim) by checking each step against a
fresh frame and the environment state, with typed verdicts and receipts. The starting point
is the maintainer's Blender bench, imported as
[components/blender-visual-qa](../components/blender-visual-qa/README.md).

## Phases

| Phase | Content | State |
|---|---|---|
| R | Import the Blender bench as a component; route index v2 over its code and documents | done on this branch |
| P | Cited context pack from PapersHub (mainly PapersVideojuegos, plus Papers_Software, PapersSim, PapersVisionEngine, OmniGodot) on 3D asset development, frame performance, determinism and scene verification | not started |
| C | Generic core with one adapter per environment; Blender and MuJoCo implemented, the others declared | not started |

## Component boundary

`components/blender-visual-qa/` is declared in [OWNERSHIP.json](../OWNERSHIP.json), so the
root scanners and root router leave it to its own suite and its own index, like the other
components. It is not in `deps.lock`: no root runtime imports it. Its own suite runs from
the component folder:

```bash
cd components/blender-visual-qa
python3 -B -m unittest discover -s tests -t .
```

Edits made while importing are listed in [PROVENANCE.md](../PROVENANCE.md#blender-visual-qa-import-15-nov-2026).

Licence: the upstream repository carries no licence file, so the component falls under this
repository's default `AGPL-3.0-only` ([NOTICE-EVALUATION.md](../NOTICE-EVALUATION.md)). It has
no row in that notice yet: the notice's component table is pinned byte for byte by
`route/study/agents.jsonl`, and changing it needs a study re-pin. That is the maintainer's call.

## Route index v2

[route/README.md](../components/blender-visual-qa/route/README.md) has the commands. In short:

- `route/build.py` indexes every tracked Python definition (AST), Markdown section, JSON schema
  and test file of the component. It is standard library only, has no clock, and writes the
  same bytes on every run. `--check` returns `ROUTE_OK` or rc 4 with the stale outputs.
- `route/query.py` answers `any`, `find`, `where`, `tests`, `error`, `env` and `bench` with
  `path:line` coordinates in one JSON line of at most 2000 bytes.
- It abstains with rc 3 when a question relies on words the product code and documents never
  use. A proper name counts on its own, so a question about MuJoCo, AutoCAD or Omniverse
  returns `terms_absent_from_corpus` instead of near misses.
- Every symbol has an `env` value: `bpy` (runs inside Blender), `engine` (touches the pinned
  vision engine), `blender` (host code that names Blender), `generic` (no Blender word found).
  This is a lexical signal for phase C, not a proof of independence.

Measured on 15 Nov 2026 over the component's non-test code: 1065 definitions in 84 files,
of which 217 `bpy`, 106 `blender`, 13 `engine` and 729 `generic` [MEASURED].

### Benchmark

`route/bench/queries.tsv` keeps the 42 queries of the older router (splits FIT, DEV, TEST) and
adds HOLD, 44 queries written by a separate agent that was told to paraphrase and spread over
the whole code base. HOLD was frozen ([FROZEN.json](../components/blender-visual-qa/route/bench/FROZEN.json))
before any scoring, and no change was tuned on its misses. Recall@5 [MEASURED]:

| Split | Older router (`tools/route_code.py`) | Route index v2 | Target |
|---|---|---|---|
| TEST, 18 code questions | 16/18 | 16/18 (888888 ppm) | 700000 ppm, met |
| HOLD, 26 code questions | 8/26 | 14/26 | — |
| HOLD, all 38 positive (code, docs, schemas, tests) | not comparable (no such ids) | 21/38 (552631 ppm) | 700000 ppm, **not met** |
| Negatives (TEST 4, HOLD 6) | 10/10 abstained | 10/10 abstained | all abstain |

The recall floors in `FROZEN.json` are the measured values and may only rise.
[tests/test_bvqa_route.py](../tests/test_bvqa_route.py) runs freshness, abstention and the
floors in the `laws-contracts` job.

## Phase C design (planned)

One shared core and one adapter per environment. The core owns capture bookkeeping, the
verdict fold and the receipt; adapters own how to drive and observe their environment.

| Piece | Responsibility | Reuse |
|---|---|---|
| `EnvAdapter.probe()` | Report whether the environment is installed and which version | — |
| `EnvAdapter.capture(step)` | Return a frame (PNG bytes, decoded-RGB sha256) and a state digest | frame side of `vadapt.base.Adapter` in `components/vision-engine/vadapt/base.py` |
| `EnvAdapter.scene_state()` | Canonical, integer-only state: object transforms for Blender, `qpos` and contacts for MuJoCo | component `bpy_side/canon.py`, `q.py` |
| `EnvAdapter.act(action)` | Apply one action through the environment API | Blender inbox bridge pattern of `hackathon/swarm/broker/bridge.py` |
| Verdict fold | `PASS`, `FAIL`, `CANNOT_SEE`, `ADVISORY`, `ERROR` with the worst one winning; a model judge can only make it worse | component `blender_visual_qa/oracles/harness.py` |
| Receipt | Frame hashes before and after, state digest change, adapter id and version | `contracts/v0.2/action_receipt.schema.json` (`state_sha256_change`) |
| Intent router | Question or task to adapter and code coordinates; abstains for an environment with no adapter | route index v2 |

Adapters:

| Environment | Plan | Evidence rule |
|---|---|---|
| Blender | Implemented on the component's drive and oracles | its own suite, plus one logged live run |
| MuJoCo | Offscreen `mujoco.Renderer`, state from `qpos` and contacts; always called a simulation | unit tests with a fake; one logged live run on a host with `mujoco` installed |
| AutoCAD, Omniverse, Isaac Sim | Declared with status `not_implemented`, rc 3 on every call | never return a verdict |

Hosted CI has neither `bpy` nor `mujoco`, so CI tests use recorded fakes. Live behaviour is
claimed only with a log.
