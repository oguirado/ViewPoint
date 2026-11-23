# Components

This page indexes the build units and support trees in the MetaViewer checkout. [ARCHITECTURE.md](ARCHITECTURE.md) owns the cross-component flows and interfaces; each paragraph here owns one component's inventory and links the most specific page present in this checkout. The rule is the same at every level: a component consumes the layer below it instead of re-implementing it (AX-12 in P01.12, applied to narrow command surfaces in CLX02.02). Each entry is admitted by its role, path, interface owner and the evidence named here; a missing pin blocks that component's use, not the whole index (HB-0007).

Answers: which components exist, what does each own, and where is each one documented?

| Component | Tree | Interface it owns |
| --- | --- | --- |
| Authority core | `core/` | Core endpoint, admission decisions and the journal |
| Surface adapter | `daemon-adapter/` | Native surface lifecycle, input and capture publication |
| Wall UI | `ui/` | The wall, human input and frame presentation |
| Agent client | `agents/` | Agent tools, CLI and MCP transport |
| Contracts | `contracts/` | Versioned wire schemas and examples |
| Tools and gates | `tools/`, `scripts/` | Verification, generation and installation |
| Retrieval | `route/`, `router/`, `catalogue/` | Source and document lookup |

## Authority core

The authority core is the Rust crate `metaviewer-core` under `core/`, the only Cargo workspace member. `core/src/lib.rs` exposes admission, serve, journal, verify, replay, transport, wire, catalog, control, record, supervisor and the guest, headless and rewind state machines. The `metaviewer-core` binary binds the core endpoint, validates registrations and serves the session; `mv-replay` reconstructs or verifies a journal without injecting input. The core owns admission, authority, verification and journal state; it does not capture pixels, start native programs or render the wall. Headless requests are routed by `core/src/serve/headless.rs` and admitted by the state machine in `core/src/headless.rs`, with separate handlers for guest commands and Rewind. Read [Architecture](ARCHITECTURE.md#core-authority-and-replay) and [Reference](REFERENCE.md#authority).

## Surface adapter

The surface adapter is the Go module `metaviewer/daemon-adapter` under `daemon-adapter/`. Its single binary `mv-daemon-adapter` opens the adapter endpoint and routes lifecycle, input, frame, record and guest traffic; internal packages keep custody, capture, transport bounds, native process calls and guest bridges separate. It validates a native target before starting it, publishes captured frames and retains uncertain custody; it does not decide admission. The guest-side Python runtimes live under `daemon-adapter/guest_command_runtime/` and `daemon-adapter/rewind_runtime/`, and the standalone broker is `daemon-adapter/guest-command-broker/`. Read [Architecture](ARCHITECTURE.md#process-and-transport-model) and [Create and destroy](ARCHITECTURE.md#create-and-destroy).

## Wall UI

The wall UI is the Electron main process plus a sandboxed Vue renderer under `ui/`. It draws admitted state, sends closed intents, verifies each frame's packed size, digest and generation before painting, and keeps terminal and guest-console paths distinct. It holds no admission authority, and a presentation-only move never creates, stops or rebinds a capsule. The renderer's component kit is pinned separately in `ui/kit.lock.json`; that kit is not one of the `deps.lock` private components. Read [UI architecture](UI_ARCHITECTURE.md).

## Agent client

The agent client is the Python package `metaviewer-agents` under `agents/`. `agents/metaviewer_agents/client.py` speaks the core envelope; `agents/metaviewer_agents/tools.py` validates the closed observe, list, propose and wait results; `agents/metaviewer_agents/mcp_server.py` is a mechanical stdio MCP bridge that never opens sockets; `agents/metaviewer_agents/mvctl.py` is the CLI; `agents/metaviewer_agents/planner.py` and `agents/metaviewer_agents/wender_bridge.py` call the pinned private components over process interfaces, and `agents/metaviewer_agents/perception/vcore.py` drives vision-engine while `agents/metaviewer_agents/vision.py` validates the perception boundary. Source checkouts are selected through `VQA_VE_ROOT` and `MV_WENDER_ROOT`, but lock verification is caller-specific: `VCorePerceptionAdapter` defaults to `verify_lock=False`, while the Wender verification entry enables it. Source-free distributions are selected through `MV_VISION_EXECUTABLE` and `MV_WENDER_EXECUTABLE` and verified by `agents/metaviewer_agents/perception/compiled.py` on Linux x64 only; do not infer pin enforcement from root selection alone. The client proposes and observes; it does not admit its own proposals. Read [Agent entry](AGENT_ENTRY.md) and [AI operator guide](AI_OPERATOR_GUIDE.md).

## Contracts

The contract set is the versioned JSON Schema tree under `contracts/`: `contracts/v0.1/`, `contracts/v0.2/` and `contracts/v0.3/`, with valid and invalid examples under `contracts/examples/`. Law L3 makes a schema and its examples precede producer or consumer code; a breaking change opens a new version directory, and old versions stay until no pin uses them. `tools/check_contracts.py` is the checker. Read [contracts v0.2](../contracts/v0.2/README.md) and [LAWS.md](../LAWS.md).

## Tools and gates

The verification and generation layer is `tools/` plus `scripts/`. `tools/gate.sh` is the root aggregate with its eleven legs; `tools/vp_gate.py` adds the component suites and runs every step in a memory-capped scope; `tools/check_laws.py` and `tools/check_contracts.py` enforce the law and schema checks; the `tools/generate_*.py` utilities derive guest-command, rewind and headless bindings; `scripts/` holds the install and judge entry points. These tools consume the components; they do not become a second runtime. Read [Testing](TESTING.md) and [monorepo gates](MONOREPO_GATES.md).

## Retrieval

The retrieval layer has three trees with different jobs. `route/` builds the current document and symbol index and serves `route/route_query.py`; `router/` carries the pinned router-kit bootstrap and its gate; `catalogue/` is the legacy machine-readable catalogue record (`catalogue/protocol.json`, `catalogue/routes.tsv`, `catalogue/nodes.jsonl`). The route index is a finding aid, not proof of behavior: read the cited bytes before relying on a hit. Read [route/README.md](../route/README.md) and [AGENTS.md](../AGENTS.md).

## Vendored components

Three imported trees under `components/` are separate programs, not MetaViewer source; their source is published here under `AGPL-3.0-only`. `deps.lock` records the version and full commit for each. The vendored digest in `deps.lock` identifies the exact shipped snapshot, including local integration changes. Their internal algorithms, containment and judge results are outside this repository's public scope; the links below are the in-tree public descriptions.

### vision-engine

`vision-engine` is a deterministic, integer-only visual QA engine: an adapter input flows through a frame manifest, a tile grid, descriptors, change classes, regions, a temporal GOP, a token-bounded judge pack and visual memory. Its public seam is the `vcore` and `vmem` command-line interface and the `vadapt` server; in source mode the agent perception adapter passes RAM-only frames and reads closed JSON replies. Adapters can instead run a source-free distribution selected by `MV_VISION_EXECUTABLE`, whose lock, commit, files and hashes `agents/metaviewer_agents/perception/compiled.py` verifies before launch; that implementation is Linux x64 only. It is pinned as `python_package` `v0.2.1` at commit `00abe6eba802d050acf217c80f2b890bd2b7f594`. The shipped tree reports package version 1.1.1 and incorporates the public render-input fixes and regression fixtures. Read [components/vision-engine/README.md](../components/vision-engine/README.md) and [AGENTS.md](../components/vision-engine/AGENTS.md).

### wender-agent

`wender-agent` is the hard layer of computer use: perceive from the cheapest available rung (structured state, AX tree, pixels through vision-engine, crops, blind coordinates), act by the most structured path first with one actor per surface, remember a program map and verified skills, and learn from demonstrations. MetaViewer's producer path uses the pipe-only planner entry (`python -m wender.plan.cli`) in source mode, or the `plan` entry of a verified compiled distribution selected by `MV_WENDER_EXECUTABLE`; callers that request verification resolve the source checkout against `deps.lock` first. It is pinned as `python_package` `v0.3.2` at commit `5a1579047fbaf788da7c011d15ef2c6dcdd8a999`. Read [components/wender-agent/README.md](../components/wender-agent/README.md) and [AGENTS.md](../components/wender-agent/AGENTS.md).

### OpenSuperficies

`OpenSuperficies` is an autonomous Docker, Xpra and CDP substrate for graphical surfaces: each web is data and the engine is invariant, with Xvfb, a window manager, streaming and the browser inside one capsule bounded by loopback ports. The adapter invokes its `srf-*` binaries from `MV_SRF_BIN_DIR` and receives bounded JSON; the surface lifecycle is a command-line contract, not an in-process library. It is pinned as `go_binary` `os-cu-w9-mv1` at commit `3cb86d752f38457b6730eefd235d4da4c4039721`. Read [components/OpenSuperficies/README.md](../components/OpenSuperficies/README.md) and [QUICKSTART.md](../components/OpenSuperficies/QUICKSTART.md).

## Blender Visual QA

[Blender Visual QA](../components/blender-visual-qa/README.md) provides profile checks and recorded replay for 3D scenes. The [3D Visual QA guide](VISUAL_QA_3D.md) describes the component boundary, shipped fixtures, and verification required for runtime claims. Its [Vision Engine wheel](../NOTICE-EVALUATION.md#blender-visual-qa-and-its-pinned-vision-engine-wheel) has its own source pin.

## Public feature and candidate status

| Feature | Public status and evidence |
| --- | --- |
| REA | The accepted `rea.slot.project` control operation lives under [vp-direct control](../components/vp-direct/control/core/). `components/video-rea` is present in public main. Its [wide-fixture tests](../components/video-rea/demo/synth_game/tests/test_wide.py) and [fixture qualification script](../components/video-rea/demo/synth_game/tests/qualify_wide.py) cover the [synthetic wide fixtures](../changes/unreleased/video-rea-wide-fixtures.md), not live operation. |
| Blender playback and full 3D Visual QA | The [README](../README.md) documents published Blender playback, which is recorded, not live qualification. The imported Blender Visual QA component ships profile checks and recorded replay. The separate full 3D Visual QA feature remains a candidate on `feat/visual-qa-3d`. The vendored [`vision-engine`](#vision-engine) is a separate visual QA program, not the full 3D feature. |
| ViewPoint hackathon swarm | `hackathon/swarm/` is a public reference implementation documented in the [swarm contract](../hackathon/swarm/CONTRACT.md). The [README](../README.md) describes recorded swarm replay, not a live run. |

## Supporting trees

`tests/` holds the root unit, integration, end-to-end, performance and supervisor suites; `examples/computer-use/` holds the runnable computer-use examples; `docs/` is this documentation set; `changes/unreleased/` holds changelog fragments; `memoryweb-builtin/` holds the MemoryWeb site and workflow schemas plus a sample site. None of these is a runtime component: they are inputs to the gate, the documentation set or the release. Read [Testing](TESTING.md) and [changes/README.md](../changes/README.md).
