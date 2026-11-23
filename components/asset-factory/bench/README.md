# Evidence replay bench

From the component root:

```sh
python3 -I bench/run_bench.py --cases PLAN.json --provider ID --output-dir RESULTS --timeout-s 300
```

`plan.schema.json` defines the frozen input. Cases, selected providers and the
required gate checks form the comparison denominator. Every selected cell gets
a row, including licence refusals and missing evidence. Paths are relative to
the plan; paths inside a cell are relative to its evidence file. Paths cannot
escape their declared root. The timeout bound is a design limit [TARGET].

`case.schema.json`, `evidence.schema.json`, `gate.schema.json` and
`measurement.schema.json` define the input records. `results/result.schema.json`
and `results/matrix.schema.json` define outputs. The tests supply synthetic
positive and negative examples for the contracts.

Gate receipts use **af.bench-gate/v1**, the bench normalization contract.
Integration must produce them from verified input, appearance and vqa3d gates.
That verifier boundary is unavailable: normalized assertions alone always
yield asset acceptance CANNOT_SEE, even when `replay_rc` is zero.
`gate_results` retains every independently evaluated gate and its outcome.
Each receipt binds the case, provider, policy, toolchain and actual asset bytes,
contains the required named check outcomes and the pinned vqa3d profile, and links to the preceding receipt
in input → appearance → vqa3d order. Hashes use F0 canonical JSON including its
trailing newline. Raw `vqa.receipt/v1`, `bvqa.asset-receipt/v1` and
`vqa3d.profile-trace/v1` are deliberately rejected at this boundary; they need
their declared upstream verifier. A score, `accepted`, or a summary boolean
cannot substitute for the receipt chain.

Provider bundles contain `manifest`, `factory`, `policy` and the U2 `decision`.
The bench validates the frozen consumed record shapes in `frozen_contracts.py`,
jurisdiction membership, configured provider/profile, decision hashes and context
on every cell. It does not
infer licence rights. A blocked provider or a Hunyuan output reference is
refused. Other references require explicit redistribution clearance and
provenance; operator approval to use an image is not publication permission.

Missing measurements remain null and block completion. Non-null observations
require a hashed, case/provider/toolchain/output-bound measurement record,
an owned execution identity and a rehashed `observation.schema.json` record
binding the same cgroup, provider and execution. Wall time is derived from
monotonic observation times; peak memory is bound to the cgroup observation.
A scope label alone proves nothing: unknown attribution leaves both metrics
null. This verifies supplied observation data and does not run a sampler.

Replay prints diagnostic JSON with every evaluated row and preserves existing
`RESULTS/results.json` bytes. It cannot publish asset PASS while upstream
verification is unavailable. Missing receipts are CANNOT_SEE under frozen C12. `source_plan` locates the frozen plan relative to the
results directory and binds its file bytes; `evidence_records` locates each cell
relative to that plan. Gate and measurement paths remain relative to the cell. Aggregate precedence comes from F0 `worst_rc`;
specific stale/budget reasons remain on the individual row. Stage return codes
never use the inherited report failure code.

The copied inputs in `references/` retain their source hashes in
`manifest.json`, checked by `references.schema.json` and the host tests. They
are the operator-approved sheet and two crops, cleared for publication by the
operator ruling. The GemDroid render is excluded and guarded by a test. The
original reference files remain in place.

This command replays supplied evidence. Provider execution, normalization of
sibling gate bodies, real appearance review, and real-provider acceptance are
integration dependencies. No model, Blender process, download or GPU job is
started. Factory JSON uses the integrated `asset_factory.core` kernel; the
whole gate refuses any remaining temporary kernel shim.
