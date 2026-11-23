# Appearance evidence gate

From the component root (the command also supports isolated Python):

```sh
python3 -I gates/appearance_gate/evaluate.py --request REQUEST.json --upstream BRIDGE.json --review REVIEW.json --out RESULT.json
```

`evaluate(request, upstream, review)` consumes `af.appearance-request/v1`,
`af.bridge-verdict/v1` and `af.appearance-review/v1`; it returns
`af.appearance-result/v1`. It never dispatches a reviewer or runs Blender.
The schemas beside this file describe the closed records. CLI input is bounded;
refusals print diagnostics and preserve existing output bytes. Only rc 0 publishes.
Factory JSON uses the integrated `asset_factory.core` kernel.

The caller supplies a pinned licence decision, independent builder/reviewer
identities, an upstream bridge verdict, and an explicit hashed policy. The policy
is distinct from the territory policy. Required `licence_context` carries the current
`provider_manifest`, `configuration`, `territory_policy`, and expected `operation`.
Their canonical hashes must match the decision; provider id/revision/slot and
configuration jurisdiction/commercial intent/slot assignment must also agree.
Missing context gives `CANNOT_SEE`; changed bindings give `STALE`/`CANNOT_SEE`.
The caller obtains validated licence records from the licence checker.
The appearance policy
contains craft floor/target, likeness floor/target, and timeout. There are no
threshold defaults. F1 publication uses craft floor 4 and target 7 [CITED:
NEEDS_F1-U5.md:58]; likeness calibration remains AUSENTE. Scores are strict
integers; only non-character face appeal may be null. Minimum craft and likeness
both block acceptance. An independent reviewer can veto; it cannot clear an
upstream failure. Review `min_score` must be an integer but is recomputed;
explicit `FAIL` is a veto. All declared nested input-schema fields are validated.

`evidence_root` is an absolute caller-owned directory, preferably RAM scratch for
captures. All bound paths inside it are relative and symlink-free. Asset and
reference file digests bind source bytes. The render manifest binds those files,
profile, visual-only capture scope, helper exclusion and RGB24 frames named
`reference`, `render`, `comparison`. RGB24 is decoded row-major RGB with no
padding, with explicit width and height. The comparison must equal the actual
reference-first horizontal concatenation, row by row. PNG decoding and capture
remain the capture producer's responsibility. Helper exclusion metadata does not
prove correct visual isolation: that live obligation belongs to F2-U4.

Digests establish identity within the caller's evidence chain; they do not
authenticate a reviewer's honesty, licence entitlement or the capture producer.
The caller must obtain those records from authorized intake/capture/review paths.
No raw `vqa.report/v1` or run-bundle parser is implemented here.

`gates/fixtures_negative/registry.json` is a pointer-only original-case ledger.
It contains no asset bytes. Missing identity, conflicted provenance and prohibited
Hunyuan originals remain `CANNOT_SEE`; no synthetic case closes those obligations.
`registry.assess_fixture` admits only verified MIT/TRELLIS evidence, then evaluates
its bound review. `registry.coverage(observations, registry_data=..., registry_sha256=...)`
requires an independently trusted canonical registry pin and revalidates per-case
`request`, `upstream` and `review` evidence. Unchecked result dictionaries cannot
close coverage. `_reduce_unchecked_observations` is ledger arithmetic only.
Host tests prove contract behavior using synthetic RGB and injected scores;
they do not establish live appearance discrimination or release acceptance.
