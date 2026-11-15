# srf-holdings-capsule — Holdings test capsules for computer use

Conductor tool (os-cu-0). Starts 1..4 desktop capsules (`opensuperficies-desktop-arm64:1.1.0`, network none, 4 GiB,
CPUs 10-14, app folder read-only, `--no-sandbox` only in this test capsule per operator decision D-AL1) running the
Holdings test build, and gives a computer-use client exact frames, XTEST input and agent-lane state.
Every command prints one JSON line; exit codes 0, 2 input, 3 setup, 4 unavailable, 5 capture/verification, 6 indeterminate.

    srf-holdings-capsule start  --slots N --app <readable build dir> --scenario <id> --seed <n>
    srf-holdings-capsule frame  --container <c> [--state]
    srf-holdings-capsule input  --container <c> (--key <keysym> | --click X,Y [--button 1..3])
    srf-holdings-capsule observe --container <c>
    srf-holdings-capsule stop   --container <c>

- `frame`: grab-held `srf-fbread pair`, both legs must agree bit-exactly; canonical RGB24 PNG written to RAM only:
  `/dev/shm/vqa-ramws/opensuperficies/os-cu-0/<run_id>/frames/<slot>/fNNNNNN.png` (STORAGE LAW, U-RUL-0007).
  `capture_mono_ns` = host CLOCK_MONOTONIC (shared with the capsule) right before the capture.
  `generation` is an opaque string (the container name; a new container is a new generation), `seq` the per-run
  counter (starts at 1, monotonic within one generation): key frames by (generation, seq); `sha256_rgb24` is content identity.
- `input`: `input_mono_ns` = in-capsule CLOCK_MONOTONIC right before xdotool injects (includes xdotool start-up).
- `--state` / `observe`: agent_lane/v1 observation (tick, revision, state_sha256, elements, frame_state_sha256).
- The app folder must be readable by uid 10001 (stage a copy with `chmod -R a+rX`).
Measured (w8/holdings_conc/REPORT.md): READY ~0.55-0.9 s; key-to-frame p50 20 ms (N=1), 30-36 ms (N=4).
