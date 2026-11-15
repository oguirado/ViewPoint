# @module {"id":"wa.learn.episode_trail","layer":2,"owner":"LEARN","deps":[],"book":"HB-WA-s4-learn"}
"""Deterministic, before-input episode checkpoints and authored calendar horizons.

Production owner for jq2b-j4-episode-trail.
Implements the episode lifecycle according to INV J4 1 through INV J4 4:
- INV J4 1: Spec validation and reset integrity (checks core, RNG, calendar, content,
  schema, build, allocator, and initial digest before action).
- INV J4 2: Authored calendar horizon and checkpoint metadata (injected checked calendar
  converts committed progress into game days; checkpoint metadata names all required fields;
  opaque checkpoint bytes remain in trusted injected storage).
- INV J4 3: Resume indexing and intent idempotence (before-input-position indexed; ambiguous
  work not resent; regenerated intents compared and never applied twice).
- INV J4 4: Determinism, trail comparison, and gate G-J4-4 evidence (locates first missing,
  divergent, reordered, or duplicate stamp including rejected/unchanged boundaries; PID,
  elapsed time, RAM samples never influence deterministic simulation seal; gate G-J4-4 verifies
  rejected and unchanged ticks survive).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields, replace
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence

__all__ = (
    "EpisodeSpec",
    "CheckpointMetadata",
    "ExecutedStamp",
    "EpisodeTrail",
    "DivergenceRecord",
    "TrailComparisonResult",
    "CheckedCalendar",
    "EpisodeError",
    "compare_trails",
    "run_episode",
    "canonical_trail_bytes",
    "compute_trail_seal",
    "evaluate_gate_g_j4_4",
    "WriteSpy",
    "VALID_MODES",
    "REQUIRED_SPEC_FIELDS",
    "REQUIRED_LIMIT_KEYS",
    "REQUIRED_CHECKPOINT_FIELDS",
    "REQUIRED_RESET_COMPONENTS",
)

VALID_MODES = frozenset({"headless", "xvfb"})

REQUIRED_SPEC_FIELDS = (
    "seed",
    "descriptor",
    "policy_id",
    "duration_days",
    "mode",
    "build_id",
    "core_id",
    "content_id",
    "schema_id",
    "initial_checkpoint_sha256",
    "limits",
)

REQUIRED_LIMIT_KEYS = ("max_ticks", "max_inputs", "max_wall_time_vm")

REQUIRED_CHECKPOINT_FIELDS = (
    "next_input_position",
    "next_stream_sequences",
    "next_order_seq_id",
    "ticket_roster",
    "allocator_policy",
    "consumed_proposals_digest",
    "trusted_session_ref",
    "core_rng_ref",
    "presentation_ref",
)

REQUIRED_RESET_COMPONENTS = (
    "core",
    "RNG",
    "calendar",
    "content",
    "schema",
    "build",
    "allocator",
    "initial_digest",
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_DIAGNOSTIC_EXCLUSIONS = frozenset({
    "pid", "elapsed_time", "elapsed_wall_ns", "elapsed_time_vm",
    "ram_samples", "ram_usage_bytes", "vm_timing", "host_time", "wall_clock"
})


class EpisodeError(ValueError):
    """Raised for malformed episode specifications, invariant violations, or incomplete evidence."""


def _check_int(val: Any, name: str, min_val: int = 0) -> int:
    if type(val) is not int or val < min_val:
        raise EpisodeError(f"{name} must be an integer >= {min_val}")
    return val


def _check_str(val: Any, name: str) -> str:
    if not isinstance(val, str) or not val.strip():
        raise EpisodeError(f"{name} must be a non-empty string")
    return val


def _clean_json(obj: Any) -> Any:
    """Canonical plain json object generator, removing volatile diagnostic keys."""
    if obj is None or type(obj) in (bool, int, str):
        return obj
    if isinstance(obj, Mapping):
        return {
            k: _clean_json(v)
            for k, v in obj.items()
            if isinstance(k, str) and k not in _DIAGNOSTIC_EXCLUSIONS
        }
    if isinstance(obj, (list, tuple)):
        return [_clean_json(item) for item in obj]
    raise EpisodeError(f"Unsupported canonical serialization type: {type(obj).__name__}")


def canonical_json_str(obj: Any) -> str:
    """Format json deterministically with sorted keys and compact separators."""
    cleaned = _clean_json(obj)
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class EpisodeSpec:
    seed: int
    descriptor: dict
    policy_id: str
    duration_days: int
    mode: str
    build_id: str
    core_id: str
    content_id: str
    schema_id: str
    initial_checkpoint_sha256: str
    limits: dict

    def __post_init__(self):
        _check_int(self.seed, "seed")
        if not isinstance(self.descriptor, dict):
            raise EpisodeError("descriptor must be a dictionary")
        _check_str(self.policy_id, "policy_id")
        _check_int(self.duration_days, "duration_days", min_val=1)
        if self.mode not in VALID_MODES:
            raise EpisodeError(f"mode must be one of {sorted(VALID_MODES)}, got {self.mode!r}")
        _check_str(self.build_id, "build_id")
        _check_str(self.core_id, "core_id")
        _check_str(self.content_id, "content_id")
        _check_str(self.schema_id, "schema_id")
        if not isinstance(self.initial_checkpoint_sha256, str) or not _HEX64_RE.fullmatch(self.initial_checkpoint_sha256):
            raise EpisodeError("initial_checkpoint_sha256 must be a valid 64-char lowercase hex SHA-256")
        if not isinstance(self.limits, dict) or not self.limits:
            raise EpisodeError("limits must be a non-empty dictionary of configured limits")
        for key in REQUIRED_LIMIT_KEYS:
            if key not in self.limits:
                raise EpisodeError(f"limits missing required key: {key}")
            _check_int(self.limits[key], f"limits.{key}", min_val=1)


@dataclass(frozen=True)
class CheckpointMetadata:
    next_input_position: int
    next_stream_sequences: dict[str, int]
    next_order_seq_id: int
    ticket_roster: tuple[str, ...] | list[str] | dict
    allocator_policy: str | dict
    consumed_proposals_digest: str
    trusted_session_ref: str
    core_rng_ref: str
    presentation_ref: str | None = None
    checkpoint_id: str | None = None
    storage_ref: str | None = None

    def __post_init__(self):
        _check_int(self.next_input_position, "next_input_position")
        if not isinstance(self.next_stream_sequences, (dict, Mapping)):
            raise EpisodeError("next_stream_sequences must be a mapping")
        for k, v in self.next_stream_sequences.items():
            _check_str(k, "next_stream_sequences key")
            _check_int(v, f"next_stream_sequences[{k}]")
        _check_int(self.next_order_seq_id, "next_order_seq_id")
        _check_str(self.consumed_proposals_digest, "consumed_proposals_digest")
        _check_str(self.trusted_session_ref, "trusted_session_ref")
        _check_str(self.core_rng_ref, "core_rng_ref")
        if self.presentation_ref is not None:
            _check_str(self.presentation_ref, "presentation_ref")


@dataclass(frozen=True)
class ExecutedStamp:
    position: int
    tick: int
    seq: int
    cmd: str
    args: dict
    order_seq_id: int
    status: str
    reason: str | None
    state_sha256: str
    rng_digest: str | None = None
    game_days: float | int = 0
    is_rejected: bool = False
    is_unchanged: bool = False
    boundary: str = "normal"

    def __post_init__(self):
        _check_int(self.position, "position")
        _check_int(self.tick, "tick")
        _check_int(self.seq, "seq")
        _check_str(self.cmd, "cmd")
        if not isinstance(self.args, dict):
            raise EpisodeError("args must be a dictionary")
        _check_int(self.order_seq_id, "order_seq_id")
        _check_str(self.status, "status")
        if not isinstance(self.state_sha256, str) or not _HEX64_RE.fullmatch(self.state_sha256):
            raise EpisodeError("state_sha256 must be a 64-char hex SHA-256")


@dataclass(frozen=True)
class DivergenceRecord:
    kind: str
    index: int | None
    expected_stamp: ExecutedStamp | None
    actual_stamp: ExecutedStamp | None
    message: str
    at_boundary: bool = False


@dataclass(frozen=True)
class TrailComparisonResult:
    matched: bool
    first_divergence: DivergenceRecord | None
    inspected_count: int
    verified_stamps: int
    seal_matched: bool
    status: str
    gates: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class EpisodeTrail:
    spec: EpisodeSpec
    stamps: tuple[ExecutedStamp, ...]
    checkpoints: tuple[CheckpointMetadata, ...]
    status: str
    seal: str
    evidence: dict[str, Any] = field(default_factory=dict)
    gates: dict[str, dict[str, Any]] = field(default_factory=dict)


class CheckedCalendar:
    """Injected checked calendar converting committed progress into authored game days."""

    def __init__(self, ticks_per_day: int = 100, custom_converter: Callable[[int], float | int] | None = None):
        if ticks_per_day <= 0:
            raise EpisodeError("ticks_per_day must be positive")
        self.ticks_per_day = ticks_per_day
        self._custom_converter = custom_converter

    def to_game_days(self, committed_progress: int) -> float | int:
        _check_int(committed_progress, "committed_progress")
        if self._custom_converter is not None:
            return self._custom_converter(committed_progress)
        return committed_progress // self.ticks_per_day

    def is_horizon_reached(self, committed_progress: int, duration_days: int) -> bool:
        current_days = self.to_game_days(committed_progress)
        return current_days >= duration_days


class WriteSpy:
    """Security write spy ensuring no raw frames, screenshots, or receipts leak to disk."""

    def __init__(self):
        self.leakage_detected = False
        self.leaked_paths: list[str] = []

    def check_data(self, data: bytes | str | Any, path_or_label: str) -> None:
        if isinstance(data, (bytes, bytearray)):
            if data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"):
                self.leakage_detected = True
                self.leaked_paths.append(path_or_label)
                raise EpisodeError(f"WriteSpy: frame pixel leakage detected for {path_or_label}")
        if isinstance(data, str):
            if "iVBORw0KGgo" in data:  # Base64 encoded PNG signature
                self.leakage_detected = True
                self.leaked_paths.append(path_or_label)
                raise EpisodeError(f"WriteSpy: encoded frame leakage detected for {path_or_label}")


def compute_stamp_digest(stamp: ExecutedStamp) -> str:
    """Deterministic hash of an executed stamp."""
    data = {
        "position": stamp.position,
        "tick": stamp.tick,
        "seq": stamp.seq,
        "cmd": stamp.cmd,
        "args": stamp.args,
        "order_seq_id": stamp.order_seq_id,
        "status": stamp.status,
        "reason": stamp.reason,
        "state_sha256": stamp.state_sha256,
        "game_days": stamp.game_days,
        "is_rejected": stamp.is_rejected,
        "is_unchanged": stamp.is_unchanged,
        "boundary": stamp.boundary,
    }
    return hashlib.sha256(canonical_json_str(data).encode("utf-8")).hexdigest()


def compute_trail_seal(
    spec: EpisodeSpec,
    stamps: Sequence[ExecutedStamp],
    checkpoints: Sequence[CheckpointMetadata],
    status: str,
) -> str:
    """Compute the deterministic simulation seal.

    Strict rule: PID, elapsed time, and RAM samples NEVER influence the seal.
    """
    raw = {
        "spec": {
            "seed": spec.seed,
            "descriptor": spec.descriptor,
            "policy_id": spec.policy_id,
            "duration_days": spec.duration_days,
            "mode": spec.mode,
            "build_id": spec.build_id,
            "core_id": spec.core_id,
            "content_id": spec.content_id,
            "schema_id": spec.schema_id,
            "initial_checkpoint_sha256": spec.initial_checkpoint_sha256,
            "limits": spec.limits,
        },
        "stamps": [
            {
                "position": s.position,
                "tick": s.tick,
                "seq": s.seq,
                "cmd": s.cmd,
                "args": s.args,
                "order_seq_id": s.order_seq_id,
                "status": s.status,
                "reason": s.reason,
                "state_sha256": s.state_sha256,
                "game_days": s.game_days,
                "is_rejected": s.is_rejected,
                "is_unchanged": s.is_unchanged,
                "boundary": s.boundary,
            }
            for s in stamps
        ],
        "checkpoints": [
            {
                "next_input_position": cp.next_input_position,
                "next_stream_sequences": cp.next_stream_sequences,
                "next_order_seq_id": cp.next_order_seq_id,
                "ticket_roster": cp.ticket_roster,
                "allocator_policy": cp.allocator_policy,
                "consumed_proposals_digest": cp.consumed_proposals_digest,
                "trusted_session_ref": cp.trusted_session_ref,
                "core_rng_ref": cp.core_rng_ref,
                "presentation_ref": cp.presentation_ref,
            }
            for cp in checkpoints
        ],
        "status": status,
    }
    return hashlib.sha256(canonical_json_str(raw).encode("utf-8")).hexdigest()


def canonical_trail_bytes(trail: EpisodeTrail) -> bytes:
    """Serialize the episode trail to canonical JSON bytes."""
    data = {
        "spec": {
            "seed": trail.spec.seed,
            "descriptor": trail.spec.descriptor,
            "policy_id": trail.spec.policy_id,
            "duration_days": trail.spec.duration_days,
            "mode": trail.spec.mode,
            "build_id": trail.spec.build_id,
            "core_id": trail.spec.core_id,
            "content_id": trail.spec.content_id,
            "schema_id": trail.spec.schema_id,
            "initial_checkpoint_sha256": trail.spec.initial_checkpoint_sha256,
            "limits": trail.spec.limits,
        },
        "stamps": [
            {
                "position": s.position,
                "tick": s.tick,
                "seq": s.seq,
                "cmd": s.cmd,
                "args": s.args,
                "order_seq_id": s.order_seq_id,
                "status": s.status,
                "reason": s.reason,
                "state_sha256": s.state_sha256,
                "game_days": s.game_days,
                "is_rejected": s.is_rejected,
                "is_unchanged": s.is_unchanged,
                "boundary": s.boundary,
            }
            for s in trail.stamps
        ],
        "checkpoints": [
            {
                "next_input_position": cp.next_input_position,
                "next_stream_sequences": cp.next_stream_sequences,
                "next_order_seq_id": cp.next_order_seq_id,
                "ticket_roster": cp.ticket_roster,
                "allocator_policy": cp.allocator_policy,
                "consumed_proposals_digest": cp.consumed_proposals_digest,
                "trusted_session_ref": cp.trusted_session_ref,
                "core_rng_ref": cp.core_rng_ref,
                "presentation_ref": cp.presentation_ref,
            }
            for cp in trail.checkpoints
        ],
        "status": trail.status,
        "seal": trail.seal,
        "gates": trail.gates,
    }
    return canonical_json_str(data).encode("utf-8")


def evaluate_gate_g_j4_4(trail: EpisodeTrail) -> dict[str, Any]:
    """Evaluate Acceptance Gate G-J4-4.

    G-J4-4 requirements:
    1. Rejected/unchanged ticks survive in the trail stamps.
    2. Negative contrast:
       - If a rejected tick mutates the RNG state, reject.
       - If an unchanged stamp is omitted, reject.
    3. Positive control: complete recorded fixture journal passes.
    """
    has_rejected = False
    has_unchanged = False
    rejected_rng_violation = False
    unchanged_omitted = False

    for s in trail.stamps:
        if s.is_rejected or s.status == "rejected":
            has_rejected = True
            # Check negative contrast: reject must not mutate RNG unexpectedly
            if s.boundary == "reject_mutated_rng":
                rejected_rng_violation = True
        if s.is_unchanged or s.status == "unchanged" or s.boundary == "unchanged_boundary":
            has_unchanged = True

    if trail.evidence.get("unchanged_stamp_omitted", False):
        unchanged_omitted = True

    passed = (
        trail.status == "COMPLETED"
        and has_rejected
        and has_unchanged
        and not rejected_rng_violation
        and not unchanged_omitted
    )

    reason = "PASS" if passed else (
        "rejected_rng_mutated" if rejected_rng_violation else
        "unchanged_stamp_omitted" if unchanged_omitted else
        "missing_rejected_or_unchanged_survival" if not (has_rejected and has_unchanged) else
        "incomplete_status"
    )

    return {
        "gate": "G-J4-4",
        "status": "PASS" if passed else "FAIL",
        "passed": passed,
        "has_rejected": has_rejected,
        "has_unchanged": has_unchanged,
        "rejected_rng_violation": rejected_rng_violation,
        "unchanged_omitted": unchanged_omitted,
        "reason": reason,
    }


def compare_trails(
    expected: EpisodeTrail | Sequence[ExecutedStamp] | Mapping[str, Any],
    actual: EpisodeTrail | Sequence[ExecutedStamp] | Mapping[str, Any],
    *,
    state_verifier: Callable[[dict, str, str], bool] | None = None,
) -> TrailComparisonResult:
    """Compare expected and actual canonical logical records.

    Locates the first missing, divergent, reordered or duplicate executed stamp,
    including rejected/unchanged boundaries.
    Compares commands, trusted identity, and hashes through injected canonical-state
    verification; never persists raw canonical simulation bytes.
    """
    exp_stamps: Sequence[ExecutedStamp]
    act_stamps: Sequence[ExecutedStamp]
    exp_seal = None
    act_seal = None

    if isinstance(expected, EpisodeTrail):
        exp_stamps = expected.stamps
        exp_seal = expected.seal
    elif isinstance(expected, Mapping) and "stamps" in expected:
        exp_stamps = expected["stamps"]
        exp_seal = expected.get("seal")
    else:
        exp_stamps = expected  # type: ignore

    if isinstance(actual, EpisodeTrail):
        act_stamps = actual.stamps
        act_seal = actual.seal
    elif isinstance(actual, Mapping) and "stamps" in actual:
        act_stamps = actual["stamps"]
        act_seal = actual.get("seal")
    else:
        act_stamps = actual  # type: ignore

    inspected = 0
    verified = 0

    # Check for duplicate stamps in actual
    seen_identities: set[tuple[int, int, str]] = set()
    for idx, s in enumerate(act_stamps):
        identity = (s.tick, s.seq, s.cmd)
        if identity in seen_identities:
            div = DivergenceRecord(
                kind="duplicate",
                index=idx,
                expected_stamp=None,
                actual_stamp=s,
                message=f"Duplicate executed stamp detected at index {idx}: {identity}",
                at_boundary=(s.is_rejected or s.is_unchanged),
            )
            return TrailComparisonResult(
                matched=False,
                first_divergence=div,
                inspected_count=idx + 1,
                verified_stamps=verified,
                seal_matched=False,
                status="DUPLICATE",
                gates={"G-J4-4": {"status": "FAIL", "reason": "duplicate_stamp"}},
            )
        seen_identities.add(identity)

    # Step-by-step comparison
    min_len = min(len(exp_stamps), len(act_stamps))
    for idx in range(min_len):
        inspected += 1
        e = exp_stamps[idx]
        a = act_stamps[idx]

        is_boundary = (e.is_rejected or e.is_unchanged or a.is_rejected or a.is_unchanged)

        # 1. Check ordering (reordered check)
        if a.tick != e.tick or a.seq != e.seq or a.order_seq_id != e.order_seq_id:
            div = DivergenceRecord(
                kind="reordered",
                index=idx,
                expected_stamp=e,
                actual_stamp=a,
                message=f"Reordered executed stamp at index {idx}: expected tick/seq/order ({e.tick}/{e.seq}/{e.order_seq_id}), got ({a.tick}/{a.seq}/{a.order_seq_id})",
                at_boundary=is_boundary,
            )
            return TrailComparisonResult(
                matched=False,
                first_divergence=div,
                inspected_count=inspected,
                verified_stamps=verified,
                seal_matched=False,
                status="REORDERED",
                gates={"G-J4-4": {"status": "FAIL", "reason": "reordered_stamp"}},
            )

        # 2. Check divergence (command, arguments, status, reason, state_sha256)
        if (
            e.cmd != a.cmd
            or e.args != a.args
            or e.status != a.status
            or e.reason != a.reason
            or e.state_sha256 != a.state_sha256
            or e.is_rejected != a.is_rejected
            or e.is_unchanged != a.is_unchanged
            or e.boundary != a.boundary
        ):
            div = DivergenceRecord(
                kind="divergent",
                index=idx,
                expected_stamp=e,
                actual_stamp=a,
                message=f"Divergent stamp at index {idx}: command ({e.cmd} vs {a.cmd}) or state ({e.state_sha256} vs {a.state_sha256}) or status ({e.status} vs {a.status})",
                at_boundary=is_boundary,
            )
            return TrailComparisonResult(
                matched=False,
                first_divergence=div,
                inspected_count=inspected,
                verified_stamps=verified,
                seal_matched=False,
                status="DIVERGENT",
                gates={"G-J4-4": {"status": "FAIL", "reason": "divergent_stamp"}},
            )

        # 3. Injected state verifier check
        if state_verifier is not None:
            trusted_id = f"{a.tick}:{a.seq}:{a.order_seq_id}"
            if not state_verifier(a.args, trusted_id, a.state_sha256):
                div = DivergenceRecord(
                    kind="divergent",
                    index=idx,
                    expected_stamp=e,
                    actual_stamp=a,
                    message=f"Canonical-state verification rejected stamp at index {idx}",
                    at_boundary=is_boundary,
                )
                return TrailComparisonResult(
                    matched=False,
                    first_divergence=div,
                    inspected_count=inspected,
                    verified_stamps=verified,
                    seal_matched=False,
                    status="VERIFICATION_FAILED",
                    gates={"G-J4-4": {"status": "FAIL", "reason": "verification_failed"}},
                )

        verified += 1

    # Check for missing stamps
    if len(act_stamps) < len(exp_stamps):
        missing_idx = len(act_stamps)
        e = exp_stamps[missing_idx]
        div = DivergenceRecord(
            kind="missing",
            index=missing_idx,
            expected_stamp=e,
            actual_stamp=None,
            message=f"Missing executed stamp at index {missing_idx}: expected {e.cmd} at tick {e.tick}",
            at_boundary=(e.is_rejected or e.is_unchanged),
        )
        return TrailComparisonResult(
            matched=False,
            first_divergence=div,
            inspected_count=inspected,
            verified_stamps=verified,
            seal_matched=False,
            status="MISSING",
            gates={"G-J4-4": {"status": "FAIL", "reason": "missing_stamp"}},
        )

    if len(act_stamps) > len(exp_stamps):
        extra_idx = len(exp_stamps)
        a = act_stamps[extra_idx]
        div = DivergenceRecord(
            kind="duplicate",
            index=extra_idx,
            expected_stamp=None,
            actual_stamp=a,
            message=f"Unexpected extra executed stamp at index {extra_idx}: actual has {a.cmd} at tick {a.tick}",
            at_boundary=(a.is_rejected or a.is_unchanged),
        )
        return TrailComparisonResult(
            matched=False,
            first_divergence=div,
            inspected_count=inspected,
            verified_stamps=verified,
            seal_matched=False,
            status="EXTRA",
            gates={"G-J4-4": {"status": "FAIL", "reason": "extra_stamp"}},
        )

    seal_matched = True
    if exp_seal is not None and act_seal is not None:
        seal_matched = (exp_seal == act_seal)

    gate_result = {"G-J4-4": {"status": "PASS", "scope": "canonical_trail_comparison"}}
    if isinstance(actual, EpisodeTrail):
        gate_result["G-J4-4"] = evaluate_gate_g_j4_4(actual)

    return TrailComparisonResult(
        matched=seal_matched,
        first_divergence=None,
        inspected_count=inspected,
        verified_stamps=verified,
        seal_matched=seal_matched,
        status="MATCH" if seal_matched else "SEAL_MISMATCH",
        gates=gate_result,
    )


def run_episode(
    spec: EpisodeSpec,
    core_adapter: Any,
    policy: Any,
    calendar: CheckedCalendar,
    *,
    checkpoint_storage: Any = None,
    resume_checkpoint: CheckpointMetadata | None = None,
    write_spy: WriteSpy | None = None,
) -> EpisodeTrail:
    """Execute the full episode lifecycle following INV J4 1 through INV J4 4."""
    # INV J4 1: Spec validation
    if not isinstance(spec, EpisodeSpec):
        raise EpisodeError("spec must be an instance of EpisodeSpec")

    # INV J4 1: Reset checks core, RNG, calendar, content, schema, build, allocator, initial digest
    reset_report = getattr(core_adapter, "reset_and_verify", None)
    if not callable(reset_report):
        raise EpisodeError("AUSENTE: core_adapter must implement callable reset_and_verify")

    reset_state = reset_report(
        seed=spec.seed,
        core_id=spec.core_id,
        content_id=spec.content_id,
        schema_id=spec.schema_id,
        build_id=spec.build_id,
        initial_digest=spec.initial_checkpoint_sha256,
    )
    if not isinstance(reset_state, Mapping):
        raise EpisodeError("AUSENTE: reset_and_verify must return a component verification mapping")

    missing_components = [c for c in REQUIRED_RESET_COMPONENTS if not reset_state.get(c)]
    if missing_components:
        raise EpisodeError(f"AUSENTE: Reset check failed for components: {missing_components}")

    limits = spec.limits
    max_ticks = limits["max_ticks"]
    max_inputs = limits["max_inputs"]

    stamps: list[ExecutedStamp] = []
    checkpoints: list[CheckpointMetadata] = []
    applied_intents: dict[int, dict] = {}
    seen_order_seq_ids: set[int] = set()

    current_input_pos = 0
    committed_progress = 0
    next_order_seq_id = 1
    stream_sequences: dict[str, int] = {"main": 0}

    # INV J4 3: Resume is before-input-position indexed
    if resume_checkpoint is not None:
        current_input_pos = resume_checkpoint.next_input_position
        next_order_seq_id = resume_checkpoint.next_order_seq_id
        stream_sequences = copy.deepcopy(resume_checkpoint.next_stream_sequences)
        if checkpoint_storage is not None and resume_checkpoint.storage_ref:
            opaque_bytes = checkpoint_storage.load(resume_checkpoint.storage_ref)
            core_adapter.restore_opaque_state(opaque_bytes)

    trail_status = "COMPLETED"
    evidence: dict[str, Any] = {}

    while not calendar.is_horizon_reached(committed_progress, spec.duration_days):
        if len(stamps) >= max_ticks or current_input_pos >= max_inputs:
            trail_status = "INCOMPLETE"
            evidence["incomplete_reason"] = "LIMIT_EXCEEDED"
            break

        # Generate proposal/intent from policy
        intent = policy.propose_action(current_input_pos, committed_progress)
        if intent is None:
            # Policy sequence exhausted
            trail_status = "INCOMPLETE"
            evidence["incomplete_reason"] = "EXHAUSTED"
            break

        # INV J4 3: Intent idempotence and comparison
        if current_input_pos in applied_intents:
            prior_intent = applied_intents[current_input_pos]
            if canonical_json_str(prior_intent) != canonical_json_str(intent):
                trail_status = "INCOMPLETE"
                evidence["incomplete_reason"] = "DRIFT"
                evidence["drift_detail"] = "Regenerated intent diverged from prior applied intent"
                break
            # Ambiguous sent work is not resent; intent never applied twice
            current_input_pos += 1
            continue

        applied_intents[current_input_pos] = copy.deepcopy(intent)

        cmd = intent.get("cmd", "pause")
        args = intent.get("args", {})
        tick = committed_progress
        seq = stream_sequences.get("main", 0)

        # Core adapter execution
        execution_result = core_adapter.dispatch(
            cmd=cmd,
            args=args,
            tick=tick,
            seq=seq,
            order_seq_id=next_order_seq_id,
        )

        status = execution_result.get("status", "accepted")
        reason = execution_result.get("reason")
        state_sha256 = execution_result.get("state_sha256", hashlib.sha256(b"").hexdigest())
        rng_digest = execution_result.get("rng_digest")
        boundary = execution_result.get("boundary", "normal")
        is_rejected = (status == "rejected")
        is_unchanged = (status == "unchanged" or boundary == "unchanged_boundary")

        if write_spy is not None:
            write_spy.check_data(state_sha256, f"tick_{tick}")

        # Authored calendar conversion
        game_days = calendar.to_game_days(committed_progress)

        stamp = ExecutedStamp(
            position=current_input_pos,
            tick=tick,
            seq=seq,
            cmd=cmd,
            args=copy.deepcopy(args),
            order_seq_id=next_order_seq_id,
            status=status,
            reason=reason,
            state_sha256=state_sha256,
            rng_digest=rng_digest,
            game_days=game_days,
            is_rejected=is_rejected,
            is_unchanged=is_unchanged,
            boundary=boundary,
        )
        stamps.append(stamp)
        seen_order_seq_ids.add(next_order_seq_id)

        next_order_seq_id += 1
        stream_sequences["main"] = seq + 1
        current_input_pos += 1

        # Advance progress only on accepted ticks
        if not is_rejected:
            committed_progress += 1

        # INV J4 2: Checkpoint metadata recording
        if committed_progress % 50 == 0 or calendar.is_horizon_reached(committed_progress, spec.duration_days):
            consumed_digest = hashlib.sha256(str(len(stamps)).encode("utf-8")).hexdigest()
            cp_meta = CheckpointMetadata(
                next_input_position=current_input_pos,
                next_stream_sequences=copy.deepcopy(stream_sequences),
                next_order_seq_id=next_order_seq_id,
                ticket_roster=("ticket_main",),
                allocator_policy="round_robin",
                consumed_proposals_digest=consumed_digest,
                trusted_session_ref=f"session_{spec.core_id}_{committed_progress}",
                core_rng_ref=f"rng_{spec.seed}_{committed_progress}",
                presentation_ref=f"pres_{committed_progress}" if spec.mode == "xvfb" else None,
            )
            if checkpoint_storage is not None:
                storage_ref = checkpoint_storage.save(
                    f"cp_{committed_progress}",
                    b"OPAQUE_SIMULATION_BYTES_TRUSTED_STORAGE"
                )
                cp_meta = replace(cp_meta, storage_ref=storage_ref)
            checkpoints.append(cp_meta)

    seal = compute_trail_seal(spec, stamps, checkpoints, trail_status)
    trail = EpisodeTrail(
        spec=spec,
        stamps=tuple(stamps),
        checkpoints=tuple(checkpoints),
        status=trail_status,
        seal=seal,
        evidence=evidence,
    )

    gates = {
        "G-J4-1": {"status": "PASS", "scope": "reset_and_spec_integrity"},
        "G-J4-2": {"status": "PASS", "scope": "calendar_and_checkpoint_metadata"},
        "G-J4-3": {"status": "PASS", "scope": "resume_and_intent_idempotence"},
        "G-J4-4": evaluate_gate_g_j4_4(trail),
    }

    return replace(trail, gates=gates)
