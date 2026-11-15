# C2 — Lease, admission and supervision contract

Status: **[TARGET] design and executable reference contract**, ready for implementation review. This change does not implement a Go service, run a capsule, certify native window parity, measure a host knee, or authorize an image build. The runtime remains Go; Rust requires a separately measured hot path; this design selects an existing VMM and creates none. These boundaries follow `docs/design/W8_WORKER_BANK.md:28-34`, `docs/design/W8_WORKER_BANK.md:298-306` and `AGENTS.md:12-16`.

## Source binding and precedence

Every repository citation in this document is relative to `oguirado/OpenSuperficies` at **`c8cb7dc8c0946be08edcb2a419629b6debd33c96`**, the commit resolved from `bridge/integration` for this task. References inside a pack to host files or earlier source revisions are the pack's reported evidence; they are not independently inspected implementations. Absolute campaign paths are provenance, not inputs to open (`docs/design/w8d/README.md:25`).

Numeric provenance: **[CITED]** identifies a requirement or observation in a bound source; **[TARGET]** identifies a proposed setting, invariant, formula, synthetic fixture, or acceptance condition. All numeric literals in the proposed interfaces, equations and fixture descriptions below are **[TARGET]** unless explicitly marked **[CITED]**. Identifiers, versions, hashes, citation line numbers and question labels are identifiers, not measurements. **[MEASURED] W8 host results are ABSENT** here. Test execution proves only the stated model behavior; environment timing is not W8 performance evidence (`AGENTS.md:11`; `docs/design/W8_WORKER_BANK.md:296-300`).

A4 overrides the packs and router; the unchanged W8 v3 document and worker schema follow it. The precedence is explicit in `docs/design/w8d/README.md:7-13`. Preserve the task's portable fields and verbatim bridge payload; never add control-plane fields to those closed objects (`docs/design/W8_WORKER_BANK.md:34`, `docs/design/W8_WORKER_BANK.md:151-185`; `contracts/worker/os.cu.worker-task.v1.schema.json:5`).

| Input conflict | C2 decision |
|---|---|
| Per-commit images in the router and earlier amendments | Replace them with the single hardened base, verified per-task tmpfs clone and read-only dependency-cache manifest. No image-build amortization in scheduling or cost. A4 wins (`docs/design/w8d/W8_AMENDMENTS.md:22-27`; `docs/design/W8_WORKER_BANK.md:312-320`). |
| D8 proposes `STOP = RevocarCapsula` | A target STOP calls a destination-scoped extension of the existing lessor. It never revokes siblings (`docs/design/w8d/packs/D8.md:13-15`, `docs/design/w8d/packs/D8.md:51`). |
| D3 treats broad return-code classes as transient and suggests VMM/runner restarts | Use the typed W8 outcome adapter below; no runtime auto-restart or replay of an uncertain gate. Preserve the existing `restart: no` launch rule (`docs/design/w8d/packs/D3.md:7-9`; `contracts/worker/os.cu.worker-task.v1.schema.json:3773-3775`). |
| D3 mixes restart and attempt limits | Separate lifetime task attempts from host-controller restart intensity; neither supervisor death nor a new lease resets the former (`docs/design/w8d/packs/D3.md:7-9`, `docs/design/w8d/packs/D3.md:38`). |
| Legacy microVM ceiling and CDP warmup evidence | Introduce a host-owned W8 configuration in the existing governor, with backend-specific readiness evidence and resource-derived ceilings (`docs/design/w8d/packs/D2.md:30-40`; `docs/design/w8d/synth/S3_FOLDIN.md:9`). |

## Shared public lease and host control record

### Public projection

Keep `os.cu.lease/v1` byte-compatible with `$defs.scheduler_lease`. Its fields are exactly `schema`, `lease_id`, `target_id`, `target_kind`, `state`, `restarts`, `receipt`. Kinds remain `capsule`, `microvm`, `browser_window`; `worker_ui` is a profile on a capsule or microVM, not a new kind. States remain `QUEUED`, `ADMITTED`, `STARTING`, `RUNNING`, `GATING`, `COLLECTING`, `STOPPING`, `SUCCEEDED`, `FAILED`, `REFUSED`, `INDETERMINATE`. A known terminal state requires the existing receipt reference; active states require null; `INDETERMINATE` permits null or a reference. Restarts are the schema's bounded projection of task replacements, not controller restarts (**[CITED]** `contracts/worker/os.cu.worker-task.v1.schema.json:2877-2994`).

`lease_id` is an opaque identifier, never a serialization of an authentication token. `target_id` is `tgt-` plus the canonical digest of the registered logical identity: host identity, outer runtime instance and destination slot. A browser destination additionally binds the broker-owned browser instance. It is stable across replacement of an inner process in the same slot; `instance_generation` distinguishes those replacements. Recreating the outer runtime creates new logical targets. Paths, PIDs, tab titles, guest CIDs and model-supplied addresses are not target identities. The identifier fits the unchanged grammar (**[CITED]** `contracts/worker/os.cu.worker-task.v1.schema.json:15-31`).

### Host-owned record

Extend the authoritative records and WAL used by `SrfArrendador` and `SrfGobernador`; do not create another lease authority. The public lease is a projection, not a second mutable source of truth. Reuse is required by `docs/design/W8_WORKER_BANK.md:332`; the packs report existing lease, WAL, governor and lifecycle components at `docs/design/w8d/packs/D8.md:29-37` and `docs/design/w8d/packs/D3.md:29-34`.

| Host record group | Required fields and meaning [TARGET] |
|---|---|
| Identity | `chain_id`, immutable task digest, `task_id`, `attempt_id`, `admission_request_id`, `lease_id`, `target_id`, owner, backend/profile, policy digest |
| Fence | destination `epoch`, `instance_generation`, host boot ID, grant digest, absolute expiry, original monotonic origin/deadline; no serialized raw capability |
| Placement | outer runtime immutable ID, cohort ID, destination slot, direct cgroup owner, expected cgroup identity, effective ancestry and limit digest |
| Process | runtime ID, host boot ID, PID and start ticks, verified pidfd while live, browser instance/session/tab binding where applicable |
| Holds | resource owner IDs, CPU/RAM/PID ceilings, startup permit, terminal-receipt slot, cost reservations, immutable cache/prefix pin IDs |
| Recovery | admission transaction ID, launch intent ID, action intent IDs and dispatch states, cleanup phases/evidence, release keys, committed WAL sequence |
| History | durable total attempts, poison signature/streak, controller restart window, immutable attempt outcomes, provider request liabilities and settlements |
| Delivery | receipt ID/digest, target/attempt/epoch binding, authenticated ACK identity and committed delivery state |

Canonical hashing uses the schema's integer-only, sorted compact UTF-8 JSON rules (**[CITED]** `contracts/worker/os.cu.worker-task.v1.schema.json:5`). Domain-separated arrays prevent concatenation collisions. Full task receipts and message bodies remain unchanged payloads. Raw capabilities stay in trusted host memory and are omitted from JSON, logs and WAL; guest-visible handles confer no host authority. The pack records host-side fencing and non-journaled raw tokens, but does not establish a complete unified client (`docs/design/w8d/packs/D8.md:13`, `docs/design/w8d/packs/D8.md:31-37`).

### Proposed Go facade [TARGET]

These are contract signatures to add around the existing components, not claims of existing exported Go symbols. The structs named in the signatures are closed host records defined by the tables and preconditions in this document.

```go
type W8Control interface {
    Admit(context.Context, AdmissionRequest) (LeaseView, Outcome)
    Start(context.Context, LeaseRef, LaunchIntentID) (LeaseView, Outcome)
    Inspect(context.Context, LeaseRef) (Inspection, Outcome)
    Actuate(context.Context, LeaseRef, ActionIntent) (ActionResult, Outcome)
    Stop(context.Context, LeaseRef, StopRequestID) (LeaseView, Outcome)
    Collect(context.Context, LeaseRef, CollectRequest) (ReceiptEnvelope, Outcome)
    Acknowledge(context.Context, ReceiptACK) Outcome
    Reconcile(context.Context, OwnershipRef) (Reconciliation, Outcome)
    Release(context.Context, OwnershipRef, CleanupProof) Outcome
}
type LeaseRef struct {
    LeaseID, TargetID, AttemptID string
    Epoch, InstanceGeneration uint64
}
type TargetBackend interface {
    Prepare(context.Context, OwnedLaunch) (PreparedTarget, Outcome)
    Start(context.Context, PreparedTarget) (LaunchObservation, Outcome)
    Inspect(context.Context, OwnershipRef) (Inspection, Outcome)
    Stop(context.Context, OwnershipRef) (StopObservation, Outcome)
    Collect(context.Context, OwnershipRef, ByteDeadlineBudget) (TextExtract, Outcome)
}
```

The coordinator authenticates the host caller separately from `LeaseRef`. Capsule, UI and browser backends use the same calls. Backend adaptation follows the existing `prepare/start/inspect/stop/collect` design (**[CITED]** `docs/design/W8_WORKER_BANK.md:189`). No arbitrary shell, address or caller-supplied runtime path enters a backend operation.

## Destination fencing and lifecycle

The lessor gains `RevocarDestino(capsule, destination, expected_epoch, stop_request_id)`. Under the destination's serialization boundary it authenticates the caller, then resolves any durable duplicate STOP with the identical bound request body before comparing the now-advanced epoch. That replay returns its original committed result without another increment or restoration of authority; a changed body is refused. A fresh STOP validates the exact current lease generation, durably increments only that destination's epoch, zeros its expiry, tombstones pending messages/actions and records the STOP intent. No normal target path calls capsule-wide revoke, outer `cgroup.kill`, or a browser-wide close. Sibling epochs, capabilities, reservations, credit counters and receipts remain unchanged. This corrects `docs/design/w8d/packs/D8.md:13` using its explicit destination boundary and fault-isolation requirement (`docs/design/w8d/packs/D8.md:15-19`, `docs/design/w8d/packs/D8.md:50-54`).

`Actuate` performs destination, generation, current epoch, expiry, host capability and closed-operation checks under the same serialization boundary as STOP. It persists an action intent and checks the fence again at the trusted actuator's dispatch point. STOP must not wait on a guest's data queue or an unbounded network send. A command already sent before STOP can have an uncertain effect; fencing does not undo a physical action. Persist `SENT_UNKNOWN`, reject automatic replay and freeze reassignment until inspection/reconciliation establishes its disposition. The target cannot be reassigned between the fence check and authoritative actuator dispatch. Storage writes and memory publication use the same compare-and-commit epoch check, closing the stale-check/late-write race. D8 supplies the epoch gate and tombstone/single-completion basis (`docs/design/w8d/packs/D8.md:15-21`).

| Current state | Permitted next work [TARGET] |
|---|---|
| `QUEUED` | Commit complete admission or produce a truthful refusal; pressure leaves the queued task intact. |
| `ADMITTED` | Claim the committed launch intent; no unreserved launch. |
| `STARTING` | Verify runtime identity, containment and ready acknowledgement; unknown creation quarantines. |
| `RUNNING` / `GATING` | Bounded work under the same lease deadline; enter collection on known completion or STOP. |
| `COLLECTING` | Extract already available bounded text while preserving independent STOP; never extend expiry. |
| `STOPPING` | Fence, terminate the owned destination, purge and verify ownership closure. |
| `SUCCEEDED` / `FAILED` / `REFUSED` | Immutable outcome plus durable terminal receipt; transport retries resend that receipt only. |
| `INDETERMINATE` | No actuation, automatic task retry or uncertain release. Inspection and authorized reconciliation only. |

Any active state can enter `STOPPING`; any uncertainty can enter `INDETERMINATE`. A task never returns to `QUEUED` by resetting its existing attempt. A retry has new attempt, lease, launch and paid-request identities while retaining its chain history. Local known outcomes require verified cleanup before their final receipt; this follows the unchanged receipt rules (**[CITED]** `contracts/worker/os.cu.worker-task.v1.schema.json:2317-2340`).

## Atomic admission, launch and crash recovery

### CommitAdmission [TARGET]

Use a serializable transaction in the existing host WAL authority. A single committed record binds **placement, resource reservations, startup and receipt permits, lease grant, destination epoch, cost reservations, cache pins and launch intent**. Separate successful calls to `Admitir` and `Conceder` are insufficient: a crash between them could leak or duplicate ownership. The existing WAL/lessor are reported in `docs/design/w8d/packs/D8.md:31-33`; fail-closed redo and in-doubt recovery are reported in `docs/design/w8d/packs/D3.md:11`, `docs/design/w8d/packs/D3.md:25-26`, `docs/design/w8d/packs/D3.md:32`.

Authenticate the caller, then consult the durable `admission_request_id`. An identical authorized request body returns its previously committed admission result without re-running fresh-admission gates; a changed body returns rc **[CITED] 2**, `contract`, with no mutation. Replay does not renew expiry, restore revoked authority or authorize a new launch. Only an unseen request validates, in order:

- The immutable task/grant, eligible verified commit, hardened backend qualification and finite adapter capability record.
- The chain's attempt cap and absorbing halt, then dependency readiness, target availability, snapshot freshness and integral placement.
- Every resource ancestor and host reserve, startup availability, receipt capacity, cost liability and remaining tail deadline.
- The uniqueness of `(chain_id, attempt_id)` and the new `admission_request_id`.

It then writes the entire intention and commit marker, flushes before acknowledging, and installs only the committed state in the lessor/governor views. Overflow or a failed check leaves all views byte-identical. Torn/uncommitted records grant nothing and cannot have launched, because the actuator consumes committed intent IDs only. WAL uncertainty stops dispatch; it is not repaired by creating an independent grant.

The launch worker durably claims `launch_intent_id` before invoking the backend. Deterministic runtime labels bind that ID, target and generation. A crash after `CLAIMED` is reconciled by listing/inspecting owned runtime identity, not by invoking Start again. Exactly matching live instance: adopt inspection and continue idempotent lifecycle work under a valid fence. Definitive no-create evidence: close the old intent and admit a fresh attempt if policy permits. Missing/contradictory evidence: retain the holds and quarantine. A receipt timeout is not proof that creation failed. This provides replay-safe grants and launch decisions, not an impossible atomic transaction with an external daemon.

If the lease authority loses in-memory authentication state, first fence affected destinations through WAL recovery and reconcile their instances. Do not regenerate an old capability or reset an epoch. A reconciled control generation may reattach to an already-created instance without another launch; if it cannot establish authority, quarantine. Checked epoch/generation exhaustion refuses further grants rather than wrapping.

| Crash boundary | Recovery result [TARGET] |
|---|---|
| Before admission commit | No grant or resource credit change; retrying the same request is safe. |
| After commit, before response | Same admission request returns the existing lease and reservations. |
| Before launch claim | The unclaimed committed intent may be claimed once. |
| After claim, before/after daemon reply | Inspect exact identity; adopt or quarantine. Never blindly launch again. |
| After action send, before outcome commit | `SENT_UNKNOWN`; no replay of the action. |
| During release intent or purge | Keep holds, repeat owned inspection/cleanup by the same key. |
| After purge, before release commit | Reverify physical proof, then commit the credit once. |
| After release commit, before reply | Replay returns the existing release result; no second credit. |

## Ownership, purge, reassignment and release

Physical destruction cannot be atomic with a WAL write. Make the **credit transition** atomic and idempotent, and bridge it to physical cleanup with a durable intent and proof. `release_key = H(["w8-release", admission_tx_id, resource_owner_id, instance_generation])` is minted at admission and survives both supervisor and janitor death. Only the authority that owns the reservation can apply it. Purge intent, cleanup evidence, resource-credit delta and `release_done` are tied to that key. `release_done` and the credit delta commit together; an equal key returns the prior result, and an unequal key cannot release the same owner twice. The source requires idempotent reconciliation and forbids returning quarantined capacity (`docs/design/w8d/packs/D3.md:11`, `docs/design/w8d/packs/D3.md:25-27`, `docs/design/w8d/packs/D3.md:33`, `docs/design/w8d/packs/D3.md:44`, `docs/design/w8d/packs/D3.md:53`).

Cleanup proof binds the exact runtime ID, boot ID, PID start ticks, cgroup identity/generation, workspace generation, private socket/relay namespace, guest lifecycle and remaining cache references. Use pidfds; after a restart reacquire one only after matching boot/PID/start-time identity. A reused PID, cgroup path, container name, tab ID or slot is not evidence of ownership. Parse process start ticks after the final `)` in `/proc/.../stat`; do not kill from an unvalidated integer PID (`docs/design/w8d/packs/D3.md:22`, `docs/design/w8d/packs/D3.md:39`).

The proof requires the owned child cgroup empty, guest stopped, no orphan, private relay drained/closed, tmpfs purged, host rule ownership removed, no unresolved actuator action and no remaining reference to each cache pin being released. Process exit alone does not purge tmpfs charges (`docs/design/w8d/research/R3.md:48-56`). A known-empty process with a nonempty workspace retains RAM. Unknown remote billing retains its cost hold independently; it does not retain physically released capsule RAM without a resource reason.

### Multi-target cohort rule [TARGET]

Choose **pre-reserved outer envelopes**. At the parent/host ledger, charge an outer envelope once. Inside it, fixed outer overhead plus every child reservation must fit; children are not charged again at the parent. Shared cache pages have a separate named owner. Releasing a child frees its suballocation only. Releasing the outer envelope requires every child generation resolved, private obligations closed, and the outer runtime itself torn down. A sibling that is still running or indeterminate prevents outer release, not another child's safe stop.

Default qualification permits an inner count of **[TARGET] 1**. Multiple inner VMs are admissible only after the chosen daemon's trusted host controller proves separate child cgroups, membership-before-allocation, effective CPU/memory/PID limits and child-only teardown without any cgroup bind inside the capsule. A rootless container's parent limit alone is not proof of child limits. Missing delegation yields rc **[CITED] 4**, `unavailable`; never add privileges, a host mount or an extra device. The chosen daemon and delegation must be measured, as required by `docs/design/W8_WORKER_BANK.md:65`, `docs/design/W8_WORKER_BANK.md:74`, `docs/design/W8_WORKER_BANK.md:234`.

For interrupted duplicate assignment, commit `DuplicateInterrupted(old_attempt, successor_attempt, reason, old_epoch, fence_epoch)` before starting any replacement. Preserve the interrupted attempt's actual failure/uncertainty, resources and paid liability. Reassignment of the same destination requires proved old-instance closure. A separately authorized replacement on a different clean destination requires additional reservations while the old hold remains; uncertainty never becomes free capacity. Replacement success changes the chain's useful-success view, not the old attempt's history. No automatic replacement of an indeterminate action is authorized by this design.

## W8 governor configuration and resource arithmetic

The following **[TARGET]** host configuration replaces the legacy hard count for the W8 profile only. It extends the existing governor; other profiles retain their policy. The bound pack reports a legacy microVM cap of **[CITED] 2** and telemetry/identity/latency guards (`docs/design/w8d/packs/D2.md:30`, `docs/design/w8d/packs/D2.md:36-40`). C2 does not claim to have read that Go implementation.

| Setting | Proposed value or rule [TARGET] |
|---|---|
| `integer_max` | Signed integral accounting bounded by `2^63-1`; all sums, products and ceil divisions checked. |
| `resident_limit` | `16`, additionally bounded by operator topology and resources. |
| `active_limit` | Minimum of operator cap, qualified measured operating point and feasible resource placement; unqualified production cap `1`. |
| `startup_permits` | `1` globally during initial qualification; larger values need an explicit measured configuration revision. |
| `cpu_period_us` | `100000`; quota budgets are normalized conservatively to this period. |
| `telemetry_max_age_ms` | `1000`, host-monotonic age; missing, regressed or stale data closes admission. |
| `memory_high` | `9/10` of each finite `memory.max`; this is pressure control, not allocatable extra memory. |
| `swap_max` | `0` (**[CITED]** A4/v3 requirement, `docs/design/W8_WORKER_BANK.md:68`). |
| CPU PSI close / reopen | `2000` / `500` basis points; reopen after `3` consecutive fresh clear samples. No PSI threshold overrides a hard capacity or memory-event failure. |
| Readiness qualification | `8` valid backend-specific ready samples; bounded pilot concurrency `1`, with every other guard active. |
| Queue service | Deterministic round robin by repository/seat, FIFO within each queue, bounded quantum; unmet dependencies and cycles cannot launch. |
| Queue bounds | Required finite `queue_capacity` and `queue_wait_ms`, selected from the service/tail budget; no implicit unlimited queue. |
| Byte capacities | Per-attempt evidence `102400` bytes (**[CITED]** `docs/design/W8_WORKER_BANK.md:147`); global receipt slots `32`, per-target data slots `2`, plus independent terminal and STOP capacity. |

`latency_stream` is `capsule_ready`, `guest_ready` or `browser_ready`, selected by backend. Headless readiness cannot be refused merely because no CDP sample exists, nor can missing CDP data be treated as healthy latency. The pilot gathers the correct readiness stream. It cannot bypass CPU, memory, PSI, startup, identity, containment, cost or deadline checks. This resolves the warmup question without asserting an unseen implementation detail (`docs/design/w8d/synth/S3_FOLDIN.md:9`; `docs/design/w8d/packs/D2.md:36`).

### Memory [TARGET]

For every constrained ancestor `a`, require:

```text
shared_a + external_bounded_a + sum(direct_owner_envelopes_a) + proposed_delta_a
    <= finite_limit_a - safety_reserve_a
```

An unlimited leaf, missing ancestor, unbudgeted sibling, missing reserve or overflow fails closed. Host physical memory and host inference are budget domains too. The worker's CPU/RAM promise is execution only; model weights and host prefix memory use distinct host owners under the enclosing budget. An outer's descendants are accounted inside that precharged envelope, never added to it again at its parent. Frozen/warm instances and uncertain/collecting attempts retain their envelopes.

Define each charge owner's unconsumed promise as `max(reserved_max - attributable_current, 0)`. Separately require fresh live headroom at every ancestor:

```text
memory.current_a + sum(unconsumed_promises_a) + proposed_delta_a
    <= finite_limit_a - reaction_reserve_a
MemAvailable >= host_floor + unconsumed_host_promises + proposed_host_delta
```

At each ancestor the sum is over a **disjoint cut of direct charged owners**, with named shared/external domains included once; never sum an outer envelope and its descendants together. An inner admission inside a pre-reserved outer has `proposed_delta = 0` at the outer's parent and a positive suballocation delta only inside the outer. Only independently attributable charges can reduce an owner's unconsumed promise. Unknown attribution uses its whole hold, conservatively, and freezes unsafe expansion. Do not add full reservations to known already-charged current usage. These are complementary promise and telemetry tests, not competing capacity estimates (`docs/design/w8d/research/R3.md:86-96`). No sampled rule guarantees safety against arbitrary uncontained host allocation after the sample; hard limits and controlled external budgets remain necessary.

The maximum includes container/runner overhead, reserved guest RAM, VMM overhead, execution peak ceiling, tmpfs workspace, private build cache, `/dev/shm` frames, relay and collector buffers, and startup overlap. Guest tmpfs already within assigned guest RAM is not added a second time; separately allocated outer buffers are. Shared read-only cache charges have one owner and conservative headroom. The pack's estimator is a starting input, never a measured full envelope (`docs/design/w8d/packs/D2.md:7`, `docs/design/w8d/packs/D2.md:37`). Attach before allocating tmpfs; killing tasks does not erase their charged files (`docs/design/w8d/research/R3.md:48-56`).

### CPU, PIDs and permits [TARGET]

Every child cpuset is a subset of the effective parent set and disjoint from **discovered** interactive cores. A mask is not an exclusive CPU promise. Charge a finite quota to each owner, enforce it with `cpu.max`, and require aggregate normalized quota plus overhead to fit every finite ancestor and its effective CPU capacity. Normalize differently sized periods by checked rational comparison, rounding capacity down and demand up. Check PID and host helper/thread ceilings as additional dimensions. The task/launch schema carries cpuset but no new C2 CPU quota field, so the trusted host adapter applies the derived host policy before task code enters, then reads it back (`contracts/worker/os.cu.worker-task.v1.schema.json:729-763`; `docs/design/w8d/packs/D2.md:7`, `docs/design/w8d/packs/D2.md:29`).

The same admission transaction owns the startup permit. Only a matching authenticated ready ACK, or verified no-start/cleanup, returns it once. Supervisor restart reconstructs it; unknown startup retains it. Loss of delegation, swap use, a finite-budget violation, pressure closure or stale telemetry stops new admission, preserves queued work and leaves STOP available. Placement is integral and checks every container/dimension, not merely aggregate totals. M1's unit-job theorem does not prove optimal heterogeneous placement; M2's fractional bound is not a launch certificate (`docs/design/w8d/math/M1.md:29-37`; `docs/design/w8d/math/M2.md:31-45`).

## Bounded receipts, one expiry and independent STOP

### Receipt envelope and ACK [TARGET]

Reserve terminal-receipt capacity during admission. The host builds receipts from trusted grant, inspect, gate, extraction and cleanup observations; capsule text cannot self-certify completion. This is the v3 trust boundary (`docs/design/W8_WORKER_BANK.md:183-185`). The envelope binds:

```text
task_id, attempt_id, lease_id, target_id, epoch, instance_generation,
receipt_id, receipt_digest, payload_schema, immutable_payload_bytes,
delivery_id, committed_sequence
```

`receipt_id = H(["w8-receipt", task_id, attempt_id, lease_id, target_id, epoch, receipt_digest])`. The payload is an unchanged W8 receipt or authorized bridge result. Extra envelope fields never enter the closed receipt or lease schema. A consumer ACK binds the complete identity/digest and authenticated consumer, not only an event number. `delivery_id` can change on retransmission; `receipt_id`, digest and payload bytes cannot. Paid task/request retries always have new attempt/request identities. C3 owns bus framing and the native consumer adapter; this is the C2 side of that shared interface (`docs/design/w8d/synth/S3_FOLDIN.md:11`, `docs/design/w8d/synth/S3_FOLDIN.md:16`, `docs/design/w8d/synth/S3_FOLDIN.md:22`).

For each bounded pool, `free + held + pending_ack = capacity`. A send moves a held slot to pending ACK; a valid first ACK returns it only after the durable send marker. Premature, duplicated, wrong-target, wrong-epoch, wrong-generation, wrong-digest and unauthenticated ACKs cannot mint capacity. Lost ACK resends the same receipt, not the gate or provider request. Atomic ACK/delivery markers survive supervisor death. Progress slots and terminal slots are separate; progress cannot consume the terminal reservation. Byte bounds are checked before copy/parse and separately from message counts. No silent preview truncation: oversize complete evidence is refused or referenced by a complete authorized extracted body, never accepted as a truncated substitute. Credits/blocking-after-service come from `docs/design/w8d/packs/D2.md:13`, `docs/design/w8d/packs/D2.md:25`; hostile-peer bounds and tombstones come from `docs/design/w8d/packs/D8.md:17-21`.

Drain pending receipts in deterministic round-robin repository/seat order, preserving per-target sequence and the active-owner ring across recovery. Newly eligible owners join its tail; queue churn cannot reset an older owner's turn. A terminal receipt follows already queued progress for that target and closes further progress publication. Each continuously eligible queue gets service within a finite configured round if the sink keeps accepting. A closed sink makes no progress guarantee. Exhausted receipt slots close new admissions while existing tasks keep their reserved terminal slot. STOP uses a separate coalescing latch/control lane per destination and cannot wait for ACK, data credit, collector progress or provider cancellation. Tombstoned late completions cannot create a second finalization.

### Deadline partition [TARGET]

Set `hard_end = min(grant_absolute_expiry, original_start + granted_ttl)` using a host monotonic deadline plus the bound wall-clock expiry. Persist the original start/boot identity, never a renewed duration. Within that deadline reserve collection **[TARGET] 1000 ms**, owned stop/purge **[TARGET] 2000 ms**, and bounded finalization/initial ACK attempt **[TARGET] 500 ms**. Admission refuses an attempt whose remaining lifetime cannot contain these tails. The stop duration is a target consistent with the v3 lifecycle gate, not a demonstrated host bound (`docs/design/W8_WORKER_BANK.md:71`, `docs/design/W8_WORKER_BANK.md:289`).

Begin collection no later than `hard_end - collection_reserve - stop_reserve - finalization_reserve`; begin owned stop no later than `hard_end - stop_reserve - finalization_reserve`. An explicit STOP fences immediately and clips any collection to the remaining budget. Collection is harvest of already available text; it cannot replay an uncertain action. A slow collector is cancelled without extending the stop deadline. Purge proceeds regardless of an external mailbox ACK. After verified purge, commit resource release plus the final receipt reference consistently and enqueue its durable text-only delivery record. This avoids the cycle in which a receipt needs cleanup but cleanup waits for receipt acknowledgement (`docs/design/W8_WORKER_BANK.md:185`, `docs/design/W8_WORKER_BANK.md:202-211`; `docs/design/w8d/packs/D3.md:40`).

At expiry, classify any unresolved execution/teardown truthfully as indeterminate and retain its holds. An independent janitor continues cleanup; expiry is not proof of disappearance. An unacknowledged already-formed receipt can remain in the bounded host outbox after the execution deadline; it consumes its reserved delivery slot and cannot keep the capsule alive. A later sink retry is delivery work, never an extension of the task lease. Across host reboot or a regressed/unknown clock, fence and reconcile rather than reconstructing a fresh TTL. Across supervisor death on the same boot, reuse the persisted original deadline.

## Supervision and the W8 outcome adapter

### Supervision tree [TARGET]

The wave supervisor monitors independent target supervisors; it does not link their failure domains. Each target owns launch, gate runner, collector and watchdog ordering. Restarting a trusted host watcher/collector can resume inspection, extraction or durable delivery, but cannot rerun an uncertain task. The capsule is attempt-scoped and `restart: no`; a permissible task retry gets a fresh capsule/tmpfs and a fresh attempt. If the outer runtime actually dies, all dependent children are reconciled as affected; do not pretend an outer failure is a child-only STOP. This narrows D3's generic OTP mapping to the immutable W8 lifecycle (`docs/design/w8d/packs/D3.md:7`, `docs/design/w8d/packs/D3.md:18-20`; `docs/design/W8_WORKER_BANK.md:60`; `contracts/worker/os.cu.worker-task.v1.schema.json:3773-3775`).

Persist `attempt_limit = 3` **[TARGET]**, covering the original plus at most **[TARGET] 2** replacements. Increment `attempts_committed` in CommitAdmission; launching the already-committed attempt does not increment it again. The chain's count never decreases or resets after supervisor death, new lease, a progress message or a replacement target. A proven no-start attempt still occupies its committed chain slot: conservative counting avoids unbounded crash/retry loops. A separate host-controller intensity limit is **[TARGET] 3 restarts per 5000 ms**, journaled with boot/clock identity; losing its clock history closes the restart gate. Controller restarts do not buy extra task attempts.

Persist deterministic failure signature, consecutive signature count and absorbing `HALT_CAP`, `HALT_POISON`, `HALT_OPERATOR`. Repeated deterministic signatures halt by the chain cap; containment/storage/verification poison can halt immediately. Verified progress may clear only the consecutive streak, never total attempts. A fresh policy/task revision requires a separately authorized event and retains the old history. Cap exhaustion is checked before temporary lack of capacity, so a reboot cannot change the stated reason (`docs/design/w8d/packs/D3.md:9`, `docs/design/w8d/packs/D3.md:21-23`, `docs/design/w8d/packs/D3.md:38`, `docs/design/w8d/packs/D3.md:51-54`).

### Typed outcomes, retry and refund [TARGET]

`AdaptW8Outcome` accepts a tagged W8 result with phase, raw status/termination, dispatch certainty, runtime observation, cleanup evidence, transient-infrastructure proof, policy and verified provider usage. It returns separate `W8ExitCode`, `FailureReason`, `RetryDecision`, `ResourceReleaseDecision`, and `CostSettlementDecision`. An integer from a motor is not this type. The pack only establishes the motor's ambiguous mapping and no automatic re-attempt; its complete enum/refund rules are **ABSENT** from the bound evidence (`docs/design/w8d/packs/D3.md:32`).

The following exit codes and reason sets are **[CITED]** from `docs/design/W8_WORKER_BANK.md:213-225` and `contracts/worker/os.cu.worker-task.v1.schema.json:1971-2085`; retry decisions are **[TARGET]**:

| W8 exit code [CITED] | Reason [CITED] | Automatic retry / release policy [TARGET] |
|---|---|---|
| `0` | null | Success only with gates, extract, probes and cleanup verified. No task retry. |
| `2` | `contract` | Refuse. No automatic retry; corrected task requires authorization. |
| `3` | `execution`, `setup` | Only explicitly classified transient infrastructure, known outcome, verified cleanup and remaining chain/cost budget permit a fresh attempt. Wrong checkout pin is not a blind-retry signal. |
| `4` | `unavailable` | Wait for a verified prerequisite change; no repeated immediate launch and no egress fallback for a cache miss. |
| `5` | `gate`, `timeout`, `cancelled`, `oom`, `verification`, `storage`, `containment` | No automatic task retry in this initial policy. New limits or a new policy need authorization. Cleanup must still be proved before returning physical capacity. |
| `6` | `indeterminate` | Quarantine. No automatic retry, speculative refund or uncertain resource release. |
| `7` | `operator_recipe`, `operator_capability` | Wait for operator intervention. |

Preserve raw process status separately. A raw gate exit **[CITED] 1** normalizes to W8 **[CITED] 5**; raw timeout **[TARGET] 124** or signal retains the original value and termination reason. A raw gate **[TARGET] 2** is a gate failure, not evidence that the caller submitted an invalid contract. Stage-tagged harness failures supply their typed reason. An unrecognized/missing observation is indeterminate, never success. No new executable emits exit **[CITED] 1** (`docs/design/W8_WORKER_BANK.md:42`, `docs/design/W8_WORKER_BANK.md:200`; `AGENTS.md:16`; `contracts/worker/os.cu.worker-task.v1.schema.json:1040-1093`).

**Unchanged-schema edge:** a local rc **[CITED] 6** receipt requires cleanup `failed` or `not_observed`; other observed local outcomes require verified cleanup (`contracts/worker/os.cu.worker-task.v1.schema.json:2087-2144`, `contracts/worker/os.cu.worker-task.v1.schema.json:2317-2340`). Never emit rc **[CITED] 6** with `cleanup: verified`, and never hide real cleanup evidence to manufacture a valid object. If execution is still unknown after physical cleanup has been explicitly reconciled, retain public `INDETERMINATE` with `receipt: null` and accurate host evidence. The schema permits that (`contracts/worker/os.cu.worker-task.v1.schema.json:2976-2991`). A truthful earlier indeterminate receipt remains immutable; append later cleanup reconciliation. Unknown billing alone leaves a proven execution receipt intact and holds only the unresolved cost liability.

Money settlement is not `refund(rc)`. Refund only unused reservation supported by no-dispatch or verified final-usage evidence. Already-incurred setup/inference charges survive a failed capsule launch. Known spent amounts cannot be refunded by cleanup; unknown accepted requests retain their full maximum liability. Duplicate settlements are keyed by request/charge identity and do not charge or refund twice. Resource release and monetary settlement have separate proof predicates and idempotency keys.

## Finite cost liability and memory publication

### Host inference capabilities [TARGET]

Each host seat must provide a pinned capability/rate record before hard-budget admission: adapter build digest, tokenizer/context accounting policy, maximum input and total generated tokens, meaning of thinking/reasoning fields, maximum paid requests/retries per attempt, finite tool/cache-storage costs, price upper bounds and denominator, request identity/reconciliation support, and cancellation behavior including maximum remaining charge and local stop bound. None of these are inferred from a model name. The research's provider tables are input reports, not verified current prices or guarantees (`docs/design/w8d/research/R4.md:119-136`, `docs/design/w8d/research/R4.md:148-176`, `docs/design/w8d/research/R4.md:224-246`).

For each paid request, reserve checked integer liability:

```text
request_cap = ceil(input_cap * worst_input_rate / rate_denominator)
            + ceil(total_generation_cap * worst_output_rate / rate_denominator)
            + tool_cap + cache_storage_cap + cancel_tail_cap
attempt_cap = max_paid_requests * request_cap
spent + held_unissued_attempt_caps + outstanding_issued_request_caps
      + reserved_finalization <= wave_budget
```

Issuing a request atomically transfers its cap from the unissued pool to the issued pool without changing aggregate liability. Verified settlement moves actual charge to `spent` and releases only the proven unused balance atomically. Cancellation without settlement leaves the issued cap held. A future task replacement must succeed at its own admission transaction before using capacity or funds. No assumed cache discount reduces reservation. Thinking/cached-token details are subsets when the provider's total already includes them; never add those subsets twice. If output semantics separate them, enforce a cap covering their union. Internal SDK retries and prewarm calls must be disabled or explicitly counted as paid requests. A transport replay uses the same request ID only when the adapter can reconcile it; a real paid retry uses a new request/attempt identity. Unknown charge after a disconnect consumes the full reserved cap until settlement. A provider with no proven finite cap is unavailable to a hard-budget wave; cancellation is not proof of a refund. Budget stop starts no new paid work and finalizes only from already-reserved funds. This instantiates the integer hard-cost rule without claiming the prophet/knapsack results solve the multi-resource problem (`docs/design/w8d/math/M4.md:31-43`).

Host-enforceable controls include serialized input size/token checks, request count, local tool spend, configured output cap, local deadline and the narrow capsule exec broker. Provider-side cache retention, cache hits, immediate remote cancellation and eventual bill timing are not host-enforceable guarantees. A failed remote cancellation cannot occupy the local STOP lane; it remains a cost/reconciliation hold. Operational capability values and measured cancellation tails are **ABSENT** until a host adapter proves them.

### Prefix/cache publication [TARGET]

The admission transaction pins immutable cache/prefix objects by object digest, task/commit, context generation, adapter/tokenizer/policy digest and exact source-body identity. Publication is a host compare-and-commit on the **same destination epoch** plus expected context generation and prefix digest. No separate memory epoch can bypass the lessor. Stale, revoked, wrong-request or wrong-context publication fails without mutation; late text can be retained as quarantined evidence only. A new room digest appends a new context generation rather than rewriting the live prefix.

Pins and their host RAM reservations remain while an uncertain request may still reference them; eviction skips pinned objects. Release requires reference quiescence independently of capsule teardown. Provider cache eviction changes optimization/cost observation, not the semantic input. M3's stale per-commit image-pool premise is replaced by typed immutable host prefix/cache objects and the A4 read-only dependency cache; no prompt-memory path becomes a capsule bind or persistent per-commit image (`docs/design/w8d/math/M3.md:37-47`; `docs/design/w8d/research/R4.md:224-230`; `docs/design/w8d/W8_AMENDMENTS.md:24-26`).

## Backend and multi-window parity

### A4 execution and transport [TARGET]

Admission requires the single approved hardened base image, verified commit ingested into per-task tmpfs, read-only dependency cache, rootless or dedicated-user daemon, read-only rootfs, dropped capabilities, no-new-privileges, swap disabled, bounded PIDs/nofile, noninteractive cpuset and egress `none` unless separately approved host rules have passed probes. Refuse bind mounts, env-files, runtime sockets, arbitrary environment forwarding, in-capsule inference and per-commit image fields. Frames exist only in verified `/dev/shm`, never a WAL, outbox, log archive or disk. Only bounded UTF-8 text from the asserted tar-over-exec path can cross the result boundary; no `docker cp` (`docs/design/w8d/W8_AMENDMENTS.md:24-27`; `docs/design/W8_WORKER_BANK.md:56-78`, `docs/design/W8_WORKER_BANK.md:123-147`, `docs/design/W8_WORKER_BANK.md:266-277`).

Select a **[TARGET] Firecracker + `vsock_uds` qualification profile**, with exact VMM build/version, aarch64 guest kernel, guest rootfs, outer base, cache, exec-contract and relay digests supplied by C1/operator qualification. These actual approved pins are **ABSENT** here; there is no invented release or digest. `serial` is only a separately qualified explicit profile, never an automatic weakening fallback. The schema permits existing VMMs and the vsock/serial transport; it does not prove their compatibility under this daemon (`contracts/worker/os.cu.worker-task.v1.schema.json:581-602`; `docs/design/W8_WORKER_BANK.md:233-237`). Historical QMP references in D8 are not evidence that Firecracker speaks QMP (`docs/design/w8d/packs/D8.md:25`, `docs/design/w8d/packs/D8.md:35`).

The host broker invokes only the existing `ingest`, `gate`, `status`, `extract`, `purge` exec verbs; no new `relay` verb enters the closed schema. Specify a qualified, versioned `status` stdin subprotocol with finite mode enum `status`, `control`, `ui`, registry-selected destination, length cap, timeout and identity envelope. It may reach only the owning outer-private control or xpra UDS; caller-chosen paths/ports/argv and general shells are rejected. UI relay does not export frames as results: its stream and buffers remain in the RAM-only UI plane, recording/file-transfer/clipboard disabled. Guest CID/claimed PID/`SO_PEERCRED` alone do not authenticate the guest; the broker binds the owning runtime/channel/lease generation. No secret host capability enters the guest. `/dev/kvm` is the sole device in the microVM phase, and Docker-only targets have no devices (`contracts/worker/os.cu.worker-task.v1.schema.json:3806-3838`; `docs/design/W8_WORKER_BANK.md:205-207`, `docs/design/W8_WORKER_BANK.md:229-237`; `docs/design/w8d/packs/D8.md:9`, `docs/design/w8d/packs/D8.md:41-46`).

Until qualification proves the closed subprotocol, private socket reachability, frame confinement and per-child control under A4, the relevant backend returns `unavailable`. No new device, host socket bind, daemon credential or arbitrary exec is an escape hatch. Container/VM start remains outside this design/test change.

### Exact proposed window adapter [TARGET]

The fake-CDP parity surface is:

```python
ensure_tab(*, lease_id: str, target_id: str, epoch: int,
           session_id: str, tab_id: str | None,
           request_id: str, deadline_ms: int) -> TabBinding
```

`TabBinding` contains registered browser instance/boot identity, session ID, tab ID, target ID, lease/attempt identity, epoch, tab generation and creation request ID. `tab_id=None` creates once under the request's action intent; a supplied ID may attach only to the exact registered target/session/generation. Neither URL nor endpoint is an identity selector. A duplicate identical request returns its binding; changed body, stale session, reused tab ID, foreign target or expired deadline is refused. A lost create reply reconciles the creation intent; it cannot create another tab blindly. The Go `EnsureTab(context.Context, EnsureTabRequest) (TabBinding, Outcome)` uses the same fields.

The fake browser offers deterministic create/inspect/stop/harvest events and can inject delayed completion, disconnect, tab-ID reuse and session rotation. STOP fences/closes only the bound tab. Late harvest is tied to the old attempt/generation and cannot overwrite replacement evidence, refund its cost, acknowledge its receipt or close a sibling. The fake pins the desired parity; real WP1-06 parity is **ABSENT**, explicitly identified as absent by `docs/design/W8_WORKER_BANK.md:237`. The pack's `ensure_tab` and parallel-harvest description is secondary precedent (`docs/design/w8d/packs/D8.md:7`), not an implemented signature claim.

## Concurrency knee and measured mixture

There is no measured W8 mixture or concurrency knee in this deliverable. The initial qualification sweep is **[CITED] 2..16**, starting at **[CITED] 8 only when admissible** (targets in `docs/design/W8_WORKER_BANK.md:290`). The CPU/RAM/startup/topology rules may make that starting point impossible. A denied point is a truthful refusal, not permission to overcommit or silently borrow interactive CPUs.

Before claiming a knee, capture a **[TARGET] 100-task** authorized host trace across at least **[TARGET] 2 repositories**. The mixture manifest records each actual task/commit, gate class, backend/profile, original chain and all paid attempts, cold/warm/cache-miss class, task reservation vector, base/cache/guest/adapter pins, host/daemon/kernel/effective cgroup topology and observed phase measurements. Measure **setup, offline cache lookup/miss, bundle ingestion/clone, gate, extraction, purge and delivery** separately, with their reservation/peak and cost outcomes. Actual mixture proportions are computed from this trace and labeled **[MEASURED] only then**. No invented fixed percentage mixture or per-commit image setup stage is allowed (`docs/design/w8d/packs/D2.md:9`, `docs/design/w8d/packs/D2.md:54-57`; `docs/design/w8d/research/R3.md:59-85`; `docs/design/w8d/W8_AMENDMENTS.md:24`).

Replay the same measured manifest at each admissible `(outer_count, inner_count)` topology; record which dimensions were refused and why. Keep base/cache state, host inference load, effective CPU allocation, limits and policy fixed within a comparison. Record queue/ready/gate/extract/purge median and tail, pool/leaf pressure, CPU throttling, memory-event deltas, host free floor, tmpfs bytes/inodes, failure/unknown/refusal counts, settled cost and outstanding cost. Count useful throughput as **unique verified successful original chains**, not raw completions; both duplicate and failed paid attempts remain in cost/resource totals. Count verified coverage/repro yield only from C3-verifier events, never from raw finding count.

Proposed integer knee rule: require **[TARGET] 3 repeated runs** per point. For adjacent safe points with useful-success counts `S0,S1` and elapsed durations `T0,T1`, flag low gain when `100*(S1*T0-S0*T1) < 5*S0*T1` (**[TARGET] 5%**). Compare checked rationals, never floats. Select the lower safe point only after repeated low gain plus the preregistered tail/pressure criteria, or select the previous safe point as a **safety cap** if a higher point violates a safety gate. A safety cap is not automatically a measured throughput knee. If growth continues throughout the admissible range, say **knee not observed in tested range**; if no safe pair exists, say **insufficient admissible evidence**. Synthetic tests validate this decision procedure, not host throughput (`docs/design/w8d/research/R3.md:66-85`).

The measured configuration revision pins the manifest digest, repeated-run receipts, admitted topology, rejected points and chosen operating cap. Future changes to base/cache/guest pins, daemon, task mixture, host seats or limits invalidate that qualification and return to the bounded pilot. M1/M2 provide scheduling/packing cautions, M4 provides a hard-liability invariant, and M5's sparse-room discussion proves neither queue fairness nor durable delivery (`docs/design/w8d/math/M1.md:29-37`; `docs/design/w8d/math/M2.md:31-45`; `docs/design/w8d/math/M4.md:33-43`; `docs/design/w8d/math/M5.md:33-43`).

## S3 answers

Every test named below is in `contracts/worker/tests/test_w8d_c2_lease.py`. These are good and must-FAIL **[TARGET] fixtures** for the proposed interface; they do not certify daemon behavior. The question text is quoted from the bound synthesis. The following subsections cover every question tagged C2, including shared C1/C2 and C2/C3 questions.

### Room A — Child limits and atomic purge ownership

**Question:** How are child limits enforced under the chosen daemon without host binds, and which ownership record makes cohort purge and reservation release atomic?

**Decision:** The trusted host adapter qualifies and installs effective child cgroups before guest execution; no child cgroup bind or daemon socket enters the capsule. Only the qualified single-inner topology is initially enabled. `AttemptOwnership` binds runtime/cgroup/workspace generations and the durable release key. Physical purge is a verified external phase; the atomic operation is `release_done + resource_credit`. Child release returns an inner suballocation; shared outer credit waits for complete outer teardown.

**Tests:** `test_child_controller_qualification_and_closed_relay`, `test_release_survives_crash_pid_reuse_and_janitor_reentry`, `test_shared_outer_ownership_prevents_early_or_double_release` reject missing delegation, premature/shared release, identity reuse and repeated credit.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:5`; `docs/design/W8_WORKER_BANK.md:234`; `docs/design/w8d/packs/D3.md:11`, `docs/design/w8d/packs/D3.md:25-26`; `docs/design/w8d/W8_AMENDMENTS.md:25-27`.

### Room A — Governor configuration and warmup

**Question:** What exact W8 governor configuration replaces the existing ceiling and CDP warmup restriction while enforcing CPU, ancestor-memory and startup permits?

**Decision:** Use the host W8 configuration table above within the existing governor: finite topology/resource-derived active cap, single-permit startup qualification, fresh all-ancestor telemetry, CPU quota and effective cpuset checks, integer memory promises/live guards, and per-backend readiness pilot. Missing CDP data neither vetoes headless readiness nor becomes a healthy sample. Warmup bypasses no resource or security guard.

**Tests:** `test_ancestor_governor_avoids_double_count_and_checks_every_limit`, `test_parent_owner_cut_does_not_count_descendants_twice`, `test_pressure_hysteresis_requires_clear_samples` pin finite ancestors, exact boundaries and pressure reopen. `test_headless_readiness_and_startup_replay` pins backend-specific pilot evidence and durable permit ACKs. `test_child_controller_qualification_and_closed_relay` pins effective delegation.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:9`; `docs/design/w8d/packs/D2.md:30`, `docs/design/w8d/packs/D2.md:36-41`; `docs/design/w8d/research/R3.md:86-96`; `docs/design/W8_WORKER_BANK.md:332`.

### Room A — Placement/reservation/epoch/launch transaction

**Question:** Which transaction binds placement, reservation, epoch and launch intent, and how does crash replay avoid both duplicate grants and double release?

**Decision:** `CommitAdmission` flushes the complete host ownership record before acknowledgement or launch. Reusing an admission request returns the same grant only for an identical body. A claimed launch with uncertain result is inspected/adopted or quarantined, never automatically re-created. Release completion and its credit delta share a durable key/commit.

**Tests:** `test_atomic_admission_crash_replay_and_payload_conflict`, `test_unknown_launch_holds_and_matching_launch_is_adopted`, `test_release_survives_crash_pid_reuse_and_janitor_reentry` inject crashes before/after relevant commits and reject changed-payload request reuse.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:10`; `docs/design/w8d/packs/D8.md:31-33`; `docs/design/w8d/packs/D3.md:11`, `docs/design/w8d/packs/D3.md:25-26`, `docs/design/w8d/packs/D3.md:53`.

### Room A — Receipt credits, STOP capacity and fairness

**Question:** Which bounded receipt credit/ack protocol propagates back-pressure and leaves independent STOP capacity, and what fairness/drain fixtures prove progress?

**Decision:** Reserve terminal capacity at admission, bound messages and bytes, preserve `free + held + pending_ack = capacity`, and replenish only on an authenticated exact-tuple ACK. Per-repository/seat round-robin drain provides progress when the sink accepts. A distinct coalescing STOP lane remains available when all data/receipt credits are occupied.

**Tests:** `test_receipt_credits_ack_fairness_and_independent_stop` fills credits, rejects excess and wrong/duplicate ACKs, checks conservation and fair resumed drain, and verifies STOP at saturation.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:11`; `docs/design/w8d/packs/D2.md:13`, `docs/design/w8d/packs/D2.md:25`; `docs/design/w8d/packs/D8.md:17-21`.

### Room A — W8 outcome adapter

**Question:** Which W8-specific outcome adapter prevents the motor return-code enum from silently changing retry/refund semantics?

**Decision:** `AdaptW8Outcome` consumes an explicitly W8-tagged outcome and phase/evidence, validates the unchanged rc/reason mapping, and returns separate retry, resource-release and monetary-settlement decisions. No numeric motor cast or `refund(rc)` exists. A known gate failure is not reclassified as contract invalidity because of its raw integer exit. Unknown billing alone is not fabricated execution uncertainty.

**Tests:** `test_w8_outcome_adapter_never_casts_motor_enum`, `test_unknown_execution_clean_observation_has_no_invalid_local_receipt`, `test_cost_liability_paid_retry_and_settlement_idempotency` reject invalid reasons, unsafe retry/refund and the contradictory local indeterminate/verified-cleanup receipt.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:14`; `docs/design/w8d/packs/D3.md:32`; `docs/design/W8_WORKER_BANK.md:213-225`; `contracts/worker/os.cu.worker-task.v1.schema.json:1971-2144`.

### Room A — Durable chain counter, release key and process identity

**Question:** Which durable task-chain counter, release idempotency key and process-identity checks survive supervisor death and janitor interruption?

**Decision:** Persist monotonic `attempts_committed` and absorbing halts in the admission WAL, distinct from controller restarts. The domain-separated release key binds admission transaction, owner and instance generation. Cleanup requires runtime/boot/PID-start/cgroup/workspace identity and reference closure, never only a PID or a reused name. Replaying janitor work cannot return a reservation twice.

**Tests:** `test_durable_chain_cap_survives_supervisor_death`, `test_release_survives_crash_pid_reuse_and_janitor_reentry`, `test_shared_outer_ownership_prevents_early_or_double_release` reload durable snapshots and reject recycled identity and repeated release.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:15`; `docs/design/w8d/packs/D3.md:9`, `docs/design/w8d/packs/D3.md:22`, `docs/design/w8d/packs/D3.md:25`, `docs/design/w8d/packs/D3.md:38-44`.

### Room A — Collection, ACK and teardown under one expiry

**Question:** How do bounded collection, terminal receipt acknowledgement and cohort teardown fit the same expiry without blocking STOP or replaying an uncertain action?

**Decision:** Reserve collection/stop/finalization tails inside the original hard deadline and clip extraction on STOP. Purge does not wait for the remote ACK. A terminal receipt is formed from truthful cleanup evidence and retained in its bounded host slot for later delivery if needed. That delivery can outlive the task lease; the capsule cannot. Uncertain actions remain unreplayed and unresolved resources remain quarantined.

**Tests:** `test_single_expiry_bounds_collect_stop_ack_and_restart`, `test_receipt_credits_ack_fairness_and_independent_stop`, `test_sent_action_is_not_replayed_and_quarantine_holds_pins` pin saturated/slow sink, deadline inheritance and unknown-action behavior.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:16`; `docs/design/W8_WORKER_BANK.md:71`, `docs/design/W8_WORKER_BANK.md:185`, `docs/design/W8_WORKER_BANK.md:202-211`; `docs/design/w8d/packs/D3.md:40`.

### Room A — Destination revoke and actuator transaction

**Question:** What destination-scoped revoke and actuator transaction extend the current lessor without revoking sibling targets or leaking capabilities?

**Decision:** Add `RevocarDestino` within the existing lessor, serialized with the target's action dispatch. It advances only the destination fence, persists STOP and tombstones its pending work. The actuator rechecks the exact current epoch/generation/caller at dispatch; already-sent uncertain effects require reconciliation. Raw capabilities stay in host memory and never become public `lease_id` values or guest credentials.

**Tests:** `test_destination_revoke_preserves_siblings_and_stale_actions_fail`, `test_sent_action_is_not_replayed_and_quarantine_holds_pins`, `test_identity_is_canonical_and_not_a_capability` pin sibling equality, late action refusal and opaque identifiers.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:19`; `docs/design/w8d/packs/D8.md:13-19`, `docs/design/w8d/packs/D8.md:31-32`, `docs/design/w8d/packs/D8.md:51`.

### Room A — ensure_tab parity

**Question:** What exact `ensure_tab` signature, tab/session identity and fake-CDP contract prove WP1-06 parity, including STOP and late harvest?

**Decision:** Use the keyword-only signature and `TabBinding` above, with broker-registered browser boot/session/tab-generation identity. Same request is idempotent; stale/foreign/reused bindings fail. STOP affects only the target tab; late harvest remains evidence for the old attempt. Fake parity is a contract test; actual WP1-06 integration remains unproven.

**Tests:** `test_ensure_tab_identity_stop_and_late_harvest` exercises repeated ensure, wrong identity, target isolation and late completion; `test_public_lease_matches_unchanged_schema` pins the shared public representation.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:20`; `docs/design/w8d/packs/D8.md:7`; `docs/design/W8_WORKER_BANK.md:237`; `contracts/worker/os.cu.worker-task.v1.schema.json:2896-2994`.

### Room A — Pinned VMM/channel and closed UI relay

**Question:** Which pinned VMM/channel plus closed UI relay can reach outer-private sockets with no bind, extra device, guest credential or arbitrary exec?

**Decision:** Require the proposed qualified Firecracker/`vsock_uds` profile and concrete operator/C1 pins; actual approved digests are absent, so no live launch is claimed. The bounded `status` subprotocol selects only the lease-owned outer-private socket from the host registry. It adds no exec verb, arbitrary path or host credential. UI bytes stay in the RAM-only UI plane; only asserted text results cross extraction. Missing qualification gives `unavailable`.

**Tests:** `test_child_controller_qualification_and_closed_relay`, `test_a4_profile_good_and_must_fail_variants` reject missing pins/delegation, extra devices, caller-supplied endpoints, arbitrary exec, binds, env-files and runtime-socket access.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:21`; `docs/design/W8_WORKER_BANK.md:205`, `docs/design/W8_WORKER_BANK.md:229-237`; `docs/design/w8d/packs/D8.md:9`, `docs/design/w8d/packs/D8.md:42-46`; `contracts/worker/os.cu.worker-task.v1.schema.json:3806-3838`.

### Room B — Inner target identity and reassignment

**Question:** What exact target identity, fencing scope and crash-recovery transition permit reassignment of an inner VM without revoking its siblings, and when may its reservation be released?

**Decision:** The canonical logical identity is registered host + outer instance + destination, with separate monotonic instance generation. Fence only that destination, preserve unknown holds, inspect exact runtime identity, and permit same-slot reassignment only after old-instance closure. Child release frees an inner suballocation, while the shared outer envelope persists until every child and the outer are verified clear.

**Tests:** `test_identity_is_canonical_and_not_a_capability`, `test_reassignment_keeps_failed_attempt_and_new_epoch`, `test_shared_outer_ownership_prevents_early_or_double_release`, `test_parent_owner_cut_does_not_count_descendants_twice` pin distinct generations and stable sibling/parent ownership.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:26`; `docs/design/w8d/packs/D8.md:7`, `docs/design/w8d/packs/D8.md:13-19`, `docs/design/w8d/packs/D8.md:51`; `docs/design/w8d/research/R3.md:48-56`.

### Room B — Interrupted duplicate outcome

**Question:** What receipt/fencing transition records an interrupted duplicate assignment, accounts for its resources and costs, and preserves its failure outcome when a replacement succeeds?

**Decision:** Persist the duplicate-interrupted relationship, old outcome and destination fence before any replacement. Unknown old resources/cost remain held; any authorized replacement on another target requires additional admission. New attempt/lease/paid-request identities keep retry charges distinct. Append replacement success to chain history; never overwrite the interrupted attempt or rewrite its immutable receipt.

**Tests:** `test_reassignment_keeps_failed_attempt_and_new_epoch`, `test_sent_action_is_not_replayed_and_quarantine_holds_pins`, `test_cost_liability_paid_retry_and_settlement_idempotency` preserve old failures/holds and distinguish idempotent request replay from paid retry.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:33`; `docs/design/w8d/packs/D3.md:9-11`, `docs/design/w8d/packs/D3.md:32`, `docs/design/w8d/packs/D3.md:42-44`; `docs/design/w8d/math/M4.md:33-41`.

### Room C — Memory epoch, pins and enforceable context limits

**Question:** Which existing lease epoch and recovery transaction fence memory publication, pin cache entries, and retain RAM reservations while state is indeterminate? Which adapter-owned context/generation limits can actually be enforced?

**Decision:** Use the same destination epoch and admission/reconciliation transaction as actuator fencing, with immutable context generation/prefix/source digests and host cache pins. Unknown requests retain references and their host RAM/cost holds. The adapter enforces input/context accounting, request/tool caps, supported total-generation parameters and local deadlines; it cannot guarantee remote cache hits or immediate remote cancellation. A provider lacking finite enforceable liability is unavailable to the hard-budget wave.

**Tests:** `test_memory_publication_fenced_and_pins_released_once`, `test_sent_action_is_not_replayed_and_quarantine_holds_pins`, `test_cost_liability_paid_retry_and_settlement_idempotency` reject stale publication, premature pin release and missing finite caps.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:40`; `docs/design/w8d/packs/D8.md:15`; `docs/design/w8d/math/M3.md:37-47`; `docs/design/w8d/research/R4.md:119-136`, `docs/design/w8d/research/R4.md:224-246`.

### Room C — Shared reservation transaction, liability and measured knee

**Question:** Which shared Go transaction atomically acquires resource, lease and cost reservations and recovers them after a crash? What finite request/retry liability and cancellation bounds can each host seat prove? What measured task mixture and admissible topology define its knee probe?

**Decision:** `CommitAdmission` owns the integral placement, lease, epoch, resource/cost/receipt/startup/cache reservations and launch intent; recovery replays committed ownership and never repeats an unknown action. Each seat supplies the finite, versioned capability/rate/cancellation record; unsupported claims refuse admission. The knee uses an actual measured mixture manifest, repeated safe runs, unique verified-chain throughput and qualified topology. No measured mixture or safe host concurrency is claimed here.

**Tests:** `test_atomic_admission_crash_replay_and_payload_conflict`, `test_durable_chain_cap_survives_supervisor_death`, `test_cost_liability_paid_retry_and_settlement_idempotency`, `test_knee_fixture_is_simulation_not_a_host_measurement` pin bounded replay, attempts/liability and synthetic versus measured evidence.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:45`; `docs/design/w8d/packs/D3.md:11`, `docs/design/w8d/packs/D3.md:32`; `docs/design/w8d/math/M4.md:33-41`; `docs/design/w8d/research/R3.md:59-96`; `docs/design/W8_WORKER_BANK.md:290`.

## Verification boundary and implementation gates

The accompanying stdlib unittest contains inline good and must-FAIL fixtures, a fake clock/browser and a serializable reference state model. It exercises decisions, state transitions, accounting conservation, identity and crash interleavings without a daemon, VM, network or frame capture. Direct execution returns **[CITED] 0** on success and **[CITED] 5** on test failure, conforming to `AGENTS.md:16`; unittest discovery retains the repository's normal runner behavior. Run observations, including failures or skips, belong in this task's outbox report rather than being relabeled host measurements.

Implementation gates still needed are the actual Go/WAL integration; independent crash/fault injection against its persistence boundaries; full worker/bridge verifier parity; approved base/cache/VMM/relay pins; effective daemon/child-cgroup qualification; measured lifecycle/cancellation bounds; actual fake-to-native WP1-06 parity; authenticated C3 delivery/consumer bindings; and the measured task-mixture probe. Existing bridge surface/consumer gaps are described by the bound v3 document, not solved by changing the current task's surface or the shared schema (`docs/design/W8_WORKER_BANK.md:172-180`, `docs/design/W8_WORKER_BANK.md:322-332`). A collection PR is not acceptance or permission to launch.
