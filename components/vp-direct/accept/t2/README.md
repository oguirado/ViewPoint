# A5 · Host attach acceptance

A5 replays paired runs against the T2 targets. Each declared app, channel and operation combination needs 100 attempt positions [TARGET] at 1920×1080 [TARGET] in each arm. Semantic readback must verify at least 95% [TARGET]. Failures and refusals count toward the denominator, and every attempt contributes its proposal-to-receipt duration. The candidate limits are p95 of 2,000 ms [TARGET] from proposal to receipt, 500 ms [TARGET] for protected observation, and 1,000/3,000 ms [TARGET] for asleep/off wake. Percentiles use nearest rank.

Each combination requires a complete sleep receipt and 60 s [TARGET] of unchanged counters for agent acquisition, analysis, OCR, inference and decisions after the acknowledgement. Wake evidence binds the initial perception state to the attached session and requires a fresh receipt observation. A continuous A3 trace covers fixture verification, attach, operations, rest, wake and detach. The synthetic login probe must yield `HUMAN_NEEDED` with zero acquisition and publication callbacks [TARGET]. Compare complete, stable-identity inventories before and after each attach for zero container, guest or private display server deltas [TARGET]. These snapshots detect replacements and persistent changes. An entity created and removed inside the attach interval requires an independent creation monitor to detect.

The sampling window is 10 min [TARGET] at 100 ms [TARGET] per slot. Every slot must be present. Capture monotonic boundaries, load, host and scope PSI, cgroup peak/current bytes, host `MemAvailable`, thermal maximum, configured throttle counters and named competing cgroups. Collect baseline, candidate, candidate, baseline on the same boot with the same fixture pins and CPU placement. Predeclare pair tolerances for peak load, temperature and competing cgroup CPU and memory. Throttle increments, missing evidence or values beyond tolerance make the pair unavailable. Each candidate must retain at least 90% [TARGET] of its paired baseline's minimum `MemAvailable`. Report queue, wake, service and end-to-end latency separately. Scope charges come from the cgroup. Kernel UVM watermarks and cache attribution remain AUSENTE when the collector has no source.

## Offline checks

```sh
components/vp-direct/accept/t2/tests/safe_run.sh
```

The wrapper pins the benchmark process and its threads to CPUs 16–19 [TARGET] and applies `MemoryHigh=6G` and `MemoryMax=8G` [TARGET]. Tests use synthetic evidence and cannot make a profile available.

The offline ABBA receipt is `calibration.json`. Each arm validates the same frozen synthetic focus trace and samples host telemetry once. Reproduce the receipt with `components/vp-direct/accept/t2/tests/safe_run.sh calibrate`. Profile qualification requires the live evidence below.

## LIVE operator run

Use an already provisioned synthetic fixture on a private Xvfb. The trusted operator adapter must bind the fixture profile, the app PID and window witness, and the X server PID to the selected display. The runner verifies the frozen session and surface binding and reuses A3's peer-checked native focus source.

```sh
components/vp-direct/accept/t2/tests/safe_run.sh --live \
  --arm baseline --plan PLAN.json --driver FIXTURE_DRIVER.py --receipt BASELINE_FIRST.json

components/vp-direct/accept/t2/tests/safe_run.sh --score \
  PLAN.json BASELINE_FIRST.json CANDIDATE_FIRST.json CANDIDATE_SECOND.json BASELINE_SECOND.json
```

Collect each candidate and the last baseline in ABBA order, supplying the corresponding arm and a fresh output path. Use the committed baseline or candidate for each arm, with identical app state and fixture inputs. Record the declared worktree commit with each raw receipt. The replay command includes raw receipt paths and digests. A profile stays unavailable without a qualified paired run.

The benchmark plan is a local measurement input. Its `workload_sha256` pins the trusted adapter file. `fixtures` maps paths relative to the plan directory to SHA-256 digests. `warmups` and `wake_samples` declare the per-combination warm-ups and per-state wake samples. `combinations` maps app/channel/operation labels to `profile`, `backend`, `op` and an independently expected `readback_sha256`. The eligible profile is `private-xvfb` at 1920×1080 [TARGET]. Host composition (B2-10) and Wayland, occluded or minimized profiles (B2-11) remain unavailable. `competing_cgroups` lists the separately observed cgroup-v2 coordinates. `throttle_counters` lists documented monotonically increasing integer counters in sysfs. `pair_tolerance` declares `load`, `temperature_millic`, `competing_memory_bytes` and `competing_cpu_usec`.

The trusted adapter exposes `prepare(plan)`, returning the following ports. Use existing A1, A2, S5, P4 and P7 providers to supply evidence.

- `target(combo)` returns the private display, Xvfb PID, frozen `vp.session/v1` and `vp.surface_binding/v1`. `verify_fixture(combo, session, binding)` verifies actual scoped profile, app PID and window witness and returns true while the evidence holds.
- `inventory()` returns complete stable identifiers for `containers`, `guests` and `private_display_servers`. `attach(combo)` and `detach(combo, session_id)` return the frozen workspace operation results.
- `privacy_ports(combo, login=...)` supplies an A2 `PrivacyBinding`, scoped metadata reader, acquisition callback, gap sink and handoff sink. The login arm selects a synthetic login state and uses fixture sinks. For normal operation the acquisition callback returns the sanitized S5 descriptor covered by the metadata witness.
- `warmup(combo)` exercises the fixture synchronously and acknowledges success. `perform(combo, index, frame, marks)` returns the actual frozen request/reply pair. Call `marks.admitted()` when accepted and `marks.first_byte()` at first result arrival.
- `rest(combo, duration_ns)` returns the P4/P7 sleep receipt with `ack_mono_ns`, `end_mono_ns` and agent-scoped monotonic `before`/`after` counter snapshots. `prepare_wake(combo, state)` returns the frozen prior perception state. `wake(combo, state, marks)` returns the actual P4/P7 wake receipt, with the first-byte marker at the fresh observation.

Benchmark timestamps, workload identity, host-resource snapshots, agent counter deltas and adapter evidence coordinates live in local raw measurement records. The frozen receipts carry their original fields. Provider-level attestations covering these observations across processes are AUSENTE.

## QT2b measurement-only sampler

For a new QT2b private-driver collection, prepend `components/vp-direct/accept/t2/qt2b/bin` to the driver's `PATH`. Supply `QT2B_DEADLINE_EPOCH` for the new window and `QT2B_FIRST_TIMER` for the first timer. This selects the corrected measurement entry through the existing bubblewrap launcher. The files in `qt2-evidence/` reproduce the historical runs.

The sampler starts in `qd-runner.scope` to identify the cgroup it measures, then moves itself to the sibling `qd-control` before reporting ready. The parent verifies the child's live membership before releasing the sampling window or starting any combination. `scope` identifies the measured runner. `sampler_placement` records the observer's cgroup, CPU affinity and delegated parent memory limits. The measurement-only arm retains its `UNAVAILABLE` paired verdict.
