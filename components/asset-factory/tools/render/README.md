# External OIDN render stage

Run from the asset-factory component root. Configure both binaries explicitly:

```sh
python3 -B -m tools.render.pipeline --scene SCENE.blend --output NEW_DIRECTORY \
  --blender BLENDER_BINARY --oidn OIDN_BINARY --device CPU \
  --frames START END --references FIRST SECOND --samples 96
python3 -B -m tools.render.evaluate --run NEW_DIRECTORY --vqa-root VQA_COMPONENT
```

The pipeline renders linear colour, denoising albedo and denoising normal passes,
calls the external CPU OIDN binary, then applies the source scene's view transform
in Blender. It never saves the source scene. CUDA is refused before launch until
its admission path is qualified; admitted subprocesses explicitly mask CUDA.
The external denoiser version is pinned in `pipeline.py`.
No denoiser or third-party source is vendored. `frame.schema.json` defines each
`af.render-frame/v1` receipt. Binary, scene, dependency, script and artifact hashes
are verified before publication. Frame completion is `OK`, never quality approval.
New directory publication is atomic; existing outputs are refused. Failed scratch
and logs remain in a hidden sibling with a `.failed` suffix for diagnosis.

The default is 96 spp with guided CPU OIDN [MEASURED selection:
`construct-rescate/build/w3/out/rendertune/selection.json`]. This historically passed only the
declared frames 600 and 601 of `final_scene_v3.blend`; it gives no approval to
other frames or scenes. That selection predates the RGB fidelity and execution
identity checks below and needs fresh evidence. `policy_sweep` reauthenticates
measured runs and chooses the cheapest passing declared trial under the current
frozen policy.

`evaluate` rehashes every receipt and artifact, then imports the sibling's noise
function without copying it. Its canonical quality record is written to stdout.
`policy.json` freezes DESIGN_CHOICE thresholds [TARGET]; no corpus calibrates them.
Reference-difference residuals remove real motion and illumination where matching
references exist. Auxiliary-guided block translation provides a separate proxy
elsewhere, with bidirectional consistency and measured coverage. It cannot prove
whether real illumination changes are artificial flicker. Missing detail truth
remains `CANNOT_SEE`. A known failure takes precedence over missing evidence.
Gradient energy includes both a global ratio and the worst feature-bearing tile.
Admission also requires independent RGB MSE against the actual reference: at most
1000 ppm of squared full-scale RGB [TARGET], equivalent to PSNR at least 30 dB
[TARGET]. Global detail must be at least 950000 ppm [TARGET]. Missing RGB evidence
is CANNOT_SEE. Inversion, chroma corruption and displaced content must fail even
when gradient energy and temporal deltas match. All metric reductions are
integer; PFM guides are explicitly quantized before matching.
The primary high-sample reference is OIDN-cleaned, matching the prototype's
detail comparison. Raw high-sample diagnostics are also reported; shared filter
bias remains a limitation rather than independent proof of fine detail.

Frame and run records bind seed, CPU device, resolution, toolchain and the hashed
render job. Execution exports effective settings which must match before
publication. Use `--expected-request-sha256 HASH` on `evaluate` to pin the caller's
request identity. Old receipts without this provenance are CANNOT_SEE; changed
bindings are STALE. This authenticates the declared local execution record, not
an independent appearance or whole-clip approval.

Callers must apply their resource and PID-namespace scope. Every subprocess has
an owned group and timeout; cleanup checks for surviving group members on success,
error and timeout through `asset_factory.proc`. Dynamic image sequences/UDIM/movie dependencies are refused;
unpacked image, volume, sound, font and library dependencies are hash-bound.
The stage does not qualify arbitrary simulation caches or external drivers.
Host pixel evaluation requires NumPy and Pillow; absent decoders return
`CANNOT_SEE`. Rendering uses Blender's bundled NumPy.
