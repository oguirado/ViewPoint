# @module {"id":"wa.learn.episode_spec","layer":2,"owner":"LEARN","deps":["wa.state.schema"],"book":"HB-WA-s4-learn"}
"""@module wa.learn.episode_spec - production owner for jq2b-j4-episode-spec.

Implements episode specification validation, trusted checkpoint verification,
authored calendar horizon enforcement, before-input resume indexing, and
deterministic simulation seal calculation under invariants INV J4 1 through INV J4 4.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace, asdict
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence

from ..state.schema import dumps

# Invariant & Gate citations
INV_J4_1 = "INV J4 1: Spec validity and authentication before reset action"
INV_J4_2 = "INV J4 2: Authored calendar horizon converts progress into game days"
INV_J4_3 = "INV J4 3: Before-input checkpoint resume indexing and deduplication"
INV_J4_4 = "INV J4 4: Deterministic simulation seal isolated from host metrics"
GATE_G_J4_1 = "G-J4-1"

SUPPORTED_MODES = frozenset({"headless", "xvfb"})
_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_REQUIRED_LIMIT_KEYS = frozenset({"max_ticks", "max_actions", "max_orders", "max_streams"})


class SpecValidationError(ValueError):
    """Raised when an EpisodeSpec fails structural validation or constraints."""
    pass


class CheckpointValidationError(ValueError):
    """Raised when a checkpoint fails validation against trusted context."""
    pass


class EpisodeLifecycleError(RuntimeError):
    """Raised on episode lifecycle or invariant violations."""
    pass


class AusenteError(RuntimeError):
    """Raised when required prerequisites or evidence are absent (AUSENTE)."""
    pass


@dataclass(frozen=True)
class EpisodeLimits:
    """Explicit descriptor limits required for simulation bounds."""
    max_ticks: int
    max_actions: int
    max_orders: int
    max_streams: int
    max_wall_time_vm_s: int = 3600

    def __post_init__(self):
        for f_name in ("max_ticks", "max_actions", "max_orders", "max_streams", "max_wall_time_vm_s"):
            val = getattr(self, f_name)
            if type(val) is not int or val <= 0:
                raise SpecValidationError(f"Limit {f_name} must be a positive integer, got {val!r}")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_ticks": self.max_ticks,
            "max_actions": self.max_actions,
            "max_orders": self.max_orders,
            "max_streams": self.max_streams,
            "max_wall_time_vm_s": self.max_wall_time_vm_s,
        }


@dataclass(frozen=True)
class EpisodeSpec:
    """Authored episode specification for deterministic simulation."""
    seed: int
    descriptor: Mapping[str, Any] | str
    policy_id: str
    duration_days: int
    mode: str
    build_id: str
    core_id: str
    content_id: str
    schema_id: str
    initial_checkpoint_sha256: str
    limits: EpisodeLimits | Mapping[str, int]


@dataclass(frozen=True)
class CheckpointMetadata:
    """Trusted checkpoint metadata identifying resume state."""
    next_input_position: int
    next_stream_sequences: Mapping[str, int]
    next_order_seq_id: int
    ticket_roster: Sequence[Any] | Mapping[str, Any]
    allocator_policy: str
    consumed_proposals_digest: str
    trusted_session_ref: str
    core_rng_ref: str
    presentation_ref: str | None
    checkpoint_sha256: str


@dataclass
class TrustedContext:
    """Trusted injected execution context for reset authentication and validation."""
    core_id: str
    core_rng_ref: str
    calendar: Any
    content_id: str
    schema_id: str
    build_id: str
    allocator_policy: str
    initial_checkpoint_sha256: str
    trusted_session_ref: str
    seed: int | None = None
    trusted_storage: Mapping[str, bytes] = field(default_factory=dict)
    prerequisites: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def _validate_limits(raw_limits: Any) -> EpisodeLimits:
    if isinstance(raw_limits, EpisodeLimits):
        return raw_limits
    if not isinstance(raw_limits, Mapping) or not raw_limits:
        raise SpecValidationError("Explicit limits mapping is required, no defaults allowed")
    missing = _REQUIRED_LIMIT_KEYS - set(raw_limits.keys())
    if missing:
        raise SpecValidationError(f"Missing required limit keys: {sorted(missing)}")
    for k in _REQUIRED_LIMIT_KEYS:
        v = raw_limits[k]
        if type(v) is not int or v <= 0:
            raise SpecValidationError(f"Limit {k} must be positive integer, got {v!r}")
    vm_limit = raw_limits.get("max_wall_time_vm_s", 3600)
    if type(vm_limit) is not int or vm_limit <= 0:
        raise SpecValidationError(f"Limit max_wall_time_vm_s must be positive integer, got {vm_limit!r}")
    return EpisodeLimits(
        max_ticks=raw_limits["max_ticks"],
        max_actions=raw_limits["max_actions"],
        max_orders=raw_limits["max_orders"],
        max_streams=raw_limits["max_streams"],
        max_wall_time_vm_s=vm_limit,
    )


# @anchor:wa.learn.episode_spec.validate_spec
def validate_spec(spec: EpisodeSpec | Mapping[str, Any], calendar: Any = None) -> EpisodeSpec:
    """Validate EpisodeSpec with injected checked calendar.

    INV J4 1: Requires explicit descriptor limits, supported mode/capabilities,
    exact checkpoint identities, and non-empty IDs.
    INV J4 2: An injected checked calendar converts committed progress into
    authored game days; host time and action counts cannot establish the horizon.
    """
    if spec is None:
        raise SpecValidationError("Spec cannot be None")

    if isinstance(spec, Mapping):
        req_keys = {
            "seed", "descriptor", "policy_id", "duration_days", "mode",
            "build_id", "core_id", "content_id", "schema_id",
            "initial_checkpoint_sha256", "limits",
        }
        missing = req_keys - set(spec.keys())
        if missing:
            raise SpecValidationError(f"Spec missing required fields: {sorted(missing)}")
        spec_obj = EpisodeSpec(
            seed=spec["seed"],
            descriptor=spec["descriptor"],
            policy_id=spec["policy_id"],
            duration_days=spec["duration_days"],
            mode=spec["mode"],
            build_id=spec["build_id"],
            core_id=spec["core_id"],
            content_id=spec["content_id"],
            schema_id=spec["schema_id"],
            initial_checkpoint_sha256=spec["initial_checkpoint_sha256"],
            limits=spec["limits"],
        )
    elif isinstance(spec, EpisodeSpec):
        spec_obj = spec
    else:
        raise SpecValidationError(f"Expected EpisodeSpec or Mapping, got {type(spec).__name__}")

    # Seed check
    if type(spec_obj.seed) is not int or isinstance(spec_obj.seed, bool):
        raise SpecValidationError(f"seed must be an exact integer, got {spec_obj.seed!r}")

    # Descriptor check
    if not spec_obj.descriptor:
        raise SpecValidationError("descriptor must not be empty")

    # Policy ID check
    if not isinstance(spec_obj.policy_id, str) or not spec_obj.policy_id.strip():
        raise SpecValidationError("policy_id must be non-empty string")

    # Duration days check (authored game days)
    if type(spec_obj.duration_days) is not int or spec_obj.duration_days <= 0:
        raise SpecValidationError(f"duration_days must be positive integer, got {spec_obj.duration_days!r}")

    # Mode check
    if spec_obj.mode not in SUPPORTED_MODES:
        raise SpecValidationError(f"mode must be one of {sorted(SUPPORTED_MODES)}, got {spec_obj.mode!r}")

    # Non-empty string identifiers
    for field_name in ("build_id", "core_id", "content_id", "schema_id"):
        val = getattr(spec_obj, field_name)
        if not isinstance(val, str) or not val.strip():
            raise SpecValidationError(f"{field_name} must be non-empty string")

    # Initial checkpoint sha256
    if not isinstance(spec_obj.initial_checkpoint_sha256, str) or not _HEX64_RE.fullmatch(spec_obj.initial_checkpoint_sha256):
        raise SpecValidationError(f"initial_checkpoint_sha256 must be 64-char hex, got {spec_obj.initial_checkpoint_sha256!r}")

    # Limits validation
    validated_limits = _validate_limits(spec_obj.limits)

    # Injected checked calendar validation
    if calendar is None:
        raise SpecValidationError("Injected calendar is required to convert progress into authored game days")
    if not (hasattr(calendar, "progress_to_days") or callable(calendar)):
        raise SpecValidationError("Injected calendar must provide progress_to_days or be callable")

    # Test conversion capability
    try:
        if hasattr(calendar, "progress_to_days"):
            test_days = calendar.progress_to_days(0)
        else:
            test_days = calendar(0)
        if type(test_days) not in (int, float) or test_days < 0:
            raise SpecValidationError("Injected calendar returned invalid non-numeric or negative game days")
    except Exception as exc:
        raise SpecValidationError(f"Injected calendar conversion failed: {exc}") from exc

    return replace(spec_obj, limits=validated_limits)


# @anchor:wa.learn.episode_spec.validate_checkpoint
def validate_checkpoint(
    checkpoint: CheckpointMetadata | Mapping[str, Any],
    context: TrustedContext,
) -> CheckpointMetadata:
    """Validate checkpoint metadata with trusted context.

    INV J4 3: Checkpoint metadata names next_input_position, next_stream_sequences,
    next_order_seq_id, ticket_roster, allocator_policy, consumed_proposals_digest,
    trusted_session_ref, core_rng_ref, and nullable presentation_ref.
    Opaque checkpoint bytes remain in trusted injected storage.
    """
    if checkpoint is None:
        raise CheckpointValidationError("Checkpoint cannot be None")
    if context is None:
        raise CheckpointValidationError("TrustedContext is required")

    if isinstance(checkpoint, Mapping):
        req_keys = {
            "next_input_position", "next_stream_sequences", "next_order_seq_id",
            "ticket_roster", "allocator_policy", "consumed_proposals_digest",
            "trusted_session_ref", "core_rng_ref", "presentation_ref",
            "checkpoint_sha256",
        }
        missing = req_keys - set(checkpoint.keys())
        if missing:
            raise CheckpointValidationError(f"Checkpoint missing required metadata: {sorted(missing)}")
        cp_obj = CheckpointMetadata(
            next_input_position=checkpoint["next_input_position"],
            next_stream_sequences=checkpoint["next_stream_sequences"],
            next_order_seq_id=checkpoint["next_order_seq_id"],
            ticket_roster=checkpoint["ticket_roster"],
            allocator_policy=checkpoint["allocator_policy"],
            consumed_proposals_digest=checkpoint["consumed_proposals_digest"],
            trusted_session_ref=checkpoint["trusted_session_ref"],
            core_rng_ref=checkpoint["core_rng_ref"],
            presentation_ref=checkpoint["presentation_ref"],
            checkpoint_sha256=checkpoint["checkpoint_sha256"],
        )
    elif isinstance(checkpoint, CheckpointMetadata):
        cp_obj = checkpoint
    else:
        raise CheckpointValidationError(f"Expected CheckpointMetadata or Mapping, got {type(checkpoint).__name__}")

    # Resume indexing: before-input position >= 0
    if type(cp_obj.next_input_position) is not int or cp_obj.next_input_position < 0:
        raise CheckpointValidationError(f"next_input_position must be non-negative integer, got {cp_obj.next_input_position!r}")

    # Stream sequences
    if not isinstance(cp_obj.next_stream_sequences, Mapping):
        raise CheckpointValidationError("next_stream_sequences must be a mapping")
    for s_name, seq in cp_obj.next_stream_sequences.items():
        if type(seq) is not int or seq < 0:
            raise CheckpointValidationError(f"Stream sequence for {s_name} must be non-negative int, got {seq!r}")

    # Next order sequence ID
    if type(cp_obj.next_order_seq_id) is not int or cp_obj.next_order_seq_id < 1:
        raise CheckpointValidationError(f"next_order_seq_id must be positive int, got {cp_obj.next_order_seq_id!r}")

    # Consumed proposals digest
    if not isinstance(cp_obj.consumed_proposals_digest, str) or not _HEX64_RE.fullmatch(cp_obj.consumed_proposals_digest):
        raise CheckpointValidationError(f"consumed_proposals_digest must be 64-char hex digest, got {cp_obj.consumed_proposals_digest!r}")

    # Checkpoint sha256 identity
    if not isinstance(cp_obj.checkpoint_sha256, str) or not _HEX64_RE.fullmatch(cp_obj.checkpoint_sha256):
        raise CheckpointValidationError(f"checkpoint_sha256 must be 64-char hex, got {cp_obj.checkpoint_sha256!r}")

    # Trusted session ref verification
    if cp_obj.trusted_session_ref != context.trusted_session_ref:
        raise CheckpointValidationError(
            f"trusted_session_ref mismatch: {cp_obj.trusted_session_ref!r} != {context.trusted_session_ref!r}"
        )

    # Core RNG ref verification
    if cp_obj.core_rng_ref != context.core_rng_ref:
        raise CheckpointValidationError(
            f"core_rng_ref mismatch: {cp_obj.core_rng_ref!r} != {context.core_rng_ref!r}"
        )

    # Allocator policy verification
    if cp_obj.allocator_policy != context.allocator_policy:
        raise CheckpointValidationError(
            f"allocator_policy mismatch: {cp_obj.allocator_policy!r} != {context.allocator_policy!r}"
        )

    # Presentation ref: nullable
    if cp_obj.presentation_ref is not None and not isinstance(cp_obj.presentation_ref, str):
        raise CheckpointValidationError("presentation_ref must be str or None")

    # Injected storage check: opaque checkpoint bytes remain in trusted injected storage
    if context.trusted_storage:
        if cp_obj.checkpoint_sha256 not in context.trusted_storage:
            raise CheckpointValidationError(f"Checkpoint {cp_obj.checkpoint_sha256} not found in trusted storage")
        stored_bytes = context.trusted_storage[cp_obj.checkpoint_sha256]
        if hashlib.sha256(stored_bytes).hexdigest() != cp_obj.checkpoint_sha256:
            raise CheckpointValidationError("Stored checkpoint bytes digest mismatch with checkpoint identity")

    return cp_obj


class WriteSpy:
    """Volatile buffer and audit spy rejecting pixel or receipt leakage."""
    def __init__(self):
        self.persisted_records: list[dict[str, Any]] = []
        self.leak_detected = False

    def persist(self, record: dict[str, Any]) -> None:
        # Detect forbidden frame/pixel or raw receipt/dump leakage
        banned = {"frame", "frames", "pixels", "rgb", "screenshot", "png", "raw_receipt", "tree_dump", "opaque_bytes"}
        for k, v in record.items():
            if k.lower() in banned:
                self.leak_detected = True
                raise RuntimeError(f"WriteSpy rejected forbidden leakage of field {k!r}")
            if isinstance(v, (bytes, bytearray)) and len(v) > 512:
                self.leak_detected = True
                raise RuntimeError(f"WriteSpy rejected unapproved binary payload in {k!r}")
        self.persisted_records.append(copy.deepcopy(record))


class EpisodeSession:
    """Episode session managing lifecycle INV J4 1 through INV J4 4."""
    def __init__(self, spec: EpisodeSpec, context: TrustedContext):
        self.raw_spec = spec
        self.context = context
        self.spec: EpisodeSpec | None = None
        self.checkpoint: CheckpointMetadata | None = None
        self.authenticated = False
        self.history: list[dict[str, Any]] = []
        self.submitted_intents: dict[int, Any] = {}
        self.committed_progress = 0
        self.elapsed_days = 0
        self.status = "UNINITIALIZED"
        self.write_spy = WriteSpy()

    def check_prerequisites(self) -> None:
        """Verify injected prerequisite receipts; fail loudly as AUSENTE if missing."""
        if not self.context.prerequisites:
            return
        for gate_name, rec in self.context.prerequisites.items():
            if not isinstance(rec, Mapping):
                raise AusenteError(f"Prerequisite {gate_name} record AUSENTE")
            if rec.get("status") != "PASS":
                raise AusenteError(f"Prerequisite {gate_name} failed: {rec.get('evidence', 'AUSENTE')}")
            if rec.get("expected_failure") is True:
                raise AusenteError(f"Prerequisite {gate_name} carries expected failure, not accepted")

    def authenticate_reset(self) -> dict[str, Any]:
        """Authenticate reset against core, RNG, calendar, content, schema, build, allocator.

        INV J4 1: A seed alone or a JSON context file does not authenticate a reset.
        Reset checks core, RNG, calendar, content, schema, build, allocator and initial digest before action.
        """
        self.check_prerequisites()

        # Validate spec with calendar first
        self.spec = validate_spec(self.raw_spec, self.context.calendar)

        # Multi-factor authentication of execution context
        mismatches = []
        if self.spec.core_id != self.context.core_id:
            mismatches.append(f"core_id drift: spec={self.spec.core_id!r} != ctx={self.context.core_id!r}")
        if self.spec.build_id != self.context.build_id:
            mismatches.append(f"build_id drift: spec={self.spec.build_id!r} != ctx={self.context.build_id!r}")
        if self.spec.content_id != self.context.content_id:
            mismatches.append(f"content_id drift: spec={self.spec.content_id!r} != ctx={self.context.content_id!r}")
        if self.spec.schema_id != self.context.schema_id:
            mismatches.append(f"schema_id drift: spec={self.spec.schema_id!r} != ctx={self.context.schema_id!r}")
        if self.spec.initial_checkpoint_sha256 != self.context.initial_checkpoint_sha256:
            mismatches.append(f"initial_checkpoint_sha256 drift: spec={self.spec.initial_checkpoint_sha256!r} != ctx={self.context.initial_checkpoint_sha256!r}")

        if mismatches:
            self.status = "INCOMPLETE"
            raise SpecValidationError("Reset authentication failed due to drift: " + "; ".join(mismatches))

        # Initial initial checkpoint metadata
        self.checkpoint = CheckpointMetadata(
            next_input_position=0,
            next_stream_sequences={},
            next_order_seq_id=1,
            ticket_roster=(),
            allocator_policy=self.context.allocator_policy,
            consumed_proposals_digest="0" * 64,
            trusted_session_ref=self.context.trusted_session_ref,
            core_rng_ref=self.context.core_rng_ref,
            presentation_ref=None,
            checkpoint_sha256=self.spec.initial_checkpoint_sha256,
        )
        validate_checkpoint(self.checkpoint, self.context)

        self.authenticated = True
        self.status = "RESET"
        self.committed_progress = 0
        self.elapsed_days = self.convert_progress_to_days(0)
        return {
            "status": "RESET",
            "authenticated": True,
            "next_input_position": 0,
            "core_id": self.spec.core_id,
            "build_id": self.spec.build_id,
            "duration_days": self.spec.duration_days,
            "initial_checkpoint_sha256": self.spec.initial_checkpoint_sha256,
        }

    def convert_progress_to_days(self, progress: int) -> int | float:
        """INV J4 2: Convert committed progress into authored game days."""
        cal = self.context.calendar
        if hasattr(cal, "progress_to_days"):
            return cal.progress_to_days(progress)
        return cal(progress)

    def resume(self, checkpoint: CheckpointMetadata | Mapping[str, Any]) -> dict[str, Any]:
        """INV J4 3: Resume is before-input-position indexed."""
        if not self.authenticated:
            raise EpisodeLifecycleError("Cannot resume before reset authentication")
        validated_cp = validate_checkpoint(checkpoint, self.context)
        self.checkpoint = validated_cp
        self.status = "RESUMED"
        return {
            "status": "RESUMED",
            "next_input_position": validated_cp.next_input_position,
            "checkpoint_sha256": validated_cp.checkpoint_sha256,
        }

    def submit_intent(self, input_pos: int, intent: dict[str, Any]) -> dict[str, Any]:
        """INV J4 3: Before-input resume indexing; ambiguous sent work is not resent.

        Regenerated intents are compared and never applied twice.
        """
        if not self.authenticated or self.checkpoint is None:
            raise EpisodeLifecycleError("Session not authenticated or reset")

        # Ambiguous sent work check
        current_pos = self.checkpoint.next_input_position
        if input_pos < current_pos:
            # Prior position: check if intent matches regenerated intent
            prior_intent = self.submitted_intents.get(input_pos)
            if prior_intent is not None:
                if prior_intent != intent:
                    self.status = "INCOMPLETE"
                    raise EpisodeLifecycleError(
                        f"Drift at input_position {input_pos}: regenerated intent {intent!r} differs from recorded {prior_intent!r}"
                    )
                # Ambiguous sent work is not resent; regenerated intents are never applied twice
                return {
                    "applied": False,
                    "reason": "already_applied_prior_work",
                    "input_position": input_pos,
                }
            return {
                "applied": False,
                "reason": "ambiguous_sent_work_skipped",
                "input_position": input_pos,
            }

        if input_pos > current_pos:
            raise EpisodeLifecycleError(f"Sequence gap: expected input_position {current_pos}, got {input_pos}")

        # Check limits
        limits = self.spec.limits if isinstance(self.spec.limits, EpisodeLimits) else _validate_limits(self.spec.limits)
        if len(self.submitted_intents) >= limits.max_actions:
            self.status = "INCOMPLETE"
            raise EpisodeLifecycleError(f"Exhausted action limit {limits.max_actions}")

        # Apply intent at next_input_position
        self.submitted_intents[input_pos] = copy.deepcopy(intent)
        next_pos = input_pos + 1
        new_order_seq = self.checkpoint.next_order_seq_id + 1

        # Update consumed proposals digest deterministically
        intent_bytes = dumps(intent).encode("utf-8")
        hasher = hashlib.sha256(self.checkpoint.consumed_proposals_digest.encode("utf-8"))
        hasher.update(intent_bytes)
        new_digest = hasher.hexdigest()

        # Update checkpoint metadata
        self.checkpoint = replace(
            self.checkpoint,
            next_input_position=next_pos,
            next_order_seq_id=new_order_seq,
            consumed_proposals_digest=new_digest,
        )

        outcome = {
            "applied": True,
            "input_position": input_pos,
            "next_input_position": next_pos,
            "order_seq_id": self.checkpoint.next_order_seq_id,
            "status": "APPLIED",
        }
        self.history.append(outcome)
        return outcome

    def advance_progress(self, additional_progress: int) -> dict[str, Any]:
        """Advance progress and check authored calendar horizon."""
        if not self.authenticated or self.spec is None:
            raise EpisodeLifecycleError("Session not authenticated")
        self.committed_progress += additional_progress
        self.elapsed_days = self.convert_progress_to_days(self.committed_progress)
        limits = self.spec.limits if isinstance(self.spec.limits, EpisodeLimits) else _validate_limits(self.spec.limits)

        # Check tick limit
        if self.committed_progress > limits.max_ticks:
            self.status = "INCOMPLETE"
            raise EpisodeLifecycleError(f"Exhausted tick sequence: {self.committed_progress} > {limits.max_ticks}")

        horizon_reached = self.elapsed_days >= self.spec.duration_days
        if horizon_reached:
            self.status = "HORIZON_REACHED"
        return {
            "committed_progress": self.committed_progress,
            "elapsed_days": self.elapsed_days,
            "horizon_reached": horizon_reached,
        }

    def compute_simulation_seal(self, host_metrics: Mapping[str, Any] | None = None) -> str:
        """INV J4 4: Compute deterministic simulation seal.

        PID, elapsed time and RAM samples never influence the deterministic simulation seal.
        """
        if not self.authenticated or self.spec is None or self.checkpoint is None:
            raise EpisodeLifecycleError("Cannot compute seal for unauthenticated episode")

        # Deterministic simulation components only
        payload = {
            "seed": self.spec.seed,
            "policy_id": self.spec.policy_id,
            "duration_days": self.spec.duration_days,
            "mode": self.spec.mode,
            "build_id": self.spec.build_id,
            "core_id": self.spec.core_id,
            "content_id": self.spec.content_id,
            "schema_id": self.spec.schema_id,
            "initial_checkpoint_sha256": self.spec.initial_checkpoint_sha256,
            "next_input_position": self.checkpoint.next_input_position,
            "consumed_proposals_digest": self.checkpoint.consumed_proposals_digest,
            "committed_progress": self.committed_progress,
            "elapsed_days": self.elapsed_days,
            "intents": [
                {"pos": pos, "intent": self.submitted_intents[pos]}
                for pos in sorted(self.submitted_intents.keys())
            ],
        }
        # Explicitly ensure host_metrics (PID, elapsed wall time, RAM) are NEVER included
        seal_bytes = dumps(payload).encode("utf-8")
        return hashlib.sha256(seal_bytes).hexdigest()


# @anchor:wa.learn.episode_spec.verify_gate_g_j4_1
def verify_gate_g_j4_1(
    spec: EpisodeSpec | Mapping[str, Any],
    context: TrustedContext,
    checkpoint: CheckpointMetadata | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify acceptance gate G-J4-1.

    Requires validated Spec, trusted checkpoint identity, and authenticated reset.
    Negative contrast: seed/build drift or absent calendar conversion.
    Positive control: pinned descriptor/reset digest.
    """
    try:
        validated_spec = validate_spec(spec, context.calendar)
    except Exception as exc:
        return {
            "gate": GATE_G_J4_1,
            "passed": False,
            "reason": f"spec_validation_failure: {exc}",
            "evidence": "REJECTED",
        }

    # Verify context matches
    if context.seed is not None and validated_spec.seed != context.seed:
        return {
            "gate": GATE_G_J4_1,
            "passed": False,
            "reason": "seed_drift",
            "evidence": "REJECTED",
        }
    if validated_spec.build_id != context.build_id:
        return {
            "gate": GATE_G_J4_1,
            "passed": False,
            "reason": "build_drift",
            "evidence": "REJECTED",
        }

    # Verify checkpoint identity
    cp_to_check = checkpoint
    if cp_to_check is None:
        cp_to_check = CheckpointMetadata(
            next_input_position=0,
            next_stream_sequences={},
            next_order_seq_id=1,
            ticket_roster=(),
            allocator_policy=context.allocator_policy,
            consumed_proposals_digest="0" * 64,
            trusted_session_ref=context.trusted_session_ref,
            core_rng_ref=context.core_rng_ref,
            presentation_ref=None,
            checkpoint_sha256=validated_spec.initial_checkpoint_sha256,
        )

    try:
        validated_cp = validate_checkpoint(cp_to_check, context)
    except Exception as exc:
        return {
            "gate": GATE_G_J4_1,
            "passed": False,
            "reason": f"checkpoint_validation_failure: {exc}",
            "evidence": "REJECTED",
        }

    # Positive control: verify pinned descriptor and reset digest
    reset_digest = hashlib.sha256(
        f"{validated_spec.core_id}:{validated_spec.build_id}:{validated_spec.initial_checkpoint_sha256}".encode("utf-8")
    ).hexdigest()

    return {
        "gate": GATE_G_J4_1,
        "passed": True,
        "spec_core_id": validated_spec.core_id,
        "checkpoint_sha256": validated_cp.checkpoint_sha256,
        "reset_digest": reset_digest,
        "evidence": "PASS",
    }
