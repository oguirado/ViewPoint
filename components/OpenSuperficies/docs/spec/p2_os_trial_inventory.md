# Area F — PROTO2 trial inventory v1 (frozen producer/consumer contract)

Status: production pure Go implementation and deterministic offline unit suite; **no live farm, runner, admission, disk-frame, queue, launcher, or deadline authority**. The host's aarch64 bridge gate, not this worker, is authoritative for acceptance.

## Bound source and separation

Read the repository at `bridge/integration`, observed commit `94bafd1d57513712492289096bfe5f67640c4687` (task-card metadata previously recorded `cd61dc19a10a06222206031faecd9740ae9f3218`). Reference only: `go.mod:1` declares `opensuperficies.local/srf`; `src/w8wave/aggregator/aggregator.go:43` defines `HashFunc`; `:324` defines `ComputeIdentity(r ReproRecord, hashFn HashFunc) (ReproIdentity, error)`; `:350` defines `ParseReproRecord`; `:411` defines `ParseAndComputeIdentity`; `:835` defines `MergeManifestations`. Aggregator reproduction identity (`w8-repro/1`) is **not** a PROTO2 trial identity. The merged `src/workerbridge/json.go:25-32` defines `*workerbridge.Reject` (`Code`, `Class`, `Detail`) and its `Class: Detail` message. Its `Parse` refuses duplicate JSON keys, malformed UTF-8, non-integer numbers, and trailing data; the Area F parser reuses that real parser, never a fake.

## Ordered immutable wire declarations

`TrialInventory` is `p2.trial_inventory/v1`, with **exact** ordered JSON keys:

`schema,farm_id,repo,commit,baseline_commit,suite_projection_sha256,execution_contract_sha256,declared_trials,required_cases,created_by_host`

`baseline_commit` is explicitly nullable; `commit` and non-null `baseline_commit` are lowercase hex40; both SHA-256 pins are lowercase hex64. Each nonempty declared trial has these exact ordered keys:

`trial_id,shard_id,repetition_id,capsule_class,seed_policy_sha256,expected_case_ids`

A `(shard_id,repetition_id)` pair and a `trial_id` are unique. Expected cases are nonempty, unique subsets of the required set, and their union must cover all required cases. `trial_id` is allocated before any receipt or result. No outcome, receipt, provenance field, attempt ID, or runtime source of nondeterminism enters the declaration identity. Missing keys, extra keys, duplicate keys, wrong order, whitespace, LF, invalid pins, duplicate trials, missing/foreign cases and unattested declarations are refused (no shared null/unknown success). Top-level field order also determines the UTF-8 hash of the exact wire bytes.

`created_by_host` is **not** a guest-controlled boolean: it is an ordered `{host_id,mac_sha256}` claim authenticated by a real HMAC-SHA256 over `"p2.trial_inventory/v1" || NUL || host_id || NUL || unsignedCanonicalInventoryBytes`, with `created_by_host:null` in the unsigned serialization. The host-held ≥32-byte key is injected into `NewHostAuthority` only on the trusted host and is never serialized, embedded in observations, or made available to guest code. Host key provisioning/custody is an external host obligation, not a new runtime capability. A guest-provided `true`, arbitrary tag, absent key, or forged MAC cannot satisfy `ParseTrialInventory`.

`TrialAttempt` adds `attempt_id,capsule_id,incarnation,lease_id,nonce`, without minting another independent repetition. Its typed grant SHA-256 covers `{"trial_id":...,"attempt":{...}}` in that order. A farm observation has the **shared** 16 ordered keys `schema,farm_id,trial_id,attempt_id,capsule_id,incarnation,commit,receipt_sha256,grant_sha256,execution_contract_sha256,host_validation_ref,gate_exit_code,case_results,cleanup,telemetry_ref,outcome` with schema `p2.farm_observation/v1`; it does not mutate to add lease/nonce. The signed observation binds them indirectly through `grant_sha256`. `host_validation_ref` is a trusted-host HMAC of the observation with this field set to `""`; it cannot be a self-certified guest bit. Allowed outcomes are `PASS`, `FAIL`, and explicitly scoped infrastructure `INFRA`. `UNKNOWN` is refused for admission. `case_results` use `{case_id,status,frame_num}`, increasing positive frames, and exact declared-case membership for executed attempts. A passing attempt requires exit 0, every case passing, verified cleanup, and no forbidden I/O. Infra evidence cannot be counted as a pass.

## Two separate pins and baseline projection

`ExecutionContract` freezes `cases,gate_argv,environment,toolchain_sha256,image_sha256,runtime_sha256,capability_class,seed_policy_sha256,isolation_resource_class,intended_commit,full_manifest_sha256` in this order. `FullExecutionPin` hashes **all eleven** serialized fields, including intended code revision and full manifest. `CanonicalComparisonProjection` retains the first nine fields in their original order and **only** excludes `intended_commit` and `full_manifest_sha256`. A comparison also checks the separately pinned `SuiteProjection` (`required_cases,shards`). Different legitimate commits and different full manifests may be comparable; any seed, image, runtime, toolchain, gate argv, environment, capability, isolation/resource class or suite difference is **not** comparable. The signed inventory's two full suite/execution pins are checked against real supplied source objects, not against their own output.

This design treats the comparison projection as an explicit independently inspectable canonical byte string, not as an accidental output of reproduction identity. Test `baseline_comparison_excludes_only_revision_and_manifest` compares it against a literal and independently computed fixed SHA-256 `0a38979691e027339099fb4d74ee291ecab9ef704010f1c6a73e72dfa0592a2a`.

## Public producer API and paired consumer (source coordinates)

See **`src/w8wave/p2trialinventory/contract.go`** at the following implementation line anchors after gofmt:

| Anchor | Frozen API/behavior |
| --- | --- |
| 205 | `NewHostAuthority([]byte) (*HostAuthority,error)`; trusted host key custody |
| 320 | `(*HostAuthority).AttestInventory(TrialInventory,string) ([]byte,error)` |
| 339 | `ParseTrialInventory([]byte,*HostAuthority) (TrialInventory,error)`; strict closed parser |
| 385 | `FullExecutionPin(ExecutionContract) (string,error)` |
| 410 | `CanonicalComparisonProjection(ExecutionContract) ([]byte,error)` |
| 429 | `ComparableExecution(ExecutionContract,ExecutionContract,SuiteProjection,SuiteProjection) (bool,error)` |
| 451 | `SealTrialInventory([]byte,*HostAuthority,ExecutionContract,SuiteProjection) (SealedInventory,error)`; immutable independently signed denominator |
| 560 | `(*HostAuthority).AttestObservation(FarmObservation) ([]byte,error)` |
| 576 | `ParseFarmObservation([]byte,*HostAuthority) (FarmObservation,error)` |
| 603 | `GrantSHA256(string,TrialAttempt) (string,error)` |
| 623 | `ParseFarmBundle([]byte) (FarmBundle,error)` |
| 710 | `ReconcileFarmBundle(SealedInventory,[]byte,*HostAuthority) (FarmReport,error)` |
| 824 | `CompareFarmReports(FarmReport,*FarmReport,ExecutionContract,ExecutionContract,SuiteProjection,SuiteProjection) (string,error)` |
| 862 | `AssertComplete(FarmReport) error` (Area F acceptance gate) |

`p2.farm_bundle/v1` has ordered keys `schema,trial_inventory,execution_contract,suite_projection,deliveries`. Each delivery has ordered keys `trial_id,attempt,observation`. The original host-sealed inventory bytes are compared with the complete bundle, and each raw delivery is passed to `ParseFarmObservation` and checked against `GrantSHA256`. Raw byte identity makes redelivery of an identical attempt idempotent; conflicting bytes against the same `(trial_id,attempt_id)` cause HOLD, never last-write-wins. Reusing one attempt ID under another trial, guest-created attestation, a different farm/commit, incomplete result cases, invalid pin, changed execution contract, or a missing obligation cannot create/release another independent trial.

`FarmReport` always includes every `TrialObligation` from the original seal, including on refusal, with states `MISSING`, `VALID`, `FAILURE`, `FLAKE`, `INFRASTRUCTURE`, or `CONFLICT`; `Declared`, `ValidIndependent`, `Missing`, `Infrastructure`, `Conflicted`, and `Attempts` are separate counters. `Attempts` may exceed `Declared` but **never** changes the independent denominator. An erroneous bundle returns a `FarmReport` with `Hold=true`, `Verdict=INCONCLUSIVE`, every original obligation, and a typed refusal. Exact full pass requires no missing/infra/conflict/flaky/failed obligations and a valid host seal. Verdict vocabulary is `COMPLETE_PASS`, `INCOMPLETE`, `REGRESSION_OBSERVED`, `OBSERVED_FAILURE`, `BASELINE_ABSENT`, `EXISTING_FAILURE`, `FLAKE_OBSERVED`, `INCONCLUSIVE`. Never transform unknown evidence into `COMPLETE_PASS`.

## Refusal representation and exact external controls

`src/workerbridge/json.go:25-32`: errors are `&workerbridge.Reject{Class: ..., Code: ..., Detail: ...}`; `Error()` renders `Class + ": " + Detail`. Parser-level malformed inputs return normalized `Code=2` with classes such as `MALFORMED`, `NONCANONICAL_WIRE`, `INVALID_PIN`, `UNKNOWN_CASE`, `DUPLICATE_TRIAL`, or `UNKNOWN_OUTCOME`; untrusted provenance/obligation conflicts return normalized `Code=5` such as `BAD_HOST_ATTESTATION`, `INVENTORY_CHANGED`, `CONFLICTING_DELIVERY`, `RETRY_AS_INDEPENDENT`, `FOREIGN_OBSERVATION`, `CASE_MEMBERSHIP_MISMATCH`, `EXECUTION_PROJECTION_MISMATCH`, and `INCOMPLETE_TRIAL_POPULATION`. Existing aggregator examples at `src/w8wave/aggregator/aggregator.go:361-364` use `PROVENANCE_IN_IDENTITY` (5) and `UNKNOWN_KEY` (2); `:339` uses `INVALID_HASH` (5). These are aggregator representations, not farm verdicts.

The fixed negative JSON controls expand from the task's base64 templates by changing quoted `Z/O/T/H` into quoted 64-fold `0/1/2/3` strings. The full serialized inventory control has SHA-256 `cc7a8b9e12fb11b1a38a27122dc1241028c3cfa8e306409da24709e69a3ba6e9`; the full observation control has SHA-256 `f645d38f21ee759525d0d419bcc917706852f2d8bc3b1b55864196ef3daa2b4b`. Independent Python stdlib decoding, JSON serialization, and SHA-256 agree; Go tests independently serialize both negative controls and pin exact bytes/digests. Null host claims, empty trials/cases/cleanup, and `UNKNOWN` do **not** establish complete success. Reordered keys, whitespace, or LF alter the hash and are refused by admission.

## Offline tests and explicit limits

`src/w8wave/p2trialinventory/p2_unit_test.go` defines the **exact-name** top-level `TestProto2Unit`. Independent literal truth set: `trial-a1`, `trial-a2`, `trial-b1` (two predeclared repetitions of one shard plus one of another); deliveries arrive `b1,a2,a1,retry`, with an idempotent exact redelivery and a fourth attempt that never inflates `Declared=3`. Tests cover legitimate separate commits, changed seed/image/resource, removed trial and expected case, duplicate conflicts, retry-as-trial, untrusted/foreign attestations, missing/infra results, flake, malformed pins, unknown cases, and the shared frozen vectors. Tests use real Go JSON, hashing, custody HMACs and reducer paths; no mock parser/reducer/hashing/provenance and no filesystem/network/media calls. Production code imports only Go stdlib and the pre-existing merged `workerbridge` parser.

Exact host gate (from repository root, host workspace available):

```sh
bash -c "env -u OS_CU_TEST taskset -c 10-14 env PATH=/usr/local/go/bin:$PATH bash -euo pipefail -c 'mkdir -p /workspace/.gotmp; export GOTMPDIR=/workspace/.gotmp; go test -list ^TestProto2Unit$ ./src/w8wave/p2trialinventory/ | grep -Fx TestProto2Unit; go test -count=1 ./src/w8wave/p2trialinventory/ -run ^TestProto2Unit$'"
```

The local sandbox is Linux amd64 with Go 1.23.2; `go.mod` demands Go 1.26.0, `/workspace` is unavailable, and `taskset -c 10-14` failed `Invalid argument`. Therefore a local host-equivalent gate, revision-bound aarch64 live test, host-inference timing, deadline/launcher authority, and semantic-mutation killed/valid denominators are **AUSENTE**; no fake green claim. GitHub Actions `bridge-gate` on this branch's PR is the available real CI evidence if the runner permits it. Never interpret a PR being open as host acceptance.
