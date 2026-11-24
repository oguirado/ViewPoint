# SPDX-License-Identifier: AGPL-3.0-only
"""Evidence receipts and interruption reconciliation.

Receipts follow rea.slot_evidence_profile/v1: an attempt opened before
dispatch, a validated rea.slot_result/v1 exchange, and process-bound
observation custody.  A failed or interrupted attempt is retained; a
reconciliation creates a new receipt that references the earlier attempt
without mutating it.
"""

import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass

from ..compare.tools import _ComparisonReportWitness, _comparison_digest
from . import evidence_projection as projection
from .replay_lineage import validate_parent_episode_id, validate_replay_block

_PENDING = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending"
_PUBLISHED_REF = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2"
_PUBLISHED_ID = "https://metaviewer.local/contracts/v0.2/attempt_evidence_record.schema.json"
_PUBLISHED_SHA = "b3bfd6715abfa60a0866b655d23088d8bee09b0cf8e0a3b44c156e5692c19d8c"
_V03_REF = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.3"
_V03_ID = "https://metaviewer.local/contracts/v0.3/attempt_evidence_record.schema.json"
_V03_SHA = "9402c78a4780cab53d9ad9726b103d05e76b504fe693c16b572078c9d8c98dc7"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ABSTAIN_REASONS = {
    "control_refused", "uncertain_outcome", "exchange_invalid", "evidence_binding_rejected",
    "custody_unverified", "tape_mismatch", "slot_stale", "stale_evidence", "slot_error",
    "slot_incomplete", "slot_budget_exceeded", "slot_claim_only", "slot_host_disagreement",
    "empty_reference", "timeline_invalid", "clock_source_mismatch", "clock_source_unsuitable",
    "missing_clock_binding", "origin_mismatch", "missing_reference_coverage", "coverage_insufficient",
    "perception_ceiling", "perception_unavailable", "vision_pool_timeout", "vision_pool_not_granted",
    "lease_custody_unverified", "policy_undeclared", "unrepeatable_effect", "contrast_failed",
    "negative_control_missing", "negative_control_unactivated", "negative_control_survived",
    "determinism_mode_unsupported",
}
_PUBLISHED_MAP = {_PUBLISHED_REF: (_PUBLISHED_ID, _PUBLISHED_SHA), _PUBLISHED_ID: (_PUBLISHED_ID, _PUBLISHED_SHA)}
_V03_MAP = {_V03_REF: (_V03_ID, _V03_SHA), _V03_ID: (_V03_ID, _V03_SHA)}
_LIVE_PINS = {(_PUBLISHED_ID, _PUBLISHED_SHA), (_V03_ID, _V03_SHA)}
_LEVEL_RANK = {"none": 0, "cdp_atspi": 1, "vision": 2}
_CHECK_KINDS = {"event_timeline", "event_order", "event_coverage", "presentation_time", "pixels", "structured_value", "error_class", "state", "effect"}
_CHECK_OUTCOMES = {"match", "mismatch", "abstain"}
_DIVERGENCE_KEYS = {"event_id", "occurrence", "frame_index", "case_id", "reference_t_ms", "candidate_t_ms", "delta_ms", "note"}


def _shared_adapter(shared_schema_ref: str | None, shared_schema_sha256: str | None = None, shared_receipt_ref: str | None = None) -> dict:
    # No reference is trusted until an owner supplies authenticated readback.
    # This includes shared:fix, shared:fix-attempt and substituted artifact refs.
    del shared_receipt_ref
    pending = {
        "profile": "rea.slot_evidence_profile/v1",
        "binding_state": "pending_publication",
        "shared_schema_id": None,
        "shared_schema_sha256": None,
        "live_use_permitted": False,
    }
    ref = shared_schema_ref if isinstance(shared_schema_ref, str) else ""
    if not ref.strip() or ref == _PENDING or ref.endswith("@pending"):
        return dict(pending)
    for pin_map in (_PUBLISHED_MAP, _V03_MAP):
        if ref in pin_map:
            pub_id, pub_sha = pin_map[ref]
            if shared_schema_sha256 is not None and shared_schema_sha256 != pub_sha:
                return dict(pending)
            # Schema binding is offline. A reference alone supplies no
            # authenticated terminal readback (owner-pending: T01).
            return {
                "profile": "rea.slot_evidence_profile/v1",
                "binding_state": "bound",
                "shared_schema_id": pub_id,
                "shared_schema_sha256": pub_sha,
                "live_use_permitted": False,
            }
    # Arbitrary schema references never bind: only the published pin above
    # resolves. A caller-supplied sha-shaped value cannot create a live
    # binding for an unresolved reference.
    return dict(pending)


@dataclass(frozen=True)
class RetainedReadback:
    inspection: dict
    artifact: bytes
    index: dict
    profile: dict


def _retained_ref(value, prefix):
    if type(value) is not str or re.fullmatch(prefix + r"_[0-9a-f]{32}", value) is None:
        raise ValueError(f"expected a Tools {prefix} reference")
    return value


def read_object(inspector, attempt_id, entry, *, collect=False):
    """Validate all pages and the entire object, collecting only when needed."""
    length, digest = entry["bytes"], entry["sha256"]
    reference = _retained_ref(entry["object_ref"], "obj")
    offset = 0
    measured = hashlib.sha256()
    raw = bytearray() if collect else None
    while True:
        page = inspector.object_page(attempt_id, reference, offset=offset)
        if ((page.reference, page.total_bytes, page.sha256, page.offset) !=
                (reference, length, digest, offset)
                or type(page.data) is not bytes or offset + len(page.data) > length
                or (not page.data and offset < length)):
            raise ValueError("object page does not bind the requested object and offset")
        measured.update(page.data)
        if raw is not None:
            raw.extend(page.data)
        offset += len(page.data)
        if offset == length:
            break
    if measured.hexdigest() != digest:
        raise ValueError("readback object digest differs from the frozen index")
    return None if raw is None else bytes(raw)


def _canonical_document(raw):
    document = json.loads(raw)
    if type(document) is not dict or projection.record_bytes(document) != raw:
        raise ValueError("retained document must have canonical record bytes")
    return document


def verify_inspection(inspection, *, tenant, attempt_id, admission=None, start_raw=None):
    held = inspection["admission"]
    if (inspection["tenant"] != tenant or inspection["attempt_id"] != attempt_id
            or held["attempt_id"] != attempt_id or (admission is not None and held != admission)):
        raise ValueError("inspection does not bind the admitted tenant and attempt")
    start = inspection["start"]
    observed = projection.record_bytes(start)
    request = start.get("external_process", start)
    if (start["attempt_id"] != attempt_id or inspection["start_sha256"] != hashlib.sha256(observed).hexdigest()
            or (request["request_id"], request["request_sha256"]) !=
            (held["request_id"], held["request_sha256"]) or (start_raw is not None and start_raw != observed)):
        raise ValueError("inspection does not bind the exact start and normalized intent")
    return held


def read_retained(inspector, validator, validate_profile, *, tenant, attempt_id, admission=None,
                  start_raw=None, terminal_raw=None, artifact_raw=None, receipt_ref=None):
    """Independently verify the receipt, artifact and byte graph before publication."""
    inspection = inspector.inspect(attempt_id)
    admitted = verify_inspection(inspection, tenant=tenant, attempt_id=attempt_id,
                                 admission=admission, start_raw=start_raw)
    if inspection["state"] != "committed":
        raise ValueError("an unresolved attempt has no retained receipt")
    reference = _retained_ref(inspection["receipt_ref"], "rcp")
    if (receipt_ref is not None and reference != receipt_ref) or inspector.receipt(reference) != inspection:
        raise ValueError("receipt lookup does not match the authorized inspection")
    validator.validate_live_receipt_binding(_V03_REF, _V03_SHA, reference)
    terminal = inspection["terminal"]
    observed_terminal = projection.record_bytes(terminal)
    if (terminal["attempt_id"] != attempt_id or terminal["start_sha256"] != inspection["start_sha256"]
            or inspection["terminal_sha256"] != hashlib.sha256(observed_terminal).hexdigest()
            or (terminal_raw is not None and observed_terminal != terminal_raw)):
        raise ValueError("inspection does not bind the exact terminal to its start")
    artifact_meta = terminal["artifact"]
    if artifact_meta["content_kind"] != "object_index":
        raise ValueError("the retained REA artifact must be the frozen index")
    artifact_ref = _retained_ref(inspection["artifact_ref"], "art")
    raw = inspector.artifact(attempt_id, artifact_ref, length=artifact_meta["bytes"], digest=artifact_meta["sha256"])
    if ((len(raw), hashlib.sha256(raw).hexdigest()) != (artifact_meta["bytes"], artifact_meta["sha256"])
            or (artifact_raw is not None and raw != artifact_raw)):
        raise ValueError("artifact does not match the terminal")
    index = _canonical_document(raw)
    validator.validate_index(index)
    for key in ("attempt_id", "request_id", "request_sha256", "operation",
                "profile_schema", "profile_schema_sha256", "admission_sha256"):
        if index.get(key) != admitted[key]:
            raise ValueError(f"index does not bind admitted {key}")
    profile = None
    for entry in index["objects"]:
        is_profile = entry["role"] == "rea_profile"
        if is_profile and entry["bytes"] > projection.RECORD_LIMIT:
            raise ValueError("retained profile is too large")
        body = read_object(inspector, attempt_id, entry, collect=is_profile)
        if is_profile:
            profile = _canonical_document(body)
    if profile is None or validate_profile(admitted, profile, terminal) is not True:
        raise ValueError("readback does not bind the approved REA profile")
    projection.verify_index_objects(profile, index["objects"])
    timelines = {entry["side"]: entry["object_ref"] for entry in index["objects"]
                 if entry["role"] == "timeline_events"}
    if timelines != {side: value.get("events_object_ref")
                     for side, value in terminal.get("timeline", {}).items()}:
        raise ValueError("terminal timeline references do not bind the verified index sides")
    return RetainedReadback(inspection, raw, index, profile)


def publish_retained_receipt(wrapper, readback):
    """Return a publishable receipt only for an independently verified observation."""
    profile = wrapper["receipt"]
    if not _accepted_observation(profile, wrapper["custody"]):
        return None
    if wrapper["custody"].get("terminal_retention_failed") is True:
        return None
    return _retained_outer_receipt(profile, readback)


def _retained_outer_receipt(profile, readback):
    if type(readback) is not RetainedReadback or readback.profile != profile:
        raise ValueError("readback does not bind the exact REA receipt")
    adapter = _shared_adapter(_V03_REF, _V03_SHA)
    if profile["shared_receipt_adapter"] != adapter:
        raise ValueError("the inner profile must bind the frozen schema without a receipt back-reference")
    outer = copy.deepcopy(profile)
    outer["shared_receipt_adapter"] = adapter | {
        "live_use_permitted": True, "shared_receipt_ref": readback.inspection["receipt_ref"],
    }
    return outer


def _validate_identifier(value, field_name: str) -> None:
    if type(value) is not str or len(value) < 1 or len(value) > 256 or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {field_name}: {value!r}")


def _is_identifier(value) -> bool:
    return type(value) is str and 1 <= len(value) <= 256 and _IDENTIFIER_RE.fullmatch(value) is not None


def _valid_first_divergence(div) -> bool:
    if not isinstance(div, dict) or not div:
        return False
    if set(div.keys()) - _DIVERGENCE_KEYS:
        return False
    for key in div:
        val = div[key]
        if key in ("event_id", "case_id"):
            if not _is_identifier(val):
                return False
        elif key in ("occurrence", "frame_index", "reference_t_ms", "candidate_t_ms"):
            if type(val) is not int or val < 0:
                return False
        elif key == "delta_ms":
            if type(val) is not int:
                return False
        elif key == "note":
            if type(val) is not str or len(val) > 2000:
                return False
    return True


def _valid_check(check) -> bool:
    if not isinstance(check, dict):
        return False
    if set(check.keys()) - {"check_id", "kind", "outcome", "detail", "first_divergence"}:
        return False
    if not _is_identifier(check.get("check_id")):
        return False
    if check.get("kind") not in _CHECK_KINDS:
        return False
    if check.get("outcome") not in _CHECK_OUTCOMES:
        return False
    if "detail" in check:
        detail = check["detail"]
        if type(detail) is not str or len(detail) > 2000:
            return False
    if check.get("outcome") == "mismatch":
        if "first_divergence" not in check:
            return False
        if not _valid_first_divergence(check["first_divergence"]):
            return False
    elif "first_divergence" in check:
        if not _valid_first_divergence(check["first_divergence"]):
            return False
    return True


def _verdict_checks_valid(verdict) -> bool:
    checks = verdict.get("checks") if isinstance(verdict, dict) else None
    if not isinstance(checks, list):
        return False
    if not checks:
        return False
    for check in checks:
        if not _valid_check(check):
            return False
    return True


def _independent_qualification_valid(verdict) -> bool:
    qualified = verdict.get("independent_qualification")
    if type(qualified) is not bool:
        return False
    if not qualified:
        return True
    if verdict.get("result") != "match":
        return False
    if verdict.get("oracle_independence") != "independent":
        return False
    controls = verdict.get("negative_controls")
    if not isinstance(controls, list) or not controls:
        return False
    for control in controls:
        if not isinstance(control, dict):
            return False
        if not _is_identifier(control.get("control_id")):
            return False
        if control.get("executed") is not True or control.get("activated") is not True:
            return False
        if control.get("outcome") != "rejected":
            return False
    return True


def _validate_episode_ids(parent_episode_id, reference_episode_id, candidate_episode_id) -> None:
    if parent_episode_id is not None:
        _validate_identifier(parent_episode_id, "parent_episode_id")
    if reference_episode_id is not None:
        _validate_identifier(reference_episode_id, "reference_episode_id")
    if candidate_episode_id is not None:
        _validate_identifier(candidate_episode_id, "candidate_episode_id")


def _valid_ticket(ticket, level) -> bool:
    if level != "vision":
        return False
    if not isinstance(ticket, dict):
        return False
    if ticket.get("projection") not in ("redacted", "restricted"):
        return False
    if ticket.get("status") != "granted":
        return False
    sha = ticket.get("lease_id_sha256")
    if type(sha) is not str or _SHA256_RE.fullmatch(sha) is None:
        return False
    front = ticket.get("front")
    if type(front) is not str or not front.strip():
        return False
    if ticket.get("projection") == "redacted" and "lease_id" in ticket:
        return False
    if ticket.get("projection") == "restricted":
        if type(ticket.get("lease_id")) is not str or not ticket["lease_id"]:
            return False
    return True


def _episode_for(operation: str, attempt_id: str, parent_episode_id: str | None, reference_episode_id: str | None, candidate_episode_id: str | None) -> dict:
    if operation == "replay":
        return {
            "episode_id": f"ep-{attempt_id}", "role": "candidate",
            "parent_episode_id": validate_parent_episode_id(parent_episode_id),
        }
    if operation == "compare":
        ep = {"episode_id": f"ep-{attempt_id}", "role": "comparison"}
        ep["reference_episode_id"] = reference_episode_id or f"ep-ref-{attempt_id}"
        ep["candidate_episode_id"] = candidate_episode_id or f"ep-cand-{attempt_id}"
        return ep
    return {"episode_id": f"ep-{attempt_id}", "role": "original"}


def _verdict(result: str, abstain_reason: str | None = None, abstain_detail: str = "", *, comparator_id: str = "host.compare", comparator_version: str = "1", policy_id: str = "policy:fixture@1", policy_version: str = "1") -> dict:
    if result == "abstain":
        reason = abstain_reason or "custody_unverified"
        if reason == "perception_denied":
            reason = "perception_ceiling"
        if reason not in _ABSTAIN_REASONS:
            reason = "custody_unverified"
    else:
        reason = None
    policy_sha = hashlib.sha256(f"{policy_id}@{policy_version}".encode()).hexdigest()
    v: dict = {
        "comparator_id": comparator_id,
        "comparator_version": comparator_version,
        "result": result,
        "policy": {"id": policy_id, "version": policy_version, "sha256": policy_sha},
        "checks": [],
        "contrast_ids": [],
        "negative_controls": [],
        "oracle_independence": "independent",
        "independent_qualification": False,
    }
    if result == "abstain":
        v["abstain_reason"] = reason
        v["abstain_detail"] = (abstain_detail or "")[:2000]
    return v


def _valid_slot_result(value) -> bool:
    if not isinstance(value, dict):
        return False
    if set(value.keys()) - {"schema", "status", "rc", "sha256", "path"}:
        return False
    if value.get("schema") != "rea.slot_result/v1":
        return False
    if type(value.get("rc")) is not int:
        return False
    if type(value.get("sha256")) is not str or _SHA256_RE.fullmatch(value["sha256"]) is None:
        return False
    if "path" in value and (type(value["path"]) is not str or not value["path"]):
        return False
    return _paired_slot_result(value.get("status", ""), value.get("rc", -1))


def build_attempt_receipt(
    *,
    attempt_id: str,
    request_id: str,
    operation: str = "record",
    slot_role: str = "verify",
    slot_id: str = "fixture-slot",
    slot_version: str = "1.0.0",
    tool_version: str | None = None,
    upstream_licence_id: str = "MIT",
    source_url: str = "https://example.com/fixture",
    use_disposition: str = "external_execution",
    provider_id: str | None = None,
    provider_revision: str | None = None,
    implementation_sha256: str | None = None,
    producer_kind: str = "external_slot",
    producer_id: str | None = None,
    producer_version: str | None = None,
    producer_implementation_sha256: str | None = None,
    process_bound: bool = False,
    perception_level: str = "none",
    perception_ceiling: str = "none",
    vision_pool_ticket: dict | None = None,
    slot_result: dict | None = None,
    control_outcome: str | None = None,
    started_at: str | None = None,
    parent_episode_id: str | None = None,
    reference_episode_id: str | None = None,
    candidate_episode_id: str | None = None,
    replay: dict | None = None,
    media: dict | None = None,
    timeline: dict | None = None,
    shared_schema_ref: str = _PENDING,
    shared_schema_sha256: str | None = None,
    shared_receipt_ref: str | None = None,
    manifest_sha256: str | None = None,
    comparator_id: str = "host.compare",
    comparator_version: str = "1",
    policy_id: str = "policy:fixture@1",
    policy_version: str = "1",
) -> dict:
    """Open a retained attempt before dispatch (schema-valid)."""
    if started_at is None:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if operation not in ("record", "replay", "compare"):
        operation = "record"
    _validate_identifier(attempt_id, "attempt_id")
    _validate_identifier(request_id, "request_id")
    _validate_identifier(slot_id, "slot_id")
    if provider_id is not None:
        _validate_identifier(provider_id, "provider_id")
    producer_id_resolved = producer_id or slot_id
    _validate_identifier(producer_id_resolved, "producer_id")
    if control_outcome is not None:
        _validate_identifier(control_outcome, "control_outcome")
    if producer_kind not in ("external_slot", "host_mini_tool", "host_level"):
        raise ValueError(f"invalid producer_kind: {producer_kind!r}")
    if perception_level not in _LEVEL_RANK:
        raise ValueError(f"invalid perception_level: {perception_level!r}")
    if perception_ceiling not in _LEVEL_RANK:
        raise ValueError(f"invalid perception_ceiling: {perception_ceiling!r}")
    # Retain the actual ceiling verbatim; cap the effective level at the
    # ceiling instead of promoting the ceiling (schema requires
    # ceiling >= level, and the ceiling records what was in force).
    _validate_episode_ids(parent_episode_id, reference_episode_id, candidate_episode_id)
    attempt: dict = {
        "attempt_id": attempt_id,
        "operation": operation,
        "opened_before_dispatch": True,
        "request_id": request_id,
    }
    if started_at is not None:
        attempt["started_at"] = started_at
    # slot_role is only meaningful when a slot ran; keep it for all here
    # but omit when it would violate the role enum (defensive).
    _ROLES = {"ingest", "sample", "segment", "scene", "track", "atlas", "fit", "rules", "replay", "binary_ingest", "binary_lift", "binary_model", "game_model", "model", "spec", "plan", "scaffold", "export", "verify", "report"}
    if slot_role in _ROLES:
        attempt["slot_role"] = slot_role
    if slot_result is not None:
        if not _valid_slot_result(slot_result):
            raise ValueError("invalid slot_result")
        if slot_role not in _ROLES:
            raise ValueError("slot_result requires a valid slot_role")
        attempt["slot_result"] = copy.deepcopy(slot_result)
    if control_outcome is not None:
        attempt["control_outcome"] = control_outcome
    if operation == "replay":
        # replay marker is stored as reconciles pointer on the new receipt,
        # not here; attempt lineage uses reconciles_attempt_id when needed.
        pass
    slot: dict = {
        "tool_id": slot_id,
        "tool_version": (tool_version or slot_version),
        "upstream_licence_id": upstream_licence_id,
        "source_url": source_url,
        "user_installed": True,
        "out_of_process": True,
        "use_disposition": use_disposition,
    }
    if provider_id is not None:
        slot["provider_id"] = provider_id
    if provider_revision is not None:
        slot["provider_revision"] = provider_revision
    if implementation_sha256:
        slot["implementation_sha256"] = implementation_sha256
    if manifest_sha256:
        # Only include when it is a concrete digest (schema requires sha256).
        if len(manifest_sha256) == 64 and all(c in "0123456789abcdef" for c in manifest_sha256.lower()):
            slot["manifest_sha256"] = manifest_sha256.lower()
    producer: dict = {"kind": producer_kind, "id": producer_id_resolved, "process_bound": bool(process_bound)}
    if producer_version is not None:
        producer["version"] = producer_version
    if producer_implementation_sha256:
        producer["implementation_sha256"] = producer_implementation_sha256
    # Cap the requested level at the retained ceiling, then apply the
    # vision-ticket gate. A request above the ceiling degrades to the
    # ceiling level and never borrows a ticket.
    ceiling_rank = _LEVEL_RANK[perception_ceiling]
    requested_rank = _LEVEL_RANK[perception_level]
    capped_rank = ceiling_rank if ceiling_rank < requested_rank else requested_rank
    capped_level = [name for name, rank in _LEVEL_RANK.items() if rank == capped_rank][0]
    effective_level = capped_level
    clean_ticket = None
    if capped_level == "vision":
        if _valid_ticket(vision_pool_ticket, "vision"):
            clean_ticket = copy.deepcopy(vision_pool_ticket)
        else:
            effective_level = "none"
            clean_ticket = None
    else:
        effective_level = capped_level
        clean_ticket = None
    obs: dict = {
        "perception_level": effective_level,
        "perception_ceiling": perception_ceiling,
        "outside_capsule": True,
        "producer": producer,
    }
    if clean_ticket is not None:
        obs["vision_pool_ticket"] = clean_ticket
    receipt: dict = {
        "attempt": attempt,
        "episode": _episode_for(operation, attempt_id, parent_episode_id, reference_episode_id, candidate_episode_id),
        "observation": obs,
        "slot": slot,
        "shared_receipt_adapter": _shared_adapter(shared_schema_ref, shared_schema_sha256, shared_receipt_ref),
    }
    if operation == "replay":
        receipt["replay"] = validate_replay_block(replay)
    for group, value in (("media", media), ("timeline", timeline)):
        if value is None:
            continue
        if not isinstance(value, dict) or not value or set(value) - {"reference", "candidate"}:
            raise ValueError(f"{group} must name reference and/or candidate sides")
        receipt[group] = copy.deepcopy(value)
    if operation == "compare":
        # Compare always carries a verdict; default to abstain until finalized.
        receipt["verdict"] = _verdict("abstain", "custody_unverified", "attempt opened before dispatch", comparator_id=comparator_id, comparator_version=comparator_version, policy_id=policy_id, policy_version=policy_version)
    return receipt


def _paired_slot_result(status: str, rc: int) -> bool:
    pairs = {"OK": 0, "ERROR": 2, "INCOMPLETE": 3, "STALE": 4, "FAIL": 5, "BUDGET_EXCEEDED": 6}
    return pairs.get(status) == rc


def _verify_custody(custody: dict | None, *, implementation_sha256: str | None = None, provider_id: str | None = None, request_id: str | None = None, observed_sha256: str | None = None, observation_level: str | None = None, observation_ticket: dict | None = None) -> bool:
    """Strict custody gate: identity, digest, cleanup, timeout, overflow, lease."""
    if not isinstance(custody, dict):
        return False
    bound = custody.get("bound_process_id")
    obs = custody.get("observed_process_id")
    if type(bound) is not int or type(obs) is not int or bound <= 0 or obs <= 0 or bound != obs:
        return False
    bound_st = custody.get("bound_starttime")
    obs_st = custody.get("observed_starttime")
    if type(bound_st) is not int or type(obs_st) is not int or bound_st != obs_st:
        return False
    if custody.get("cleanup_observed") is not True:
        return False
    if custody.get("timed_out") is True:
        return False
    if custody.get("producer_binding_verified") is not True:
        return False
    if custody.get("artifact_digest_verified") is not True:
        return False
    if custody.get("overflow") is True:
        return False
    if custody.get("stdout_truncated") is True:
        return False
    if custody.get("lease_custody_unverified") is True:
        return False
    if type(implementation_sha256) is not str or _SHA256_RE.fullmatch(implementation_sha256) is None:
        return False
    if custody.get("observed_sha256") != implementation_sha256:
        return False
    if observed_sha256 is not None and observed_sha256 != implementation_sha256:
        return False
    if type(provider_id) is not str or not provider_id or custody.get("provider_id") != provider_id:
        return False
    if type(request_id) is not str or not request_id or custody.get("request_id") != request_id:
        return False
    if observation_level == "vision":
        if not _valid_ticket(observation_ticket, "vision"):
            return False
    return True


def _valid_comparison_verdict(verdict):
    if not _verdict_checks_valid(verdict) or not _independent_qualification_valid(verdict):
        return False
    outcomes = {item["outcome"] for item in verdict["checks"]}
    if verdict.get("result") == "match":
        return outcomes == {"match"}
    return verdict.get("result") == "mismatch" and "mismatch" in outcomes


def _offline_adapter_bound(receipt, custody):
    adapter = receipt.get("shared_receipt_adapter")
    return (
        isinstance(adapter, dict) and custody.get("offline_fixture") is True
        and adapter.get("binding_state") == "bound"
        and (adapter.get("shared_schema_id"), adapter.get("shared_schema_sha256")) in _LIVE_PINS
        and adapter.get("live_use_permitted") is False and "shared_receipt_ref" not in adapter
        and custody.get("terminal_retention_failed") is not True
    )


def _bound_comparison(receipt, custody, verdict):
    witness = custody.get("comparison_report")
    if type(witness) is not _ComparisonReportWitness or not _offline_adapter_bound(receipt, custody):
        return False
    attempt = receipt["attempt"]
    episode = receipt["episode"]
    policy = verdict.get("policy")
    if not (
        witness.receipt.request_id == attempt.get("request_id")
        and witness.receipt.attempt_id == attempt.get("attempt_id")
        and witness.reference_episode_id == episode.get("reference_episode_id")
        and witness.candidate_episode_id == episode.get("candidate_episode_id")
        and isinstance(policy, dict) and witness.policy_sha256 == policy.get("sha256")
    ):
        return False
    try:
        return (witness.verdict_sha256 == _comparison_digest(verdict)
                and witness.slot_result_digest == _comparison_digest(attempt.get("slot_result")))
    except (TypeError, ValueError, RecursionError):
        return False


def finalize_receipt(attempt: dict, slot_result: dict | None, *, custody: dict | None = None, implementation_sha256: str | None = None, verdict: dict | None = None) -> dict:
    """Attach the validated slot_result and custody to an opened attempt.

    Only a schema-valid paired provider result is retained in
    attempt.slot_result.  Custody verification is recomputed; a mismatch
    forces producer_binding_verified=False so evidence_accepted() fails.
    The retained result digest must match the witnessed result digest,
    and a decisive verdict is downgraded when custody, control or the
    exchange fails (first applicable STATUS_MAPPING reason).
    """
    receipt = copy.deepcopy(attempt)
    adapter = receipt["shared_receipt_adapter"]
    if "shared_receipt_ref" in adapter or adapter.get("live_use_permitted") is not False:
        adapter.pop("shared_receipt_ref", None)
        adapter["live_use_permitted"] = False
        if receipt["attempt"].get("control_outcome") is None:
            receipt["attempt"]["control_outcome"] = "UNCERTAIN_OUTCOME"
        custody = None
    if _valid_slot_result(slot_result) and "slot_role" in receipt.get("attempt", {}):
        receipt["attempt"]["slot_result"] = copy.deepcopy(slot_result)
    else:
        receipt["attempt"].pop("slot_result", None)
    retained = receipt["attempt"].get("slot_result")
    # Re-verify producer custody when custody is supplied: the runtime
    # itself must downgrade a foreign/wrong process, not the test (issue 1).
    custody_ok = False
    result_digest_ok = False
    lease_unverified = False
    if custody is not None:
        impl = implementation_sha256 or receipt.get("slot", {}).get("implementation_sha256")
        prov = receipt.get("slot", {}).get("provider_id")
        req = receipt.get("attempt", {}).get("request_id")
        observed_sha = custody.get("observed_sha256")
        obs_level = receipt.get("observation", {}).get("perception_level")
        obs_ticket = receipt.get("observation", {}).get("vision_pool_ticket")
        custody_ok = _verify_custody(custody, implementation_sha256=impl, provider_id=prov, request_id=req, observed_sha256=observed_sha, observation_level=obs_level, observation_ticket=obs_ticket)
        lease_unverified = custody.get("lease_custody_unverified") is True
        if isinstance(retained, dict):
            held = custody.get("result_sha256")
            if type(held) is str and _SHA256_RE.fullmatch(held) is not None and held == retained.get("sha256"):
                result_digest_ok = True
        bound = bool(custody_ok and custody.get("producer_binding_verified") is True)
        if isinstance(retained, dict) and (not bound or not result_digest_ok):
            receipt["attempt"].pop("slot_result")
            bound = False
        try:
            receipt["observation"]["producer"]["process_bound"] = bound
        except Exception:
            pass
        receipt_custody = copy.deepcopy(custody)
        receipt_custody["producer_binding_verified"] = bool(bound)
        receipt["_custody"] = receipt_custody
    else:
        bound = False
        receipt["attempt"].pop("slot_result", None)
        receipt["observation"]["producer"]["process_bound"] = False
    if receipt["attempt"].get("operation") == "compare":
        if verdict is not None:
            current = _normalize_verdict(verdict)
        elif "verdict" in receipt:
            current = _normalize_verdict(receipt["verdict"])
        else:
            current = None
        if current is not None:
            control = receipt["attempt"].get("control_outcome")
            sr = receipt["attempt"].get("slot_result")
            # Evaluation order: control, custody, exchange, provider status,
            # then admitted-check validity. Downgrade anything decisive that
            # depends on a failed earlier gate.
            if current.get("result") != "abstain":
                reason = None
                detail = ""
                if control == "UNCERTAIN_OUTCOME":
                    reason, detail = "uncertain_outcome", "prior attempt uncertain"
                elif control == "PERCEPTION_DENIED":
                    reason, detail = "perception_ceiling", "perception refused"
                elif control is not None:
                    reason, detail = "control_refused", "attempt refused"
                elif lease_unverified:
                    reason, detail = "lease_custody_unverified", "vision lease custody unverified"
                elif not custody_ok:
                    reason, detail = "custody_unverified", "producer custody not verified"
                elif isinstance(retained, dict) and not result_digest_ok:
                    reason, detail = "custody_unverified", "result digest does not match custody witness"
                elif not isinstance(sr, dict):
                    reason, detail = "exchange_invalid", "no attributable validated provider result"
                elif isinstance(sr, dict) and sr.get("status") in ("ERROR", "STALE", "BUDGET_EXCEEDED"):
                    reason = {"ERROR": "slot_error", "STALE": "slot_stale", "BUDGET_EXCEEDED": "slot_budget_exceeded"}[sr["status"]]
                    detail = "provider status bars a decisive verdict"
                elif isinstance(sr, dict) and sr.get("status") in ("INCOMPLETE", "FAIL"):
                    if current.get("result") == "match":
                        reason = "slot_incomplete" if sr.get("status") == "INCOMPLETE" else "slot_claim_only"
                        detail = "provider outcome without host divergence"
                    elif current.get("result") == "mismatch" and (not _verdict_checks_valid(current) or not _independent_qualification_valid(current)):
                        reason = "slot_incomplete" if sr.get("status") == "INCOMPLETE" else "slot_claim_only"
                        detail = "provider outcome without admitted divergence"
                if reason is None and not _valid_comparison_verdict(current):
                    reason, detail = "custody_unverified", "decisive verdict lacks qualified admitted checks"
                if reason is None and not _bound_comparison(receipt, custody, current):
                    reason, detail = "custody_unverified", "comparison has no bound host report"
                if reason is not None:
                    current = _verdict("abstain", reason, current.get("abstain_detail") or detail, comparator_id=current.get("comparator_id", "host.compare"), comparator_version=current.get("comparator_version", "1"))
            receipt["verdict"] = copy.deepcopy(current)
    return _strip_private(receipt)


def _normalize_verdict(verdict: dict) -> dict:
    v = copy.deepcopy(verdict)
    result = v.get("result", "abstain")
    if result == "abstain":
        reason = v.get("abstain_reason") or "custody_unverified"
        if reason == "perception_denied":
            reason = "perception_ceiling"
        if reason not in _ABSTAIN_REASONS:
            reason = "custody_unverified"
        v["abstain_reason"] = reason
        v["abstain_detail"] = str(v.get("abstain_detail", "") or "")[:2000]
    else:
        v.pop("abstain_reason", None)
        v.pop("abstain_detail", None)
    if "comparator_id" not in v or "comparator_version" not in v or "policy" not in v or "checks" not in v:
        base = _verdict(result, v.get("abstain_reason"), v.get("abstain_detail", ""), comparator_id=v.get("comparator_id", "host.compare"), comparator_version=v.get("comparator_version", "1"))
        for key in ("checks", "contrast_ids", "negative_controls", "oracle_independence", "independent_qualification"):
            if key in v:
                base[key] = v[key]
        if "policy" in v:
            base["policy"] = v["policy"]
        v = base
        if result == "abstain" and v.get("abstain_reason") == "perception_denied":
            v["abstain_reason"] = "perception_ceiling"
    return v


def _strip_private(receipt: dict) -> dict:
    receipt.pop("_custody", None)
    return receipt


def _canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def reconcile_run(
    interrupted: dict,
    *,
    new_attempt_id: str,
    request_id: str,
    reason: str = "UNCERTAIN_OUTCOME",
    outcome: str = "abstain",
    detail: str = "Interrupted attempt reconciled; new receipt references prior attempt.",
    started_at: str | None = None,
    custody: dict | None = None,
    cleanup_observed: bool | None = None,
    strict: bool = False,
) -> dict:
    """Create a new receipt that reconciles an interrupted attempt.

    Accepts only a runtime wrapper {"receipt":..., "custody":...} with a
    trusted separate custody witness, or a bare receipt with the witness
    supplied explicitly via custody. A custody dict embedded in the
    receipt itself is caller-asserted and never counts as a witness.
    Fails closed unless the witness binds the prior process and cleanup;
    the prior receipt is never mutated.
    """
    if type(interrupted) is not dict:
        raise ValueError("interrupted must be a receipt dict")
    # Validate identifiers and reason before any other work.
    _validate_identifier(new_attempt_id, "new_attempt_id")
    _validate_identifier(request_id, "request_id")
    if type(reason) is not str or reason != "UNCERTAIN_OUTCOME":
        raise ValueError("reconciliation requires reason=UNCERTAIN_OUTCOME")
    if type(outcome) is not str or outcome != "abstain":
        raise ValueError("reconciliation outcome must be abstain")
    if type(detail) is not str:
        raise ValueError("reconciliation detail must be a string")
    inner_receipt: dict | None = None
    prior_custody: dict | None = None
    if isinstance(interrupted.get("receipt"), dict) and isinstance(interrupted.get("custody"), dict):
        inner_receipt = interrupted["receipt"]
        prior_custody = interrupted["custody"]
        if custody is not None:
            prior_custody = custody
    elif "attempt" in interrupted:
        inner_receipt = interrupted
        if custody is not None:
            prior_custody = custody
        # Embedded interrupted["custody"] is caller-asserted; ignore it.
    if inner_receipt is None or "attempt" not in inner_receipt:
        raise ValueError("interrupted must be a receipt dict with an attempt")
    prev = inner_receipt["attempt"]
    prev_attempt_id = prev.get("attempt_id")
    # Every reconciliation requires an uncertain prior attempt with a
    # trusted identity-bound cleanup witness (clean success never
    # reconciles; a caller-asserted cleanup flag is not a witness).
    if type(prev_attempt_id) is not str or not prev_attempt_id:
        raise ValueError("prior attempt_id is required")
    if new_attempt_id == prev_attempt_id:
        raise ValueError("reconciliation requires a distinct new attempt_id")
    prior_outcome = prev.get("control_outcome")
    if prior_outcome != "UNCERTAIN_OUTCOME":
        raise ValueError("reconciliation requires a prior UNCERTAIN_OUTCOME attempt")
    if not isinstance(prior_custody, dict):
        raise ValueError("reconciliation requires a trusted cleanup witness")
    bound_id = prior_custody.get("bound_process_id")
    obs_id = prior_custody.get("observed_process_id")
    if type(bound_id) is not int or type(obs_id) is not int or bound_id <= 0 or obs_id <= 0 or bound_id != obs_id:
        raise ValueError("cleanup witness must bind the prior process identity")
    bound_st = prior_custody.get("bound_starttime")
    obs_st = prior_custody.get("observed_starttime")
    if type(bound_st) is not int or type(obs_st) is not int or bound_st != obs_st:
        raise ValueError("cleanup witness must bind the prior process starttime")
    # The trusted witness must name the retained receipt's request,
    # provider and implementation identity. A caller flag cannot supply it.
    prior_req = prev.get("request_id")
    prior_slot = inner_receipt.get("slot", {}) if isinstance(inner_receipt.get("slot"), dict) else {}
    prior_prov = prior_slot.get("provider_id")
    prior_impl = prior_slot.get("implementation_sha256")
    if type(prior_req) is not str or not prior_req or prior_custody.get("request_id") != prior_req:
        raise ValueError("cleanup witness must bind the prior request_id")
    if type(prior_prov) is not str or not prior_prov or prior_custody.get("provider_id") != prior_prov:
        raise ValueError("cleanup witness must bind the prior provider_id")
    if type(prior_impl) is not str or _SHA256_RE.fullmatch(prior_impl) is None:
        raise ValueError("cleanup witness requires a prior implementation digest")
    if prior_custody.get("observed_sha256") != prior_impl:
        raise ValueError("cleanup witness must bind the prior implementation digest")
    if prior_custody.get("cleanup_observed") is not True:
        raise ValueError("reconciliation requires verified cleanup_observed")
    if cleanup_observed is not None and (cleanup_observed is not True or cleanup_observed is not prior_custody.get("cleanup_observed")):
        raise ValueError("reconciliation requires verified cleanup_observed")
    if started_at is None:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    prior_digest = receipt_digest(inner_receipt)
    if prior_custody.get("prior_attempt_id") != prev_attempt_id:
        raise ValueError("cleanup witness must bind the prior attempt_id")
    if prior_custody.get("prior_receipt_sha256") != prior_digest:
        raise ValueError("cleanup witness must bind the exact prior receipt digest")
    prev_slot = inner_receipt.get("slot", {})
    prev_obs = inner_receipt.get("observation", {})
    prev_prod = prev_obs.get("producer", {}) if isinstance(prev_obs, dict) else {}
    prev_op = prev.get("operation", "record")
    prev_adapter = inner_receipt.get("shared_receipt_adapter", {})
    prev_verdict = inner_receipt.get("verdict", {})
    if prev_adapter.get("binding_state") == "bound" and prev_adapter.get("shared_schema_id") and prev_adapter.get("shared_schema_sha256"):
        shared_ref = prev_adapter["shared_schema_id"]
        shared_sha = prev_adapter["shared_schema_sha256"]
    else:
        shared_ref = _PENDING
        shared_sha = None
    receipt = build_attempt_receipt(
        attempt_id=new_attempt_id,
        request_id=request_id,
        operation=prev_op,
        slot_role=prev.get("slot_role", "verify"),
        slot_id=prev_slot.get("tool_id", "fixture-slot"),
        slot_version=prev_slot.get("tool_version", "1.0.0"),
        tool_version=prev_slot.get("tool_version", "1.0.0"),
        upstream_licence_id=prev_slot.get("upstream_licence_id", "MIT"),
        source_url=prev_slot.get("source_url", "https://example.com/fixture"),
        use_disposition=prev_slot.get("use_disposition", "external_execution"),
        provider_id=prev_slot.get("provider_id") or (prev_prod.get("id") if isinstance(prev_prod, dict) else None),
        provider_revision=prev_slot.get("provider_revision") or (prev_prod.get("version") if isinstance(prev_prod, dict) else None),
        implementation_sha256=prev_slot.get("implementation_sha256"),
        producer_kind=prev_prod.get("kind", "external_slot") if isinstance(prev_prod, dict) and prev_prod.get("kind") in ("external_slot", "host_mini_tool", "host_level") else "external_slot",
        producer_id=prev_prod.get("id") if isinstance(prev_prod, dict) else None,
        producer_version=prev_prod.get("version") if isinstance(prev_prod, dict) else None,
        producer_implementation_sha256=prev_prod.get("implementation_sha256") if isinstance(prev_prod, dict) else None,
        process_bound=False,
        perception_level=prev_obs.get("perception_level", "none") if isinstance(prev_obs, dict) else "none",
        perception_ceiling=prev_obs.get("perception_ceiling", "none") if isinstance(prev_obs, dict) else "none",
        vision_pool_ticket=prev_obs.get("vision_pool_ticket") if isinstance(prev_obs, dict) else None,
        slot_result=None,
        control_outcome=None,
        started_at=started_at,
        parent_episode_id=inner_receipt.get("episode", {}).get("parent_episode_id"),
        reference_episode_id=inner_receipt.get("episode", {}).get("reference_episode_id"),
        candidate_episode_id=inner_receipt.get("episode", {}).get("candidate_episode_id"),
        replay=inner_receipt.get("replay"),
        shared_schema_ref=shared_ref,
        shared_schema_sha256=shared_sha,
        manifest_sha256=prev_slot.get("manifest_sha256"),
        comparator_id=prev_verdict.get("comparator_id", "host.compare") if isinstance(prev_verdict, dict) else "host.compare",
        comparator_version=prev_verdict.get("comparator_version", "1") if isinstance(prev_verdict, dict) else "1",
    )
    receipt["attempt"]["reconciles_attempt_id"] = prev.get("attempt_id")
    receipt["attempt"]["control_outcome"] = reason
    # A reconciliation never carries the prior result; it links the prior
    # receipt by digest and lineage instead.
    receipt["attempt"].pop("slot_result", None)
    if prev_op == "compare":
        receipt["verdict"] = _verdict("abstain", "uncertain_outcome", detail, comparator_id=prev_verdict.get("comparator_id", "host.compare") if isinstance(prev_verdict, dict) else "host.compare", comparator_version=prev_verdict.get("comparator_version", "1") if isinstance(prev_verdict, dict) else "1")
    receipt["attempt"]["host_artifact_sha256"] = prior_digest
    return receipt


def evidence_accepted(receipt: dict, *, readback: RetainedReadback | None = None) -> bool:
    """Host verdict for whether the wrapper supplies an accepted observation.

    Requires a wrapper {"receipt":..., "custody":...} and all gates:
    strict custody, process_bound, no control/reconcile outcome, valid OK
    slot_result. Comparisons also require a bound host comparator report.
    A standalone receipt without custody never accepts.
    """
    if not isinstance(receipt, dict):
        return False
    inner = receipt.get("receipt") if isinstance(receipt.get("receipt"), dict) else receipt
    wrapper_form = isinstance(receipt.get("receipt"), dict) and isinstance(receipt.get("custody"), dict)
    custody = receipt.get("custody") if isinstance(receipt.get("custody"), dict) else None
    if not wrapper_form or not isinstance(custody, dict):
        return False
    if receipt.get("evidence_accepted") is False or not _accepted_observation(inner, custody):
        return False
    if readback is None:
        return _offline_adapter_bound(inner, custody)
    return (custody.get("terminal_retention_failed") is not True
            and inner == _retained_outer_receipt(readback.profile, readback))


def _accepted_observation(inner, custody):
    attempt = inner.get("attempt", {})
    if attempt.get("control_outcome") is not None:
        return False
    if attempt.get("reconciles_attempt_id") is not None:
        return False
    producer = inner.get("observation", {}).get("producer", {})
    if producer.get("process_bound") is not True:
        return False
    slot = inner.get("slot", {})
    impl = slot.get("implementation_sha256")
    prov = slot.get("provider_id")
    req = attempt.get("request_id")
    observed_sha = custody.get("observed_sha256")
    obs_level = inner.get("observation", {}).get("perception_level")
    obs_ticket = inner.get("observation", {}).get("vision_pool_ticket")
    if not _verify_custody(custody, implementation_sha256=impl, provider_id=prov, request_id=req, observed_sha256=observed_sha, observation_level=obs_level, observation_ticket=obs_ticket):
        return False
    sr = attempt.get("slot_result")
    if not _valid_slot_result(sr):
        return False
    if sr.get("status") != "OK" or sr.get("rc") != 0:
        return False
    held = custody.get("result_sha256")
    if type(held) is not str or _SHA256_RE.fullmatch(held) is None or held != sr.get("sha256"):
        return False
    if attempt.get("operation") == "compare":
        verdict = inner.get("verdict")
        return isinstance(verdict, dict) and _valid_comparison_verdict(verdict) and _bound_comparison(inner, custody, verdict)
    return True


def receipt_digest(receipt: dict) -> str:
    return hashlib.sha256(_canonical_bytes(receipt)).hexdigest()
