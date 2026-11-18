# vp-direct schema freeze

The contract inventory uses JSON Schema Draft 2020-12. Each top-level file replaces the contract slash with an underscore. Each wire identifier maps to a local `urn:` resource. `tests/contract_inventory.json` records ownership and source digests.

| Contract ID | Schema file | Owners |
|---|---|---|
| `lineage.event/v1` | `lineage.event_v1.schema.json` | U3c |
| `lineage.run/v1` | `lineage.run_v1.schema.json` | U3c |
| `rec.timeline/v1` | `rec.timeline_v1.schema.json` | U2a, U2b |
| `tm/v1` | `tm_v1.schema.json` | U3a, U3b |
| `vp.admission/v1` | `vp.admission_v1.schema.json` | P6 |
| `vp.artifact_ref/v1` | `vp.artifact_ref_v1.schema.json` | D1, D2, U4b |
| `vp.capability/v1` | `vp.capability_v1.schema.json` | C1, C5, C6, U6b, U6c |
| `vp.channel.reply/v1` | `vp.channel.reply_v1.schema.json` | U1a, U1b, U1c, U1d |
| `vp.channel.req/v1` | `vp.channel.req_v1.schema.json` | U1a, U1b, U1c, U1d |
| `vp.control.reply/v1` | `vp.control.reply_v1.schema.json` | C1, C4, U6b, U6c |
| `vp.control.req/v1` | `vp.control.req_v1.schema.json` | C1, C2, C3, C4, U6b, U6c |
| `vp.demand/v1` | `vp.demand_v1.schema.json` | P2 |
| `vp.focus_trace/v1` | `vp.focus_trace_v1.schema.json` | A3 |
| `vp.forge_receipt/v1` | `vp.forge_receipt_v1.schema.json` | C6, U3c |
| `vp.forge_tool/v1` | `vp.forge_tool_v1.schema.json` | C6 |
| `vp.frame/v1` | `vp.frame_v1.schema.json` | S5, R2, U2a |
| `vp.guest/v1` | `vp.guest_v1.schema.json` | G2, G3, G4, G5 |
| `vp.human_lease/v1` | `vp.human_lease_v1.schema.json` | S4, U3a, U3c |
| `vp.image_pack/v1` | `vp.image_pack_v1.schema.json` | G1 |
| `vp.model_lease/v1` | `vp.model_lease_v1.schema.json` | D4 |
| `vp.oflane.reply/v1` | `vp.oflane.reply_v1.schema.json` | C4 |
| `vp.oflane.req/v1` | `vp.oflane.req_v1.schema.json` | C4 |
| `vp.perception_state/v1` | `vp.perception_state_v1.schema.json` | P4, U3a |
| `vp.rewind_receipt/v1` | `vp.rewind_receipt_v1.schema.json` | U4a, U4b, U4c |
| `vp.seat/v1` | `vp.seat_v1.schema.json` | W-B0′ |
| `vp.session/v1` | `vp.session_v1.schema.json` | S1, S2, S4, S7, S8, U1a |
| `vp.snapshot_receipt/v1` | `vp.snapshot_receipt_v1.schema.json` | G8 |
| `vp.surface_binding/v1` | `vp.surface_binding_v1.schema.json` | U3a, A1 |
| `vp.tool_route.index/v1` | `vp.tool_route.index_v1.schema.json` | C5, M2 |
| `vp.tool_route.index_entry/v1` | `vp.tool_route.index_entry_v1.schema.json` | C5 |
| `vp.tool_route.reply/v1` | `vp.tool_route.reply_v1.schema.json` | C5, U6b, U6c |
| `vp.transition_receipt/v1` | `vp.transition_receipt_v1.schema.json` | P4, P7 |
| `vp.window.reply/v1` | `vp.window.reply_v1.schema.json` | S3, S4, C4 |
| `vp_capsule_call.args/v1` | `vp_capsule_call.args_v1.schema.json` | C2 |
| `vp_capture_call.args/v1` | `vp_capture_call.args_v1.schema.json` | C2 |
| `vp_data_call.args/v1` | `vp_data_call.args_v1.schema.json` | D1, D2, D4 |
| `vp_factory_call.args/v1` | `vp_factory_call.args_v1.schema.json` | M1, M3, U5 |
| `vp_guest_call.args/v1` | `vp_guest_call.args_v1.schema.json` | G1, G2, G3, G8 |
| `vp_guest_command_call.args/v1` | `vp_guest_command_call.args_v1.schema.json` | C2 |
| `vp_handoff_call.args/v1` | `vp_handoff_call.args_v1.schema.json` | C3 |
| `vp_headless_call.args/v1` | `vp_headless_call.args_v1.schema.json` | C2 |
| `vp_identity_call.args/v1` | `vp_identity_call.args_v1.schema.json` | C3 |
| `vp_perception_call.args/v1` | `vp_perception_call.args_v1.schema.json` | P4 |
| `vp_saved_data_call.args/v1` | `vp_saved_data_call.args_v1.schema.json` | C2 |
| `vp_settings_call.args/v1` | `vp_settings_call.args_v1.schema.json` | C3 |
| `vp_terminal_call.args/v1` | `vp_terminal_call.args_v1.schema.json` | C3 |
| `vp_workspace_call.args/v1` | `vp_workspace_call.args_v1.schema.json` | C3 |

## Adapter resources

Each `<tool>.args/v1` file bundles the individually addressable `<tool>.<op>.args/v1` and `<tool>.<op>.result/v1` resources in its `$defs`. The argument object includes the literal selector `op`. The result is the unwrapped payload. Register embedded resources by their own `$id` if the validator needs an explicit URI store. `validation.py` supplies an offline loader. No HTTP resolution is necessary.

`adapter_ops.json` lists the schema identifiers, pins, policy and source for each operation. Pins cover the UTF-8 encoding of the complete embedded resource, including its identifier, with sorted keys, compact separators and a trailing LF [TARGET]. The per-op structural examples live in `tests/examples/adapter_pairs.json`. Production dispatch checks the live registry, a verified binding and authority. A registry entry with an `op_table` projects its ordered row names into `ops`, takes `any(supported)` and takes `all(operator_only)`. Capability publication uses `validate_contract` to check ordering and exact projection.

`validate_adapter_exchange(request, reply)` checks both envelopes, correlates the tool and request ID, and validates the successful result against the request's exact `args.op`. A missing pair raises `ARGS_SCHEMA_ABSENT`. The result of a refusal is an empty object and its code is preserved on the outer reply. Parked operations have only a refusal payload. Every current row has `binding.kind = "none"`, an empty binding name and a null source [TARGET]. Only a carbon-verified route or IPC row can enable one. The synthetic examples exercise wire shapes without admitting any backend.

The critical-path field names follow SF3 §2.2. Other adapter fields are minimal wire projections [TARGET]. The evidence anchors are in each catalog row. For example, capsule kinds come from `ui/src/catalog/catalogModel.js`, the saved-data payloads from the v0.2 capsule state contracts, and demo state and terminal request fields from `ui/electron/preload.js`. The factory uses common `goal_id`, `inputs`, optional `seed` and `permit_ref`, and returns `artifact_refs`, optional `gate` and a hashed `receipt` [TARGET]. M1 must reconcile these with the producer manifest before dispatch. `af.publish`, `af.lipsync`, `pack_import`, storyboard operations, capsule pin and layout save/load are parked.

The `vp_factory_call.vqa3d.score.result/v1` and `vp_factory_call.vqa3d.gate.result/v1` resources also accept U5's quality projection. Both carry `scene_digest`, an integer `score` on the producer's million-point scale [CITED], and ordered `predicates` with `render` and `frame_not_blank`. The gate projection also carries `accepted`, which agrees with every predicate. The common factory fields remain valid on their own for existing callers and may accompany the quality projection. The result schema pins changed in `adapter_ops.json`. Both rows still require verified bindings for dispatch.

## Derived v3 record layouts [TARGET]

| Contract | Derivation and validation boundary |
|---|---|
| `vp.admission/v1` | P6 records the post-decision surface count, memory margin, retained charge and old/new ring sizes. An admitted expansion requires at most 16 surfaces and at least 15% margin [TARGET]. GPU-dependent admission requires known GPU accounting. The byte comparison is checked by `validation.py`. |
| `vp.focus_trace/v1` | A3 records an interval, a continuity flag and monotonic events. Focus and active-window events carry an XID. XInput2 events carry time only. The provider establishes continuity and identifies unsolicited activations. |
| `vp.image_pack/v1` | G1 records requested and resolved machine specifications and hashed boot artifacts. The initial profile is `notepad-vm` on ARM64/QEMU/KVM. The native layout supplies `/usr/local/share/srf-vm/Image` and `/usr/local/share/srf-vm/root.raw` and omits `initrd`. If present, `initrd` requires the same complete pin on both sides. G1 verifies the bytes on disk before admission. |
| `vp.channel.req/v1` | SF3 §3.2 adds a VM target with `target.surface_kind: "vm"` and `target.based_on_frame.sha256`. Both fields are required when either appears. U1a must compare the digest with the trusted observation and refuse stale evidence before dispatch. |
| `vp.guest/v1` | G2–G5 distinguish managed VM, managed graphical MicroVM and external viewer. Only managed records require workload and profile IDs. An external viewer has false VMM, disk and snapshot custody. Workload and surface IDs must differ. |
| `vp.snapshot_receipt/v1` | G8 records participants, generation, acknowledgements, writer count, an admission fence, action reconciliation, channel drain and per-resource coverage. Completion requires each declared participant's acknowledgement in the same generation. |
| `vp.seat/v1` | One named SF3 seat maps to a model ID. The example uses the assignment's `gpt-6.2-astra`. No deployment seat map is provisioned here. |
| `vp.demand/v1`, `vp.surface_binding/v1`, `vp.perception_state/v1` | Obligations, leases, endpoints and consumers are opaque identifiers. Credits and generations are nonnegative integers. Perception is independent of session authority. |
| `vp.transition_receipt/v1` | Drain, retention, retirement, buffer and acknowledgement lists identify resources or participants. A complete wake to ACTIVE requires a fresh observation. P7 compares acknowledgements against the demand snapshot and checks the producer evidence. |
| `vp.artifact_ref/v1`, `vp.model_lease/v1` | Locators allow `af://` and `file://`. D2 checks actual content and registered roots. D4 uses an explicit eviction policy and records held, released or evicted state. |

## Forge and routing

C6 issues `vp.forge_receipt/v1` for the SF2 proposal, quarantine, validation, approval and admission sequence, or a rejection. Validation requires a passing schema and oracle and a negative control that failed as required. Admission requires an operator actor and non-null publication fields. Other transitions require null publication fields. The journal event of kind `vp.forge_receipt/v1` carries the receipt body with the `lineage` reference omitted. It applies the same field and transition constraints as the sealed receipt. U3c appends the event before the caller seals the receipt with its `lamport`, `prev_hash` and `event_hash`. A first lineage event has the zero genesis hash. U3c verifies contiguous Lamport values and hashes LF-terminated canonical event bytes across the journal.

`lineage.event/v1` and `lineage.run/v1` follow the local Lineage reference schemas in `<workspace>/Lineage/repo/schemas/` and the event journal contract in `<workspace>/Lineage/specs/SPEC_U03.md:127`. The event bundles the upstream resources for `agent.born`, `memory.put` and `bench.declared` and resolves every reference offline. The embedded agent resource retains upstream's map fields `rule_overrides` and `promoted_seat`. The ViewPoint additions are `vp.human_lease/v1` and `vp.forge_receipt/v1` event kinds and U3c's bare `lineage.run/v1` payload for `run.close`, alongside upstream's wrapped payload. These ViewPoint records use the existing journal's operation IDs and normalized relative evidence paths. The inventory pins the reference source bytes. The synthetic examples cover both frozen contracts. Structural validation leaves the hash-chain and replay checks to U3c.

The embedded forge parameter schema is a closed object with at most 16 properties and 4096 canonical UTF-8 bytes including LF [TARGET]. A string definition must specify a maximum length of at most 256 [TARGET]. Integer, boolean and finite scalar `enum` definitions are allowed. `validate_contract` checks the encoded size and verifies that required names are declared. An empty legacy marker denotes zero parameters. SF3's named assertions are `code_eq`, `field_eq`, `digest_eq`, `ax_node_exists`, `ax_text_sha256_eq` and `frame_differs`. Their operands are explicit in the forge schema. The earlier closed digest-predicate form remains available.

The router index contains calibrated integer thresholds, hashed golden data, the leave-one-out method and its receipt. The index digest covers canonical bytes with LF while omitting the self-referential `index_sha256` field [TARGET]. Entries are ordered by tool and op, and excluded providers cannot enter the index. C5 must prove that the selected margin is the smallest qualifying threshold, refuse publication if none exists and check the live registry and both scores at query time. Fixture thresholds are synthetic. A declared but unavailable operation uses `ABSTAIN`, `ROUTE_ABSTAIN`, `reason: "policy_denied"` and `detail: "op_unbound"`. An absent capability uses `reason: "absent_capability"` with `detail` omitted. Each MATCH must keep its flattened MCP arguments consistent with its normalized JSON arguments.

A model can appear in a supported capability only with runnable qualification, a hashed receipt and territorial permission. `Hunyuan3D-2.1` has only the excluded state. Omit unmeasured facts from `measured`. A present fact requires a metric, numeric value, tag, profile and hashed source. Only verified MEASURED facts rank in M2. TARGET and CITED records retain their labels for display. A null resident byte count encodes an unavailable measurement.

## Compatibility and execution

- Existing top-level IDs and mailbox aliases are retained. A legacy adapter selector alone now fails when its operation requires payload fields. Legacy success replies for those adapters must carry the appropriate result. Router candidate margins now use integer thousandths in the closed scoring range. These are wire changes specified by SF3.
- The factory origin, four adapter names, optional capability provider and operation table, optional abstention detail and previously unavailable forge receipt are additive. The session state enum and the initial `browser|app` kinds retain their W-B0 form. The optional `vm_viewer` decision belongs to session admission.
- Previously advertised support flags for unverified operations now read false. Parked and operator-only rules survive registry projection. No backend acquired authority from this freeze.
- R-SCH adds wire shapes and preserves previously valid v1 documents. Consumers comparing the VQA3D result pins must refresh them. VM target identity and frame freshness, initrd-less artifact iteration, the forge append and seal sequence, and the router golden corrections require their respective downstream owners.
- `tests/safe_run.sh` runs the structural, cross-field, negative and mutant tests inside the capped scope. See `VALIDATION.md` for results. Live bindings, provider qualification, calibration and event-chain verification belong to the named downstream units.
