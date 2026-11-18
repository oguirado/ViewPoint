# Offline TRELLIS.2 geometry recipe

Check the selected inputs from the component root:

```sh
python3 -B -m providers.trellis2.build.build
```

The check returns `CANNOT_SEE` while base-image, compiler, wheel or Debian
package identities are absent. Supply measured digest-qualified ARM64 CUDA images, the compiler hash, and
complete local package inventories before preparing a context. Each package
entry binds its filename and SHA-256; wheels additionally require `name==version`.
Local package folders are `wheels/`, `debs/builder/` and `debs/runtime/`.
Source archives retain their small budget; CUDA packages have a separate bounded
budget. Ubuntu zstd-compressed Debian members require the host's libzstd decoder.
Debian inventories must cover Python, venv, build tools, system Eigen headers
and runtime libraries. Package resolution/linking remain future build checks.

`build.py --sources REPOS --packages PACKAGES --prepare NEW_CONTEXT` exports
only the selected committed revisions, verifies licence bytes and gitlinks,
applies checked diffs, parses changed Python and stages packages before fsync and rename. A sorted file/link inventory and SHA-256 manifest are verified before package use in each Docker stage; the printed argv pins the manifest digest.
It refuses existing outputs. A preflight checks lock format only and does not verify image availability, architecture or reproducibility. It prints a context-bound build argv and never
executes Docker. The later build still needs an approved memory/time budget;
the ordinary test scope does not authorize the larger build ceiling.

The patched entry is
`trellis2.pipelines.trellis2_image_to_3d.Trellis2GeometryPipeline`.
Its `run(image, *, seed, pipeline_type, ...)` requires both parameters and
returns raw `Mesh` objects. The inherited loader selects shape models only.
The adapter publishes checked raw GLBs. The calibration entry below also keeps
the original model arrays in `model-native.npz`. No weights are included.

`python3 -B -m providers.trellis2.runtime` provides an offline native calibration
entry from the component root. It requires explicit `--image`, `--out`,
`--model-root`, `--dino-root`, `--decoder-root`, `--seed` and `--pipeline-type`.
Mount the component and all checkpoint roots read-only into the approved image;
keep output outside the repository and enforce the operator's memory, CPU and
timeout envelope. It selects local shape checkpoints and the local DINO model,
never the inherited background-removal loader. It preserves model-native raw
geometry and leaves missing intake calibration and appearance evidence as
`CANNOT_SEE`. Its native execution record does not replace factory gate receipts.

Native execution currently refuses with `runtime_admission_unavailable` before
any CUDA call. An authoritative container, scheduler lease and idle-throttle
verifier is AUSENTE; caller flags cannot supply that authority. Historical
measurements remain evidence for the recorded runs, not qualification of this
repaired runtime. The adapter refuses an unmeasured mode or extra workload
parameters when a measured provider peak is supplied. Scheduler admission must
also bind the measurement scope before copying that peak.

Local model configuration and selected checkpoint bytes must match
`../provider.json`. Configurations are bounded and reject duplicate keys;
configuration, checkpoint and DINO paths must stay inside their read-only roots.
Build commands additionally require an owned nested PID namespace: init death
cleans detached descendants independently of the Python supervisor. Directory
publication uses atomic no-replace; an existing or racing caller name is kept.

`runtime_probe.py` checks compiler/import prerequisites and refuses stale cache
markers without rewriting them. It requires a bounded external invocation.
Even successful prerequisites return `CANNOT_SEE` until real geometry and GLB
evaluators exist. It emits no build receipt and proves no asset acceptance.

Host tests: `python3 -I -B -m unittest discover -s tests -p 'test_f1u1_*.py'`.
Run them in the campaign memory scope. `AF_F1U1_REPOS` selects the read-only upstream corpus checkout (`docker/trellis2/repos` in The Construct source corpus). Missing sources fail the source tests explicitly. Synthetic parameter
and import-boundary tests do not prove CUDA execution or rebuild equality.

The image uses the standalone isolated script; host entry points also support `-m providers.trellis2.build.runtime_probe`. Integrated host entry points use `asset_factory.core`; the isolated image probe loads its explicit sibling modules. Build preflight is an input status, not an asset verdict. No index writes are performed here.
