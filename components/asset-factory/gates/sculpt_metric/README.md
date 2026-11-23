# Sculpt metric

`api.evaluate(reference_glb, candidate_glb, *, mesh_policy, region_policy)` returns
`af.sculpt-metric/v1`. The mesh policy is the existing `tools.mesh.api` policy.
The region policy requires `resolution`, `max_changed_pixels_ppm`,
`max_mean_abs_delta_ppm`, and `timeout_s`; there are no acceptance defaults.
Pixel budgets apply independently to each view and each exact engine region.

The evaluator snapshots inputs into verified tmpfs, calls `compare_meshes`,
renders a raw-bound orthographic exterior ring with CPU Cycles, and evaluates
decoded RGB differences through the committed vision-engine wheel. The RAM
adapter converts unequal decoded pixels to a declared binary RGB witness for
S0–S4; original frame hashes remain distinct from residual frame hashes. Engine
spans define membership for regional scores; bounding boxes do not. Uncovered
changed pixels make regional evidence incomplete. Frames and snapshots are
removed on return. Only canonical receipts leave the evaluator.

Existing silhouette and distance guards remain **DESIGN_CHOICE, uncalibrated**:
IoU at least 950000 ppm [TARGET], both directional distances at most 20000 ppm
[TARGET] of the raw diagonal. Distance remains a sampled estimate. Local pixel
budgets require separate calibration before production use. The conservative
`score_ppm` is the minimum of silhouette agreement and the complements of
relative distance, worst-view changed-pixel fraction and RGB absolute residual.
It is a regression score, not an appearance rating. Regional scores preserve
local evidence beside that ranking number. `rank()` admits measured PASS/FAIL
records with identical context pins, rejects duplicate candidates, and uses
candidate digest to break ties. Ranking never upgrades a verdict.

CLI from the asset-factory root:

```sh
python3 -B -m gates.sculpt_metric.api --reference RAW.glb --candidate CANDIDATE.glb \
  --mesh-policy MESH_POLICY.json --region-policy REGION_POLICY.json
```

Run CLI/tests under the task's `systemd-run`, affinity and niceness scope.
Missing evidence returns CANNOT_SEE; malformed evidence returns ERROR. No caller
output path exists. Sources, sealed engine trees, models and GPU are untouched.
Interior surfaces and appearance remain outside the measured coverage.
