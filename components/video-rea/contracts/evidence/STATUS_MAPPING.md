<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Mapping rea.slot_result/v1 statuses to evidence verdicts

Profile `rea.slot_evidence_profile/v1`. Sources [CITED] are `rea.slot_result/v1`, `SLOTHOST_ARCH.json` and `VISION_POOL_CONTRACT.md`. `TIMELINE_SEMANTICS.md` defines the channel and custody rules. This mapping applies to `attempt.operation: compare`. `record` and `replay` retain the validated provider result when present and have no `verdict`.

## Exchange checks

The host validates the result against the pinned `rea.slot_result/v1` contract and verifies its `status` and `rc`. It binds `request_id`, `provider_id`, `provider_revision`, `implementation_sha256`, `execution_profile` and provider role `slot` to the dispatched `rea.slot_request/v1` and admitted registration. It verifies `record_schema`, retained output digests and process provenance before accepting observations. `offline_fixture` permits fixture exchange only. An invalid or inadmissible exchange produces `abstain` with `exchange_invalid`. A rejected request, file or process binding produces `evidence_binding_rejected`. Missing proof of custody produces `custody_unverified`. The invalid result stays in the retained attempt artifacts. Only a schema-valid provider result reference goes in `attempt.slot_result`.

## Provider status and host verdict

The status and return code pairs below are fixed by `rea.slot_result/v1` [CITED]. The host stores them in `attempt.slot_result.status` and `attempt.slot_result.rc`. `verdict.result` is the host comparator's decision.

| Provider `status` | Provider `rc` | Host compare verdict |
| --- | --- | --- |
| `OK` | `0` | `match`, `mismatch` or `abstain` according to admitted host checks and policy gates. The provider delivered the required role outputs. |
| `ERROR` | `2` | `abstain` with `slot_error`. Retain provider failure artifacts and leave `verdict.checks` empty. |
| `INCOMPLETE` | `3` | `abstain` with `slot_incomplete` when the verified partial outputs cannot establish a divergence. A host-verified divergence in an admitted check yields `mismatch` if it does not depend on missing coverage. Missing output remains unknown. |
| `STALE` | `4` | `abstain` with `slot_stale`. Leave `verdict.checks` empty. A separate host freshness failure uses `stale_evidence`. |
| `FAIL` | `5` | `mismatch` with an admitted host divergence. Use `abstain` with `slot_claim_only` when no host check can assess the provider's claim, `slot_host_disagreement` when the host checks match, or the host's more specific reason when the host comparison abstains. Retain any provider outcome as advisory `verdict.slot_reported_outcome`. |
| `BUDGET_EXCEEDED` | `6` | `abstain` with `slot_budget_exceeded`. Retain partial artifacts and leave `verdict.checks` empty. |

A provider pass claim with a host mismatch produces `mismatch`. A provider status cannot qualify a match. A refused comparison may carry an abstaining evidence receipt in the store even when the control reply contains only a refusal. Provider `rc` retains its exchange meaning. CLI exit rules belong to `contracts/manifest/PROJECTIONS.md`.

## Control and pool outcomes

| Host outcome | Receipt handling |
| --- | --- |
| `HOLD`, `OWNER`, `FORGE_APPROVAL_REQUIRED`, `ARGS_SCHEMA_ABSENT`, `NO_LEASE`, `REQUEST_ID_CONFLICT` or another dispatch refusal | Record `attempt.control_outcome`. A compare receipt uses `abstain` with `control_refused` and an empty `verdict.checks` array. |
| `UNCERTAIN_OUTCOME` | Record `attempt.control_outcome`. A compare receipt uses `abstain` with `uncertain_outcome`. A reconciliation creates a new receipt referencing the earlier attempt. |
| Pool `timeout`, exit `3` [CITED] | No grant for this acquisition. An unavailable required pixel check abstains with `vision_pool_timeout`. Independently authorized channels may proceed. |
| Pool `lease_mismatch`, exit `4` [CITED], or missing grant or release proof for claimed pixel work | Discard those pixel observations and use `lease_custody_unverified` if the missing witness prevents a decision. |
| An acquisition failed without timeout and granted no lease | A required pixel channel abstains with `vision_pool_not_granted`. |
| Duplicate request across MCP, CLI or JSON | RequestStore reuses the durable claim and receipt for the same normalized request. A conflicting request with the same id follows the control refusal rule. |

## Evaluation order

1. Apply control handling and validate the exchange and custody. Use the first applicable failure reason and retain later diagnostics in the attempt artifacts.
2. Check lineage and tape equality. Use `tape_mismatch` when they disagree on the input tape.
3. Check freshness. Use `slot_stale` for provider `STALE` or `stale_evidence` for independently verified host freshness failure.
4. Apply the provider status table. `ERROR`, `STALE` and `BUDGET_EXCEEDED` stop evaluation. For `INCOMPLETE` continue only to look for a bound divergence in the delivered observations. For `OK` and `FAIL` evaluate the host checks allowed by the policy.
5. Check policy, timing preconditions, required coverage, perception and pool custody. An empty reference timeline stops a comparison with `empty_reference` if the earlier gates passed. Evaluate admitted channels and retain unavailable required channels as abstaining checks when evaluation reached them.
6. Apply contrast and negative-control gates. A failed contrast yields `contrast_failed` and an empty `verdict.checks` array. Preserve diagnostic checks outside the verdict. An absent, unactivated or surviving required negative control only blocks `match`.
7. Resolve a provider `FAIL` against the host outcome as specified above. Record oracle independence and whether a match independently qualifies. Apply the shared receipt gate before using the receipt for live acceptance.
