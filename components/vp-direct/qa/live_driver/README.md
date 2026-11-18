# Private LIVE qualification driver

Run these commands from the repository root. QD refuses any inherited `DISPLAY`, including `:0`. Use `--display :N` to request a particular free local display at or above `:90` [TARGET]. The launcher checks the host lock and Unix socket inventory without connecting to the requested endpoint.

The driver uses Xvfb, Openbox, the google-chrome-stable DEB, and a fixture-only network namespace. The namespace contains loopback and private X11, D-Bus and runtime directories. Xpra profiles start a local Xpra shadow on the owned Xvfb. AT-SPI profiles create a private session bus and check the accessibility launcher PID before passing its address to Chrome. Every browser uses a fresh temporary profile and a witnessed loopback CDP endpoint. Frames stay in RAM. On exit the driver waits for owned children, the X11 lock and socket, and a clean scope.

```sh
qd() {
    env -u DISPLAY systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
        python3 -IB components/vp-direct/qa/live_driver/driver.py "$@"
}
qd_delegated() {
    env -u DISPLAY systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p Delegate=yes -- \
        python3 -IB components/vp-direct/qa/live_driver/driver.py "$@"
}
```

The memory ceilings are [TARGET]. The delegated runs put the browser, X server and fixture helpers in a different child cgroup from the inventoried observer, with the memory ceilings on their common scope.

## Commands for provisioned private entry points

Paths passed to QD are relative to `components/vp-direct/`. The launcher executes the existing script with its original arguments. Only the timer smoke has a GUI qualification receipt in this cell. The commands below are **NOT-DONE** as live checks.

```sh
qd privacy/tests/LIVE_login_fixture.py
qd frames/tests/LIVE_hidden_fixture.py
qd channel/cdp/tests/LIVE_s2_fixture_click.py
qd --backend cdp channel/core/LIVE_channel.py
qd --backend atspi channel/core/LIVE_channel.py
qd channel/atspi/live_check.py
qd attach/live_check.py
qd perception/stream/tests/LIVE_fixture_cycles.py
qd perception/demand/tests/LIVE_recording_sleep.py
qd perception/pool/tests/LIVE_pool_close.py
qd_delegated perception/lifecycle/tests/LIVE_chrome_cycle.py
qd_delegated perception/receipts/tests/LIVE_transition_receipts.py
qd_delegated vision/tests/LIVE_vision_fixture.py
```

For R2, the normal command requires system Tesseract with English trained data. A provisioned Tesseract package prefix can also supply the executable and language file. The C2 and Wender provider dependency locks still apply.

```sh
qd_delegated --ocr-prefix "$QD_OCR_PREFIX" vision/tests/LIVE_vision_fixture.py
```

For A3 and S3, set `QD_SRF_BIN_DIR` to the directory containing the provisioned `srf-host-window` executable. QD uses explicit private display and Xauthority arguments on that executable. Native S3 needs a genuine window witness from the helper. The helper path for this run is **AUSENTE**.

```sh
qd --srf-bin-dir "$QD_SRF_BIN_DIR" focus/tests/LIVE_focus_trace.py
qd --srf-bin-dir "$QD_SRF_BIN_DIR" window/tests/LIVE_private_window.py
```

The A1 command exercises attach and detach against an already-open synthetic window with fresh CDP and AT-SPI evidence. It leaves the window alive through detach and tears it down with the rest of the fixture. A1 operator-desktop qualification is **NOT-DONE**.

## Self-provisioning acceptance workloads

```sh
qd_accept() {
    env -u DISPLAY systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p Delegate=yes -- \
        taskset -c 16-19 python3 -IB components/vp-direct/qa/live_driver/driver.py "$@"
}
```

| Entry point | Supported workload | Launch |
|---|---|---|
| `accept/t1/LIVE_t1.py` | Owned Chrome timer fixtures, one Xvfb per admitted surface. The capacity profile keeps every requested surface in the manifest. | `qd_accept accept/t1/LIVE_t1.py --apps 16 --arm baseline --order-index 0 --warmups 1 --receipt-dir ./t1-baseline-first` |
| `accept/t2/LIVE_t2.py` | Per-app Chrome timer, CDP observe with semantic readback, and a separately witnessed synthetic login fixture. A1 uses both CDP and AT-SPI attachment evidence. | `qd_accept accept/t2/LIVE_t2.py --apps 2 --arm baseline --warmups 1 --wake-samples 1 --controls ./t2-controls.json --receipt-dir ./t2-baseline-first` |

For the T1 capacity profile, run the T1 command with `--apps 32` and a fresh receipt directory [TARGET]. It provisions the same resident cohort of 16 Chrome windows at 800×600 [TARGET] and leaves the excess requests for P6 to decide. Use `--controls` with the T1 acceptance pair plan to collect ABBA evidence. T2 full runs require operator-declared `competing_cgroups`, `throttle_counters`, and `pair_tolerance` controls as described by the T2 harness. Missing thermal or throttle evidence leaves the arm unavailable. T2 CDP protected captures use the full 1920×1080 viewport [TARGET]. Inventory reads the local Docker engine, running guest hypervisors and display servers through a read-only host adapter. A failed Docker query makes the inventory unavailable.

Both commands generate the runtime manifest and plan in the disposable work root and export data files to a fresh `--receipt-dir` when supplied. The captures carry the workload pins. Harness telemetry samples the runner cgroup. The driver also reports the aggregate fixture RSS, which counts shared pages in each process that maps them, and the total cgroup charge under the shared memory cap. The driver reaps its fixtures before removing the work root.

```sh
qd_accept accept/t1/LIVE_t1.py --smoke-workload --apps 2
qd_accept accept/t1/LIVE_t1.py --smoke-workload --apps 32
qd_accept accept/t2/LIVE_t2.py --smoke-workload --apps 2
```

The smoke flag exercises the real producer hooks and emits `SMOKE` receipts. The stock qualification runners keep their prescribed schedules. Full T1 and T2 qualification is **NOT-DONE**.

## Driver hooks with independent prerequisites

| Entry point | Hooks | QD disposition |
|---|---|---|
| `recorder/timeline/tests/LIVE_record_fixture.py` | `bind`, `record_demo` | **NOT-DONE**. Approved H1 DemoEvent source and verified RAM capture feed are **AUSENTE**. |
| `recorder/compiler/tests/LIVE_record_compile_replay.py` | `bind`, `record_demo`, `review`, `replay` | **NOT-DONE**. H1, admitted recording review, ReplayOwner and verified workflow transport are **AUSENTE**. |
| `loop/tests/LIVE_selector_repair.py` | `provision`, `break_selector`, `dispatches`, `broker_events` | **NOT-DONE**. Admitted H1 goal, independent transport and broker are **AUSENTE**. |

## Other LIVE files

These entry points do not consume the QD fixture driver. The right column gives the source contract that controls launch.

| Entry point | Launch contract |
|---|---|
| `admission/tests/LIVE_surface_ceiling.py` | Standalone synthetic admission probe. |
| `apps/tests/LIVE_oauth_observe.py` | Operator-run OAuth and provisioned profile and capsule. |
| `bus/broker/LIVE_handoff.py` | Standalone broker scenario with an isolated runtime directory. |
| `bus/push/LIVE_wall.py` | Synthetic broker events, fresh room and initialized wall pipes. |
| `control/adapters_core/tests/LIVE_core_readback.py` | `run` needs the provisioned Core dispatcher, identity and binding context. |
| `control/core/tests/LIVE_control_parity.py` | Normalized request and captured lane replies. |
| `data/abi/tests/LIVE_file_fallback.py` | Pinned artifact location, digest, length, media type and stage. |
| `data/frames/tests/LIVE_file_sequence.py` | Produced render and video frames with admitted file storage. |
| `data/locator/tests/LIVE_night_glb.py` | Pinned GLB artifact and independent source record. |
| `data/residency/tests/LIVE_gpu_residency.py` | Qualified GPU wiring, model digest and holders. |
| `expose/cli/tests/LIVE_cli_parity.py` | Built CLI and actual MCP and mailbox receipts. |
| `expose/mcp/tests/LIVE_mcp_registration.py` | Pinned registry and actual MCP server command. |
| `factory/manifest/tests/LIVE_factory_manifest.py` | Production factory manifest. |
| `forge/tests/LIVE_verify_forge.py` | Actual forge receipt and lineage journal. |
| `guest/custody/tests/LIVE_managed_custody.py` | Provisioned guest registry, reservation and workload. |
| `guest/demand/tests/LIVE_guest_drain.py` | Genuine guest-demand provider. |
| `guest/imagepack/tests/LIVE_validate_image_pack.py` | Trusted and candidate guest image packs. |
| `human/tests/LIVE_handoff.py` | `run` requires the operator's S4 exchange, desktop and human turn. |
| `oflane/tests/LIVE_mailbox_summon_screenshot.py` | `run` requires the C4 mailbox, verifier key and trusted binding. |
| `rewind/blender_rw1/tests/LIVE_checkpoint_restore.py` | Provisioned Blender checkpoint fixture and restore backend. |
| `rewind/machine/tests/LIVE_goals.py` | Capture root and goal database. |
| `rewind/microvm/tests/LIVE_microvm_restore.py` | Provisioned guest/microVM restore lane. |
| `router/tests/LIVE_window_summon.py` | Verified router index, registry and native window summon binding. |
| `session/launch/tests/LIVE_chrome_profile_restart.py` | Operator-authorized profile restart and operator interaction. |
| `session/registry/LIVE_session.py` | Host-X11 launcher with a dedicated operator-provisioned capsule. Its site configuration pins `:0`. |
| `vqa3d_bridge/tests/LIVE_score_checkpoints.py` | Pinned checkpoint inputs and VQA3D score binding. |
| `window/live_check.py` | Operator workspace/S3 host display; use `window/tests/LIVE_private_window.py` for the private arm. |
| `xpra/tests/LIVE_firefox_xpra.py` | `run` requires the provisioned H1 Firefox Xpra exchange and operator turn. |

## QD-only verification

```sh
components/vp-direct/qa/live_driver/tests/safe_run.sh

env -u DISPLAY systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
    taskset -c 16-19 python3 -IB components/vp-direct/qa/live_driver/driver.py smoke
```

See [SMOKE.md](SMOKE.md) for the timer receipt.
