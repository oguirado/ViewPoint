# JQ3 R7 C3 — M3 multi-describe deterministic fixture repair

Task: `jq3-r-r7-c3-fix-vadapt-multi-describe-det-a86534`, attempt `a1`, role `builder`. The change is confined to owned **tests and evidence**. `vadapt/multi_describe.py` is protected and unmodified. No validation is relaxed to accommodate old fixtures.

## Source binding and defect

Read `bridge/integration` from GitHub at observed commit `5ac4fa8b9e924c4cfa6f440bf481a667621a0d3b` (the task card records earlier dispatch commit `6ed757d33f6a2674e58c92c48642cc0832496d14`; it is **not** the ref head observed in this session). SHA-256 of **pre-edit UTF-8 source text** on this ref:

| Path | SHA-256 |
|---|---|
| `vadapt/multi_describe.py` | `9737d4b5404fa9a819332a56e14a116abaaa37eaa992e1776e27f30f24066219` |
| `tests/test_jq2a_m3_c1_multi_describe_implementation.py` | `564123d3ab9590d5271c96e9f2fb62dd7c6405560a76cc5e2dcdb662ec05e9c7` |
| `tests/test_jq2a_m3_j5_vadapt_multi_describe_determinism.py` | `1ad50b6af037a5edd73bc0d77132a325f654d2436224113535bdbb805935f1fb` |
| `tests/test_jq2a_m3_j6_vadapt_multi_describe_batch_and_validation.py` | `b0baf111b7ef3059f2b5715e5c9cedf33a536f1ebb1625d5c53df1fedff28d24` |

The production contract uses frozen `Handle(kind, canonical_id, generation)`, `WindowFrames(handle, epoch, frames, frame_seqs, dpr_ppm)` and `WindowDescription(handle, epoch, frame_seqs, descriptors, regions, error)`; structural frame references are **not** a locally defined canonical `FrameRef`. `describe_windows` validates per target and returns error receipts for invalid DPR, RGB dtype or stride, rather than raising `ValueError`. Runners receive `_Batch` jobs and return bare `(batch.index, ((entry.index, WindowDescription), ...))` results.

The stale J5 runner executes in reverse but re-sorts before returning, so the test cannot observe completion-order handling. The old J6 fake runners double-tag results with `enumerate` and treat a `_Batch` as a `WindowFrames`; owner conformance tests wrongly demand exceptions or original exception message text. These are fixture defects, not permission to weaken identity, dtype, frame-shape, contiguous RGB, geometry-epoch, or chronological validation.

## Pre-repair inventory and outcomes

Qualified original method IDs below are an inventory from the above pre-edit files. There were **no class-level or inherited expected-failure decorators** in these classes. Five J5 methods carry `@owner_gate`; four J6 owner methods carry `@owner_required`, both conditional on module presence; C1 methods are undecorated. Source digests are recorded above.

**C1 — `tests.test_jq2a_m3_c1_multi_describe_implementation.TestMultiDescribe`** (not edited; original assertion suite preserved):
- `test_public_canonical_frozen_field_order`
- `test_m3_1_reversed_completions_keep_identity_and_serial_bytes`
- `test_m3_5_same_dimensions_never_share_unrelated_predecessor`
- `test_m3_5_generation_and_geometry_epoch_isolation`
- `test_m3_5_same_stream_slices_share_only_own_predecessor`
- `test_m3_1_invalid_inputs_are_isolated_before_runner_admission`
- `test_frame_ref_structural_compatibility_and_guards`
- `test_m3_1_own_readonly_snapshot_before_injected_runner_executes`
- `test_runner_death_retains_completed_without_dispatch_retries`
- `test_core_failure_isolated_by_target`
- `test_geometry_conflict_sequence_overlap_and_bad_workers_refused`
- `test_zero_disk_activity_when_out_dir_none`
- `test_worker_count_parity_and_repeatable_bytes`

**J5 — `tests.test_jq2a_m3_j5_vadapt_multi_describe_determinism.TestVadaptMultiDescribeDeterminism`** (all five replayed, because shared `setUp` and runner change):
- `test_gate_g_m3_5_independent_predecessor_histories_match_serial` [`@owner_gate`]
- `test_negative_mutant_unrelated_frames_grouped_as_temporal_sequence_fails_isolation` [`@owner_gate`]
- `test_inv_m3_1_validation_non_contiguous_or_non_uint8_rgb_and_positive_dpr` [`@owner_gate`]
- `test_ram_input_ownership_read_only_and_offline_io_safety` [`@owner_gate`]
- `test_canonical_dataclass_field_order_and_structures`

**J6 — `tests.test_jq2a_m3_j6_vadapt_multi_describe_batch_and_validation.TestJq2aM3J6VadaptMultiDescribeBatchAndValidation`** (all seven replayed, because shared `setUp`, constructors, and runner change):
- `test_harness_positive_control_preserves_submit_identity_and_serial_bytes`
- `test_harness_negative_mutant_completion_order_detected`
- `test_harness_negative_mutant_accepts_non_positive_dpr_detected`
- `test_owner_multi_describe_rejects_non_positive_dpr_before_dispatch` [`@owner_required`]
- `test_owner_multi_describe_rejects_non_contiguous_or_non_uint8_rgb` [`@owner_required`]
- `test_owner_multi_describe_reversed_fake_runner_preserves_submit_identity` [`@owner_required`]
- `test_owner_multi_describe_isolates_per_item_failures_without_retry` [`@owner_required`]

**Pre-edit measured outcomes:** AUSENTE for the full C1/J5/J6 original-method runs: these exact pre-edit suites were not executed in this session against the complete repository VM. Source inspection independently identifies the invalid-input `assertRaises(ValueError)` stale pins (J5/J6), J5's vacuous reverse completion, J6's double-tagged `_Batch` results, and J6's reliance on a propagated runner exception message. These are *source-derived defect observations, not invented pre-edit test results*.

## Repair, owned regression, and named contrasts

1. **J5:** import genuine production frozen records; retain independent target pixels/history and existing serial assertions; treat frame references as structural; return actually reversed, bare tagged `_Batch` completions; assert reversal occurred. Assert typed per-target receipts and zero runner calls for bad stride, dtype and non-positive DPR.
2. **J6:** replace local public record copies with production records; correct C-contiguous requirement; send properly tagged harness jobs into fake runner and let production runner see unwrapped `_Batch` results; retain positive/negative harness assertions; assert typed refusal receipts and no dispatch; detect runner death by `E_M3_RUNNER_DIED: RuntimeError` (exception **type** only).
3. **New G-R7-8 gate:** reproduces pre-repair nested-completion protocol rejection before demonstrating the corrected runner; checks real frozen field order and structural frame identity, digest, height/width and private immutable RGB ownership. Exercises 18 seeded RGB/DPR cases (`replay_seed=0xA86534`) with smallest failing counterexample in the diagnostic message. It inventories all 25 original method names and invokes **every** edited J5/J6 original method by its exact `unittest.TestLoader` ID, requiring no failures, errors, skips, expected failures or unexpected successes. C1 remains read-only and inventoried.
4. **M-vadapt-multi-describe-1:** locate the unique `_run_batch` `MemoryFrameSource` manifest key `channels: 3` and mutate it to `4`. Run identical `channels == 3` boundary assertions on the real implementation and an isolated private-module mutant; only a mutant-side `AssertionError` is accepted as a behavioral kill.
5. **M-vadapt-multi-describe-2:** uniquely find `_identity`'s structural `or` guard and remove only the `kind not in _SUPPORTED_KINDS` predicate; assert zero submission for unknown kinds on real and mutant implementations, with the mutant's synthetic result using its **own** `WindowDescription` class. Only a mutant assertion failure counts. Mutated source is compiled in RAM, not written; private module, bytecode flag and temporary pools are restored in `finally`.

## Verification and limitations

- **Observed local synthetic VM, after fixture-gate repair:** `python3 -m unittest -v tests.test_jq3_r_r7_c3_fix_vadapt_multi_describe_det_a86534.TestR7FixtureAndMutationGate` — **5 tests, OK**, 0.025 seconds. This environment has a local test double for `visioncore.pipeline`; it does **not** prove sealed-core parity.
- **Production/source readback:** the corrected G-R7-8 gate was committed on the named branch as `477c07945c1da862874e450d34b4c0e555c7fd29`; its local candidate had Git blob SHA `d1053fe738c440c8ad499f5a3566a72ece686a56`. J5 and J6 were committed separately.
- **Full exact gate against the complete current `bridge/integration` checkout:** AUSENTE until executed in a VM containing the genuine source and `visioncore` dependencies; a synthetic-core success must not be represented as this acceptance result. **Host wall-clock, tokens and costs:** AUSENTE. No skipped or expected-failure tests were observed in the five focused synthetic runs.
- **Protected source boundary:** no `vadapt/multi_describe.py` write. No new protected-path defect is conclusively established by this fixture repair; any such producer defect must be escalated to its external owner rather than worked around here.
