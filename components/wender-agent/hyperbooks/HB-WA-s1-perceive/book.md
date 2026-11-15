# HB-WA-s1-perceive - PERCEIVE (v0 stub book)

Reduced companion book (`hb.companion-format/v1`). Architecture and decisions live in
`HB-WA-a0-arch`; this book will hold the module-level contracts, failure modes and receipts.

## <a id="hb-s0"></a>0. Scope in v0

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s0","code_anchors":["wa.perceive.ax.canon","wa.perceive.ax.context_matches","wa.perceive.ax.describe","wa.perceive.ax.level_allows_irreversible","wa.perceive.ax.prune","wa.perceive.ax.release","wa.perceive.ax.sha256_text","wa.perceive.browser.AmbiguousDiscoveryError","wa.perceive.browser.EpochChangedError","wa.perceive.browser.HandleError","wa.perceive.browser.PerceptionFocusError","wa.perceive.browser.SessionDisagreementError","wa.perceive.browser.SharedSessionError","wa.perceive.browser.StaleGenerationError","wa.perceive.browser.inpage_script","wa.perceive.cdp_pipe.CdpError","wa.perceive.cdp_pipe.CdpPipe","wa.perceive.cdp_pipe.WriterFence","wa.perceive.cdp_pipe._rmtree_profile","wa.perceive.cdp_port.CdpTabError","wa.perceive.disambig.disambiguate","wa.perceive.element_table.assemble","wa.perceive.mark_node_join.join_marks_nodes","wa.perceive.parallel_perceive.ObservationError","wa.perceive.session_registry.SessionRegistry","wa.perceive.srf_identity.srf_hash","wa.perceive.tab_manager.TabManager","wa.perceive.transform.estimate_transform","wa.perceive.vision_bridge.SubprocessVisionEngine","wa.perceive.vision_engine_real.FrameDtypeError","wa.perceive.vision_engine_real.FrameValidationError","wa.perceive.vision_engine_real.InvalidDprError","wa.perceive.vision_engine_real.InvalidFrameSequenceError","wa.perceive.vision_engine_real.InvalidTargetError","wa.perceive.vision_engine_real.RunnerDeathError","wa.perceive.vision_engine_real.validate_window_frames"],"kind":"reading","n":0,"schema":"hb.section.v2","title":"Scope in v0"}
```
<!-- HB:END -->
cdp_pipe (real), ax (real, ported), browser (real), atspi (discovery real, tree walk AUSENTE), ladder (real), vision_bridge (stub).

## <a id="hb-s1"></a>1. Open questions

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s1","code_anchors":[],"kind":"decisions","n":1,"schema":"hb.section.v2","title":"Open questions"}
```
<!-- HB:END -->
Contracts, budgets and receipts for this module: AUSENTE in v0. No performance number is claimed.

## <a id="s10-perceive-script-observation"></a>10. Script subsystem: script_observation

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-perceive-script-observation","code_anchors":["wa.perceive.script_observation.ObservationBundle","wa.perceive.script_observation.build","wa.perceive.script_observation.image_need","wa.perceive.script_observation.project_cascade"],"kind":"reading","n":10,"schema":"hb.section.v2","title":"Script subsystem: script_observation"}
```
<!-- HB:END -->
Missing facts remain UNKNOWN. Private inventory includes competing elements and relevant turn owners even when the public projection is smaller. Certified joins bind source publication and transform; no assumed identity transform. Receipt-backed structural projection does not authorize export of raw text, images or private values. Ambiguous unnamed imagery requires safe image evidence or refusal. The implemented observation ingress is script_observation.build; a public ObservationBundle definition is AUSENTE.

## <a id="s10-perceive-step-binding"></a>11. Script subsystem: step_binding

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-perceive-step-binding","code_anchors":["wa.perceive.step_binding.bind_target","wa.perceive.step_binding.normalize_facts","wa.perceive.step_binding.verify_association"],"kind":"reading","n":11,"schema":"hb.section.v2","title":"Script subsystem: step_binding"}
```
<!-- HB:END -->
Missing facts remain UNKNOWN. Private inventory includes competing elements and relevant turn owners even when the public projection is smaller. Certified joins bind source publication and transform; no assumed identity transform. Receipt-backed structural projection does not authorize export of raw text, images or private values. Ambiguous unnamed imagery requires safe image evidence or refusal. The implemented observation ingress is script_observation.build; a public ObservationBundle definition is AUSENTE.

## <a id="s10-perceive-redact"></a>12. Script subsystem: redact

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-perceive-redact","code_anchors":["wa.perceive.redact.project_image","wa.perceive.redact.project_observation","wa.perceive.redact.sanitize_text","wa.perceive.redact.validate_public_payload"],"kind":"reading","n":12,"schema":"hb.section.v2","title":"Script subsystem: redact"}
```
<!-- HB:END -->
Missing facts remain UNKNOWN. Private inventory includes competing elements and relevant turn owners even when the public projection is smaller. Certified joins bind source publication and transform; no assumed identity transform. Receipt-backed structural projection does not authorize export of raw text, images or private values. Ambiguous unnamed imagery requires safe image evidence or refusal. The implemented observation ingress is script_observation.build; a public ObservationBundle definition is AUSENTE.

## <a id="s10-perceive-element-table"></a>13. Script subsystem: element_table

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-perceive-element-table","code_anchors":["wa.perceive.element_table.ABSTAIN_TEXT","wa.perceive.element_table.BIT_ACKED","wa.perceive.element_table.BIT_BUSY","wa.perceive.element_table.BIT_CHECKED","wa.perceive.element_table.BIT_CURRENT","wa.perceive.element_table.BIT_DISABLED","wa.perceive.element_table.BIT_EXPANDED","wa.perceive.element_table.BIT_FOCUSED","wa.perceive.element_table.BIT_HIDDEN","wa.perceive.element_table.BIT_PRESSED","wa.perceive.element_table.BIT_READ","wa.perceive.element_table.BIT_SELECTED","wa.perceive.element_table.BIT_VISIBLE","wa.perceive.element_table.ElementRow","wa.perceive.element_table.ElementTable","wa.perceive.element_table.ElementTable.inventory_receipt_dict","wa.perceive.element_table.ElementTable.recency_rows","wa.perceive.element_table.ElementTable.to_wire_dict","wa.perceive.element_table.InventoryReceipt","wa.perceive.element_table.MAX_RECENCY_MATCHED","wa.perceive.element_table.MAX_SEB_OPTIONS","wa.perceive.element_table.MAX_TABLE_ROWS","wa.perceive.element_table.MAX_TABLE_TOKENS","wa.perceive.element_table.M_ACT","wa.perceive.element_table.ProvenanceRecord","wa.perceive.element_table.REGION_RANK","wa.perceive.element_table.SCHEMA_ID","wa.perceive.element_table.SENTINEL_ZERO_SHA256","wa.perceive.element_table.assemble","wa.perceive.element_table.build_seb_key_map","wa.perceive.element_table.count_tokens","wa.perceive.element_table.validate_seb_key_map","wa.perceive.element_table.validate_table_budget"],"kind":"reading","n":13,"schema":"hb.section.v2","title":"Script subsystem: element_table"}
```
<!-- HB:END -->
Missing facts remain UNKNOWN. Private inventory includes competing elements and relevant turn owners even when the public projection is smaller. Certified joins bind source publication and transform; no assumed identity transform. Receipt-backed structural projection does not authorize export of raw text, images or private values. Ambiguous unnamed imagery requires safe image evidence or refusal. The implemented observation ingress is script_observation.build; a public ObservationBundle definition is AUSENTE.

## <a id="s10-perceive-disambig"></a>14. Script subsystem: disambig

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-perceive-disambig","code_anchors":["wa.perceive.disambig.TurnOwnership","wa.perceive.disambig.answer_container_for_turn","wa.perceive.disambig.canon_name","wa.perceive.disambig.capture_turn_boundary","wa.perceive.disambig.container_ctx_for","wa.perceive.disambig.container_vs_leaf","wa.perceive.disambig.disambiguate","wa.perceive.disambig.format_label","wa.perceive.disambig.verify_harvested_text","wa.perceive.disambig.wl_signature"],"kind":"reading","n":14,"schema":"hb.section.v2","title":"Script subsystem: disambig"}
```
<!-- HB:END -->
Missing facts remain UNKNOWN. Private inventory includes competing elements and relevant turn owners even when the public projection is smaller. Certified joins bind source publication and transform; no assumed identity transform. Receipt-backed structural projection does not authorize export of raw text, images or private values. Ambiguous unnamed imagery requires safe image evidence or refusal. The implemented observation ingress is script_observation.build; a public ObservationBundle definition is AUSENTE.

## <a id="wpplx2-viewport"></a>WPPLX2 viewport controls

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"wpplx2-viewport","code_anchors":["wa.perceive.srf_identity._generating","wa.perceive.srf_identity._viewport_thread_margin"],"kind":"reading","n":99,"schema":"hb.section.v2","title":"WPPLX2 viewport controls"}
```
<!-- HB:END -->
