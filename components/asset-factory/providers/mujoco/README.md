# Rigid-body physics slot

The manifest remains a blocked draft pending dependency rights, production peak
and installed-code provenance review. `tools.physics.native_gate` qualifies the
local runtime on bounded analytic fixtures; this component installs or downloads
nothing. Use a local MuJoCo interpreter matching `tools.physics.worker.RUNTIME_VERSION`.
The slot adapter consumes an authenticated licence decision and requires the
user's explicit `commercial_use`. Scheduler admission remains the caller's job.

`tools/physics/scene.schema.json` defines the rigid-body asset-record view. Each
body binds a visual/CAD source digest, explicit physical units, transform,
mass and full inertia about its centre of mass, plus analytic collision shapes
or closed oriented convex hulls. Mass/inertia use micrograms and micrograms
times squared micrometres. Quaternions are wxyz in ppm. Body and collider positions
are world and body-local respectively; COM and tensor are body-local, linear
velocity is world-space and angular velocity is joint-local. No physical scale,
density or collision shape is inferred from visual triangles. Decomposition
fidelity must be qualified by the collision provider before constructing this
record. The inertia provenance digest binds its declared source; it does not
authenticate an external CAD calculation by itself.

`tools/physics/protocol.schema.json` requires gravity, a separately declared
expected gravity, timestep, solver budgets, contact bounds and a contact-free
analytic fall interval. The runner collects coarse, half-step and replay traces
in owned headless subprocesses with memory limits. It compares compiled mass and
inertia, ordered sample coverage, analytic fall, trajectory and quaternion
convergence, replay identity and sampled contact depths. This version qualifies
rigid-body fall/contact scenes. Controls, deformables and continuous collision
proof are unavailable. Thresholds are TARGET_UNCALIBRATED, not general accuracy
or physical-realism guarantees. Matching seed or replay digests proves neither.

From the component root, run the deterministic host checks with:

```sh
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 taskset -c 0-15 nice -n 15 env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. timeout 300 python3 -B -m unittest discover -s tests -p 'test_w3_physics_*.py'
```

Runtime qualification (all paths supplied by the caller):

```sh
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 taskset -c 0-15 nice -n 15 env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. timeout 300 python3 -B -m tools.physics.api --scene SCENE --protocol PROTOCOL --root EVIDENCE_ROOT --out NEW_DIRECTORY
```

For the native qualification harness, use the same scope and interpreter with
`-m tools.physics.native_gate --out NEW_DIRECTORY`. It retains coarse, half-step
and replay jobs, XML, observations and receipts. Soft contact, initial overlap
and wrong gravity are real solver runs. Other controls are explicitly mutated
copies of observed traces. Every red must match its expected refusal and the
unmodified observations must restore PASS. A separate short fixture compiles
box, plane, sphere, cylinder, convex hull and off-diagonal inertia/COM records.
It checks readback, not the collision fidelity of a visual mesh.

The analytic fixture uses a timestep of 500 microseconds [TARGET], a contact
time constant of 2000 microseconds [TARGET], and the original accuracy guards.
No production asset mass, collision record or runtime ceiling is inferred from
that fixture. Its native report schema is `tools/physics/native-gate.schema.json`.

The slot CLI is `python3 -B -m providers.mujoco.adapter` with `--request`,
`--provider`, `--decision`, `--factory`, `--policy`, `--root`, `--out`.
Each input path is supplied explicitly. Publication uses a new directory and a
receipt commit marker. Refusals preserve existing caller bytes. Missing runtime
or an unavailable oracle is CANNOT_SEE; a physics PASS covers only its stated
qualification scope, never appearance. The bundled slot/provider draft schemas
are used until the registered physics contract lands.
