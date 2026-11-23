<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Slot manifest projections

`rea.slot_manifest/v2` selects the release, provider, exchange and operation declarations used to generate router entries, MCP descriptors, CLI commands and JSON calls. It reuses the roles in `rea.provider_slot_manifest/v1` (`rea.provider_slot_manifest_v1`, `provider_slot_manifest.json`). The host owns local installation bindings, grants, licence disposition, the control registry and conformance receipts. See `SLOTHOST_ARCH.json` (`rea.slothost.arch.v1`), `REQUIREMENTS.md` and `rea.slot_manifest_v2.schema.json`.

The licence fence receives `upstream.user_installed`, `name`, `source_url`, `version`, `licence_id`, `licence_evidence_refs`, `component_notices`, `transport.executable_binding` and `capabilities.adapter_identity`. It verifies the independent user installation, release, notices and adapter provenance under host policy. Host policy retains the internal-research-only disposition for `raea` under FSL-1.1 [CITED].

## Inputs and validation

The projector takes a manifest `M`, a local binding snapshot `B` and a host policy and verified registry snapshot `H`. It reads those snapshots without running probes, discovery commands or installations. Parse with duplicate-key rejection and validate with Draft 2020-12 [TARGET] and URI format assertions. Compute `manifest_digest` as lowercase SHA-256 hex of JCS(`M`) using RFC 8785 [CITED]. The manifest `$comment` participates in the digest. Emit JCS for generated JSON, sort rows by `id` and sort set projections by UTF-8 bytes except for perception levels. Preserve ordered argv words, input arrays, observation order and discovery example order.

Apply these compiler gates before publication. A structurally or semantically invalid candidate produces no rows.

- Require unique `contract_refs[].role`, `operations[].id`, `capabilities.prerequisite_probes[].id` and `perception.observation_sources[].id`. The `conform` segment belongs to qualification. For each operation select the row with the same `role` and require `input_ref == request_schema_ref + '#slot=' + role` and `output_ref == result_schema_ref + '#slot=' + role`. The suffix is a compiler selector. Resolve the base URI in the pinned registry. Check its schema identifier, digest, role support and supported profile before generating a callable schema. Verify provider identity and revision, the canonical provider-record digest, implementation digest and adapter identity against the selected binding.
- `rea.provider/v1`, `urn:rea.slot_request:v1` and `urn:rea.slot_result:v1` describe `offline_fixture` exchange. A role row that uses either v1 exchange URI requires `offline_fixture`. An `external_process` row needs a concrete v2-or-later request/result/provider generation registered together as compatible for that role and profile. Verify all pins and the provider's profile support. A versioned spelling or matching major number alone confers no admission. The existing v1 inventory and exchange remain the compatibility path for v1 fixtures.
- Resolve transport kinds from the primary `transport.kind` and `transport.alternates[].kind`. `cli_subcommand` requires `cli_argv` and `mcp_tool` requires `mcp_stdio`. Enforce unique `typed_args` names and positions contiguous from zero [TARGET]. Resolve a `host_composition` through the host mini-tool registry and its optional `native_call` through the qualified adapter. Pending binding and adapter declarations can supply discovery data.
- Order perception levels `none < cdp_atspi < vision`. Each observation source uses a declared level and each declared level has a source. Every `vision` source declares `vision_pool`. Source ids are labels. Producer identity and a real outside-capsule observation witness are checked at runtime.
- Require the union of `record` and `replay` effects to cover `record_replay.effects`. Require discovery operation descriptions to have exactly the operation ids as keys. Validate each example's `args` against the generated input schema of its `op`, including parity with the normalization rules below. Require an empty example list when input schemas are pending.
- Reject collisions across all router ids, normalized MCP names and CLI routes before publishing. Verify the generated public schemas and all resolved registry dependencies against their selected pins. Apply the rule to host commands, mini-tools, conformance cases and external operations together.

`not_installed` permits passive discovery. `null` pins, `unpinned` versions and reserved `.invalid` coordinates mark unresolved data. A `registered` manifest requires a concrete upstream release, verified source coordinate and concrete provider, implementation, contract and adapter pins. Reject `0.0.0` [CITED] when used as the supplied release placeholder. Replacing a declaration with a registered manifest changes its digest. Runtime admission binds that digest to the selected local installation and qualified implementation.

## Availability and qualification

Compute operation availability by applying these conditions in order. Reevaluate against current state at dispatch.

| Availability | Condition |
| --- | --- |
| `not_installed` | The manifest status is `not_installed` or there is no local binding. |
| `blocked` | The selected binding has an identity, pin or platform failure, required probe evidence is absent or failed, the licence is unresolved or disallows execution, or policy or the capsule ceiling disallows the required perception. |
| `parked` | Eligible binding with unresolved installation selection, live profile, effects, grants, shared evidence schema or matching passing conformance receipt. |
| `admitted` | The operation satisfies each applicable gate for the selected binding and current policy. |

Implicit installation selection succeeds when a sole binding exists. Multiple bindings with no selection yield `parked` in discovery. An invocation requiring a binding returns `SCHEMA_INVALID` for an ambiguous or unknown installation ref. Probe results come from host checks under probe grants. A live call requires the admitted `external_process` generation and a bound shared evidence schema. `evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending` parks live execution. `offline_fixture` permits fixture exchange under the host kit. A qualification receipt binds the manifest, binding, implementation, adapter, transport, contract pins, case/check scope and policy it tested. Withdrawal or drift invalidates the affected qualification for future dispatch.

Conformance is a host command with its own eligibility. Use binding, identity, licence, fixture ownership, profile, effects, perception and grant gates applicable to the kit. For a live qualification attempt require explicit host authorization scoped to that kit and a ready live profile. This authorization allows isolated fixture work before a passing operation receipt exists. Per-case runs may return case evidence. An aggregate run requires all applicable cases and required checks, including negative controls, before the host issues a passing qualification receipt. A missing case, skipped required contrast or unbound evidence schema keeps the operation parked. Discovery commands are callable under their own passive grants and report the availability of the entities they describe.

## Router entries

Emit `ext.<slot_id>` for slot describe, `ext.<slot_id>.<operation.id>` for every external operation, `ext.<slot_id>.conform` for the host kit, and `ext.<slot_id>.conform.<case_id>` for each published case. Emit `host.slot.list` and `host.<mini_tool_id>` for host discovery and mini-tools. All rows have these keys.

| Key | Projection rule |
| --- | --- |
| `id`, `owner` | Id from the rules above. Owner is `viewpoint-rea`. |
| `manifest_digest`, `provider_ref` | Manifest digest and exact `M.provider_ref` for `ext.*`. Both are null for independent host rows. |
| `role`, `operation` | External operation `role` and `id`. Otherwise `role` is null and `operation` is `describe`, `conform`, `conform.<case_id>`, `list` or the host mini-tool id, according to the route. |
| `summary` | Slot `discovery.summary`, external `discovery.operation_descriptions[operation.id]`, `Run conformance for <slot_id>` for the kit, or the host registry summary for a case or mini-tool. Use `List REA slot router entries` for `host.slot.list`. |
| `search_terms` | On `ext.*`, sorted distinct union of `discovery.search_terms`, `slot_id`, its hyphen-split parts, `upstream.name`, `capabilities.target_kinds`, `record_replay.observable_channels`, `comparison.comparator_id` and the projected `operation`. Add the case id and host case keywords on case rows. Host rows use the route id, operation and registered keywords when present. |
| `input_schema_ref` | `$id` of the generated public argument schema. Null for unresolved operation or host descriptor dependencies. |
| `output_schema_ref` | `urn:vp.control.reply:v1` using the registered `vp.control.reply/v1` schema. |
| `mcp_name`, `cli_argv_shape` | Exact name and ordered CLI words from the table below. CLI argv omits the executable and optional flags. |
| `json_contract_ref` | Object with `control_request_schema_ref: "urn:vp.control.req:v1"`, `control_reply_schema_ref: "urn:vp.control.reply:v1"`, `op: "rea.slot.project"` and `route_id` equal to `id`. On external operation rows augment it with the six exact keys of the selected `contract_refs[]` object and with `request_selector: operations[].input_ref` and `result_selector: operations[].output_ref` from that operation. Each selector is the complete URI including `#slot=<role>`. For a host mini-tool with verified schemas, add `argument_schema_ref` and `result_schema_ref` from its registered descriptor. |
| `required_effects` | Sorted `operations[].required_effects` for an external operation. Use the ordered kit plan's effect union for a kit and the registered effect set for a case or mini-tool. Discovery commands use the empty set of execution effects. |
| `perception` | Object containing `required_level` and `optional_levels` in level order. Use the manifest levels for slot and external operation rows. Use the verified host plan for kit and case rows and the host descriptor for mini-tools. Passive list uses `none` with no optional levels. Represent unresolved host requirements as null and park the route. |
| `availability` | For an operation, case, kit or independent host row use its applicable eligibility. The slot describe row reports the first present operation state in `admitted`, `parked`, `blocked`, `not_installed` order. |
| `conformance_receipt_ref` | Verified matching receipt reference for the selected binding and scope, or null. A case receipt attests to that case scope. Use null for passive or independent host rows with no qualification receipt. |
| `outside_capsule` | `true`. |
| `facets` | On `ext.*`, include `target_kinds`, `platforms`, primary `transport_kind`, `adapter_identity`, `determinism_modes`, `artifact_roles`, `comparator_id` and `upstream` with `name`, `version`, `licence_id`. Set `binding_kind` to the operation's binding kind or null. Case rows also carry `case_id` and the verified host case reference. Independent host rows use an empty object. |

`rea_slot_inspect` and passive slot describe expose reserved names and availability when a row cannot execute. Public projections exclude local executable paths, probe commands, transport argv, environment values and private adapter dispatch data.

### Conformance and mini-tool source descriptors

The host registry resolves the manifest's `fixture_refs`, `negative_control_refs` and `required_checks` to cases validated against the pinned `contracts/oracle/rea.conformance_case_v1.schema.json`. Add host-mandated applicable cases. The host descriptor supplies a stable public `case_id` using the `opId` grammar, the case ref and digest, summary, keywords and the approved effects, perception and execution policy. Verify the case ref and digest before publishing a case. Deduplicate repeated references to the same case and reject conflicting public ids. Execute the aggregate cases in bytewise `case_id` order, subject to any prerequisites in the verified host plan. A failed prerequisite records abstention for dependent cases. Missing host resolutions park the aggregate, and inspect reports the unresolved references.

The host registry is authoritative for `slot.inspect`, `behaviour.record`, `behaviour.replay`, `behaviour.compare`, `timeline.compare`, `visual.compare`, `evidence.inspect` and `fixture.ground_truth`. A mini-tool descriptor supplies its id, summary, keywords, pinned argument and result schemas, effects, grants, perception, retry policy and handler. `slot.inspect` uses the passive argument and result rules below. Other mini-tools use their registered contracts after pin verification. Their unresolved entries are parked and expose no callable schema. Host compositions resolve those same registered ids and retain producer attribution for every stage. A manifest can declare the dependencies a composition needs. Host policy determines the permitted effects, grants, perception and independent witness.

## Shared input schemas

Generate closed Draft 2020-12 [TARGET] public object schemas for CLI and MCP arguments. JSON uses the same validation after the control-envelope mapping below. Use the prefix `https://viewpoint.local/contracts/rea/projections/` followed by `<slot_id>/<op_id>.input.json` for an external operation, `<slot_id>/describe.input.json` under `discovery/` for slot describe, `<slot_id>/conform.input.json` for the kit, `<slot_id>/conform/<case_id>.input.json` for a case, and `host/<domain>/<operation>.input.json` for a host command or mini-tool. `host.slot.list` uses `host/slot/list.input.json`. Distinct generated ids are part of the collision gate.

All public wrappers require `request_id`, a string matching `^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$` [TARGET]. They accept `dry_run` as a boolean defaulting to false. Existing JSON control envelopes carry these two fields at the root. Apply defaults explicitly during normalization. Merge caller values from CLI flags only when absent or equal to the JSON argument values.

An external operation with verified request and result pins adds these argument properties.

| Property | Schema and normalization |
| --- | --- |
| `installation_ref` | Nonempty string. Resolve omission only for a sole binding. |
| `inputs` | Array, default `[]`. Each closed item requires nonempty strings `role` and `ref` and permits lowercase hexadecimal `sha256` using the manifest digest grammar. `ref` resolves to an authorized artifact or evidence reference. Preserve order. Materialize `role`, `path`, `media_type`, `sha256` and `record_schema` and validate the role-specific constraints on the complete request. |
| `params` | Use the effective closed params object for the selected request role as described below. Default to `{}` only if that object validates. Otherwise require `params`. |
| `perception_level` | Enum of the required and optional declared levels, ordered by level. Default to `required_level`. |
| `determinism_mode` | Enum of `record_replay.determinism_modes`, sorted bytewise. Default to the sole value when there is one. Require an explicit value for multiple modes. |

Resolve `params` after verifying the base URI and digest of `contract_refs[].request_schema_ref` and matching `operations[].input_ref` to its exact role selector. For `urn:rea.slot_request:v1` in `rea.slot_request_v1.schema.json`, find the unique zero-based `allOf` index `i` whose `if.properties.slot.const` equals the operation role and whose `if.required` contains `slot`. Use `then.properties.params` from that branch. Emit the `$ref` `urn:rea.slot_request:v1#/allOf/<i>/then/properties/params` against the pinned registry. The root `properties.params` is the generic object shape and the branch owns closure and allowed keys. Preserve `seed` wherever declared, `mode` and `sample_count` for `replay`, and `budget_frames` and `strategy` for `sample`.

For later generations, the registered role projection identifies the exact schema location and any effective common constraints. Verify that it produces a closed object with `additionalProperties: false` and preserves the role's declared keys, types and restrictions. Apply closure at the object that declares the allowed keys. If a supported role has no `params` declaration and the pinned contract accepts `{}`, emit `{"type":"object","properties":{},"additionalProperties":false}`. An unresolved role, ambiguous branch or unverified closure fails compilation. Validate this same schema for discovery examples, CLI, MCP, JSON and dry-run. Revalidate the complete request, including role inputs, params and host context, after mapping. Pass `params.seed` through the common params path and verify its use independently when claiming seeded replay.

Slot describe adds optional `op_id` using `opId` constrained to the slot's declared operations. List adds optional boolean `all`, default false. `slot.inspect` requires `slot_id` using the manifest's slot id grammar and accepts optional `installation_ref` as above. Kit and case routes bind `slot_id` and, where applicable, `case_id` from the route and accept optional `installation_ref`. Host mini-tools besides `slot.inspect` require `payload`, validated through a `$ref` to the descriptor's verified argument schema. Host wrappers are closed and own the request control fields. Verify effects, limits and perception under host policy.

## MCP, CLI and JSON mappings

Let `S` be `slot_id` with `-` replaced by `_`, and let `D` and `O` be the registered mini-tool domain and operation on either side of its dot. The MCP descriptor name and CLI argv shape follow this table. Each route maps to the same control selector and its exact router `id`.

| Router id | MCP name | CLI argv shape |
| --- | --- | --- |
| `ext.<slot_id>` | `rea_slot_describe__<S>` | `["slot","describe",slot_id]` |
| `ext.<slot_id>.<op_id>` | `rea_ext_<S>__<op_id>` | `["slot","call",slot_id,op_id,"--args","<json>"]` |
| `ext.<slot_id>.conform` | `rea_conform_<S>` | `["slot","conform",slot_id]` |
| `ext.<slot_id>.conform.<case_id>` | `rea_conform_<S>__<case_id>` | `["slot","conform",slot_id,case_id]` |
| `host.slot.list` | `rea_slot_list` | `["slot","list"]` |
| `host.slot.inspect` | `rea_slot_inspect` | `["slot","inspect","<slot_id>"]` |
| Other `host.<domain>.<operation>` | `rea_<D>_<O>` | `["slot","mini","<domain.operation>","--args","<json>"]` |

Use the existing stdio MCP machinery. Sort `tools/list` by name. List admitted external, kit and case calls and policy-eligible host commands, including passive inspect and describe for discovery. The existing parked `vp_rea_call` retains its control semantics. Each enabled descriptor uses its public `inputSchema`, router `summary` as description, and its router id as title except for external operations, which use `upstream.name + ' / ' + operation.id`. Append ` (slot <slot_id> <upstream.version>, perception <required_level>, effects <comma-joined sorted effects>, availability <availability>)` to external operation descriptions. Append a newline and JCS of `args` for each validated example for that operation, in manifest order. `readOnlyHint` is true when all effect actions are `read` or `observe`. `destructiveHint` is true for any `delete` or `overwrite` action. `openWorldHint` is true for any `network` domain. `idempotentHint` is true when the selected binding qualified the `idempotent` retry class. Return the common control reply as `structuredContent` and its JCS as text content.

The commands extend the existing REA exposure CLI with the `slot` namespace. Preserve the existing list, describe and call entry points. `slot list` includes admitted rows by default, accepts `--all` to include every state and emits canonical JSON by default. `--json` is synonymous with the default. `slot describe` accepts an optional operation id. `slot inspect` and `slot conform` accept `--installation REF`. `slot call` and `slot mini` accept either `--args JSON` or `--args-file PATH`. A mini-tool args file follows its public wrapper shape. The generated commands accept `--dry-run` and an optional `--request-id ID` when no argument document supplies the request id. Without a supplied id, a CLI command with no argument document obtains one from the control lane's request-id issuer. Use the same id for a retry. `--strict` is a CLI-only flag on `slot call`. Print one common JSON reply on stdout and diagnostics on stderr.

Use the existing `vp.control.req/v1` (`urn:vp.control.req:v1`) for request envelopes and `vp.control.reply/v1` (`urn:vp.control.reply:v1`) for replies. Register `rea.slot.project` as the proposed projection selector in the existing control dispatch registry. Before exposing a route as callable, verify that the selected control schemas and lane accept its selector, args and reply variants. The existing `vp_rea_call` selector remains registered under its own handler.

| `vp.control.req/v1` field | Mapping |
| --- | --- |
| `schema` | `vp.control.req/v1`. |
| `request_id` | Public request id lifted from the MCP or CLI arguments, or supplied in the JSON envelope. |
| `op` | `rea.slot.project` for these generated routes. |
| `context` | Existing authenticated control context. Resolve and verify it with the same host control policy across lanes. |
| `args` | Closed object with required `route_id` and `arguments`. `route_id` is the exact router row `id`. `arguments` contains the public properties other than `request_id` and `dry_run`. The route supplies bound selectors such as an external operation's `slot_id` and `op_id`. |
| `dry_run` | Public boolean lifted from MCP or CLI arguments or supplied in the JSON envelope. Default false when omitted under the control schema. |

For MCP or CLI, validate the public wrapper and lift the request fields. For JSON or mailbox, validate the existing envelope and route args, reconstruct the public wrapper, and validate it by the same rules. Router invocation uses the row's `json_contract_ref` and the same JSON mapping. Reject unknown route args and conflicting values. Existing control handling owns envelope parsing, authentication and pre-envelope errors. Use the vp-direct `RequestStore` for centrally enrolled MCP, CLI and mailbox calls.

## Dispatch, results and custody

Normalize an external call to a closed object with required `route_id`, `manifest_digest`, `slot_id`, `op_id`, `installation_ref`, `request_id`, `inputs`, `params`, `perception_level`, `determinism_mode`, `dry_run` and `policy_sha256`. Use the public types above. The route and identifier fields, mode, qualified installation ref and digests come from the resolved call. `policy_sha256` and `manifest_digest` are lowercase SHA-256 hex strings. Other routes normalize to a closed object with required `route_id`, `request_id`, `dry_run`, `policy_sha256`, `manifest_digest` and `arguments`. Use the normalized public route arguments, including resolved binding references where applicable. Independent host routes set `manifest_digest` to null. Bind case and host contract identities through the verified policy and route registry snapshot. Claim the durable request id against the hash of the JCS normalized request within its verified RequestStore scope. Equal requests join or recover the recorded claim. Unequal requests under the same claim key yield `REQUEST_ID_CONFLICT`. After interruption, an `at_most_once` claim yields `UNCERTAIN_OUTCOME` until reconciled. A qualified `idempotent` binding may retry the same claim under the host's retry policy.

For an executed operation, derive the materialized request schema and kind from the pinned exchange, put `operations[].role` in request `slot`, and copy the verified provider id, revision and implementation digest. Use the admitted row's `execution_profile`. Populate request `context` from the bound host policy and toolchain. Check artifact digests during materialization. Enforce operation timeout and output limits capped by both provider and host policy. Validate the request and the returned exchange against their pinned schemas and the selected role. Verify the result's association with the request, provider, implementation and process binding. Adapt typed arguments with the admitted adapter. For `cli_subcommand`, concatenate the qualified transport's `argv_prefix`, literal `binding.argv` and adapter-resolved values in `typed_args.position` order. For `mcp_tool`, use the selected stdio prefix and framing and invoke `binding.tool`. Both transport kinds use the user-selected executable, `run_scratch` cwd and the intersection of `env_allowlist` with host policy. Launch directly as argv and deny inline executable content, shell fragments and vendored bindings.

A level above the current grant or capsule ceiling produces `PERCEPTION_DENIED`. `vision` requires a shared pool grant before the first pixel operation. Apply `VISION_POOL_CONTRACT.md` and use the declared `vision_pool.owner` and `ticket_field`. Release the lease after vision work stops. A pre-dispatch pool timeout produces `POOL_TIMEOUT`. After dispatch, account for missing observations through the evidence profile. Dry-run runs pre-dispatch validation and authorization and returns the normalized request preview without starting the provider, writing evidence or acquiring a pixel lease.

Every reply to an accepted envelope uses the carrier's `schema: "vp.control.reply/v1"` and echoes its `request_id` and `op`. The existing lane handles parsing, framing and other failures that precede envelope acceptance. Use the existing field types and serialization for `effective_request_sha256`, `evidence` and `not_verified`. Include the carrier-established effective request hash in `effective_request_sha256` whenever it has been established. If the hash is unavailable, use the registered reply schema's representation for an unavailable digest where permitted. If the schema requires a hash and the carrier cannot establish it, report the failure through the existing lane. Put authorized retained artifacts and receipt references in `evidence` using the existing carrier representation. Use `ok: true` and `code: "OK"` for a completed control dispatch or accepted preview. A valid returned provider error, comparison mismatch or conformance failure remains an execution result. Use `ok: false`, `result: {}` (a closed empty object) and the following `code` values for control refusals. The existing lane's error reporting supplies diagnostic detail.

| CLI exit [TARGET] | Control `code` |
| --- | --- |
| `10` | `HOLD`, `HUMAN_NEEDED` |
| `11` | `NO_LEASE` |
| `12` | `REQUEST_ID_CONFLICT` |
| `13` | `UNCERTAIN_OUTCOME` |
| `14` | `SCHEMA_INVALID` |
| `15` | `PERCEPTION_DENIED`, `POOL_TIMEOUT` |
| `16` | `PROFILE_NOT_ADMITTED`, `AVAILABILITY_NOT_INSTALLED`, `AVAILABILITY_BLOCKED`, `AVAILABILITY_PARKED` |

`PROFILE_NOT_ADMITTED` covers an offline-only or otherwise unadmitted profile requested for a live call. Remaining availability gates use the corresponding `AVAILABILITY_*` code. For an executed provider exchange, the CLI preserves nonzero provider `rc`. When provider `rc` is zero [CITED], `--strict` maps `mismatch` to exit `5` and `abstain` to exit `3` [TARGET]. Other completed control calls exit `0` [TARGET].

### Types of `vp.control.reply/v1.result`

The projection validates these payloads inside the existing reply. Each listed object is closed. Listed properties are required unless marked optional. `ref` means a nonempty string authorized for the caller or null when unavailable or redacted. All tags are literal strings. The router advertises the existing control reply schema URI.

| Result kind | Property schemas and source |
| --- | --- |
| `slot_exchange` | `kind: "slot_exchange"`; `status` and integer `rc` constrained as the pairs below; `slot_result_ref: ref` for the retained pinned exchange result; `evidence_ref: ref` for the retained observation bundle; `receipt_ref: ref` for the shared evidence-store attempt receipt; `availability: "admitted"`; `produced_by`: nonempty qualified adapter id; `perception_used`: `none`, `cdp_atspi` or `vision` as witnessed; `outside_capsule: true`; `verdict`: the closed object below, required on `compare` and absent on other operations. |
| `dry_run` | `kind: "dry_run"`; `dry_run: true`; `normalized_request`: the closed normalized request described above; `outside_capsule: true`. Apply the control lane's authorization before returning referenced data. |
| `conformance` | `kind: "conformance"`; `cases`: ordered array of closed `{case_id, outcome, case_receipt_ref, reason}` objects, with `case_id` using `opId`, `outcome` in `pass`, `fail`, `abstain`, `case_receipt_ref: ref`, and `reason` as a nonempty string for `fail` or `abstain` and null for `pass`; `conformance_receipt_ref: ref` for the aggregate qualification scope, or null for a case-only run; `outside_capsule: true`. |
| `host_result` | `kind: "host_result"`; `payload`: value validated against the host mini-tool's registered result schema; `outside_capsule: true`. `slot.inspect` uses the inspect object below. |
| `slot_describe` | `kind: "slot_describe"`; `slot`: object produced by the describe projection below. |
| `slot_list` | `kind: "slot_list"`; `rows`: array of router row objects in router id order, filtered by `all`. |

Preserve `OK/0`, `ERROR/2`, `INCOMPLETE/3`, `STALE/4`, `FAIL/5` and `BUDGET_EXCEEDED/6` [CITED] for the existing compatible exchange. Admit an external generation for this projector only after verifying that it preserves these public pairs. The `verdict` object requires `result` in `match`, `mismatch`, `abstain`, nonempty strings `comparator_id` and `comparator_version`, and `verdict_ref: ref` identifying the retained host verdict. Require `abstain_reason` as a nonempty string exactly when `result` is `abstain`. Retain coverage, oracle independence and executed contrasts in the evidence profile. The shared attempt `receipt_ref`, the authorization-specific observation `evidence_ref`, and the qualification `conformance_receipt_ref` address different retained objects. When disclosed, each locator must resolve to its matching object in the carrier's `evidence` projection.

## Evidence and describe

Use `REA_RECEIPT_FIELDS.md`, `contracts/evidence/rea.slot_evidence_profile_v1.schema.json` (`rea.slot_evidence_profile/v1`) and `contracts/evidence/STATUS_MAPPING.md`. The evidence adapter binds the resolved `evidence.shared_schema_ref` to the verified `ViewPoint_Tools` shared schema digest. Project the evidence `slot` group as follows.

| Evidence key | Source |
| --- | --- |
| `slot.tool_id` | `slot_id`. |
| `slot.tool_version` | Verified `upstream.version`. |
| `slot.upstream_licence_id` | Reviewed `upstream.licence_id`. |
| `slot.source_url` | Verified `upstream.source_url`. |
| `slot.user_installed` | `upstream.user_installed` confirmed against installation custody. |
| `slot.out_of_process` | Host witness of the actual process boundary. |
| `slot.use_disposition` | Verified host licence-policy disposition. |
| `slot.manifest_sha256` | `manifest_digest`. |

The host binds episode and parent lineage, media digest, container and codec, frame index, PTS, clock source and decoded frame digest to the actual evidence. The timeline binds event id, event time, kind, input digest and timeline digest. Observation binds the actual producer, perception level and outside-capsule witness. For vision, restrict custody of the actual `vision_pool_ticket` and return the evidence profile's redacted projection. Replay binds the witnessed seed when exercised, input tape digest and `replay.determinism_mode`. `record_replay.input_codec`, `output_codec`, `observable_channels` and `state_domain` define the recording/replay plan, and `freshness_inputs` define the inputs the host must verify before reuse.

For each validated manifest mode, admit a host-policy mapping from the declared label to one evidence mode and an explicit witness predicate for that mode. Resolve the selected label in the admitted mapping bound to the adapter and policy. Emit `exact` only when the registered exact controls and state identity were witnessed, `seeded` when the required controls and claimed seed use were witnessed, or `distribution` when the declared sampling and distribution controls were witnessed. Emit `uncontrolled` when the mapping declares that mode or when its claimed controls were not witnessed. Record the missing controls and apply coverage and abstention rules. A label such as `recorded_inputs` or `pinned_outputs` conveys no witnessed mode by itself. An absent mapping fails live qualification.

Apply `comparison.comparator_id`, `version`, `policy_ref`, `coverage_requirements`, `tolerance_units` and `waiver_policy` to host verdict production. Bind waiver source, inputs, actual values, reason, scope and policy version and fail stale waivers. Preserve the producer and scope of each fixture claim, upstream observation and host-verified verdict. Apply `evidence.required_rea_fields` to operation and policy scopes, including `replay` for replay, `timeline` for event/PTS checks and `verdict` for compare. Retain payloads by `artifact_roles`, `retention` and `visibility` subject to evidence-store policy. The conformance kit uses the manifest's fixtures, negative controls, required checks and supported entry points to verify record, replay, compare, perception and transport parity. The router path participates in the parity check.

The closed describe object contains `schema`, `slot_id`, `status`, `manifest_digest`, `upstream`, `provider_ref`, `capabilities`, `perception`, `record_replay`, `comparison`, `evidence`, `conformance`, `discovery` and `operations`. Copy the named manifest fields with these projections. Filter `upstream.licence_evidence_refs` and `component_notices` to authorized coordinates. For `capabilities.prerequisite_probes`, emit closed objects with `id`, `effects`, `requires_binding` and `verified_state` in `passed`, `failed`, `unavailable`. Each operation is a closed object containing the manifest's `id`, `role`, `input_ref`, `output_ref`, `required_effects`, `required_grants`, `limits`, `retry_semantics`, plus `binding_kind`, `availability` and `conformance_receipt_ref` from its binding type and router row. Sort operations by id and filter to `op_id` when requested. In that case also filter `discovery.operation_descriptions` and `discovery.examples` to that operation. The closed inspect object contains `slot` as the describe object, `installation_refs` as a bytewise-sorted list of authorized refs, `selected_installation_ref: ref`, `conformance_receipt_ref: ref` for the verified aggregate qualification and `unresolved_conformance_refs` as a bytewise-sorted list of unresolved fixture or negative-control refs. The private execution plan consumes the binding and transport fields omitted from public projections.

## Conformance contrasts

The examples `examples/rust-anything.slot.json`, `examples/rea.slot.json` and `examples/craft-anything.slot.json` must validate and yield `not_installed` without a local binding. Their ids are `rust-anything`, `rea` and `craft-anything`. The rust-anything record row is `ext.rust-anything.record`, with role `replay`, MCP name `rea_ext_rust_anything__record`, and CLI shape `["slot","call","rust-anything","record","--args","<json>"]`. Its pending request pin gives `input_schema_ref: null`. Its `json_contract_ref.request_selector` is `urn:rea.slot_request:v1#slot=replay` and `result_selector` is `urn:rea.slot_result:v1#slot=replay`. Project the same snapshots twice and require byte equality. Alter a binding or manifest digest and require the prior receipt to fail qualification.

Collect all schema errors for each negative. Require the sole assertion below. Apply only the named repair in memory and require schema validity and passive discovery projection.

| Fixture | Sole assertion | Positive contrast |
| --- | --- | --- |
| `negative/agent-inside-capsule.slot.json` | `const` at `/transport/driver_location` | Set `outside_capsule`. |
| `negative/vision-without-pool.slot.json` | `required` for `vision_pool` at `/perception/observation_sources/1` | Add `{"required":true,"owner":"ViewPoint_Tools","ticket_field":"vision_pool_ticket"}` as `vision_pool` on that source. |
| `negative/vendored-code.slot.json` | `const` at `/upstream/user_installed` | Set `true`. |
| `negative/missing-licence.slot.json` | `required` for `licence_id` at `/upstream` | Set `licence_id` to `MIT`. |

Use a schema-only declaration with concrete test pins and v2 provider, request and result identifiers to exercise the `external_process` structural path. With all operation selectors generated from their role rows, it passes structural validation. Independently replace either the request or result URI with v1, keeping its operation selectors consistent, and require rejection at `execution_profile`. Replace the provider schema with `rea.provider/v1` in the live case and require rejection at `provider_ref.schema`. Null out a live exchange pin and require rejection at that pin. Withhold verified registry admission and require zero live dispatches [TARGET]. Test that a valid v1 fixture still uses `offline_fixture`.

For v1 projected params, accept `replay` with `seed`, `mode` and `sample_count`, and `sample` with `seed`, `budget_frames` and `strategy` using valid values. Reject `budget_frames` on `replay` and an undeclared property on either role across all lanes, including dry-run. Check collision rejection using an external `conform` operation. Compare a known case route and `host.slot.inspect` across router, CLI, MCP and JSON, then withhold the case's registry pin and require its callable projection to be unavailable. Replay a request id with equal normalized input across lanes and require the same claim. Change a normalized argument under that id and require `REQUEST_ID_CONFLICT`. An executed `mismatch` must remain in `result.verdict` and a control refusal in `code`.

This contract is scoped to the supplied manifest and slot-host requirements corpus.
