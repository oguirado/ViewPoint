# @module {"id":"wa.learn.proto1_shard_summary","layer":2,"owner":"LEARN","deps":[],"book":"HB-WA-s4-learn"}
"""PROTO1 shard-manifest, runner, and completion gates; no I/O or guest trust.

A manifest is frozen before any execution. Receipts are untrusted guest reports;
independent host-side cleanup attestations are required for success. This module
never performs a network operation, opens a file, or starts a process.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Callable

MANIFEST_SCHEMA = "proto1.shard-manifest/v1"
RECEIPT_SCHEMA = "proto1.shard-receipt/v1"
COMPLETION_SCHEMA = "proto1.completion/v1"
SUCCESS = 0
INVALID = 2
EMPTY_OR_INCAPABLE = 3
UNAVAILABLE = 4
VERIFICATION_FAILED = 5
INDETERMINATE = 6
OPERATOR_HOLD = 7
_ALLOWED_CODES = frozenset({0, 2, 3, 4, 5, 6, 7})
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_COMMIT = re.compile(r"[a-f0-9]{40}\Z")
_RECEIPT_KEYS = (
    "schema", "repo", "source_commit", "manifest_sha256", "shard_id",
    "attempt_id", "selected_case_ids", "executed_case_ids", "tests_run",
    "failures", "errors", "skips", "unexpected_successes", "gate_exit_code",
    "stdout_sha256", "started_mono_ns", "finished_mono_ns", "cpu_ns",
    "cleanup", "host_metric", "reason",
)
_CLEANUP_KEYS = frozenset({"known", "verified", "remaining_bytes", "remaining_children"})
_METRIC_KEYS = frozenset({"name", "value", "unit", "evidence", "status"})
_MANIFEST_KEYS = frozenset({"schema", "repo", "source_commit", "shards", "obligations", "allowed_skip_case_ids"})


class GateRefusal(ValueError):
    """Refused before promotion: exit_code is a PROTO1 runner code, not an OS error."""

    def __init__(self, exit_code: int, reason: str):
        if exit_code not in _ALLOWED_CODES or exit_code == SUCCESS:
            raise ValueError("invalid refusal code")
        self.exit_code = exit_code
        self.reason = reason
        super().__init__(f"PROTO1 exit {exit_code}: {reason}")


def _require(ok: bool, code: int, reason: str) -> None:
    if not ok:
        raise GateRefusal(code, reason)


def _name(value: object, where: str) -> str:
    _require(type(value) is str and _IDENT.fullmatch(value) is not None, INVALID, f"invalid_{where}")
    return value


def _int(value: object, where: str, *, minimum: int = 0) -> int:
    _require(type(value) is int and value >= minimum, INVALID, f"invalid_{where}")
    return value


def _array(value: object, where: str) -> list:
    _require(type(value) in (list, tuple), INVALID, f"invalid_{where}")
    return list(value)


def _mapping(value: object, where: str, keys: frozenset[str] | None = None) -> Mapping:
    _require(isinstance(value, Mapping), INVALID, f"invalid_{where}")
    if keys is not None:
        _require(set(value) == keys, INVALID, f"unexpected_{where}_fields")
    return value


def _unique_names(raw: object, where: str, *, nonempty: bool = False) -> list[str]:
    result = [_name(x, where) for x in _array(raw, where)]
    _require(len(result) == len(set(result)), VERIFICATION_FAILED, f"duplicate_{where}")
    if nonempty:
        _require(bool(result), EMPTY_OR_INCAPABLE, f"empty_{where}")
    return result


def canonical_bytes(value: object) -> bytes:
    """Strict canonical JSON. No host diagnostics, float, sets or object coercion."""
    def project(obj: object) -> object:
        if obj is None or type(obj) in (str, bool, int):
            return obj
        if type(obj) in (list, tuple):
            return [project(v) for v in obj]
        if isinstance(obj, Mapping):
            _require(all(type(k) is str for k in obj), INVALID, "non_string_json_key")
            return {k: project(v) for k, v in obj.items()}
        raise GateRefusal(INVALID, "noncanonical_json_type")
    return json.dumps(project(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _normalize_draft(raw: object) -> dict:
    m = _mapping(raw, "manifest")
    _require(set(m) == _MANIFEST_KEYS, INVALID, "unexpected_manifest_fields")
    _require(m["schema"] == MANIFEST_SCHEMA, INVALID, "manifest_schema")
    repo = _name(m["repo"], "repo")
    _require("/" in repo and repo.count("/") == 1, INVALID, "repo_not_owner_name")
    commit = m["source_commit"]
    _require(type(commit) is str and _COMMIT.fullmatch(commit) is not None, INVALID, "source_commit")
    raw_shards = _array(m["shards"], "shards")
    _require(bool(raw_shards), EMPTY_OR_INCAPABLE, "empty_shards")
    shards = []
    shard_ids: set[str] = set()
    case_ids: set[str] = set()
    for raw_shard in raw_shards:
        s = _mapping(raw_shard, "shard", frozenset({"shard_id", "sequence", "selected_case_ids"}))
        sid = _name(s["shard_id"], "shard_id")
        _require(sid not in shard_ids, VERIFICATION_FAILED, "duplicate_shard_id")
        shard_ids.add(sid)
        sequence = _int(s["sequence"], "sequence")
        cases = _unique_names(s["selected_case_ids"], "selected_case_ids", nonempty=True)
        _require(not (case_ids & set(cases)), VERIFICATION_FAILED, "duplicate_planned_case")
        case_ids.update(cases)
        shards.append({"shard_id": sid, "sequence": sequence, "selected_case_ids": cases})
    shards.sort(key=lambda s: s["sequence"])
    _require([s["sequence"] for s in shards] == list(range(len(shards))),
             VERIFICATION_FAILED, "missing_sequence")
    obligations = []
    obligation_ids: set[str] = set()
    obligated: set[str] = set()
    for item in _array(m["obligations"], "obligations"):
        o = _mapping(item, "obligation", frozenset({"obligation_id", "case_ids"}))
        oid = _name(o["obligation_id"], "obligation_id")
        _require(oid not in obligation_ids, VERIFICATION_FAILED, "duplicate_obligation")
        obligation_ids.add(oid)
        covered = _unique_names(o["case_ids"], "obligation_case_ids", nonempty=True)
        _require(set(covered) <= case_ids, VERIFICATION_FAILED, "unplanned_obligation_case")
        obligated.update(covered)
        obligations.append({"obligation_id": oid, "case_ids": sorted(covered)})
    _require(bool(obligations), EMPTY_OR_INCAPABLE, "empty_obligations")
    _require(obligated == case_ids, VERIFICATION_FAILED, "incomplete_obligation_set")
    allowed = _unique_names(m["allowed_skip_case_ids"], "allowed_skip_case_ids")
    _require(set(allowed) <= case_ids, VERIFICATION_FAILED, "unplanned_allowed_skip")
    return {"schema": MANIFEST_SCHEMA, "repo": repo, "source_commit": commit,
            "shards": shards, "obligations": sorted(obligations, key=lambda o: o["obligation_id"]),
            "allowed_skip_case_ids": sorted(allowed)}


def freeze_manifest(draft: Mapping) -> dict:
    """Canonical immutable *content* pin; caller must retain this sealed snapshot."""
    body = _normalize_draft(draft)
    return {**body, "manifest_sha256": _digest(body)}


def validate_manifest(manifest: Mapping) -> dict:
    m = _mapping(manifest, "frozen_manifest")
    _require(set(m) == _MANIFEST_KEYS | {"manifest_sha256"}, INVALID, "manifest_not_frozen")
    expected = m["manifest_sha256"]
    _require(type(expected) is str and _SHA256.fullmatch(expected) is not None, INVALID, "manifest_digest_format")
    body = _normalize_draft({k: v for k, v in m.items() if k != "manifest_sha256"})
    _require(_digest(body) == expected, VERIFICATION_FAILED, "manifest_digest_mismatch")
    return {**body, "manifest_sha256": expected}


def _cleanup(raw: object) -> dict:
    m = _mapping(raw, "cleanup", _CLEANUP_KEYS)
    _require(type(m["known"]) is bool and type(m["verified"]) is bool,
             INVALID, "invalid_cleanup_flags")
    if m["known"]:
        b = _int(m["remaining_bytes"], "remaining_bytes")
        c = _int(m["remaining_children"], "remaining_children")
    else:
        _require(not m["verified"] and m["remaining_bytes"] is None and
                 m["remaining_children"] is None, INVALID, "unknown_cleanup_must_be_null")
        b, c = None, None
    _require(not m["verified"] or m["known"], INVALID, "unverified_cleanup_claim")
    return {"known": m["known"], "verified": m["verified"],
            "remaining_bytes": b, "remaining_children": c}


def _metric(raw: object) -> dict | None:
    if raw is None:
        return None
    m = _mapping(raw, "host_metric", _METRIC_KEYS)
    name = _name(m["name"], "metric_name")
    unit = _name(m["unit"], "metric_unit")
    _require(type(m["evidence"]) is str and bool(m["evidence"].strip()),
             INVALID, "metric_missing_evidence_or_reason")
    if m["value"] is None:
        _require(m["status"] == "AUSENTE", INVALID, "null_metric_needs_ausente")
    else:
        _int(m["value"], "metric_value")
        _require(m["status"] == "MEASURED", INVALID, "metric_not_measured")
    return {"name": name, "value": m["value"], "unit": unit,
            "evidence": m["evidence"], "status": m["status"]}


def validate_receipt(manifest: Mapping, raw: Mapping) -> dict:
    """Reject foreign identity, missing cases, false success, and malformed counters."""
    m = validate_manifest(manifest)
    r = _mapping(raw, "receipt", frozenset(_RECEIPT_KEYS))
    _require(r["schema"] == RECEIPT_SCHEMA, INVALID, "receipt_schema")
    _require(r["repo"] == m["repo"], VERIFICATION_FAILED, "foreign_repo")
    _require(r["source_commit"] == m["source_commit"], VERIFICATION_FAILED, "foreign_source_pin")
    _require(r["manifest_sha256"] == m["manifest_sha256"], VERIFICATION_FAILED, "foreign_manifest_digest")
    sid = _name(r["shard_id"], "shard_id")
    _name(r["attempt_id"], "attempt_id")
    planned = next((s for s in m["shards"] if s["shard_id"] == sid), None)
    _require(planned is not None, VERIFICATION_FAILED, "unplanned_shard")
    selected = _unique_names(r["selected_case_ids"], "selected_case_ids")
    executed = _unique_names(r["executed_case_ids"], "executed_case_ids")
    _require(selected == planned["selected_case_ids"], VERIFICATION_FAILED, "selected_case_sequence_mismatch")
    _require(executed == selected, VERIFICATION_FAILED, "missing_or_unplanned_executed_case")
    counts = {k: _int(r[k], k) for k in
              ("tests_run", "failures", "errors", "skips", "unexpected_successes")}
    _require(counts["tests_run"] > 0, EMPTY_OR_INCAPABLE, "claimed_execution_zero_tests")
    _require(counts["tests_run"] == len(executed), VERIFICATION_FAILED, "tests_case_count_mismatch")
    _require(sum(counts[k] for k in ("failures", "errors", "skips", "unexpected_successes")) <= counts["tests_run"],
             VERIFICATION_FAILED, "counters_exceed_tests")
    code = _int(r["gate_exit_code"], "gate_exit_code")
    _require(code in _ALLOWED_CODES, INVALID, "unknown_runner_code")
    if code == SUCCESS:
        _require(not any(counts[k] for k in ("failures", "errors", "skips", "unexpected_successes")),
                 VERIFICATION_FAILED, "false_success_status")
    if counts["skips"]:
        _require(set(selected) <= set(m["allowed_skip_case_ids"]),
                 VERIFICATION_FAILED, "disallowed_skip")
    out_hash = r["stdout_sha256"]
    _require(type(out_hash) is str and _SHA256.fullmatch(out_hash) is not None, INVALID, "stdout_hash")
    started = _int(r["started_mono_ns"], "started_mono_ns")
    finished = _int(r["finished_mono_ns"], "finished_mono_ns")
    _require(finished >= started, VERIFICATION_FAILED, "clock_regressed")
    _int(r["cpu_ns"], "cpu_ns")
    cleanup = _cleanup(r["cleanup"])
    metric = _metric(r["host_metric"])
    _require(type(r["reason"]) is str, INVALID, "invalid_receipt_reason")
    if metric is None:
        _require(bool(r["reason"].strip()), INVALID, "absent_metric_needs_reason")
    if code != SUCCESS:
        _require(bool(r["reason"].strip()), INVALID, "failed_run_needs_reason")
    result = dict(r)
    result["selected_case_ids"] = selected
    result["executed_case_ids"] = executed
    result["cleanup"] = cleanup
    result["host_metric"] = metric
    return {k: result[k] for k in _RECEIPT_KEYS}


def receipt_bytes(receipt: Mapping) -> bytes:
    """Format-only canonical receipt bytes (not a substitute for validation)."""
    _mapping(receipt, "receipt", frozenset(_RECEIPT_KEYS))
    return canonical_bytes({k: receipt[k] for k in _RECEIPT_KEYS})


@dataclass(frozen=True)
class HostCleanup:
    """Separately acquired host evidence; guest cleanup flags have no authority.

    Authentication of the evidence's author is the caller's trusted-host duty.
    The receipt content hash binds one exact attempt to the observation.
    """
    shard_id: str
    attempt_id: str
    receipt_sha256: str
    known: bool
    verified: bool
    remaining_bytes: int | None
    remaining_children: int | None
    evidence: str
    host_metric: Mapping | None = None


def _host_evidence(r: Mapping, value: HostCleanup | None) -> tuple[int, str, dict | None]:
    if value is None:
        return INDETERMINATE, "host_cleanup_absent", None
    _require(type(value) is HostCleanup, INVALID, "host_cleanup_not_attestation")
    _require(value.shard_id == r["shard_id"] and value.attempt_id == r["attempt_id"],
             VERIFICATION_FAILED, "foreign_host_cleanup_identity")
    _require(type(value.receipt_sha256) is str and
             value.receipt_sha256 == hashlib.sha256(receipt_bytes(r)).hexdigest(),
             VERIFICATION_FAILED, "unbound_host_cleanup")
    _require(type(value.evidence) is str and bool(value.evidence.strip()),
             INVALID, "host_cleanup_missing_evidence")
    observed = _cleanup({"known": value.known, "verified": value.verified,
                         "remaining_bytes": value.remaining_bytes,
                         "remaining_children": value.remaining_children})
    metric = _metric(value.host_metric)
    if not observed["known"] or not observed["verified"]:
        return INDETERMINATE, "host_cleanup_unverified", metric
    if observed["remaining_bytes"] or observed["remaining_children"]:
        return VERIFICATION_FAILED, "host_resources_remaining", metric
    return SUCCESS, "host_cleanup_accepted", metric


@dataclass(frozen=True)
class Reduction:
    exit_code: int
    accounting_closed: bool
    passed: bool
    manifest_sha256: str
    receipt_set_sha256: str
    totals: dict
    obligations_covered: tuple[str, ...]
    raw_shard_status: tuple[dict, ...]
    retained_shards: tuple[str, ...]
    host_metrics: tuple[dict, ...]
    reasons: tuple[str, ...]


def reduce_receipts(manifest: Mapping, receipts: Sequence[Mapping], *,
                    host_cleanups: Mapping[str, HostCleanup] | None = None) -> Reduction:
    """Exact accounting with refusal on all provenance/coverage conflicts.

    Known failed attempts close accounting but cannot pass. Unknown host cleanup
    blocks completion and requires resource retention, even if guest reports zero.
    """
    m = validate_manifest(manifest)
    _require(type(receipts) in (list, tuple), INVALID, "receipts_not_sequence")
    planned_ids = {s["shard_id"] for s in m["shards"]}
    _require(len(receipts) > 0, EMPTY_OR_INCAPABLE, "empty_receipts")
    seen_shards: set[str] = set()
    seen_attempts: set[tuple[str, str]] = set()
    accepted = []
    for raw in receipts:
        r = validate_receipt(m, raw)
        pair = (r["shard_id"], r["attempt_id"])
        _require(pair not in seen_attempts, VERIFICATION_FAILED, "duplicate_attempt_replay")
        _require(r["shard_id"] not in seen_shards, VERIFICATION_FAILED, "duplicate_shard_receipt")
        seen_attempts.add(pair)
        seen_shards.add(r["shard_id"])
        accepted.append(r)
    _require(seen_shards == planned_ids, VERIFICATION_FAILED, "missing_shard_sequence")
    _require(len(accepted) == len(m["shards"]), VERIFICATION_FAILED, "receipt_coverage_mismatch")
    cases = {x for r in accepted for x in r["executed_case_ids"]}
    planned_cases = {x for s in m["shards"] for x in s["selected_case_ids"]}
    _require(cases == planned_cases, VERIFICATION_FAILED, "missing_case_accounting")
    hosts = _mapping(host_cleanups or {}, "host_cleanups")
    _require(set(hosts) <= planned_ids, VERIFICATION_FAILED, "foreign_host_cleanup")
    total_keys = ("tests_run", "failures", "errors", "skips", "unexpected_successes")
    totals = {k: sum(r[k] for r in accepted) for k in total_keys}
    totals["selected_cases"] = sum(len(r["selected_case_ids"]) for r in accepted)
    totals["executed_cases"] = len(cases)
    reasons: list[str] = []
    retained: list[str] = []
    raw_status = []
    metrics = []
    closed = True
    passed = True
    status_code = SUCCESS
    for r in sorted(accepted, key=lambda x: (x["shard_id"], x["attempt_id"])):
        c, reason, host_metric = _host_evidence(r, hosts.get(r["shard_id"]))
        if c != SUCCESS:
            passed = False
            reasons.append(f"{r['shard_id']}:{reason}")
            if c == INDETERMINATE:
                closed = False
                retained.append(r["shard_id"])
            else:
                retained.append(r["shard_id"])
            status_code = max(status_code, c)
        if r["gate_exit_code"] != SUCCESS:
            passed = False
            reasons.append(f"{r['shard_id']}:runner_exit_{r['gate_exit_code']}")
            if r["gate_exit_code"] not in (SUCCESS, VERIFICATION_FAILED):
                closed = False
            status_code = max(status_code, r["gate_exit_code"])
        if any(r[k] for k in ("failures", "errors", "unexpected_successes", "skips")):
            passed = False
            status_code = max(status_code, VERIFICATION_FAILED)
            reasons.append(f"{r['shard_id']}:nonpassing_test_counters")
        raw_status.append({"shard_id": r["shard_id"], "attempt_id": r["attempt_id"],
                           "gate_exit_code": r["gate_exit_code"],
                           **{k: r[k] for k in total_keys},
                           "guest_cleanup": r["cleanup"]})
        if host_metric is None:
            metrics.append({"shard_id": r["shard_id"], "name": "host_metric",
                            "value": None, "status": "AUSENTE",
                            "reason": "AUSENTE: independent host metric not provided"})
        else:
            metrics.append({"shard_id": r["shard_id"], **host_metric})
    _require(bool(accepted), EMPTY_OR_INCAPABLE, "nothing_executed")
    if passed:
        _require(status_code == SUCCESS and not reasons and closed, VERIFICATION_FAILED, "inconsistent_pass")
    receipt_payload = {"manifest_sha256": m["manifest_sha256"],
                       "receipts": sorted(accepted, key=lambda r: (r["shard_id"], r["attempt_id"]))}
    covered = tuple(o["obligation_id"] for o in m["obligations"]
                    if set(o["case_ids"]) <= cases)
    _require(len(covered) == len(m["obligations"]), VERIFICATION_FAILED, "obligations_not_covered")
    return Reduction(status_code, closed, passed, m["manifest_sha256"],
                     _digest(receipt_payload), totals, covered, tuple(raw_status),
                     tuple(sorted(set(retained))), tuple(metrics), tuple(reasons))


def completion_certificate(manifest: Mapping, receipts: Sequence[Mapping], *,
                           host_cleanups: Mapping[str, HostCleanup] | None = None) -> dict:
    """Never accept a caller-crafted summary; recompute all proof obligations."""
    m = validate_manifest(manifest)
    reduction = reduce_receipts(m, receipts, host_cleanups=host_cleanups)
    _require(reduction.passed and reduction.accounting_closed and not reduction.retained_shards,
             reduction.exit_code or VERIFICATION_FAILED, "completion_not_proven")
    payload = {"schema": COMPLETION_SCHEMA, "repo": m["repo"],
               "source_commit": m["source_commit"], "manifest_sha256": m["manifest_sha256"],
               "receipt_set_sha256": reduction.receipt_set_sha256,
               "obligation_ids": list(reduction.obligations_covered),
               "tests_run": reduction.totals["tests_run"]}
    return {**payload, "certificate_sha256": _digest(payload)}


class ReceiptLedger:
    """Thread-safe first-writer claim; never double-count or replace an attempt."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed: dict[str, tuple[str, str]] = {}

    def claim(self, manifest: Mapping, receipt: Mapping) -> str:
        r = validate_receipt(manifest, receipt)
        digest = hashlib.sha256(receipt_bytes(r)).hexdigest()
        with self._lock:
            prev = self._claimed.get(r["shard_id"])
            if prev is None:
                self._claimed[r["shard_id"]] = (r["attempt_id"], digest)
                return "NEW"
            if prev == (r["attempt_id"], digest):
                return "IDENTICAL_REPLAY"  # no new accounting or side effects
            if prev[0] == r["attempt_id"]:
                raise GateRefusal(VERIFICATION_FAILED, "altered_attempt_replay")
            raise GateRefusal(VERIFICATION_FAILED, "shard_already_claimed")

    def count(self) -> int:
        with self._lock:
            return len(self._claimed)


@dataclass(frozen=True)
class CapabilityEvidence:
    available: bool
    evidence: str
    declared: tuple[str, ...]
    operator_hold: bool = False


@dataclass(frozen=True)
class ExecutionObservation:
    """Data from an external exec channel; never sufficient for completion alone."""
    executed_case_ids: tuple[str, ...]
    tests_run: int
    failures: int
    errors: int
    skips: int
    unexpected_successes: int
    driver_exit_code: int
    stdout: bytes
    guest_cleanup: Mapping
    host_metric: Mapping | None = None
    reason: str = "AUSENTE: host metrics not measured"


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    receipt: dict | None
    host_cleanup: HostCleanup | None
    reason: str


def run_shard(manifest: Mapping, shard_id: str, attempt_id: str, *,
              capability: CapabilityEvidence, executor: Callable[[tuple[str, ...]], ExecutionObservation],
              host_probe: Callable[[Mapping], HostCleanup] | None = None,
              monotonic_ns: Callable[[], int] = time.monotonic_ns,
              cpu_clock_ns: Callable[[], int] = time.process_time_ns) -> RunResult:
    """Invoke only a preflighted external executor; never infer tests from exit=0.

    Host-probe authority is supplied by the trusted caller, never by the guest.
    Failed/uncertain external executions retain an explicit non-success status.
    """
    m = validate_manifest(manifest)
    sid = _name(shard_id, "shard_id")
    aid = _name(attempt_id, "attempt_id")
    shard = next((s for s in m["shards"] if s["shard_id"] == sid), None)
    _require(shard is not None, INVALID, "unplanned_runner_shard")
    _require(type(capability) is CapabilityEvidence and type(capability.available) is bool,
             INVALID, "capability_evidence_required")
    _require(type(capability.evidence) is str, INVALID, "capability_evidence_invalid")
    _require(type(capability.operator_hold) is bool, INVALID, "operator_hold_invalid")
    declared = _unique_names(capability.declared, "capability_names")
    if capability.operator_hold:
        return RunResult(OPERATOR_HOLD, None, None, "operator_only_hold")
    required = {"offline", "test_execution", "exact_case_identity"}
    if not capability.available or not capability.evidence.strip() or not required <= set(declared):
        return RunResult(UNAVAILABLE, None, None, "runner_capability_unavailable")
    if not callable(executor):
        return RunResult(UNAVAILABLE, None, None, "runner_executor_unavailable")
    planned = tuple(shard["selected_case_ids"])
    if not planned:
        return RunResult(EMPTY_OR_INCAPABLE, None, None, "runner_empty_shard")
    try:
        start = monotonic_ns()
        cpu_start = cpu_clock_ns()
        outcome = executor(planned)
        finish = monotonic_ns()
        cpu_finish = cpu_clock_ns()
    except Exception:
        return RunResult(INDETERMINATE, None, None, "executor_or_clock_indeterminate")
    if type(outcome) is not ExecutionObservation:
        return RunResult(VERIFICATION_FAILED, None, None, "executor_invalid_observation")
    try:
        _int(start, "started_mono_ns")
        _int(finish, "finished_mono_ns")
        _int(cpu_start, "cpu_start")
        _int(cpu_finish, "cpu_finish")
        _require(finish >= start and cpu_finish >= cpu_start, VERIFICATION_FAILED, "runner_clock_regressed")
        _require(type(outcome.stdout) is bytes, INVALID, "stdout_not_bytes")
        executed = _unique_names(outcome.executed_case_ids, "executed_case_ids")
        _require(executed == list(planned), VERIFICATION_FAILED, "runner_case_sequence_mismatch")
        counts = {k: _int(getattr(outcome, k), k) for k in
                  ("tests_run", "failures", "errors", "skips", "unexpected_successes")}
        _require(counts["tests_run"] > 0, EMPTY_OR_INCAPABLE, "runner_zero_tests")
        _require(counts["tests_run"] == len(planned), VERIFICATION_FAILED, "runner_test_count_mismatch")
        _require(sum(counts[k] for k in ("failures", "errors", "skips", "unexpected_successes")) <= counts["tests_run"],
                 VERIFICATION_FAILED, "runner_counters_exceed_tests")
        _int(outcome.driver_exit_code, "driver_exit_code")
        guest_cleanup = _cleanup(outcome.guest_cleanup)
        metric = _metric(outcome.host_metric)
        _require(type(outcome.reason) is str, INVALID, "executor_reason")
    except GateRefusal as exc:
        return RunResult(exc.exit_code, None, None, exc.reason)
    nonpassing = any(counts[k] for k in ("failures", "errors", "skips", "unexpected_successes"))
    code = VERIFICATION_FAILED if nonpassing or outcome.driver_exit_code != 0 else SUCCESS
    if counts["skips"] and not set(planned) <= set(m["allowed_skip_case_ids"]):
        return RunResult(VERIFICATION_FAILED, None, None, "disallowed_skip")
    reason = outcome.reason or ("test_execution_failed" if code else "AUSENTE: no host measurements")
    candidate = {"schema": RECEIPT_SCHEMA, "repo": m["repo"],
                 "source_commit": m["source_commit"], "manifest_sha256": m["manifest_sha256"],
                 "shard_id": sid, "attempt_id": aid, "selected_case_ids": list(planned),
                 "executed_case_ids": executed, **counts, "gate_exit_code": code,
                 "stdout_sha256": hashlib.sha256(outcome.stdout).hexdigest(),
                 "started_mono_ns": start, "finished_mono_ns": finish,
                 "cpu_ns": cpu_finish - cpu_start, "cleanup": guest_cleanup,
                 "host_metric": metric, "reason": reason}
    try:
        candidate = validate_receipt(m, candidate)
    except GateRefusal as exc:
        return RunResult(exc.exit_code, None, None, exc.reason)
    attestation = None
    if host_probe is None:
        code = max(code, INDETERMINATE)
        reason = "host_cleanup_probe_absent"
    else:
        try:
            attestation = host_probe(candidate)
            h_code, h_reason, _ = _host_evidence(candidate, attestation)
        except Exception:
            h_code, h_reason = INDETERMINATE, "host_cleanup_probe_indeterminate"
            attestation = None
        code = max(code, h_code)
        if h_code:
            reason = h_reason
    if code != candidate["gate_exit_code"]:
        # Preserve a known test failure (5), even when cleanup later becomes
        # indeterminate (6). The aggregate exit still reports uncertainty.
        original = candidate["gate_exit_code"]
        receipt_code = original if original == VERIFICATION_FAILED else code
        candidate = {**candidate, "gate_exit_code": receipt_code,
                     "reason": candidate["reason"] + "; " + reason}
        # Attestation to an earlier candidate is not valid for the revised one.
        attestation = None
    return RunResult(code, candidate, attestation, reason)
