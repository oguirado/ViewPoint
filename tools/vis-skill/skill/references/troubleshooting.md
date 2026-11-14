# Diagnose before retrying

Every failure receipt carries `reason` and `action` (F7): read them first, in `/dev/shm/vqa-cu/vis/<job>/receipts.jsonl`.

| Result | Check next |
|---|---|
| 2 E_USAGE | Job name and arguments, mark changed since the observation that returned it, explicit reuse of a passed step ID |
| 3 E_EXPECT | Fresh look: did the input land, did the result paint where you said, is the region still animating, was the baseline evicted? With `exit`, the target was still alive at the end of the settle window (F3) |
| 4 E_RETRIES | Three attempts on one step ID. Investigate; do not rename the step to retry blindly |
| 5 E_RULE | Display ownership; actor lock (one actor per X server for started, adopted and attached jobs; `:N` and `:N.0` share it); a job made in another PID namespace; secret-shaped text, `--type-file` outside the job RAM dir (F9), image egress, `MV_HOSTED=1` (MetaViewer hosted mode refuses every act; actuation requires core admission, never unset `MV_HOSTED`); test-mode `start` whose cgroup has no finite `memory.max`. Fix the cause without bypassing it. |
| 6 E_BUDGET | Job files over 2 GiB (app caches `home/.cache` and `cache/` are not counted), scope memory or OOM, command time or output, or the 10 s wait for another act on the same job (F6). Reduce load; keep the caps |
| 7 E_ABSENT | Missing tool, desktop or Electron STATE, framebuffer. `cause:"session_runtime_absent"`: doctor or `start` found no usable `XDG_RUNTIME_DIR` or user manager, before any job dir; fix the `prerequisite` it names. Report it; no invented fallback |
| 8 E_TARGET | Target died, vanished or failed a loop probe. The death witness starts with `app.log` (F8). If you meant to close the app, use `--expect-change exit`; it still gives rc 8 when another owned process, such as Xvfb, exited with the client (F3) |
| 9 E_INTERNAL | Keep the receipts and report the error; no green claim |

## Retries

Attempts are counted per step ID within a job run (`attempts_scope:"per_step_id"`); there is no job-wide budget. Without `--step` each act gets a fresh `auto-<n>` ID (F4), kept even when that act is refused; refusals before the job lock (`MV_HOSTED`, read-only attach, lock wait, actor busy) record `step: null`. Re-observe and diagnose between attempts: a retry that changes nothing usually fails the same way.

## Common distinctions

A click can miss a tall or noisy mark; it uses the mark centre. Marks from the latest `look` or passing `act` are clickable (F5). A failing `act` invalidates marks: run `look` again before clicking. If the surface changed under the mark before input, vision refuses the stale mark.

A no-op may still paint focus or a pressed button: `any` is not a semantic success predicate. A frozen handler can leave a pressed frame on screen; only a loop probe (Tk send, advertised `_NET_WM_PING`, web CDP) shows responsiveness. Without a loop witness, acts proceed on PID, window and pixel checks; `loop_witness.before`/`after` in the response are true (obtained), false (unavailable) or null (incomplete).

`--settle-ms` bounds sampled waiting. For `none`, any sampled unmasked change fails, even one that reverts. Mask carets, clocks and spinners with `--mask X,Y,W,H`; never mask the feature under test. Finite samples do not prove continuous stillness. The act response and receipt carry `sampling`:

- `slow_factor`: worst ratio of real capture spacing to the planned tick (`spacing_ms`); expired ticks are skipped, not caught up.
- `settle_degraded:true` means `slow_factor` > 2: fewer samples than planned, so a `none` pass is weaker evidence; reduce host load and rerun.
- `samples[].budget_violation:true`: that frame's `decode_ms` exceeded the tick; it raises `slow_factor`, not the verdict.

`check --require-stable N` needs N consecutive matching observations. `--region-changed-since STEP` compares with that step's pre-action frame; an unknown step is rc 2, an evicted frame rc 3.

The secret filter works per text payload: split text and desktop key events bypass it, and X11 password fields are not detected. It guards against accidents, not a determined caller.

Tessdata defaults to `~/.local/share/tessdata`; OCR text is advisory, never an oracle. A missing framebuffer is rc 7; a failed read from a dying display is rc 8.

## Cleanup

Read back the result before the exit act and `stop`. `stop` verifies owned processes, archives text receipts to `~/.local/state/vis/receipts/<job>-<run_id>.jsonl` (`<job>.jsonl` points to the latest) and frees the RAM job. `stop --keep-frames` stops the processes but keeps `frame_*` and `marks_*` in RAM; `sweep` removes such jobs after 24 h (F8). `sweep` also collects jobs whose target died, even while the supervisor lives (F8); it skips jobs and kept frames from another PID namespace. If cleanup cannot prove the scope empty, it keeps the job and reports it; do not delete lock or socket files under a live owner.

## Other displays

`adopt --job J --display :N --xvfb-pid P` takes over an Xvfb you started yourself (a screen suffix such as `:N.0` is accepted); vision checks its PID, command line and start time, and `stop` leaves it running. `attach --job J --display :N --owner-ok TOKEN [--allow-act]` is an operator-managed flow, read-only unless `--allow-act`; do not read or obtain owner tokens yourself, and never probe real credentials to test isolation. Ownership comes from recorded PID and start time, never the display number or an inherited `DISPLAY`. Never restart another owner's browser or capsule.

Test mode: under `safe_run` (`NEWTON_TEST_PIDNS=1` in a private PID namespace) `start` runs the supervisor under a reaper with no systemd scope; `--mem` is not applied and the caller's cgroup `memory.max` caps the job. Its jobs are refused (rc 5) from any other PID namespace.

Private HOME, XDG, bus and TMPDIR stop singleton and config leakage into the real session; they do not confine files or network. Untrusted programs need an operator-provided capsule.

## Absent capabilities

Windows; macOS; Wayland; GPU or Vulkan under Xvfb; audio or video judgement; desktop and Electron STATE (AT-SPI), so `check --state` there is rc 7; Electron CDP outside a capsule; hosted image judges (`ask` is absent, rc 2; read frames with your own image reader; hosted egress needs the operator's consent); universal first-paint or responsiveness proof. `--untrusted` and `check --ask-file` were removed (F10). Report a gap instead of inventing a predicate.
