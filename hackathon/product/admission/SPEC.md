# Multi-Actor Lease & Region-Scoped Admission — Specification

**Unit**: P1 (Track P) · **Status**: reference spec, implemented by `admission.py`, checked by `test_admission.py`
**Extends**: `swarm/CONTRACT.md` §3 (THINK/ENQUEUE/TURN/RETURN), §4 (claims/leases), §6 (verdict codes), and the
single-actor `observe -> propose -> wait-verify` path used by `mcp/vp_mcp/vp_mcp.py` (`_mvctl_observe`,
`_mvctl_propose`, `_mvctl_wait_verify`).

## 1. Scope

The single-actor pipeline admits a proposal only when `based_on_frame` equals the current frame; any newer frame
gives `STALE_FRAME`. With several actors sharing one surface, that rule refuses almost every proposal, because
another actor's verified act always publishes a newer frame first. This spec adds:

1. **Multi-actor identity**: every proposal names an `actor_id` and must show that actor's lease.
2. **Epoch-versioned leases** that can be issued, revoked and expired.
3. **Region-scoped rebase**: a proposal based on an older frame may still be admitted (`ADMITTED_REBASED`) when it
   is provably unaffected by what changed since. Otherwise it gets a precise refusal: `STALE_VIEW`, `STALE_SCENE`,
   `STALE_REGION`, or the strict `STALE_FRAME`.
4. **One exclusive actuator**: at most one dispatch is in flight (`ACTUATOR_BUSY`).

Out of scope here and still owned by the broker (`swarm/CONTRACT.md` §3): per-agent FIFO and `QUEUE_FULL`, ring
order, `PASS`, and the `events.jsonl` / `osb` projections. The controller is the admission core a broker calls on
each turn.

## 2. Terms

| Term | Definition |
|---|---|
| Rect | `(x, y, w, h)`: ints, `w > 0`, `h > 0`, fully on the `1920x1080` display. Treated as the half-open box `[x, x+w) x [y, y+h)`. |
| Frame `F_n` | Immutable `FrameObservation(frame_seq=n, view_epoch, scene_epoch, changed_regions, sha256, ts_ms, attributed_to)`. |
| `changed_regions(F_n)` | Rects that changed between `F_{n-1}` and `F_n`, or **`None` = extent unknown** (unattributed foreign change, failed job, reconcile capture, surface restart). |
| Head `H` | `frame_seq` of the newest published frame. |
| Ring | The `ring_size` most recent frames (default 16). Older frames are **evicted**. |
| `view_epoch` | Incremented by a verified `view_change` GUI act, a foreign view change, a reconcile, and a surface restart. |
| `scene_epoch` | Incremented by `STATE_VERIFIED`, a foreign scene change, a reconcile, and a surface restart. |
| Margin `m` | Safety margin in px (default 8). `inflate(r, m)` grows `r` by `m` on every side. |
| Guard set `G(P)` | `{ inflate(target_region, m), inflate(footprint, m) }`. |

## 3. Leases

```
Lease = { lease_id, actor_id, lease_epoch, ttl_ms, issued_ts_ms, expires_ts_ms = issued_ts_ms + ttl_ms,
          allow_gui, write_prefixes, allowed_regions }
```

| Operation | Effect |
|---|---|
| `issue_lease(actor_id, now_ms, ttl_ms, allow_gui, write_prefixes, allowed_regions)` | New `lease_id`. `lease_epoch` = the actor's previous epoch + 1 (strictly increasing for each actor). Any previous active lease becomes `superseded`. `allowed_regions=None` means the whole display. Resets the actor's consecutive-timeout counter. |
| `revoke_lease(actor_id)` | Removes the actor's lease. Its `lease_id` becomes `revoked`. |
| `expire_leases(now_ms)` | Every lease with `now_ms >= expires_ts_ms` becomes `expired`. `propose` also applies this rule lazily. |

Lease states: `active -> superseded | revoked | expired`. A lease never becomes active again.

**L1 (authority).** A proposal passes the lease check only if all of these hold: `lease_id` was issued to
`proposal.actor_id`, it is that actor's current lease, `lease_epoch` matches, and `now_ms < expires_ts_ms`.

| Failure | Code |
|---|---|
| Actor holds no lease; `lease_id` unknown, revoked, or issued to another actor | `NO_LEASE` |
| `lease_id` expired or superseded; `lease_epoch` stale; TTL elapsed | `LEASE_EXPIRED` |

**L2 (claims).**
- Script lane: `write_set` must be non-empty, and every entry must start with one of the lease's `write_prefixes`.
  Otherwise `CLAIM_REFUSED`.
- GUI lane: requires `allow_gui`, otherwise `CLAIM_REFUSED`. Requires a valid `footprint`, otherwise
  `FOOTPRINT_MISSING`. `target_region` must be null or a valid Rect. `keys` must be null or 1..8 entries from
  `{KP_1, KP_3, KP_7, KP_4, KP_Add, Home}`. `footprint` and `target_region` must each lie inside one
  `allowed_regions` entry. Any failure is `CLAIM_REFUSED`.
- An unknown lane, or an `action_class` not valid for the lane (gui: `view_change` | `input`; script: `state_job`),
  is `CLAIM_REFUSED`.

**L3 (lease lifetime during a dispatch).** A lease that expires or is revoked while its dispatch is in flight does
not stop verification. The input has already been sent, so `wait_verify` still classifies the physical outcome.
Refusing at that point would hide an actuation.

## 4. Observe

`observe(actor_id, now_ms) -> FrameObservation` returns `F_H`. It is read-only: no lease is needed, matching the
rule that agents read `frames/` read-only. `sha256` is the SHA-256 of the canonical JSON of
`[frame_seq, view_epoch, scene_epoch, changed_regions, content_sha256, attributed_to]`. It binds a proposal to
exactly one published frame.

Frames are published only by the controller: by `wait_verify` (post-dispatch capture), `capture` (a change that did
not go through admission), `resolve_reconcile`, and `surface_restart`. Every epoch change publishes a new frame, so
the epochs are attributes of the frames.

## 5. Propose

```
Proposal = { proposal_id, actor_id, lane, lease_id, lease_epoch, based_on_frame, view_epoch, scene_epoch,
             based_on_sha256?, target_region?, footprint?, write_set, keys?, allow_rebase=true, action_class }
```

`propose(P, now_ms)` is **atomic** (one controller lock) and evaluates these checks in order. The first failure
returns its code.

| # | Check | Refusal |
|---|---|---|
| 1 | `proposal_id` non-empty and not in `consumed` (ids already dispatched or parked) | `CLAIM_REFUSED` / `DUPLICATE_EVENT` |
| 2 | L1 authority | `NO_LEASE` / `LEASE_EXPIRED` |
| 3 | L2 claims | `CLAIM_REFUSED` / `FOOTPRINT_MISSING` |
| 4 | Actuator free: no dispatch in flight and not in `RECONCILE` | `ACTUATOR_BUSY` |
| 5 | Freshness, GUI lane only (§5.1) | `STALE_*` / `FRAME_EVICTED` |
| 6 | Admit: take the actuator, add `proposal_id` to `consumed`, return a 128-bit `dispatch_token` | — |

The script lane skips step 5. Its safety comes from disjoint `write_prefixes` (`swarm/CONTRACT.md` §3.1 step 2).
Refusals at steps 2–5 do not consume the id, so the same proposal can be resubmitted. The exception is a park
(§5.2).

### 5.1 Freshness and region-scoped rebase (GUI lane)

Let `b = based_on_frame`.

1. If `b` is not an int, `b < 0`, or `b > H`, it was never published: `STALE_FRAME`.
2. If `F_b` is not in the ring: `FRAME_EVICTED`.
3. **Binding.** If `based_on_sha256` is given and differs from `F_b.sha256`: `STALE_FRAME`. If the declared
   `(view_epoch, scene_epoch)` differ from `F_b`'s: `STALE_FRAME`.
4. If `b == H`: `ADMITTED`.
5. If `b < H`, the proposal is rebased only under **Invariant R**:

> **Invariant R (sound region-scoped rebase).** For `b < H`, admission is `ADMITTED_REBASED` **if and only if**
> all of the following hold:
> - (R1) `F_b.view_epoch == F_H.view_epoch`, otherwise **`STALE_VIEW`**;
> - (R2) `F_b.scene_epoch == F_H.scene_epoch`, otherwise **`STALE_SCENE`**;
> - (R3) `allow_rebase` is true and `target_region` is not null, otherwise **`STALE_FRAME`**;
> - (R4) for every `n` in `(b, H]`, `changed_regions(F_n)` is not `None`, otherwise **`STALE_FRAME`**
>   (a foreign unattributed change gives no region safety);
> - (R5) `( U_{n in (b,H]} changed_regions(F_n) ) ∩ ( U G(P) ) = ∅`, otherwise **`STALE_REGION`**.

The checks run in the order R1, R2, R3, R4, R5, so a frame window that fails several checks returns the first
code. The window is scanned from oldest to newest: an unknown-extent frame that comes before an overlapping change
gives `STALE_FRAME`.

**Why R is sound.** A GUI act's meaning depends on three things: the viewport transform (`view_epoch`), the scene
contents (`scene_epoch`), and the pixels it reads (`target_region`) or affects (`footprint`). R1 and R2 fix the
first two. R4 and R5 show that every pixel changed since `F_b` is known and lies at least `m` px away from the
pixels the act reads or affects. So, restricted to `G(P)`, `F_b` and `F_H` are identical, and the decision the
actor made on `F_b` is the decision it would have made on `F_H`. The margin covers anti-aliasing, hover
highlights and capture jitter at region borders.

**Why R is necessary.** If any condition fails, some state the act depends on may differ, so admitting it could
silently overwrite, or misdirect input onto, content the actor never saw. Each failed condition maps to a distinct
code, so the actor knows the minimum re-observation it needs:

| Code | What the actor must do |
|---|---|
| `STALE_REGION` | Re-read the regions it targets |
| `STALE_VIEW` / `STALE_SCENE` | Re-plan against the new transform or scene |
| `STALE_FRAME` | Re-observe everything |
| `FRAME_EVICTED` | Full `latest` re-observe |

### 5.2 Drops and park

A `STALE_FRAME` refusal increments `drops[proposal_id]`. At `drops == 3` the decision has `parked = true` and the
id joins `consumed`, so later submissions get `DUPLICATE_EVENT` (`swarm/CONTRACT.md` §3.1 step 3).

## 6. Wait-verify

`wait_verify(dispatch_token, post_frame_obs: PostCapture | None, executor_outcome: ExecutorOutcome | None, now_ms)`
closes the in-flight dispatch exactly once.

| Condition | Verdict |
|---|---|
| Token already closed | `DUPLICATE_EVENT` (no state change; never closes twice) |
| Token is not the in-flight dispatch (forged or unknown) | `CLAIM_REFUSED` (the real dispatch stays open) |
| `now_ms > deadline` or `elapsed_ms > cap` (GUI 60 000 ms, script 120 000 ms) | `TURN_TIMEOUT` |
| **Script lane** | |
| Receipt missing, or `event_id != proposal_id` | `OUTCOME_UNCERTAIN` |
| No post-dispatch capture | `OUTCOME_UNCERTAIN` |
| `status == "OK"` and `error` is null | `STATE_VERIFIED` (`scene_epoch + 1`) |
| `status == "ERROR"` | `JOB_ERROR` (published frame has `changed_regions = None`) |
| Any other status, or `OK` with an error text | `OUTCOME_UNCERTAIN` |
| **GUI lane** | |
| No integer `rc`, or capture missing / extent unknown / invalid rects | `INPUT_UNCERTAIN` |
| `rc == 3` and no change | `NO_VISIBLE_CHANGE` |
| `rc == 3` but a visible change | `INPUT_UNCERTAIN` (contradiction) |
| `rc == 0` and no change | `INPUT_UNCERTAIN` (contradiction) |
| `rc == 0`, every change inside `footprint` | `VERIFIED` |
| `rc == 0`, some change outside `footprint` | `VERIFIED_WITH_SPILL` (`spill_regions` listed) |
| Any other `rc` | `INPUT_UNCERTAIN` |

Effects of each verdict:

- **Definite verdicts** (`VERIFIED*`, `NO_VISIBLE_CHANGE`, `STATE_VERIFIED`, `JOB_ERROR`) publish `F_{H+1}`, with
  the changed regions attributed to the actor. Spill regions are included, so later rebases see them.
- `view_change` acts bump `view_epoch`. `input` acts do not: their effect is fully described by the attributed
  regions.
- **Uncertain verdicts** (`INPUT_UNCERTAIN`, `OUTCOME_UNCERTAIN`, `TURN_TIMEOUT`) publish nothing and put the
  controller in `RECONCILE`. Every `propose` then returns `ACTUATOR_BUSY` until `resolve_reconcile(now_ms)`.
  `resolve_reconcile` bumps both epochs and publishes a frame of unknown extent, so no pre-reconcile frame can ever
  be rebased. Nothing is repeated automatically: the id stays consumed.
- Two consecutive `TURN_TIMEOUT`s from one actor revoke its lease (park). Any non-timeout verdict resets the
  count.

## 7. Global invariants

| Id | Invariant | Enforced by | Checked by |
|---|---|---|---|
| I1 | At most one dispatch in flight (zero double actuation) | `_inflight` + lock; RECONCILE gate | `NegativeControlACTUATOR_BUSY`, `MultiPeerConcurrency` (exclusive executor, barrier race) |
| I2 | A `proposal_id` is dispatched at most once | `consumed` set, checked first | `NegativeControlDUPLICATE_EVENT` (incl. 16-thread same-id race) |
| I3 | A dispatch is closed at most once | `_closed` map | `test_second_wait_verify_never_double_closes` |
| I4 | No silent overwrite: an admitted GUI act's guard set was untouched since its base frame | Invariant R | `MultiPeerConcurrency` re-derives it from a test-owned ground-truth log |
| I5 | Ambiguity never becomes success | §6 contradiction rows + RECONCILE | `NegativeControl{INPUT,OUTCOME}_UNCERTAIN`, `VERIFIED`/`NO_VISIBLE_CHANGE` contrasts |
| I6 | Lease epochs are strictly increasing for each actor, and a superseded, expired or revoked lease never authorises | §3 | `NegativeControl{NO_LEASE,LEASE_EXPIRED}` |
| I7 | Refusals have no side effects except the drop counter and park on `STALE_FRAME` | `_refuse` | `snapshot()` equality in refusal tests |

## 8. Code catalogue

| Code | Phase | Meaning |
|---|---|---|
| `ADMITTED` | propose | `b == H`; actuator taken |
| `ADMITTED_REBASED` | propose | `b < H` and Invariant R holds |
| `VERIFIED` | verify (GUI) | rc 0, all change inside the footprint |
| `VERIFIED_WITH_SPILL` | verify (GUI) | rc 0, some change outside the footprint |
| `NO_VISIBLE_CHANGE` | verify (GUI) | rc 3, zero change |
| `INPUT_UNCERTAIN` | verify (GUI) | Missing or contradictory evidence → RECONCILE |
| `STATE_VERIFIED` | verify (script) | Correlated `OK` receipt |
| `JOB_ERROR` | verify (script) | Correlated `ERROR` receipt |
| `OUTCOME_UNCERTAIN` | verify (script) | Missing, uncorrelated or malformed receipt → RECONCILE |
| `STALE_FRAME` | propose | Strict refusal: no region safety, broken binding, or rebase disabled |
| `STALE_REGION` | propose | R5 violated |
| `STALE_VIEW` | propose | R1 violated |
| `STALE_SCENE` | propose | R2 violated |
| `FRAME_EVICTED` | propose | Base frame left the ring |
| `NO_LEASE` | propose | No authority (§3 L1) |
| `LEASE_EXPIRED` | propose | Authority lapsed (§3 L1) |
| `ACTUATOR_BUSY` | propose | Dispatch in flight or RECONCILE |
| `FOOTPRINT_MISSING` | propose | GUI footprint null or invalid |
| `CLAIM_REFUSED` | propose / verify | Outside `write_prefixes`, `allow_gui`, keys or `allowed_regions`; forged dispatch token |
| `DUPLICATE_EVENT` | propose / verify | Id already dispatched or parked; dispatch already closed |
| `TURN_TIMEOUT` | verify | Cap exceeded → RECONCILE; 2 consecutive → park |

## 9. AUSENTE

- How the broker maps a `vision` capture diff to `changed_regions` rects: AUSENTE — not invented here. The
  controller receives the rects in `PostCapture` and `capture`.
- Cross-process sharing of one controller: AUSENTE. The reference is in-process: threads share one lock. A broker
  that hosts it serialises all calls through one process, as `swarm/CONTRACT.md` §1 already requires.
