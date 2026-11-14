# HB-WA-s4-learn - LEARN (v0 stub book)

Reduced companion book (`hb.companion-format/v1`). Architecture and decisions live in
`HB-WA-a0-arch`; this book will hold the module-level contracts, failure modes and receipts.

## <a id="hb-s0"></a>0. Scope in v0

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s0","code_anchors":["wa.learn.calibrate.select_thresholds","wa.learn.cascade.CascadePolicy","wa.learn.cu_tasks.observation_status","wa.learn.cu_tasks.predicate_distance","wa.learn.cu_tasks.task_family_hint","wa.learn.demonstrations.reproduced_share_ppm","wa.learn.drift.check_drift","wa.learn.model_client.bounded_output","wa.learn.model_client.evidence_value","wa.tools.cu_live.main","wa.tools.cu_offline_e2e.main","wa.tools.cu_web.main"],"kind":"reading","n":0,"schema":"hb.section.v2","title":"Scope in v0"}
```
<!-- HB:END -->
reproduced-share metric (real); segment/label/map/replay (stub).

## <a id="hb-s1"></a>1. Open questions

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s1","code_anchors":[],"kind":"decisions","n":1,"schema":"hb.section.v2","title":"Open questions"}
```
<!-- HB:END -->
Contracts, budgets and receipts for this module: AUSENTE in v0. No performance number is claimed.

## <a id="s10-scripts-types"></a>10. Script subsystem: types

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-scripts-types","code_anchors":["wa.scripts.types.ABISet","wa.scripts.types.AcquisitionDisposition","wa.scripts.types.AcquisitionResult","wa.scripts.types.ActionKind","wa.scripts.types.AdmissionCheck","wa.scripts.types.ArgSpec","wa.scripts.types.ArtifactDigest","wa.scripts.types.ArtifactIdentity","wa.scripts.types.AssertSpec","wa.scripts.types.AuthorityCheckPort","wa.scripts.types.AuthorityCheckPort.check_final","wa.scripts.types.AuthorityRevision","wa.scripts.types.BindingRegistryPort","wa.scripts.types.BindingRegistryPort.check","wa.scripts.types.CancellationPolicy","wa.scripts.types.CancellationPort","wa.scripts.types.CancellationPort.request","wa.scripts.types.CancellationPort.verify","wa.scripts.types.CancellationProof","wa.scripts.types.CancellationResult","wa.scripts.types.CancellationStatePort","wa.scripts.types.CancellationStatePort.is_cancelled","wa.scripts.types.CanonicalPlanAssociation","wa.scripts.types.ClockPort","wa.scripts.types.ClockPort.now_ns","wa.scripts.types.ContextPolicy","wa.scripts.types.ContextRequest","wa.scripts.types.ContractError","wa.scripts.types.DisplayPolicy","wa.scripts.types.EffectSpec","wa.scripts.types.EventAuthorityPort","wa.scripts.types.EventAuthorityPort.acknowledge_seal","wa.scripts.types.EventAuthorityPort.append","wa.scripts.types.EventAuthorityPort.reserve","wa.scripts.types.EventState","wa.scripts.types.EvidenceSource","wa.scripts.types.ExecutionWitness","wa.scripts.types.ExportDestination","wa.scripts.types.ExportReceipt","wa.scripts.types.ExportReceiptPort","wa.scripts.types.ExportReceiptPort.check","wa.scripts.types.Fact","wa.scripts.types.FactEvidence","wa.scripts.types.FailureClass","wa.scripts.types.FailureClassification","wa.scripts.types.FindSpec","wa.scripts.types.FrameWaitPort","wa.scripts.types.FrameWaitPort.next_observation","wa.scripts.types.FrameWaitPort.wait_until","wa.scripts.types.HarvestSpec","wa.scripts.types.Holder","wa.scripts.types.HolderContract","wa.scripts.types.HostKeyVocabulary","wa.scripts.types.InputDisposition","wa.scripts.types.Lane","wa.scripts.types.Lifecycle","wa.scripts.types.MatchLevel","wa.scripts.types.MeasuredValue","wa.scripts.types.MetadataDigest","wa.scripts.types.MetadataEntry","wa.scripts.types.MissingReason","wa.scripts.types.MissingRef","wa.scripts.types.NodeKind","wa.scripts.types.ObservationRef","wa.scripts.types.Operand","wa.scripts.types.OwnerExchangePort","wa.scripts.types.OwnerExchangePort.check_candidate","wa.scripts.types.OwnerExchangePort.check_certificate","wa.scripts.types.OwnerExchangePort.check_event","wa.scripts.types.OwnerExchangePort.check_lease","wa.scripts.types.OwnerExchangePort.check_seal","wa.scripts.types.OwnerExchangePort.check_snapshot","wa.scripts.types.Ownership","wa.scripts.types.ParserPolicy","wa.scripts.types.PerformanceObjectives","wa.scripts.types.PlanDefinition","wa.scripts.types.PlanDigest","wa.scripts.types.PlanLimits","wa.scripts.types.PredicateOp","wa.scripts.types.PredicateOperator","wa.scripts.types.PredicateResult","wa.scripts.types.PredicateSpec","wa.scripts.types.PrivateRef","wa.scripts.types.ProducerLayout","wa.scripts.types.Purpose","wa.scripts.types.QueryDeclaration","wa.scripts.types.Reason","wa.scripts.types.RegistryRef","wa.scripts.types.ReplayPolicy","wa.scripts.types.Reservation","wa.scripts.types.ReservationGrant","wa.scripts.types.ReservationOutcome","wa.scripts.types.ReservationPort","wa.scripts.types.ReservationPort.grow","wa.scripts.types.ReservationPort.release_unused","wa.scripts.types.ReservationPort.reserve","wa.scripts.types.ReservationRequest","wa.scripts.types.ResolverPolicy","wa.scripts.types.ResourcePolicy","wa.scripts.types.RestartBarrierView","wa.scripts.types.ResultCapabilityPort","wa.scripts.types.ResultCapabilityPort.deliver","wa.scripts.types.ResultDeclaration","wa.scripts.types.RuntimeOutcome","wa.scripts.types.ScopeIdentity","wa.scripts.types.ScriptHeader","wa.scripts.types.Sensitivity","wa.scripts.types.SlotCapabilityPort","wa.scripts.types.SlotCapabilityPort.consume_input","wa.scripts.types.SlotDescriptor","wa.scripts.types.SlotPolicy","wa.scripts.types.SourcePolicy","wa.scripts.types.StepAuthorityPort","wa.scripts.types.StepAuthorityPort.begin_step","wa.scripts.types.StepAuthorityPort.end_step","wa.scripts.types.StepAuthorityPort.release","wa.scripts.types.StepIntent","wa.scripts.types.StepKind","wa.scripts.types.StepPlan","wa.scripts.types.StepResult","wa.scripts.types.StorePolicy","wa.scripts.types.TargetBinding","wa.scripts.types.TargetFacts","wa.scripts.types.TargetSelector","wa.scripts.types.TerminalGoalRef","wa.scripts.types.TerminalStatus","wa.scripts.types.Truth","wa.scripts.types.TruthValue","wa.scripts.types.ValidatedPlan","wa.scripts.types.ValidationRegistryPort","wa.scripts.types.ValidationRegistryPort.check","wa.scripts.types.ValueKind","wa.scripts.types.VerificationBudget","wa.scripts.types.WaitPolicy","wa.scripts.types.WaitSpec","wa.scripts.types.freeze_entries","wa.scripts.types.record_from_entries","wa.scripts.types.truth_all","wa.scripts.types.truth_any","wa.scripts.types.truth_not"],"kind":"reading","n":10,"schema":"hb.section.v2","title":"Script subsystem: types"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-scripts-header"></a>11. Script subsystem: header

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-scripts-header","code_anchors":["wa.scripts.header.AttemptAdmissionPort","wa.scripts.header.AttemptAdmissionPort.charge","wa.scripts.header.LocalParserSupervisor","wa.scripts.header.LocalParserSupervisor.__init__","wa.scripts.header.LocalParserSupervisor.check","wa.scripts.header.LocalParserSupervisor.decode","wa.scripts.header.LocalParserSupervisor.outstanding","wa.scripts.header.LocalParserSupervisor.parse","wa.scripts.header.LocalParserSupervisor.resolve","wa.scripts.header.LocalParserSupervisor.verify","wa.scripts.header.ParserBackendPort","wa.scripts.header.ParserBackendPort.parse","wa.scripts.header.SourceAdmission","wa.scripts.header.SourceAdmission.__init__","wa.scripts.header.SourceAdmission.admit","wa.scripts.header.SourceAdmission.finish","wa.scripts.header.SourceAdmission.outstanding","wa.scripts.header.SupervisorResolverPort","wa.scripts.header.SupervisorResolverPort.resolve","wa.scripts.header.SupervisorVerifierPort","wa.scripts.header.SupervisorVerifierPort.check","wa.scripts.header.SupervisorVerifierPort.verify","wa.scripts.header.admit_source","wa.scripts.header.canonical_identity","wa.scripts.header.canonical_metadata","wa.scripts.header.parse_header","wa.scripts.header.recompute_identity","wa.scripts.header.validate_arguments","wa.scripts.header.verify_pins","wa.scripts.header.verify_registered_plan"],"kind":"reading","n":11,"schema":"hb.section.v2","title":"Script subsystem: header"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-scripts-ast-gate"></a>12. Script subsystem: ast_gate

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-scripts-ast-gate","code_anchors":["wa.scripts.ast_gate.admit_source","wa.scripts.ast_gate.validate_restricted_ast"],"kind":"reading","n":12,"schema":"hb.section.v2","title":"Script subsystem: ast_gate"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-scripts-compiler"></a>13. Script subsystem: compiler

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-scripts-compiler","code_anchors":["wa.scripts.compiler.CompilerInputs","wa.scripts.compiler.CompilerRegistry","wa.scripts.compiler.CompilerRegistry.__init__","wa.scripts.compiler.CompilerRegistry.check","wa.scripts.compiler.CompilerRegistry.outstanding","wa.scripts.compiler.CompilerRegistry.release","wa.scripts.compiler._Bridge.__enter__","wa.scripts.compiler._Bridge.__exit__","wa.scripts.compiler._Bridge.__init__","wa.scripts.compiler._Bridge.close","wa.scripts.compiler._Bridge.parse","wa.scripts.compiler.canonical_plan","wa.scripts.compiler.compile_draft","wa.scripts.compiler.validate_plan","wa.scripts.compiler.verify_pairing"],"kind":"reading","n":13,"schema":"hb.section.v2","title":"Script subsystem: compiler"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-scripts-predicates"></a>14. Script subsystem: predicates

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-scripts-predicates","code_anchors":["wa.scripts.predicates.evaluate","wa.scripts.predicates.evaluate_transition"],"kind":"reading","n":14,"schema":"hb.section.v2","title":"Script subsystem: predicates"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-scripts-runner"></a>15. Script subsystem: runner

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-scripts-runner","code_anchors":["wa.scripts.runner.RunnerState","wa.scripts.runner.RunnerState.__post_init__","wa.scripts.runner._GoalOwner","wa.scripts.runner._GoalOwner.evaluate","wa.scripts.runner._PredicateContext","wa.scripts.runner._PredicateContext.context","wa.scripts.runner._SealOwner","wa.scripts.runner._SealOwner.finish","wa.scripts.runner._check","wa.scripts.runner._goal","wa.scripts.runner._merge","wa.scripts.runner._outcome","wa.scripts.runner.abort","wa.scripts.runner.advance","wa.scripts.runner.finish","wa.scripts.runner.interpret","wa.scripts.runner.start"],"kind":"reading","n":15,"schema":"hb.section.v2","title":"Script subsystem: runner"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-script-store"></a>16. Script subsystem: script_store

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-script-store","code_anchors":["wa.learn.script_store.ArtifactCell","wa.learn.script_store.ArtifactKey","wa.learn.script_store.ArtifactKey.canonical_sort_key","wa.learn.script_store.ArtifactKey.slot_key","wa.learn.script_store.ByteAccounting","wa.learn.script_store.ByteAccounting.total_charged_bytes","wa.learn.script_store.CandidateRef","wa.learn.script_store.ScriptLease","wa.learn.script_store.ScriptStore","wa.learn.script_store.ScriptStore.__init__","wa.learn.script_store.ScriptStore.acquire","wa.learn.script_store.ScriptStore.admit_artifact","wa.learn.script_store.ScriptStore.begin_step","wa.learn.script_store.ScriptStore.check_candidate","wa.learn.script_store.ScriptStore.check_certificate","wa.learn.script_store.ScriptStore.check_event","wa.learn.script_store.ScriptStore.check_lease","wa.learn.script_store.ScriptStore.check_seal","wa.learn.script_store.ScriptStore.check_snapshot","wa.learn.script_store.ScriptStore.compact_evidence","wa.learn.script_store.ScriptStore.complete_holder_cancellation","wa.learn.script_store.ScriptStore.current_authority_revision","wa.learn.script_store.ScriptStore.deny_sequence","wa.learn.script_store.ScriptStore.end_step","wa.learn.script_store.ScriptStore.enforce_holder_deadlines","wa.learn.script_store.ScriptStore.evict_idle","wa.learn.script_store.ScriptStore.invalidate_authority","wa.learn.script_store.ScriptStore.mint_certificate","wa.learn.script_store.ScriptStore.pinned_generation_count","wa.learn.script_store.ScriptStore.publish_default","wa.learn.script_store.ScriptStore.reinstate_quarantined","wa.learn.script_store.ScriptStore.release","wa.learn.script_store.ScriptStore.release_snapshot_pin","wa.learn.script_store.ScriptStore.resolver_policy","wa.learn.script_store.ScriptStore.retain","wa.learn.script_store.ScriptStore.revoke","wa.learn.script_store.ScriptStore.service_stopped","wa.learn.script_store.ScriptStore.slot_check","wa.learn.script_store.ScriptStore.slot_policy","wa.learn.script_store.ScriptStore.store_lifecycle_revision","wa.learn.script_store.ScriptStore.store_policy","wa.learn.script_store.ScriptStore.vocabulary_check","wa.learn.script_store.acquire","wa.learn.script_store.admit_artifact","wa.learn.script_store.begin_step","wa.learn.script_store.check_lease","wa.learn.script_store.end_step","wa.learn.script_store.evict_idle","wa.learn.script_store.publish_default","wa.learn.script_store.release","wa.learn.script_store.retain","wa.learn.script_store.revoke"],"kind":"reading","n":16,"schema":"hb.section.v2","title":"Script subsystem: script_store"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-catalogue"></a>17. Script subsystem: catalogue

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-catalogue","code_anchors":["wa.learn.catalogue.CatalogueEntry","wa.learn.catalogue.CatalogueProbe","wa.learn.catalogue.CatalogueSnapshot","wa.learn.catalogue.build_snapshot","wa.learn.catalogue.publish","wa.learn.catalogue.snapshot"],"kind":"reading","n":17,"schema":"hb.section.v2","title":"Script subsystem: catalogue"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-resolver"></a>18. Script subsystem: resolver

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-resolver","code_anchors":["wa.learn.resolver.ResolveRequest","wa.learn.resolver.ResolveResult","wa.learn.resolver.resolve"],"kind":"reading","n":18,"schema":"hb.section.v2","title":"Script subsystem: resolver"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-dream-replay"></a>19. Script subsystem: dream_replay

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-dream-replay","code_anchors":["wa.learn.dream_replay.DreamCertificate","wa.learn.dream_replay.ReplayView","wa.learn.dream_replay.ReplayView.current_observation","wa.learn.dream_replay.ReplayView.declared_actions","wa.learn.dream_replay.ReplayView.history_digest","wa.learn.dream_replay.ReplayView.reveal_step","wa.learn.dream_replay.ScreeningOutcome","wa.learn.dream_replay.SupportIndex","wa.learn.dream_replay.SupportIndex.__init__","wa.learn.dream_replay.SupportIndex.charged_bytes","wa.learn.dream_replay.SupportIndex.check_certificate","wa.learn.dream_replay.SupportIndex.cohort_ids","wa.learn.dream_replay.SupportIndex.replay_view","wa.learn.dream_replay.SupportedTransition","wa.learn.dream_replay.VerifiedEpisode","wa.learn.dream_replay.build_index","wa.learn.dream_replay.load_verified","wa.learn.dream_replay.screen"],"kind":"reading","n":19,"schema":"hb.section.v2","title":"Script subsystem: dream_replay"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-script-promotion"></a>20. Script subsystem: script_promotion

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-script-promotion","code_anchors":["wa.learn.script_promotion.MutantProofEvidence","wa.learn.script_promotion.PairedEpisodeUtility","wa.learn.script_promotion.PromotionEvidence","wa.learn.script_promotion.PromotionPolicy","wa.learn.script_promotion.SPRTPolicy","wa.learn.script_promotion.ShadowTrialRecord","wa.learn.script_promotion.TransitionResult","wa.learn.script_promotion.UtilityEvaluation","wa.learn.script_promotion.evaluate","wa.learn.script_promotion.record_shadow","wa.learn.script_promotion.reinstate","wa.learn.script_promotion.retire","wa.learn.script_promotion.suspend","wa.learn.script_promotion.transition"],"kind":"reading","n":20,"schema":"hb.section.v2","title":"Script subsystem: script_promotion"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-context-router"></a>21. Script subsystem: context_router

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-context-router","code_anchors":["wa.learn.context_router.ContextCard","wa.learn.context_router.ContextEpoch","wa.learn.context_router.ContextHost","wa.learn.context_router.ContextHost.attest","wa.learn.context_router.ContextHost.cancel_and_drain","wa.learn.context_router.ContextHost.check","wa.learn.context_router.ContextHost.check_delta","wa.learn.context_router.ContextHost.consume","wa.learn.context_router.ContextHost.fence","wa.learn.context_router.ContextHost.now_ns","wa.learn.context_router.ContextHost.register_epoch","wa.learn.context_router.ContextHost.register_request","wa.learn.context_router.ContextResources","wa.learn.context_router.ContextResources.drain","wa.learn.context_router.ContextResources.recovery","wa.learn.context_router.ContextResources.reserve","wa.learn.context_router.ContextResources.retain","wa.learn.context_router.ContextResult","wa.learn.context_router.DeltaBase","wa.learn.context_router.EpochConfiguration","wa.learn.context_router.ExportProjectionOwner","wa.learn.context_router.ExportProjectionOwner.receipt_for","wa.learn.context_router.PackedContext","wa.learn.context_router.ReceiptExportAdapter","wa.learn.context_router.ReceiptExportAdapter.approve","wa.learn.context_router.SuffixEvidence","wa.learn.context_router.close_epoch","wa.learn.context_router.open_epoch","wa.learn.context_router.pack_suffix","wa.learn.context_router.validate_response"],"kind":"reading","n":21,"schema":"hb.section.v2","title":"Script subsystem: context_router"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-script-generator"></a>22. Script subsystem: script_generator

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-script-generator","code_anchors":["wa.learn.script_generator.GenerationBrief","wa.learn.script_generator.GenerationHost","wa.learn.script_generator.GenerationHost.check","wa.learn.script_generator.GenerationPolicy","wa.learn.script_generator.GenerationResult","wa.learn.script_generator.GenerationTransport","wa.learn.script_generator.GenerationTransport.exchange","wa.learn.script_generator.ProvenanceOwner","wa.learn.script_generator.ProvenanceOwner.approve_lineage","wa.learn.script_generator.ProvenanceOwner.check","wa.learn.script_generator.ProvenanceOwner.check_brief","wa.learn.script_generator.ProvenanceOwner.check_draft","wa.learn.script_generator.ProvenanceOwner.loader_inputs","wa.learn.script_generator.ProvenanceOwner.project","wa.learn.script_generator.ProvenanceOwner.register_brief","wa.learn.script_generator.ProvenanceOwner.register_draft","wa.learn.script_generator.SourceDraft","wa.learn.script_generator.extract_segment","wa.learn.script_generator.generate_draft","wa.learn.script_generator.submit_draft"],"kind":"reading","n":22,"schema":"hb.section.v2","title":"Script subsystem: script_generator"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-script-policy"></a>23. Script subsystem: script_policy

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-script-policy","code_anchors":["wa.learn.script_policy.AdmissionGovernor","wa.learn.script_policy.AdmissionGovernor.__init__","wa.learn.script_policy.AdmissionGovernor.charge","wa.learn.script_policy.AdmissionGovernor.grow","wa.learn.script_policy.AdmissionGovernor.outstanding","wa.learn.script_policy.AdmissionGovernor.release","wa.learn.script_policy.AdmissionGovernor.reserve","wa.learn.script_policy.AdmissionPolicy","wa.learn.script_policy.CalibrationArtifact","wa.learn.script_policy.CostModel","wa.learn.script_policy.RouteEligibility","wa.learn.script_policy.eligible_routes","wa.learn.script_policy.expected_cost","wa.learn.script_policy.guard_drift","wa.learn.script_policy.validate_calibration_artifact"],"kind":"reading","n":23,"schema":"hb.section.v2","title":"Script subsystem: script_policy"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-script-metrics"></a>24. Script subsystem: script_metrics

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-script-metrics","code_anchors":["wa.learn.script_metrics.CostEvidence","wa.learn.script_metrics.CostEvidence.value","wa.learn.script_metrics.GateResult","wa.learn.script_metrics.ProviderUsage","wa.learn.script_metrics.RequestTiming","wa.learn.script_metrics.cache_metrics","wa.learn.script_metrics.compare_routes","wa.learn.script_metrics.latency_metrics"],"kind":"reading","n":24,"schema":"hb.section.v2","title":"Script subsystem: script_metrics"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-cascade"></a>25. Script subsystem: cascade

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-cascade","code_anchors":["wa.learn.cascade.CascadeDecision","wa.learn.cascade.CascadeDecision.metadata","wa.learn.cascade.CascadePolicy","wa.learn.cascade.CascadePolicy.__init__","wa.learn.cascade.CascadePolicy.decide","wa.learn.cascade.CascadePolicy.reset_cycle","wa.learn.cascade.EscalationReason","wa.learn.cascade.MODEL_CALIBRATION_REGISTRY","wa.learn.cascade.TierReceipt","wa.learn.cascade.TierReceipt.metadata","wa.learn.cascade.intent_to_action","wa.learn.cascade.is_image_worthy","wa.learn.cascade.validate_tier2_laya_probe"],"kind":"reading","n":25,"schema":"hb.section.v2","title":"Script subsystem: cascade"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-calibrate"></a>26. Script subsystem: calibrate

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-calibrate","code_anchors":["wa.learn.calibrate.CALIBRATION_CODES","wa.learn.calibrate.CalibrationResult","wa.learn.calibrate.CalibrationResult.metadata","wa.learn.calibrate.DistributionReceipt","wa.learn.calibrate.DistributionReceipt.metadata","wa.learn.calibrate.calibrate_decision","wa.learn.calibrate.clopper_pearson_upper_promille","wa.learn.calibrate.conformal_quantile","wa.learn.calibrate.conformal_set","wa.learn.calibrate.ece_brier","wa.learn.calibrate.fit_temperature","wa.learn.calibrate.select_thresholds","wa.learn.calibrate.verify_distribution"],"kind":"reading","n":26,"schema":"hb.section.v2","title":"Script subsystem: calibrate"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-memory"></a>27. Script subsystem: memory

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-memory","code_anchors":["wa.learn.memory.ContextExportAdapter","wa.learn.memory.ContextExportAdapter.approve","wa.learn.memory.ContextTokenizer","wa.learn.memory.ContextTokenizer.count","wa.learn.memory.MemoryRejected","wa.learn.memory.build_planner_memory_view","wa.learn.memory.canonical_dump","wa.learn.memory.control_map_digest","wa.learn.memory.estimate_tokens","wa.learn.memory.freeze_context_memory","wa.learn.memory.is_trusted","wa.learn.memory.load_memory","wa.learn.memory.record_reproduction","wa.learn.memory.resolve_control_candidates","wa.learn.memory.validate_control_map","wa.learn.memory.validate_lessons","wa.learn.memory.validate_memory"],"kind":"reading","n":27,"schema":"hb.section.v2","title":"Script subsystem: memory"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="s10-learn-model-client"></a>28. Script subsystem: model_client

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-learn-model-client","code_anchors":["wa.learn.model_client.ActionValidationError","wa.learn.model_client.Backend","wa.learn.model_client.Backend.generate","wa.learn.model_client.BackendError","wa.learn.model_client.CommandBackend","wa.learn.model_client.CommandBackend.generate","wa.learn.model_client.ContextEpochTransport","wa.learn.model_client.ContextEpochTransport.exchange","wa.learn.model_client.LocalBackend","wa.learn.model_client.LocalBackend.choose","wa.learn.model_client.ModelClient","wa.learn.model_client.ModelClient.act","wa.learn.model_client.ModelTimeoutError","wa.learn.model_client.MuseBackend","wa.learn.model_client.MuseBackend.analyze","wa.learn.model_client.MuseBackend.choose","wa.learn.model_client.MuseBackend.generate","wa.learn.model_client.MuseBackend.plan","wa.learn.model_client.MuseOutput","wa.learn.model_client.PROMPT_Z1_MAX_BYTES","wa.learn.model_client.PROMPT_Z1_PREFIX","wa.learn.model_client.PROMPT_Z1_SHA256","wa.learn.model_client.ReplayBackend","wa.learn.model_client.ReplayBackend.generate","wa.learn.model_client.bounded_output","wa.learn.model_client.evidence_value","wa.learn.model_client.generate_context_epoch","wa.learn.model_client.parse_muse_output","wa.learn.model_client.parse_seb_response","wa.learn.model_client.prompt_zones","wa.learn.model_client.render_context_epoch","wa.learn.model_client.schema_path","wa.learn.model_client.seb_key_map","wa.learn.model_client.seb_payload","wa.learn.model_client.validate_action"],"kind":"reading","n":28,"schema":"hb.section.v2","title":"Script subsystem: model_client"}
```
<!-- HB:END -->
The compiler admits finite immutable plans through closed grammar and pinned validation. Resident store ownership, atomic catalogue publication and exact resolver eligibility precede selection. Supported sealed replay cannot manufacture support. Draft generation is inert; passive shadow has no live capabilities. Promotion requires independent identity, mutant, utility, cohort and publication proof; it never replaces live authorization. Context prefixes remain frozen, mandatory pager evidence survives budgeting, and response acceptance rechecks current authority. Calibration and metrics use owner-validated artifacts, explicit units, actual denominators and null measurements. Policy budgets are configured owner enforcement, not measured design targets.

## <a id="wpplx2-controls"></a>WPPLX2 viewport controls

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"wpplx2-controls","code_anchors":[],"kind":"reading","n":99,"schema":"hb.section.v2","title":"WPPLX2 viewport controls"}
```
<!-- HB:END -->
