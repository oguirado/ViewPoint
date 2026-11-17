# Seed ranking

`python3 -B -m gates.rank.rank --request request.json --receipt ranking.json`
runs from the component root. `request.schema.json` is the closed input contract.
Only an rc-zero ordinal-ranking receipt is published; refusals appear on stdout.
The independent asset verdict always remains `CANNOT_SEE`.

Run tests and inference under the assigned resource scope:
`systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 taskset -c 0-15 nice -n 15 timeout 300 python3 -B -m unittest discover -s tests -p 'test_w3_rank_*.py'`.
The CLI supervises a separate process group with a caller-declared bounded timeout.

Every PNG and optional GLB/receipt has a relative path and SHA-256 under
`evidence_root`. Explicit binary L-mode masks contain only zero/background and
255/foreground pixels. Image and mask dimensions must match the capture contract;
no alignment, threshold, registration or mask is inferred. Input bytes are retained.
The capture identity binds the declared front orthographic camera and framing.
Seed mode requires distinct seeds, authenticated generation and capture receipts,
and a source-bound upstream gate. Diagnostic mode cannot select a winner.

Every supplied gate requires a caller `gate_expectation` with `operation`
(`metrics`, `compare`, or `repair`), `policy_sha256` and `toolchain_sha256`.
The complete `af.mesh-regression/v1` receipt is validated before eligibility.
Intake metrics may have explicitly unavailable comparison distances; comparison
and repair success require the full bidirectional and per-view evidence.
Missing expectations refuse seed selection rather than inferring a policy.

Both weights must be positive integers. Eligible upstream-gate candidates clearing
both caller floors sort first; then the weighted mean of silhouette IoU and shifted
DINO cosine sorts descending, with seed and id as deterministic tie-breaks.
Arithmetic uses ppm, floor rounding. Weights and floors are DESIGN_CHOICE,
uncalibrated. The raw-versus-repair IoU bound is not a reference-image ranking floor.

DINO requires pre-existing `torch`, `transformers`, `safetensors` and Pillow. The
qualified host interpreter is `/home/admin/AIModelos/venv/bin/python`.
This host supplies Pillow through explicit `PYTHONPATH=.:/usr/lib/python3/dist-packages`;
the supervised worker preserves supplied offline dependency directories.
Point
`model.root` at a local directory containing pinned `config.json` and
`model.safetensors`. Explicit `model.id:dinov2` requires `Dinov2Model`;
`model.id:dinov3` requires `DINOv3ViTModel`. Their config families must match;
neither a missing config nor an unsupported architecture gets a default.
The loader strictly checks missing/unexpected weights,
uses CPU float32/eval and disables network access. It never installs or downloads.
Runtime versions, preprocessing, checkpoint hashes and source-code hashes bind the
measurement. DINOv2 uses the versioned short-edge resize and centre crop;
DINOv3 uses the versioned bilinear square resize declared by its local processor.
Missing DINO produces null similarity/composite and no preferred seed.
The local checkpoint's licence declaration and each user's `commercial_use` stay
explicit data; this gate does not replace the factory licence decision.

`capture.py`, invoked through Blender's background CLI on scratch outputs, emits
CPU renders and alpha masks with a source-bound capture receipt. It never changes
the source GLB. Finish each scratch capture with
`python3 -B -m gates.rank.finalize --capture SCRATCH_DIRECTORY` to publish its
binary alpha mask and `af.rank-capture/v1` receipt.
Reference masks must be supplied separately and qualified before
production use. Ranking alone supplies neither appearance nor vqa3d acceptance.
