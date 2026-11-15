# JQ2A M6 C1: WindowScheduler contracts and integration boundary

## Scope and source coordinates

Task `jq2a-m6-c1-window-scheduler`, attempt `a1`, implements
`wender/act/window_scheduler.py` and ordinary offline public-path tests in
`tests/test_jq2a_m6_c1_window_scheduler_implementation.py`. This is a scheduler
implementation against explicit injected contracts. It does not establish host
integration or acceptance of the coordinated task.

The bound files were read through the GitHub connector at explicit ref
`bridge/integration`, resolved to
`21dbf0dd56f5c1c8d544cda7c99624cf8e4c0f3b`. The task metadata's
`2174cf9caebafabe5b0e3d3f10fe74763493110b` is older; the attached instruction to
resolve the current requested ref controls. Coordinates below refer to the
resolved source, not the historical snapshots described inside earlier notes.

| Source | Consequence for this implementation |
| --- | --- |
| `docs/spec/jq1_a6_multiwindow_harvest.md:312-316` | Control observation, each effective send, and terminal result consumption require a common linearization boundary. |
| `docs/spec/jq1_a6_multiwindow_harvest.md:326-343` | The deadline includes discovery, ownership waits, transport, verification and extraction. Unfenced compound readers and executor retries cannot be wired directly. |
| `docs/spec/jq1_a7_actor_safety_contract.md:98-122` | Short metadata transitions protect ownership; uncertain outcomes retain diagnostics and quarantine instead of advertising a resource as free. |
| `wender/act/actor.py:34-86` | The existing primitive is a nonblocking per-instance lock. It does not supply the required canonical target registry. This task does not edit it. |
| `wender/act/cdp_executor.py:371-374,454-459` | The existing executor has its own action/retry path. This task injects a reviewed dispatch adapter instead of modifying that executor. |
| `wender/perceive/cdp_pipe.py:309-351` | The existing pipe writes at line 323 and starts its reply deadline at line 335. It does not check this shared writer fence. Real pipe integration remains **AUSENTE**. |

There is one explicit policy conflict: the earlier multiwindow proposal at
`docs/spec/jq1_a6_multiwindow_harvest.md:162-185` releases local leases immediately
on STOP or timeout. The current task's **INV-M6-3 takes precedence**: after a
possible send, retain the claim and ownership until positive terminal or
quiescence evidence permits reconciliation. No cleanup command is an exception
to the STOP fence.

## Public API and structural providers

The constructor is
`WindowScheduler(*, registry, locks, focus_policy, budget, chain, writer_fence,
dispatch, clock=None, max_queue, max_targets, service_cap_ns=None)`.

| Public operation | Contract |
| --- | --- |
| `submit(request)` | Snapshot and validate the request, reserve global/per-target credits, and return `Ticket` or an explicit refusal `Receipt`. Invalid record/evidence shapes raise `InvalidRequest`, a `ValueError` subclass. No service cap yields `CONFIG_REQUIRED`. |
| `step(now_ns)` | Advance monotonic absolute time, finalize expired/revoked jobs, attempt at most one selected target, and drain newly emitted terminal receipts. |
| `complete(ticket, result)` | Validate structural action/handle identity and dispatched history. A positive terminal acknowledgment settles ownership/accounting; an unknown or unacknowledged result cannot release a possible-send claim. |
| `stop(handle=None)` | Latch local canonical-resource STOP or global STOP. A retained binding can fence a job even after its registry entry retires. |
| `reconcile(handle, *, acknowledged=False)` | Retain quarantine unless the caller positively attests terminal/quiescence evidence. Reconciliation never clears a user STOP or changes a previously emitted terminal outcome. |

Provider methods are public structural contracts chosen here because their owner
modules were absent at the bound ref. They are not claims about unmerged APIs.

| Injection | Required methods and meaning |
| --- | --- |
| `registry` | `get(handle) -> TargetRecord | None`; `canonical_resource(handle) -> str`. Return the current incarnation and geometry epoch. Map a capsule and its child screen to one resource when they actuate the same destination. |
| `locks` | `try_acquire(handle, *, owner, expires_ns) -> ActorLease | None`; `release(lease)`; `quarantine(lease)`. Acquire atomically and nonblockingly; validate exact issued token ownership on release. |
| `focus_policy` | `try_acquire(actor_lease, *, epoch) -> FocusLease | None`; `release(focus_lease)`; `quarantine(focus_lease)`. Serialize conflicting focus domains, while independent domains remain eligible. |
| `budget` | `reserve(handle, cost, *, action_id, deadline_ns) -> token | None`; `settle(token, *, consumed)`. Reserve atomically against both global and canonical per-target credits. Refused reservations have no effect. `consumed=False` refunds an unsent reservation; `True` permanently charges it. |
| `chain` | `is_settled(handle, epoch) -> bool`. Only literal `True` is positive settled evidence. No import from another repository is needed. |
| `clock` | A callable returning nonnegative integer nanoseconds in the same monotonic time domain as `step` and the fence. |
| `dispatch` | Callable `dispatch(request, context) -> Completion | None`. `None` means asynchronous completion is pending. A returned completion is handled through the same public `complete` path. |

Ownership methods perform local metadata operations only. They may refuse busy
ownership; they must not wait for transport or silently release an active or
quarantined lease when its timestamp expires. Budget per-target canonicalization
must agree with the registry's resource aliases. A provider exception must not leave an
unreported successful acquisition. Release/quarantine operations must validate
their issued token, including generation and nonce, so a stale callback cannot
affect a later owner. Actual provider compatibility requires re-verification
when their modules merge.

Returned actor/focus capabilities are validated before they become cleanup
authority for a job. A foreign or malformed capability quarantines the requested
resource and records a refusal; it never grants permission to release or
quarantine the foreign incumbent's token. Positive quiescence is required to
settle the affected request's retained reservation.

`Completion(action_id, handle, status, acknowledged=False, reason=None)` is an
owned helper. Terminal statuses are `SUCCEEDED`, `FAILED`, `CANCELLED`, and
`UNKNOWN`; `UNKNOWN` is never positive acknowledgment. A matching success label
without `acknowledged is True` cannot authorize reuse. A canonical `Receipt`
alone does not contain that acknowledgment field.

## Identity and evidence

Shared records are imported from `wender.perceive.session_registry` whenever
that module exists. Only a `ModuleNotFoundError` naming that exact missing
provider enables the local frozen record layouts specified by the task. Missing
exports and internal import errors propagate. The three absent ownership/budget
providers similarly use local structural `Protocol` declarations, without
pretending to implement those providers.

Handle equality is structural `(kind, canonical_id, generation)`, with arbitrary
size integer generations and a separate geometry/navigation epoch. Booleans are
rejected where integers are required. Supported identity kinds are `cdp_tab`,
`x11_window`, `xvfb_screen`, `capsule`, and `godot_window`; accepting an identity
does not provide its transport driver.

Submission copies identity, action id, epoch, payload, cost and the original
absolute deadline. Recognized `observation` and `window_frames` payload evidence
must match that handle/epoch, have positive DPR and valid local frame sequences,
and have matching RGB dimensions/digests. Frame sequences within a window must
increase and match the frame count. RGB arrays are copied to owned contiguous
`uint8` storage and made read-only. Raw RGB window arrays are normalized to owned
`FrameRef` values. Stale sequence regression or changed content for an already
admitted sequence is refused. No frames are written to disk.

## Claim, writer and STOP linearization

`WriterFence.metadata_lock` is also the scheduler's metadata mutex. Selection
atomically publishes a tentative per-resource claim before invoking readiness,
actor or focus providers. Their calls occur after releasing the mutex. Budget
reservation happens during admission and remains held while a request waits.
Final authorization rechecks current identity/epoch, settled evidence,
prerequisites, deadline and STOP, and publishes an active writer permit only
with that claim and reservation present. STOP during preparation invalidates
authorization and releases only known-unsent reservations.

Every actual writer must use the same `WriterFence`. `DispatchContext.check()`
binds the check to the ticket and the exact issued permit. `context.send(fn, ...)`
checks first, then invokes `fn` with no metadata lock held. A successful check
marks a possible send before returning: STOP after that boundary cannot recall
the already-authorized operation. The next send, including cleanup, must check
again and is refused. `writer_fence.check(handle)` is also exposed for barrier
traces; adapters should use the context's permit-bound form.

The registry must publish epoch changes through
`writer_fence.publish_epoch(handle, epoch)` before exposing the changed record,
and retire an incarnation through `writer_fence.invalidate(handle)`. These
revoke the old permit; a possible-send permit additionally quarantines the
resource. They are distinct from user STOP, so a new epoch/incarnation can
progress after required acknowledgment rather than inheriting a fabricated
permanent STOP. Real registry participation in this boundary is an integration
prerequisite.

`check_lock_order(ranks)` rejects inversions without waiting. Ranks are
scheduler/fence (0), registry (1), budget (2), target actor (3), focus domain (4),
stream bank (5), and per-stream metadata (6). Same-rank acquisitions require
strictly increasing canonical keys; release order is reversed. Clock, provider,
dispatch and transport callbacks run outside scheduler metadata protection.

## Fair service, admission and absolute deadlines

The cursor reserves its next fair target before considering urgency. Each target
keeps FIFO submission order; urgency compares the heads of those target queues
by deadline and then submission sequence. At most one earlier-deadline target
is promoted before the reserved fair visit. An urgent job that finishes preparing after its
fair round has advanced is retried without dispatch authorization. The fair
visit is committed when its atomic target claim begins, allowing independent
targets to prepare while a provider for the first target is busy. Budget cost
does not alter the fairness quantum: every visit costs one **TARGET**.

`starvation_bound = 2 * max_targets` counts selection slots until a continuously
eligible target's next fair visit. `fair_visits` and `eligibility_stalls` are
integer counts keyed by structural handle; `urgent_dispatches` counts authorized
urgent dispatches. Eligibility excludes held/quarantined resources, unavailable
ownership/focus, unsettled evidence, STOP, and exhausted deadlines. The bound is
not a wall-time claim or a promise that an unavailable provider becomes ready.

Admission counts admitting, queued, active and quarantined jobs against
`max_queue`, and their distinct canonical resources against `max_targets`.
Its configured feasibility check requires remaining time of at least
`starvation_bound * (same_target_jobs_ahead + 1) * service_cap_ns`, rechecked
after reservation. Dispatch also requires enough remaining time for the service
cap. The cap is a declared bound that the integrated providers must establish;
the scheduler cannot forcibly preempt an arbitrary blocking Python callback.
FIFO within a target prevents later arrivals from overtaking its admitted
backlog. Changing eligibility or a provider that exceeds its declared cap can
still cause expiration; the absolute deadline remains authoritative.

`WriterFence.advance` supplies the authoritative monotonic maximum to every
scheduler sharing that fence; a later time observed by one scheduler cannot be
rolled back by another scheduler's clock sample. No retry creates a new
deadline. Discovery/reservation, queueing, ownership
attempts, transport, verification and extraction all consume the original
budget. Dispatch adapters must propagate `context.remaining_ns()` to bounded
operations. Identity keys of retired requests remain as tombstones for the
scheduler lifetime, preventing action-id reuse from accepting stale completion.
These tombstones and telemetry are retained history, distinct from the bounded
live queue/population; this task does not implement persistent recovery or
history compaction.

## Terminal accounting and prerequisite limits

A known-unsent expiry/STOP releases its reservation exactly once. After any
successful writer check, timeout, exception or missing acknowledgment yields
`UNKNOWN`, fences the canonical resource, and keeps budget/actor/focus ownership
held. It neither refunds consumed work nor authorizes a replay. Positive terminal
acknowledgment or explicitly attested quiescence permits local release and budget
settlement with `consumed=True`. The first terminal receipt remains the outcome;
late acknowledgment may settle ownership without rewriting that receipt as
success. Local release failure records diagnostics and retains quarantine.

During acknowledged completion the old writer permit is revoked but retained
through unlocked focus/actor release and budget settlement. Its final retirement
and quarantine reconciliation happen together under the shared fence lock. This
prevents a second scheduler from authorizing the same resource in the middle of
the first scheduler's terminal accounting.

The prerequisite order is identity/streams, ownership/perception, readiness,
scheduling/Godot, then scorecard. At the resolved ref all four modules below are
**AUSENTE**, as are their integrated passing gates:

- `wender/perceive/session_registry.py` — canonical records and live registry.
- `wender/act/target_lock_registry.py` — canonical actor ownership.
- `wender/act/focus_policy.py` — reviewed focus-domain ownership.
- `wender/act/budget_governor.py` — atomic global/per-target credit accounting.

Dispatch must expose explicit `prerequisite_gates` with literal `True` entries
for `identity_streams`, `ownership_perception`, `readiness`, and `writer_fence`,
and its `writer_fence` attribute must be the same instance. These are conductor
attestations, not self-certification by the scheduler. A missing gate/interface
produces `AUSENTE`; absent owner modules also block production dispatch.
`dispatch.offline=True` permits the explicitly typed test fixtures to exercise
the proposed contracts while providers are absent; it does not make an
integration gate pass. The unchanged pipe/executor cannot meet the writer
contract by merely setting these attributes.

The ordinary test module uses production scheduler/fence public paths with
typed in-memory dependency fakes, fake clocks, barriers and RAM NumPy frames.
Fixtures make forbidden file/network/process operations raise. Tests distinguish
non-atomic claims, unbounded EDF promotion, refund-on-uncertain-timeout, and
cleanup-after-STOP mutations, with independent-target and nonconflicting-domain
healthy controls. Run:

```text
python3 -m unittest tests.test_jq2a_m6_c1_window_scheduler_implementation
```

Observed results, the separate pre-existing offline regression outcomes, and
unresolved integration limits belong in this attempt's `report.md`. Expected
failures are not passing evidence. Any reported timing is a VM measurement;
neither it nor this collection pull request certifies host acceptance.
