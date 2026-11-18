# Domain quality profiles

Profiles make the acceptance objective explicit. They are reusable policy data;
the adapter, source identity, expanded obligations and retained observations decide
what can actually be verified. The film profile prioritises shot quality and has
null asset budgets. The game profile applies asset budgets to the same source.
Film assets are not game assets: a failed game budget does not establish a film
quality defect.

| Profile | Objective | Governing policy | Capture request |
|---|---|---|---|
| `film-animation` | Quality per shot of an animated series | Feet clear of the floor surface, planted feet that do not slide, per-joint rotation without one-frame pops, declared props held by their bone, authored camera, nonblank Cycles frames, source set for a Cycles render; advisory noise, interpenetration, GPU and denoiser capability | CPU Cycles, 48 samples [MEASURED reference], 96x72, 12 frames per shot [TARGET] |
| `game-realtime` | Declared real-time asset costs | Per-object/scene triangles, texture dimensions, independent replay receipts, nonblank frames | Workbench, 1 sample [TARGET] |
| `hyperreal-still` | Physical fidelity of a still | Evaluated world bbox extents, observable light energy/colour, AgX transform and matching look, source settings, image noise | CPU Cycles, 96 samples [TARGET] |
| `simulation` | MuJoCo simulation reproducibility | Independent complete receipts, simulation contact pairs, decoded step effects above a target noise floor | EGL, runtime renderer; samples field is descriptive [TARGET] |

```sh
python3 -B -m vqa3d profile list
python3 -B -m vqa3d profile show film-animation
python3 -B -m vqa3d profile check --profile film-animation --source /path/to/scene.blend
python3 -B -m vqa3d profile check --profile game-realtime --adapter blender --source /path/to/scene.blend --frames 1,345,1
python3 -B -m vqa3d profile check --profile simulation --adapter mujoco --record-trace /path/to/profile-trace.json
python3 -B -m vqa3d check --adapter mujoco --profile simulation --scenario vqa3d/scenarios/profile_simulation.json
```

Use the resource scope specified by the campaign for live execution. The paths
above are placeholders, not claims that those files exist. The simulation command
requires the independently supplied MuJoCo interpreter and EGL environment.

## Policy and expansion contract

`profile.py` exports `load`, `validate`, `resolve`, `expand`, `adapter_kwargs`,
`apply_defaults`, `identity`, and `run`. Schema `vqa3d.profile/v1` rejects unknown
top-level and nested keys, duplicate JSON keys, floats and booleans. All numeric
leaves, including capture settings and frame bounds, need JSON-Pointer keys in
`provenance`. Sources are `measured:<log>`, `cited:<evidence-id>`, or `target`.
Policy numeric values below are [TARGET] unless their provenance says measured.
No numeric budget is attributed to a paper that only motivates a check.

`extends` is a single profile ID in the same directory. Cycles and missing parents
refuse. Objects merge recursively; lists replace; null clears. Resolution removes
`extends`; identity is the SHA-256 of the resolved canonical profile. Receipts keep
schema `vqa3d.receipt/v1`, adding `{profile:{id,sha256}}` before hashing. Existing
non-profile receipts keep their old structure. Explicit scenario parameters win
over defaults, without mutating caller data. Budget checks consume `budgets`;
conflicting template budget parameters refuse.

Each check has a stable `id`, `kind`, `select`, `params`, `frames`, `severity`, and
one-line `why`. A nonempty selector requires a declared object type (`MESH`,
`ARMATURE`, `LIGHT`, `CAMERA`, `EMPTY`), with optional `name_regex` and
`parent_bone_regex`. The latter requires actual bone parenting. An empty selector
means scene scope; it is used for runtime capabilities, images, whole-scene budgets
and MuJoCo simulation state. It never means an empty object match. Object and bone
expansion sorts names, escapes JSON Pointer components, and supports `{object}`,
`{bone}`, `{parent}` and `{parent_bone}` in parameter paths. Every bone gets its own
rotation check. A missing match yields `CANNOT_SEE selector_empty:<check id>`.

Film checks `feet_floor`, `foot_slide`, `bone_rotation_continuity` and `attachment`
are profile-local oracles described in the `film-animation` section. Feet checks
take an empty selector and resolve their own feet and floor; `attachment` needs a
`MESH` selector with `parent_bone_regex`; regex parameters may not contain braces,
which are reserved for placeholders. `render_settings` accepts `enforce`, the list
of settings that gate; it defaults to every declared setting, and `look` may be
omitted from `render`. `capture.max_frames` (optional) bounds how many frames the
pixel checks render: evenly spaced, both ends included, with
`frames_sampled=<n>_of_<m>` at the start of the reason. When a profile contains a
`scene_camera` check, pixels from the adapter's fallback camera report
`CANNOT_SEE capture_camera_not_scene:fallback` and never qualify the shot.

Film frame bounds use the declared stride, action keys and adjacent witnesses
by default (see Production sampling and declarations). A bounded source query supplies the original frame bounds, selected
camera and render settings where the base Blender adapter omits them. It disables
autoexec, never saves the source, hashes source bytes and retains its process log.
It does not synthesize missing mesh, image, light or bone state. If this query is
unavailable, missing frame bounds remain `CANNOT_SEE state_path_absent:/frame_start`;
an explicit `--frames` declaration can supply the range. Explicitly sampled ranges
qualify only that declared coverage. Continuity refuses nonadjacent sampling and
single-frame coverage. The legacy `max_delta_over_frames` path on a bone
`rotation_mdeg` leaf keeps its Euler shortest-arc proxy; the film profile now uses
`bone_rotation_continuity` on quaternions instead.

`range_over_frames`, `max_delta_over_frames`, `within_distance`, `capability`, and
pixel kinds consume core checks. Other kinds are profile-local oracles, leaving
the core obligation contract unchanged. `tri_budget` requires every evaluated mesh
count; `texture_budget` requires a complete image inventory (an explicit empty
inventory can pass); `scale_range` checks all world bbox extents; `light_state`
requires energy and integer colour; `scene_camera` uses the source's actual camera,
without accepting the adapter fallback. `render_settings` compares original source
settings with declared policy (samples are a minimum); it does not rewrite the
source or scratch rendering.
`contacts` steps the MuJoCo simulation before reading contact counts/pairs.

Probe and `requires` produce the first receipt, before metadata and expanded
checks. State checks use observed state and samples independently of capture.
Capture controls require named constructor parameters `capture_engine` and
`capture_samples`; `**kwargs` alone is insufficient. Unsupported controls yield
`CANNOT_SEE capture_engine_unsupported` for pixels and capture-dependent replay.
Any missing state leaf yields `CANNOT_SEE state_path_absent:<path>`. No missing
count is treated as zero. Requested capability absence prevents dependent pixel
qualification while independent state checks continue.

Each emitted receipt preserves its actual verdict. Only gate checks determine the
worst gate verdict. Failed/unobservable advisory checks can elevate PASS to
ADVISORY, and cannot turn it into FAIL, CANNOT_SEE or ERROR. An empty gate set is
CANNOT_SEE. The CLI emits canonical JSON per check, then
`vqa3d.profile-summary/v1`. It uses the existing verdict-command return codes.

`--record-trace` writes `vqa3d.profile-trace/v1`: exact profile identity, probe,
source state/metadata, expanded scenario, samples/actions/observations, receipts,
summary and its digest. PNGs stay in RAM until encoded inside the JSON trace;
there are no standalone frame files. Independent replay runs retain core
`vqa3d.trace/v1` traces, complete observation references and identical-receipt
comparison. Matching failures or missing pixels cannot qualify replay. These
claims apply to the measured host/runtime and model, not cross-platform physics.

## Evidence and calibration boundary

| Evidence ID or measured reference | Check shaped | Limit |
|---|---|---|
| `FA-0030` | Claim-specific pixels, source/camera binding, missing evidence remains unresolved | Does not calibrate any production image threshold |
| `FA-0031` | Explicit runtime and target capability requirements | Enumeration is not a production render measurement |
| `VG-0054` | Feet/contact placement over motion | No universal floor margin |
| `VG-0051` | Per-bone motion and bounded rotation errors | Does not calibrate the pop threshold |
| `VG-GFX-0040` | Colour pipeline and noise caveats | Local outliers are not a denoising quality oracle |
| `VG-GFX-0042` | Declared real-time rendering costs | Game triangle limits are target policy |
| `VG-GFX-0240` | Texture dimension and memory concerns | Texture dimension ceiling is target policy |
| `VG-GFX-0252` | Physical material/scale sanity motivation | Scale extent limits are target policy |
| `VG-GFX-0039` | Explicit light energy and colour state | No photometric sufficiency claim |
| `VG-TOOL-0253` | Interpenetration limitation | Self-intersection implementation is absent in that evidence |
| `VG-TOOL-0235` | Observable simulation contact geometry | Not physical-world validation |
| `VG-MATH-0012`, `VG-MATH-0013` | Independent deterministic replay | Same-host comparison is not cross-platform determinism |
| `live/F3_logs/final_film_*.log`, `live/F3_logs/inspect_*.json` | Film anchors of take 1827: floor-relative foot clearance, planted-run drift, per-joint step and detour, mallet reach and slip | Anchors of three approved or rejected files, not universal film standards |
| `feedback/scenarios_f3/` (take 0448 actions) | One-frame pop size on a reconstruction of the NLA blend defect, and the clean sequence of the same actions | A reconstruction from the real actions; the original defective file was never saved |
| `live/F3_logs/noise_probe_metrics.log` | Local-outlier metric at 8, 48 and 512 samples on the capture size | Scene detail alone gives 31540 to 46731 ppm at 512 samples; the metric is a relative proxy for this take |
| `PLAYBOOK_VIDEO_A_3D.md` section 7 | 48 samples as the clean reference; 8 samples rejected for fireflies | Measured at 723x1286 on the GPU with indirect clamp 3, not on this capture |

The sample setting does not assert convergence or render minutes per frame.

## Checks and numeric provenance

Every row below is generated directly from the canonical policy. A `target` value
is uncalibrated [TARGET]; the measured foot floor is [MEASURED] in its named log.
All limits use integer units from their field names. Every numeric policy leaf,
including descriptive settings, is listed so calibration gaps remain inspectable.

### `film-animation`

Quality per shot for an animated series: floor contact, planted feet, continuous joints, held props, the authored camera and qualified production captures.

The objective is the quality of one shot of an animated series. There is no
triangle or texture budget. Every gate below states a defect a viewer can see.

| Check | Kind | Severity | Why |
|---|---|---|---|
| `feet-floor` | `feet_floor` | gate | VG-0054 and take 1827: no foot geometry may go below the floor surface at any frame of the shot. |
| `foot-slide` | `foot_slide` | gate | VG-0054 and take 1827 audit: a foot resting on the floor must not be dragged while it stays planted. |
| `bone-continuity` | `bone_rotation_continuity` | gate | VG-0051 and take 0448: every joint rotation must be free of one-frame pops on adjacent frames. |
| `attachments` | `attachment` | gate | Take 1827 mallet: a declared prop must follow its bone; grip props must also reach the grip target. |
| `scene-camera` | `scene_camera` | gate | FA-0030: a fallback view cannot qualify the authored shot camera. |
| `nonblank` | `frame_not_blank` | gate | FA-0030: decoded Cycles pixels through the shot camera on declared sample frames; unavailable captures stay unresolved. |
| `render-settings` | `render_settings` | gate | FA-0031 and PLAYBOOK section 7: the source must be set for a Cycles render at no fewer samples than the clean reference. |
| `noise` | `frame_noise_below` | advisory | VG-GFX-0040: integer local-outlier proxy on a small capture; texture edges are not a production noise oracle. |
| `interpenetration` | `interpenetration` | advisory | VG-TOOL-0253 explicitly lacks self-intersection detection; report absent support. |
| `gpu-render` | `capability` | advisory | FA-0031: production GPU rendering counts only when a probe render succeeds on this runtime. |
| `denoiser` | `capability` | advisory | FA-0031: denoised production pixels need a denoiser present in this Blender build. |

#### What each check measures

**`feet-floor`** (gate). For every foot and sampled frame, clearance is the lowest
foot geometry minus the top of the floor, both evaluated in world space.
`clearance < min_clearance_um` is `FAIL foot_below_floor` with the frame. The
threshold 0 [TARGET] is the definition of penetration, not a calibrated margin.
The Blender path resolves the surface under each foot by raycast at each
sample, with declared floor fallback. See Production sampling and declarations
for explicit object, collection and plane controls. The earlier single-floor
resolver remains for legacy recorded adapters; Blender never selects its floor
by a name regex. Missing contact support cannot clear a Blender gate.

Feet are resolved in this order, and every rig stays visible:
1. `mesh`: a non-skinned mesh with `vqa3d_role = "foot"` or a name matching
   `foot_regex` (foot, feet, shoe, boot, toe). Its bone is its `BONE` parent.
2. `skin`: an armature-deformed mesh exposes each vertex group that matches
   `foot_regex`, bounded over the vertices weighted at least 0.5 to it. This is
   the rule for a unified character mesh.
3. `bone`: a matching bone that no mesh or skin part covers. Joints are not
   soles: a bone-only foot can give `FAIL` (a joint below the floor) but never
   `PASS`; a clear one reports `CANNOT_SEE sole_geometry_unresolved`.

Parts whose bones form a chain (`foot.L` then `toe.L`) are one foot. No foot at
all is `CANNOT_SEE selector_empty:feet-floor`.

**`foot-slide`** (gate). A foot is planted on a sample when its clearance is at
most `plant_max_um` and its velocity is within the declared speed ceiling. For each run of two or more consecutive planted frames, the
oracle takes the points at k/8 along every bone segment of the foot (ankle to
toe) and measures how far each travels horizontally from where it was when the
run began. The drift of the run is the smallest of those maxima: a foot that
pivots or rolls about a point of its ankle-toe line does not slide, a dragged
foot does. `drift > max_slide_um` is `FAIL foot_slide` with the frame range.
A foot that is never planted is `CANNOT_SEE foot_never_planted`; a foot without
a bone is `CANNOT_SEE foot_bone_unresolved`. Sparse runs describe only sampled contact.
Limits: a pivot centred off the ankle-toe line counts as drift (radius times
angle), and a deliberate slide (a moonwalk) is a `FAIL` here by design: declare
it by lowering this check to advisory in a child profile.

**`bone-continuity`** (gate, one receipt per bone). It reads `quat_local_ppm`, the
rotation of each pose bone relative to its parent, on measured adjacent frame pairs.
The angle between two frames is `2*atan2(|v|, |w|)` of `conj(a)*b`, in integer
millidegrees; it does not depend on Euler order or quaternion sign.
- `max_delta_mdeg` caps the rotation in one frame.
- `max_detour_mdeg` caps `angle(f-1,f) + angle(f,f+1) - angle(f-1,f+1)`. It is
  zero for motion that keeps turning the same way and twice the excursion for a
  pose that jumps away for one frame and returns. This is the signature of the
  one-frame rest-pose dips that NLA blending produced in take 0448.

A fast swing passes; a pop fails with `bone_rotation_pop:<path>:frame=<f>`.
Limits: a pop smaller than half the detour ceiling passes; a kick that extends
and retracts within two frames looks the same as a pop on that one joint;
bone translation pops are not measured.

**`attachments`** (gate). An attachment is a `MESH` parented to a bone
(`parent_type = BONE`) that is declared a prop:
1. if the object has `vqa3d_role`, it is a prop only when the value is `"prop"`;
2. otherwise its name or one of its collections must match `prop_regex`
   (`prop` or `props` as a separate word: `PROP_mallet`, collection `PROP`).

Structure alone cannot be the rule: on these scenes heads, gloves and shoes are
rigid meshes parented to bones exactly like the mallet. Bone-parented meshes
with no declared prop give `CANNOT_SEE selector_empty:attachments:undeclared_bone_parented_meshes=<n>`.
For each prop the oracle measures bone-relative position drift on the sampled
frames (`max_drift_um`). Declared `in_grip` props additionally check the distance
to the bone tail or grip point (`max_um`); undeclared bone-parented props use
`rigid_follow`. Props held by constraints are not seen. The declarations and
coordinate system are specified below.

**`scene-camera`** (gate). The source must have its own scene camera. The
adapter's fallback camera never counts, and with this check present no pixel
check accepts a fallback capture.

**`nonblank`** (gate) and **`noise`** (advisory). CPU Cycles at the declared
samples, 96x72, no denoiser, through the scene camera, on `max_frames` evenly
spaced frames. `nonblank` proves the shot renders to a non-uniform image.
`noise` counts pixels whose luma differs from their 3x3 median by more than 32.
On this capture size scene detail triggers it too, so it is a relative proxy.

**`render-settings`** (gate). The source must be set to `CYCLES`, at least the
declared samples, and the declared view transform. Look, motion blur,
resolution percentage, clamps, adaptive sampling, denoising and device are
written into the reason as `(recorded)` shot choices and do not gate.

**`gpu-render`** and **`denoiser`** (advisory). They pass only when the runtime
probe renders on CUDA and lists OpenImageDenoise. On the GB10 with Blender 4.0.2
both are `CANNOT_SEE`: the CUDA `sm_121` kernel fails to load and the build has
no OIDN. No production-pixel or GPU claim is made by this profile on this host.

**`interpenetration`** (advisory). No adapter reports intersecting pairs yet, so
it is `CANNOT_SEE state_path_absent:/interpenetration_pairs`.

#### Thresholds and their anchors

Every number is `target` unless a file measured that exact number. The anchors
are what take 1827 and take 0448 measured; they bound the targets, they are not
universal truths. Logs are under `vqa3d-campaign/live/F3_logs/`.

| Threshold | Value | Measured anchors [MEASURED] |
|---|---|---|
| `min_clearance_um` | 0 [TARGET] | `motion.blend` -36098 (left, frame 98) and -6920 (right, frame 166): rejected. `motion_v2.blend` 4500 both feet. `final_scene_v2.blend` 3000 (left) and 263 (right, frame 175). |
| `plant_max_um` | 6000 [TARGET] | Resting clearance 4500 (`motion_v2`) and 3000 (`final_scene_v2`, left shoe). |
| `max_slide_um` | 40000 [TARGET] | `motion_v2` 0 on every planted run. `motion` 533242 (left, frames 93-102) and 152140 (right). `final_scene_v2` 0 (left) and 267797 (right, frames 9-18). |
| `max_delta_mdeg` | 90000 [TARGET] | Largest clean step 21414 (take 1827) and 52862 (take 0448 clean sequence). Pop reconstruction 110000. |
| `max_detour_mdeg` | 60000 [TARGET] | Largest clean detour 8792 (take 1827) and 40000 (take 0448 clean sequence, a kick). Pop reconstruction 144000 to 212000 on four arm bones. |
| `max_um` | 75000 [TARGET] | Mallet origin to `hand.R` tail 54527 to 54529 over 345 frames. |
| `max_drift_um` | 1000 [TARGET] | Mallet 2. |
| `max_outlier_ppm` | 95000 [TARGET] | 48 samples 47454 to 70892 over 345 frames; 8 samples 120660 to 135707; 512 samples 31540 to 46731 (three frames). |
| `capture.samples`, `render.samples` | 48 [MEASURED] | PLAYBOOK section 7: 48 samples clean, 8 samples rejected. |
| `capture.max_frames` | 12 [TARGET] | One 96x72 frame at 48 samples took 0.24 s of render time on the CPU. |

| Numeric path | Value | Provenance |
|---|---|---|
| `/capture/max_frames` | 12 [TARGET] | `target` |
| `/capture/max_h` | 72 [TARGET] | `target` |
| `/capture/max_w` | 96 [TARGET] | `target` |
| `/capture/samples` | 48 [MEASURED] | `measured:PLAYBOOK_VIDEO_A_3D/PLAYBOOK_VIDEO_A_3D.md` |
| `/checks/8/params/max_pairs` | 0 [TARGET] | `target` |
| `/defaults/attachment/max_drift_um` | 1000 [TARGET] | `target` |
| `/defaults/attachment/max_um` | 75000 [TARGET] | `target` |
| `/defaults/bone_rotation_continuity/max_delta_mdeg` | 90000 [TARGET] | `target` |
| `/defaults/bone_rotation_continuity/max_detour_mdeg` | 60000 [TARGET] | `target` |
| `/defaults/feet_floor/min_clearance_um` | 0 [TARGET] | `target` |
| `/defaults/foot_slide/max_slide_um` | 40000 [TARGET] | `target` |
| `/defaults/foot_slide/plant_max_um` | 6000 [TARGET] | `target` |
| `/defaults/frame_change/min_changed_ppm` | 1000 [TARGET] | `target` |
| `/defaults/frame_change/pixel_tolerance` | 3 [TARGET] | `target` |
| `/defaults/frame_noise_below/max_outlier_ppm` | 95000 [TARGET] | `target` |
| `/defaults/frame_noise_below/threshold` | 32 [TARGET] | `target` |
| `/defaults/frame_not_blank/max_uniform_ppm` | 995000 [TARGET] | `target` |
| `/render/motion_blur` | 1 [TARGET] | `target` |
| `/render/resolution_pct` | 100 [TARGET] | `target` |
| `/render/samples` | 48 [MEASURED] | `measured:PLAYBOOK_VIDEO_A_3D/PLAYBOOK_VIDEO_A_3D.md` |

#### Proposals, not implemented

- Pose pop across bones: in the pop reconstruction 15 bones exceed a 15 degree
  detour on the defect frames, against 3 in the clean sequence. A per-armature
  count would catch smaller whole-pose dips.
- Seed-pair noise: pixels differing by more than 32 between two seeds are 65682
  to 72482 ppm at 48 samples and 245949 to 251157 ppm at 8 samples, and scene
  detail cannot trigger it. It needs a capture seed control on the adapter.
- Bone translation continuity, prop to hand geometry distance, floating feet,
  character in frame (`view_ppm`), and arcs or spacing: not measured here.

### `game-realtime`

Bound scene and object asset costs for a declared real-time target, then test repeatability.

| Check | Kind | Severity | Why |
|---|---|---|---|
| `triangles` | `tri_budget` | gate | VG-GFX-0042: explicit target asset budgets; no shipped-platform threshold is claimed. |
| `textures` | `texture_budget` | gate | VG-GFX-0240: inspect resident texture dimensions, with an uncalibrated target ceiling. |
| `determinism` | `replay_identical` | gate | VG-MATH-0012: independent runs must retain identical complete receipts. |
| `nonblank` | `frame_not_blank` | gate | FA-0030: changed metadata cannot establish nonblank rendered feedback. |

| Numeric path | Value | Provenance |
|---|---|---|
| `/budgets/max_object_tris` | 100000 [TARGET] | `target` |
| `/budgets/max_scene_tris` | 1000000 [TARGET] | `target` |
| `/budgets/max_texture_px` | 4096 [TARGET] | `target` |
| `/capture/max_h` | 72 [TARGET] | `target` |
| `/capture/max_w` | 96 [TARGET] | `target` |
| `/capture/samples` | 1 [TARGET] | `target` |
| `/checks/2/params/steps` | 1 [TARGET] | `target` |
| `/defaults/frame_not_blank/max_uniform_ppm` | 995000 [TARGET] | `target` |
| `/render/motion_blur` | 0 [TARGET] | `target` |
| `/render/resolution_pct` | 100 [TARGET] | `target` |
| `/render/samples` | 1 [TARGET] | `target` |

### `hyperreal-still`

Physical fidelity of a still: world scale, explicit lights, colour management and image noise.

| Check | Kind | Severity | Why |
|---|---|---|---|
| `scale` | `scale_range` | gate | VG-GFX-0252: scale sanity uses evaluated world bounds, with declared target extents. |
| `lights` | `light_state` | gate | VG-GFX-0039: light energy and colour state must be observable; presence alone is insufficient. |
| `colour` | `capability` | gate | VG-GFX-0040: qualify the named colour transform and its matching look. |
| `render-settings` | `render_settings` | gate | FA-0031: source settings must agree with the declared still policy. |
| `noise` | `frame_noise_below` | gate | VG-GFX-0040: qualify decoded pixels at declared Cycles samples; local outliers remain a proxy. |

| Numeric path | Value | Provenance |
|---|---|---|
| `/capture/max_h` | 72 [TARGET] | `target` |
| `/capture/max_w` | 96 [TARGET] | `target` |
| `/capture/samples` | 96 [TARGET] | `target` |
| `/checks/0/params/max_extent_um` | 100000000 [TARGET] | `target` |
| `/checks/0/params/min_extent_um` | 100 [TARGET] | `target` |
| `/checks/1/params/min_energy_mw` | 1 [TARGET] | `target` |
| `/defaults/frame_noise_below/max_outlier_ppm` | 5000 [TARGET] | `target` |
| `/defaults/frame_noise_below/threshold` | 32 [TARGET] | `target` |
| `/render/motion_blur` | 0 [TARGET] | `target` |
| `/render/resolution_pct` | 100 [TARGET] | `target` |
| `/render/samples` | 96 [TARGET] | `target` |

### `simulation`

Reproducibility in a MuJoCo simulation: independent replay receipts, contacts and visible step effects.

| Check | Kind | Severity | Why |
|---|---|---|---|
| `identical-receipts` | `replay_identical` | gate | VG-MATH-0012, VG-MATH-0013: compare independent MuJoCo simulation runs on this host and runtime. |
| `frame-change` | `frame_change` | gate | FA-0030: require changed decoded pixels above an explicit target sensor-noise floor in the MuJoCo simulation. |
| `contacts` | `contacts` | gate | VG-TOOL-0235: observe simulation contact pairs after explicit settling; this is not physical-world validation. |

| Numeric path | Value | Provenance |
|---|---|---|
| `/capture/max_h` | 72 [TARGET] | `target` |
| `/capture/max_w` | 96 [TARGET] | `target` |
| `/capture/samples` | 1 [TARGET] | `target` |
| `/checks/0/params/steps` | 120 [TARGET] | `target` |
| `/checks/1/frames/0` | 0 [TARGET] | `target` |
| `/checks/1/frames/1` | 1 [TARGET] | `target` |
| `/checks/1/frames/2` | 1 [TARGET] | `target` |
| `/checks/1/params/steps` | 120 [TARGET] | `target` |
| `/checks/2/params/min_contacts` | 1 [TARGET] | `target` |
| `/checks/2/params/steps` | 240 [TARGET] | `target` |
| `/defaults/frame_change/min_changed_ppm` | 1000 [TARGET] | `target` |
| `/defaults/frame_change/pixel_tolerance` | 3 [TARGET] | `target` |
| `/render/motion_blur` | 0 [TARGET] | `target` |
| `/render/resolution_pct` | 100 [TARGET] | `target` |
| `/render/samples` | 1 [TARGET] | `target` |

## AUSENTE — no inventar

Production film papers, approved universal shot thresholds, calibrated scale and
triangle budgets, production render timing, photometric correctness, exact
interpenetration, silhouette and deformation quality, and cross-platform MuJoCo
simulation determinism are AUSENTE.

For `film-animation` specifically, AUSENTE on this host and corpus:
- GPU rendering and denoised pixels (CUDA `sm_121` kernel fails, no OIDN);
- production-resolution pixels and render minutes per frame: the capture is
  96x72 and proves only that the shot renders through its camera;
- a noise threshold that holds across scenes: the outlier metric has a scene
  detail floor, and no clamp effect was measurable at this size;
- any threshold from a film paper: every film number is a target bounded by the
  measured anchors above, or the measured sample count;
- sole geometry for a rig that has only foot bones;
- props attached by constraints, uneven floors, several floors;
- the original take 0448 defect file: the pop evidence is a reconstruction from
  the real actions.

The small capture proxy does not certify final-resolution quality. The
`hyperreal-still` noise gate (5000 ppm) is below the detail floor measured on
take 1827 and has not been calibrated.

The repository ignore rule `profiles/` also covers the policy and profile-trace
directories. Integration must explicitly include those assets; this lane does not
change `.gitignore` or stage/commit files.

The mandate is limited to the supplied component, evidence pack and recorded C3 and F3 runs.

## Production sampling and declarations

The film policy declares `sampling.stride = 6` [TARGET] and a total child budget
of `600 s` [TARGET], with JSON-Pointer provenance. Samples include the scene ends,
action keys, mapped NLA keys and strip ends, and the adjacent frames around each
anchor. `--frames start,end,step` overrides this policy. Receipts retain the actual
sample list and `frame_sampling`; continuity compares only adjacent measured
frames. A sampled PASS describes those samples, never an exhaustive shot audit.
FA-0031 motivates retaining that coverage boundary; VG-0052 motivates preserving
authored event frames. CPU samples for all shot checks share one batch in one
Blender session. Film captures and capability probes reuse that same child.
Independent game replay still requires separate runs by its own contract.

`profile check --gates-only` omits advisory checks. `--frame production.png`
supplies the noise oracle with production pixels and promotes that check to a
gate; otherwise preview noise reports ADVISORY with its measured proxy value.
A production frame is judged against `production_max_outlier_ppm`, not the
preview limit, because a 96x72 preview counts texture edges as outliers and its
limit passes grainy full-size frames. film-animation declares 6000 ppm [TARGET].
On 11 production frames of the nightclub take, labelled for grain by the
orchestrator and the operator, it classified all 11 correctly
(campaign `feedback/RETEST_0752_315b7a8c_video-montaje.md`) [MEASURED]:
- publishable frames scored 6 to 1860 ppm;
- unpublishable frames scored 14763 to 20159 ppm, raw and lightly denoised
  frames alike.

That is one take and one scene; other scenes may need their own limit.
hyperreal-still keeps 5000 ppm [TARGET]. A profile without the key reports
CANNOT_SEE `production_noise_limit_undeclared` for `--frame`.

The outlier scale depends on frame size. The same frame scored 618 ppm at
540x960 and 6 ppm after an upscale to 1080x1920, so a grainy frame rescaled
before measuring can pass. The profile therefore declares
`production_calibrated_px`, the pixel count of the frames the limit was
calibrated on: 518400 for film-animation, which is 540x960 [MEASURED]. A FAIL
stands at any size. A PASS on a frame of another pixel count, or under a profile
without the key (hyperreal-still), becomes ADVISORY
`uncalibrated_size:frame_px=…:calibrated_px=…`, with a suggested action to
measure before any upscale. The rule compares pixel counts only, so pass the
renderer's native output to `--frame`. A size check cannot catch a frame of a
different shape with the same count, or a larger grainy frame downscaled to the
calibrated size, which averages the grain away.
The frame may be grey, grey with alpha, RGB or RGBA, at 8 or 16 bits. It is
reduced to RGB8 by keeping the high byte and dropping alpha without compositing,
which gives the same pixels as `ffmpeg -pix_fmt rgb24`. The receipt's
`production_frame` records the source hash, size, colour type, bit depth,
whether alpha was dropped, and the calibrated pixel count. Palette or interlaced PNGs give ERROR
`frame_png_unsupported` with the ffmpeg command that converts them.
A noise warning or failure carries `suggested_action`, also gathered in the
summary's `suggested_actions`. The text depends on the denoisers the probe
listed: with none, it says to expect visible grain and to raise samples or plan
a post-render denoise.
The existing threshold provenance is retained. `--nice 10 --cpus 0-15` [TARGET]
controls the child. Nice values below `10` [TARGET] are refused. Summaries retain
`total_wall_ms`, shared `timings.batch_sample_ms`, and each check's `wall_ms`.
Shared sampling time is accounted separately from oracle evaluation time.

Feet use per-frame evaluated downward raycasts: a 3 x 3 grid at the quarter
points of the foot's world bounds, starting at the upper foot sample (sole
bounds or foot joint) to expose shallow sole penetration. The floor is the
highest surface any of those rays reaches, so a joint between paving stones
under the foot centre does not report the sub-base below it. The clearance
uses the lowest evaluated sole geometry. Meshes that belong to any character
(parented under or deformed by an armature) are skipped, including repeated ray
hits, unless they are declared as a floor. VG-PHY-0047 informs the explicit
query filter. If no ray resolves a surface, only declared floor
objects/collections whose XY bounds contain the sample can supply the highest
planar bbox top below it. A floor-shaped name is insufficient in this Blender
path. `floors_used` records frame, object, height and method for every foot.

A frame without a surface under the foot carries no clearance. It is never a
plant and it never hides a measurement: a penetration or a drag measured on
other frames is FAIL (the reason ends with `floor_unresolved_frames=N`). A foot
that is otherwise clear is CANNOT_SEE
`floor_unresolved:<foot>:frames=N_of_M:first=F:resolved_min_clearance_um=…`
until the floor is declared. Clearance and three-dimensional foot velocity both
constrain plant detection. The conservative speed ceiling is
`100000 um/frame` [TARGET]; it is overridable in
`defaults.foot_slide.plant_max_speed_um_per_frame`.

The floor is a fact about one scene, so it is declared for the run, not in a
profile file: `profile check --floor z_um=-2000`, `--floor object=Stage` or
`--floor collection=Ground`. In a `check` scenario the same declaration is the
obligation's `floor` field (`feet_floor`, `foot_slide`). In the .blend it is the
custom property `vqa3d_role = "floor"` on the mesh. Object and collection
declarations filter ray hits; `z_um` replaces geometry with a declared plane
(geometry above that plane is then not consulted). A run declaration outranks a
check parameter and a profile `floor`; each receipt carries
`floor_declaration` with `declared_by`. Take 1827: the motion-stage scenes
stand on a 1.7 m preview floor at `-2000 um` [MEASURED] that one foot leaves on
7 of 345 frames [MEASURED: live/F4_B_motion_v2_default.log]; `--floor z_um=-2000`
extends that plane and the profile stays `film-animation`.

Stride sampling is followed by one contact pass. Every gap between samples is
filled when a foot is at, or within reach of, its floor (reach = the per-frame
motion seen by an adjacent witness times the gap) and the foot moves across the
gap by more than `plant_max_um`. Resting feet and feet out of reach add nothing.
`timings.refined_frames` reports the added frames; all shot checks then share
the larger sample. A planted run continues across a remaining gap only when the
foot holds the same pose on both sides; otherwise the run ends there, so a step
taken on unsampled frames is not read as a drag. Joint continuity still judges
only adjacent sampled frames: a one-frame pop between two strides, away from
any action key and any moving contact, is not seen at the default stride
(`--frames start,end,1` measures every frame).

Attachment modes are selected by, in order: a run declaration
(`profile check --attach OBJECT=in_grip|rigid_follow`, repeatable), a profile
`attachment.mapping` entry, the object's `vqa3d_attach` (`rigid` or `grip`) or
`vqa3d_role` (`rigid_follow` or `in_grip`), then the bone that carries it:
`attachment.grip_bone_regex` (film: hand, palm, finger, thumb, grip) makes an
undeclared prop on such a bone `in_grip`. Any other bone-parented prop defaults
to `rigid_follow`: compare its positions in the parent bone's evaluated
coordinate system. `in_grip` also checks distance to the bone tail, or to a
declared `grip_point_um` in bone coordinates. Mapping entries may be a mode
string or `{mode: "in_grip", grip_point_um: [x,y,z]}`. A Blender
`vqa3d_grip_point` property expresses the same point in metres. Shipped
profiles name no scene objects. Receipts carry `attachment_mode` and
`attachment_declared_by`. VG-PHY-0049 informs the distinction between an
attachment target and the prop origin; it supplies no acceptance threshold for
this host.

`--blender-env KEY=VALUE` is repeatable on `check`, `profile check` and `probe`;
profile `render.env` provides the same declared mapping, with CLI values taking
precedence. These values overlay an otherwise empty environment. Loader and
Python injection keys are refused. Receipts and probes retain each key and its
value SHA-256; path values may also be recorded as paths. GPU results remain per
backend and require an actual render. FA-0014 and VG-PLAT-0100 inform keeping
runtime qualification separate from enumeration. No denoiser is implied by a
successful GPU probe. Kernel compilation also needs an available cache or its
own bounded preparation: a timeout remains unresolved capability evidence.
