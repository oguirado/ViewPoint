# P8 T1 acceptance harness

T1 requests a resident `ACTIVE:ASLEEP:OFF` mix of `4:6:6` across `16` surfaces [TARGET]. Its capacity challenge requests `4:12:16` across `32` surfaces [TARGET] against the existing ceiling of `16` [CITED: `admission/gate.py`, `daemon-adapter/internal/daemon/environment.go`, `ui/electron/profilePicker.js`].

Each scenario has `100` planned wake attempts over `600 s` [TARGET]. The plan alternates asleep and off and rotates through every requested surface in each stratum. The capacity challenge must record the ordered admission attempt for every requested surface. It expects `16` admissions and `16` capacity refusals [TARGET]. The requests made beyond capacity need sufficient memory margin and a qualified accounting path to attribute the refusal to the ceiling. Planned wake attempts for those surfaces retain the original refusal and perform no wake.

Both scenarios require at least `95%` verified, asleep wake p50/p95 at most `250/1000 ms`, off wake p50/p95 at most `1000/3000 ms`, and queue p95 at most `500 ms` [TARGET]. The admitted scenario uses every planned attempt in `cycle_denominator`. The capacity scenario uses only attempts scheduled on initially admitted surfaces. A failed or refused wake on one of those surfaces still counts toward the denominator. An empty eligible set produces a null rate and fails the verified gate. The scorer checks and reports all planned cycles in `outcomes` for both scenarios, including the capacity refusals.

The capacity report includes `planned_cycles`, `verified_gate`, `capacity_refusal_gate`, and `capacity_refusal_denominator`. The refusal gate accounts for every ordered admission request and requires the expected admissions and capacity refusals. A memory refusal fails that gate. Replay returns `EXPECTED_CAPACITY_REFUSAL` when the refusal gate and performance gates pass.

Queue latency uses admitted attempts with both markers. Wake latency uses verified observations and stays stratified. End-to-end latency covers every attempt through terminal disposition. Percentiles use nearest rank on unrounded nanoseconds.

## Replay and offline tests

From the repository root:

```sh
components/vp-direct/accept/t1/tests/safe_run.sh
python3 -I -B components/vp-direct/accept/t1/score.py /path/to/raw-capture.json
```

The replay result includes the raw capture path and SHA-256. The exit status is `0` for `PASS` or `EXPECTED_CAPACITY_REFUSAL`, `1` for a failed gate with valid evidence, and `2` for invalid or incomplete evidence [TARGET]. The test wrapper uses the designated CPU affinity and memory scope for offline tests and calibration. The calibration receipt at `tests/calibration.json` uses the integrated P4/P7 synthetic framebuffer and P6 with synthetic memory inputs. It measures clock boundaries using identical code in each ABBA position.

The [T1-GATE replay receipt](qt1-evidence/t1-gate-replay.json) records the rescored captures alongside their original [QT1b verdicts](qt1-evidence/execution.json).

SHA-256 of `tests/calibration.json` is `7367b946be6c6a67463393d9329c0b85a0dd19f9faf6eec760012ee682faa0d6` [MEASURED].

For paired replay, supply separate captures of the same scenario in ABBA order.

```sh
python3 -I -B components/vp-direct/accept/t1/score.py --pair-plan PAIR.json \
  BASELINE_FIRST.json CANDIDATE_FIRST.json CANDIDATE_SECOND.json BASELINE_SECOND.json
```

Declare `pair_tolerance` for `load`, `temperature_millic`, `competing_memory_bytes` and `competing_cpu_usec` before collection. List the cgroup and sysfs counter coordinates in `competing_cgroups` and `throttle_counters`. Pass the same `--pair-plan PAIR.json` to every live invocation so each capture pins the plan. Paired replay requires stable commits within each arm, one boot, matching inputs and scope placement, completed warm-ups outside the scored window, and complete telemetry before, during and after the run. Each arm retains its own scenario outcome and the replay includes each raw capture's digest.

Paired captures use the T2 arm wrapper for provenance, placement, timing and host samples. The `scenario` field carries the T1 cycle plan and frozen receipts.

## LIVE private fixture

`LIVE_t1.py` uses the existing Chrome/Xvfb timer fixture and P4/P7 capture ports. Supply a local manifest for one scenario. Its `ring_bytes` must match the P3 fixture ring geometry. `fixture_cgroups` lists the distinct cgroup paths covering the pre-existing private fixture processes, outside the runner scope. Each entry of `surfaces` provides a unique `surface_id` and its requested `state`. Each entry that can be admitted also supplies:

- `session`, `binding` and `agent_demand`, paths to the frozen `vp.session/v1`, `vp.surface_binding/v1` and `vp.demand/v1` records.
- `private_fixture`, with `browser_pid`, `xvfb_pid`, `fixture_port`, `max_frame_bytes` and an absolute, dedicated `synthetic_profile` path.

The P6 reservation covers fixture allocation and accounting. P7 verifies the evidence supplied by P4's public transition hook. Each verified wake includes fresh RAM frame readback. The capture carries the frozen admission, transition and broker-state records alongside local benchmark metadata. It stores no pixels. It checks the resident state and identity at the scenario boundaries, including for the open OFF cohort. The ACTIVE cohort starts with verified frame readback and remains resident during the run.

```sh
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
  taskset -c 16-19 python3 -I -B components/vp-direct/accept/t1/LIVE_t1.py \
  /path/to/private-fixture-manifest.json /path/to/capture.json \
  --arm baseline --order-index 0 --warmups 1
```

Use a fresh output path per run. For a performance comparison, collect separate captures in baseline, candidate, candidate, baseline order with the same workload, fixture and warm-up count. Use `t_submit` before P6, `t_admitted` at the reserved admission, `t_first_byte` at the first validated fresh publication, and `t_complete` after readback. The raw capture includes before/after host load and PSI, sampled temperature, scope peak, CPU affinity and cgroup cpuset, plus counts and per-attempt evidence. Null throttle counters or UVM watermarks mean AUSENTE. Retain the raw captures for both arms and reject performance comparisons with invalid clock/scope evidence or materially different thermal or competing load conditions.

## Evidence limits

`vp.transition_receipt/v1` begins completed wakes at `WAKING` and supplies no benchmark timing fields. Replay uses the frozen `vp.perception_state/v1` broker boundaries for the prior stratum and separate monotonic instrumentation for timing. P6 reports an admission decision and the prospective count. It has no refusal-reason field, so the capacity contrast also requires adequate memory margin and GPU qualification where applicable. The T1 capture contains verified P7 transition receipts. The P7 lineage runner records publication separately. Full W-B4 execution, the production semantic workload, and operator-desktop qualification are pending.
