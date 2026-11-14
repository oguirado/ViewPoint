# W8U Godot perception — PREPARED

## Frozen local capability boundary

This is the wave-1 Godot consumer, not a replacement OpenSuperficies wire
implementation. Production `w8u-os-contract` binding/result wire schemas, control
magic/kind/body values, the complete END numeric error table, XWD subset and
genesis domain are **AUSENTE** in the observed integration snapshot. No values
are invented for them. Transport credit accounting, STOP/END/EOF/join, action
actuation, receipt serialization and transition serialization remain with their
production owners. Collection does not certify those acceptance gates.

The local Python interface is frozen before delegation (not a wire schema):

* `CapsuleBinding(run_id, lease_id, session_id, stream_id, target_handle, epoch)`.
  The handle has only `kind`, `canonical_id`, `generation`.
* `VerifiedCapsulePair(binding, base, sidecar, frame_metadata, left_rgb,
  right_rgb, left_grab_id, right_grab_id, xwd_subset)` is returned only by the
  injected authenticated verifier. RGB bytes are canonical, packed, in RAM.
* `CommittedPresentation(binding, presentation_id, grab_id, frame_seq,
  frame_sha256, binding_sha256, basis_sha256, action_id, action_seq,
  requested_tick, applied_tick, outcome_sha256, committed)` is returned only
  by the injected presentation verifier. Payload flags do not grant authority.
* `GodotPerceptionResult(observation, frame_metadata, basis_sha256,
  outcome, outcome_sha256, presentation_id, tick_bound)` keeps the unwrapped
  `wender.state/v1` inside the existing observation envelope.
* `W8UGodotPerception(binding, *, contract, contract_revision, registries,
  command_validator, publish=None).perceive(capsule, *, command=None,
  action_id=None, outcome=None, presentation=None)` publishes only after all
  validation. Command and outcome arguments, when present, are canonical
  UTF-8 JSON-L bytes including LF.

The injected object declares `contract_name = "w8u-os-contract"`, an immutable
`revision` equal to the caller's explicit pin, and the owner's `xwd_subset`.
Its `current_binding()` returns the currently authenticated binding.
`verify_pair(capsule, binding)`, `verify_outcome(outcome_bytes, binding)` and
`verify_presentation(presentation, binding)` return the respective trusted pair,
the exact authenticated outcome bytes, and the committed presentation. The
verifier owns authentication and common-XWD-subset decoding. There is no default
verifier, import of an unmerged adapter, wire codec or authority payload flag.
An injected `command_validator(record)` returns an empty error list only for
the producer's closed command schema. A nonempty or malformed result refuses.

Frame metadata has exactly `seq,tick,tick_valid,capture_mono_ns,width,height,
stride,pixel_format,sha256,binding_sha256`; the format comes from the pinned
owner and has no local wire default. The capability also declares
`pixel_format` for its canonical packed RGB output. This local conversion uses
the existing `FrameRef` RGB API; it does not claim an XWD wire layout.
An invalid tick is null. Without tick evidence the screenshot is explicitly
unticked; no outcome or presentation is inferred. A valid tick requires the
full correlated accepted-outcome/presentation/STATE/frame path. Same-grab
equality alone never establishes an engine revision.

## Digests and joins

Node IDs hash the literal UTF-8 control path with SHA-256, taking the first 16
lowercase hex characters. Slash paths are never normalized to dots and must
start with the exact sidecar `screen_id`. STATE basis is SHA-256 of the validated
`schema.dumps(base)` UTF-8 bytes, without LF. Outcome digest hashes canonical
UTF-8 JSON-L including LF. These are the task-specified domains, not substitute
wire digest domains. Binding digest is opaque and must agree with authenticated
presentation evidence; its production derivation is AUSENTE here.

The join uses run/lease/session/stream, target generation and epoch, action ID
and sequence, requested/applied tick, canonical STATE basis, presentation ID,
grab ID, frame sequence and digests. Applied tick must not precede requested
tick and must equal the STATE tick and valid frame tick. The existing Godot
basis contract requires the outcome `state_sha256` to equal that canonical
basis. No timestamps or list positions establish a join.

## Errors and validation

`W8UGodotError` carries a task-defined code and rc: malformed input `E_SCHEMA`
(2), rejected producer schema `E_VERB` (5), failed authentication `E_LOCK` (5),
changed epoch `E_EPOCH` (5), identity/basis/tick/frame mismatch `E_STALE_FRAME`
(5), and unproven effect or lost publication acknowledgement `E_UNCERTAIN`
(6). Missing capabilities raise `W8UGodotUnavailable` with rc 4 and AUSENTE.
Only an empty validation error list succeeds. Validation refusals publish
nothing. Publication can emit before its acknowledgement is lost; it consumes
the frame sequence and quarantines the adapter so a new ID cannot retry it.
Cancellation also quarantines before propagating. The publication mutex blocks
reentrant/concurrent calls, and even unticked frames cannot roll STATE back
behind the last committed tick.

## Evidence and limitations

Source and gate citations, observed VM output, and the remaining owner gates
are recorded below. All fixture dimensions, capacities,
revision labels and numeric choices are TARGET, not host measurements.

## Source file/line citations and gate evidence

Observed source: `bridge/integration` at `0231b1ae001d8ad22bfc2f7e8a35898f58e3e7ea`.
The task metadata names `ccd0204954ff3b2d5b83a3f40389a687bec22bad`; the
attachment explicitly requires the live ref, which takes precedence.

| Source at observed commit | Contract used |
| --- | --- |
| [wender/perceive/godot.py:184](https://github.com/example-owner/wender-agent/blob/0231b1ae001d8ad22bfc2f7e8a35898f58e3e7ea/wender/perceive/godot.py#L184) | Shared dotted/slash grammar |
| [wender/perceive/godot.py:209](https://github.com/example-owner/wender-agent/blob/0231b1ae001d8ad22bfc2f7e8a35898f58e3e7ea/wender/perceive/godot.py#L209) | Literal UTF-8 node ID |
| [wender/perceive/godot.py:402](https://github.com/example-owner/wender-agent/blob/0231b1ae001d8ad22bfc2f7e8a35898f58e3e7ea/wender/perceive/godot.py#L402) | Base/sidecar validation |
| [wender/perceive/godot.py:774](https://github.com/example-owner/wender-agent/blob/0231b1ae001d8ad22bfc2f7e8a35898f58e3e7ea/wender/perceive/godot.py#L774) | Closed T2 outcome validation |
| [wender/perceive/godot.py:922](https://github.com/example-owner/wender-agent/blob/0231b1ae001d8ad22bfc2f7e8a35898f58e3e7ea/wender/perceive/godot.py#L922) | Closed command fields |
| [wender/state/schema.py:248](https://github.com/example-owner/wender-agent/blob/0231b1ae001d8ad22bfc2f7e8a35898f58e3e7ea/wender/state/schema.py#L248) | Canonical STATE dump |
| [wender/state/schema.py:300](https://github.com/example-owner/wender-agent/blob/0231b1ae001d8ad22bfc2f7e8a35898f58e3e7ea/wender/state/schema.py#L300) | Empty error-list STATE validation |
| [wender/perceive/session_registry.py:120](https://github.com/example-owner/wender-agent/blob/0231b1ae001d8ad22bfc2f7e8a35898f58e3e7ea/wender/perceive/session_registry.py#L120) | Immutable FrameRef |
| [wender/perceive/session_registry.py:152](https://github.com/example-owner/wender-agent/blob/0231b1ae001d8ad22bfc2f7e8a35898f58e3e7ea/wender/perceive/session_registry.py#L152) | Frame-required Observation |

The owned target gate is now at `godot_window_target.py:211`; it validates
grammar, then screen root, then literal ID. The metadata envelope at line 58
preserves missing frame evidence without weakening registry Observation.
The old wrong-kind test now uses valid generation 2 so the intended kind refusal
is exercised instead of a registry constructor failure. The unowned
`godot_adapter.py:4` docstring still describes the former dotted restriction.

Independent literal goldens in both gate modules are
`portfolio/table_companies → a388e17dafda554b` and
`portfolio.table_companies → 8b0ac696871ad4b1`. New tests use runtime RAM-only
pixels, opaque authenticated vectors, and disk/process/socket spies with zero
calls. Each refusal asserts its error code/rc and publication count.
The final exact gate reports **45 tests, OK** (0.132 s, VM); the checked runner
reports 45 tests, zero failures/errors/skips/expected failures/unexpected
successes (0.134 s, VM). See the task outbox report for retained baseline and
intermediate failures.

```sh
python3 -m unittest tests.test_w8u_wa_godot_unit tests.test_jq2a_m8_c1_godot_target_implementation
```

Production XWD/wire vectors, capacity-one/exhaustion/duplicate-ACK/inflation
credit cases, STOP/END/EOF/joined reader exit, the closed action actuator,
receipt and transition acceptance remain **AUSENTE** here. Test-only capability
pins do not substitute for the accepted production owner revision. No ACCEPT
claim is made.

