# ViewPoint Swarm Agents SDK & Runner (`swarm/agents/`)

Implements the **H2** multi-agent layer strictly adhering to `swarm/CONTRACT.md`:
- Read-only inspection of `/dev/shm/vqa-cu/swarm/frames/` (`latest.json` + `frame_<N>.png`)
- Atomic intent enqueueing (`.tmp` + `os.replace()`) strictly to `/dev/shm/vqa-cu/swarm/intents/<agent_id>/<event_id>.json`
- Never calls `vision`; never writes to `SWARM_INBOX` (`/dev/shm/vqa-cu/swarm/inbox`)

## Modules

- `swarm/agents/__init__.py`: Canonical role definitions, write prefixes, allowed GUI keys (`KP_1`, `KP_3`, `KP_7`, `KP_4`, `KP_Add`, `Home`), and default paths.
- `swarm/agents/intent_cli.py`: Deterministic CLI (`observe`, `enqueue-script`, `enqueue-gui`, `wait-event`) enforcing `CONTRACT.md` §5.1 schemas, claim prefixes, GUI key/agent constraints, inbox write refusal, run-scoped `.offsets/<event_id>.json` log offsets, `--timeout-s 540`, and `wait-event` exit codes (`0` verified, `2` `NO_VISIBLE_CHANGE`, `3` `STALE_FRAME` parked at `drops == 3`, `4` `DUPLICATE_EVENT`/`QUEUE_FULL`, `124` CLI timeout, `1` hard error without auto-repeating `INPUT_UNCERTAIN`/`OUTCOME_UNCERTAIN`).
- `swarm/agents/prompts.py`: Byte-identical `SHARED_BRIEF_PREFIX` (`>= 1200` bytes at offset `0`) + run-scoped `ROLE_SUFFIXES` for `modeler-agent`, `props-agent`, `look-agent`, and `camera-agent` collaborating on **StarForge Sentinel**, with H4 (`swarm/projection/projector.py`) as the sole TaskLight (`context-wall`) owner.
- `swarm/agents/scripted_smoke.py`: Non-LLM deterministic scripted producer (`--agents 2|4`, `--demo-stale-rebase`, `--dry-run`) using Blender 4.0.2 `bmesh` + `bpy.data` pure-datablock scripts and `intent_cli.py`; any relative `--output-dir` is resolved against `REPO_ROOT` (`~/Documentos/viewpoint-hackathon`).
- `swarm/agents/runner.py`: Parallel headless `~/.local/bin/agy` launcher inside deterministic `systemd-run --user --scope --unit=vpswarm-agent-<run_tag>-<role> -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0` (with `GOMEMLIMIT` and `SWARM_AGENT_ID`), `--dry-run`, `--kill-all` (verifying `/proc/<pid>/stat` `starttime_ticks` from `.proc.json`), proof logs, and SHA-256 summary JSON.
- `swarm/agents/test_agents.py`: Unit tests and negative controls.

## Quick Commands

```bash
# Run unit tests inside systemd-run memory scope
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  python3 -m unittest discover -s swarm/agents -p 'test_*.py' -v

# Dry-run 4-agent briefs and exact agy command lines
python3 -m swarm.agents.runner --dry-run

# Dry-run 4-agent scripted smoke
python3 -m swarm.agents.scripted_smoke --agents 4 --dry-run

# Live 4-agent scripted smoke with STALE_FRAME rebase demo (requires running H1 broker; relative --output-dir resolves against REPO_ROOT)
python3 -m swarm.agents.scripted_smoke --agents 4 --demo-stale-rebase --output-dir ~/Documentos/viewpoint-hackathon/swarm/run/output
```
