# Motion receipt reason codes

The ACT evaluator emits each sampled and rest limit failure under `af.motion-receipt/v1` using the corresponding key in `metrics`. The `limits` keys in the table identify the thresholds used for both kinds of checks.

| Previous code | New code and metric key | Limit key | Failure condition |
|---|---|---|---|
| `joint_angular_rate` | `rotation_rate_upper_urad_s` | `rotation_rate_upper_urad_s` | Sampled joint rotation rate exceeds its upper limit. |
| `sole_stance_slide` | `stance_slide_upper_um` | `stance_slide_upper_um` | Sampled sole slide exceeds its upper limit during stance. |
| `sole_plant_speed` | `plant_speed_upper_um_s` | `plant_speed_upper_um_s` | Sampled sole speed exceeds its upper limit during stance. |
| `sole_penetration` | `floor_clearance_lower_um` | `floor_clearance_lower_um` | Sampled signed floor clearance falls below its lower limit. |
| `joint_displacement` | `joint_displacement_um` | `joint_displacement_um` | Joint displacement between output frames exceeds its upper limit. |
| `joint_rest_displacement` | `joint_rest_displacement_um` | `joint_rest_displacement_um` | Sampled joint displacement from rest exceeds its upper limit. |
| `joint_speed` | `joint_speed_um_s` | `joint_speed_um_s` | Sampled joint speed exceeds its upper limit. |
| `stance_gap` | `stance_gap_upper_um` | `stance_gap_upper_um` | Sampled sole gap exceeds its upper limit during stance. |
| `rest_sole_penetration` | `rest_floor_clearance_lower_um` | `floor_clearance_lower_um` | Rest-pose signed floor clearance falls below the floor-clearance limit. |
| `rest_stance_gap` | `rest_stance_gap_upper_um` | `stance_gap_upper_um` | Rest-pose sole gap exceeds the stance-gap limit. |

Each listed condition returns `FAIL`. INT consumers that read historical receipts can translate the previous codes when displaying or aggregating reasons. Retain the original receipt bytes and digest.

An invalid support-plane normal returns `ERROR` with `support_normal_invalid` before replay. In the registered `gltf_Y_up_right_handed` frame the Y component must be positive and the tilt from the up axis must be at most 30° [TARGET], with calibration state `TARGET_UNCALIBRATED`. The evaluator checks `3 * (nx² + nz²) <= ny²` on the integer components before normalization.

The receipt scope is `fixture-output-frame-and-decoded-key-samples`. A fixture `PASS` carries `TARGET_UNCALIBRATED` and production verdict `CANNOT_SEE`. [JSON Schema](af.motion-receipt.v1.schema.json)
