# P1 VA shard manifest — deterministic A-manifest planner

Status: **TARGET** for downstream admission; this document specifies the implementation
in `eval/v2eval/proto1_shard_manifest.py`. It does **not** assert an authoritative
receipt, harvest, certificate, capsule gate execution or consumer merge.

## Boundary and Python API

```python
from eval.v2eval.proto1_shard_manifest import build_manifest, verify_manifest, ManifestRefusal
plan = build_manifest(request, source_files=current_source_bytes)
assert verify_manifest(plan.manifest, request, source_files=current_source_bytes) == plan
```

- `request` is a `dict`, UTF-8 JSON `str`, or UTF-8 JSON `bytes`. All input keys
  are mandatory; unknown keys and duplicate JSON keys refuse admission.
- `source_files: Mapping[str, bytes]` supplies the **actual, current bytes** of
  every selected inventory path. Exactly the inventory paths must be supplied.
  The host must read this snapshot from its checkout, **not** from claimed
  inventory hashes. The planner hashes actual bytes; a modified source with
  unchanged cases/duration and an old source seal refuses.
- `build_manifest` returns frozen `ManifestPlan(manifest: bytes,
  longest_module_lower_bound_ns, average_load_lower_bound_ns,
  makespan_lower_bound_ns, estimated_makespan_ns, oversized_modules)`.
  No files, jobs, reservations or publications are created by these functions.
- `verify_manifest` requires an exact byte match to a freshly re-admitted plan;
  a valid-looking JSON document is not sufficient. It rejects duplicate keys.
- Failure is a `ManifestRefusal` with stable `.code`, `.detail` and
  `.blocked_case_ids` (empty except blocked live-capability obligations).

## Request, identity and field order

Required request fields (input JSON order arbitrary):

| Field | Type / interpretation |
| --- | --- |
| `repo` | Literal `vision-engine` |
| `source_commit` | Lowercase 40-character hexadecimal source commit, supplied by host |
| `seed` | Exact integer in `[0, 2**63-1]`; recorded, **not** used for randomized scheduling |
| `requested_shards` | Explicit exact integer 1–64; no default |
| `selected_case_ids` | Array of discovered `tests.module.Class.test_method` IDs; exact and disjoint |
| `inventory` | Array of `{module,path,source_sha256,case_ids,resource_class,required_capabilities,exclusive_resources}` |
| `suite_sha256` | Canonical identity of selected cases, file hashes, classes and inventory |
| `duration_ledger` | `{"samples":[{"sample_id","module","duration_ns","source_sha256"},...]}` (object notation shows required keys) |
| `duration_ledger_sha256` | Canonical identity of *all actual* duration sample records |
| `available_capabilities` | Explicit currently live capability labels |
| `shard_slots` | Array of `{slot_id,resource_class,capabilities,exclusive_resources}`, length exactly `requested_shards` |

Python unittest modules are indivisible and match canonical `tests/**.py` paths.
`case_ids` must be of the form `module.Class.test_method`; at least one case
per module is required. Module path, module name and case IDs must correspond.
All capability, class, slot, sample and exclusive-resource labels use lowercase
ASCII `[a-z][a-z0-9_-]*`. `source_sha256` is lowercase hex64. Source snapshot
paths cannot use `..`, `.` segments, backslashes, or noncanonical imports.

Suite identity is the SHA-256 of sorted/compact JSON with keys `modules`,
`repo`, `selected_case_ids`, `source_commit`. Each module record contains the
seven inventory fields above, with arrays sorted. The source snapshot itself
must independently hash to each module's declared `source_sha256`. Ledger
identity is the SHA-256 of `{"samples":[...]}`, samples sorted by `(module,
sample_id)`, including every individual sample's integer nanoseconds and
source SHA. Sample IDs are unique. Multiple samples per module yield an integer
median, rounded up for an even sample count; absent/zero/negative/float/bool
durations refuse. Nanoseconds may not exceed 2**63-1 per sample or shard.

The **manifest wire key order** is exactly:

`schema, repo, source_commit, suite_sha256, duration_ledger_sha256, planner_version, seed, requested_shards, selected_case_ids, capabilities, shards, manifest_sha256`.

Schema is `proto1.suite-manifest/v1`; `planner_version` is `p1-va-lpt/v1`.
Each shard's wire field order is exactly:

`shard_id, index, resource_class, modules, case_ids, estimated_duration_ns, required_capabilities, exclusive_resources, gate_argv, expected_outputs, field_sha256`.

`shard_id` is `shard-000`, `shard-001`, ... in canonical slot-ID order;
`index` begins at zero. `gate_argv` is **literally** `["python3","-m",
"unittest", ...modules]`, with module names sorted. `expected_outputs` is a
single prospective `artifacts/p1_va/shard-NNN/receipt.json` path; declaring it
does not create it or provide an authoritative receipt.

**Hash domain:** SHA-256 of UTF-8 JSON with *all object keys sorted*, compact
separators `,` and `:`, integer-only exact JSON types, no newline, and no
`ensure_ascii` escaping. Shard `field_sha256` hashes the shard **excluding its
own** digest; root `manifest_sha256` hashes the root **excluding its own**
digest, but including every shard's `field_sha256`. The emitted wire JSON is
also compact UTF-8 and has no trailing newline; it preserves the prescribed
wire field order, rather than alphabetically sorting its fields. This is
intentionally distinct from `tools.gates.canon`.

## Scheduling / bounds

Sort modules by decreasing integer median duration, breaking ties lexically by
module. A module fits a slot only when its `resource_class` is equal, its
required capabilities are included in the slot capabilities, and its exclusive
resources are included in that slot's exclusive resources. Exclusive physical
resources may be owned by only one slot. Assign each module to the compatible
slot of least current nanosecond load, with canonical slot-index tie break.
Refuse infeasible layouts and *any* empty shard. `estimated_duration_ns` sums
complete module estimates, not individual cases. Thus module and class fixtures
remain intact. The response exposes `max(module duration)` and
`ceil(total duration/requested_shards)` lower bounds, their maximum, and
modules larger than the average lower bound. These are **lower bounds**, not
claimed optimal makespans or timings measured on the host.

A missing live capability is `E_CAPABILITY_BLOCKED`, with all affected case IDs
in `blocked_case_ids`; it never turns into passed or skipped test coverage.
Only the host produces patches. The capsules may later execute the declared
`gate_argv`, but this module neither executes gates nor certifies acceptance.

## Admission refusal codes

`E_JSON` malformed/invalid UTF-8 JSON; `E_DUPLICATE_KEY` repeated JSON key;
`E_TYPE` invalid strict types, booleans/floats, labels or hex; `E_RANGE` numeric
bounds; `E_UNKNOWN_FIELD` unexpected key; `E_MISSING_FIELD` absent key;
`E_PATH` noncanonical test module/path; `E_REPO` wrong repository;
`E_EMPTY` empty discovery; `E_DUPLICATE_MODULE`, `E_DUPLICATE_CASE`,
`E_DUPLICATE_SAMPLE`, `E_DUPLICATE_SLOT`, `E_DUPLICATE_VALUE` duplicate identity;
`E_CASE_COVERAGE` missing, foreign or malformed selected case;
`E_SOURCE_SNAPSHOT` incomplete/extraneous raw-source snapshot;
`E_SOURCE_MISMATCH` current source bytes differ from recorded SHA;
`E_SUITE_SEAL` inventory/selection/source seal mismatch;
`E_DURATION_MISSING` incomplete measured durations; `E_LEDGER_SOURCE`
sample source hash missing/mismatched to inventory; `E_LEDGER_SEAL` actual
sample records changed without seal update; `E_SHARDS` unavailable slot count;
`E_SHARDS_EMPTY` empty actual partition; `E_RESOURCE_CONFLICT` duplicated
physical resource owner; `E_RESOURCE_INCOMPATIBLE` no compatible slot;
`E_CAPABILITY_BLOCKED` unavailable required live capability;
`E_MANIFEST_TAMPER` altered, extra, omitted or noncanonical manifest fields/bytes.
All refusals precede any manifest return and make no filesystem or registration
mutations. A failed request does **not** produce partial coverage.

## Bound source evidence — bridge/integration

**CITED:** read directly using the GitHub connector at `bridge/integration`
(initially `f998fb90cec9b51b2d83991f1843e7d8773607eb` and then
`fc20cdb0b92b8f5953d9f1fa4e0e90032087120d`, the branch creation base;
all three bound Git blobs were byte-identical across both refs. The task
metadata had older commit `b599f4c0e43d5f9bdb2bbc814f7a910db9dc710b`).

- `tools/gates.py:46`: `def canon(o):`, whose body on line 47 emits sorted,
  compact, UTF-8-preserving JSON **with** a final newline.
- `tools/gates.py:650`: `def run_gates(root, names=None):` returns gate codes
  and messages; on exceptions the gate reports `E_GATE`.
- `tools/gates.py:662`: `def receipt(res):` returns VG pass/fail summary,
  not a proto1 certificate.
- `eval/v2eval/run.py:37`: `def verify_source_seal(dev):` validates existing
  benchmark source sealing; this new planner does not import that code.
- `eval/v2eval/run.py:48`: `def write_json(path, data):` emits indented JSON
  with a newline; that format must not be used for proto1 digests.
- `tests/test_v2eval/test_evaluation.py:19`:
  `class NativeDifferential(unittest.TestCase):` — examples of existing
  class-level unittest collection to preserve in unsplit modules.

**MEASURED (isolated VM fixture, not full-repo CI):** the exact command
`python3 -m unittest tests.test_p1_va_shard_manifest_unit` reports 26 tests,
zero failures/errors/skips; three deliberately invalid semantic mutants yield
three assertion-based kills out of three applicable, followed by a healthy
reproof. Actual full-checkout and authoritative capsule receipts are **AUSENTE**.
