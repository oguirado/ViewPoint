# vqa3d

Standard-library obligation checks, canonical integer state, observation identities,
and `vqa3d.receipt/v1` receipts. Optional MuJoCo simulation dependencies are loaded
inside its adapter. The receipt does not claim `action_receipt/v0.2` validity.

Run from the component root:

```sh
python3 -B -m vqa3d route 'move a Blender object'
python3 -B -m vqa3d probe --adapter blender
python3 -B -m vqa3d check --adapter blender --scenario vqa3d/scenarios/blender_move.json
```

A scenario is a nonempty list of `{action, obligation}`. Obligations use
`kind`: `state_change` (`keys`, optional `allow_changes`), `state_equals`
(`expected`: path -> int, optional `tolerance` or `tolerances`), `frame_change` (optional `region`: [x,y,width,height]),
or `no_change` (state and pixels). Paths use dots, including list indices, or JSON Pointer for names containing dots
(e.g. `/objects/Cube.001 Big/location_um`).
List indices use canonical nonnegative ASCII integers (`0`, `1`, ...); negative,
signed and zero-padded indices are rejected. Each obligation kind rejects unknown keys.
Missing state keys fail; invalid regions error. A state obligation may pass without
pixels. A pixel obligation cannot pass without pixels. No change can be a legitimate
result; only the requested obligation decides. No image-content acceptance oracle
is implied by frame-change detection.

Canonical JSON rejects floats, NaN, infinity and non-string keys. JSON booleans
remain distinct from integers and are prohibited in numeric state and integer
parameters. Finite runtime floats are quantised explicitly with decimal half-even
rounding. Quantisation does not establish determinism across platforms.

Verdict commands and stages use distinct return-code tables in `verdict.py`.
`state_comparison_available=false` qualifies the boolean
`state_sha256_change=false` when a complete observation pair is absent; it does not
assert unchanged state.

`check` returns the worst verdict's code, not the numerical maximum of codes.
The model judge can only worsen results. Judge `ADVISORY` becomes `CANNOT_SEE`,
following the documented contract in `blender_visual_qa/oracles/harness.py:43-46`;
that existing file is unchanged.

`--record-trace F.json` retains original states, small PNGs, actions, receipts and
provenance atomically. `--process-log F.json` retains Blender stdout/stderr together
with the owned pid/pgid, return code and environment digest. Traces have a digest;
replay checks action order, tags, observation identities and decoded RGB. Their
provenance qualifies the recorded run only, never current runtime availability.
`fakes.compare_replay_runs(first, second)` compares complete, digest-validated receipt
lists from independent runs of the same model and scenario. It returns `FAIL` with
`nondeterministic_replay:step:<index>` when the receipts differ. Matching missing
observations cannot establish determinism.

Blender transform actions contain exactly `op`, `object` and the matching integer
vector: `set_location` uses `value_um` (micrometres), `set_rotation` uses `value_mdeg`
(millidegrees), and `set_scale` uses `value_ppm` (parts per million). `hide` uses an
integer `value` of `0` or `1`. Floats, boolean numeric parameters and the former
unquantised transform `value` field are rejected before Blender starts. Conversion
to Blender's floating-point units happens only in `blender_side.py`.

Blender uses a scratch copy of `examples/cube3.blend`, Workbench, an empty declared
environment, bounded subprocess waits and recorded process groups. PNG capture is
through an anonymous RAM file. `--source` selects a read-only .blend source. Source files are read only. Existing scene cameras
are preserved; a fallback camera is configured only when there is no scene camera; its fallback provenance survives scratch-file reopening.
MuJoCo simulation defaults `MUJOCO_GL` to `egl` with `setdefault` before importing
`mujoco`; it never replaces a caller's value. This adapter supports EGL only.
Select the backend before importing `mujoco`; an unsupported caller selection or
an already-imported non-EGL context is reported unavailable, with a reason.
Probe output and recorded trace provenance include the backend used.
Render failure is intentionally sticky and fail-closed for the adapter instance:
state remains usable with `capture_available=false`, but rendering is not retried.
Create a new adapter instance to retry rendering. Probe reports unavailable on a
failed render while retaining the independently available state/action capabilities.
All adapter probes return unavailable with reasons on setup failures; the CLI
reports every adapter even if a constructor, probe or cleanup fails. Stub
integrations declare `not_implemented`; calls other than `probe` raise the typed
`AdapterNotImplemented` exception.

Capture latency, renderer performance, grader accuracy and cross-platform
reproducibility: AUSENTE — no inventar. The mandate covers the supplied component,
its explicit obligations, and the retained live evidence only.

Intent routing imports `route/query.py` and uses its `any` query with the tracked
index and a read-only AST overlay built by `route/build.py` for `vqa3d/*.py`. This
provides current adapter coordinates before the new files are staged, without
changing `route/index/` or the frozen benchmark. An absent match stays absent.


## World state and time (round two)

The core stays stdlib-only. All numeric state leaves are integers. Conversion from
Blender floats happens only in `adapters/blender_side.py`. Each object retains its
local `location_um`, `rotation_mdeg`, `scale_ppm`, and `hide_render`. It also reports
`parent` (name or empty string), `parent_type`, `parent_bone`, and evaluated
`matrix_world` decomposition as `world_location_um`, `world_rotation_mdeg`, and
`world_scale_ppm`. Armatures expose evaluated pose bones under
`/objects/RIG/bones/<name>/head_world_um` and `tail_world_um`. State includes
`frame_current`; `set_frame` takes exactly `{op:"set_frame", frame:int}`.

Each object also carries two author declarations: `role` (the string custom
property `vqa3d_role`, or an empty string) and `collections` (sorted collection
names). Profiles use them to tell a prop, a foot or a floor from other geometry.

Each pose bone also reports `parent` (bone name or empty string) and two unit
quaternions as `[w, x, y, z]` in ppm, rounded half-even, with the first nonzero
component positive (`q` and `-q` are one rotation):
- `quat_world_ppm`: `armature.matrix_world @ pose_bone.matrix` (bone +Y runs from
  head to tail);
- `quat_local_ppm`: `parent.matrix^-1 @ pose_bone.matrix`; a root bone uses
  armature space.

An armature-deformed mesh adds `skin_armature` and `vertex_groups/<bone>/` with
`world_bbox_min_um`, `world_bbox_max_um` and `verts`, over the evaluated vertices
weighted at least 0.5 to that group. Groups without such vertices are omitted.
This is how a unified character mesh exposes its feet.

Mesh `world_bbox_min_um` and `world_bbox_max_um` come from evaluated mesh vertices,
including modifiers and armature deformation, in world coordinates. Mesh memory
is released after extraction. With a scene camera, bounding boxes also expose
`view_ppm` (projected bbox area inside the normalized camera rectangle divided by
its full projected area) and mean `view_depth_um`. Projected corners behind the
camera are excluded from the area. Fully behind-camera or zero-area boxes score
zero; without a camera the view fields are absent. This is a bbox framing proxy;
it does not measure occlusion or the fraction of visible object pixels.

Optional `sample(frames, paths)` returns `{frame: {path: int|[int]}}`. Blender
samples all requested frames in one bounded process, without saving the sampled
frame. The generic fallback applies `set_frame`, reads state, and restores the
original `frame_current` when present, including on errors. Recording stores the
samples; replay consumes and validates frame/path order without a runtime.
JSON frame keys are decimal strings; adapters may return integer frame keys.
Missing paths fail (`sample_state_key_missing`); malformed sample values error.
At most 2000 samples are accepted [TARGET].

| Obligation | Fields and semantics | Failure diagnostic |
|---|---|---|
| `state_equals` | `expected` path→int; optional nonnegative global `tolerance` **or** `tolerances` path→nonnegative int; omitted paths use exact equality | `state_not_equal:<path>:expected=<int>:actual=<int>`; noninteger state: `state_not_integer`; missing path: `expected_state_key_missing` |
| `state_change` | Existing `keys` and `allow_changes`; descendant changes still require explicit permission, including newly observable world/bbox/view effects | `required_state_unchanged:<path>:value=<int>` for each vector element; `unexpected_state_change:<path>:expected=<value>:actual=<value>`; missing path: `required_state_key_missing` |
| `within_distance` | `a`, `b`: path resolving to integer xyz or literal integer xyz; nonnegative `max_um`; optional `frames` is an **explicit list**, e.g. `[1,106,250]` | `distance_exceeded[:frame=<f>]:distance_um=<int>:max_um=<int>`; squared distance is compared exactly, `math.isqrt` gives diagnostic floor distance |
| `range_over_frames` | `frames:[start,end,step]`, inclusive end when reached, positive step; `path` scalar or vector; `min` and/or `max` integers | First `range_exceeded:<path>:frame=<f>:element=<index>:value=<int>` |
| `max_delta_over_frames` | Same range triple, `path` scalar/vector, nonnegative `max_delta`; componentwise absolute jumps between consecutive samples | Largest jump `max_delta_exceeded:<path>:frames=<previous>,<current>:delta=<int>`; a single sample has no jump |
| `camera_is` | Nonempty `name`; requires `camera_source=scene` and matching name | `camera_not_equal:expected=<name>:actual=<name>:source=<source>` |
| `frame_not_blank` | Optional `max_uniform_ppm`, default 995000 [TARGET]; most frequent **decoded RGB** color fraction must be strictly below it | `frame_uniform_ppm=<int>:max_uniform_ppm=<int>` |
| `frame_noise_below` | Required `max_outlier_ppm`; optional luma `threshold`, default 32 [TARGET]; score must be at or below the limit | `frame_outlier_ppm=<int>:max_outlier_ppm=<int>` |
| `frame_change` | Existing optional `region`; optional per-channel `pixel_tolerance` (0..255), `min_changed_ppm` (0..1000000); a pixel changes if **any** channel exceeds tolerance, and at least one pixel must change | With thresholds: `changed_ppm=<int>:min_changed_ppm=<int>`; legacy exact mode: `decoded_rgb_unchanged`; size mismatch: `frame_dimensions_changed` |
| `capability` | Any nonempty subset of `engine`, `device_type`, `denoiser`, `view_transform`, `look`; checked by `probe(render=True)` **before capture/action** | Unmet requirements produce **CANNOT_SEE** `capability_missing:<field>:<value>` and do not act |

All new kinds reject unknown fields. Invalid types, negative tolerances, reversed
bounds, empty/oversized frame lists, boolean numeric inputs, invalid ppm values
and noncanonical state paths error. Both image metrics give CANNOT_SEE
`capture_unavailable` without after pixels. `frame_change` requires both captures.

Image metrics use integer arithmetic: luma is `(77R+150G+29B+128)//256`;
noise counts pixels whose luma differs from the median of their 3×3 neighbourhood
by **more than** `threshold`, using edge replication. The denominator is all
pixels. A constant image scores zero; N isolated interior bright pixels on black
score `ceil(N*1000000/total)` when their luma exceeds threshold. Noise fractions round up,
so a sub-ppm outlier never satisfies a zero-outlier obligation.
These are declared proxies; texture edges can be outliers and the metric alone
does not certify render quality. `frame_change` thresholds do not claim semantic
image acceptance.

## Camera and render provenance

Observations, receipt `before`/`after`/`after_frame` refs, and recorded trace rows
carry `camera_source` (`scene`, `fallback`, `none`, `unknown`) and `camera_name` (empty for
none). Camera provenance participates in observation identity when present.
Legacy observations retain their identity and default to `none`, never infer a
scene camera. Receipt schema remains `vqa3d.receipt/v1`. Original trace observations
are retained; migrated receipt snapshots record their source trace digest.

`python3 -B -m vqa3d probe --adapter blender --render` adds `engines`,
`cycles_devices` (device type→names), context-dependent `denoisers` enum items,
`view_transforms`, `looks` by view transform, and `gpu_render_ok_by_type` (backend → boolean), plus the aggregate
`gpu_render_ok` diagnostic. Capability gates use only the requested backend's
entry in `gpu_render_ok_by_type`. GPU execution
uses an 8×8, one-sample Cycles render with CPU devices disabled [TARGET]. Its
owned process has a default probe limit of 300 seconds [TARGET], capped by
the adapter timeout and the remaining profile-session deadline. Failure never raises from `probe`; it sets
`gpu_render_ok=false` and retains bounded `gpu_render_stderr_tail`. Standard
`available` still describes the independent Workbench/state/action capture path.
CUDA enumeration alone cannot satisfy a CUDA capability: GPU render must succeed.
A look paired with `view_transform` must belong to that transform. Probe and sample
events are retained in traces so capability refusals also replay without acting.

The existing move scenarios now explicitly permit the moved object's derived
world/bbox/view fields; unrelated object changes remain forbidden. No intersection
obligation is implemented: optional B13 remains AUSENTE — no inventar.


## Adapter configuration and review corrections

Both Blender adapters accept `capture_engine` (`workbench` default, `eevee`, or
`cycles`) and `capture_samples` (default 1 [TARGET], bounded by
`max_capture_samples`, default 4096 [TARGET]). Cycles captures use CPU and
`capture_denoising=False` by default; set it explicitly only after a successful
denoiser capability check. Captures
record engine/samples in observation notes; step receipts seal them in
`capture_settings.before/after`. The live action receipt seals the whole action
and the before/post-action draw and depsgraph counters. Receipt schema remains
`vqa3d.receipt/v1`.

Constructor overrides also include `width`, `height`, `max_width`/`max_height`
(default 96/72 [TARGET]), `color_mode`/`color_depth` (default RGB/8 [TARGET]),
`max_sample_frames` (default 2000 [TARGET]), `probe_width`/`probe_height`
(default 8/8 [TARGET]), `probe_samples` (default 1 [TARGET]) and `probe_timeout`
(default 300 seconds [TARGET], capped by the adapter/session timeout). Live `timeout_ceiling` defaults to 180 seconds
[TARGET] and can be raised explicitly along with `timeout`. These are constructor
kwargs; the shared CLI and profile files belong to the separate profiles lane.
The existing observation decoder requires RGB8 PNGs; other requested formats
are dispatched but fail explicitly if the returned image is unsupported.

Temporal obligations accept `max_sample_frames` to raise/lower their validation
ceiling; adapters have an independent ceiling and must admit the same request.
`frame_not_blank.max_uniform_ppm` and `frame_noise_below.threshold` override their
existing defaults. Noise obligations also accept `luma_weights: [r,g,b]`,
nonnegative integers with positive sum (default `[77,150,29]` [TARGET]); luma uses
nearest-integer weighted averaging. Missing sampled paths yield FAIL
`sample_state_key_missing`; empty sample paths are rejected. Probes and samples
restore engine, devices, resolution, color management and frame on exceptions.
Successful capability checks return immediately without capture or action.
Rendered replay probes require an actual recorded probe event.

Mesh objects include `tris` (evaluated triangle count). `images/<name>/size_px`
is the current integer image dimensions. Light rows retain object `type: LIGHT`
and add `light_type`, `energy_mw`, `color_ppm`, and `hide_render`. Internally-created
fallback cameras are marked and excluded from `objects`; capture provenance
still identifies the fallback. An unrecorded provenance is `unknown`, never
implicitly `scene`. Unknown ops produce ERROR `unsupported_action_op:<op>`;
missing targets produce ERROR `object_not_found:<name>` through the subprocess
boundary.

Blender sampling object paths must use JSON Pointer (e.g.
`/objects/Cube.001 Big/location_um`); ambiguous dotted object paths raise
`blender_object_paths_require_json_pointer`. Whole-map `objects` and `/objects`
paths preserve every eligible object. View bounds clip bbox segments to camera
near/far planes before projection; mean depth covers the clipped points. This
remains a bbox-area approximation, not visible-pixel segmentation or occlusion.

Live probe requires an actual VIEW_3D area. A responsive bridge with no fresh draw
returns CANNOT_SEE `stale_frame_timeout`; a silent bridge or canonical-schema
violation returns ERROR. Capture uses one shared deadline for freshness and
render exchange. PNG and optional viewport files are removed after each capture.
Bridge root/inbox/outbox directories are private even under a permissive umask.

## Production controls

`check --capture-engine workbench|eevee|cycles --capture-samples N` selects a
one-off capture. All captures have a `30 s` [TARGET] child deadline by default
(`capture_timeout` constructor override). EEVEE has a `15 s` [TARGET] bounded
capture attempt and returns CANNOT_SEE `eevee_probe_timeout:15` if it cannot
complete; this is a budget observation, not a claim that all EEVEE contexts fail.
Killed children report `driver_killed:<signal>` and timed-out children report
`driver_timeout:<seconds>`. A failed session never respawns to manufacture
remaining evidence.

`state_change` and `no_change` accept `allow_derived: true`. Only objects named
in `keys` imply their `world_*`, `view_*`, `bbox`, and evaluated `vertex_groups`
fields; `frame_current` is also implied when an object is named. Other objects
and local fields remain strict. `no_change.keys` identifies the object scope;
it does not permit changes to those keys. Pixel equality still applies to
`no_change`. The default remains strict.

For `set_frame` scenarios, opt into `allow_animated: true` on `state_change`
or `no_change`. For example: `{"action":{"op":"set_frame","frame":805},
"obligation":{"kind":"state_change","keys":["/frame_current"],"allow_animated":true}}`.
The observations declare `animation_paths` from actions, NLA, drivers,
constraints, animated parents and armature deformation. Across different frames,
only those declared channels and their evaluated dependencies are implied;
static transforms and structural fields remain strict. Declarations must match
in both observations. Missing declarations grant no exception. `no_change`
still requires equal decoded pixels. `allow_derived` is independent and optional.

The bundled live bridge publishes its PID in `bridge.json`. The adapter checks
that PID before and during exchanges; a disappeared or zombie target returns
`live_target_dead`, and a silent bridge returns `bridge_no_answer:<mode>` within
the shared exchange deadline. VG-TOOL-0223 informs the liveness distinction.
The adapter does not terminate an attached GUI.

Scenario `feet_floor` and `foot_slide` obligations accept `frames: [start,end,step]`
and the same `floor` declaration as a profile. The film defaults supply omitted
contact thresholds; explicit obligation parameters win. They record `floors_used`
and refuse a missing contact interface. They do not turn a global world-Z bound
into an implicit floor assertion.

`profile check --floor z_um=<int>|object=<name>|collection=<name>` and
`--attach OBJECT=in_grip|rigid_follow` declare facts about one scene for one run.
They are recorded in every affected receipt and never change the profile identity.

GPU probes keep the small image and sample budget while allowing cold CUDA
kernel compilation up to the bounded probe timeout. A scratch cache is fresh
for each adapter; a running production render can already have compiled kernels.
A failed render reports `gpu_probe_failed:<backend>:<reason>:stage=<stage>`,
not just device enumeration. The probe and capability receipt retain `gpu_probe`
with separate stdout/stderr tails (at most 4096 characters each [TARGET]),
selected dimensions/samples, the declared environment, and environment hashes
observed in the Blender child. Declared absolute `*_PATH`/`*_DIR` values also
retain their paths; other declared values are redacted from diagnostic text.
