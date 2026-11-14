<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Installing and running ViewPoint (assembled release)

This repository bundles the ViewPoint core (MetaViewer based), its agents and UI, the
vendored engines (`components/vision-engine`, `components/wender-agent`,
`components/OpenSuperficies`), the vision CLI (`tools/vis-skill`), the hackathon layer
(`hackathon/`) and the Claude reference stack (`product/`). Everything is AGPL-3.0-only
unless a file or directory carries its own licence notice.

All paths below are relative to the repository root. No step needs a host-specific path:
where a tool lives outside this repository, an environment variable names it.

## 1. Requirements

| Need | Version / note | Used by |
|---|---|---|
| Linux | x86_64 or aarch64; systemd user session (`systemd-run --user`) recommended | everything |
| Python | 3.12 (standard library is enough for `product/` and `tools/vis-skill`) | product, hackathon, vision CLI, engines |
| Rust toolchain | stable `cargo` | `core/` (Cargo workspace), `components/vision-engine/rust` |
| Node.js | with `npm` | `ui/` (Electron + Vite), parts of `components/` |
| Blender | 4.0.2 (the version the demos were measured with) | hackathon swarm, `product/ref_demo` |
| Docker | daemon reachable by your user | `product/ref_capsule`, `hackathon/gateway` image, capsules |
| X tools | `Xvfb`, `xdotool`, `ffmpeg`, `openbox`, `xprop`, `dbus-run-session` | vision CLI private displays, recording |
| Tesseract data | optional; `TESSDATA_PREFIX` (default `~/.local/share/tessdata`) | OCR in the vision engine |
| Agent host | Antigravity CLI (`agy`) or another MCP-capable agent host (Claude Code, Codex) | real (non-stub) agents |
| Optional | `pytest` (the suites also run with `python3 -m unittest`) | tests |

Check the desktop prerequisites with the vision CLI:

```sh
python3 -I tools/vis-skill/vision doctor
```

It lists missing tools per surface (`desktop`, `web`) and the engines it found.

## 2. Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `VP_REPO_ROOT` | the checkout root (computed from file locations) | used by a few tests that read sibling units |
| `VP_VISION_CLI` | `tools/vis-skill/vision` in this checkout | vision CLI used by `product/ref_broker`, `product/agent_interface/cli/vp` and `hackathon/swarm/broker` |
| `VIS_WENDER_REPO`, `VIS_VE_REPO` | `components/wender-agent`, `components/vision-engine` | git checkouts the vision CLI archives its engines from (see limits) |
| `VP_AGY` | `agy` on `PATH` | Antigravity CLI used by `product/ref_agents/runner.py` for real agents |
| `VP_REF_DIR` | `product/` of this checkout | reference stack root used by `product/ref_tasklight/live` |
| `VP_REF_BROKER_DIR` | `product/ref_broker` | broker used by `product/ref_auction` tests |
| `VP_HACKATHON_ROOT` | `hackathon/` of this checkout | root of the swarm run directory (`swarm/run/...`) |
| `VP_OSB_CLI` | `switchboard-skill/bin/osb` (relative, external) | OpenSwitchboard CLI for room projection (optional) |
| `VP_CW_REPO` | `FireBase_BuildHybridAI/repo` (relative, external) | TaskLight / context-wall checkout for projection (optional) |
| `VP_OPS_ROOT` | the directory that holds this checkout | workspace root for `vp`; per-key overrides in `product/agent_interface/cli/vp_paths.json` or `$VP_OPS_<KEY>` |
| `VP_ROUTE_ENGINE` | `$VP_OPS_ROOT/newton-py/route/route.py` | engine for `vp docs route` (external, optional) |
| `GEMINI_API_KEY` | unset | only for the hackathon Cloud Run gateway planner (see section 7) |

## 3. Build

```sh
# Rust core
cargo build --workspace
# UI (Electron + Vite)
(cd ui && npm ci && npm run build)
# Engines: see each component's README / QUICKSTART
#   components/vision-engine/QUICKSTART.md, components/wender-agent/README.md,
#   components/OpenSuperficies/README.md
```

`product/`, `hackathon/swarm/` and `tools/vis-skill/` need no build step (standard library
Python plus the system tools above). `hackathon/gateway` has its own `requirements.txt`
and `Dockerfile`.

## 4. Test

Run Python suites inside a memory-capped scope; the commands below are the ones used to
check this release.

```sh
cap() { systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 "$@"; }
export PYTHONDONTWRITEBYTECODE=1

# Rust core and UI
cargo test --workspace
(cd ui && npm test)

# Hackathon layer (run from hackathon/)
(cd hackathon && for m in swarm.broker.test_broker swarm.agents.test_agents swarm.overlay.test_overlay \
   swarm.projection.test_projection swarm.viewer.test_viewer swarm.docs.test_docs \
   product.fanout.test_fanout product.admission.test_admission; do cap python3 -m unittest "$m"; done)

# Claude reference stack: each unit from its own directory
for t in $(find product -name 'test*.py' -not -path '*/ref_auction/judge/*' | sort); do (cd "$(dirname "$t")" && cap python3 -m unittest "$(basename "$t" .py)"); done
(cd product/ref_auction && cap python3 -m unittest judge.test_judge judge.test_replay)   # package imports

# Vision CLI (needs git clones of the engines, see section 8)
(cd tools/vis-skill && VIS_VE_REPO=/path/to/vision-engine.git VIS_WENDER_REPO=/path/to/wender-agent.git \
   cap python3 -m unittest discover -s tests)
```

Some checks need a git history (`git init && git add -A && git commit` first) or the
sibling tools of the original workspace; see section 8 for the list measured on this release.

Live tests (names containing `live`) need Blender, Xvfb and the external tools; they skip
when those are absent.

## 5. Integrated demo

```sh
product/ref_demo/run_demo.sh [tag] [--no-auction] [--no-settle]
```

One command: starts a private Blender on its own Xvfb display through the vision CLI, the
rewind broker with the auction scheduler and the judge, records the display, runs a scenario
with one deliberate wrong edit (the judge rejects it and the capsule rewinds), composes the
overlay, runs four stub agents, and writes evidence to `product/ref_demo/evidence/<tag>/`
(created at run time, not shipped). Projection to OpenSwitchboard and TaskLight runs only
when `VP_OSB_CLI` / `VP_CW_REPO` point to those tools; the local event log stays the record
of truth either way. Details: `product/ref_demo/README.md`.

The hackathon swarm (Gemini Track H) is documented in `hackathon/README.md`.

## 6. `vp` CLI and the MCP server

```sh
python3 -I product/agent_interface/cli/vp --help          # 88 commands, noun/verb taxonomy
python3 -I product/agent_interface/cli/vp sys doctor --dry-run
```

Every command prints bounded text then one JSON receipt line (`ok, code, rc, data, next`);
`--dry-run` shows the upstream command without starting anything. See
`product/agent_interface/cli/README.md`.

MCP server (stdio, standard library only), exposing every `vp` command as a tool:

```sh
python3 -I product/agent_interface/mcp/viewpoint_ops_mcp.py
```

Ready-to-copy registrations for Claude Code, Codex and Antigravity are in
`product/agent_interface/mcp/register/` (`claude.mcp.json`, `codex.config.toml`,
`REGISTER.md`); run the client from the repository root or make the path absolute in your
copy. The hackathon `viewpoint-agent` MCP server is in `hackathon/mcp/`.

## 7. What needs a Gemini key or account

| Feature | Needs |
|---|---|
| `hackathon/gateway` planner (Cloud Run) | `GEMINI_API_KEY` (from Secret Manager in Cloud Run). Without it the gateway answers `GEMINI_KEY_MISSING`. |
| Real Gemini agents (`hackathon/swarm/agents`, `product/ref_agents` without `--stub`) | an Antigravity CLI (`agy`) login on the machine; no key is read by this repository |
| Everything else (core, UI, engines, vision CLI, `vp`, MCP server, stub agents, `run_demo.sh`) | nothing |

Never put a key in the repository; the capsule gates (`product/ref_capsule/gates`) refuse
runs whose logs show credential reads.

## 8. Honest limits

- Measured on one Linux aarch64 host; x86_64 is expected to work but was not re-measured for this release.
- The Blender swarm and the demo run agents as host processes in memory-capped scopes under
  one UID on a private Xvfb display. Nonces and write-set claims catch accidental
  cross-writes; they are not an OS security boundary against a hostile same-UID process.
- The vision CLI fetches its engines with `git archive` at the SHAs pinned in `deps.lock`. The
  vendored `components/` directories are plain copies, so point `VIS_WENDER_REPO` and
  `VIS_VE_REPO` at git clones of the engine repositories that contain those commits.
- `vp docs route` needs the external newton-py route engine (`VP_ROUTE_ENGINE`); without it
  the command answers `DEPENDENCY`. The shipped router index
  (`product/agent_interface/router/findings.jsonl`) was built in the original workspace and
  may cite documents that are not in this repository; rebuild it with `vp docs build`.
- OpenSwitchboard and TaskLight projection are optional external tools, not vendored here.
- Hackathon proof logs (`hackathon/*/proof/`) are redacted copies; their SHA-256 values in
  `hackathon/PROVENANCE.md` refer to the unredacted originals, so `swarm.docs.test_docs`
  reports hash mismatches (`test_d2_proof_hashes_pass`), and two of its checks
  (`test_d1_committed_links_pass`, `test_bl8_...`) need a git `HEAD`.
- Other checks that depend on the original multi-worktree workspace or on git history:
  `product/ref_rewind*/` "untouched since b255057" checks (git history),
  `product/agent_interface/design` `caps.py --check` (cites sibling worktrees under `$VP_OPS_ROOT`),
  `product/agent_interface/cli` `RealReadOnly.test_sys` / `test_room_read_real` (external tools),
  `product/agent_interface/router` pack-size bound (3004 > 3001 bytes/3 on the rebuilt public corpus),
  `tools/vis-skill/tests/test_route.py` (git `ls-files`).
