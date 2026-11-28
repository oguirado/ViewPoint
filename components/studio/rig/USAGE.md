<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Studio rig tools

Run these commands from `components/studio/rig`. The tools use Python 3.12 or later, NumPy, SciPy, and, for the pose oracle, Pillow. Blender operations use background mode with startup scripts disabled. Pass `--blender` to the rig commands or set `STUDIO_BLENDER` for the oracle to select the local executable.

Rig inputs are embedded-buffer glTF 2.0 GLBs. The template and transfer paths take a single identity-transform textured mesh with indexed triangles in Y-up coordinates. Provide an accepted template GLB and its matching joint map. The joint map supplies `joint_glb_nodes`, `adapter_semantics` and ordered `adapter_map.joints` rows with `source`, `target` and `parent` fields. The landmark fit uses the humanoid template's numbered joint roster and proportions.

## Template and rigid skinning

```sh
python3 -B cli.py template --method auto --selected selected.glb \
    --template reference.glb --joint-map template-map.json --out-dir out/auto
python3 -B cli.py template --method rigid --selected selected.glb \
    --template reference.glb --joint-map template-map.json --out-dir out/rigid
python3 -B cli.py rigidity --poses samples.npz --out out/rigidity.json
```

Both template modes write `rig.glb` and `report.json` after structural validation. A pose `HOLD` preserves these files for diagnosis and rejects the run. Auto weights come from Blender bone heat, with bounded antenna-fragment completion, a central-shell limb-weight constraint, and fourth-power sharpening of the strongest influences. The tool replays the original textured mesh with its index and shading buffers preserved and measures local hull volume in stress poses. Rigid mode splits triangles at joint planes, interpolates shading attributes at the cuts, and checks the per-part distance bound after Blender poses the result. The standalone rigidity gate takes an NPZ with `rest` and one or more named poses, each an N×3 array in metres, and `parts`, an integer joint or part label for each vertex. Its JSON reports every failing part and pose.

## Pre-rig weld, bind recomputation, and UniRig

```sh
python3 -B cli.py weld --input selected.glb --out out/welded.glb
python3 -B cli.py bindfix --input provider/rigged.glb --out out/rebound.glb
python3 -B cli.py unirig-plan --original original.glb --seed 20 --out out/original-trial.json
python3 -B cli.py unirig-transfer --original original.glb --source provider/rigged.glb \
    --inference provider/inference.json --selected selected.glb --joint-map template-map.json \
    --height-m 1.8 --out-dir out/transfer
```

Create the parent directory before commands with a file output. Weld preserves ordered triangle corners and emits a compact geometry-only input. It refuses open or inconsistently wound geometry. Bindfix recomputes the inverse binds from the serialized rest transforms inside an exclusive bind buffer view. The GLB header, JSON and other binary ranges retain their bytes.

Use the plan's input digest and seed to submit the unchanged original GLB through your approved UniRig provider. Give transfer the raw merged GLB and the corresponding `af.unirig-host-inference/v1` receipt. The command checks the original and merged hashes, the completed provider stages, the supported source hierarchy and the selected surface's coordinate frame. It projects the source's skin to the selected textured mesh, recomputes binds, and reopens the result for the pose gate. A local `HOLD` writes the candidate and report for diagnosis and returns a refusal. `PASS_LOCAL_GATES` records the transfer and pose checks. Production admission requires its own qualification receipt.

```sh
python3 -B cli.py diagnose-prepare --original original.glb --selected selected.glb \
    --control control.glb --seed 20 --out-dir out/diagnostics
python3 -B cli.py diagnose-report --plan out/diagnostics/trials.json \
    --observations out/diagnostics/observations.json --out out/diagnostics/report.json
```

The plan contains the control, two linear subdivisions, the original surface and two seed variants of the exact weld. A diagnostic observations file is a JSON array. Each entry contains `variant`, `repeat`, `seed`, `input_path`, `output_path` and `receipt_path`. For a committed provider failure, supply `input_path` and `failed_attempt_path` with the attempt's `result.json`, `host-launch.json`, `admission.json` and `scope-exit.json`. Paths are relative to the observations file. The report checks the input and merged GLB against each host receipt before evaluating the control and replay agreement. Planning, preparation, and readback run offline. Provider execution is a separate authorization boundary.

## Independent pose oracle

Give the oracle the same registered contract, camera roster and pose manifest for both assets. Pin each comparison map to its own asset. The map describes semantic `joints`, `chains` and `soles`. The contract supplies the metric tolerances and, for a generated sweep, the anatomical axes, joint degrees of freedom and sweep profiles. An inspection produces the reference geometry for sweep generation.

```sh
python3 -B pose/oracle inspect --rig incumbent.glb --map incumbent-map.json \
    --contract pose-contract.json --out out/reference.json
python3 -B pose/oracle sweep --reference out/reference.json --map incumbent-map.json \
    --contract pose-contract.json --profile quick --out-dir out/poses
python3 -B pose/oracle cameras --map incumbent-map.json --seed held-out-example \
    --out out/cameras.json
python3 -B pose/oracle compare --incumbent incumbent.glb --incumbent-map incumbent-map.json \
    --candidate candidate.glb --candidate-map candidate-map.json --contract pose-contract.json \
    --cameras out/cameras.json --poses out/poses/sweep.json --reference out/reference.json \
    --out out/compare.json
```

`pose/oracle --help` lists the single-asset `measure`, native scene binding and historical fixture commands. `synth --contract pose-contract.json --out-dir out/controls` builds small synthetic GLBs, a joint map, cameras and a sweep for checking the oracle. `scene` accepts explicit incumbent and candidate Blender files, their SHA-256 digests, and one or more `--witness` object names.

## Exit codes and tests

Successful commands exit `0`. A measured rejection exits `1`. Usage or harness errors exit `2`. Refusals emit JSON with `reason`, `retry_when`, and `next_command`. Keep the JSON report from a measured rejection for the gate evidence.

Run the offline suite with the product worktree as the current directory for this component.

```sh
LC_ALL=C.UTF-8 LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1 \
systemd-run --user --scope -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 -- \
    nice -n 15 taskset -c 0-15 python3 -B -m unittest discover -v -s tests
```
