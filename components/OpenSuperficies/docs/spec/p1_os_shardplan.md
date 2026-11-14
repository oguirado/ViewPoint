# P1 / OS shardplan: frozen-to-registered Muse source boundary

Status: **implementation candidate, not host acceptance**. Source read from
`bridge/integration` at observed commit `1372a80c59c8d3384c64adac49a735f6eb4bffc4`.
The task card embeds an earlier `a5aa4a1a4266222d24bb9151f4fa4af08ac1c87f`
commit; the branch ref was later than that value when read. The branch-ref
observation is the source binding used for this candidate.

## Boundary and exact ordering

`Compile(manifestBytes, Config) (Result, error)` is pure with respect to
publication, lease creation and execution. It reads host-registered briefs,
individual task records and source pins; calls the existing read-only Muse
source `musesource.Build` once for each available shard; then returns literal
compiled queue rows, Muse item bytes, Muse frozen-source manifests, and a
fully sealed suite manifest. The caller is responsible for dispatch and for
holding later waves until the producer-source refresh. It must not treat this
result as a flattened `create_batch` input.

`Manifest` JSON **emission order** is `schema`, `repo`, `source_commit`,
`suite_sha256`, `duration_ledger_sha256`, `planner_version`, `seed`,
`requested_shards`, `selected_case_ids`, `capabilities`, `shards`,
`manifest_sha256`. `Shard` order is `shard_id`, `index`, `resource_class`,
`modules`, `case_ids`, `estimated_duration_ns`, `required_capabilities`,
`exclusive_resources`, `gate_argv`, `expected_outputs`, `field_sha256`.
The schema literal is `proto1.suite-manifest/v1`.

All source SHA-256 commitments are lowercase hexadecimal: commit pins 40
characters; SHA-256 pins and per-field seals 64 characters. `seed`, count,
index and durations are finite nonnegative **exact** JSON integers (bounded
to signed 64-bit arithmetic); no floats, booleans-as-integers, duplicate keys,
unknown fields or JSON encoding ambiguities. Hashing excludes the applicable
digest member and uses sorted-key, compact, integer-only UTF-8 JSON with no
trailing newline, via `workerbridge.Canonical`. The emitted wire order does
not define the hashed order. All ten non-seal shard fields have independent
`field_sha256` seals; all field seals are preserved in the output.

`Config.SuiteJSON` is a **host-bound frozen inventory** with one array named
`modules`. Each complete module has `name`, `resource_class`, `case_ids`,
`required_capabilities`, `exclusive_resources`, `gate_argv` and
`expected_outputs`. `Config.DurationLedgerJSON` has `cases` records of
`case_id` and measured `duration_ns`. SHA-256 of the original raw bytes of
both documents must equal the two manifest pins. The suite partitions whole
modules only; their resource class, capability/resource vector and literal
fixed gate argv must match within a shard. Case IDs are globally unique,
the exact selected-case union is checked, each selected case must have a
measured duration, and the sum is checked with overflow protection. A missing
measurement is a refusal, never an imputed zero. Planner version and seed
are frozen data, not authority to execute commands.

Each ordered `Registration` binds a shard ID to one existing task ID, exact
absolute brief path and SHA-256, absolute output target, and exact individual
relative expected output files. Before any registration lookup, `Compile`
checks manifest/seals/coverage/durations/gate shape/paths/counts. It then
checks the brief and trusted host witness (output safety, capabilities,
exclusive resource and executable scratch evidence), compares the registered
`allowed_write_paths` with the expected output inventory, and invokes the
existing `musesource.Build`. Muse performs its own registration, expected
record and live lease ledger checks, and never fabricates them.

The source item is the existing closed shape `task_id`, `attempt`,
`objective`, optional `patch_path`. It remains **a producer-owned wire
format**; this compiler does not rewrite producer item bytes or source-field
seals. A changed task binding refuses without returning any compiled queue or
item; no compilation call sends, allocates seats, releases leases, or
publishes. `Result.PendingShardIDs` are held, not allocated.

## Literal runner prerequisites and accounting

A Go capsule shard records `Prepare=[['mkdir','-p','/workspace/.gotmp']]`,
`Env=['GOTMPDIR=/workspace/.gotmp']` and a literal fixed argv beginning
`['env','GOTMPDIR=/workspace/.gotmp','go','test',...]`. An executable
`/workspace/.gotmp` witness in the target namespace is required **before**
compilation. A Python capsule shard requires the exact vector
`['python3','-m','unittest', MODULES...]`, plus executable
`/workspace/.pytmp` witness and `TMPDIR=/workspace/.pytmp`. The host must
prepare scratch; this code does **not** claim a sealed runner or fix its
filesystem permissions. Host-only taskset gates start with literal
`['taskset','-c','10-14','go','test',...]` and are never sent to a capsule.
Arguments are vectors, not shell scripts or interpolated arbitrary strings.
A trusted `HostWitness` must establish capabilities and scratch/outputs;
untrusted manifest fields cannot themselves attest host capabilities.

The wave/task inventory cap is 64; the actual concurrent seat cap is **2**.
`Source.MaxInFlight` must be in 1..2 and credits equal
`MaxInFlight - InFlight`, with `WaveOpen` and `SeatReady` as explicit
preconditions. Any remaining shards are held. There is no `MaxWaveTasks`
property in `musesource.Config`. No active lease is minted by shardplan.

Exit outcomes remain: 0 success candidate; 2 invalid inputs; 3 empty/incapable
execution; 4 unavailable trusted capability/registration; 5 test, coverage or
hash verification failure; 6 uncertain cleanup/closed wave/no seat; 7
operator dependency. `Invocation.NativeExitStatus` is a distinct nullable
field. The compiler cannot claim runner evidence from an unset native status.

## Finite budgets and refusal taxonomy

Manifest, source and duration ledger inputs have a 256 KiB budget each;
maximum 64 shards, 256 suite modules, 16,384 duration/selected-case records,
4,096 cases per module, 64 argument fields per command, 2 concurrent seats,
and `musesource.MaxBriefBytes` (64 KiB) per pinned brief. All unmeasured
host time/cost/rate budgets are **TARGET**, never zero measured values.
Missing test/host proof and cost remain null/AUSENTE.

| Refusal | Outcome | Boundary |
|---|---:|---|
| Invalid shape, unknown keys, duplicate case, empty shard, missing duration, impossible shard count, path, mixed module resource or changed fixed gate | 2 | before registration |
| Source, ledger, field or manifest digest mismatch | 5 | before registration |
| Registration or host capability/scratch/output witness missing | 4 | host read prerequisite |
| Already open wave, no free credit or no ready seat | 6 | admission |
| Failed child verification, uncertain cleanup, operator dependency | 5/6/7 | **not** inferred by this compiler; host runner evidence required |

## Evidence and independent controls

The exact test symbol is `TestProto1Shardplan`. A small manually partitioned
fixture has three cases, two **unequal** shard durations (5 ns and 11 ns),
fixed seed 23 and exact selected-case union. SHA-256 expected values are
calculated independently with standard-library `sha256.Sum256` and JSON
encoding; no `SealManifest` round-trip is used to create the test goldens.
Two registration task files, expected records and hash-bound live ledger
events are present in the local test fixture, but the surface must currently
be `chatgpt-mcp` to pass the producer's schema (see defect below).

Nine must-FAIL fixtures test duplicate case, empty shard, missing measured
duration, forged suite seal, unknown capability, impossible count, changed
registered write paths, directory target and nonexecutable scratch evidence.
Five additional strict JSON/path fixtures reject duplicate keys, unknown
fields, floating and Boolean integer substitutions, and traversal paths.
Assertions cover exact refusal code/detail, zero returned queue/items, and
unchanged lease ledger. Build has no send or publisher interface. Barrier
synchronization with fixed seed 23 checks eight concurrent deterministic
compiler calls, without sleeps. Test fixtures/logs contain no frame bytes,
no image persistence and no application launch. No real native runner is
launched; a production external sink/publisher is not part of this API.

**Observed only in an isolated Go 1.23 surrogate**, not in the real repository
with all producer packages: 1 named green control, 1 credit control, 9/9
negative fixtures rejected, 5/5 strict controls rejected, and 8/8 concurrent
outputs equal. The surrogate substitutes the producer packages; it does not
provide revision-bound producer receipts. Mutant **kill rate for the real
repository is AUSENTE** (not 9/9). Survivors/equivalents/invalid controls in
real gate: AUSENTE. Host rerun, host test timing, runner output, allocation,
execution budget and acceptance remain **AUSENTE**. A source-tree test of the
actual bridge/integration code and the exact taskset 10–14 gate is required.

## Source coordinates, limitations and defects

- `src/w8wave/musesource/source.go:27-30`: 256 KiB queue, 64 lines, 64 KiB brief, private 16 MiB ledger.
- `src/w8wave/musesource/source.go:40-53`: producer `Config` fields; `MaxWaveTasks` absent.
- `src/w8wave/musesource/source.go:128-149`: task ID, absolute brief and output paths in one TAB-delimited row.
- `src/w8wave/musesource/source.go:206-272`: strict registered identity, expected record, authenticated matching active lease ledger.
- `src/w8wave/musesource/source.go:276-317`: exact valid source item members.
- `src/w8wave/musesource/source.go:340-413`: separate Muse source item wire and frozen-source bytes; credit/admission checks.
- `src/w8muse/registration.go:59-139`: real Muse live registration/lease checks.
- `src/w8muse/driver.go:18-23`: typed item member order.
- `src/workerbridge/json.go:59-181,196-222`: duplicate-key rejecting integer-only decode and sorted canonical serializer.
- `src/w8wave/harvest/types.go:40-52`: normalized exit code separation.
- `go.mod:1-3`: project module path and Go 1.26.0 requirement.

**PRODUCTION_DEFECT 1 (outside grant, not repaired).**
`src/w8muse/registration.go:80-86` requires `os-capsule` for live jobs, but
`src/workerbridge/worker.schema.json:2489-2500` excludes `os-capsule` from
`bridge_task_v1.surface`. Both `w8muse.loadRegistration` and
`musesource.checkRegistration` validate with `ValidateDef(task,"bridge_task_v1")`
(`registration.go:74-75`, `source.go:212-224`). Therefore an `os-capsule`
live-positive registration cannot be demonstrated against these producers.
The chosen positive control exercises **real registered source compilation**
using an admissible *dry writer* surface only; live admission remains blocked.

**PRODUCTION_DEFECT 2 (outside grant, not repaired).**
`src/w8wave/musesource/source.go:379-395` constructs items using maps, whose
`encoding/json` key order is alphabetical, not the declared struct field
order in `src/w8muse/driver.go:18-23`. Re-encoding item bytes here would
invalidate `wave_sha256` in the frozen-source result (`source.go:399-409`).
This compiler preserves the producer's authoritative bytes and notes the
literal serialized field-order mismatch rather than forging an independent
source seal.

**AUSENTE producer capabilities:** immutable suite inventory production
format, host witness issuer/verification API, suite/duration provenance
signatures beyond pinned raw bytes, a sealed runner scratch fix, an OS-level
expected_outputs consumer and exact host re-run receipts are not defined in
the seven task-bound source files. The `HostWitness` and frozen inventory
formats are newly declared compiler-side contracts, **not claims that
musesource implements them**. The corpus paths cited by the task are external
to the accessible repository: those source lines are AUSENTE, not verified.
