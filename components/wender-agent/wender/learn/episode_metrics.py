# @module {"id":"wa.learn.episode_metrics","layer":2,"owner":"LEARN","deps":["wa.learn.multiwin_scorecard"],"book":"HB-WA-s4-learn"}
"""Production owner for jq2b-j5-episode-metrics.

Implements episode battery metrics reduction, rational rate pooling, closed failure
mapping, and complete-episode uncertainty reporting under invariants INV J5 1 through INV J5 4:
- INV J5 1: Independently eligible equals completed plus refused plus incomplete;
  excluded and unsupported cases stay separately visible.
- INV J5 2: Completion requires matched engine outcomes and independent task postconditions.
- INV J5 3: Empty or absent denominators yield null rates and UNKNOWN, never perfection.
  Metrics preserve integer counts and rational numerator and denominator fields.
- INV J5 4: Safe refusal is correctness evidence without economic completion.
  Emitted attempts are distinct from local refusals, and unknown is distinct from zero.
  Confidence is descriptive complete-episode evidence only (status AUSENTE, unit complete-episode,
  method null, lower null, upper null); correlated frame rows never establish independent trials.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
import copy
from dataclasses import dataclass, field, fields
from fractions import Fraction
import hashlib
import json
import re
from typing import Any

from .multiwin_scorecard import Handle, Scorecard, WindowEvidence

__all__ = (
    "INV_J5_1",
    "INV_J5_2",
    "INV_J5_3",
    "INV_J5_4",
    "GATE_G_J5_2",
    "GATE_G_J5_4",
    "UNKNOWN_STATUS",
    "MEASURED_STATUS",
    "AUSENTE_STATUS",
    "DESIGNING_STATUS",
    "CLOSED_FAILURE_CODES",
    "EpisodeVerdict",
    "EpisodeRow",
    "EpisodeError",
    "WriteSpy",
    "PixelPersistenceError",
    "map_failure_code",
    "make_rational_rate",
    "reduce_episode",
    "report_uncertainty",
    "evaluate_gate_g_j5_2",
    "evaluate_gate_g_j5_4",
    "make_holdings_pause",
    "make_holdings_outcome",
    "validate_holdings_pause",
    "validate_holdings_outcome",
    "Handle",
    "WindowEvidence",
    "Scorecard",
)

INV_J5_1 = "INV J5 1: eligible == completed + refused + incomplete; excluded and unsupported cases stay separately visible"
INV_J5_2 = "INV J5 2: Completion requires matched engine outcomes and independent task postconditions"
INV_J5_3 = "INV J5 3: Empty or absent denominators yield null rates and UNKNOWN, never perfection"
INV_J5_4 = "INV J5 4: Safe refusal is correctness evidence without economic completion; confidence is AUSENTE"

GATE_G_J5_2 = "G-J5-2"
GATE_G_J5_4 = "G-J5-4"

UNKNOWN_STATUS = "UNKNOWN"
MEASURED_STATUS = "MEASURED"
AUSENTE_STATUS = "AUSENTE"
DESIGNING_STATUS = "DESIGNING"

CLOSED_FAILURE_CODES = frozenset({
    "empty_battery",
    "vacuous_denominators",
    "duplicate_episode_identity",
    "foreign_battery_membership",
    "missing_battery_membership",
    "unmatched_engine_outcomes",
    "unmet_postconditions",
    "emitted_attempt_not_local_refusal",
    "unsupported_refusal",
    "unsupported_episode",
    "terminal_count_mismatch",
    "denominator_mismatch",
    "missing_declared_completion_threshold",
    "threshold_not_met",
    "prerequisite_AUSENTE",
    "expected_failure_not_accepted",
    "conductor_admission_AUSENTE",
    "source_policy_relabel",
    "window_violation",
})

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")


class EpisodeError(ValueError):
    """Raised for invalid episode evidence, schema violations, or invariant failures."""


class PixelPersistenceError(RuntimeError):
    """Raised by WriteSpy when forbidden pixel, frame, or receipt persistence is attempted."""


def _require_int(val: Any, name: str, min_val: int = 0) -> int:
    if type(val) is not int or val < min_val:
        raise EpisodeError(f"{name} must be an integer >= {min_val}, got {val!r}")
    return val


def _require_str(val: Any, name: str) -> str:
    if not isinstance(val, str) or not val.strip():
        raise EpisodeError(f"{name} must be a non-empty string, got {val!r}")
    return val


def map_failure_code(raw: str) -> str:
    """Validate and canonicalize a failure token against CLOSED_FAILURE_CODES."""
    _require_str(raw, "failure_code")
    prefix = raw.split(":", 1)[0]
    if prefix in CLOSED_FAILURE_CODES:
        return raw
    return f"window_violation:{raw}"


def make_rational_rate(
    numerator: int | None,
    denominator: int | None,
    *,
    threshold: float | int | str | Fraction | None = None,
    pass_when: Callable[[int, int], bool] | None = None,
) -> dict[str, Any]:
    """Construct an INV-J5-3 rational rate preserving integer numerator and denominator.

    Absent or non-positive denominator yields None (null) and status UNKNOWN, never perfection.
    """
    if numerator is not None:
        _require_int(numerator, "numerator", min_val=0)
    if denominator is not None:
        _require_int(denominator, "denominator", min_val=0)

    if denominator is None or denominator <= 0 or numerator is None:
        return {
            "numerator": None,
            "denominator": None,
            "rate": None,
            "fraction": None,
            "status": UNKNOWN_STATUS,
            "passed": False,
        }

    if numerator > denominator:
        raise EpisodeError(f"numerator ({numerator}) cannot exceed denominator ({denominator})")

    frac = Fraction(numerator, denominator)
    rate_float = float(frac)

    passed = True
    if callable(pass_when):
        passed = bool(pass_when(numerator, denominator))
    elif threshold is not None:
        if isinstance(threshold, bool):
            raise EpisodeError("threshold cannot be a bool")
        thresh_frac = threshold if isinstance(threshold, Fraction) else Fraction(str(threshold))
        passed = frac >= thresh_frac

    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": rate_float,
        "fraction": f"{numerator}/{denominator}",
        "status": MEASURED_STATUS,
        "passed": passed,
    }


def _default_confidence() -> dict[str, Any]:
    return {
        "status": AUSENTE_STATUS,
        "unit": "complete-episode",
        "method": None,
        "lower": None,
        "upper": None,
    }


@dataclass(frozen=True)
class EpisodeVerdict:
    """Canonical verdict record for reduced episode batteries under INV J5 1--4."""
    eligible: int
    completed: int
    refused: int
    incomplete: int
    excluded: int
    failures: tuple[str, ...]
    metrics: dict[str, Any]
    reward: Any = None
    reward_status: str = DESIGNING_STATUS
    confidence: dict[str, Any] = field(default_factory=_default_confidence)
    passed: bool = False

    def __post_init__(self) -> None:
        _require_int(self.eligible, "eligible")
        _require_int(self.completed, "completed")
        _require_int(self.refused, "refused")
        _require_int(self.incomplete, "incomplete")
        _require_int(self.excluded, "excluded")
        if not isinstance(self.failures, (tuple, list)):
            raise EpisodeError("failures must be a tuple or list of strings")
        object.__setattr__(self, "failures", tuple(str(f) for f in self.failures))
        if not isinstance(self.metrics, Mapping):
            raise EpisodeError("metrics must be a mapping")
        if self.reward is not None:
            raise EpisodeError(f"reward must remain None (null), got {self.reward!r}")
        if self.reward_status != DESIGNING_STATUS:
            raise EpisodeError(f"reward_status must be {DESIGNING_STATUS!r}, got {self.reward_status!r}")
        if not isinstance(self.confidence, Mapping):
            raise EpisodeError("confidence must be a mapping")
        if (
            self.confidence.get("status") != AUSENTE_STATUS
            or self.confidence.get("unit") != "complete-episode"
            or self.confidence.get("method") is not None
            or self.confidence.get("lower") is not None
            or self.confidence.get("upper") is not None
        ):
            raise EpisodeError(
                "INV J5 4 violation: confidence must have status AUSENTE, unit complete-episode, "
                "and null method/lower/upper"
            )
        if type(self.passed) is not bool:
            raise EpisodeError("passed must be a bool")

        # INV J5 1 invariant check
        if self.eligible != self.completed + self.refused + self.incomplete:
            raise EpisodeError(
                f"INV J5 1 violation: eligible ({self.eligible}) != "
                f"completed ({self.completed}) + refused ({self.refused}) + incomplete ({self.incomplete})"
            )

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "completed": self.completed,
            "refused": self.refused,
            "incomplete": self.incomplete,
            "excluded": self.excluded,
            "failures": list(self.failures),
            "metrics": copy.deepcopy(self.metrics),
            "reward": self.reward,
            "reward_status": self.reward_status,
            "confidence": copy.deepcopy(self.confidence),
            "passed": self.passed,
        }


@dataclass(frozen=True)
class EpisodeRow:
    """Structured container for single-episode evidence input to reduce_episode."""
    episode_id: str
    status: str
    terminal: str | None = None
    expected_terminal: str | None = None
    engine_outcomes: dict[str, Any] | None = None
    expected_outcomes: dict[str, Any] | None = None
    postconditions_met: bool = False
    is_safe_refusal: bool = False
    is_emitted_attempt: bool = False
    is_local_refusal: bool = False
    supported_frames: int = 1
    exact_frames: int = 1
    trail_stamps: int = 1
    matched_stamps: int = 1
    frame_seq: int | None = None
    supported: bool = True
    violations: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class WriteSpy:
    """Security audit spy preventing raw frames, pixels, or receipts from leaking to disk."""
    def __init__(self) -> None:
        self.leakage_detected = False
        self.retained_records: list[dict[str, Any]] = []

    def check_data(self, data: Any, label: str = "data") -> None:
        if isinstance(data, (bytes, bytearray, memoryview)):
            raw = bytes(data)
            if raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff"):
                self.leakage_detected = True
                raise PixelPersistenceError(f"WriteSpy: Raw image pixel leakage rejected for {label}")
            if len(raw) > 2048:
                self.leakage_detected = True
                raise PixelPersistenceError(f"WriteSpy: Unbounded binary payload leakage rejected for {label}")
        elif hasattr(data, "__array_interface__") or type(data).__name__ == "ndarray":
            self.leakage_detected = True
            raise PixelPersistenceError(f"WriteSpy: Raw ndarray frame leakage rejected for {label}")
        elif isinstance(data, str):
            if "iVBORw0KGgo" in data:
                self.leakage_detected = True
                raise PixelPersistenceError(f"WriteSpy: Encoded PNG base64 leakage rejected for {label}")
        elif isinstance(data, Mapping):
            for k, v in data.items():
                if k in ("raw_receipt", "tree_dump", "opaque_checkpoint_bytes", "rgb", "frame_bytes"):
                    if k in ("raw_receipt", "tree_dump", "opaque_checkpoint_bytes"):
                        self.leakage_detected = True
                        raise PixelPersistenceError(f"WriteSpy: Volatile receipt/tree dump leakage rejected for key {k}")
                self.check_data(v, f"{label}.{k}")
        elif isinstance(data, (list, tuple)):
            for idx, item in enumerate(data):
                self.check_data(item, f"{label}[{idx}]")

    def persist(self, record: dict[str, Any]) -> None:
        self.check_data(record, "persist_record")
        self.retained_records.append(copy.deepcopy(record))


def make_holdings_pause(tick: int, seq: int) -> dict[str, Any]:
    """Construct inline Holdings pause command per COMMANDS.md."""
    _require_int(tick, "tick")
    _require_int(seq, "seq")
    return {"tick": tick, "seq": seq, "cmd": "pause", "args": {}}


def make_holdings_outcome(
    seq: int,
    requested_tick: int,
    applied_tick: int,
    order_seq_id: int,
    status: str,
    reason: str | None = None,
    state_sha256: str = "0" * 64,
) -> dict[str, Any]:
    """Construct inline Holdings engine outcome record per CONTRACTS.md."""
    _require_int(seq, "seq")
    _require_int(requested_tick, "requested_tick")
    _require_int(applied_tick, "applied_tick")
    _require_int(order_seq_id, "order_seq_id")
    _require_str(status, "status")
    if reason is not None and not isinstance(reason, str):
        raise EpisodeError(f"reason must be str or None, got {reason!r}")
    if not isinstance(state_sha256, str) or not _HEX64_RE.fullmatch(state_sha256):
        raise EpisodeError(f"state_sha256 must be a 64-char lowercase hex SHA-256, got {state_sha256!r}")
    return {
        "seq": seq,
        "requested_tick": requested_tick,
        "applied_tick": applied_tick,
        "order_seq_id": order_seq_id,
        "status": status,
        "reason": reason,
        "state_sha256": state_sha256,
    }


def validate_holdings_pause(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate that a Holdings pause record adheres strictly to COMMANDS.md."""
    if not isinstance(record, Mapping):
        raise EpisodeError("Pause record must be a mapping")
    keys = set(record.keys())
    if keys != {"tick", "seq", "cmd", "args"}:
        raise EpisodeError(f"Pause record keys must be tick, seq, cmd, args; got {sorted(keys)}")
    if record["cmd"] != "pause":
        raise EpisodeError(f"cmd must be 'pause', got {record['cmd']!r}")
    if record["args"] != {}:
        raise EpisodeError(f"Pause args must be empty dict, got {record['args']!r}")
    _require_int(record["tick"], "tick")
    _require_int(record["seq"], "seq")
    return dict(record)


def validate_holdings_outcome(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate that a Holdings engine outcome record adheres strictly to CONTRACTS.md."""
    if not isinstance(record, Mapping):
        raise EpisodeError("Outcome record must be a mapping")
    req_keys = {"seq", "requested_tick", "applied_tick", "order_seq_id", "status", "reason", "state_sha256"}
    if set(record.keys()) != req_keys:
        raise EpisodeError(f"Outcome record keys must be {sorted(req_keys)}, got {sorted(record.keys())}")
    return make_holdings_outcome(
        seq=record["seq"],
        requested_tick=record["requested_tick"],
        applied_tick=record["applied_tick"],
        order_seq_id=record["order_seq_id"],
        status=record["status"],
        reason=record.get("reason"),
        state_sha256=record["state_sha256"],
    )


def _validate_handle(handle: Any) -> Handle:
    """Validate a target handle using session_registry.Handle rules (generation >= 1, declared kind)."""
    try:
        return Handle(handle.kind, handle.canonical_id, handle.generation)
    except AttributeError as exc:
        raise EpisodeError("handle must expose kind, canonical_id, and generation") from exc


def _extract_episode_data(row: Any) -> dict[str, Any]:
    """Normalize input EpisodeRow, WindowEvidence, or mapping into a uniform evaluation structure."""
    if isinstance(row, EpisodeRow):
        _require_str(row.episode_id, "episode_id")
        _require_str(row.status, "status")
        _require_int(row.supported_frames, "supported_frames")
        _require_int(row.exact_frames, "exact_frames")
        _require_int(row.trail_stamps, "trail_stamps")
        _require_int(row.matched_stamps, "matched_stamps")
        if row.frame_seq is not None:
            _require_int(row.frame_seq, "frame_seq")
        return {
            "episode_id": row.episode_id,
            "status": row.status,
            "terminal": row.terminal,
            "expected_terminal": row.expected_terminal,
            "engine_outcomes": copy.deepcopy(row.engine_outcomes),
            "expected_outcomes": copy.deepcopy(row.expected_outcomes),
            "postconditions_met": bool(row.postconditions_met),
            "is_safe_refusal": bool(row.is_safe_refusal),
            "is_emitted_attempt": bool(row.is_emitted_attempt),
            "is_local_refusal": bool(row.is_local_refusal),
            "supported_frames": row.supported_frames,
            "exact_frames": row.exact_frames,
            "trail_stamps": row.trail_stamps,
            "matched_stamps": row.matched_stamps,
            "frame_seq": row.frame_seq,
            "supported": bool(row.supported),
            "violations": [map_failure_code(v) for v in row.violations],
            "metadata": copy.deepcopy(row.metadata) if isinstance(row.metadata, Mapping) else {},
        }
    if isinstance(row, WindowEvidence):
        validated_handle = _validate_handle(row.handle)
        _require_int(row.epoch, "epoch")
        _require_int(row.frame_seq, "frame_seq")
        ops = row.ops if isinstance(row.ops, dict) else {}
        ep_id = ops.get("episode_id", f"{validated_handle.kind}:{validated_handle.canonical_id}:{row.epoch}:{row.scene}")
        _require_str(ep_id, "episode_id")
        raw_status = ops.get("status")
        is_safe_ref = bool(ops.get("is_safe_refusal", False) or row.terminal == "safe_refusal")
        is_emitted = bool(ops.get("is_emitted_attempt", ops.get("sent", False)))
        is_local_ref = bool(ops.get("is_local_refusal", is_safe_ref and not is_emitted))
        supported_flag = bool(ops.get("supported", True))

        if not raw_status:
            if ops.get("excluded", False):
                raw_status = "excluded"
            elif not supported_flag:
                raw_status = "unsupported"
            elif is_safe_ref:
                raw_status = "refused"
            elif row.terminal is not None and row.terminal == row.expected_terminal and ops.get("postconditions_met", False):
                raw_status = "completed"
            else:
                raw_status = "incomplete"

        return {
            "episode_id": ep_id,
            "status": raw_status,
            "terminal": row.terminal,
            "expected_terminal": row.expected_terminal,
            "engine_outcomes": copy.deepcopy(ops.get("engine_outcomes")),
            "expected_outcomes": copy.deepcopy(ops.get("expected_outcomes")),
            "postconditions_met": bool(ops.get("postconditions_met", is_safe_ref)),
            "is_safe_refusal": is_safe_ref,
            "is_emitted_attempt": is_emitted,
            "is_local_refusal": is_local_ref,
            "supported_frames": 1 if (row.frame_supported and not row.dropped) else 0,
            "exact_frames": 1 if (row.frame_supported and not row.dropped and row.exact is True) else 0,
            "trail_stamps": int(ops.get("trail_stamps", 1)),
            "matched_stamps": int(ops.get("matched_stamps", 1 if (row.exact is True) else 0)),
            "frame_seq": row.frame_seq,
            "supported": supported_flag,
            "violations": [map_failure_code(v) for v in row.violations],
            "metadata": {
                "handle_kind": validated_handle.kind,
                "canonical_id": validated_handle.canonical_id,
                "generation": validated_handle.generation,
                "source_kind": ops.get("source_kind", "godot" if validated_handle.kind == "godot_window" else "cdp"),
            },
        }
    if isinstance(row, Mapping):
        if "handle" in row and row["handle"] is not None:
            _validate_handle(row["handle"])
        ep_id = row.get("episode_id", row.get("id"))
        if not ep_id or not isinstance(ep_id, str):
            raise EpisodeError(f"Episode row missing required string 'episode_id': {row!r}")
        supported_flag = bool(row.get("supported", True))
        is_safe_ref = bool(row.get("is_safe_refusal", False) or row.get("terminal") == "safe_refusal")
        is_emitted = bool(row.get("is_emitted_attempt", row.get("sent", False)))
        is_local_ref = bool(row.get("is_local_refusal", is_safe_ref and not is_emitted))

        status = row.get("status")
        if not status:
            if row.get("excluded", False):
                status = "excluded"
            elif not supported_flag:
                status = "unsupported"
            elif is_safe_ref:
                status = "refused"
            elif row.get("completed", False):
                status = "completed"
            else:
                status = "incomplete"

        frame_seq = row.get("frame_seq")
        if frame_seq is not None:
            _require_int(frame_seq, "frame_seq")

        return {
            "episode_id": str(ep_id),
            "status": str(status),
            "terminal": row.get("terminal"),
            "expected_terminal": row.get("expected_terminal"),
            "engine_outcomes": copy.deepcopy(row.get("engine_outcomes")),
            "expected_outcomes": copy.deepcopy(row.get("expected_outcomes")),
            "postconditions_met": bool(row.get("postconditions_met", False)),
            "is_safe_refusal": is_safe_ref,
            "is_emitted_attempt": is_emitted,
            "is_local_refusal": is_local_ref,
            "supported_frames": int(row.get("supported_frames", 1 if row.get("frame_supported", True) else 0)),
            "exact_frames": int(row.get("exact_frames", 1 if row.get("exact", True) else 0)),
            "trail_stamps": int(row.get("trail_stamps", 1)),
            "matched_stamps": int(row.get("matched_stamps", 1)),
            "frame_seq": frame_seq,
            "supported": supported_flag,
            "violations": [map_failure_code(v) for v in row.get("violations", ())],
            "metadata": copy.deepcopy(dict(row.get("metadata", {}))) if isinstance(row.get("metadata"), Mapping) else {},
        }
    raise EpisodeError(f"Unsupported row type: {type(row).__name__}")


def _check_source_policy_integrity(metadata: Mapping[str, Any], engine_outcomes: Any, ep_id: str, failures: list[str]) -> None:
    """Verify that Godot and CDP source policies are not relabeled as one another."""
    source_kind = metadata.get("source_kind")
    handle_kind = metadata.get("handle_kind")
    outcome_source = engine_outcomes.get("source") if isinstance(engine_outcomes, Mapping) else None
    if source_kind and outcome_source and source_kind != outcome_source:
        failures.append(f"source_policy_relabel:{ep_id}:{source_kind}!={outcome_source}")
    if handle_kind == "godot_window" and source_kind == "cdp":
        failures.append(f"source_policy_relabel:{ep_id}:godot_window_as_cdp")
    if handle_kind == "cdp_tab" and source_kind == "godot":
        failures.append(f"source_policy_relabel:{ep_id}:cdp_tab_as_godot")


def _check_manifest_prerequisites(manifest_dict: Mapping[str, Any], failures: list[str]) -> dict[str, Any]:
    """Validate optional prerequisite gate receipts and vision composition prerequisites."""
    prereq_status: dict[str, str] = {}
    if manifest_dict.get("vision_composition"):
        vision_rec = manifest_dict.get("jq2a_vision_scorecard")
        valid_vision = (
            isinstance(vision_rec, Mapping)
            and vision_rec.get("status") == "PASS"
            and vision_rec.get("expected_failure") is False
            and isinstance(vision_rec.get("revision"), str)
            and bool(vision_rec.get("revision"))
            and isinstance(vision_rec.get("evidence"), str)
            and bool(vision_rec.get("evidence"))
        )
        if not valid_vision:
            failures.append("prerequisite_AUSENTE:jq2a_vision_scorecard")
            prereq_status["jq2a_vision_scorecard"] = AUSENTE_STATUS
        else:
            prereq_status["jq2a_vision_scorecard"] = "PASS"

    prereqs = manifest_dict.get("prerequisites")
    if isinstance(prereqs, Mapping):
        for gate_id, rec in sorted(prereqs.items()):
            if not isinstance(rec, Mapping):
                failures.append(f"prerequisite_AUSENTE:{gate_id}")
                prereq_status[gate_id] = AUSENTE_STATUS
                continue
            if rec.get("expected_failure") is True:
                failures.append(f"expected_failure_not_accepted:{gate_id}")
                prereq_status[gate_id] = "FAIL"
                continue
            has_revision = isinstance(rec.get("revision"), str) and bool(rec.get("revision"))
            has_evidence = isinstance(rec.get("evidence"), str) and bool(rec.get("evidence"))
            if rec.get("status") != "PASS" or not has_evidence or (rec.get("require_revision", False) and not has_revision):
                failures.append(f"prerequisite_AUSENTE:{gate_id}")
                prereq_status[gate_id] = AUSENTE_STATUS
            else:
                prereq_status[gate_id] = "PASS"

    if manifest_dict.get("require_conductor_admission"):
        failures.append("conductor_admission_AUSENTE:hold_dispatch")

    return prereq_status


def reduce_episode(
    rows: Sequence[Any],
    manifest: Mapping[str, Any] | None = None,
    *,
    outcome_matcher: Callable[[Any, Any], tuple[int, int, bool]] | None = None,
    postcondition_checker: Callable[[Mapping[str, Any]], bool] | None = None,
    failure_mapper: Callable[[str], str] | None = None,
) -> EpisodeVerdict:
    """Reduce episode battery rows and manifest according to INV J5 1 through INV J5 4.

    Validates independently declared battery membership, denominators, expected terminal counts,
    nonvacuous rational metrics, closed failure mapping, and preserves descriptive confidence AUSENTE.
    """
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise EpisodeError("rows must be a sequence of episode evidence records")
    manifest_dict = dict(manifest) if manifest is not None else {}
    mapper = failure_mapper or map_failure_code
    failures: list[str] = []

    prereq_status = _check_manifest_prerequisites(manifest_dict, failures)

    declared_battery = manifest_dict.get("battery")
    if declared_battery is not None:
        if not isinstance(declared_battery, (list, tuple, set, frozenset)):
            raise EpisodeError("manifest.battery must be a sequence or set")
        declared_battery_set = {str(x) for x in declared_battery}
    else:
        declared_battery_set = None

    thresholds = manifest_dict.get("thresholds")
    if thresholds is not None and not isinstance(thresholds, Mapping):
        raise EpisodeError("manifest.thresholds must be a mapping")
    thresholds_map = dict(thresholds) if thresholds is not None else {}

    if "thresholds" in manifest_dict and len(thresholds_map) == 0:
        failures.append("missing_declared_completion_threshold")
    elif manifest_dict.get("require_declared_thresholds", False):
        if "completion" not in thresholds_map and "completion_rate" not in thresholds_map:
            failures.append("missing_declared_completion_threshold")

    expected_terminal_counts = manifest_dict.get("expected_terminals") or manifest_dict.get("terminal_counts")
    declared_denominators = manifest_dict.get("denominators")

    mig_cfg = manifest_dict.get("retail_holdings_migration")
    migration_disabled = isinstance(mig_cfg, Mapping) and mig_cfg.get("enabled") is False
    optional_training_failures = list(manifest_dict.get("optional_training_failures", ()))

    # Negative contrast & nonvacuous check: empty battery
    if (declared_battery_set is not None and len(declared_battery_set) == 0) or (len(rows) == 0):
        failures.append("empty_battery")
        empty_metrics: dict[str, Any] = {
            "completion": make_rational_rate(None, None),
            "refusal": make_rational_rate(None, None),
            "incomplete": make_rational_rate(None, None),
            "supported_exactness": make_rational_rate(None, None),
            "trail": make_rational_rate(None, None),
            "outcomes": make_rational_rate(None, None),
            "counts": {
                "eligible": 0,
                "completed": 0,
                "refused": 0,
                "incomplete": 0,
                "excluded": 0,
                "unsupported": 0,
                "emitted_attempts": 0,
                "local_refusals": 0,
                "supported_frames": 0,
                "exact_frames": 0,
                "trail_stamps": 0,
                "matched_stamps": 0,
                "outcomes_expected": 0,
                "outcomes_matched": 0,
            },
            "conductor_admission_binding": AUSENTE_STATUS,
            "prerequisites": prereq_status,
            "optional_training_failures": optional_training_failures,
            "retail_holdings_migration_blocked": False if migration_disabled else bool(optional_training_failures),
        }
        return EpisodeVerdict(
            eligible=0,
            completed=0,
            refused=0,
            incomplete=0,
            excluded=0,
            failures=tuple(mapper(f) for f in failures),
            metrics=empty_metrics,
            reward=None,
            reward_status=DESIGNING_STATUS,
            passed=False,
        )

    # Group correlated frame rows by episode_id while rejecting duplicate episode/frame identities
    episodes_by_id: dict[str, dict[str, Any]] = {}
    seen_frame_seqs: dict[str, set[int | None]] = {}
    duplicate_episode_ids: set[str] = set()

    for r in rows:
        edata = _extract_episode_data(r)
        ep_id = edata["episode_id"]
        fseq = edata["frame_seq"]

        if ep_id not in episodes_by_id:
            episodes_by_id[ep_id] = edata
            seen_frame_seqs[ep_id] = {fseq}
        else:
            # If fseq is None or already seen for this episode_id, it is a duplicate row
            if fseq is None or None in seen_frame_seqs[ep_id] or fseq in seen_frame_seqs[ep_id]:
                duplicate_episode_ids.add(ep_id)
                failures.append(f"duplicate_episode_identity:{ep_id}")
                continue
            # Otherwise, correlate distinct frame_seq rows into the single episode (no trial inflation)
            seen_frame_seqs[ep_id].add(fseq)
            existing = episodes_by_id[ep_id]
            existing["supported_frames"] += edata["supported_frames"]
            existing["exact_frames"] += edata["exact_frames"]
            existing["trail_stamps"] += edata["trail_stamps"]
            existing["matched_stamps"] += edata["matched_stamps"]
            existing["postconditions_met"] = existing["postconditions_met"] and edata["postconditions_met"]
            existing["is_emitted_attempt"] = existing["is_emitted_attempt"] or edata["is_emitted_attempt"]
            existing["is_local_refusal"] = existing["is_local_refusal"] and edata["is_local_refusal"]
            existing["supported"] = existing["supported"] and edata["supported"]
            if edata["status"] != "completed" and existing["status"] == "completed":
                existing["status"] = edata["status"]
            if edata["terminal"] is not None:
                existing["terminal"] = edata["terminal"]
            for v in edata["violations"]:
                if v not in existing["violations"]:
                    existing["violations"].append(v)

    seen_episodes = set(episodes_by_id.keys())
    episodes_data: list[dict[str, Any]] = []

    if declared_battery_set is not None:
        foreign_episodes = seen_episodes - declared_battery_set
        for fep in sorted(foreign_episodes):
            failures.append(f"foreign_battery_membership:{fep}")

        for ep_id, edata in episodes_by_id.items():
            if ep_id in declared_battery_set:
                episodes_data.append(edata)

        missing_episodes = declared_battery_set - seen_episodes
        for mep in sorted(missing_episodes):
            failures.append(f"missing_battery_membership:{mep}")
            episodes_data.append({
                "episode_id": mep,
                "status": "incomplete",
                "terminal": None,
                "expected_terminal": None,
                "engine_outcomes": None,
                "expected_outcomes": None,
                "postconditions_met": False,
                "is_safe_refusal": False,
                "is_emitted_attempt": False,
                "is_local_refusal": False,
                "supported_frames": 0,
                "exact_frames": 0,
                "trail_stamps": 0,
                "matched_stamps": 0,
                "frame_seq": None,
                "supported": True,
                "violations": ["missing_battery_membership:" + mep],
                "metadata": {},
            })
    else:
        episodes_data = list(episodes_by_id.values())

    completed_count = 0
    refused_count = 0
    incomplete_count = 0
    excluded_count = 0
    unsupported_count = 0
    emitted_attempts_count = 0
    local_refusals_count = 0

    total_supported_frames = 0
    total_exact_frames = 0
    total_trail_stamps = 0
    total_matched_stamps = 0
    total_outcomes_expected = 0
    total_outcomes_matched = 0

    observed_terminals: Counter[str] = Counter()

    for edata in episodes_data:
        ep_id = edata["episode_id"]
        ep_status = edata["status"]
        term = edata["terminal"]
        if term is not None:
            observed_terminals[term] += 1

        _check_source_policy_integrity(edata.get("metadata", {}), edata["engine_outcomes"], ep_id, failures)

        for v in edata["violations"]:
            mapped_v = mapper(v)
            if mapped_v not in failures:
                failures.append(mapped_v)

        if ep_status == "excluded":
            excluded_count += 1
            continue

        if ep_status == "unsupported" or not edata.get("supported", True):
            excluded_count += 1
            unsupported_count += 1
            failures.append(f"unsupported_episode:{ep_id}")
            continue

        if edata["is_emitted_attempt"]:
            emitted_attempts_count += 1

        # Emitted attempts must remain distinct from local refusals
        if edata["is_emitted_attempt"] and edata["is_local_refusal"]:
            failures.append(f"emitted_attempt_not_local_refusal:{ep_id}")
            incomplete_count += 1
            continue

        # INV J5 2: Completion requires matched engine outcomes AND independent task postconditions
        engine_outcomes = edata["engine_outcomes"]
        expected_outcomes = edata["expected_outcomes"]

        outcomes_matched = True
        if callable(outcome_matcher):
            exp_c, mat_c, outcomes_matched = outcome_matcher(engine_outcomes, expected_outcomes)
            total_outcomes_expected += exp_c
            total_outcomes_matched += mat_c
        elif expected_outcomes is not None:
            if isinstance(expected_outcomes, Mapping):
                total_outcomes_expected += len(expected_outcomes)
                if isinstance(engine_outcomes, Mapping):
                    for k, exp_val in expected_outcomes.items():
                        if engine_outcomes.get(k) == exp_val:
                            total_outcomes_matched += 1
                        else:
                            outcomes_matched = False
                else:
                    outcomes_matched = False
            else:
                outcomes_matched = False
        elif engine_outcomes is not None:
            total_outcomes_expected += 1
            if isinstance(engine_outcomes, Mapping) and str(engine_outcomes.get("status", "")).lower() in ("accepted", "settled", "completed", "success"):
                total_outcomes_matched += 1
            else:
                outcomes_matched = False
        elif ep_status == "completed":
            total_outcomes_expected += 1
            total_outcomes_matched += 1

        postconditions_met = (
            bool(postcondition_checker(edata))
            if callable(postcondition_checker)
            else bool(edata["postconditions_met"])
        )

        # Duplicate evidence cannot silently improve pooled frame/trail rates
        if ep_id not in duplicate_episode_ids:
            total_supported_frames += edata["supported_frames"]
            total_exact_frames += edata["exact_frames"]
            total_trail_stamps += edata["trail_stamps"]
            total_matched_stamps += edata["matched_stamps"]

        if ep_status == "completed":
            if outcomes_matched and postconditions_met and ep_id not in duplicate_episode_ids:
                completed_count += 1
            else:
                incomplete_count += 1
                if not outcomes_matched:
                    failures.append(f"unmatched_engine_outcomes:{ep_id}")
                if not postconditions_met:
                    failures.append(f"unmet_postconditions:{ep_id}")
        elif ep_status == "refused":
            # INV J5 4: Safe refusal is correctness evidence without economic completion
            if edata["is_emitted_attempt"]:
                incomplete_count += 1
                failures.append(f"emitted_attempt_not_local_refusal:{ep_id}")
            elif edata["is_safe_refusal"] or term in ("safe_refusal", "REFUSED"):
                refused_count += 1
                local_refusals_count += 1
            else:
                incomplete_count += 1
                failures.append(f"unsupported_refusal:{ep_id}")
        else:
            incomplete_count += 1

    eligible_count = completed_count + refused_count + incomplete_count

    # Expected terminal counts check
    if expected_terminal_counts is not None:
        if not isinstance(expected_terminal_counts, Mapping):
            raise EpisodeError("manifest.expected_terminals must be a mapping")
        all_term_keys = set(expected_terminal_counts.keys()) | set(observed_terminals.keys())
        for t_name in sorted(all_term_keys):
            exp_t_count = expected_terminal_counts.get(t_name, 0)
            act_t_count = observed_terminals.get(t_name, 0)
            if act_t_count != exp_t_count:
                failures.append(
                    f"terminal_count_mismatch:{t_name}:expected={exp_t_count}:actual={act_t_count}"
                )

    # Independently declared denominators check
    actual_denominators = {
        "eligible": eligible_count,
        "completion": eligible_count,
        "refusal": eligible_count,
        "incomplete": eligible_count,
        "supported_exactness": total_supported_frames,
        "exactness": total_supported_frames,
        "supported_frames": total_supported_frames,
        "trail": total_trail_stamps,
        "trail_stamps": total_trail_stamps,
        "outcomes": total_outcomes_expected,
    }
    if declared_denominators is not None:
        if not isinstance(declared_denominators, Mapping):
            raise EpisodeError("manifest.denominators must be a mapping")
        for d_key, exp_den in sorted(declared_denominators.items()):
            _require_int(exp_den, f"denominators.{d_key}", min_val=0)
            act_den = actual_denominators.get(d_key)
            if exp_den <= 0:
                failures.append(f"vacuous_denominators:{d_key}")
            elif act_den != exp_den:
                failures.append(f"denominator_mismatch:{d_key}:expected={exp_den}:actual={act_den}")

    # Metric computations under INV J5 3 (integer counts and rational rates)
    thresh_comp = thresholds_map.get("completion", thresholds_map.get("completion_rate"))
    thresh_exact = thresholds_map.get("supported_exactness", thresholds_map.get("exactness"))
    thresh_trail = thresholds_map.get("trail")
    thresh_outcomes = thresholds_map.get("outcomes")

    completion_metric = make_rational_rate(
        completed_count if eligible_count > 0 else None,
        eligible_count if eligible_count > 0 else None,
        threshold=thresh_comp,
    )
    refusal_metric = make_rational_rate(
        refused_count if eligible_count > 0 else None,
        eligible_count if eligible_count > 0 else None,
    )
    incomplete_metric = make_rational_rate(
        incomplete_count if eligible_count > 0 else None,
        eligible_count if eligible_count > 0 else None,
    )
    exactness_metric = make_rational_rate(
        total_exact_frames if total_supported_frames > 0 else None,
        total_supported_frames if total_supported_frames > 0 else None,
        threshold=thresh_exact,
    )
    trail_metric = make_rational_rate(
        total_matched_stamps if total_trail_stamps > 0 else None,
        total_trail_stamps if total_trail_stamps > 0 else None,
        threshold=thresh_trail,
    )
    outcomes_metric = make_rational_rate(
        total_outcomes_matched if total_outcomes_expected > 0 else None,
        total_outcomes_expected if total_outcomes_expected > 0 else None,
        threshold=thresh_outcomes,
    )

    metrics: dict[str, Any] = {
        "completion": completion_metric,
        "refusal": refusal_metric,
        "incomplete": incomplete_metric,
        "supported_exactness": exactness_metric,
        "trail": trail_metric,
        "outcomes": outcomes_metric,
        "counts": {
            "eligible": eligible_count,
            "completed": completed_count,
            "refused": refused_count,
            "incomplete": incomplete_count,
            "excluded": excluded_count,
            "unsupported": unsupported_count,
            "emitted_attempts": emitted_attempts_count,
            "local_refusals": local_refusals_count,
            "supported_frames": total_supported_frames,
            "exact_frames": total_exact_frames,
            "trail_stamps": total_trail_stamps,
            "matched_stamps": total_matched_stamps,
            "outcomes_expected": total_outcomes_expected,
            "outcomes_matched": total_outcomes_matched,
        },
        "conductor_admission_binding": AUSENTE_STATUS,
        "prerequisites": prereq_status,
        "optional_training_failures": optional_training_failures,
        "retail_holdings_migration_blocked": False if migration_disabled else bool(optional_training_failures),
    }

    # Gate G J5 2 Nonvacuous denominator accounting and pass determination
    passed = True
    if eligible_count == 0 or len(episodes_data) == 0:
        passed = False
        failures.append("vacuous_denominators")

    if manifest_dict.get("require_supported_denominators", False):
        for denom_metric_name in ("supported_exactness", "trail", "outcomes"):
            if metrics[denom_metric_name]["denominator"] is None:
                passed = False
                failures.append(f"vacuous_denominators:{denom_metric_name}")

    # Check explicitly declared thresholds
    for m_name, alt_key in (
        ("completion", "completion_rate"),
        ("supported_exactness", "exactness"),
        ("trail", "trail_rate"),
        ("outcomes", "outcomes_rate"),
    ):
        if m_name in thresholds_map or alt_key in thresholds_map:
            if not metrics[m_name]["passed"]:
                passed = False
                failures.append(f"threshold_not_met:{m_name}")

    if failures:
        passed = False

    canonical_failures = tuple(dict.fromkeys(mapper(f) for f in failures))

    return EpisodeVerdict(
        eligible=eligible_count,
        completed=completed_count,
        refused=refused_count,
        incomplete=incomplete_count,
        excluded=excluded_count,
        failures=canonical_failures,
        metrics=metrics,
        reward=None,
        reward_status=DESIGNING_STATUS,
        passed=passed,
    )


def report_uncertainty(
    samples: Sequence[Any],
    protocol: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Report descriptive complete-episode fractions with confidence status AUSENTE.

    Under Gate G J5 4:
    - Calculates independent fractions for complete, refused, and incomplete episodes.
    - Replicated frame counts never change episode counts or invent confidence bounds.
    - Confidence remains descriptive complete-episode evidence only: status AUSENTE,
      unit complete-episode, method null, lower null, upper null.
    """
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        raise EpisodeError("samples must be a sequence")
    if protocol is not None and not isinstance(protocol, Mapping):
        raise EpisodeError("protocol must be a mapping or None")

    episodes_by_id: dict[str, dict[str, Any]] = {}
    raw_sample_count = len(samples)

    for s in samples:
        edata = _extract_episode_data(s)
        ep_id = edata["episode_id"]
        # Kill frame-count pseudoreplication: replicated frames for an episode never multiply episode count
        if ep_id not in episodes_by_id:
            episodes_by_id[ep_id] = edata
        else:
            existing = episodes_by_id[ep_id]
            existing["postconditions_met"] = existing["postconditions_met"] and edata["postconditions_met"]
            existing["is_emitted_attempt"] = existing["is_emitted_attempt"] or edata["is_emitted_attempt"]
            if edata["status"] != "completed" and existing["status"] == "completed":
                existing["status"] = edata["status"]

    episodes = list(episodes_by_id.values())

    completed = 0
    refused = 0
    incomplete = 0
    excluded = 0
    unsupported = 0

    for ep in episodes:
        st = ep["status"]
        if st == "excluded":
            excluded += 1
        elif st == "unsupported" or not ep.get("supported", True):
            excluded += 1
            unsupported += 1
        elif st == "completed" and ep["postconditions_met"]:
            completed += 1
        elif (st == "refused" or ep["is_safe_refusal"]) and not ep["is_emitted_attempt"]:
            refused += 1
        else:
            incomplete += 1

    eligible = completed + refused + incomplete

    completed_fraction = float(Fraction(completed, eligible)) if eligible > 0 else None
    refused_fraction = float(Fraction(refused, eligible)) if eligible > 0 else None
    incomplete_fraction = float(Fraction(incomplete, eligible)) if eligible > 0 else None

    confidence = _default_confidence()

    return {
        "unit": "complete-episode",
        "counts": {
            "eligible": eligible,
            "completed": completed,
            "refused": refused,
            "incomplete": incomplete,
            "excluded": excluded,
            "unsupported": unsupported,
            "total_episodes": len(episodes),
            "raw_samples": raw_sample_count,
        },
        "fractions": {
            "completed": completed_fraction,
            "refused": refused_fraction,
            "incomplete": incomplete_fraction,
        },
        "rational": {
            "completed": make_rational_rate(completed if eligible > 0 else None, eligible if eligible > 0 else None),
            "refused": make_rational_rate(refused if eligible > 0 else None, eligible if eligible > 0 else None),
            "incomplete": make_rational_rate(incomplete if eligible > 0 else None, eligible if eligible > 0 else None),
        },
        "confidence": confidence,
        "protocol": copy.deepcopy(dict(protocol)) if protocol is not None else None,
    }


def evaluate_gate_g_j5_2(verdict: EpisodeVerdict | Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate Acceptance Gate G-J5-2: Nonvacuous denominator accounting.

    Requires nonvacuous eligible denominator (eligible > 0), integer preservation,
    consistency between eligible and completed + refused + incomplete, and rejection
    of empty batteries or vacuous perfection claims.
    """
    eligible = verdict["eligible"]
    completed = verdict["completed"]
    refused = verdict["refused"]
    incomplete = verdict["incomplete"]
    excluded = verdict["excluded"]
    metrics = verdict["metrics"]
    verdict_passed = verdict["passed"]

    ints_valid = all(
        type(x) is int and x >= 0
        for x in (eligible, completed, refused, incomplete, excluded)
    )
    nonvacuous = ints_valid and eligible > 0
    consistent = ints_valid and (eligible == completed + refused + incomplete)
    empty_battery = (eligible == 0)

    comp_metric = metrics.get("completion", {}) if isinstance(metrics, Mapping) else {}
    comp_num = comp_metric.get("numerator")
    comp_den = comp_metric.get("denominator")
    comp_status = comp_metric.get("status")

    if nonvacuous:
        denominator_valid = (
            type(comp_num) is int
            and type(comp_den) is int
            and comp_num == completed
            and comp_den == eligible
            and comp_status == MEASURED_STATUS
        )
    else:
        denominator_valid = (
            comp_num is None
            and comp_den is None
            and comp_metric.get("rate") is None
            and comp_status == UNKNOWN_STATUS
            and comp_metric.get("passed") is False
            and verdict_passed is False
        )

    passed = bool(nonvacuous and consistent and denominator_valid and not empty_battery)

    reason = "PASS" if passed else (
        "empty_battery_or_vacuous_denominators" if empty_battery else
        "inconsistent_denominator_accounting" if not consistent else
        "invalid_metric_denominator"
    )

    return {
        "gate": GATE_G_J5_2,
        "status": "PASS" if passed else "FAIL",
        "passed": passed,
        "nonvacuous": nonvacuous,
        "eligible": eligible,
        "reason": reason,
    }


def evaluate_gate_g_j5_4(report_or_confidence: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate Acceptance Gate G-J5-4: Rejection of invented bounds and pseudoreplication.

    Verifies that confidence status is AUSENTE, unit is complete-episode, method is None,
    bounds (lower, upper) are strictly None, and correlated frames are not treated as
    independent trials.
    """
    if not isinstance(report_or_confidence, Mapping):
        return {"gate": GATE_G_J5_4, "status": "FAIL", "passed": False, "reason": "malformed_confidence"}

    conf = report_or_confidence.get("confidence", report_or_confidence)
    if not isinstance(conf, Mapping):
        return {"gate": GATE_G_J5_4, "status": "FAIL", "passed": False, "reason": "malformed_confidence"}

    status_ok = (conf.get("status") == AUSENTE_STATUS)
    unit_ok = (conf.get("unit") == "complete-episode")
    method_null = (conf.get("method") is None)
    lower_null = (conf.get("lower") is None)
    upper_null = (conf.get("upper") is None)

    no_pseudoreplication = True
    if "pseudoreplicated" in conf or conf.get("frame_trials_counted", False):
        no_pseudoreplication = False

    counts = report_or_confidence.get("counts")
    if isinstance(counts, Mapping):
        eligible = counts.get("eligible", 0)
        comp = counts.get("completed", 0)
        ref = counts.get("refused", 0)
        inc = counts.get("incomplete", 0)
        if eligible != comp + ref + inc:
            no_pseudoreplication = False
        total_ep = counts.get("total_episodes")
        if type(total_ep) is int and eligible > total_ep:
            no_pseudoreplication = False

    passed = bool(status_ok and unit_ok and method_null and lower_null and upper_null and no_pseudoreplication)
    reason = "PASS" if passed else (
        "invented_bounds_detected" if not (lower_null and upper_null) else
        "invented_method_or_theorem" if not method_null else
        "status_not_ausente" if not status_ok else
        "unit_not_complete_episode" if not unit_ok else
        "pseudoreplication_detected"
    )

    return {
        "gate": GATE_G_J5_4,
        "status": "PASS" if passed else "FAIL",
        "passed": passed,
        "reason": reason,
    }
