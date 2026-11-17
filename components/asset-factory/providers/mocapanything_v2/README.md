# MoCapAnything V2 motion slot

AGPL-3.0-only adapter code. The upstream code licence is **AUSENTE**; model-card
MIT declarations do not clear dependencies or outputs. `provider.json` remains
`runnable:false`, `blocked_reason:research_only_unverified_runtime`; production
readiness is **CANNOT_SEE**. Each user supplies `commercial_use` through the factory.

From the asset-factory root:

```sh
python3 -B -m providers.mocapanything_v2.adapter preflight --motion-root "$MOTION_MODELS" --birefnet-root "$BIREFNET_MODELS"
python3 -B -m providers.mocapanything_v2.adapter inspect-rig --in "$RIG_GLB"
python3 -B -m providers.mocapanything_v2.adapter generate --request "$REQUEST"
python3 -B -m providers.mocapanything_v2.adapter accept-bvh --request "$REQUEST" --bvh "$BVH" --reference "$REFERENCE" --observation "$OBSERVATION" --output "$NEW_OUTPUT_DIRECTORY"
```

`build/recipe.json` pins existing checkpoint bytes, BiRefNet custom code and the
catalogued upstream revision. No command downloads, installs or starts a model.
Source-tree provenance, a dependency lock, the DINOv2-large pin, measured model
peak, release proof and the upstream feature bridge remain missing. The archived
inference source is sandbox-modified; it is not a verified patch base.

`offline.load_birefnet` authenticates local code and weights, uses local-only
loading and returns RGBA foreground images. It never impersonates BriaRMBG's
tensor interface. Its Python audit guard rejects sockets and child processes;
native-library egress containment is unqualified. Run it only in an owned worker.

`schemas/` define the bound rest pose and sole-surface observations. The gate
measures FK consistency, shortest-arc rotation changes, clearance, stance slide
and plant speed. Contact windows come from the bound reference, not motion-derived
relabeling. Pose repeats are legitimate; frame identity and indexed pose hashes
detect stale observations. Bone-only feet give CANNOT_SEE.

`contact-policy.json` freezes the strategy's **TARGET_UNCALIBRATED** bounds. An
observed-motion run never reports PASS before calibration and independent source
capture qualification. The explicit `--synthetic-fixture` flag, also declared in
request params, permits fixture-only contract publication as `af.motion/v1` with
`action_bvh`. Its contact receipt retains `scope:synthetic_fixture`; this is not
model execution or asset approval. Existing output directories are refused.
