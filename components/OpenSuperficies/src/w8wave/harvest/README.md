# Harvest orchestration core

`harvest` coordinates a frozen obligation inventory through `Generate`,
`AdmitExecute`, `ExtractVerify`, `Deduplicate`, and `CollectPublish`. It contains
no live runner or service adapter. The host supplies the existing Muse seat
adapter, receipt authenticator, identity normalizer, consumption WAL, and stores
through narrow interfaces.

Protocol versions, source locations, enum values, hash widths, and return codes
below are **CITED** from the task and bound contracts. Attempt limits, local
defensive bounds, and all synthetic fixture quantities are **TARGET**. There are
no production measurements or performance claims in this package.

## Constructing a wave

Call `Freeze` before dispatch. It rejects an empty or duplicate inventory, a zero
obligation weight, missing ordered gate arguments, and invalid commit or digest
bindings. It records a SHA-256 for each exact JSON-encoded field and a seal over
the field-hash map. Accessors return copies. These hashes bind scheduling input;
they are unrelated to repro identity.

Create a `Config` with the frozen wave, a positive `QueueCap` for every stage,
a `WaveDeadline`, and a finite attempt budget. The default and maximum are
**3 [TARGET]** attempts per obligation, including the original. A retry must
name its immediately preceding failed attempt, whose receipt has been collected
and whose purge has been verified. Retries never create obligations.

`MaxCandidates` independently bounds candidate delivery, including rejected and
duplicate candidates. Its default is inventory size multiplied by the attempt
budget. An explicit positive value can allow additional bounded candidate
delivery without increasing the attempt cap.

Every coordinator must share one `WaveBarrier` across its pipelines. An active
wave retains ownership between `Run` calls until all queues close. Concurrent
calls on the same pipeline serialize. After process restart, the host must
reconstruct and resume its active wave before offering another wave; the barrier
is process-local and is not a replacement lifecycle journal.

## Queue and handoff semantics

The cooperative scheduler services downstream work first. A receiving queue
acknowledges a handoff by taking the item before the upstream slot is released.
Every blocked item continues to occupy its queue slot. The generator receives
only its currently available credits and a finite delivery cursor. Excess
candidates are explicitly rejected without submission; an empty unfinished
batch yields a prerequisite HOLD instead of spinning.

`Run` may return with unresolved work or a publication HOLD. Calling it again
reconciles that same work. It does not repeat an external submission. A deadline
stops new generation and sends; already admitted cleanup remains available for
reconciliation. Ports must honor the supplied context. Caller cancellation does
not turn held capacity into verified cleanup.

## Host port responsibilities

| Port | Required contract |
| --- | --- |
| `Generator` | Produce bounded host candidates from the frozen objective; preserve inventory and attempt links. |
| `Submitter` | Adapt the existing `srf-w8-muse` seat path; preserve task, attempt, lease, nonce, input, backend, and resource-policy binding. An accepted submission alone is not counted as observed gate execution. |
| `Extractor` | Authenticate host receipt and grant bytes, A4 observations, absolute freshness, actual branch-manifest evidence, and purge. Trusted proof fields must never come from guest JSON. Query an uncertain stable attempt key even without an execution handle. |
| `Identity` | Supply the C3 canonical bytes and canonical identity under `w8-repro/1`. Harvest validates the returned binding but does not implement another normalizer or repro digest. |
| `Sink` | Durably retain local receipt observations independently of findings; merge only exact canonical bytes with an ordered witness-set union; persist an immutable publication reservation before any external write. |
| `Publisher` | Write only against an injected capability naming a pinned verifier, `os-capsule`, and writer authorization. Reconciliation queries the original publication identity without another write. |
| `Log` / `History` | Use the same existing `wal.LibroConsumo` and its records. Supply a consistent history of intentions, outcomes, and resolutions before dispatch. |
| `Clock` | Supply host time; tests use a synthetic clock. |

The submit envelope rejects guest inference, host mounts, environment files,
runtime sockets, alternate routes, and a process-only backend for a KVM-required
attempt. No fallback adapter exists in this package. An intention is written
before the only submit call. A refused seat or uncertain acknowledgement leaves
`EN_DUDA`, retains capacity, and requires authenticated same-identity resolution.

Existing intentions are query-only during recovery. A changed wave binding or
terminal receipt is quarantined. Bound host receipts are retained immediately,
including when purge is unknown; later receipt status cannot erase a known gate
failure. Known usage conflicts also survive `EN_DUDA` recovery.

## Receipt and identity boundaries

Receipt validation uses the unchanged merged `workerbridge` schema and strict
JSON decoder. It checks task/grant digests; task, attempt, lease and nonce;
repository and commit; ordered gate; runtime and A4 observations; policy limits;
text extract bytes, count and digest; evidence; and verified cleanup. Text
archives remain in memory, with flat safe names and no link entries. A forged
completion flag cannot establish success. A malformed receipt with independently
verified cleanup can be classified as missing evidence, but cannot become a
successful obligation.

The C3 golden fixture is the exact **761-byte [CITED]** canonical JSON record,
without a length prefix or trailing newline. Its SHA-256 is
`4e4d3b0b2679041be017a73ffb043bec9674aecf368dc57392e3b03b5d316934`.
The fixture's source is
`bridge/os-w8d-c3-switchboard-protocol/a1` at commit
`3f1e1319f3bc977968a32f1cf4223583ca9a3a06`,
`contracts/worker/tests/test_w8d_c3_switchboard.py::test_repro_golden_vector`.
It is supplemental contract evidence; it is not an imported unmerged package.

That inherited record has no input digest. `ReproBinding` therefore preserves
input and frame order separately, alongside repository, commit and gate order.
The reference sink uses a reversible address containing the external key,
canonical bytes, and binding. Equal hashes with different bytes occupy separate
buckets. Witness identity includes provenance, outcome and receipt evidence, so
contradictory observations are retained and delivery order does not change the
union. This address is not a replacement repro hash.

`MemorySink` is a reference implementation for fake-based tests. It survives
repeated calls sharing the same instance, but is not durable across process
loss. A production adapter must preserve its reservation acknowledgement
semantics. Once a finding's publication is reserved, additional witnesses remain
in the local union; they cannot mutate or cause replay of that original external
publication.

## Publication and accounting

Local collection precedes live publication. Missing verifier, surface or writer
capability produces prerequisite HOLD, return code **4 [CITED]**. An explicitly
relabeled surface fails verification. Host Muse provenance remains `host-muse`.
A lost external acknowledgement produces return code **6 [CITED]** and the next
call reconciles the same reservation. Even an ambiguous reservation write never
authorizes a repeated publish.

The account always returns the exact frozen obligation inventory. Each member
is `Succeeded`, `ExplainedFailure`, `Unresolved`, or `MissingEvidence`. An
all-failed inventory can be fully accounted and closed while remaining
unsuccessful. A missing inventory member is never silently dropped.

| Field | Meaning |
| --- | --- |
| `GeneratedCandidates` | Every candidate delivered by the generator, including duplicates and refused overproduction. |
| `SubmittedAttempts` | Admitted attempts with a submit call or a recovered external intention; recovered intention is not evidence of gate execution. |
| `ExecutedAttempts` | Unique admitted attempts with a bound host gate observation. |
| `Duplicates` | Suppressed duplicate candidate/attempt deliveries plus repeated exact finding buckets; it never adds paid attempts. |
| `Failures` | Unique admitted attempts with observed failure or terminal invalid evidence, including failed evidence still awaiting cleanup. Pre-admission refusals are explicit `Dispositions`. |
| `Pending` | Queued work plus obligations that have no attempt or disposition evidence. Publication HOLD remains pending after local success. |
| `CapacityHeld` | Admitted attempts whose external execution or purge remains unresolved. |
| `TotalCost` | Sum of known usage once per admitted attempt, including retries. Missing, conflicting, overflowing or mixed-unit usage gives null with `AUSENTE`. |
| `UsefulHarvest` | Verified distinct finding numerator and nullable wall-nanosecond denominator, kept separately. |

The collection interval begins at first admission and ends after every admitted
attempt has verified collection and cleanup. Publication delay does not extend
that interval. A missing endpoint stays null. A zero synthetic interval stays
zero instead of becoming an invented rate. The attempt receipt set and cost
remain independent of finding deduplication.

## Validation

Tests use fake host ports, in-memory consumption records, synthetic clocks and
text fixtures inside this package. The host gate is:

```sh
taskset -c 10-14 env -u OS_CU_TEST go test -race -p 5 -count=1 ./src/w8wave/harvest/...
```

The command's CPU and concurrency values are **CITED** from the task. The task
permits `taskset -c 0` on a VM with fewer than **15 [CITED]** CPUs. Go compilation,
race testing and formatting require a host Go toolchain; the collection report
records the actual observed validation outcome rather than claiming acceptance.
