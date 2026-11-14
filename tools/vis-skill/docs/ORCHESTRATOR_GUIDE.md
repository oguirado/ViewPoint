# vision: how orchestrators use it (v2)

Skill name: vision (formerly vis). Install from the retained checkout with `VISION_INSTALL_OK=1 ./install.sh --apply`; preview with `./install.sh --dry-run` (no writes). See [README](../README.md) for compatibility names and installation guards.

`vision` lets an AI agent run a program, see it, act on it, and prove what happened. Use it for any software whose acceptance depends on a rendered interface: web pages, Linux desktop apps, Electron. Pair it with your own tests; it does not replace them.

Executable: `python3 -I tools/vis-skill/vision`
Full reference: `tools/vis-skill/dist/claude/vision/SKILL.md`, and `references/` next to it.

The core implements F1–F10; the former branch prerequisite is already in history. Optional `--step` establishes only that capability: it cannot detect `--keep-frames`, target-dead sweep, or `--type-file`. Do not choose cleanup or input syntax with that probe. vision has no version number. Older builds require separate verification; these instructions do not provide a compatibility fallback.

## Use it when

| Situation | Use `vision`? |
|---|---|
| "Does this screen, button or flow work for a user?" | Yes |
| A bug you can only see on screen, or a data change that may not be painted | Yes |
| API, service or CLI logic with no interface | No: use tests |
| A unit test or static check can decide it | No |
| Godot | No: `godot-visual-qa` |
| Windows, macOS, mobile, Wayland | Not possible. Say so; do not invent a route |

## Fast path

```sh
V="python3 -I tools/vis-skill/vision"
$V doctor                                              # once; --target-repo DIR for a project with deps.lock
$V start --job editor --app 'gedit --standalone' --wait-title gedit
$V look  --job editor                                  # open marks_png
$V act   --job editor --click 3                        # expects any change; returned marks are clickable next
$V act   --job editor --type 'Example' --expect-change 0,50,800,500
$V look  --job editor                                  # read back the meaning before closing
$V act   --job editor --key ctrl+q --expect-change exit   # a clean exit is a pass
$V stop  --job editor                                  # prints the receipt archive path; cite it
```

Web: `start --web 'file:///abs/page.html'`, then `act --ax NODE_ID --op click` and read back with `check --state 'button=Done'` (exact role and name, web only).

## Defaults after the code unit

| Behaviour | Before | Now |
|---|---|---|
| `--expect-change` | mandatory | defaults to `any`; also `none`, `X,Y,W,H`, `exit` (F2, F3) |
| Closing the app on purpose | rc 8 E_TARGET (70 % of observed errors) | `--expect-change exit`: rc 0 on a clean exit, rc 3 if still alive, rc 6 on OOM, rc 8 if another owned process such as Xvfb exited too (F3) |
| `--step` | mandatory | optional; receipts get `auto-<n>`, kept even when that act is refused; three attempts per ID still counted (F4) |
| Marks returned by `act` | expired | clickable in the next `act`; the pre-input region check stays (F5) |
| Two acts on one job | second one rc 5 | second one waits up to 10 s, then rc 6 (F6) |
| Failure receipts | no reason, no action | `reason` and `action` recorded (F7) |
| `stop` | always deletes frames | `--keep-frames` keeps `frame_*` and `marks_*` in RAM; sweep removes them after 24 h (F8) |
| `sweep` | skipped jobs whose target died | collects them; skips jobs from another PID namespace (F8) |
| Actor lock | started jobs locked their job dir, others the display | one per X server for started, adopted and attached jobs; `:N` and `:N.0` share it |
| `--type-file` | always refused | allowed for a regular file in the job RAM dir, at most 64 KB; receipts record `type-file:NAME`, never the content (F9) |
| `--untrusted`, `ask`, `check --ask-file` | always refused or rc 7 | removed; unknown flag is rc 2 (F10) |
| Job file budget | every job file counted | app caches `home/.cache` and `cache/` not counted |
| No user session runtime | `start` failed late (`Failed to connect to bus`) | doctor and `start` give rc 7 `cause:"session_runtime_absent"` with the `prerequisite`, before any job dir |
| Settle sampling | sleep then capture | fixed ticks; `sampling.slow_factor`, `settle_degraded` (>2) and per-sample `budget_violation` in act receipts |

## Exit codes

| rc | Meaning | What to do |
|---|---|---|
| 0 | That operation's condition passed | Still verify the meaning |
| 2 | Bad arguments, unknown job, reused passed step | Fix the call |
| 3 | The expected change did not happen | **Possible real bug.** Look and diagnose |
| 4 | 3 attempts on this step used | Investigate |
| 5 | A safety rule refused | Do not bypass it |
| 6 | RAM, time or lock-wait budget | Reduce the load |
| 7 | Capability not available here | Report it; no invented fallback |
| 8 | Target died, vanished or froze | Possible real bug. Inspect, stop |
| 9 | Internal error | Keep receipts; no green claim |

## Rules

- Never read credentials, tokens, keys or auth files; never type real secrets. On-screen labels such as token or password are ordinary UI to look at and click.
- Never drive an inherited or person-owned display. For an Xvfb you started: `vision adopt --job J --display :N --xvfb-pid P`.
- Frames stay on tmpfs (`/dev/shm/vqa-cu/vis/<job>/`), never on disk. Agents read them with their own image reader. Hosted image egress needs the operator's consent; vision has no hosted judge (`ask` is absent, F10).
- An act pass proves only its declared visible change. Acceptance needs a read-back before the exit act and `stop`: a `check` pass receipt or the inspected frame cited by `frame_sha256`. `look` and `stop` receipts, app exit 0, stdout "OK", OCR or a model's opinion are not proof. Use `stop --keep-frames` (F8) when frames are still needed after stop; sweep removes them after 24 h.
- Trusted programs only: Xvfb isolates the display, not files or network. Untrusted programs need a capsule.
- `MV_HOSTED=1` refuses every act; MetaViewer actuation goes through core admission.

## Workers

| Worker | Sees the skill? | Do this |
|---|---|---|
| Codex or Sol (`codex exec`, `scr-obrero-sol`) | Yes, once installed [MEASURED] | Nothing |
| Claude (`claude -p`) | Yes, once installed [MEASURED] | Nothing |
| `scr-obrero-claude` | No [MEASURED] | Paste the worker block |
| Muse, `oc-*` | Not tested | Paste the worker block |

Worker block: `tools/vis-skill/dist/claude/vision/references/worker_block.md`. It is byte-stable (install-time path only, no dates), so it can sit in the frozen prefix of every seat. It requires the worker to cite the receipt path printed by `stop` in its final output; the swarm receipt gate can check that path. Workers need shell access: not `--sin-herramientas`.

Text-only workers cannot judge pixels. They decide on STATE, region and title predicates and on `marks` OCR text and `changed_regions` in the JSON; pixel judgement goes to an image-capable seat. Image reading by Muse and `oc-*` is untested: AUSENTE.

## Swarm admission

Before launching N vision jobs on one host, run `vision doctor` once and read:

- `missing.desktop`, `missing.web`, `missing.electron`: the surface the wave needs must be empty. Doctor exits rc 7 when the desktop core is missing.
- `missing.session_runtime` must be empty unless `session_runtime.scope_required` is false (test mode); otherwise doctor exits rc 7 `session_runtime_absent` naming the `prerequisite` (`XDG_RUNTIME_DIR`, user manager).
- `ram_available`: free bytes on `/dev/shm`. Each job may hold up to 2 GiB of job files [CITED, `vision` `budget()`], including the private HOME on tmpfs; the app caches `home/.cache` and `cache/` are not counted there and only the scope `--mem` cap (default 4G) bounds them. Admit `N <= ram_available / (2 GiB + --mem)` with margin, and size `--mem` from host free RAM.

One job per worker, each with its own private display. Acts on one job are serialised (F6); one actor per X server (`:N` and `:N.0` share it), and a second actor is refused at once (rc 5). Workers report `settle_degraded:true` acts as weak `none` evidence (host overloaded). Have workers `stop` every job, and run `vision sweep` after the wave to collect jobs whose target died (F8).

Under `safe_run` (`NEWTON_TEST_PIDNS=1`) `start` uses test mode: no systemd scope, `--mem` not applied, the caller's cgroup `memory.max` is the cap (rc 5 if it is not finite). Those jobs are refused from other PID namespaces (rc 5), and `sweep` skips them.

## swarmSSH (remote hosts)

- **Engines from the local forge.** `vision` fetches `wender-agent` and `vision-engine` with `git archive` at a pinned SHA. On a remote host set `VIS_WENDER_REPO` and `VIS_VE_REPO` to forge clones that contain the same pinned SHAs; the default paths under `~/Documentos` do not exist there.
- **Doctor is the capability manifest.** Run `vision doctor` on the remote host before admission; its `missing` map, `tools`, `engines` and `ram_available` describe what that machine can do. Tessdata (`TESSDATA_PREFIX`, default `~/.local/share/tessdata`) and Chrome (`/usr/bin/google-chrome`) are host paths too. Remote architecture and installed tools: AUSENTE until doctor runs there.
- **Frames never leave the remote tmpfs.** Only text receipts and `frame_sha256` values come back through the forge. Moving pixels across hosts needs an explicit operator decision.
- One vision job per remote worker, `--mem` sized from the remote doctor.

## Build programs that are cheap to verify

Stable id, role and accessible name on each control. A small test hook or state dump. Closed verbs. A deterministic mode with no animation. A Quit control or key so `--expect-change exit` can close the app cleanly. `references/make_it_visible.md` has more.

## Known limits

- Measured on small and medium programs: gedit, gnome-mines, small Electron and web pages. Run a pilot on your real program first.
- Not tested: terminal interfaces, GPU or Vulkan under Xvfb, false triggering of the skill.
- Desktop and Electron have no STATE: `check --state` returns rc 7 there.
- No hosted image judge: read frames with your own image reader.
- Known defect: a target exceeding its memory cap may be reported as rc 8, not rc 6. The cap itself works.

## Evidence

Independent false-green set 17/17; 0 failures in the 20-cycle stability run under load; fresh Codex and Claude, no hints, used it and found the seeded bug in 4 of 4 tasks. These figures are for v1 (`4cd4d20`); details in `docs/RELEASE_NOTES.md`. false_green 17/17 again at `7460edf` under safe_run [CITED, REPORT_U_VIS_CORE2]; not rerun at `b2b5901`. The cold-agent rerun on the F1 to F11 tree is not done yet.

## Block for your memory

```
vision (agent vision for GUI work): python3 -I tools/vis-skill/vision. Guide: tools/vis-skill/docs/ORCHESTRATOR_GUIDE.md.
Use when acceptance needs a rendered web, Linux desktop or Electron UI; not for headless logic, Godot (godot-visual-qa), or Windows/macOS/mobile (not possible).
Fast path: start --job J --app CMD, look, act --click N (expects any change), read back the meaning, act --key ctrl+q --expect-change exit, stop; cite the receipt path stop prints. Core F1–F10 is already merged; optional --step proves no other capability.
rc 3/8 = possible real bug: look and diagnose. rc 5 = rule refused, do not bypass. A visible change, exit 0 or OCR is not proof.
Frames only in /dev/shm; hosted pixels need the operator's consent. Never an inherited display; never read credentials or type real secrets; trusted programs only. Swarm: read vision doctor (missing, ram_available) before launching N jobs; scr-obrero-claude, Muse and oc-* need references/worker_block.md.
```
