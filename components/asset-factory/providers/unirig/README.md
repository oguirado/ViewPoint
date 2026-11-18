# UniRig rig slot

`provider.json` is blocked: `runnable:false`, `research_only_unverified_runtime`.
No model or Blender run is authorized by these files. Code SPDX metadata says
MIT. The offline source archive, image digest,
hash-locked wheels, ARM extension qualification and GB10 runtime approval remain
AUSENTE. Checkpoint digests identify local bytes; their revision comes from the
download ledger and is not independent upstream authentication.

From the component root, inspect without building or loading models:

```sh
python3 -B -m providers.unirig.build.build --inputs-root OFFLINE_INPUTS --weights-root LOCAL_WEIGHTS
python3 -B -m providers.unirig.gate --input INPUT.glb --policy providers/unirig/gate-policy.json --probes PROBES.json
python3 -B -m providers.unirig.adapter --request REQUEST.json --input-root INPUTS --output-dir NEW_GENERATION
```

The incomplete recipe returns CANNOT_SEE. `build.build.prepare` accepts a complete
recipe with matching frozen code/checkpoint/launch pins, verifies offline bytes
and prepares an exclusive Docker context. It never installs, builds or runs.
Use `--network=none` for any subsequently authorized container build. No weights
enter the image. Upstream skeleton/skin/merge commands in `build/recipe.json` are
documented evidence; their `eval` wrappers and implicit fetching need an audited
offline bridge before approval. The CLI adapter has no approval override.

`adapter.run` requires a trusted caller's explicit approval digest plus bound
build, provider identity and scheduler evidence before invoking a hash-pinned Python
bridge. That proposed bridge accepts `--request`, `--output-dir`, `--weights-root`
and writes `rigged.glb`; a qualified implementation is AUSENTE. The scheduler
owns the model lease. The caller must release it after success or refusal.

`records.schema.json` defines the local policy/probe/approval records. G06 checks
every supported vertex's weights, joint references and contributing rest binds,
then applies exported glTF linear blend skinning against caller-supplied analytic
pose samples. Missing probes is CANNOT_SEE. Float32 GLB numbers stay internal;
receipts are canonical integer-only JSON. Limits in `gate-policy.json` are
DESIGN_CHOICE_UNCALIBRATED. PASS covers those probes, not anatomy or appearance.
Metres and glTF basis are explicit policy declarations, without physical calibration.
Unsupported extra influence sets, morphs, compression and multiple skins return
CANNOT_SEE. Bad weights are refused, never silently normalized. No dual-quaternion
replacement conceals linear-blend collapse. Successful publication is a new
directory containing raw input, rigged GLB, skeleton, gate and result together.

G06 requires a declared active scene with reachable mesh and skin joints. Invalid
scene, mesh or skin indices are errors; absent selected-scene coverage is
CANNOT_SEE. Descendants of selected scene roots count as reachable.

Host execution is restricted to explicitly declared CPU synthetic fixtures;
production host execution and GPU requests are refused before launch. The runner
receives an empty `CUDA_VISIBLE_DEVICES`. Its owned subreaper must prove descendant
cleanup, including escaped sessions, before success; the scheduler still owns
the lease and must not release it without fresh release observation.

Exclusive rename is the publication commit. Staged-directory sync and parent
open happen before it; failures there preserve the target. After commit, parent
sync/close errors retain the committed result. `publish_directory` returns a
durability boolean separately; slot OK confirms publication and does not attest
crash durability. The returned slot result equals the committed `result.json`.
