# W8U canonical run-root contract

Task: `fix-w8u-run-root-contract`, attempt `a1`.

## Required path

For this campaign, a binding must name exactly:

```text
frames_root   = /dev/shm/vqa-ramws
frame_run_root = /dev/shm/vqa-ramws/opensuperficies/os-cu-0/<run_id>
```

The run directory is one direct child of the campaign directory. `run_id`
must be nonempty and contain no path separator, dot or dot-dot component,
or NUL. Existing module-specific identifier and normalization refusals remain
in force. A root or run path must not resolve through a symlink.

When a binding supplies `run_id`, validators derive the expected path from
that identifier and compare it exactly. The two-argument credit root helper,
whose caller supplies no run identifier, requires exactly one nonempty child
component and still checks existing filesystem components for symlinks.

`.../os-cu-0/run/<run_id>` is the obsolete extra-level form and must fail.
A child literally named `run` is valid only when `run_id` is also `run`.
Neither a prefix match nor a matching identifier elsewhere in a descendant
path establishes confinement.

## Source contract

The source binding validator in `vadapt/w8u_binding.py` already derives the
direct-child path. Its implementation is unchanged by this task.

The external contract sources were read at OpenSuperficies commit
`fbf329c14e3e1d9584b52aa716ec9dcbc236288e`:

- [Storage law](https://github.com/oguirado/OpenSuperficies/blob/fbf329c14e3e1d9584b52aa716ec9dcbc236288e/docs/CU_W0.md)
  specifies the campaign's `<run>` directory.
- [Golden vectors](https://github.com/oguirado/OpenSuperficies/blob/fbf329c14e3e1d9584b52aa716ec9dcbc236288e/testdata/w8u/seatstream_vectors.json)
  bind `run_id=w8u-vec-0001` to
  `/dev/shm/vqa-ramws/opensuperficies/os-cu-0/w8u-vec-0001`.

The new regression test reconstructs the vector's exact 835-byte canonical
HELLO body and checks its SHA-256:

```text
5379903b32258ef3fec684578622b9b2c6d2da22b1ed8565d07905f850f7ef08
```

## Implementation

| Module | Change |
|---|---|
| `vadapt/w8u_credits.py` | Removes the extra `run/` prefix, requires one direct child, compares with `run_id` during binding admission, and rejects symlinked components or resolved aliases. |
| `vadapt/w8u_digest.py` | Derives the exact campaign child from the binding's run identifier and rejects nested, mismatched, or symlinked paths. |
| `vadapt/w8u_memory.py` | Removes the extra-level and run-prefix aliases and uses a full identifier match before exact path comparison. |

The existing grant, contract, target, geometry, quota, credit, canonical JSON,
and lifetime checks retain their behavior. Existing negative assertions are
preserved. Positive fixtures use the canonical path; legacy paths are negative
controls.

## Regression coverage

`tests/test_fix_w8u_run_root_contract_unit.py` submits the same golden HELLO
body to `w8u_binding.admit_hello` and `W8UCreditController.receive_hello`.
The injected authority objects are fixed test fixtures, not production
authentication evidence. Both APIs retain the exact canonical body. Binding
stays in `HOLD`; credit admission grants zero credit and allocates no capacity.

Both APIs must refuse the legacy extra level, a nested child, a slash in the
run identifier, a sibling campaign, a mismatched direct child, and a symlink
at the root, tool, campaign, or run component. Additional controls exercise
empty, dot, dot-dot, and NUL children. Existing earlier `E_BINDING` refusals
remain valid where canonical JSON or identifier validation rejects an input
before its root is examined. A refused HELLO leaves the credit controller
unbound with no credit or capacity.

The existing credit, digest, memory, and RAM-policy suites also exercise the
canonical fixtures and root refusals. No native executable, live X server,
or filesystem execute-permission assumption is needed for the combined
admission test. The implementation uses standard APIs available in Python
3.12 and 3.13; the outbox report distinguishes actual runtime verification.

## Separate production defects

The run-root fix does not establish full golden wire-envelope compatibility.
The binding validator and golden vector use `SHA256(canonical_json)`;
credits computes `SHA256(contract.digest_domains["binding"] + canonical_json)`
and requires a nonempty binding domain. Consequently a golden envelope that
advertises the raw hash can fail credit envelope verification after its body
passes root admission. This is reported as `PRODUCTION_DEFECT`; hashing is
outside this task's objective.

The unchanged source snapshot also contains a generation-contract mismatch:
RAM-policy positive fixtures use generation `0`, while stream-bank reservation
requires a positive generation. Five behavior tests fail at that boundary and
the dependent semantic-mutation baseline fails as a sixth error. The outbox
report retains the observed baseline and final gate output. These assertions
are not weakened or bypassed by the run-root change.

The conductor's aarch64 host gate remains authoritative. The pull request is
submitted for collection and does not certify acceptance.
