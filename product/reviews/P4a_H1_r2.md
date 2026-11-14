# P4a_r2 Re-Review of `H1_r2` (`swarm/broker/`): Fable Reviewer, Track P

- Reviewer unit: P4a (`claude-fable-5-2`), findings only, no edits outside this file.
- Reviewed after the `H1_r2` writer exited (PID file `swarm/proof/H1_r2_1305.pid`; process gone after about 110 s of waiting [MEASURED]).
- Source hashes were identical before and after probing; all probes ran on the copy `/tmp/vp_p4a_r2_probe` inside the memory-capped `systemd-run` scope.
- Previous round: `product/reviews/P4a_H1.md` (REJECTED on L1).

## Files reviewed (SHA-256)

| File | SHA-256 |
|---|---|
| `swarm/broker/bridge.py` | `c337aa3f2fae460bf289f82ce6f8b19255768a97b2489b8875c996358d7939b4` |
| `swarm/broker/broker.py` | `5c3f4616c63a2f9cd6f77b36a1dbfcde37e5a6d5e4c83d4a78da0a9d11a6109f` |
| `swarm/broker/test_broker.py` | `76fe69a2e4fd46315641f011028317f57df78d5d483ff841efb88c55db6b6b2d` |
| `swarm/broker/README.md` (unchanged) | `aa2095448edbd38a68a92917d58c526e91791678d09f78f36d00a95518944950` |
| `swarm/broker/__init__.py` (unchanged) | `48863c4e39ddd5c9928d62fad97c2ae62b504d3a2a9223ccd4ceed4b6a4258af` |

## VERDICT: APPROVED

L1 is repaired and holds under every exit path probed. B1, B2 and B7 are repaired. No BLOCKER and no live-path regression was found. Everything left is BACKLOG.

Two launch notes for the operator (not code defects):

1. **No Blender running `swarm/broker/bridge.py` existed at 13:11 CET** [MEASURED]; the process from round 1 (PID 3984943) is gone. The next `vpswarm` launch will load the repaired bridge. If a capsule was started before 13:07 CET (the bridge's last write), relaunch it.
2. **`swarm/broker/README.md` still omits `--auto-rebase-viewport`** from the live command. Launch with the flag, as noted in round 1.

## Test results

- H1 suite on the copy: 19/19 pass in 4.228 s [MEASURED] (16 from round 1 plus tests 16, 17, 18).
- Independent probes (`/tmp/vp_p4a_r2_probe/scripts/probe.py`): 39/39 pass [MEASURED].
- The first probe run reported 13 failures. All 13 were a mistake in my own check (it counted the broker's frame-level `PASS` records as extra closes); the broker's behaviour was already correct in that run. The corrected run is the one cited.

## Gate checklist

| # | Gate item | Result | Code | Probe evidence [MEASURED] |
|---|---|---|---|---|
| 1a | L1: bridge contains `SystemExit` / `KeyboardInterrupt` | PASS | `swarm/broker/bridge.py:176-184` | 14 scripts run through the default executor, none escaped `process_inbox_once`: `sys.exit(0)`, `sys.exit('bye')`, `raise SystemExit(3)`, `raise KeyboardInterrupt()`, `exit()`, `quit()`, `GeneratorExit`, a custom `BaseException` subclass, `SyntaxError`, `RecursionError`, an exception whose `__str__` raises `SystemExit`, exit raised in a `finally`, exit after the script replaced `sys.stdout`, exit after printing a lone surrogate. A non-UTF-8 job file is also contained. |
| 1b | L1: job file removed on the first scan | PASS | `swarm/broker/bridge.py:185-199` | In all 15 cases the inbox file was gone after one scan. |
| 1c | L1: `ERROR` receipt names the exception class | PASS | `swarm/broker/bridge.py:181-184`, `swarm/broker/bridge.py:201-214` | In all 15 cases the receipt had `status` `ERROR` and the class name in `error`. |
| 1d | L1: next job succeeds | PASS | same | After each of the 14 scripts, a following job returned `OK` with its stdout captured and the inbox empty. |
| 1e | L1: timer callback returns the normal interval | PASS | `swarm/broker/bridge.py:223-229` | Fresh interpreter, paths from environment, 5 queued jobs (`sys.exit(0)`, `raise SystemExit(3)`, `raise KeyboardInterrupt()`, `exit()`, then a good one): 6 calls to `_timer_tick` each returned 0.2, statuses were `ERROR` x4 then `OK`, inbox empty, `sys.stdout` restored, child exit code 0. With the inbox path pointing at a regular file (infrastructure failure), 6 calls still returned 0.2. |
| 1f | L1: end to end through the broker with the real bridge | PASS | `swarm/broker/broker.py:985-1075` | Six intents across two agents, four exiting scripts then two good ones: `JOB_ERROR` x4, `STATE_VERIFIED` x2, exit code 0, queue `IDLE`, inbox empty, 0.47 s total (no timeout wait). |
| 2a | B1: strict `event_id` matching | PASS | `swarm/broker/broker.py:53`, `swarm/broker/broker.py:238-243` | 16 hostile ids (`"ok\n"`, `".."`, `"a..b"`, `"ok\r"`, leading newline, NUL, traversal, separator, trailing space, empty, non-ASCII digit, 129 chars, leading dot, leading dash, U+2028): 16 `CLAIM_REFUSED`, a good intent behind them `STATE_VERIFIED`, 0.08 s total, nothing reached the inbox or the bridge's `rejected/`. |
| 2b | B7: strict job filename matching | PASS | `swarm/broker/bridge.py:35`, `swarm/broker/bridge.py:144-154` | 12 names rejected without execution: trailing newline, leading newline, trailing space, sequences `01`, `007`, `00`, `-1`, a non-ASCII digit, `..` in the id, an id of only dots, a leading-dot id, a wrong nonce. 3 names accepted and run: sequence `0`, sequence `10`, upper-case nonce. |
| 2c | B7 regression check: broker never emits a leading-zero sequence | PASS | `swarm/broker/broker.py:963-964` | All 6 job names written by the broker in probe 1f match the bridge's stricter pattern. |
| 2d | B2: lone surrogates | PASS | `swarm/broker/broker.py:246-258`, `swarm/broker/broker.py:412-424`, `swarm/broker/broker.py:692-703`, `swarm/broker/broker.py:925-937`, `swarm/broker/bridge.py:84`, `swarm/broker/bridge.py:103` | Lone surrogate in 12 positions (script, `expect`, `kind`, `lane`, `action_class`, a `write_set` element, a dict key, a nested list, `agent_id`, `event_id`, `script_sha256`, a low surrogate): no `UnicodeEncodeError`, one `CLAIM_REFUSED` each, the next agent's intent `STATE_VERIFIED`, exit code 0, log pure ASCII. A valid surrogate pair is still accepted. A script that prints or raises a surrogate built at run time comes back through the receipt without a crash. Four director control records carrying surrogates: no crash. |
| 3 | Unit suite | PASS | `swarm/broker/test_broker.py:1156-1420` | 19/19. |

## Findings

### BLOCKER

None.

### LIVE_PATH

None. L1 from round 1 is closed.

### BACKLOG

Items N1 to N4 are new in this round. None affects the 14:00 run under normal conditions.

**N1. `exit()` and `quit()` close the host process's standard input.** Measured: after a job calls either one, `sys.stdin` is closed in the bridge process. The job is still contained and receipted. In a headless Blender capsule nothing reads standard input, so the effect is cosmetic. `swarm/broker/bridge.py:106-109`.

**N2. A removal failure other than "file not found" skips the receipt.** By reading, not probed: if the job file cannot be deleted (for example a permissions error on the inbox), the error leaves the cleanup block before the receipt is written. The timer survives, but the same job would run again every tick and the broker would see a timeout. The inbox is on `/dev/shm` and owned by the same user, so this needs an environment fault to occur. `swarm/broker/bridge.py:196-213`.

**N3. A malformed nonce file is still used as the nonce.** By reading, not probed: file content that fails the 8 to 64 hex rule is returned unchanged, so a short hex value would be honoured. The broker always writes a conforming nonce, so this only matters if something else writes the file. `swarm/broker/bridge.py:70-75`.

**N4. One broker JSON writer still emits non-ASCII.** The state-file writer was not switched to ASCII-only output. No crash was reachable in the probes, because surrogate-bearing intents are refused before anything is persisted. `swarm/broker/broker.py:113-114`.

**Carried from round 1, not re-examined here:** B3, B5, B6, B8, B9, B10, B11, B12, B13 stay as written in `product/reviews/P4a_H1.md`. B4 (log without a final newline) has a repair at `swarm/broker/broker.py:415-422` and a passing case in suite test 18; I did not probe it independently.

**Outside what an in-process bridge can contain:** a script that kills the process directly or never returns. The broker's timeout and `RECONCILE` path is the only defence, as in round 1.

## What was not checked

- Blender 4.0.2 itself was not launched; the timer callback was exercised in plain Python 3.12 only.
- The H1↔H2 and H1↔H4 seams were not re-probed; the suite's existing cases pass.
