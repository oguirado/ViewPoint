<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Installing and running ViewPoint (assembled release)

This repository bundles the ViewPoint core (MetaViewer based), its agents and UI, the
vendored engines (`components/vision-engine`, `components/wender-agent`,
`components/OpenSuperficies`), the vision CLI (`tools/vis-skill`), the hackathon layer
(`hackathon/`). Everything is AGPL-3.0-only unless a file or directory carries its own
licence notice. The Claude reference stack (the repository-root `product/`) that earlier assembled builds
carried is not needed to run ViewPoint and now lives on the `claude-reference` branch
(see [PROVENANCE.md](PROVENANCE.md)).

These instructions build the ViewPoint `0.3.3` source release [CITED: `VERSION`]. See the [changelog](CHANGELOG.md) for the changes in this source release. Use [deps.lock](deps.lock) for the component commits and vendored tree digests.

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
| Docker | daemon reachable by your user; on DGX Spark use the shipped `docker-ce`, never `docker.io` (see root prerequisites below) | `hackathon/gateway` image, capsules |
| X tools | `Xvfb` (`xvfb-run`), `xauth`, `xdotool`, `ffmpeg`, `openbox`, `xprop`, `dbus-run-session` (+ `python3-venv`) | vision CLI private displays, recording, headless UI tests |
| Tesseract data | optional; `TESSDATA_PREFIX` (default `~/.local/share/tessdata`) | OCR in the vision engine |
| Agent host | Antigravity CLI (`agy`) or another MCP-capable agent host (Claude Code, Codex) | real (non-stub) agents |
| Optional | `pytest` (the suites also run with `python3 -m unittest`) | tests |

Check the desktop prerequisites with the vision CLI:

```sh
python3 -I tools/vis-skill/vision doctor
```

It lists missing tools per surface (`desktop`, `web`) and the engines it found.

Keep the checkout path short. One gate-plan test asserts an AF_UNIX socket path stays under 108 bytes (`tests/test_vp_gate_plan.py`); a deep clone path (for example under `~/Documentos/publish/wave-gold/...`) can push it over the limit before the test even runs. Clone under a short parent such as `~/ViewPoint`. Verified on Linux aarch64 (DGX Spark, Ubuntu 24.04) by the clean-install audit; on Ubuntu 24.04 x86_64 the same steps are expected but were not checked beyond hosted CI (see section 7).

### Toolchain recipes (non-root, Ubuntu 24.04)

Ubuntu 24.04 `apt` ships Node 18, Go 1.22 and a Rust older than the pins above; use these non-root recipes for the language toolchains (no `sudo` — `apt` packages, `docker-ce` and the `docker` group are root steps under Root prerequisites below). The build, test, package and launch steps below were verified on aarch64 (DGX Spark); the root provisioning steps (Docker, system packages, the user-namespace `sysctl`) were **not** exercised on a fresh host — the audit host was already provisioned — and are expected-but-unchecked on x86_64.

- **Node 24 via `nvm`** (do not use the system Node 18):

  ```sh
  # install nvm once (https://github.com/nvm-sh/nvm), then:
  nvm install 24
  nvm use 24
  node --version   # expect v24.x
  ```

- **Go 1.26 from the official tarball** (never the Ubuntu 1.22 package):

  ```sh
  # download go1.26.4.linux-arm64.tar.gz (or go1.26.4.linux-amd64.tar.gz on x86_64)
  # from https://go.dev/dl and unpack to $HOME/go-1.26, then:
  export PATH="$HOME/go-1.26/bin:$PATH"
  go version       # expect go1.26.x
  ```

- **Rust stable via `rustup` plus the `1.92.0` rustfmt toolchain the gate uses:**

  ```sh
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  export PATH="$HOME/.cargo/bin:$PATH"
  rustup toolchain install stable
  rustup toolchain install 1.92.0 --profile minimal --component rustfmt
  rustup run 1.92.0 cargo fmt --version
  ```

**Root prerequisites (needs root).** The steps in this block need `sudo` and are not part of the non-root recipes above.

- **Docker on DGX Spark:** the machine ships NVIDIA's `docker-ce` (`29.6.2`). Do **not** run `sudo apt-get install docker.io` there; it attempts to remove `docker-ce`. Just ensure the daemon is reachable (`docker ps`) and your user is in the `docker` group (`sudo usermod -aG docker "$USER"`, then log in again). On a plain Ubuntu host without `docker-ce`, install the `docker-ce` engine instead of `docker.io` (with `sudo`).
- **System packages:** also install `python3-venv` and `ffmpeg` with `sudo apt-get install python3-venv ffmpeg` (needed by the venv step and by `hackathon/swarm/tools/compose.sh` `ffprobe`); the vision-CLI / swarm steps below need them. Headless UI tests (section 4) additionally need `xvfb` and `xauth`: `sudo apt-get install xvfb xauth` (this step was not exercised by the no-`sudo` audit).

**AppArmor unprivileged user-namespaces (Ubuntu 24.04, needs root).** A fresh Ubuntu 24.04 defaults to `kernel.apparmor_restrict_unprivileged_userns=1`, which blocks Electron's Chromium user-namespace sandbox. On the audit host it was already `0`; on a brand-new machine, run once with `sudo` (a root prerequisite, like the Docker and `apt` steps above):

```sh
sysctl -n kernel.apparmor_restrict_unprivileged_userns   # check: 1 means restricted
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
```

Persist it across reboots with a `sysctl.d` entry or install the AppArmor profile instead (`sudo sysctl --system`). Verified on aarch64 (DGX Spark, audit host already at `0`); on x86_64 the same system knob applies but was not checked.

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
# First install uses online `npm ci` (above). Once the npm cache is filled and
# verified, later file-locked installs may repeat offline from that cache alone:
#   (cd ui && npm ci --offline && npm run build)
# Never use `npm ci --offline` for a cold install: an empty cache fails there.
# sync_kit.mjs refuses a kit checkout whose HEAD or content differs from ui/kit.lock.json
# Electron 44.9.2: `npm ci` does NOT download the Electron binary
# (electron@44.9.2 ships no postinstall script, so ui/node_modules/electron/dist
# stays empty and ~/.cache/electron stays absent until you install explicitly).
# Run the repository's checksum-verifying installer immediately after `npm ci`
# and BEFORE `npm test`, `npm run smoke` and scripts/bootstrap.sh.
# The installer needs network access (or a pre-filled ~/.cache/electron) and
# ELECTRON_SKIP_BINARY_DOWNLOAD unset, so run it with that variable cleared:
(cd ui && env -u ELECTRON_SKIP_BINARY_DOWNLOAD node node_modules/electron/install.js)
# The installer downloads the matching zip from GitHub Releases at
# https://github.com/electron/electron/releases/download/v44.9.2/electron-v44.9.2-linux-arm64.zip
# on aarch64 (DGX Spark) or .../electron-v44.9.2-linux-x64.zip on x86_64,
# verifies the SHA-256 against ui/node_modules/electron/checksums.json,
# populates ~/.cache/electron/be022f5c.../ and extracts
# ui/node_modules/electron/dist. The CI wrapper .github/workflows/install_electron.cjs
# does the same check and is an equivalent entry point.
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
# hackathon/swarm/tools/compose.sh spawns plain `python3`, so the venv must be
# first on PATH or test_overlay fails with `No module named 'cv2'`:
export PATH="$PWD/.venv/bin:$PATH"

# Rust core (needs Go on PATH; bounded test threads avoid timing failures on busy hosts)
cargo test --workspace -j 4 -- --test-threads=4
# UI (after the kit staging of section 3 and the Electron install above). The UI
# tests need a display and a local Electron distribution. Set
# ELECTRON_OVERRIDE_DIST_PATH to the installed package dist directory
# `ui/node_modules/electron/dist` (created by the install.js step of section 3)
# and run headless under `xvfb-run -a` (`Xvfb` and `xauth` from the X tools row
# above). Full headless invocation, from the repository root:
# Two tests, test/v028-fit-render.test.js and test/floating-constraints-render.test.js,
# fail without the dist or the display (measured on aarch64: 4580 tests, 4579 pass,
# 0 fail, 1 skipped with Electron installed; without an Electron dist 4576 pass, 2 fail, 2 skipped).
(cd ui && ELECTRON_OVERRIDE_DIST_PATH="$PWD/node_modules/electron/dist" xvfb-run -a npm test)

# Packaged app (scripts/bootstrap.sh output): the retail launcher refuses to start
# with `RETAIL_ENV: retail launch refused` (exit 1) whenever any forbidden
# variable is still exported. The forbidden set is every `PYTHON*`, `ELECTRON_*`,
# `NODE_*`, `LD_*`, `DYLD_*` variable, plus `BASH_ENV`, `ENV`, `GCONV_PATH`,
# `HOSTALIASES` and `ZDOTDIR` (see ui/electron/coreLauncher.js). Unsetting only
# the variables you remember is not enough: an unlisted leftover (for example
# `ELECTRON_SKIP_BINARY_DOWNLOAD`) still refuses the launch. Launch from a
# shell where the build variables were never exported (for example a new login
# shell), or rebuild a minimal environment with `env -i` and only the display
# variables. The audit used a clean environment for its 60 s private-Xvfb launch
# on aarch64; the `env -i` reconstruction below was not separately measured.
# From the packaged output directory:
#   env -i PATH="$PATH" HOME="$HOME" DISPLAY="$DISPLAY" \
#       XAUTHORITY="$XAUTHORITY" XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
#       DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" ./metaviewer
# x86_64 launch unchecked.

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

- Checked on a clean clone on Linux aarch64 (DGX Spark, Ubuntu 24.04, Python 3.12, Rust 1.99.1, Go 1.26.4, Node 24.14) by literal execution of this guide
  under an 8 GB cap, on a pre-provisioned host: the audit ran without `sudo`,
  `kernel.apparmor_restrict_unprivileged_userns` was already `0`, and `docker-ce`
  was already installed, so it covers the build, tests, package
  (`MetaViewer-linux-arm64.tar`, 328,581,120 B, `--verify-install` and `--smoke`)
  and the 60 s private-Xvfb retail launch — not fresh-host provisioning (system
  packages, Docker access, the user-namespace `sysctl`), which the guide
  documents but the audit did not exercise. Rust workspace 593 tests pass,
  `agents/` 916 pass, `hackathon` broker/agents/viewer 55 pass,
  UI 4579/4580 with Electron 44.9.2 installed. On Ubuntu 24.04 x86_64 the same steps
  are expected to work; the hosted `Quickstart` workflow ran the README Quickstart
  literally on clean `ubuntu-24.04` (x86_64) and `ubuntu-24.04-arm` runners and
  passed (run 43672054400), together with the full hosted CI (run 43672054373).
  The desktop launch and packaging on a physical x86_64 machine remain unchecked. The live Electron dist still needs `ELECTRON_OVERRIDE_DIST_PATH` or the `ui/node_modules/electron/dist`
  install above.
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
