# ref_auction/judge: readiness judge (Track P, written by Claude Fable 5.2)

Scores how prepared an agent is to execute its queued intent on the capsule **now**.
Output: an integer in `[0, 1000]` plus the feature breakdown. Deterministic, integer-only,
stdlib-only. The turn-order auction (`../scheduler.py`, lane au-auction) consumes it as the
readiness term; the broker's `admit()` stays the only authority.

## Use

```python
from judge import Judge
j = Judge(claims, gui_agents=("camera-agent",))   # or Judge.from_rt(RT) at cold start
j.observe(record)                                  # every record the broker appends to events.jsonl
j.scorer(intent, head_frame)  # -> int 0..1000
j.score(intent, {"head_frame": H})
# {"score": 800, "gate": None, "features": {"freshness": 1000, "claim": 1000, "track": 500,
#  "effect": 500, "cost": 1000, "retry": 1000}, "weights": {...}, "base": 800, "llm_adj": 0, ...}
```

`python3 -m judge --rt RT` prints one JSON line per intent file (read-only).
The pure function is `score_intent(intent, head_frame, history, claims, gui_agents=None, identical_pixels=False, advice=None)`.

## Features (each 0..1000; weights sum to 1000)

| Feature | Weight | Source | Rule |
|---|---|---|---|
| freshness | 250 | `based_on_frame` against head | GUI: 1000 at head, 900 with the caller's byte-identical-pixels proof, else gate. Script: `1000 - lag*600/32` |
| claim | 150 | `write_set` against `claims.json`, GUI lease | 1000 or gate |
| track | 300 | `close` verdicts and `txn` rollbacks of that agent | recency-weighted Beta mean, half-life 4 outcomes, window 16, newcomer 500 |
| effect | 100 | `own_regions` (vision changed regions) of the agent's verified events on that lane | tent over the expected area: invisible 200, local (0.5%..25% of the screen) 1000, full repaint 400, no evidence 500 |
| cost | 100 | lane | script 1000 (minus 100 per 4 KiB, at most 300); GUI `700 - 150*(keys-1)`, floor 100 |
| retry | 100 | `drops` of this event | 1000 / 600 / 300 |

Gates force **score 0** and name the reason: `BAD_INTENT`, `CLAIM_MISMATCH`, `NO_LEASE`,
`STALE_FRAME`, `FRAME_EVICTED`, `PARKED` (3 drops). They mirror `ref_broker` ADMIT, so the
judge never ranks an intent the broker would refuse.

Track mass per outcome: good 1 (`VERIFIED`, `STATE_VERIFIED`); bad 1 for soft refusals
(`STALE_FRAME`, `NO_VISIBLE_CHANGE`, `VERIFIED_WITH_SPILL`, `TURN_TIMEOUT`, `FRAME_EVICTED`),
2 for hard ones (`JOB_ERROR`, `CLAIM_REFUSED`, uncertain outcomes, unknown codes), 3 for a
`txn` rollback (it replaces the close of the same event: counted once). `PASS`,
`QUEUE_FULL` and `ACTUATOR_BUSY` are ignored (not the agent's doing).

## LLM judgement (optional, never in a turn)

`python3 -m judge.llm_advice --rt RT` with `VP_JUDGE_LLM_CMD` set to a command that reads a
prompt on stdin and prints `{"adj": N, "why": "..."}`. It writes `RT/judge_advice.json`.
The hot path only reads that parsed dict: the adjustment is clamped to +-100, bound to the
sha256 of the intent (an edited script loses its advice), ignored when malformed, and it
cannot lift a gated intent. Gated intents are never sent to the model.

## Replay audit of the auction log

`python3 -m judge.replay --rt RT` (rc 0 clean, rc 1 violations, `--llm` tolerates the +-100 advice).
From `events.jsonl` alone it rebuilds the judge's memory record by record and recomputes the
`readiness` of every eligible row of every `auction` record from the records that precede it.
Codes: `READINESS_MISMATCH` (judge not consulted, stale memory, or a swallowed scorer exception
recorded as 0), `UNKNOWN_INTENT`, `GATED_WINNER` (readiness 0 won a plain `auction`),
`NO_HEAD_FRAME`. Bids, prices, balances and the wait bound belong to `scheduler.verify_log`.

## Verified (2026-11-14)

`cd product/ref_auction && python3 -m unittest judge.test_judge judge.test_replay` : 53 tests pass (42 + 11).
- Replay mutation controls (scratch copy, each red): mismatch check off (7), judge fed before the audit (4),
  gated-winner check off (1), LLM tolerance lifting gates (1).
- Cross-check with the cell's `scheduler.py` (12 picks, 4 agents, one agent always rolled back, one GUI agent
  going stale): with `Judge.scorer` the replay is clean (48 rows, 0 violations) and the failing agent gets 3
  turns of 12; with the scheduler's stand-in scorer the replay reports 38 `READINESS_MISMATCH` and it gets 4.
- Negative controls: a stale GUI intent scores 0 even with a perfect record; an agent with
  3 recent rollbacks scores lower with every other feature equal; claim theft scores 0.
- Mutation controls run on a scratch copy (each one turns the suite red): stale gate removed (4 failures),
  rollbacks ignored (3), claim check removed (3), advice not bound to the intent (1), no recency decay (1),
  GUI cost equal to script (1).
- Calibration on synthetic Bernoulli histories: mean track within 25/1000 of the analytic Beta mean at
  success rates 0.1..0.9 (300 seeds each), monotone; a 0.9 agent outranks a 0.5 agent in >= 90% of 400 pairs.
- Replay of the real `ref_broker/evidence/events.jsonl` (43 records): runs, scores 664..800.
- Hot path: about 5 microseconds per `score_intent`, 4 per `feed` (measured on this host), no file or process access
  (a test patches `open` and `subprocess.run` to raise).

## Not verified

- No real model was called: the LLM path is tested with fake commands only.
- Weights and severities are reasoned defaults, not fitted: there is no real multi-agent failure corpus yet
  (the only real log has 5 events and no rollback).
- `STALE_FRAME` costs twice for the resubmitted event (track and retry). Intended as backoff, not measured.
- `identical_pixels` must be supplied by the caller (the broker's `_pixels_identical_since`); without it a
  GUI intent behind the head is gated although the broker might auto-rebase it.
- Not wired into a live broker here; the scheduler plug-in does that.
