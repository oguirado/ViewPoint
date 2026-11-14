# ref_auction: turn-order auction for the ViewPoint turn queue

**Provenance: Track P, written by Claude (claude-fable-5-2). Not Gemini work; not part of `swarm/`.**
Status: roadmap candidate with tests. It is NOT wired into the live broker: the frozen H1 rule
(round-robin) stays until a rehearsal A/B says otherwise. `judge/` is a separate unit (au-judge).

## What it is

A plug-in scheduler that replaces `agent = ring[turn % N]` in `product/ref_broker`:

    pick = scheduler.pick(view)            # proposal; the broker's admit() remains the only authority
    recs = scheduler.settle(pick, close)   # after the broker wrote its close record

Policy `vickrey_aging_v1` (decisions D1-D3 of au-market, room `viewpoint-auction`), per turn:

| Step | Rule (integers only, no clock, no I/O) |
|---|---|
| Eligible | FIFO head of every unparked agent with a queued intent |
| Bid | sidecar `RT/bids/<agent_id>/<event_id>.json` `{v,rec:"bid",agent_id,event_id,bid}`; missing or malformed = 0; `bid_eff = min(bid, balance, bid_cap)` |
| Readiness | 0..1000 from the judge (`view.scores` or `scorer(intent, head_frame)`); default: GUI intent on a non-head frame = 0 |
| Score | `bid_eff + alpha*wait + readiness*r_max//1000`; readiness 0 gives score 0 and cannot beat a ready intent |
| Fairness | `wait >= w_max` (default 2N): longest waiter wins, price 0, `reason: aging_guard`. Picked within `w_max + N - 1` picks |
| Ties | ring order starting after the last served agent |
| Price | `clamp(runner_up_score - alpha*wait_w - bonus_w, 0, bid_eff_w)`, charged at pick, refunded on `ACTUATOR_BUSY` |
| Budget | play money held by the scheduler: start 2N, +1 per agent per closed tick, cap 4N, bid cap 2N |

Records appended by the broker to `RT/events.jsonl` (new kinds; no frozen record is changed):
`auction` (turn, tick, policy, params, head_frame, eligible rows with bid / bid_eff / wait / readiness /
bonus / score / balance, winner, event_id, reason, price, runner_up, last_served, balances, waits,
scorer_errors when a judge call failed) written immediately before its `turn` record, and
`auction_settle` (verdict, charged, refunded, held, income, clipped, balances).
`scheduler.verify_log(records, ring, params=None)` re-decides every record from its raw fields with PINNED params (the `params` you pass = run configuration, else the first record; drift and disabled safety switches are flagged) and re-derives every agent's balance at pick and settle (charged == pick price, refund rule, income 0 or params.income, cap clip). Limits: without `params` a log that forges the same params in every record passes; whether income was due (tick closed) is not re-derived from the broker close record; a pick left unsettled by a crash is not flagged.

## Files

| File | Role |
|---|---|
| `scheduler.py` | `AuctionScheduler`, `RoundRobinScheduler` (baseline), pure `decide()`, `verify_log()` |
| `broker_adapter.py` | `view_from_broker`, `read_bid`, `make_auction_broker(Broker)`, `attach_judge`, `patched_source` |
| `broker_hook.patch` | the one-line seam ref_broker needs (`_pick_agent`); not applied by this unit |
| `sim.py` | deterministic model broker; `python3 sim.py` prints the A/B against round-robin |
| `test_scheduler.py`, `test_broker_hook.py` | tests and negative controls |

## Run

    cd product/ref_auction
    systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
      python3 -m unittest test_scheduler test_broker_hook

`test_broker_hook` copies `ref_broker` (env `VP_REF_BROKER_DIR`, default the F1 worktree) to a temp
directory, applies the seam there and runs the broker's own suite plus auction scenarios on the copy;
it is skipped when the broker is absent.

Use with a seam-patched broker:

    AB = make_auction_broker(broker.Broker)
    b = AB(rt, job, nonce)
    b.scheduler = AuctionScheduler(b.agents).restore(records_of(rt + "/events.jsonl"))
    attach_judge(b)          # optional: au-judge readiness instead of the default

## Verified (this machine, 14 Nov 2026)

- 36 tests pass. Negative controls: starvation (guard off: a never-ready agent gets 0 of 300 turns),
  bid inflation (clamp and guard off: a 10^9 bidder takes 100 % of turns), stale frame (readiness off:
  the stale max bid is served first), tampered records (replay flags them).
- 9 source mutations of `scheduler.py` (guard, clamp, readiness tier, stale gate, first price, refund,
  charge, wait reset, tie order) each turn the suite red.
- The seam alone changes nothing: ref_broker's own 34 tests pass on the patched copy.
- Zero bids on saturated queues give exactly the round-robin served order.
- Synthetic A/B, 400 turns, same trace: round-robin 320 VERIFIED + 80 STALE_FRAME; auction 400 VERIFIED,
  0 STALE_FRAME, longest gap 6 picks (bound 11), shares 30/30/20/19 %.

## Not verified / limits

- No live Blender run, no recorded real trace: the A/B is a model with a coarse re-observe rule.
- Zero bids equal round-robin only on saturated queues; with staggered arrivals `alpha*wait` serves the
  longest waiter, not the next in ring (test `test_documented_gap...`).
- Incentive claim is per turn only (critical price); nothing is claimed across turns with budgets.
- Bids are read at pick time, not frozen at ingest; RT files are world-readable, so bids are not sealed.
- Event ages and the escrow of a pick open at a crash are not restored (`restore` rebuilds balances,
  waits and last served from the last records).
- `pixels identical since frame` is not passed to the judge: an auto-rebasable GUI intent scores 0 and
  waits for the aging guard.
