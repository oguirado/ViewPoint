<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Installing and running ViewPoint (assembled release)

This repository bundles the ViewPoint core (MetaViewer based), its agents and UI, the
vendored engines (`components/vision-engine`, `components/wender-agent`,
`components/OpenSuperficies`), the vision CLI (`tools/vis-skill`), the hackathon layer
(`hackathon/`). Everything is AGPL-3.0-only unless a file or directory carries its own
licence notice. The Claude reference stack (the repository-root `product/`) that earlier assembled builds
carried is not needed to run ViewPoint and now lives on the `claude-reference` branch
(see [PROVENANCE.md](PROVENANCE.md)).

All paths below are relative to the repository root. No step needs a host-specific path:
where a tool lives outside this repository, an environment variable names it.

## 1. Requirements

| Need | Version / note | Used by |
|---|---|---|
| Linux | x86_64 or aarch64; systemd user session (`systemd-run --user`) recommended | everything |
| Python | 3.12; the standard library is enough for `tools/vis-skill`, the swarm replay viewer, the gateway and the MCP server; the `agents/` tests and the `hackathon/swarm` broker also need `numpy` 2 or newer and `Pillow`, and the swarm overlay also needs OpenCV (`cv2`: `pip install opencv-python-headless`) (a venv made with `python3 -m venv --system-site-packages .venv` and `pip install 'numpy>=2' pillow`, run by its absolute path as in the README Quickstart) | agents, hackathon, vision CLI, engines |
| Rust toolchain | stable `cargo` | `core/` (Cargo workspace), `components/vision-engine/rust` |
| Go | 1.26 or newer (`daemon-adapter/go.mod`); one `cargo test` case runs `go test ./internal/daemon` | `daemon-adapter/`, `components/OpenSuperficies`, core tests |
| Node.js | 24 or newer, with `npm` (`ui/package.json` engines; Node 18 fails the build) | `ui/` (Electron + Vite), parts of `components/` |
| capsule-ui-kit | public repository `https://github.com/oguirado/capsule-ui-kit` at the commit pinned in `ui/kit.lock.json`, staged into `ui/.kit/` (section 3) | `ui/` build and tests |
| Blender | 4.0.2 (the version the demos were measured with) | hackathon swarm |
| Docker | daemon reachable by your user | `hackathon/gateway` image, capsules |
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
| `VP_VISION_CLI` | `tools/vis-skill/vision` in this checkout | vision CLI used by `hackathon/swarm/broker` |
| `VIS_WENDER_REPO`, `VIS_VE_REPO` | `components/wender-agent`, `components/vision-engine` | git checkouts the vision CLI archives its engines from. The defaults are plain vendored copies inside this repository, where `git archive` of the pinned engine commit fails; live vision use needs git clones of the engine repositories, which are not public (see limits) |
| `MV_WENDER_ROOT` | `components/wender-agent` (vendored) | `wender-agent` root verified against `deps.lock` by `agents/metaviewer_agents/perception/deps_lock.py` |
| `VQA_VE_ROOT` | `components/vision-engine` (vendored) | `vision-engine` root verified against `deps.lock` by `agents/metaviewer_agents/perception/deps_lock.py` |
| `VP_HACKATHON_ROOT` | `hackathon/` of this checkout | root of the swarm run directory (`swarm/run/...`) |
| `VP_OSB_CLI` | `switchboard-skill/bin/osb` (relative, external) | OpenSwitchboard CLI for room projection (optional) |
| `VP_CW_REPO` | `FireBase_BuildHybridAI/repo` (relative, external) | TaskLight / context-wall checkout for projection (optional) |
| `GEMINI_API_KEY` | unset | only for the hackathon Cloud Run gateway planner (see section 6) |

`deps_lock.py` uses a three-way dispatch (after any `MV_WENDER_EXECUTABLE` / `MV_VISION_EXECUTABLE` compiled override):
1. **Git checkout** (`<root>/.git` exists, checked first even if `<root>` matches `vendored_path`, or an in-repo monorepo subtree): verifies the pinned `commit`, index flags, tracked blobs/modes, and clean tracked status.
2. **Vendored directory** (when `MV_WENDER_ROOT` or `VQA_VE_ROOT` is unset, or points to the lock entry's `vendored_path`): always content-hashes `vendored_tree_sha256` over sorted relative file/symlink paths, normalized file modes (`644` or `755` by the executable bit), file bytes, and relative in-root symlink targets without directory entries (`deps.lock` also pins `opensuperficies` at `components/OpenSuperficies`). Vendored verification fails closed (`dirty_tree`) if the tree contains any `__pycache__` directory, `*.pyc`/`*.pyo` bytecode file, or any absolute or root-escaping symlink; run tests and engines with `PYTHONDONTWRITEBYTECODE=1` so Python does not write bytecode into `components/`.
3. **Wheel** (any other non-checkout root): verifies the pinned `commit` in `<name>-*.dist-info/direct_url.json` and installed `RECORD` digests, failing closed if metadata is missing, ambiguous, or tampered.

Setting `MV_WENDER_ROOT=""` or `VQA_VE_ROOT=""` (empty or whitespace-only) fails closed with `unavailable` rather than falling back to the vendored directory; fallback applies only when the variable is unset.

## 3. Build

```sh
# Rust core (bounded parallelism keeps the build under an 8 GB memory cap)
cargo build --workspace -j 4
# UI (Electron + Vite): Node 24+, then stage the pinned capsule-ui-kit into ui/.kit/
git clone https://github.com/oguirado/capsule-ui-kit.git ../capsule-ui-kit
git -C ../capsule-ui-kit checkout -q aaada9dedc177b096e8b4764959e258e76c1ef02   # commit in ui/kit.lock.json
(cd ui && node ../tools/sync_kit.mjs ../../capsule-ui-kit && npm ci && npm run build)
# sync_kit.mjs refuses a kit checkout whose HEAD or content differs from ui/kit.lock.json
# Engines: see each component's README / QUICKSTART
#   components/vision-engine/QUICKSTART.md, components/wender-agent/README.md,
#   components/OpenSuperficies/README.md
```

Build engines out of tree or in a separate copy: build outputs inside `components/` change
the vendored trees that `deps.lock` hashes, and `deps_lock.py` then refuses them
(`dirty_tree`).

`hackathon/swarm/` and `tools/vis-skill/` need no build step (Python plus the packages and
system tools above). `hackathon/gateway` has its own `requirements.txt`
(it declares no third-party packages) and `Dockerfile`.

## 4. Test

Run Python suites inside a memory-capped scope; the commands below are the ones used to
check this release.

```sh
cap() { systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 "$@"; }
export PYTHONDONTWRITEBYTECODE=1
PY="$PWD/.venv/bin/python"   # the numpy/Pillow venv of the README Quickstart, step 3
"$PY" -m pip install opencv-python-headless   # cv2, needed by swarm.overlay

# Rust core (needs Go on PATH; bounded test threads avoid timing failures on busy hosts)
cargo test --workspace -j 4 -- --test-threads=4
# UI (after the kit staging of section 3). Two tests, test/v028-fit-render.test.js and
# test/floating-constraints-render.test.js, need a local Electron dist in
# ELECTRON_OVERRIDE_DIST_PATH and xvfb-run; without them they fail
# (measured: 4580 tests, 4576 pass, these 2 fail, 2 skipped).
(cd ui && npm test)

# Hackathon layer (run from hackathon/)
(cd hackathon && for m in swarm.broker.test_broker swarm.agents.test_agents swarm.overlay.test_overlay \
   swarm.projection.test_projection swarm.viewer.test_viewer swarm.docs.test_docs \
   product.fanout.test_fanout product.admission.test_admission; do cap "$PY" -m unittest "$m"; done)
(cd hackathon/gateway && cap python3 -m unittest discover -s tests)
(cd hackathon/mcp/vp_mcp && cap python3 -m unittest discover -s tests)

# Agents package
(cd agents && cap "$PY" -m unittest discover -s tests)

# Vision CLI: maintainer-only. Needs git clones of the private engine repositories and the
# generated skill copies under tools/vis-skill/dist/ (made by tools/vis-skill/install.sh,
# which also writes into $HOME); neither is in this repository, see section 7.
(cd tools/vis-skill && VIS_VE_REPO=/path/to/vision-engine.git VIS_WENDER_REPO=/path/to/wender-agent.git \
   cap python3 -m unittest discover -s tests)
```

Run these from a git clone: a few checks read git history or `git ls-files` (an exported
tree without `.git` fails them). Known failures are listed in section 7.

Live tests (names containing `live`) need Blender, Xvfb and the external tools; they skip
when those are absent.

## 5. Hackathon layer

The hackathon swarm (Gemini Track H), the Cloud Run gateway and the `viewpoint-agent` MCP
server (`hackathon/mcp/`) are documented in [hackathon/README.md](hackathon/README.md).

The integrated Blender demo (`product/ref_demo/run_demo.sh`), the `vp` operations CLI and
its MCP server were part of the Claude reference stack; they are on the `claude-reference`
branch, not on `main`.

## 6. What needs a Gemini key or account

| Feature | Needs |
|---|---|
| `hackathon/gateway` planner (Cloud Run) | `GEMINI_API_KEY` (from Secret Manager in Cloud Run). Without it the gateway answers `GEMINI_KEY_MISSING`. |
| Real Gemini agents (`hackathon/swarm/agents`) | an Antigravity CLI (`agy`) login on the machine; no key is read by this repository |
| Everything else (core, UI, engines, vision CLI, `viewpoint-agent` MCP server, swarm viewer replays) | nothing |

Never put a key in the repository. Pre-push checks: `gitleaks` (config in `.gitleaks.toml`)
and `tools/check_public.sh`. The latter was written for the hackathon-only repository, so on
this tree it always reports the public GitHub handle, the Rust/Go sources and synthetic test
canaries (exit 1); review its `SECRET` and `PATH` lines rather than the exit code.

## 7. Honest limits

- Measured on one Linux aarch64 host; x86_64 was not checked for this release.
- The Blender swarm and the demo run agents as host processes in memory-capped scopes under
  one UID on a private Xvfb display. Nonces and write-set claims catch accidental
  cross-writes; they are not an OS security boundary against a hostile same-UID process.
- The vision CLI fetches its engines with `git archive` at the SHAs pinned in `deps.lock`. The
  vendored `components/` directories are plain copies, so it needs `VIS_WENDER_REPO` and
  `VIS_VE_REPO` pointing at git clones of the engine repositories that contain those commits.
  Those repositories are not public, so live vision use (and the Vision CLI test line above)
  works only on the maintainer's machines; the engine source itself is vendored here.
- OpenSwitchboard and TaskLight projection are optional external tools, not vendored here.
- Hackathon proof logs (`hackathon/*/proof/`) were redacted for publication before release
  `v0.3.0-hackathon` (commit `79a69b5`); their SHA-256 values in `hackathon/PROVENANCE.md`
  refer to the unredacted originals, so `swarm.docs.test_docs` reports hash mismatches
  (`test_d2_proof_hashes_pass`, an expected failure). Two Track P logs
  (`product/proof/P5_1252.jsonl`, `P6_1252.jsonl`) were redacted again during assembly; see
  `PROVENANCE.md`. Two of its checks
  (`test_d1_committed_links_pass`, `test_bl8_...`) need a git `HEAD`.
- `deps_lock.py` verifies files by path before the caller imports or runs from that directory, leaving a time-of-check/time-of-use (TOCTOU) window under a hostile same-UID writer.
- `tools/vis-skill/tests/test_route.py` needs git `ls-files` (a clone, not an exported tree).
- `tools/vis-skill/dist/` (the rendered skill copies) is not shipped: it holds absolute paths of
  the installing machine. `python3 -I tools/vis-skill/vision doctor --skill` therefore reports
  drift, and the vision CLI's `DoctorTest`/`InstallerTest` cases fail until
  `tools/vis-skill/install.sh` has rendered it.
