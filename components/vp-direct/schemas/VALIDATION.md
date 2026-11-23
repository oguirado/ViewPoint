# W-B0′ schema re-freeze verification

**B0P-SCH PASS.** The capped suite completed with return code 0, 56 passed, 0 failed, 0 errors and 0 skipped [MEASURED]. The previous run had 35 passes and one failure in `test_every_schema_has_a_valid_example` [MEASURED]. The schema-owned forge receipt now passes that gate.

## Reproduction

Run the suite from `.`.

```sh
components/vp-direct/schemas/tests/safe_run.sh
```

The wrapper ran the following capped command.

```sh
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- python3 -I -B -m unittest discover -s components/vp-direct/schemas/tests -p 'test_*.py' -v
```

The exact combined-output digest is in `tests/validation_receipt.json`. The run covered 45 top-level schemas, both valid and invalid examples for each, and 105 named argument/result pairs across 14 adapters [MEASURED]. Each embedded resource has its own positive and negative payload. All 53 tools appear in the inventory [MEASURED]. The selected schema-gap list is empty. Carbon-verified bindings are AUSENTE, and 0 adapter rows authorize dispatch [MEASURED].

## Compatibility

Every previous top-level ID still resolves. Revalidating the W-B0 positive examples found migrations for these contracts [MEASURED].

- `vp.tool_route.reply/v1` now encodes a candidate margin as integer thousandths.
- `vp_capsule_call.args/v1`, `vp_capture_call.args/v1`, `vp_guest_command_call.args/v1`, `vp_headless_call.args/v1`, `vp_identity_call.args/v1`, `vp_saved_data_call.args/v1` and `vp_terminal_call.args/v1` previously accepted selector-only examples for operations that now require inputs. The same per-op rule applies to each updated adapter, including operations whose W-B0 positive example happened to require no inputs.
- Successful adapter replies now have per-tool and per-op result contracts. The B0 support flags have been withdrawn wherever verified backend bindings are absent. All projection choices derived from the consuming units are identified in `SCHEMAS.md`.

## Independent fixture findings

These checks read the companion fixture surface without editing it. A registry may list an excluded model diagnostically. The calibrated index rejects excluded entries. Structural acceptance of a diagnostic record grants no routing rights.

| File | Record | Schema result | Evidence |
|---|---|---|---|
| `../fixtures/replay/forge/forge_receipt_invalid.json` | `forge_receipt_invalid` | REFUSE | 'failed_as_required' was expected |
| `../fixtures/replay/forge/forge_receipt_valid.json` | `forge_receipt_valid` | REFUSE | 'admitted' is not one of ['approved', 'rejected'] |
| `../fixtures/manifests/capability_asset_factory.json` | `TRELLIS.2-4B` | REFUSE | 'AUSENTE' is not one of ['MEASURED', 'CITED', 'TARGET'] |
| `../fixtures/manifests/capability_asset_factory.json` | `UniRig` | REFUSE | 'AUSENTE' is not one of ['MEASURED', 'CITED', 'TARGET'] |
| `../fixtures/manifests/capability_asset_factory.json` | `Hunyuan3D-2.1` | ACCEPT | Wire record validates |
| `../fixtures/manifests/capability_viewpoint_core.json` | `vp_window_summon` | ACCEPT | Wire record validates |
| `../fixtures/manifests/capability_viewpoint_core.json` | `vp_capsule_call` | REFUSE | ops must be the ordered projection of op_table |

The file labelled `forge_receipt_valid.json` skips the required approval edge. In the factory sample, unavailable observations belong outside the measured-facts array. In the core sample, the ordered op-table projection must include exactly the published ops.

## Evidence still outstanding

Independent `carbon-verifier` review is NOT-DONE. Production calibration thresholds, carbon-verified adapter bindings and the producer capability manifest are AUSENTE. Runtime chain verification, calibration replay, model receipt verification and live dispatch remain downstream work. The frozen hashes are recorded in the receipt and checked by `test_frozen_files_keep_initial_sha256`. No live process or profile was sampled.

## Oracles, negative controls and mutants

| Test | Status |
|---|---|
| `test_all_examples_with_cross_field_validation` | PASS |
| `test_correlated_receipts_and_capacity` | PASS |
| `test_critical_args_refuse_ambiguous_targets_and_nonliteral_argv` | PASS |
| `test_each_adapter_round_trips_both_envelopes` | PASS |
| `test_every_adapter_candidate_keeps_its_flat_arguments` | PASS |
| `test_every_adapter_pair_has_examples_and_pinned_resources` | PASS |
| `test_forge_and_router_results_use_control_and_mailbox` | PASS |
| `test_forge_subset_and_utf8_byte_limit` | PASS |
| `test_index_calibration_digest_and_route_details` | PASS |
| `test_model_qualification_and_hashed_measurements` | PASS |
| `test_mutant_accept_uncalibrated_index` | PASS |
| `test_mutant_drop_operator_admission_gate` | PASS |
| `test_mutant_drop_territory_check` | PASS |
| `test_per_op_flags_projection_and_parked_operations` | PASS |
| `test_postconditions_are_closed_and_op_typed` | PASS |
| `test_receipt_admission_gate_and_lineage` | PASS |
| `test_receipt_pins_canonical_forge_fixture_and_steps` | PASS |
| `test_session_guest_pack_and_focus_boundaries` | PASS |
| `test_wildcards_and_policy_without_binding` | PASS |
| `test_wrong_result_or_request_id_is_refused` | PASS |
| `test_mutant_admit_unknown_operation` | PASS |
| `test_mutant_allow_flattened_context` | PASS |
| `test_mutant_drop_error_code` | PASS |
| `test_mutant_drop_frame_witness` | PASS |
| `test_mutant_drop_object_closure` | PASS |
| `test_mutant_drop_polling_only_requirement` | PASS |
| `test_mutant_let_agent_write_human_control` | PASS |
| `test_adapter_policy_preserves_operator_and_unsupported_restrictions` | PASS |
| `test_adapter_selectors_freeze_concrete_ops_only` | PASS |
| `test_all_schemas_are_valid_draft_2020_12` | PASS |
| `test_capability_tool_and_json_op_are_equal` | PASS |
| `test_channel_raw_input_is_ephemeral_not_a_reply_or_timeline_field` | PASS |
| `test_control_context_is_split_and_wrapper_is_refused` | PASS |
| `test_control_unknown_op_and_missing_explicit_dry_run_refused` | PASS |
| `test_every_declared_object_is_closed` | PASS |
| `test_every_schema_has_a_valid_example` | PASS |
| `test_forge_shell_and_python_bodies_are_refused` | PASS |
| `test_frame_needs_witness_and_ax_is_not_raster` | PASS |
| `test_frozen_files_keep_initial_sha256` | PASS |
| `test_human_session_has_no_holder_and_broker_only_records` | PASS |
| `test_ids_unique_and_manifest_complete` | PASS |
| `test_incomplete_forge_receipt_fails_closed` | PASS |
| `test_invalid_examples_are_rejected` | PASS |
| `test_lane_aliases_carry_identical_wire_objects` | PASS |
| `test_live_session_needs_full_binding_pace_and_risk` | PASS |
| `test_no_schema_supplies_a_default` | PASS |
| `test_profile_ids_are_not_paths_and_hashes_have_exact_size` | PASS |
| `test_references_are_local_and_resolvable` | PASS |
| `test_route_abstention_reasons_remain_distinct` | PASS |
| `test_route_match_is_cited_and_has_no_missing_arguments` | PASS |
| `test_selected_schema_gaps_closed` | PASS |
| `test_strategy_error_code_coverage_and_sources` | PASS |
| `test_timeline_requires_polling_tag_and_redaction_pairs` | PASS |
| `test_unknown_keys_rejected_at_every_example_object` | PASS |
| `test_wl0_backend_and_focus_safety_rules` | PASS |
| `test_wl0_cited_statuses_not_run_and_history_are_preserved` | PASS |

## Files written

Paths are relative to the repository root. The removed marker was the obsolete W-B0 blocked example.

| File | Change |
|---|---|
| `components/vp-direct/schemas/SCHEMAS.md` | modified |
| `components/vp-direct/schemas/SCHEMA_GAPS.md` | modified |
| `components/vp-direct/schemas/VALIDATION.md` | modified |
| `components/vp-direct/schemas/adapter_ops.json` | modified |
| `components/vp-direct/schemas/error_codes.json` | modified |
| `components/vp-direct/schemas/schema_gaps.json` | modified |
| `components/vp-direct/schemas/tests/contract_inventory.json` | modified |
| `components/vp-direct/schemas/tests/examples/adapter_pairs.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.admission_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.admission_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.artifact_ref_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.artifact_ref_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.demand_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.demand_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.focus_trace_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.focus_trace_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.forge_receipt_v1.blocked.json` | removed |
| `components/vp-direct/schemas/tests/examples/vp.forge_receipt_v1.invalid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp.forge_receipt_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.guest_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.guest_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.image_pack_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.image_pack_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.model_lease_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.model_lease_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.perception_state_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.perception_state_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.seat_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.seat_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.snapshot_receipt_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.snapshot_receipt_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.surface_binding_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.surface_binding_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.tool_route.index_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.tool_route.index_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.tool_route.reply_v1.valid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp.transition_receipt_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp.transition_receipt_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp_capsule_call.args_v1.invalid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_capsule_call.args_v1.valid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_capture_call.args_v1.invalid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_capture_call.args_v1.valid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_data_call.args_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp_data_call.args_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp_factory_call.args_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp_factory_call.args_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp_guest_call.args_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp_guest_call.args_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp_guest_command_call.args_v1.invalid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_guest_command_call.args_v1.valid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_handoff_call.args_v1.invalid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_headless_call.args_v1.invalid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_headless_call.args_v1.valid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_identity_call.args_v1.invalid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_identity_call.args_v1.valid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_perception_call.args_v1.invalid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp_perception_call.args_v1.valid.json` | added |
| `components/vp-direct/schemas/tests/examples/vp_saved_data_call.args_v1.invalid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_saved_data_call.args_v1.valid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_settings_call.args_v1.invalid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_terminal_call.args_v1.invalid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_terminal_call.args_v1.valid.json` | modified |
| `components/vp-direct/schemas/tests/examples/vp_workspace_call.args_v1.invalid.json` | modified |
| `components/vp-direct/schemas/tests/test_refreeze.py` | added |
| `components/vp-direct/schemas/tests/test_schemas.py` | modified |
| `components/vp-direct/schemas/tests/validation_receipt.json` | modified |
| `components/vp-direct/schemas/validation.py` | added |
| `components/vp-direct/schemas/vp.admission_v1.schema.json` | added |
| `components/vp-direct/schemas/vp.artifact_ref_v1.schema.json` | added |
| `components/vp-direct/schemas/vp.capability_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp.control.reply_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp.control.req_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp.demand_v1.schema.json` | added |
| `components/vp-direct/schemas/vp.focus_trace_v1.schema.json` | added |
| `components/vp-direct/schemas/vp.forge_receipt_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp.forge_tool_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp.guest_v1.schema.json` | added |
| `components/vp-direct/schemas/vp.image_pack_v1.schema.json` | added |
| `components/vp-direct/schemas/vp.model_lease_v1.schema.json` | added |
| `components/vp-direct/schemas/vp.perception_state_v1.schema.json` | added |
| `components/vp-direct/schemas/vp.seat_v1.schema.json` | added |
| `components/vp-direct/schemas/vp.snapshot_receipt_v1.schema.json` | added |
| `components/vp-direct/schemas/vp.surface_binding_v1.schema.json` | added |
| `components/vp-direct/schemas/vp.tool_route.index_entry_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp.tool_route.index_v1.schema.json` | added |
| `components/vp-direct/schemas/vp.tool_route.reply_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp.transition_receipt_v1.schema.json` | added |
| `components/vp-direct/schemas/vp_capsule_call.args_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp_capture_call.args_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp_data_call.args_v1.schema.json` | added |
| `components/vp-direct/schemas/vp_factory_call.args_v1.schema.json` | added |
| `components/vp-direct/schemas/vp_guest_call.args_v1.schema.json` | added |
| `components/vp-direct/schemas/vp_guest_command_call.args_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp_handoff_call.args_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp_headless_call.args_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp_identity_call.args_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp_perception_call.args_v1.schema.json` | added |
| `components/vp-direct/schemas/vp_saved_data_call.args_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp_settings_call.args_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp_terminal_call.args_v1.schema.json` | modified |
| `components/vp-direct/schemas/vp_workspace_call.args_v1.schema.json` | modified |
