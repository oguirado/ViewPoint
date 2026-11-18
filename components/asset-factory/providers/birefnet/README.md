# Offline input preparation

Run modules from the asset-factory root. Both providers lack qualified production runtime and calibration. No installation, download,
container start, implicit backbone fetch or GPU inference occurs.

Read-only admission:

```sh
python3 -B -m providers.birefnet.adapter --probe --source reference.png --model-root MODEL_ROOT
python3 -B -m providers.realesrgan.adapter --probe --source reference.png --model-root MODEL_ROOT
```

`build/recipe.json` is an offline native build/admission recipe. It pins measured
local files and installed distribution versions. A null version is missing
evidence, and refuses execution. Its revision is a content identity; the upstream
commit is AUSENTE. No third-party code or weights are redistributed.

Preparation requires `--source`, `--model-root`, `--output` (a new directory),
`--factory`, `--licence-policy`, `--gate-policy`, `--calibration`, `--lease` and
`--timeout-s`. The lease path must be shared by both providers and the install's
scheduler. Stale leases require operator recovery; no automatic takeover exists.
Run tests and inference in the resource scope required by the build brief.

The adapter checks provider and configuration identity, file hashes, dependency pins,
measured peak and free-memory floor before model allocation. The child rechecks
pins, denies network access and runs on CPU. Publication exclusively creates an
output directory, stages image writes with `os.replace`, and commits canonical
`receipt.json` last. Refusals preserve existing caller paths.

BiRefNet preserves source RGB and produces soft alpha at the source resolution.
Its nested `af.reference-preparation/v1` receipt feeds the existing input gate;
the gate still labels execution provenance unverified. Stage OK proves measured
execution and the chosen alpha policy only; semantic quality stays CANNOT_SEE.
`slot: generate` denotes preparation for generation, never a mesh-producing
generate result. Neither current schemas nor supplied drafts register dedicated
matting/upscale slots; provider manifests validate against the existing schema.

Synthetic fixtures exercise wiring and refusal controls. Their calibration and
memory records are never production qualification.

CPU qualification uses the same authenticated local loader with
`CUDA_VISIBLE_DEVICES=` and offline Hugging Face flags. The loader refuses an
unset or nonempty CUDA visibility before importing Torch. `observations.py`
reports alpha-byte statistics and installed distribution versions for a hashed
environment freeze. Its midpoint partition is diagnostic only: golden alpha
error and IoU remain null, with `golden_alpha_absent`; quality stays CANNOT_SEE.
Installed version pins do not provide offline package artifacts or a rebuild
proof. Qualification does not authorize production publication or change the
blocked provider manifest.

Admission uses the minimum of host availability and every enclosing cgroup's
remaining hard memory budget. Missing cgroup observations refuse admission.
An observed worker peak above the GiB envelope used for admission refuses
publication, even when it stays below the recipe's broader hard ceiling.
Peak identities include the resource-observation implementation.
