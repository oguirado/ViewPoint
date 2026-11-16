# Offline TRELLIS.2 geometry recipe

Check the selected inputs from the component root:

```sh
python3 -B -m providers.trellis2.build.build
```

The committed `build-inputs.json` intentionally has null base-image, compiler,
wheel and Debian-package identities: AUSENTE. The check returns `CANNOT_SEE`.
Supply measured digest-qualified ARM64 CUDA images, the compiler hash, and
complete local package inventories before preparing a context. Each package
entry binds its filename and SHA-256; wheels additionally require `name==version`.
Local package folders are `wheels/`, `debs/builder/` and `debs/runtime/`.
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
The adapter and GLB export belong to the sibling lane. No weights are included.

`runtime_probe.py` checks compiler/import prerequisites and refuses stale cache
markers without rewriting them. It requires a bounded external invocation.
Even successful prerequisites return `CANNOT_SEE` until real geometry and GLB
evaluators exist. It emits no build receipt and proves no asset acceptance.

Host tests: `python3 -I -B -m unittest discover -s tests -p 'test_f1u1_*.py'`.
Run them in the campaign memory scope. `AF_F1U1_REPOS` overrides the documented read-only corpus checkout at `/home/admin/Documentos/The-Construct_v1/docker/trellis2/repos`; missing sources fail the source tests explicitly. Synthetic parameter
and import-boundary tests do not prove CUDA execution or rebuild equality.

The image uses the standalone isolated script; host entry points also support `-m providers.trellis2.build.runtime_probe`. The shared `asset_factory.core` is absent in this unit worktree; its integer-only JSON rules are regression checked locally. Build preflight is an input status, not an asset verdict. Git ignore rules outside this surface require explicit integration handling; no index writes are performed here.
