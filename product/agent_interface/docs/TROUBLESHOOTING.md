# Troubleshooting: typed codes and recorded failures

Read the receipt `code` first. `rc` is only the class. Each row says what happened and what to do next.
The second half lists failures that were recorded on real runs (13 and 14 November 2026); `vp docs ask`
returns the source line of each one with its freshness mark.

## Codes by exit class

| code | rc | What happened | What to do |
|---|---|---|---|
| `OK` | 0 | done, and verified where the verb verifies | follow `next` |
| `DRY_RUN` | 0 | nothing started; `data.argv` shows the upstream command | run again without `--dry-run` if it is what you want |
| `FAIL` | 1 | the wrapped tool ran and reported failure | read `data` and the spool; fix the cause before retrying |
| `AUDIT_VIOLATIONS` | 1 | `rewind audit`, `projection reconcile`, `queue validate` or `docs check` found violations | read `data.violations`; for `docs check` run `vp docs build` |
| `CHECK_FALSE` | 1 | `vision check`: the asserted state is not on screen | `vp vision look`, then decide; this may be a real bug |
| `USAGE` | 2 | bad arguments; nothing was started | `vp sys help <noun> <verb>` |
| `FORBIDDEN_DISPLAY` | 3 | display `:1` or the operator display | use a private display; `--allow-display` only on explicit instruction |
| `FORBIDDEN_PATH` | 3 | a path under `/dev/shm/vqa-cu/hybrid/` | use your own runtime root; `--allow-hybrid` only on explicit instruction |
| `FOREIGN_JOB` | 3 | the job is not in this session's ledger | `vp sys jobs`; `--foreign-ok <job>` only on explicit instruction |
| `HOLD` | 3 | `capsule show` is on hold in the launcher | use `vp capsule view` |
| `SECRET_PATH` | 3 | the argument points at a credential, token, key or auth file | do not read it; ask the operator |
| `URL_REFUSED` | 3 | non-https URL, query or fragment, or a domain outside the identity | navigate to the plain https page, then use `vp browser task` |
| `CONFIRM_REQUIRED` | 4 | destructive verb without `--confirm <id>`, or a wrong id | check it is intended, then pass `data.confirm_id` |
| `NOT_FOUND` | 5 | job, runtime root, file, point or entry point absent | check the name with `vp sys jobs` or `vp rewind status` |
| `AUSENTE` | 5 | the documentation corpus has no answer | do not invent; other words, `vp docs locate`, or ask |
| `NO_TAKE` | 5 | no ViewPoint take is running | `vp capsule start --dry-run`, then with `--confirm take` |
| `BUSY` | 6 | a lock is held | wait, then run the command in `next` |
| `ACT_BUSY` | 6 | a concurrent `vision act` held the job for 10 s | wait, `vp vision look`, retry once |
| `QUEUE_FULL` | 6 | the agent already has 8 pending intents | `vp queue observe` and wait for closes |
| `STALE_FRAME` | 7 | the intent or marks were based on an old frame; nothing was sent | observe again (`vp vision look`, `vp queue observe`), retry once |
| `INPUT_UNCERTAIN` | 7 | the input may or may not have landed | observe first; never repeat blindly; see below |
| `FROZEN` | 7 | the broker was stopped by control or watchdog | `vp queue control --op resume`, or `vp rewind restore --to last_good --resume` |
| `RECONCILE` | 7 | the broker waits for a human or director decision | look at the program, `vp queue control --op reconcile`, then `--op resume` |
| `NO_VISION` | 7 | the broker cannot reach the vision job | `vp vision look --job <job>`; restart the job if it died |
| `DEPENDENCY` | 8 | a required program or worktree is missing | `vp sys doctor`; `data.missing` names it |
| `UPSTREAM` | 9 | the wrapped tool failed in an unclassified way | read `data.upstream_rc` and the spool |
| `TIMEOUT` | 124 | the deadline passed | the receipt says whether the child was stopped; observe before retrying |

## Recorded failures

### STALE_FRAME

- Symptom: `vp browser navigate` or a queue GUI intent is refused with `STALE_FRAME`.
- Cause 1: you acted too soon after the capsules were switched back to agents. The display is repainted during the restore, so the frame changes between observe and propose.
- Cause 2: host load. With load1 near 32 a capture takes about 4 s and a changing page gives a new frame on every capture; the built-in 3 tries are not enough.
- Do: `STALE_FRAME` without `INPUT_UNCERTAIN` means nothing was sent, so repeat the same step once after 2 to 3 s. Before a take wait 1 minute after `vp capsule start` and at least 70 s after a switch to agents. Keep load1 at about 18 or below: pause other swarms and downloads.
- In a rewindable capsule, every queued GUI intent closes `STALE_FRAME` after a restore: observe and enqueue again.

### INPUT_UNCERTAIN

- Symptom: `INPUT_UNCERTAIN` after a proposal was admitted (wait-verify timed out, upstream rc 2 or 5).
- Cause: the verification needs a new frame after the input; under load the capture is too slow and the wait times out. The input may already be in the page.
- Do: never repeat the input. Run `vp browser read` (or `vp vision look`). If the page already shows the target, count the step as done; otherwise navigate again once. A blind retry appends text to the address bar or the search box.
- In the queue, `INPUT_UNCERTAIN` moves the broker to `RECONCILE`: decide, `vp queue control --op reconcile`, then `--op resume`.
- A second search without a reload fails with a text mismatch because the search box keeps the old query: start every search with a navigation.

### rc 3 means different things

- `vp` rc 3 is always a safety refusal (`FORBIDDEN_DISPLAY`, `FORBIDDEN_PATH`, `FOREIGN_JOB`, `HOLD`, `SECRET_PATH`, `URL_REFUSED`): nothing happened.
- Upstream vision rc 3 means the expected visible change was missed. Look again; do not repeat the same action blindly.
- Upstream broker rc 3 means `RECONCILE` or `FROZEN`; `vp` reports it as rc 7 with that code.
- Upstream `wait-event` used rc 3 for a parked `STALE_FRAME`, and a review found a version that also returned 3 for `NO_VISIBLE_CHANGE`, an act that was executed. This is why you decide on `code`: re-enqueueing on a bare rc 3 can repeat an executed act.
- A hover with `--expect-change none` returned vision rc 3 when a highlight changed pixels, and the broker froze the queue. If the queue freezes right after a hover, reconcile and resume.
- `vp_demo_take.sh show` exits 3 because it is on hold: code `HOLD`.

### Pack refused for volatile tokens

- Symptom: a context pack or a question to a liaison is refused with `pack_volatile_or_marker` (upstream `E_RULE`, rc 5).
- Cause: the text held a process id or a clock value. Packs must be byte-stable so the cached prompt prefix stays valid.
- Do: remove PIDs, start times and HH:MM:SS stamps from the text; name units, commits and file paths. Put volatile details in an evidence file. If the token is legitimate content, the swarm skill accepts `--allow-regex '<pattern>'`. `vp docs pack` output is byte-stable by construction.

### agy -p order

- Symptom: `agy` exits 2 and treats `--model` as the prompt.
- Cause: flag order.
- Do: `-p "<prompt>"` must be the last argument. `vp agents solo` and `vp agents run` wrap scripts that already do this; keep the order if you write a command by hand.

### claude --verbose

- Symptom: `claude -p --output-format stream-json` answers that it requires `--verbose`.
- Do: add `--verbose` in print mode with stream-json output. Do not add `--bare`. Smoke test before a writer lane: the last line of `echo 'Reply OK' | claude -p --verbose --model <model> --output-format stream-json | tail -1` must hold `"result":"OK"`.

### pkill self-kill

- Symptom: your own shell dies (exit 144) when you stop a process.
- Cause: `pkill -f '<pattern>'` (and `pgrep -f`) matched the shell's own command line, which contains the pattern.
- Do: stop by PID only. Use `vp record stop --pid`, `vp agents stop`, `vp vision stop`; they use the PID recorded at start. Record the PID of anything you start by hand.

### OBS stale scene

- Symptom: the OBS recording is black, then OBS closes.
- Cause: the saved scene captured a window that no longer exists.
- Do: record the private display with `vp record start` (ffmpeg). If OBS is required, make a new scene with a screen capture cropped to the viewer on a 1920x1080 canvas.

### Other recorded traps

- `vp capsule start` stops a running take before it starts: check `vp capsule status` first.
- Stop a recorder with SIGINT (what `vp record stop` sends). SIGKILL leaves the mp4 unplayable.
- `/dev/shm` is erased on reboot: copy deliverables out when the run ends.
- A worker's exit code 0 is not success: read its receipt.
- The projection tool defaults to test rooms: pass the rooms explicitly.
