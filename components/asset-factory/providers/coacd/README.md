# Collision provider

This tool provider remains `runnable:false`, with runtime readiness
`CANNOT_SEE`. `provider.json` retains the slot-catalogue revision and MIT
metadata declaration; production licence-text/dependency/revision review is
AUSENTE. No model, weight, download or install is used. `runtime-lock.json`
does not authorise production. Native experiments use a separately frozen,
measured runtime identity; they never change `provider.json` readiness.

From the component root, use `python3 -B -m providers.coacd.adapter probe`.
`inspect --source F --source-sha256 H` measures immutable source topology.
Inspection always returns `CANNOT_SEE` for collision acceptance.
`gate --source F --source-sha256 H --candidate C --candidate-sha256 D
--gate-policy P` measures candidate bytes without native CoACD.

`run --request R --gate-policy P --factory F --territory-policy T
--output-dir NEW` uses the production licence decision and explicit
`commercial_use` from F. An optional `--provider M` supplies a qualified
manifest. Pending slot registration, blocked providers, absent peak proof,
unqualified runtime pins and absent clearances prevent publication.
No-replace directory publication preserves all caller bytes on refusal.

`collision.schema.json` and `provider.schema.json` are byte-identical copies of
the supplied catalogue drafts. Registered validation is used once collision
joins `core.SLOTS`; `schema_registration` records the selected boundary.

`gate-policy.schema.json` defines explicit integer-only policies. The example
policy declares one specific fixture corridor, not an automatically inferred
gap. Clearance probes must be independently declared and clear of the visual
source. Empty coverage returns `CANNOT_SEE`. Units are glTF metres in world
coordinates; physical scale is never guessed.

G11 checks hull count, per-hull face budget, closure, consistent outward
winding, connectedness, convex support planes, sampled source loss, sampled
excess volume and clearances. Lost volume is at most 10000 ppm [TARGET,
uncalibrated]; caller limits may be stricter. Source shells with ambiguous
multiple components are refused. Self-intersection qualification is AUSENTE.
Sampling is seeded and bounded; no confidence interval, exact-volume bound,
physical-scale calibration or bitwise replay is claimed by a sampled PASS.

Output contains source snapshot, collision GLB, separate OBJ hulls, gate
receipt, manifest and slot result. Consumers must retain hull separation;
merging them before a physics engine convexifies them can close gaps.
`verify_bundle` authenticates each source-bound hull and re-runs the gate.

Tests: `python3 -B -m unittest discover -s tests -p 'test_w3_collision_*.py'`.
Run tests and adapter commands in the operator's memory/CPU scope. Synthetic
backend tests exercise publication without claiming a native CoACD run.

`python -B -m providers.coacd.qualify freeze --cases CASES --plan NEW`
freezes a nonempty list of `{name,request,policy}` cases. Requests include
source bytes' digests, explicit seeds, resource deadlines and wrapper identity.
Record the emitted `freeze_sha256` before any experiment. Then run each case:
`python -B -m providers.coacd.qualify run --plan PLAN --plan-sha256 HASH
--case NAME --output-dir NEW`. It creates qualification evidence only, with
source-bound hull hashes and a measured gate receipt. Drift refuses execution.
The plan and result families are `af.coacd-qualification-plan/v1` and
`af.coacd-qualification/v1` (provider-local experiment records).

An offline split installation may append an explicitly known dependency root
to the parent interpreter's `sys.path`. Child workers forward only discovered
numpy/trimesh/scipy/CoACD package roots, preserving interpreter precedence;
they do not install anything or inherit arbitrary `PYTHONPATH` entries.
