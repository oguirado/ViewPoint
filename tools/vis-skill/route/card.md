<!-- L0 card: model output (Gemini Argon, compacted by Claude Opus) from route findings and repo docs; not reviewed by a human. -->
# vis-skill (skill name `vision`; `./vision` is a symlink to `./vis`, the old name stays an alias): private vision skill and receipt-bearing `vis` CLI to run, inspect and verify Linux desktop, Electron and browser interfaces

A thin `SKILL.md` plus a single-file Python 3.11+ stdlib CLI (`vis`) that drives `vision-engine` and `wender` on isolated `tmpfs` displays (`Xvfb` or headless Chrome CDP). Claude Code, Codex and headless swarm workers (via `worker_block.md`) use it to verify visual behaviour without changing host config. Not for headless services, pure CLI/API logic, unit-testable checks or Godot (`godot-visual-qa`). How to query this route: `route/GUIDE.md`.

## Entry points
- `vis::main`: 150 s deadline; `doctor`, `start`, `look`, `act`, `check`, `ask`, `stop`, `sweep`, `adopt`, `attach`. `vis::serve`: background supervisor (`_serve`) for job sessions.
- `install.sh [--dry-run|--apply]`: renders `skill/SKILL.md.tmpl` (`@VISION_PATH@`, `@SOURCE_SHA@`) into `dist/claude/vision/` and `dist/codex/vision/`; `--apply` needs `VISION_INSTALL_OK=1` (`VIS_INSTALL_OK=1` still accepted); host copies are listed by `skill_copies.py`. Tests: `tests/test_vision.py`, `tests/installer_mutants.py`.
- `gates/run_gates.sh [--all]` (unit, must-fail, real programs in capped scopes); `gates/false_green/false_green.py::main` (cases A–Q); `gates/stability.py::main`; `gates/unit/test_cli.py::Unit`; `gates/real_programs.py::main` (`gedit`, `gnome-mines`, Electron, web; engine pins `current` and `compatibility`).

## Layout
Root `vis`, `vis.json` (engine pins), `install.sh`, `AGENTS.md`; `skill/` (`SKILL.md.tmpl`, `references/surfaces.md`, `make_it_visible.md`, `troubleshooting.md`, `worker_block.md`); `dist/` (generated bundles); `docs/` (`CONTRACT.md`, `STRATEGY.md`, `DECISIONS.md`, `ORCHESTRATOR_GUIDE.md`, `RELEASE_NOTES.md`, `V0_LOADING.md`, fix reports, `evidence/`); `gates/` (`common.py`, suites `unit/`, `must_fail/`, `false_green/`, `writer/`); `route/`.

## Laws (file that enforces each)
- One canonical JSON stdout line (`ok`, `rc`, `code`, `verb`), rc 0 or 2–9, never 1 (`vis`, `docs/CONTRACT.md`).
- Frames, marks and job state live only on `tmpfs` `/dev/shm/vqa-cu/vis/<job>/` (mode 0700, statfs magic checked, max 40 frames, 2 GiB); symlinked or non-tmpfs roots fail rc 5.
- Every `act` needs `--expect-change any|none|X,Y,W,H` (else rc 2) and expires earlier `look` marks; at most 3 attempts per step id (the 4th is rc 4 `E_RETRIES`); reusing a passed step id is rc 2.
- A step passes only with a machine-verified `verdict:"pass"` receipt (`vis.receipt/v1`) in `receipts.jsonl`, archived on `stop` through an atomic relative symlink. OCR text, `look`, `ask`, `stop`, exit 0 or stdout `OK` never count.
- rc 5 `E_RULE` on `MV_HOSTED=1`, a display `flock` held by another actor, `--untrusted`, typing into protected browser fields, `--type-file`, or secret-like payloads (`gates/must_fail/test_rules.py`).
- Display ownership is the recorded `Xvfb` PID plus its `/proc/<pid>/stat` start time (or `--owner-ok TOKEN` on `attach`), never the display number or inherited `DISPLAY`; `stop` signals only verified owned descendants, never adopted processes.
- Foreign FUSE mounts in a job dir abort cleanup (rc 5). `gates/false_green/` is an independent read-only suite and is never edited (`docs/DECISIONS.md`).

## Run
`python3 -I vis doctor --skill`; `vis start --job J --app 'gedit --standalone' --size 800x600`; `vis look --job J`; `vis act --job J --step S --type 'Text' --expect-change X,Y,W,H`; `vis check --job J --state 'button=Done'`; `vis stop --job J`; `gates/run_gates.sh --all`. Install: `./install.sh --dry-run`, then `VIS_INSTALL_OK=1 ./install.sh --apply` with explicit authorisation.

## Gaps and traps
- rc 7 `E_ABSENT`: Windows, macOS, mobile, Wayland, GPU/Vulkan under Xvfb; audio and video; desktop AT-SPI state (`check --state` on desktop/Electron); Electron CDP outside a capsule; web `--key`; `check --ask-file`; `vis ask` on hosted seats.
- The `--type` secret filter is per payload: split strings and desktop `--key` events bypass it; X11 password fields are undetectable.
- Early scope teardown can clear `memory.events` and report rc 8 `E_TARGET` instead of rc 6 `E_BUDGET` (`docs/VIS_FIX6_REPORT.md`).
- `--expect-change none` and `--require-stable` sample discretely and can miss short pulses. A pre-fix zombie supervisor adopted by an outside parent cannot be reaped. Codex lists the skill twice if it is defined both globally and in the project.

## Where to look first
| Task or symptom | Look in |
|---|---|
| Exit codes, receipt schema, RAM and frame limits | `docs/CONTRACT.md`, `vis` |
| `E_USAGE`, `E_RETRIES`, `E_RULE`, stale marks | `skill/references/troubleshooting.md` |
| `--web`, `--app` or `--electron`; `wender.state/v1` | `skill/references/surfaces.md` |
| Making an app observable | `skill/references/make_it_visible.md` |
| `vis` rules inside swarm workers | `skill/references/worker_block.md` |
| Orchestrator use (F1-F10, swarm admission, swarmSSH), release history | `docs/ORCHESTRATOR_GUIDE.md`, `docs/RELEASE_NOTES.md` |
| Process leaks, zombies, `displayfd` | `gates/must_fail/test_stability_cleanup.py`, `docs/VIS_FIX4_REPORT.md` |
| False passes and refusals | `gates/false_green/false_green.py`, `gates/must_fail/test_rules.py` |
