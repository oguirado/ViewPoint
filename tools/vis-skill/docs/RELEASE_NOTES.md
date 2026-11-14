# vision skill v1, closed by the operator (2026-11-02)

Skill name: vision (formerly vis). Install from the retained checkout with `VISION_INSTALL_OK=1 ./install.sh --apply`; preview with `./install.sh --dry-run` (no writes). See [README](../README.md) for compatibility names and installation guards.

Installation is performed separately by the operator. `./install.sh --dry-run` previews without writes. `VISION_INSTALL_OK=1 ./install.sh --apply` syncs the two vision dist packages and `~/.claude/skills/vision`, `~/.agents/skills/vision`. Legacy vis copies remain untouched and doctor reports them as warnings. See README for drift checks and temporary-HOME test requirements.

| Gate | Result [MEASURED] | Evidence |
|---|---|---|
| V0 loading | Done, by model self-report | docs/V0_LOADING.md |
| Unit, must-fail, real programs (`gates/run_gates.sh --all`) | rc 0, reported by the fix unit on this tree | docs/GATE_RESULTS_W.md, docs/VIS_FIX4_REPORT.md |
| False-green set by an independent author | 17/17 on the final tree; 0/17 against an always-ok fake | docs/evidence/false_green_round5.log |
| Stability under load | Tk 20, gedit 20, 3-parallel 20 batches: 0 failures, 0 owned leaks | docs/STABILITY_RESULTS.json |
| Cold agents | Codex and Claude, 2 seeded-bug tasks each, no hints: triggered 4/4, found the bug 4/4. Ran before the last fix units. | docs/evidence/cold/ |
| Independent check by AGENTIC-S1 | Passed on ac1970c: gedit, no-change click, Mines, MV_HOSTED, adopt on :98. Not rerun on the final tree. | Its report |

Known defect, unresolved: when a target exceeds its memory cap, removing the scope can erase the OOM evidence, so `vision` reports E_TARGET (rc 8) and not a budget failure. The cap itself works.

Not established:
- Cold agents: one run per task, on small programs, with the skill in a project directory, not the final install path.
- No test that the skill stays quiet when it should (false triggering).
- Large real programs, GPU or Vulkan under Xvfb, TUIs, Windows, macOS, mobile, Wayland.
- `vision ask` returns E_ABSENT with the flag: tracked as missing. Agents read frames directly.
- Attach and adopt: covered by must-fail and false-green cases, not on a live third-party display.
- Muse and oc-* workers: not tested loading the skill; use `skill/references/worker_block.md`. scr-obrero-claude workers do not load it.

History worth knowing: `docs/DECISIONS.md`. The writer's "clarifications" in the contract were judged by the gate's author; the orchestrator overruled the one that lowered the bar (attached acts).

## Lean fast path (campaign bc, unit bc-u-vis-core, 2026-11-07)

CLI changes on branch `bc/vis-core`. Not installed. SKILL.md and its references are owned by a separate docs unit and are not changed here.

| Item | Change |
|---|---|
| F1 | The credential refusal uses `is_secret()`: provider key shapes (`sk-`, `gh?_`, `github_pat_`, `AIza`, `xox?-`) anchored at a token boundary, with length bounds, and mixing upper case, lower case and digits; plus the PEM private-key header. Ordinary words such as `task-list`, `risk-free` or `ask-sol` are typed. Measured: 0/27 ordinary strings refused, 10/10 random key shapes refused [MEASURED]. |
| F2 | `--expect-change` defaults to `any`. Receipts record `expected: any`. |
| F3 | `--expect-change exit` (started desktop or Electron jobs): after the input, act polls the death witness inside `--settle-ms`, which defaults to 5000 ms for exit. A clean client exit (returncode 0, no OOM kill) passes rc 0 with `frame_sha_after: null` and `exit.returncode`. OOM gives rc 6, a target still running gives rc 3, any other exit gives the witness rc (8). Adopted, attached and web jobs get rc 2. |
| F4 | `--step` is optional. A missing id becomes `auto-N`. Attempts are counted per id. An explicitly reused passed id is still refused. |
| F5 | Marks returned by a passing act can be clicked by the next act without a `look`. The pre-input region-hash check still refuses a mark whose region changed. |
| F6 | An act waits up to 10 s for the job lifecycle lock, then fails rc 6. Overlapping acts on one job now run one after the other. The display actor lock still refuses at once. |
| F7 | Failure receipts carry `reason` (credential-shaped tokens replaced, at most 200 characters) and `action`. |
| F8 | The death witness records `exits` and `oom`. Its reason puts `app.log` first and includes `xvfb.log` only when Xvfb itself exited, without `pci id for fd` lines. `sweep` stops and removes a started job with a death witness even while its supervisor still runs. Started jobs keep the actor lock in the job directory. `stop --keep-frames` stops the processes, archives the receipts and keeps only `frame_*`/`marks_*` PNGs plus `kept.json` in the RAM job. `sweep` removes kept-frame jobs older than 24 h. |
| F9 | `--type-file P` types a regular, non-symlink file inside the job RAM directory: at most 64 KB, UTF-8, checked by `is_secret()`. Any other path is refused rc 5 from metadata alone, without opening the file. |
| F10 | `start --untrusted`, the `ask` verb and `check --ask-file` are gone. They now give rc 2 and create no job directory. The `MV_HOSTED=1` refusal is unchanged, and its reason now names the variable. |

Unchanged on purpose: real secrets are never typed (F1 plus web protected fields). An inherited or person-owned display is never driven. Frames stay on tmpfs. `MV_HOSTED=1` still refuses every act.

Limits:
- After `stop --keep-frames`, the job name stays taken until the kept directory is removed: by `sweep` after 24 h, or by deleting it by hand.
- An attached job on a started job's display takes the display-wide actor lock, not the started job's lock.
- `xdotool type` takes about 12 ms per character, so a file near 64 KB does not fit the 30 s command budget.
- Inside the safe_run PID namespace, gates that call `vision start` exit 77 (skipped) because systemd-run has no user bus there. `gates/must_fail/test_lean_adopted.py` covers F1-F10 on an adopted private Xvfb that the test starts itself.

## Advocate fixes for the lean fast path (campaign bc, unit bc-u-vis-core2, 2026-11-07)

Fixes on branch `bc/vis-core` for the A6 advocate objections. Not installed. This section supersedes the F1 and F8 actor-lock wording and the safe_run limit above.

| Item | Change |
|---|---|
| A6-1 (F1) | `is_secret()` judges the key body only, never the provider prefix. A body with fewer than 8 distinct characters is filler (`AIza000…`, `ghp_xxxx…`, `sk-ant-api03-AAAA…`) and is typed. With a provider prefix (`sk-proj-`, `sk-ant-…`, `gh?_`, `github_pat_`, `AIza`, `xox?-`) nothing more is needed, so lowercase hex keys are refused. Bare `sk-` keeps a word guard: with `-` or `_` it needs upper case, lower case and digits, otherwise letters and digits. Slack tokens need a digit. |
| A6-2 (F8) | Started, adopted and attached jobs on one display share one display-wide actor lock in the RAM root. The lock file is never unlinked. The job-directory `actor.lock` is gone. |
| A6-3 (F3) | `--expect-change exit` gives rc 8 when the death witness records any exit besides the client's, such as Xvfb, even if the client exited 0. OOM still gives rc 6. |
| A6-4 (F4) | An act without `--step` saves its `auto-N` id in the job as soon as the id is chosen. A refused act no longer hands its id to the next act. A refusal before the job lock is taken (`MV_HOSTED`, a read-only attached display, lock wait, actor busy) still records `step: null`, because no id was allocated. |
| A6-5 (F7, F9) | `--type-file` receipts record `type-file:NAME`, never the file content, on pass and on failure. |
| A6-6 | `gates/run_gates.sh` exits 3 (incomplete) when a required set is skipped (rc 77). A failure still gives rc 1. |
| A6-7 | Jobs and kept-frame records carry their PID namespace (`pidns`). Records without one were made on the host. A job from another PID namespace is refused (rc 5), and `sweep` skips it. Test mode: with `NEWTON_TEST_PIDNS=1` in a non-initial PID namespace (`safe_run`), `start` runs the supervisor under a small reaper child, with no systemd scope and no `--mem`. The caller's cgroup must have a finite `memory.max`, or `start` gives rc 5. Started-job gates now run inside `safe_run`. Checks of the systemd scope itself print `NOT COVERED IN TEST MODE`. |

Limits:
- Test mode does not cover the per-job `--mem` cap, OOM inside a 128M job scope, or the scope's `MemoryMax` and `MemorySwapMax`. Those need a host run.
- In test mode, web gates run Chrome with `--no-sandbox` through a gate-side `CHROME_BIN` shim, because the test runs as uid 0 in the user namespace. `vision` itself passes no such flag.
- `.actor-*` lock files stay in the RAM root, one per display used.


## VIS_PERF — display exclusion and bounded observation

- P0: local display screen suffixes share one stable X-server actor lock. Adopt/attach accept local screen suffixes; ownership verification still checks the Xvfb server. Regression covers both alias acquisition orders and an independent server. Gate logging can be redirected with VIS_GATE_REPORT; VIS_GATE_NO_INSTALL=1 skips installer rendering for restricted writers.
- P1: pixels() caches up to four frames by SHA-256 of file bytes. Acts retain decoded before pixels and reuse the final sample evidence, so cache eviction cannot trigger repeated before-frame decoding. Receipts include sampling.spacing_ms, before_decode_ms and per-sample decode_ms/budget_violation. Counter, content identity, eviction and over-budget regressions exercise the real decoder.
- P2: settle sampling uses monotonic fixed ticks and skips expired ticks. Timing evidence includes tick_ms, sample_ms, slow_factor and settle_degraded (factor greater than two); a slow capture cannot create a catch-up burst or extend the sample count without bound. Zero settle still obtains one after-frame.
- P3: production start checks XDG_RUNTIME_DIR and a reachable user manager before engine preparation or job creation. Missing runtime returns E_ABSENT with cause=session_runtime_absent and the exact prerequisite. Doctor includes session_runtime in missing. The explicit private-PID-namespace test mode reports scope_required=false and retains safe_run's inherited cap; production user scopes retain MemoryMax/MemorySwapMax.
- P4: job evidence accounting prunes home/.cache and cache/ before walking files. Sparse oversized downloads in those two trees no longer consume the frame budget; oversized frames, home/documents and data/cache still refuse with E_BUDGET. Production scope MemoryMax remains in force for all application memory and tmpfs caches.

## VIS_PERF advocate corrections

- Runtime CLI tests invoke only the read-only doctor. An independent fixture guard rejects start before spawning; production start preflight is exercised in process with engine access blocked and job paths redirected to a temporary directory.
- Adopt requires the Xvfb display as its leading operand; an option value cannot authorize a different server. The private adopted-display gate uses a screen suffix.
- Doctor reports missing session runtime as diagnostic data and completes skill drift checks; start retains its fail-closed preflight.
- Act receipts reuse the raw final frame hash returned by describe, including with masks and cache eviction; masked comparison hashes remain separate.
- Sampling degradation measures work plus lateness from the scheduled tick, excluding intentional waits across skipped ticks.
- Cache gates measure the exact decode delta after eviction and verify that a hit refreshes recency; FIFO and newest-entry eviction mutants are rejected.

## vision rename qualification

Rename regressions: 12/12 tests passed [MEASURED], including the temporary-HOME installer and exact-content doctor checks. Installer guard mutants: 4/4 killed [MEASURED] (authorization gate, inside-checkout refusal, orphan cleanup scope, legacy copies untouched). `quick_validate.py dist/codex/vision` prints `Skill is valid!` [MEASURED].

`VIS_GATE_NO_INSTALL=1 ./gates/run_gates.sh --all`: 14/14 gate groups passed, rc 0 [MEASURED], through campaign `g1/safe_run.sh` with a fresh temporary HOME. Systemd-scope and per-job memory checks remain explicitly uncovered in test mode. The initial run exposed a runtime test fixture that assumed skill drift returned success; its assertions and mocked subprocess boundary were corrected before the full passing rerun.

The unchanged independent false-green harness, invoked through `vision` under the same safety wrapper, passed 14/17 cases, rc 1 [MEASURED]. B, C and Q failed at web startup with E_TARGET [MEASURED]. This matches the operator-supplied root-mapping baseline (Chrome sandbox refusal) [CITED]; the reported non-root 17/17 result [CITED] was not rerun. No root mapping or sandbox policy was changed.

Router handoff: the installer-target citation `install.sh#L14-14` was fresh at the base and is now stale [MEASURED]. Route sources remain unchanged. The pinned worker block retains its vis commands and obsolete branch prerequisite; its bytes remain unchanged. The old core commit is already an ancestor [MEASURED].
