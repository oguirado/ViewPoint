# OPENSUPERFICIES: moving the rewindable Blender capsule into program capsules (rw-build §5)

Track P (claude-fable-5-2). Design notes; nothing below was executed inside OpenSuperficies.

| Step | What | Needs | State |
|---|---|---|---|
| 1 | Same broker + bridge inside the program capsule; `RT/ckpt` on a capsule-owned tmpfs/volume. Capsule state = `(image id, scene_epoch, point ring manifest)`. | no root | Not run. The live proof ran on the host under a `vision` job. |
| 2 | `blender` entry in the closed `PROGRAMS` table and a hash-pinned recipe. | builder approval file under `/etc` (operator) | Not done. |
| 3 | Admission mapping: `observe` returns `scene_epoch`; `propose` with an older epoch → `STALE_FRAME`; `wait-verify` → `INPUT_UNCERTAIN` triggers restore(base). | ViewPoint core files: AUSENTE — no inventar | Design only. |

Facts from the live proof that matter for step 1 [MEASURED 2026-11-14, job `vprw4`]:

- In-process restore (`open_mainfile` from the bridge timer, `load_ui=True`) keeps the Blender X client alive across a rewind, so a remote display session of the capsule survives it. Rung A′ (relaunch on the checkpoint) does not, and is not automated.
- A checkpoint of the demo scene is about 0.9 MB (`evidence/nominal/ckpt_manifest.jsonl`); ring of 8 slots.
- Blender must be started with `XDG_CONFIG_HOME` inside the capsule's runtime dir (recent-files write on `open_mainfile`).
- The equality witnesses are capsule-local: digest v2 computed inside Blender, and the vision frame of the viewport region. A layer snapshot (`docker commit`, rung B) would cover files only, not process memory.

Not covered: nested capsules (design details AUSENTE — no inventar), CRIU / MicroVM snapshots (rungs C, D: out of plan).
