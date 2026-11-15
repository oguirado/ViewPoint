# `wender-agent` leased actor lock

Design for `wender_leased_lock.patch`, which adds an epoch-numbered,
TTL-bounded lease with fencing-token checks to `wender/act/actor.py`.

## Problem

`wender.act.actor.ActorLock` is a non-blocking single-actor mutex. It records
`holder` and nothing else:

- It has no deadline. A holder that stalls keeps the surface until it calls
  `release`.
- It has no generation. If a supervisor force-releases it after `TURN_TIMEOUT`
  and grants the surface to the next actor, the first holder still believes it
  owns the turn. When it wakes up it injects X11 input into the next actor's
  turn, and nothing between it and the X server can tell the two apart.
- `execute(path, action)` is a stub, so there is no single place where
  ownership is checked before input is sent.

## Design

The patch is additive: `ActorLock`, `ActorBusy`, `choose_path` and `execute`
are unchanged, so existing callers and tests keep working. New names:

| Name | Role |
|---|---|
| `Lease` | Immutable fencing token: `holder_actor_id`, `lease_id`, `turn_epoch`, `expires_ns` |
| `LeasedActorLock` | Lease authority for one surface |
| `LeaseRefused` | Typed refusal, `code` is `LEASE_EXPIRED` or `NO_LEASE` |
| `execute_fenced` | Executor entry point that checks the lease first |
| `DEFAULT_TURN_TTL_NS` | 60 s, the GUI `TURN_TIMEOUT` of `swarm/CONTRACT.md` |

### State

`LeasedActorLock` holds, under one short mutex: the current `Lease` or `None`,
`turn_epoch` (starts at 0, only ever increases) and a count of executor calls
in flight.

### Operations

| Call | Effect |
|---|---|
| `grant(who, ttl_ns=None)` | Refuses with `ActorBusy` if a live lease exists or an act is still draining. Otherwise `turn_epoch += 1` and returns `Lease(who, "lease-<who>-<epoch:04d>", turn_epoch, now + ttl)`. `ttl_ns` must be in `1..max_ttl_ns`. |
| `actuate(lease, executor, *args, budget_ns=0)` | Checks the token, then calls the executor. Refuses with `LeaseRefused` without calling it. |
| `verify(lease, budget_ns=0)` | The same check without acting. |
| `renew(lease, ttl_ns=None)` | Valid lease only. Same `lease_id` and `turn_epoch`, new deadline `now + ttl`. |
| `release(lease)` | Valid lease only. A stale holder cannot release its successor's lease. |
| `revoke()` | Supervisor path for `TURN_TIMEOUT`. Ends the turn at once, returns the revoked lease. |
| `holder`, `current()` | Read-only views. `holder` matches the `ActorLock.holder` attribute. |

### The fencing check

Run under the mutex on every `actuate`, `verify`, `renew` and `release`:

1. The token is not a `Lease` → `NO_LEASE`.
2. A lease whose deadline has passed (`now >= expires_ns`) is dropped first.
3. No current lease, or `token.turn_epoch != current.turn_epoch`:
   `LEASE_EXPIRED` if the epoch was granted before (`0 < epoch <= turn_epoch`),
   else `NO_LEASE`.
4. `lease_id` or `holder_actor_id` differ from the current lease → `NO_LEASE`.
5. `now + budget_ns >= current.expires_ns` → `LEASE_EXPIRED`.

The deadline compared is the lock's own, never the one carried in the token, so
a holder cannot extend its turn by editing its token. `renew` moves the lock's
deadline and older tokens of the same lease stay valid.

### Why a stale holder cannot inject input

A holder that sleeps through `TURN_TIMEOUT` reaches the check in one of three
states, and each refuses before the executor runs:

- Nobody took the surface yet: step 2 drops the lease, step 3 answers
  `LEASE_EXPIRED`.
- The supervisor called `revoke()`: step 3 answers `LEASE_EXPIRED`.
- The next actor already holds epoch `n+1`: the token's epoch `n` fails step 3.
  This also covers the same actor being granted a later turn; an old token
  never comes back to life.

### Check-then-act window

Checking and injecting are not one atomic step, so two measures bound the gap:

- `budget_ns`: the caller states how long the act takes. A lease that would
  expire before the act can finish is refused up front.
- Drain barrier: an admitted call is counted while its executor runs, and
  `grant` refuses with `ActorBusy` until the count is zero. An act that was
  admitted just before the deadline may finish, but it never overlaps the next
  holder. `revoke()` does not wait, so the supervisor is never blocked by a
  hung act.

The consequence is that executors must be bounded by their own timeout. A hung
executor keeps the surface unavailable (`ACTUATOR_BUSY` in contract terms)
until the supervisor kills the recorded process; it does not let two actors
type at once.

### Clock

`expires_ns` comes from `time.monotonic_ns()`, so wall-clock steps cannot
shorten or extend a turn. The clock is injectable for tests. The value is only
comparable within one boot of one host; `expires_ts_ms` in the contract's
`claim` record is the wall-clock rendering for logs and must not be used for
the check.

## Mapping to `swarm/CONTRACT.md`

| Contract | Patch |
|---|---|
| `agent_id` | `holder_actor_id` |
| `lease_id` (`lease-modeler-0001`) | `lease_id` (`lease-<who>-<epoch:04d>`) |
| `lease_epoch` / `turn_epoch` | `turn_epoch` |
| `expires_ts_ms` | `expires_ns` (monotonic) |
| `LEASE_EXPIRED`, `NO_LEASE` | `LeaseRefused.code` |
| `ACTUATOR_BUSY` | `ActorBusy` from `grant` while an act drains |
| `TURN_TIMEOUT` → `turn_epoch + 1` | `revoke()` then the next `grant` |

## Limits

- The lock is in-process. The fence only holds if every input path of the
  process that owns the display goes through `actuate` / `execute_fenced`.
  The patch adds the entry point; it does not rewire `cdp_executor`,
  `coordinates` or `godot_executor`, and `TargetLockRegistry` is untouched.
  Wiring those call sites is the follow-up.
- The X server does not check tokens. An input already written to the socket
  cannot be recalled; the drain barrier is what keeps it out of the next turn.
- Tokens are not signed. A caller in the same process that knows the current
  `lease_id` and epoch can forge one. This guards against stale holders, not
  hostile ones.

## Verification

- Base file: `wender/act/actor.py`, sha256
  `39b8c97fab628a9902c6babb53af93401b72a03ac31d93558ab700db0e7c9314`, identical
  in the three `wender-*` checkouts under `~/.cache/vis/` (read
  only).
- `patch -p1 < wender_leased_lock.patch` applied cleanly to a scratch copy of
  that file, and wender's own `tests/test_fx2_f2_j3_actor_lock_concurrency.py`
  (12 tests) passed against the patched module. The checkouts themselves were
  not modified.
- `test_fanout.py::TestLeasedLock` executes the lines the patch adds (nothing
  re-implemented) and covers: expiry at the exact nanosecond, a stale holder
  against a live successor, revoke, same-actor re-grant, forged and
  self-extended tokens, renew, the budget guard, a threaded holder waking after
  the timeout, and the drain barrier.

Apply from a `wender-agent` checkout root:

```bash
patch -p1 < wender_leased_lock.patch    # or: git apply wender_leased_lock.patch
```
