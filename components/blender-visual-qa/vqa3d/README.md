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
(`expected`: path -> int), `frame_change` (optional `region`: [x,y,width,height]),
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
through an anonymous RAM file. Source files are read only. Existing scene cameras
are preserved; a fallback camera is configured only when there is no scene camera.
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
