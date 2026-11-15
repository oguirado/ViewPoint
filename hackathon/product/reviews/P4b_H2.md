# Review P4b_H2 — Fable re-review of `H2_r1` (`swarm/agents/`)

- **Reviewer model**: `claude-fable-5-2` (Track P, reviewer unit P4b, findings only)
- **Timestamp (UTC)**: 2026-11-14T11:58:30Z
- **Reviewed tree**: `~/Documentos/viewpoint-hackathon` (branch `swarm-1114`), read-only
- **Write surface**: this file only (`product/reviews/P4b_H2.md` in worktree `vp-wt-P4b`, branch `p-P4b`). Nothing under `swarm/` or in the main checkout was created, edited or deleted.
- **Probe sandbox**: byte-identical copy of `swarm/agents/` and `swarm/broker/` in `/tmp/vp_p4b_probe`, probe scripts in `/tmp/vp_p4b_scripts`, every run inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0` with `PYTHONDONTWRITEBYTECODE=1`. No `agy` agent was launched (only `agy --help`), no `vision` call, no write under `/dev/shm/vqa-cu/`.
- **Prior review**: `product/reviews/H2.md` (P4, `claude-opus-5-5`, REJECTED: B1 + M1..M6).

## Files reviewed

| File | SHA-256 |
|---|---|
| `swarm/agents/__init__.py` | `adbdc812b1385cf07f670dbdaf57be82fc47e21a13edb777a977d71c1c5e434f` |
| `swarm/agents/intent_cli.py` | `20eb195d9e7afbcf0de6acb28b7d4afa4f5aae27d7d518172528e5138237080c` |
| `swarm/agents/prompts.py` | `7630dad0c4e6537ee2c58aad740190f12de61e564a0c49b44612149d3f489a2b` |
| `swarm/agents/README.md` | `12db039a052a3f207029de417d63d0f9d24dc92ec488b3e79c1af99a6ab9a392` |
| `swarm/agents/runner.py` | `1bfee6433fb43279bf41df14bfa39efea47cbc1721db67b57ea081097d942b6e` |
| `swarm/agents/scripted_smoke.py` | `c16cf31bd9a931f8966d5d55edecff1b00e56cdfebf4e1905ab36b1dbb6c40cd` |
| `swarm/agents/test_agents.py` | `729b9786aaea03697a7ca90afd9d92815fa535eb593a4649ae05765868ce3c49` |

Cross-reference only (seam verification, not under review):

| File | SHA-256 |
|---|---|
| `swarm/CONTRACT.md` | `b392f7dd16a3689b4af34e66a25a990dc131eb07d4f34fc7c47312069eec8fa4` |
| `swarm/broker/broker.py` | `b58c741142f6d6740fd0a650de9f9c90dc7bc0553e934342cf7b74dc7e36dbe5` |
| `swarm/broker/bridge.py` | `5c1d67c6a01240275e7ee27ad8418ca93bdd90b190e8fd904538319185ae9cb1` |
| `product/reviews/H2.md` | `8010015b3dbd3a100041c5eec0c1483932272de20d61dd7b62ee6fa16f226c16` |

`swarm/agents/test_agents.py` was run but not read line by line; the scripted-smoke Blender payloads were read but not executed in Blender.

## VERDICT: REJECTED

No BLOCKER remains: the B1 false-success path is closed on the main path and the 8 GiB cap now reaches the `agy` process (both measured). The rejection rests on two live-path defects:

1. **L1** — the final deliverables are written with paths relative to Blender's working directory, which is `~`, not the repo. This is deterministic for the documented live smoke command and is what the camera brief tells the LLM to type.
2. **L2** — an `event_id` with a trailing newline passes H2 and H1 validation but is rejected by the bridge, which freezes the whole capsule for 120 s and leaves it in `RECONCILE`. Low likelihood, capsule-wide blast radius, and it is the exact seam this review was asked to gate.

Both are small fixes. Everything else is BACKLOG and does not block.

---

## Gate results

| Gate item | Result | Evidence |
|---|---|---|
| B1: pre-existing closes from prior runs | PASS | Old `VERIFIED` + fresh `DUPLICATE_EVENT` for the same id returns rc 4 through the real CLI [MEASURED]. Offset is snapshotted before the intent is written (`intent_cli.py:728-736`, `:784-792`), ids are run-scoped (`prompts.py:25`, `:152`). |
| B1: torn ASCII tail line | PASS | Line split mid-record then completed 0.6 s later returns rc 0 once [MEASURED]; carry buffer at `intent_cli.py:648-652`. |
| B1: torn UTF-8 tail (multibyte cut) | FAIL, never a false success | Traceback, rc 1, in `wait-event`, `observe` and `enqueue-*` [MEASURED]. See K2. |
| B1: truncation / inode rotation | FAIL-SAFE, not handled | rc 124 in both cases, never a false success [MEASURED]. See K3. |
| B1: missing offset metadata | FAIL-OPEN | Falls back to offset 0 and returns rc 0 on an old `VERIFIED` [MEASURED]. See K1. |
| M1: true 8 GiB cgroup for `agy` | PASS | `build_scope_prefix` scope measured: `memory.max = 8589934592`, `memory.high = 6442450944`, `memory.swap.max = 0`, cgroup `app.slice/<unit>.scope` [MEASURED]. Runner execs the ELF `~/.local/bin/agy` directly, not the shim (`runner.py:26`, `:89-91`). Residual escape routes: K4. |
| M1: `-p` strictly last flag, model, stream output | PASS with naming deviation | 4/4 argv end with `-p`, `<brief>`; `--model gemini-4-hydrogen-high`; all six flags exist in `agy --help` [MEASURED]. Stdout goes to `run_<HHMMSS>_<role>.jsonl`, not `run_<HHMM>_<agent_id>.jsonl` (K6). |
| Byte-identical shared prefix, 4 agents | PASS | 5,832 bytes, sha256 `3da48d60bb56d961560bdcb69bec0ac18d98e87632309584a4d9596563c7005f`, identical at offset 0 for 4/4 roles and across two different run ids [MEASURED]. |
| Zero `vision` CLI calls by agents | PASS | No `subprocess`/`os.system` in `intent_cli.py`, `scripted_smoke.py`, `prompts.py`; `runner.py` spawns only `systemd-run`, `systemctl --user stop` and `agy`. `vision` appears only as prohibition text (`prompts.py:14`, `:21`). |
| Claim/lease handshake before script/camera intents | PASS on the happy path | Every brief starts with `observe`; both builders read the latest `claim` before building (`intent_cli.py:490-492`, `:543-545`); the smoke observes before each GUI enqueue (`scripted_smoke.py:561`). Not enforced as a hard gate: K8. |
| Camera numpad orbit key lists | PASS | `KP_1,KP_3,KP_7` and `KP_4,KP_4,KP_Add,Home` (`prompts.py:131`, `:135`; `scripted_smoke.py:518`, `:526`), all inside the broker allow-list (`broker.py:52`), `max_acts = len(keys)`, footprint `0,80,1580,900`. |
| Never auto-repeat `INPUT_UNCERTAIN` / `OUTCOME_UNCERTAIN` | PASS | rc 1 for both [MEASURED]; smoke raises on any rc other than 0/2 or parked-stale 3 (`scripted_smoke.py:613`, `:755`); brief says stop and report (`prompts.py:47`). A `wait-event` timeout raises and is not re-enqueued. |
| `STALE_FRAME` handling vs broker tail-requeue | PASS | drops 1,2 are waited through, drops 3 returns rc 3; drops 1 then `VERIFIED` returns rc 0 [MEASURED]; matches `broker.py:1176-1213`. |
| H2 to H1 intent JSON schema | PASS | All §5.1 fields emitted; broker checks (`agent_id`, `lease_epoch`, `lane`, `write_set`, `script`, `script_sha256`, `footprint`, `keys`, `based_on_frame`) are satisfied; `.offsets/` and dot-prefixed temp files are skipped by `broker.py:626-627`. |
| H2 to H1 `event_id` regex compatibility | FAIL | See L2 and K7. |
| Unit tests | PASS | 15/15 OK in 1.843 s [MEASURED], scoped `/tmp` copy. |

---

## Findings

### BLOCKER

None.

### LIVE_PATH_MAJOR

**L1 — Deliverable paths are relative and resolve against Blender's working directory (`~`), so the `.blend`, `.glb` and PNG renders land outside the repo.**
- Location: `swarm/agents/prompts.py:138` (brief gives the literal relative `filepath="swarm/run/output/starforge_sentinel.blend"` and relative `.glb` / PNG targets); `swarm/agents/scripted_smoke.py:833` (`Path(args.output_dir)` passed through unresolved) feeding `swarm/agents/scripted_smoke.py:328` and `:334` (the string is embedded verbatim as `out_dir` in the Blender script); `swarm/agents/README.md:31` (documented live command passes `--output-dir swarm/run/output`).
- Mechanism: the script runs inside Blender, where `os.makedirs`, `save_as_mainfile`, `export_scene.gltf` and `render.filepath` resolve a plain relative path against the Blender process working directory. The running swarm Blender (`blender --factory-startup --python .../swarm/broker/bridge.py`) has working directory `~` [MEASURED via `/proc/<pid>/cwd`]. The turn therefore closes `STATE_VERIFIED` while the artifacts are created under `~/swarm/run/output/`. The contract oracle (§8.1) cannot see this, the agent reports paths that do not exist in the repo, and any downstream proof step looking in `<repo>/swarm/run/output` finds nothing. The smoke is only correct when `--output-dir` is omitted (the default is absolute).
- Must-pass test, in prose: build the smoke plan with a relative output directory and assert that the output directory literal embedded in the camera deliverables script is an absolute path equal to that relative path resolved against the repository root (not against the test's current directory). Separately, assert that the rendered camera-agent brief contains no save, export or render target that is not an absolute path under the repository's `swarm/run/output` directory. As a negative control, the test must fail against the current tree for both assertions.

**L2 — `event_id` validation uses `match` with a `$` anchor, so a trailing newline is accepted by H2 and H1 but rejected by the bridge; one such script intent freezes the capsule.**
- Location: `swarm/agents/intent_cli.py:37` (pattern ends in `$`), `swarm/agents/intent_cli.py:260` (`EVENT_ID_RE.match`). Seam: `swarm/broker/broker.py:53`, `:243` (same pattern, same `match`); `swarm/broker/bridge.py:35`, `:144` (job filename pattern, where the id sits before `.py`).
- Mechanism: in Python `$` also matches just before a final newline. The id `abc` plus newline passes `match` in H2 and in the broker's `_is_valid_event_id`, and fails `fullmatch` [MEASURED]. The real CLI exits 0 for both `enqueue-gui` and `enqueue-script` and creates an intent file whose name contains a newline [MEASURED]. For the script lane the broker then writes `<nonce>__<seq>__<id with newline>.py` to the inbox; the bridge's filename pattern does not match it [MEASURED], so the bridge moves it to `rejected/` and never writes a receipt. The broker waits the full 120 s, closes `TURN_TIMEOUT`, sets `actuator_busy` and enters `RECONCILE` (`broker.py:967-984`), which stops every agent's queue (`broker.py:1408-1409`). Likelihood is low (a shell `$(...)` strips the newline; an agent helper that reads the id from a file without stripping does not), but a single occurrence halts all four agents until a director `reconcile`/`resume`.
- Must-pass test, in prose: for an id consisting of valid characters followed by a single newline, assert that intent validation raises a validation error, that both enqueue subcommands exit non-zero, and that no file is created under the agent's intents directory. Add a seam assertion that every id accepted by H2's validator also yields a job filename accepted by the bridge's job-filename pattern, exercised over at least: a 128-character id, an id with a trailing newline, an id with an embedded newline, and an id containing two consecutive dots (see K7).

### BACKLOG

**K1 — `wait-event` fails open to offset 0 when no enqueue-offset metadata is found.**
- Location: `swarm/agents/intent_cli.py:628-633` (fallback `start_offset = 0`), `swarm/agents/intent_cli.py:446-447` (an explicit `--agent` restricts the lookup to that one agent's `.offsets/`).
- Mechanism: with no metadata the tail starts at byte 0 of the persistent log, which is the original B1 behaviour. Reproduced two ways [MEASURED]: (a) an id never enqueued through the CLI with an old `VERIFIED` close in the log returns rc 0; (b) after a real enqueue, `wait-event --agent look-agent` for a camera id returns rc 0 from the old close. Run-scoped ids make this unlikely in a live run (it needs a reused run tag plus a wrong `--agent` or a failed enqueue), hence BACKLOG.
- Must-pass test, in prose: with a log holding a `VERIFIED` close for an id and no offset metadata for it and no explicit offset argument, `wait-event` must exit with a distinct non-zero code that is not 0, 2, 3, 4 or 124 and must not print that receipt. Repeat with metadata present only under a different agent than the one named by `--agent`.

**K2 — A log that ends inside a multibyte UTF-8 character crashes `wait-event`, `observe` and both enqueue commands with a traceback.**
- Location: `swarm/agents/intent_cli.py:643-646` (text-mode `open`, `seek`, `read`), `swarm/agents/intent_cli.py:127-128` (`read_events_records`), `swarm/agents/intent_cli.py:895-903` (`main` does not catch `UnicodeDecodeError`).
- Mechanism: the broker writes records with `ensure_ascii=False` (`broker.py:399`) and echoes the agent's `expect` text into `enqueue` records (`broker.py:734`), so non-ASCII bytes do occur. Reading to end-of-file in strict text mode raises when the last character is incomplete. All three commands exit 1 with a traceback [MEASURED]; rc 1 is the code the brief maps to hard refusals. It self-heals on the next call once the line is complete, and it never yields a false success. Realistic exposure is small: a reader must catch a single append mid-write and the cut must fall inside a multibyte character (ext4, 4096-byte pages [MEASURED]); no rate was measured.
- Must-pass test, in prose: write a close record whose `reason` contains a three-byte character, truncate the file one byte into that character, and start `wait-event` in a thread; complete the remaining bytes after a short delay; the call must return rc 0 with the full record and must not raise. With the same torn file and no completion, `observe` and `enqueue-gui` must succeed and simply ignore the incomplete tail.

**K3 — Truncation or replacement of `events.jsonl` after enqueue is not detected.**
- Location: `swarm/agents/intent_cli.py:641-642` (reads only when `file_size > offset`; no inode or shrink check).
- Mechanism: after an in-place truncate the size stays below the recorded offset, so a close appended afterwards is never read; after a rename-replace by a larger file, reading resumes at the old byte offset of the new file and skips anything before it. Both return rc 124 [MEASURED]. The brief's rule for 124 is to re-run `wait-event` on the same id (`prompts.py:40`), which reloads the same stale offset, so the agent loops until `--print-timeout`. The broker itself only appends (`broker.py:400`), so this needs an operator action mid-run.
- Must-pass test, in prose: enqueue against a log with several records, truncate the log to zero, append a `VERIFIED` close for the id, and assert `wait-event` returns rc 0 within its timeout. Repeat with the log atomically replaced by a new file that is larger than the recorded offset and contains the close near its start.

**K4 — The 8 GiB cap is not escape-proof for descendants, and the scope sits outside `newton-ia.slice`.**
- Location: `swarm/agents/runner.py:67-75` (`PATH` and D-Bus variables inherited unchanged; probe confirms `PATH` is unaltered), `swarm/agents/runner.py:52-64` (no `--slice`, no `--collect`).
- Mechanism: `agy` resolves first to `~/.local/bin/shims/agy` on `PATH` [MEASURED]; that shim re-execs under a new `systemd-run --user --scope --slice=newton-ia.slice` at 12G/16G. Any descendant of an agent that invokes `agy` by name (or any `systemd-run --user --scope`) leaves the 8 GiB scope. Whether `agy` ever re-invokes itself by name: AUSENTE — no inventar; no agy-under-agy parentage was visible in the process table at review time. Separately, the runner's scopes land in `app.slice` [MEASURED], so they are not counted against the operator's shared AI slice ceiling. Without `--collect`, a scope that ends in a failed state keeps its unit name and a rerun with the same `--run-tag` cannot start.
- Must-pass test, in prose: launch the runner's scope prefix with the agent environment around a stub that invokes `agy` by bare name through `PATH`, where the stub records its own cgroup path and `memory.max`; assert the nested process's `memory.max` is at most 8 GiB. Assert that the scope's cgroup path lies under the AI slice.

**K5 — `GOMEMLIMIT` gives no headroom under `MemoryHigh`, and an inherited value is kept.**
- Location: `swarm/agents/runner.py:27`, `swarm/agents/runner.py:73` (`setdefault`).
- Mechanism: the default `6GiB` equals `MemoryHigh=6G`, and when the runner is started from a shimmed session the inherited `8GiB` is preserved [MEASURED], which equals `MemoryMax`. The Go collector then does not tighten before the kernel starts throttling (with swap disabled) or the OOM killer fires. Effect on a live run was not measured.
- Must-pass test, in prose: for an empty base environment and for a base environment carrying `GOMEMLIMIT=8GiB`, assert the agent environment's `GOMEMLIMIT`, parsed to bytes, is strictly below the scope's `MemoryHigh`.

**K6 — Proof filenames use a six-digit tag, not the specified `run_<HHMM>_<agent_id>`.**
- Location: `swarm/agents/runner.py:308`, `swarm/agents/runner.py:318-321`. Spec text: `swarm/briefs/H2_agents.md:38-39`.
- Mechanism: stdout is streamed to `swarm/proof/run_<HHMMSS>_<role>.jsonl`. This looks like a deliberate response to the earlier collision nit, but it no longer matches the path named in the H2 brief and in this review's gate; any consumer keyed on four digits will miss the files. The tag still repeats daily.
- Must-pass test, in prose: a dry run with no explicit tag must report a `jsonl` proof path whose name matches whatever single naming rule the H2 brief states; the test and the brief must cite the same rule.

**K7 — H2 accepts ids containing two consecutive dots that the broker refuses without naming the event.**
- Location: `swarm/agents/intent_cli.py:37`, `:260`. Seam: `swarm/broker/broker.py:241`, `:657-668`.
- Mechanism: `a..b` passes H2 (CLI rc 0 [MEASURED]) but fails the broker's check; the broker archives the file and emits `CLAIM_REFUSED` with `event_id: null`. `wait-event` can never match it and returns 124 on every retry.
- Must-pass test, in prose: enqueueing an id containing two consecutive dots must exit non-zero locally with a validation error and create no intent file.

**K8 — The lease check is advisory: enqueue proceeds on a default or stale claim.**
- Location: `swarm/agents/intent_cli.py:167-171` (`lease_source = "default"`, epoch 1 when the log has no claim), `swarm/agents/intent_cli.py:151-156` (latest claim taken from the whole persistent log, including earlier broker runs), `swarm/agents/intent_cli.py:492`, `:545` (no check of `expires_ts_ms`).
- Mechanism: an agent that starts before the broker, or after it died, builds and writes intents against a lease it does not hold. It is harmless today because the broker always issues epoch 1 and renews on dispatch (`broker.py:415`, `:851`), but the result surfaces only after a full queue wait.
- Must-pass test, in prose: with an events log that contains no claim for the agent, `enqueue-script` must exit non-zero with a `NO_LEASE` error and write no intent; with a latest claim whose `expires_ts_ms` is in the past it must exit non-zero with `LEASE_EXPIRED`.

**K9 — `runner.py` leaves the agents' stdin inherited.**
- Location: `swarm/agents/runner.py:380-386`.
- Mechanism: four `agy` processes share the runner's stdin. `agy --help` documents stdin use only for `--input-format stream-json`; whether print mode reads stdin when it is a pipe is AUSENTE — no inventar. If it does, a runner started with an open pipe on stdin would hang the agents.
- Must-pass test, in prose: with process creation mocked, assert every agent process is created with stdin bound to the null device.

**K10 — Brief guidance for `DUPLICATE_EVENT` and for timeouts can mislead an agent.**
- Location: `swarm/agents/prompts.py:39`, `:40`; `swarm/agents/intent_cli.py:724-727` (local duplicate exits 1, while the brief documents duplicates as rc 4); `swarm/agents/runner.py:29`.
- Mechanism: (a) re-enqueueing an id whose first copy is still queued returns rc 4 immediately; `observe` shows no close yet, so "mint a new id only if the step did not run" leads to a second copy of a pending step. (b) The 124 rule has no bound; with K3 or K7 it loops until the 1800 s print timeout, which is itself only a little over three 540 s waits. (c) Whether `agy`'s shell tool permits a 540 s foreground command: AUSENTE — no inventar.
- Must-pass test, in prose: assert the shared prefix tells the agent, on `DUPLICATE_EVENT`, to first run `wait-event` on the original id, and gives a maximum number of consecutive 124 retries after which it must stop and report. Assert a local duplicate exits with the same code the brief documents for `DUPLICATE_EVENT`.

**K11 — Stale post-condition text.**
- Location: `swarm/agents/scripted_smoke.py:421` (`expect` says boots planted at `z=0.25`; the script and brief use `z = 1.05`). Cosmetic, but the text is logged in the authoritative `enqueue` record.
- Must-pass test, in prose: assert no smoke-plan `expect` string names a height that contradicts the pedestal top height stated in the shared prefix.

---

## Status of the prior review's items (`product/reviews/H2.md`)

| Item | Status in `H2_r1` | Basis |
|---|---|---|
| B1 false success from old receipts | Fixed on the main path; residuals K1, K2, K3 | probes above |
| M1 cap not reaching `agy` | Fixed; residuals K4, K5 | cgroup measurement |
| M2 undeclared cross-claim writes | Fixed in the smoke scripts as read (`Cube` hide gone, look-agent no longer touches meshes, `render:` or `scene.eevee`; camera declares `render:engine`, `render:eevee`). Modeler/props still create `mat:` datablocks by name under the shared-prefix rule at `prompts.py:33`; the contract ruling for that is not in the reviewed corpus (AUSENTE — no inventar) | `scripted_smoke.py:121-127`, `:219-252`, `:510` |
| M3 stale retry model | Fixed (waits through tail-requeue, GUI steps gated on builders) | `intent_cli.py:671-675`, `scripted_smoke.py:517`, `:525` |
| M4 timeout too short / conflated | Fixed (540 s, rc 124, brief rule) | `__init__.py:51`, `intent_cli.py:898-900` |
| M5 double TaskLight owner | Fixed (H4 sole owner) | `prompts.py:48-49` |
| M6 unsafe `--kill-all` | Fixed (start-time identity, unit stop, pid files removed) | `runner.py:226-247`, `:412-413` |

## Required to flip to APPROVED

1. Fix L1 (absolute deliverable paths in the camera brief, the smoke plan and the README command) with the L1 test.
2. Fix L2 (full-string `event_id` validation in H2; H1 and the bridge should match it) with the L2 test.
3. Re-run the unit suite in the 6G/8G scope. BACKLOG items K1..K11 do not gate.
