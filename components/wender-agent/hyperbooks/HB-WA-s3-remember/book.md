# HB-WA-s3-remember - REMEMBER (v0 stub book)

Reduced companion book (`hb.companion-format/v1`). Architecture and decisions live in
`HB-WA-a0-arch`; this book will hold the module-level contracts, failure modes and receipts.

## <a id="hb-s0"></a>0. Scope in v0

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s0","code_anchors":["wa.learn.cu_loop.run_loop","wa.learn.cu_memory.Receipt","wa.learn.dream_journal.JournalWriter","wa.remember.program_map.ProgramMap"],"kind":"reading","n":0,"schema":"hb.section.v2","title":"Scope in v0"}
```
<!-- HB:END -->
program map in memory (real); skill admission (real); persistence (AUSENTE).

## <a id="hb-s1"></a>1. Open questions

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s1","code_anchors":[],"kind":"decisions","n":1,"schema":"hb.section.v2","title":"Open questions"}
```
<!-- HB:END -->
Contracts, budgets and receipts for this module: AUSENTE in v0. No performance number is claimed.

## <a id="s10-learn-script-ledger"></a>10. Script subsystem: script_ledger

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-script-ledger","code_anchors":["wa.learn.script_ledger.EpisodeSeal","wa.learn.script_ledger.LedgerOwner","wa.learn.script_ledger.LedgerOwner.acknowledge_seal","wa.learn.script_ledger.LedgerOwner.append","wa.learn.script_ledger.LedgerOwner.event","wa.learn.script_ledger.LedgerOwner.recover","wa.learn.script_ledger.LedgerOwner.release_unused","wa.learn.script_ledger.LedgerOwner.reserve","wa.learn.script_ledger.LedgerOwner.retire","wa.learn.script_ledger.LedgerOwner.seal_episode","wa.learn.script_ledger.LedgerOwner.snapshot","wa.learn.script_ledger.LedgerPolicy","wa.learn.script_ledger.RunEvent","wa.learn.script_ledger.append","wa.learn.script_ledger.consolidate","wa.learn.script_ledger.project_receipt","wa.learn.script_ledger.recover","wa.learn.script_ledger.reserve","wa.learn.script_ledger.seal_episode"],"kind":"reading","n":10,"schema":"hb.section.v2","title":"Script subsystem: script_ledger"}
```
<!-- HB:END -->
Intent capacity precedes input; post-input uncertainty enters authenticated reconciliation. Restart closes all lanes independently of re-admission. Journals require trusted seals and bounded recovery. Measurements preserve null with reasons. Receipt projection retains structural identity only; private receipt handles remain inside owners. Integration sessions drain real store, callback and context ownership, independently of counters. Source owner LEARN and remember book placement are distinct metadata fields.

## <a id="s10-learn-script-integration"></a>11. Script subsystem: script_integration

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-script-integration","code_anchors":["wa.learn.script_integration.ExecutionCustody","wa.learn.script_integration.IntegrationEvidence","wa.learn.script_integration.IntegrationSession","wa.learn.script_integration.LaneSelection","wa.learn.script_integration.RestartState","wa.learn.script_integration.advance_session","wa.learn.script_integration.observe","wa.learn.script_integration.reconcile_uncertain","wa.learn.script_integration.select_lane"],"kind":"reading","n":11,"schema":"hb.section.v2","title":"Script subsystem: script_integration"}
```
<!-- HB:END -->
Intent capacity precedes input; post-input uncertainty enters authenticated reconciliation. Restart closes all lanes independently of re-admission. Journals require trusted seals and bounded recovery. Measurements preserve null with reasons. Receipt projection retains structural identity only; private receipt handles remain inside owners. Integration sessions drain real store, callback and context ownership, independently of counters. Source owner LEARN and remember book placement are distinct metadata fields.

## <a id="s10-learn-dream-journal"></a>12. Script subsystem: dream_journal

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-dream-journal","code_anchors":["wa.learn.dream_journal.JournalVerificationError","wa.learn.dream_journal.JournalWriter","wa.learn.dream_journal.JournalWriter.append","wa.learn.dream_journal.build_support","wa.learn.dream_journal.canonical_payload","wa.learn.dream_journal.compute_step_hash","wa.learn.dream_journal.load_script_episode","wa.learn.dream_journal.ns","wa.learn.dream_journal.validate_script_payload","wa.learn.dream_journal.verify_chain","wa.learn.dream_journal.verify_journal"],"kind":"reading","n":12,"schema":"hb.section.v2","title":"Script subsystem: dream_journal"}
```
<!-- HB:END -->
Intent capacity precedes input; post-input uncertainty enters authenticated reconciliation. Restart closes all lanes independently of re-admission. Journals require trusted seals and bounded recovery. Measurements preserve null with reasons. Receipt projection retains structural identity only; private receipt handles remain inside owners. Integration sessions drain real store, callback and context ownership, independently of counters. Source owner LEARN and remember book placement are distinct metadata fields.

## <a id="s10-learn-cu-memory"></a>13. Script subsystem: cu_memory

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-cu-memory","code_anchors":["wa.learn.cu_memory.BlockedTargetFilter","wa.learn.cu_memory.BlockedTargetFilter.check","wa.learn.cu_memory.BlockedTargetFilter.counts","wa.learn.cu_memory.NoProgressTracker","wa.learn.cu_memory.NoProgressTracker.can_execute","wa.learn.cu_memory.NoProgressTracker.record_step","wa.learn.cu_memory.Receipt","wa.learn.cu_memory.Receipt.to_dict","wa.learn.cu_memory.Receipt.to_local_compact","wa.learn.cu_memory.WorkingMemoryRing","wa.learn.cu_memory.WorkingMemoryRing.append","wa.learn.cu_memory.WorkingMemoryRing.clear","wa.learn.cu_memory.WorkingMemoryRing.for_local","wa.learn.cu_memory.WorkingMemoryRing.for_muse","wa.learn.cu_memory.WorkingMemoryRing.last","wa.learn.cu_memory.canonical_action_sig","wa.learn.cu_memory.canonical_snapshot_bytes","wa.learn.cu_memory.classify_step_outcome","wa.learn.cu_memory.compute_loop_score","wa.learn.cu_memory.fnv1a_64_hex","wa.learn.cu_memory.project_script_receipt","wa.learn.cu_memory.verify_action_postcondition"],"kind":"reading","n":13,"schema":"hb.section.v2","title":"Script subsystem: cu_memory"}
```
<!-- HB:END -->
Intent capacity precedes input; post-input uncertainty enters authenticated reconciliation. Restart closes all lanes independently of re-admission. Journals require trusted seals and bounded recovery. Measurements preserve null with reasons. Receipt projection retains structural identity only; private receipt handles remain inside owners. Integration sessions drain real store, callback and context ownership, independently of counters. Source owner LEARN and remember book placement are distinct metadata fields.

## <a id="s10-learn-cu-loop"></a>14. Script subsystem: cu_loop

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-cu-loop","code_anchors":["wa.learn.cu_loop.BridgeDescriber","wa.learn.cu_loop.BridgeDescriber.describe","wa.learn.cu_loop.Capsule","wa.learn.cu_loop.CommandDecider","wa.learn.cu_loop.CommandDescriber","wa.learn.cu_loop.Decider","wa.learn.cu_loop.Describer","wa.learn.cu_loop.FakeCapsule","wa.learn.cu_loop.ModelDecider","wa.learn.cu_loop.ModelDecider.close","wa.learn.cu_loop.ModelDecider.decide","wa.learn.cu_loop.ModelDecider.start_episode","wa.learn.cu_loop.ReplayDecider","wa.learn.cu_loop.SrfCapsule","wa.learn.cu_loop.SrfCapsule.__init__","wa.learn.cu_loop.SrfCapsule.input","wa.learn.cu_loop._blocked_targets","wa.learn.cu_loop._compact_state_delta","wa.learn.cu_loop._enrich_retry_feedback","wa.learn.cu_loop._script_before_model","wa.learn.cu_loop._state_hash","wa.learn.cu_loop._trace_evidence","wa.learn.cu_loop._verify","wa.learn.cu_loop._web_generating","wa.learn.cu_loop._web_prompt_matches","wa.learn.cu_loop._web_viewport_bar","wa.learn.cu_loop._web_viewport_harvest","wa.learn.cu_loop.main","wa.learn.cu_loop.run_loop","wa.learn.cu_loop.run_script_turn","wa.learn.cu_loop.run_web_cycle"],"kind":"reading","n":14,"schema":"hb.section.v2","title":"Script subsystem: cu_loop"}
```
<!-- HB:END -->
Intent capacity precedes input; post-input uncertainty enters authenticated reconciliation. Restart closes all lanes independently of re-admission. Journals require trusted seals and bounded recovery. Measurements preserve null with reasons. Receipt projection retains structural identity only; private receipt handles remain inside owners. Integration sessions drain real store, callback and context ownership, independently of counters. Source owner LEARN and remember book placement are distinct metadata fields.

## <a id="s10-routing-acceptance"></a>15. Static routing acceptance controls

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-routing-acceptance","code_anchors":["wa.tests.scripts.test_routing_integration.TestS10Routing","wa.tests.scripts.test_routing_integration.TestS10Routing.setUpClass","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_book_coverage","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_coordinates","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_dependencies","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_dependency_controls","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_determinism","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_inventory","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_privacy","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_removal_mutants","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_seals","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_anchor_stale_index","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_gate_inventory","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_gate_schema_controls","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_inventory_no_false_pass","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_owner_evidence_controls","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_owner_evidence_mutants","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_owner_execution_controls","wa.tests.scripts.test_routing_integration.TestS10Routing.test_g_test_anchor_routes","wa.tests.scripts.test_routing_integration._S10EvidenceControl","wa.tests.scripts.test_routing_integration._S10EvidenceControl.test_controls"],"kind":"reading","n":15,"schema":"hb.section.v2","title":"Static routing acceptance controls"}
```
<!-- HB:END -->
Static gate contracts resolve exact test methods and fixture source labels without executing owner suites. Dependency checks include deferred imports and reject undeclared imports and cycles. External vision evidence remains AUSENTE.
