# Capsule gate suite (Track P, Claude-written)

Written by claude-fable-5-2 (lane ct-gates, wave w186). Declared product work, not hackathon code.
Stdlib Python 3 only. No network. Starts no container, stops no process, never prints a secret.

## What a gate is

A gate compares a **desired** state (a rule of 04_LANZAMIENTO / COMMON.md / the vp-capsule/v1 spec)
with an **observed** state (a proof log, a source tree, git, a cgroup, the broker ledger, an engine
snapshot) and returns one of three statuses.

| Status | Meaning | Exit code |
| :--- | :--- | :--- |
| GREEN | every observation satisfies the rule | 0 |
| RED | at least one observation violates it; finding carries `file:line` | 1 |
| UNOBSERVED | nothing, or not enough, was observed; this is **not** a pass | 2 |

UNOBSERVED is the reconciliation idea taken from PapersKubernetes KU-0001 C1 ("report the resulting
obstruction or uncertainty rather than deriving success from queue activity") and PapersContainers
CT-0022 C1 (keep desired state, received events, current observations and completed effects as
separate records). A missing log, an open stream log, a stale snapshot, a bridge that cannot be
probed and an unreadable cgroup all end as UNOBSERVED.

## Gates

| Gate | Desired | Observed | Red rules |
| :--- | :--- | :--- | :--- |
| G1 `g1_paths.py` | nothing touches X display 1, the hybrid and swarm runtimes under `/dev/shm/vqa-cu/`, or the repo `swarm/` tree | tool calls in `proof/*.jsonl`; string constants of `*.py`; lines of `*.sh`; `git diff <base>...HEAD -- swarm/` and `git status -- swarm/` | DISPLAY_1, FORBIDDEN_RT, SWARM_TREE_WRITE, SWARM_TREE_COMMIT, SWARM_TREE_DIRTY |
| G2 `g2_secrets.py` | no credential value in a proof log; no recorded read of a credential file; no environment dump | every line of the logs (format independent) and their tool calls | GOOGLE_API_KEY, GOOGLE_OAUTH_TOKEN, GOOGLE_REFRESH_TOKEN, ANTHROPIC_KEY, OPENAI_KEY, GITHUB_TOKEN, SLACK_TOKEN, AWS_ACCESS_KEY, PRIVATE_KEY_BLOCK, JWT, BEARER_HEADER, CREDENTIAL_FILE_READ, ENV_DUMP |
| G3 `g3_memcaps.py` | tests, Blender, ffmpeg, docker under `MemoryMax <= 8G`, `MemorySwapMax=0`; `docker run` with its own `--memory` and `--pids-limit`; `vision start` with `--mem` | declared: recorded commands and `*.sh`; effective: `min(memory.max)` over the cgroup-v2 ancestors of a live PID and the leaf `memory.swap.max` | UNCAPPED, NO_MEMORY_MAX, CAP_TOO_HIGH, SWAP_ALLOWED, DOCKER_NO_MEMORY, DOCKER_NO_PIDS_LIMIT, VISION_NO_MEM, EFFECTIVE_UNCAPPED, EFFECTIVE_CAP_TOO_HIGH, EFFECTIVE_SWAP_ALLOWED |
| G4 `g4_pidstops.py` | a process is stopped through a recorded PID only | recorded commands, `*.sh`, argv-like string constants of `*.py` | NAME_BASED_STOP, PATTERN_PID, PID_FROM_PIPE, KILL_ALL, NOT_A_PID |
| G5 `g5_nonce.py` | the bridge runs only `<nonce>_<event_id>.py`, quarantines the rest in `inbox/rejected/`, answers `OK`/`ERROR` | behavioural probe: 1 valid, 1 failing and 8 forged jobs in a temp RT; the gate looks at a sentinel file each job would write, not at the bridge's log | FORGED_JOB_EXECUTED, FORGED_JOB_NOT_QUARANTINED, VALID_JOB_NOT_RUN, NO_OUTBOX_ANSWER, WRONG_STATUS_LINE, OUTBOX_MAC_MISMATCH, BRIDGE_CRASHED |
| G6 `g6_claims.py` | `claims.json`; 0 accepted cross-claim jobs | `events.jsonl`: requested `write_set`, decided `verdict`, measured `changed_write_set` / `spill_write_set` | CROSS_CLAIM_ACCEPTED, SPILL_ACCEPTED, REFUSED_BUT_EFFECT, UNKNOWN_AGENT, ORPHAN_CLOSE, CLAIMS_OVERLAP |
| G7 `g7_reconcile.py` | vp-capsule/v1 spec (ct-capsule) | snapshot `{observed_at_ms, inspect, effects}` and, optionally, the containers' own cgroup directories | ct-capsule rule ids from `validate.py` (desired) and `observe.py` O-00..O-07 (applied), plus R-RUNNING, R-INCARNATION, R-KERNEL; R-FRESH is UNOBSERVED |

G7 emits a `plan` (start / restart / replace / stop_by_id). The plan is data; nothing executes it.

## Why G3 and G7 read the kernel

Declared is not effective (KU-0007: "what evidence connects declared resources to effective
hierarchical cgroup constraints"; construction success is not a receipt). Two cases on this host:

- A command inside `systemd-run --scope` is capped by the scope. G3 `check_pid` reads
  `/proc/<pid>/cgroup` and takes the minimum `memory.max` over the ancestors. The live test starts a
  child under `MemoryMax=8G` (GREEN, effective 8589934592 [MEASURED]) and under `MemoryMax=9G` (RED).
- A docker container is a child of the daemon and lives in `system.slice/docker-<id>.scope`, outside
  the caller's scope (ct-audit F1, measured by ct-capsule on `srf-egress-proxy`). A scope around
  `docker run` caps the CLI only. So G3 demands `--memory` and `--pids-limit` on the docker argv, and
  G7 R-KERNEL compares the container's own `memory.max` / `pids.max` with the spec. The test
  `test_red_kernel_disagrees_with_engine` shows a snapshot that `observe.py` accepts and the kernel
  refutes.

## Run

```bash
cd product/ref_capsule/gates
CAP="systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0"
$CAP python3 -I -B -m unittest discover -s tests -v      # 60 tests
$CAP python3 -I -B tests/mutation_sweep.py               # removes one check at a time; every mutant must be killed
$CAP python3 -I -B run_gates.py --manifest manifests/example.json --json report.json
```

Manifest keys: `logs`, `sources`, `worktrees`, `base`, `pids`, `bridges`, `claims`, `events`,
`capsule_spec`, `capsule_snapshot`, `capsule_cgroot`, `freshness_ms`, `waivers`. All optional; a gate
without input is UNOBSERVED.

## Negative controls

Every gate has tests that feed it a violating observation and assert RED with the exact rule id.
`tests/mutation_sweep.py` then removes the check itself (20 one-line mutants across the 7 gates, the runner and
the status function) and requires the test suite to fail for each: 20 of 20 killed [MEASURED].
G5 is tested against mutated bridges (nonce compare removed, prefix compare, empty nonce accepted,
no quarantine, ERROR reported as OK, crash), including a mutant of `product/ref_broker/bridge.py`
when that worktree is present.

## Waivers

A negative control of a guarded unit legitimately names a forbidden thing (a test that proves
`record.sh` refuses display 1 calls it with display 1). Such a finding is waived only by an explicit
manifest entry with rule, place and reason; it stays in the report with severity `waived`. In source
files the marker `# gate-ok: <reason>` on the line does the same for static findings.

## Limits (what is not verified)

- Log gates see what a stream log records. A command run outside a recorded tool call is invisible.
- Shell analysis is a tokenizer, not a shell: `eval`, aliases, functions and variables holding a
  command name are not expanded. Heredoc bodies are treated as data.
- G1 judges references to a forbidden path, not intent: reads are red too ("never touch").
- G2 value rules are pattern based; an unknown token format passes. GENERIC_ASSIGNMENT,
  RUN_NONCE_LITERAL and ENV_FILTERED are warnings and do not turn the gate red.
- G5 imports the bridge in-process and needs `poll_once(rt, nonce, namespace)`; other bridges are
  UNOBSERVED. It does not start Blender.
- G6 trusts that the ledger was written by the broker; it does not authenticate the ledger.
- G7 has no collector: the operator produces the snapshot (render plan `inspect` argv). No container
  was started in this wave, so R-KERNEL has only been exercised on a synthetic cgroup tree.
- Room-level claim discipline (an osb `claim` before each worktree commit) is not gated yet.
