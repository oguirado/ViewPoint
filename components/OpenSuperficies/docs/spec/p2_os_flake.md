# P2 Area F - frozen farm flake classifier (v1)

This document freezes the **new** public API in `src/w8wave/p2flake/contract.go` for a paired consumer. It is a pure classifier of a host-declared, **fixed** number of comparable trials. It does not execute a farm, authenticate a receipt, mutate custody, admit/release capacity, launch a browser, or assert live host performance. Dependencies are Go standard library plus the already-merged local `workerbridge` codec; there is **no same-wave producer import**.

## Source binding and compatibility

Bound source ref: `bridge/integration` at observed commit `94bafd1d57513712492289096bfe5f67640c4687` on 2026-10-21. Original task metadata names an older observed commit `fd82761bcf0a48df4da798b74ab75d1f9e59195f`; all implementation inputs were read again at the current ref, not silently attributed to the older revision.

- `go.mod:1` declares `opensuperficies.local/srf`; `go.mod:3` requires Go 1.26.0.
- `src/workerbridge/json.go:25-33` defines `*workerbridge.Reject` (`Class`, `Code`, `Detail`); `json.go:41-174` enforces integer-only JSON, duplicate-key and UTF-8 refusals, while `json.go:178-189` exposes `Parse`.
- `src/w8wave/aggregator/aggregator.go:43` defines `HashFunc`; `:324-347` `ComputeIdentity`; `:350-406` `ParseReproRecord`; `:411-420` `ParseAndComputeIdentity`; `:835-853` `MergeManifestations`. Existing aggregator `UNKNOWN_KEY` uses exit code 2 (`:364`), provenance-key refusal code 5 (`:361`), invalid hash code 5 (`:339`). The new API does not replace these receipt parsers or translate their errors.

## Frozen wire projections (order is normative)

**Inventory** schema `p2.farm_inventory/v1`: `schema`, `farm_id`, `repo`, `commit`, `baseline_commit`, `suite_projection_sha256`, `execution_contract_sha256`, `declared_trials`, `required_cases`, `created_by_host`.

Types: identifiers and repo strings; `commit` a 40-character lowercase git SHA; `baseline_commit` a git SHA or explicit JSON `null`; both projection hashes lowercase 64-character SHA-256; `declared_trials` a **positive integer** no greater than 10,000; `required_cases` a nonempty ordered array of distinct case IDs; `created_by_host` a boolean host assertion. Declared trials count **distinct** trial IDs fixed before outcomes, not receipts/attempts. `false` parses but invalidates confidence. The inventory does not include a full manifest digest: full manifest revisions are allowed if and only if the suite and execution projections and ordered required cases remain identical.

**Observation** schema `p2.farm_observation/v1`: `schema`, `farm_id`, `trial_id`, `attempt_id`, `capsule_id`, `incarnation`, `commit`, `receipt_sha256`, `grant_sha256`, `execution_contract_sha256`, `host_validation_ref`, `gate_exit_code`, `case_results`, `cleanup`, `telemetry_ref`, `outcome`.

Types: IDs are nonempty bounded identifiers; incarnation is a positive integer; `receipt_sha256`, `grant_sha256`, and `execution_contract_sha256` are lowercase 64-digit hex strings; `gate_exit_code` is integer 0-255 or explicit JSON `null`; `telemetry_ref` is a nonempty string or explicit JSON `null`; `host_validation_ref` is a nonempty host evidence reference, not an automatically verified signature; `outcome` is `PASS`, `FAIL`, `INFRASTRUCTURE`, or `UNKNOWN`. `case_results` is a JSON array of `{ "case_id": string, "outcome": outcome }` with distinct IDs; complete PASS/FAIL observations must include exactly the inventory's required cases. `cleanup` is `{ "verified": boolean, "state_leaked": boolean }` (both keys required). Nonzero gate exit on PASS, or FAIL without a FAIL case or nonzero gate, is inconsistent and rejected. The two nullable fields above are the **only** nullable observation fields. Parsing refuses unknown keys, duplicates, noncanonical numbers, invalid hashes, types and schemas; no unknown field is silently dropped.

## Go API and ownership

- `ParseInventory([]byte) (Inventory, error)` and `ParseObservation([]byte) (Observation, error)` perform closed-schema strict parsing via `workerbridge.Parse`, preserving the source parser's `*workerbridge.Reject` and exit code for malformed JSON.
- `Classify(current Sample, baseline *Sample) (Result, error)` is deterministic, pure and fail-closed; a rejected call returns the zero `Result`. `Sample` contains `Inventory` and `[]Observation`. `Result` contains `Classification`, `Current Summary`, and optionally `Baseline *Summary`.
- `ClopperPearson95(k,n int) (Interval,error)` inverts **two-sided equal-tailed frequentist** binomial tests with total alpha 0.05. Failure count is `k`; independent comparable PASS+FAIL count is `n`. Reject `n <= 0`, `n > 10000`, or `k` outside `[0,n]`. It does not compute beta posterior intervals.

`Summary` preserves `K`, `N`, `Declared`, `ObservedTrials`, `Missing`, `InfrastructureTrials`, `InfrastructureAttempts`, `UnknownTrials`, `RetriedTrials`, `CorrelatedTrials`, `UnverifiedTrials`, `Interval95`, ordered `Strata`, and sorted `Trials` with **all attempts and exclusion reasons**. Trial IDs, not attempt IDs or receipt digests, form groups. Same receipt SHA across distinct independently evidenced trial IDs is not deduplicated. No trial can exceed the declared population. Shared `(capsule_id,incarnation)`, shared cross-trial `attempt_id`, multiple attempts for one trial, leaked state, unverified cleanup or unattested host inventory invalidate independence and return `INCONCLUSIVE`. Such trials remain visible but never increment valid `n`. `INFRASTRUCTURE`, `UNKNOWN` and missing trials are excluded from valid `n` and reported separately. Missing = declared minus distinct observed trial IDs, regardless of retries. Every entry remains inside its commit / suite / execution stratum; foreign farm, commit or execution projection is refused rather than pooled.

A baseline, when supplied, must match the inventory's exact `baseline_commit`, repo, suite projection, execution projection and ordered required cases. Code commit revision and farm ID may differ; an unrelated execution/suite projection may **not** be called comparable. No baseline is inferred from a missing reference.

Verdict precedence: `(1)` unreliable independence or empty valid population -> `INCONCLUSIVE`; `(2)` mixed current comparable independent PASS+FAIL -> `FLAKE_OBSERVED` (even if other obligations remain missing, which stay explicitly visible); `(3)` incomplete trials/infra/unknown -> `INCOMPLETE`; `(4)` complete all-PASS -> `COMPLETE_PASS`; `(5)` complete all-FAIL with no baseline -> `BASELINE_ABSENT`; `(6)` complete all-FAIL with baseline but unavailable/incomplete baseline evidence -> `OBSERVED_FAILURE`; `(7)` comparable, complete baseline with observed failure -> `EXISTING_FAILURE`; `(8)` comparable, complete all-PASS baseline versus current all-FAIL -> `REGRESSION_OBSERVED`. `REGRESSION_OBSERVED` is comparative **sample evidence**, not proof of a newly caused deterministic defect; all-FAIL is uniform failure *observed in the sample*, not proof flakiness is impossible. The output is never a capacity admission/release decision.

## Numerical and test oracles

For `0 < k < n`, lower bound solves `Pr_p{X >= k} = 0.025`, upper solves `Pr_p{X <= k} = 0.025`. Both are inverted with at most 128 bisection iterations, checked finite domain, normalized binomial mode-centered weights and a post-convergence residual of at most 1e-9. At the boundaries, `k=0` yields `[0, 1-0.025^(1/n)]`; `k=n` yields `[0.025^(1/n), 1]`. For all-PASS, the upper finite-sample bound is **strictly positive**. For no independent observations, the interval is absent, never `[0,0]`.

`TestProto2Unit` contains literal independent controls for `(k,n)=(1,2)`, `(1,3)`, `(2,4)` via hand-expanded binomial polynomials and exact boundary formulas; 50 fixed-seed input permutations; literal expected sample population counts; and rejection controls for foreign commit/projection, missing failure obligations, receipt-hash grouping, retries, leakage, empty population, bad k/n, malformed and duplicate JSON, and baseline mismatches. The test pins 17 positive and 13 negative subtests as a dead-instrument canary. The package has no frame pipeline or `frame_nums` (the separate media fixtures requirement is inapplicable here); no X, network, shell, credentials, disk frames or browser integrations exist in its implementation.

## Evidence and limitations

Host assertions (`created_by_host`, `cleanup.verified`, `host_validation_ref`) are input data to this pure consumer, not independently validated signatures. Statistics apply **only** to the supplied clean independent comparable subset, not to missing/infra outcomes or unobserved future runs. Provenance authenticity, fixed-horizon scheduling, replay resistance, the live launcher, deadline authority and conductor acceptance must be established by the host. The conductor's aarch64 gate, not the PR, is authoritative. Exact requested gate:

```bash
bash -c "env -u OS_CU_TEST taskset -c 10-14 env PATH=/usr/local/go/bin:$PATH bash -euo pipefail -c 'mkdir -p /workspace/.gotmp; export GOTMPDIR=/workspace/.gotmp; go test -list ^TestProto2Unit$ ./src/w8wave/p2flake/ | grep -Fx TestProto2Unit; go test -count=1 ./src/w8wave/p2flake/ -run ^TestProto2Unit$'"
```

When local Go remains 1.23 versus required 1.26, local gate results are **AUSENTE**, not an inferred green. CI bridge-gate evidence, if accessible, belongs in the task's outbox `report.md`, with an exact job/run URL and log tail. No mutation kill rate, CI run, elapsed time or performance measurement is claimed unless observed.
