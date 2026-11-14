# Review P4b_H2_r2 — Fable re-review of `H2_r2` (`swarm/agents/`)

- **Reviewer model**: `claude-fable-5-2` (Track P, reviewer unit P4b, findings only)
- **Timestamp (UTC)**: 2026-11-14T12:07:39Z (probe start)
- **Reviewed tree**: `~/Documentos/viewpoint-hackathon` (branch `swarm-1114`, `swarm/agents/` untracked), read-only
- **Write surface**: this file only (`product/reviews/P4b_H2_r2.md` in worktree `vp-wt-P4b`, branch `p-P4b`). Nothing under `swarm/` or in the main checkout was created, edited or deleted by this review. The reviewed sources hashed identically before and after the probes.
- **Probe sandbox**: copy of `swarm/agents/`, `swarm/broker/` and `swarm/CONTRACT.md` in `/tmp/vp_p4b_r2_probe`, probe scripts in `/tmp/vp_p4b_r2_scripts`, every run inside `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0` with `PYTHONDONTWRITEBYTECODE=1`, working directory `/tmp`, all frames / intents / events paths pointed at temporary directories. No `agy` launch, no `vision` call, no access to `:1`, `:2`, `vpswarm`, `/dev/shm/vqa-cu/` or `playbooks/`.
- **Prior review**: `product/reviews/P4b_H2.md` (REJECTED on L1 + L2; K1..K11 BACKLOG).

## Files reviewed

| File | SHA-256 | vs `H2_r1` |
|---|---|---|
| `swarm/agents/__init__.py` | `adbdc812b1385cf07f670dbdaf57be82fc47e21a13edb777a977d71c1c5e434f` | unchanged |
| `swarm/agents/intent_cli.py` | `d00aadf6f5b0953f82d5dfe8ef25fb4c09212ef736a34ab14e6ed6d56b186f5e` | changed |
| `swarm/agents/prompts.py` | `c8a241c95d759cc2718f9c787b82ada52af8274304ebf33586aa5bb4cc7bf095` | changed |
| `swarm/agents/README.md` | `09150e021aefaa2c55a28ccf1ba8ed739d44a92dbeab1d1c530978b5abb93c94` | changed |
| `swarm/agents/runner.py` | `1bfee6433fb43279bf41df14bfa39efea47cbc1721db67b57ea081097d942b6e` | unchanged |
| `swarm/agents/scripted_smoke.py` | `7bb612c915a2441e758e33d4fe99db32a4c5b47b1174a30deba85c1f2d111deb` | changed |
| `swarm/agents/test_agents.py` | `b9c6110589616c674a7ff2be99d96e9a1074a4f7ea0efe0d18057202c1e0eeb4` | changed |

Cross-reference only (seam verification, not under review; both unchanged since the prior review):

| File | SHA-256 |
|---|---|
| `swarm/broker/broker.py` | `b58c741142f6d6740fd0a650de9f9c90dc7bc0553e934342cf7b74dc7e36dbe5` |
| `swarm/broker/bridge.py` | `5c1d67c6a01240275e7ee27ad8418ca93bdd90b190e8fd904538319185ae9cb1` |
| `swarm/CONTRACT.md` | `b392f7dd16a3689b4af34e66a25a990dc131eb07d4f34fc7c47312069eec8fa4` |

Not done: `test_agents.py` was run, not read line by line. The Blender payloads were compiled by the dry run but not executed in Blender, and no live broker run was made, so the on-disk location of real deliverables is inferred from the embedded absolute path, not observed. No line-level diff against `H2_r1` was possible (the earlier probe copy no longer exists); regressions were checked by re-measuring behaviour, not by diff.

## VERDICT: APPROVED

No BLOCKER. L1 and L2 are closed, K2 and K7 are closed, the unit suite passes 19/19, and no regression was measured on the previously passing gate items. Remaining items are BACKLOG and do not gate.

---

## Gate results

| # | Gate item | Result | Evidence |
|---|---|---|---|
| 1 | L1: camera brief uses absolute deliverable paths | PASS | `prompts.py:138` names the output directory and all five targets (`starforge_sentinel.blend`, `starforge_sentinel.glb`, `front.png`, `turntable.png`, `hero.png`) under `~/Documentos/viewpoint-hackathon/swarm/run/output/` [MEASURED: all five absolute strings present in the rendered camera brief]. Across the 4 rendered briefs, 0 mentions of `swarm/run/output` lack the absolute repo prefix [MEASURED]. The only non-path extension tokens are the bare words `.blend` / `.glb` and the `frame_<N>.png` frame-name placeholder. |
| 1 | L1: partial-role builder settle wording | PASS | `prompts.py:128` tells the camera agent, in partial-role runs, to wait only for builder agents present in `events.jsonl` [MEASURED: wording present in rendered brief]. |
| 1 | L1: `scripted_smoke.py` resolves relative `output_dir` against `REPO_ROOT` from `/tmp` | PASS | With working directory `/tmp`, `swarm/run/output` and `./swarm/run/output` both resolve to `~/Documentos/viewpoint-hackathon/swarm/run/output`; the `out_dir` literal embedded in the camera deliverables script is that absolute path for both the 4-agent and the 2-agent plan [MEASURED]. Real CLI `--dry-run --output-dir swarm/run/output` from `/tmp`: rc 0, 13 intents validated, no `/tmp/swarm` created [MEASURED]. Resolution is applied at three layers (`scripted_smoke.py:337`, `:391`, `:644`, `:844`). README command is now absolute (`README.md:31`). |
| 2 | L2: `\A...\Z` + `.fullmatch()` | PASS | `intent_cli.py:37`, `:61`; same treatment for `agent_id` (`:38`, `:74`). |
| 2 | L2/K7: `"abc\n"`, `"abc\nxyz"`, `"a..b"` rejected, zero intent files | PASS | Real CLI, each id: `enqueue-gui` rc 1, `enqueue-script` rc 1, `wait-event` rc 1, 0 files under the intents directory, `VALIDATION_ERROR` JSON on stderr, no traceback [MEASURED]. Same result for 11 further malformed ids (leading newline, trailing CR, double trailing newline, trailing space, tab, slash, 129 characters, empty, leading dot, trailing double dot, non-ASCII) [MEASURED]. Positive controls (`abc`, 128 characters, `a.b-c_d`) rc 0 [MEASURED]. |
| 2 | L2: agreement with `bridge.py` job-filename regex | PASS | 200,000 random strings over an alphabet including newline, CR, tab, space, slash, backslash, NUL, non-ASCII and dots: 16,904 accepted by H2's validator; of those, 0 rejected by the bridge job-filename pattern and 0 rejected by the broker's id check [MEASURED]. |
| 3 | K2: torn multibyte UTF-8 | PASS | Log read in binary and framed on newline before decoding (`intent_cli.py:157-161`, `:678-687`). Close record cut at each of its 8 mid-character byte positions, completed 80 ms later: `wait_for_close_event` returned rc 0 with the exact non-ASCII `reason` in 8/8 cases, no exception [MEASURED]. Real CLI on a torn, uncompleted log: `observe` rc 0, `enqueue-gui` rc 0, `enqueue-script` rc 0, `wait-event` rc 124, no traceback [MEASURED]. Real CLI `wait-event` with completion after 0.6 s: rc 0 [MEASURED]. Start offset landing inside a multibyte character: rc 0 [MEASURED]. Complete line of invalid UTF-8 followed by a valid close: rc 0 [MEASURED]. |
| 4 | Unit suite | PASS | 19/19 OK in 2.010 s [MEASURED], scoped `/tmp` copy. Tests named for L1 (2), L2/K7 (1) and K2 (1) exist. |

### Regression checks (items that passed in the prior review)

| Item | Result | Evidence |
|---|---|---|
| B1: old `VERIFIED` + fresh `DUPLICATE_EVENT` | PASS | rc 4 with and without `--agent` [MEASURED]. Offset snapshot still precedes the intent write (`intent_cli.py:763`, `:821`). |
| Never auto-repeat uncertain verdicts; exit-code map | PASS | `INPUT_UNCERTAIN` 1, `OUTCOME_UNCERTAIN` 1, `NO_VISIBLE_CHANGE` 2, `QUEUE_FULL` 4, `TURN_TIMEOUT` 1 [MEASURED]. |
| `STALE_FRAME` handling | PASS | drops 1, 2 then `VERIFIED` rc 0; drops 3 rc 3 [MEASURED]. |
| Unterminated final record is not consumed | PASS | rc 124 [MEASURED]. |
| Byte-identical shared prefix | PASS | 5,857 bytes common to 8 briefs (4 roles × 2 run ids), sha256 `1d1bfc061e4aa89a02f215af4b675e4cb3a1087c5182d5663248a344d531597b` [MEASURED]. Size and hash differ from `H2_r1` (5,832 bytes) because the prefix text was edited; identity across roles and run ids holds. |
| Camera numpad key lists | PASS | `KP_1,KP_3,KP_7` and `KP_4,KP_4,KP_Add,Home` in the brief and in the smoke plan [MEASURED]. |
| Runner (`-p` last, 8 GiB scope, kill safety) | Not re-measured | `runner.py` and `__init__.py` are byte-identical to `H2_r1`; the runner unit tests pass. |

---

## Findings

### BLOCKER

None.

### LIVE_PATH_MAJOR

None. L1 and L2 from `P4b_H2.md` are closed.

### BACKLOG

Carried from `P4b_H2.md`:

| Id | Status in `H2_r2` | Basis |
|---|---|---|
| K1 `wait-event` falls back to offset 0 without metadata | Open | Fallback still present at `intent_cli.py:669`; not re-probed. |
| K2 torn multibyte crash | Closed | Gate item 3. |
| K3 truncation / replacement of the log not detected | Open, not re-probed | Read still gated only on size exceeding the offset (`intent_cli.py:677`). |
| K4 cap escape for descendants, scope outside the AI slice | Open | `runner.py` unchanged. |
| K5 `GOMEMLIMIT` headroom | Open | `runner.py` unchanged. |
| K6 six-digit proof tag vs brief naming | Open | `runner.py` unchanged; a unit test now asserts the six-digit form. |
| K7 double-dot ids | Closed | Gate item 2. |
| K8 advisory lease check | Open, not re-probed | — |
| K9 inherited stdin | Open | `runner.py` unchanged. |
| K10 `DUPLICATE_EVENT` / unbounded 124 guidance | Not re-assessed | — |
| K11 stale `z=0.25` post-condition text | Open | One smoke-plan `expect` string still names it [MEASURED]. |

New in this pass:

**N1 — The broker's own id check still accepts a trailing newline (H1 side, outside this unit).**
- Location: `swarm/broker/broker.py:53`, `:243` (unchanged file).
- Mechanism: in the seam fuzz, 970 of 200,000 strings were accepted by the broker's check and rejected by H2 [MEASURED]. Every path through H2's CLI is now closed, so this no longer affects H2-produced intents. An intent file written into the intents directory by anything other than `intent_cli.py` can still reach the bridge with such an id and trigger the capsule freeze described in L2.
- Must-pass test, in prose: feed the broker an intent whose id ends in a newline and assert it is refused at intake with a named refusal, and that no job file is written to the inbox.

**N2 — A relative `--output-dir` containing parent references resolves outside the repository.**
- Location: `swarm/agents/scripted_smoke.py:328-333`.
- Mechanism: `../escape` resolves to `~/Documentos/escape` [MEASURED]; absolute paths anywhere on disk are passed through. Operator-supplied argument only, not reachable from an agent brief.
- Must-pass test, in prose: assert that a relative output directory whose resolved path is not inside the repository root is rejected with a non-zero exit before any plan is built; state in the README whether absolute paths outside the repository are intended to be allowed.

**N3 — Undecodable bytes inside a complete line are replaced silently.**
- Location: `swarm/agents/intent_cli.py:161`, `:687`.
- Mechanism: a complete line with invalid UTF-8 is decoded with replacement characters; if it still parses as JSON, the record is used with altered text. `event_id` matching is unaffected for valid ids (ASCII only). No false success was produced in the probes.
- Must-pass test, in prose: write a complete close record whose `reason` holds an invalid byte sequence and assert the returned record either carries a marker that its text was repaired or is skipped, per whichever rule the contract states.

---

## Status of the prior review's required items

| Required item (`P4b_H2.md`) | Status |
|---|---|
| 1. Fix L1 with its test | Done; verified independently from `/tmp` |
| 2. Fix L2 in H2 with its test; H1 and bridge to match | Done in H2; bridge already strict; broker still loose (N1, H1 scope) |
| 3. Re-run unit suite in the 6G/8G scope | Done, 19/19 |
