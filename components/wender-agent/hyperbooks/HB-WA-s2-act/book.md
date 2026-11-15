# HB-WA-s2-act - ACT (v0 stub book)

Reduced companion book (`hb.companion-format/v1`). Architecture and decisions live in
`HB-WA-a0-arch`; this book will hold the module-level contracts, failure modes and receipts.

## <a id="hb-s0"></a>0. Scope in v0

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s0","code_anchors":["wa.act.actor.choose_path"],"kind":"reading","n":0,"schema":"hb.section.v2","title":"Scope in v0"}
```
<!-- HB:END -->
path choice and single-actor lock (real); executors api/ax/coordinates (stub).

## <a id="hb-s1"></a>1. Open questions

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"hb-s1","code_anchors":[],"kind":"decisions","n":1,"schema":"hb.section.v2","title":"Open questions"}
```
<!-- HB:END -->
Contracts, budgets and receipts for this module: AUSENTE in v0. No performance number is claimed.

## <a id="s10-scripts-act-api"></a>10. Script subsystem: act_api

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-scripts-act-api","code_anchors":["wa.scripts.act_api.ActAPI","wa.scripts.act_api.ActAPI.__init__","wa.scripts.act_api.ActAPI._audit","wa.scripts.act_api.ActAPI._cancel_effect","wa.scripts.act_api.ActAPI._check","wa.scripts.act_api.ActAPI._drain","wa.scripts.act_api.ActAPI._result","wa.scripts.act_api.ActAPI.authorize_target","wa.scripts.act_api.ActAPI.dispatch_effect","wa.scripts.act_api.ActAPI.outstanding","wa.scripts.act_api.ActAPI.prepare_effect","wa.scripts.act_api._ApplicationOwner","wa.scripts.act_api._ApplicationOwner.check_post","wa.scripts.act_api._ApplicationOwner.confirm","wa.scripts.act_api._AppliedEffect","wa.scripts.act_api._BindingOwner","wa.scripts.act_api._BindingOwner.bind","wa.scripts.act_api._DispatchContext","wa.scripts.act_api._ExecutionPolicy","wa.scripts.act_api._ExecutionPolicy.__post_init__","wa.scripts.act_api._InputInterlock","wa.scripts.act_api._InputInterlock.__init__","wa.scripts.act_api._InputInterlock.check_context","wa.scripts.act_api._InputInterlock.invalidate","wa.scripts.act_api._InputInterlock.outstanding","wa.scripts.act_api._InputInterlock.register","wa.scripts.act_api._InputInterlock.release","wa.scripts.act_api._InputInterlock.send_once","wa.scripts.act_api._PreparedEffect","wa.scripts.act_api._RiskOwner","wa.scripts.act_api._RiskOwner.check","wa.scripts.act_api._WireEffect","wa.scripts.act_api._WireOwner","wa.scripts.act_api._WireOwner.accept_input","wa.scripts.act_api._WireOwner.actuator","wa.scripts.act_api._WireOwner.prepare","wa.scripts.act_api._closed","wa.scripts.act_api._duration","wa.scripts.act_api._need","wa.scripts.act_api._passed","wa.scripts.act_api._predicate","wa.scripts.act_api._true","wa.scripts.act_api._unknown","wa.scripts.act_api.authorize_target","wa.scripts.act_api.dispatch_effect","wa.scripts.act_api.prepare_effect"],"kind":"reading","n":10,"schema":"hb.section.v2","title":"Script subsystem: act_api"}
```
<!-- HB:END -->
Trusted owners retain final input custody. Revocation, STOP, current target, focus, authority and deadline are rechecked at the final fence. Possible input consumes the action: no retry or cross-lane fallback. Fresh causal semantic postconditions and terminal goal proof precede success. FrameWait owns paced acquisition, cancellation and exactly one release of each owned observation; unrelated holders remain valid. FrameWaitBridge and module-level wait/release compatibility APIs are AUSENTE.

## <a id="s10-scripts-frame-wait"></a>11. Script subsystem: frame_wait

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-scripts-frame-wait","code_anchors":["wa.scripts.frame_wait.FrameWait","wa.scripts.frame_wait.FrameWait.__init__","wa.scripts.frame_wait.FrameWait._cancel","wa.scripts.frame_wait.FrameWait._check","wa.scripts.frame_wait.FrameWait._drop","wa.scripts.frame_wait.FrameWait._now","wa.scripts.frame_wait.FrameWait._release","wa.scripts.frame_wait.FrameWait._run","wa.scripts.frame_wait.FrameWait.next_observation","wa.scripts.frame_wait.FrameWait.notify","wa.scripts.frame_wait.FrameWait.outstanding","wa.scripts.frame_wait.FrameWait.release_observation","wa.scripts.frame_wait.FrameWait.settle_effect","wa.scripts.frame_wait.FrameWait.wait_until","wa.scripts.frame_wait._ObservationOwner","wa.scripts.frame_wait._ObservationOwner.acquire","wa.scripts.frame_wait._ObservationOwner.check","wa.scripts.frame_wait._ObservationOwner.release","wa.scripts.frame_wait._WaitScheduler","wa.scripts.frame_wait._WaitScheduler.wait_until_ns","wa.scripts.frame_wait._WaitState","wa.scripts.frame_wait._need","wa.scripts.frame_wait._refusal","wa.scripts.frame_wait._true","wa.scripts.frame_wait.due_events"],"kind":"reading","n":11,"schema":"hb.section.v2","title":"Script subsystem: frame_wait"}
```
<!-- HB:END -->
Trusted owners retain final input custody. Revocation, STOP, current target, focus, authority and deadline are rechecked at the final fence. Possible input consumes the action: no retry or cross-lane fallback. Fresh causal semantic postconditions and terminal goal proof precede success. FrameWait owns paced acquisition, cancellation and exactly one release of each owned observation; unrelated holders remain valid. FrameWaitBridge and module-level wait/release compatibility APIs are AUSENTE.

## <a id="s10-act-proto1-action-policy"></a>12. Script subsystem: proto1_action_policy

<!-- HB:BEGIN hb.section.v2 -->
```json
{"anchor":"s10-act-proto1-action-policy","code_anchors":["wa.act.proto1_action_policy.ActionPolicy","wa.act.proto1_action_policy.ActionPolicy.__init__","wa.act.proto1_action_policy.ActionPolicy.dispatch"],"kind":"reading","n":12,"schema":"hb.section.v2","title":"Script subsystem: proto1_action_policy"}
```
<!-- HB:END -->
Trusted owners retain final input custody. Revocation, STOP, current target, focus, authority and deadline are rechecked at the final fence. Possible input consumes the action: no retry or cross-lane fallback. Fresh causal semantic postconditions and terminal goal proof precede success. FrameWait owns paced acquisition, cancellation and exactly one release of each owned observation; unrelated holders remain valid. FrameWaitBridge and module-level wait/release compatibility APIs are AUSENTE.
