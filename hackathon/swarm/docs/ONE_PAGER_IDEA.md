# IDEA — ViewPoint Swarm: Multi-Agent 3D Capsule Studio (`ONE_PAGER_IDEA.md`)

## Problem & Idea

Without explicit coordination boundaries, multiple AI agents issuing concurrent commands on one 3D scene overwrite scene state, scramble viewport context, and produce untraceable regressions.

**ViewPoint Swarm** orchestrates multi-agent collaborative 3D asset generation ("StarForge Sentinel") on **one shared Blender 4.0.2 instance** [MEASURED] (job `vpswarm` running on a private Xvfb display `:2` [MEASURED]) governed by an explicit non-interactive turn queue:

`THINK -> ENQUEUE -> TURN -> RETURN`

1. **Concurrent Reasoning, Serialized Actuation**: Agents inspect frozen rendered frames and plan/author scripts concurrently. However, actuation on the shared `.blend` file and viewport is strictly serialized one turn at a time by the broker's turn queue (`swarm/broker/broker.py`), preventing concurrent write hazards.
2. **Disjoint Prefix Write-Set Claims**: Resource mutations are partitioned into explicit namespaces declared per agent and checked by the broker; out-of-claim effects are detected and logged as spill:
   - `modeler-agent`: `col:CHAR_`, `obj:CHAR_`
   - `props-agent`: `col:PROPS_`, `obj:PROP_`
   - `look-agent`: `mat:`, `light:`, `world:`
   - `camera-agent`: `cam:`, `render:`
3. **Frame-Bound GUI Camera Orbits & Typed Outcomes**: The `camera-agent` commands orbital viewport view adjustments against fresh frame sidecars, and every turn is classified into explicit typed outcomes: `STATE_VERIFIED` (`VERIFIED`), `VERIFIED_WITH_SPILL`, `NO_VISIBLE_CHANGE`, `STALE_FRAME`, `CLAIM_REFUSED`, and `JOB_ERROR`. Stale frames are detected against frame sidecars before execution.
4. **Off-Display Overlay Composition**: An independent compositor (`swarm/overlay/compositor.py`) overlays per-agent colored cursors and a turn-queue status HUD directly onto the captured display video without injecting helper geometry into the Blender scene.
5. **Live Projection**: `swarm/projection/projector.py` projects live agent status and turn results (best-effort) to OpenSwitchboard (`viewpoint-capsule`) and TaskLight (`context-wall`, companion project in a separate repository) room cards.

## How Google Tools & Gemini Models Were Used

- **Google Antigravity 2 & `agy` CLI**:
  - **Orchestrator**: The orchestrator (an Antigravity 2 session running `gemini-4-carbon-high`) compiled briefs, scheduled waves, and arbitrated quality gates (in `swarm/CONTRACT.md`, `director-agent` is the HI-control broker role).
  - **Autonomous Code Generation**: All claimed code under `swarm/` was written by Gemini sessions via headless `agy --output-format stream-json` after `08:59 CET` [CITED] on Saturday 14 Nov 2026 with logged proofs [CITED: `PROVENANCE.md`, `swarm/docs/PROOF.md`].
  - **Model Specialization**:
    - `gemini-4-hydrogen-high`: `H0` (`swarm/CONTRACT.md`), `H1` (`swarm/broker/`), `H2` (`swarm/agents/`), repair waves `H1_r2`..`H4_r2`, `H6_r1`..`H6_r2`, and all `4` live Blender agents (`modeler-agent`, `props-agent`, `look-agent`, `camera-agent`).
    - `gemini-4-carbon-high`: `H3` (v0, `swarm/overlay/`) and `H5` (`swarm/viewer/`, v0 + `r1` + `r2`, `17/17` tests passing [MEASURED]).
    - `gemini-3.8-flash-high`: `H4` (v0, `r1`, `swarm/projection/`) and `H6` (v0).
- **Google Cloud Run**: Target deployment platform (`[TARGET]` until operator `deploy_receipt.txt` exists) for the hosted replay page with a server-side Gemini planner call (`gateway/`) and the standalone swarm replay viewer (`swarm/viewer/`).

## Two-Track Transparency & Honest Boundaries

ViewPoint Swarm strictly separates hackathon-claimed Gemini-generated code (`swarm/`, Track H) from declared non-Gemini reference implementations and cross-family reviews (`product/`, Track P authored by Claude before `13:59 CET`, plus post-`13:59 CET` `Sol 6.1` reviews via `swarm`).

Licence: ViewPoint is licensed under PolyForm Strict 1.0.0 (source-available, not open source); the base MetaViewer v0.2.6 keeps its AGPL-3.0-only license.

### Mandatory Trust & Process Boundary Statement

> "Browser capsules (measured 13 Nov) run as containers with their own display. Today's Blender 4.0.2 swarm capsule runs as host processes inside `systemd-run` memory-capped scopes on a private Xvfb display (`:2`) under one shared UID: per-run bridge nonces and prefix claims catch accidental cross-writes, stale frames, and buggy agents, and are not an OS security boundary against a hostile same-UID process. Per-agent UIDs/containers are roadmap work."

### Projection Guarantee

"Projection to OpenSwitchboard (`viewpoint-capsule`) and TaskLight (`context-wall`) is best-effort; `swarm/run/events.jsonl` is the authoritative local record of truth, audited at end-of-run by `projector.py --finalize` (via `reconciler.py`)."

## Measured Live Takes (Blender 4.0.2)

### Wave 2 Flagship 4-Agent Rehearsal (`take2_w2_4agents`)
- `4` `gemini-4-hydrogen-high` agents (`modeler-agent`, `props-agent`, `look-agent`, `camera-agent`) all exited `rc=0` [MEASURED].
- `184` broker events [MEASURED]; `13` verified turns (`11` `STATE_VERIFIED` + `2` `VERIFIED_WITH_SPILL`), `2` failed scripts typed `JOB_ERROR` and retried (`m1`, `l3`), `0` accepted cross-claim jobs, `0` open turns [MEASURED].
- Exported `starforge_sentinel.glb` with `28` meshes (`15` `CHAR_*` + `12` `PROP_*` + `1` factory `Cube`), `3582` vertices, `4000` polygons, `6` materials [MEASURED], plus `3` 1080p Eevee renders (`hero.png`, `turntable.png`, `front.png`) [MEASURED].
- Reconciliation audit (`python3 -m swarm.projection.projector --finalize` / `projector.py --finalize`): `CLEAN` (`0` anomalies, `0` uncertain events) [MEASURED].

### Wave 2 Deterministic Guard Take (`take3_w2_guards`)
- `69` broker events [MEASURED]; exercised `CLAIM_REFUSED` (`write_set` outside active `write_prefixes`, `tick=0` unchanged) and `STALE_FRAME` (`3` stale drops -> `parked_after_3_stale_drops` -> camera re-observe -> `VERIFIED_WITH_SPILL` with `rebased=false`) [MEASURED].
- Reconciliation audit (`projector.py --finalize`): `CLEAN` (`0` anomalies, `0` uncertain events) [MEASURED].

### Wave 1 2-Agent Live Gate (`take1_w1_2agents`, commit `04a1eb4` [CITED])
- `100` broker events [MEASURED]; `7` verified turns (`5` `STATE_VERIFIED` + `2` `VERIFIED_WITH_SPILL`), `1` failed script typed `JOB_ERROR` and retried, `0` open turns [MEASURED].
- Exported `starforge_sentinel.glb`: `16` meshes (`15` `CHAR_*` + preserved default `Cube`), `1340` vertices, `2368` polygons [MEASURED].
- Reconciliation audit (`projector.py --finalize`): `CLEAN` (`0` anomalies, `0` uncertain events) [MEASURED].
