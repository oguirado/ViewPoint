# @module {"id":"wa.remember.episode_provenance","layer":1,"owner":"REMEMBER","deps":["wa.state.schema","wa.remember.skills"],"book":"HB-WA-s3-remember"}
"""Deterministic episode provenance, holdout verification, and atomic indexing.

Exclusive production owner for jq2b-j9-episode-provenance.
Implements the episode provenance lifecycle following INV J9 1 through INV J9 4:
- INV J9 1: Exact provenance and holdout identity. Manifest, trace, and checkpoint
  sealed identities; rejects foreign build, content, schema, or source despite identical seed.
- INV J9 2: Replay support mode & branch traversal. Modes core, grounding, and branch
  distinguish trusted core reexecution, recorded-observation grounding, and supported
  recorded-branch traversal. Missing branch is unsupported until reset and actual execution;
  no invented reward or failure.
- INV J9 3: Economic success & outcome verification. Proposals, refusals, accepted navigation,
  and unverified terminals cannot become economic success. Missing frames are unavailable
  evidence (AUSENTE) rather than reconstructed screenshots.
- INV J9 4: Transactional storage, atomic episode indexing, and promotion. Serialized by
  episode, skill, and evidence version. Conflicting support or stale completion refuses
  atomically. Promotion requires public replay, skill admission, and exact provenance.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from . import skills
from ..state.schema import dumps

__all__ = (
    "ProvenanceError",
    "ProvenanceRejected",
    "ProvenanceRefused",
    "ProvenanceConflict",
    "AusenteError",
    "ProvenanceLeakageError",
    "EpisodeManifest",
    "ProvenanceRecord",
    "HoldoutIdentity",
    "EpisodeOutcome",
    "BoundProvenanceSkill",
    "EpisodeProvenanceIndex",
    "WriteSpy",
    "bind_provenance",
    "evaluate_gate_g_j9_1",
    "promote_skill",
    "evaluate_economic_success",
    "canonical_manifest_sha256",
    "PROVENANCE_FIELDS",
    "REPLAY_MODES",
    "VALID_REPLAY_MODES",
    "SOURCE_KINDS",
    "ECONOMIC_SUCCESS_DISALLOWED_STATUSES",
    "INV_J9_INVARIANTS",
)

PROVENANCE_FIELDS = (
    "episode_manifest_sha256",
    "trace_sha256",
    "initial_checkpoint_sha256",
    "build_id",
    "content_id",
    "schema_id",
    "source_kind",
    "replay_mode",
    "support_path",
    "verification_ref",
    "evidence_version",
)

REPLAY_MODES = frozenset({"core", "grounding", "branch"})
VALID_REPLAY_MODES = REPLAY_MODES

SOURCE_KINDS = frozenset({"holdings", "web", "browser", "synthetic", "capsule", "cdp"})

ECONOMIC_SUCCESS_DISALLOWED_STATUSES = frozenset({
    "proposal",
    "refusal",
    "accepted",
    "accepted_navigation",
    "navigation",
    "unverified",
    "unverified_terminal",
    "unsupported",
})

INV_J9_INVARIANTS = ("INV-J9-1", "INV-J9-2", "INV-J9-3", "INV-J9-4")

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_DIAGNOSTIC_EXCLUSIONS = frozenset({
    "pid", "elapsed_time", "elapsed_wall_ns", "elapsed_time_vm",
    "ram_samples", "ram_usage_bytes", "vm_timing", "host_time", "wall_clock"
})


class ProvenanceError(ValueError):
    """Base error for episode provenance and invariant violations."""


class ProvenanceRejected(ProvenanceError):
    """Raised when provenance validation, sealed identity, or holdout checks fail."""


class ProvenanceRefused(ProvenanceError):
    """Raised when transactional storage, append, or promotion is refused."""


class ProvenanceConflict(ProvenanceRefused):
    """Raised on conflicting support paths or contradictory provenance identity."""


class AusenteError(ProvenanceError):
    """Raised loudly when required evidence, components, or frames are AUSENTE."""


class ProvenanceLeakageError(ProvenanceError):
    """Raised when pixel frame or raw hook receipt leakage is detected."""


def _check_str(val: Any, name: str) -> str:
    if not isinstance(val, str) or not val.strip():
        raise ProvenanceRejected(f"{name} must be a non-empty string")
    return val


def _check_int(val: Any, name: str, min_val: int = 0) -> int:
    if type(val) is not int or val < min_val:
        raise ProvenanceRejected(f"{name} must be an integer >= {min_val}")
    return val


def _check_sha256(val: Any, name: str) -> str:
    if not isinstance(val, str) or not _HEX64_RE.fullmatch(val):
        raise ProvenanceRejected(f"{name} must be a valid 64-character lowercase hex SHA-256")
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
    raise ProvenanceRejected(f"Unsupported canonical serialization type: {type(obj).__name__}")


def canonical_json_str(obj: Any) -> str:
    """Format json deterministically with sorted keys and compact separators."""
    cleaned = _clean_json(obj)
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class EpisodeManifest:
    """Sealed specification and identity for an episode execution."""
    manifest_id: str
    seed: int
    build_id: str
    content_id: str
    schema_id: str
    source_kind: str
    trace_sha256: str
    initial_checkpoint_sha256: str
    support_branches: tuple[str, ...] = ()
    limits: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        _check_str(self.manifest_id, "manifest_id")
        _check_int(self.seed, "seed")
        _check_str(self.build_id, "build_id")
        _check_str(self.content_id, "content_id")
        _check_str(self.schema_id, "schema_id")
        _check_str(self.source_kind, "source_kind")
        _check_sha256(self.trace_sha256, "trace_sha256")
        _check_sha256(self.initial_checkpoint_sha256, "initial_checkpoint_sha256")
        if not isinstance(self.support_branches, (tuple, list)):
            raise ProvenanceRejected("support_branches must be a tuple or list of strings")
        for b in self.support_branches:
            _check_str(b, "support_branch")

    def canonical_bytes(self) -> bytes:
        data = {
            "manifest_id": self.manifest_id,
            "seed": self.seed,
            "build_id": self.build_id,
            "content_id": self.content_id,
            "schema_id": self.schema_id,
            "source_kind": self.source_kind,
            "trace_sha256": self.trace_sha256,
            "initial_checkpoint_sha256": self.initial_checkpoint_sha256,
            "support_branches": list(self.support_branches),
            "limits": self.limits,
            "metadata": self.metadata,
        }
        return canonical_json_str(data).encode("utf-8")

    def compute_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def canonical_manifest_sha256(manifest: EpisodeManifest | Mapping[str, Any]) -> str:
    """Compute deterministic SHA-256 for an EpisodeManifest instance or dictionary."""
    if isinstance(manifest, EpisodeManifest):
        return manifest.compute_sha256()
    if isinstance(manifest, Mapping):
        # Extract and validate fields
        _check_str(manifest.get("manifest_id", "manifest"), "manifest_id")
        _check_int(manifest["seed"], "seed")
        _check_str(manifest["build_id"], "build_id")
        _check_str(manifest["content_id"], "content_id")
        _check_str(manifest["schema_id"], "schema_id")
        _check_str(manifest["source_kind"], "source_kind")
        trace_sha = _check_sha256(manifest["trace_sha256"], "trace_sha256")
        init_cp = _check_sha256(manifest["initial_checkpoint_sha256"], "initial_checkpoint_sha256")
        branches = list(manifest.get("support_branches", ()))
        data = {
            "manifest_id": manifest.get("manifest_id", "manifest"),
            "seed": manifest["seed"],
            "build_id": manifest["build_id"],
            "content_id": manifest["content_id"],
            "schema_id": manifest["schema_id"],
            "source_kind": manifest["source_kind"],
            "trace_sha256": trace_sha,
            "initial_checkpoint_sha256": init_cp,
            "support_branches": branches,
            "limits": manifest.get("limits", {}),
            "metadata": manifest.get("metadata", {}),
        }
        return hashlib.sha256(canonical_json_str(data).encode("utf-8")).hexdigest()
    raise ProvenanceRejected("manifest must be EpisodeManifest or mapping")


@dataclass(frozen=True)
class ProvenanceRecord:
    """Exact provenance record carrying all 11 required fields."""
    episode_manifest_sha256: str
    trace_sha256: str
    initial_checkpoint_sha256: str
    build_id: str
    content_id: str
    schema_id: str
    source_kind: str
    replay_mode: str
    support_path: tuple[str, ...]
    verification_ref: str
    evidence_version: int

    def __post_init__(self):
        _check_sha256(self.episode_manifest_sha256, "episode_manifest_sha256")
        _check_sha256(self.trace_sha256, "trace_sha256")
        _check_sha256(self.initial_checkpoint_sha256, "initial_checkpoint_sha256")
        _check_str(self.build_id, "build_id")
        _check_str(self.content_id, "content_id")
        _check_str(self.schema_id, "schema_id")
        _check_str(self.source_kind, "source_kind")
        if self.replay_mode not in REPLAY_MODES:
            raise ProvenanceRejected(
                f"replay_mode must be one of {sorted(REPLAY_MODES)}, got {self.replay_mode!r}"
            )
        if not isinstance(self.support_path, tuple):
            raise ProvenanceRejected("support_path must be a tuple of path components")
        _check_str(self.verification_ref, "verification_ref")
        _check_int(self.evidence_version, "evidence_version", min_val=1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_manifest_sha256": self.episode_manifest_sha256,
            "trace_sha256": self.trace_sha256,
            "initial_checkpoint_sha256": self.initial_checkpoint_sha256,
            "build_id": self.build_id,
            "content_id": self.content_id,
            "schema_id": self.schema_id,
            "source_kind": self.source_kind,
            "replay_mode": self.replay_mode,
            "support_path": list(self.support_path),
            "verification_ref": self.verification_ref,
            "evidence_version": self.evidence_version,
        }


@dataclass(frozen=True)
class HoldoutIdentity:
    """Pinned holdout contract identity used to detect foreign builds or mutations."""
    build_id: str
    content_id: str
    schema_id: str
    source_kind: str
    pinned_manifest_sha256: str | None = None
    pinned_checkpoint_sha256: str | None = None

    def __post_init__(self):
        _check_str(self.build_id, "build_id")
        _check_str(self.content_id, "content_id")
        _check_str(self.schema_id, "schema_id")
        _check_str(self.source_kind, "source_kind")
        if self.pinned_manifest_sha256 is not None:
            _check_sha256(self.pinned_manifest_sha256, "pinned_manifest_sha256")
        if self.pinned_checkpoint_sha256 is not None:
            _check_sha256(self.pinned_checkpoint_sha256, "pinned_checkpoint_sha256")


@dataclass(frozen=True)
class EpisodeOutcome:
    """Outcome of an episode branch traversal."""
    status: str
    is_terminal: bool
    terminal_verified: bool
    reward: float | int | None = None
    economic_success: bool = False
    reason: str | None = None

    def __post_init__(self):
        _check_str(self.status, "status")
        # Invariant INV J9 3:
        # A proposal, refusal, accepted navigation or unverified terminal cannot become economic success.
        if self.economic_success:
            if self.status in ECONOMIC_SUCCESS_DISALLOWED_STATUSES:
                raise ProvenanceRejected(
                    f"Status {self.status!r} cannot become economic success (INV J9 3)"
                )
            if not self.is_terminal or not self.terminal_verified:
                raise ProvenanceRejected(
                    "Economic success requires a verified terminal outcome (INV J9 3)"
                )
            if self.reward is None:
                raise ProvenanceRejected(
                    "Economic success requires explicit verified reward, not None (INV J9 3)"
                )


def evaluate_economic_success(
    status: str,
    is_terminal: bool,
    terminal_verified: bool,
    reward: float | int | None = None,
) -> bool:
    """Evaluate whether an episode outcome satisfies INV J9 3 economic success."""
    if status in ECONOMIC_SUCCESS_DISALLOWED_STATUSES:
        return False
    if not is_terminal or not terminal_verified:
        return False
    if reward is None or reward <= 0:
        return False
    return True


@dataclass(frozen=True)
class BoundProvenanceSkill:
    """Immutable outer provenance wrapper preserving existing inner skill schema."""
    skill: dict[str, Any]
    provenance: ProvenanceRecord
    is_supported: bool
    economic_success: bool
    evidence_version: int
    outcome: EpisodeOutcome | None = None
    manifest_summary: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.skill, dict):
            raise ProvenanceRejected("skill must be a dictionary")
        if not isinstance(self.provenance, ProvenanceRecord):
            raise ProvenanceRejected("provenance must be a ProvenanceRecord")
        _check_int(self.evidence_version, "evidence_version", min_val=1)

    @property
    def skill_id(self) -> str:
        return self.skill.get("id", "")


class WriteSpy:
    """Security spy ensuring no raw frames, pixel buffers, or receipts leak to disk."""
    def __init__(self):
        self.leakage_detected = False
        self.leaked_paths: list[str] = []

    def check_data(self, data: bytes | str | Any, path_or_label: str) -> None:
        if isinstance(data, (bytes, bytearray)):
            if data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"):
                self.leakage_detected = True
                self.leaked_paths.append(path_or_label)
                raise ProvenanceLeakageError(f"WriteSpy: frame pixel leakage detected for {path_or_label}")
        if isinstance(data, str):
            if "iVBORw0KGgo" in data:  # Base64 encoded PNG signature
                self.leakage_detected = True
                self.leaked_paths.append(path_or_label)
                raise ProvenanceLeakageError(f"WriteSpy: encoded frame leakage detected for {path_or_label}")


# @anchor:wa.remember.episode_provenance.bind_provenance
def bind_provenance(
    skill: dict[str, Any],
    episode_manifest: EpisodeManifest | Mapping[str, Any],
    *,
    trace_sha256: str | None = None,
    initial_checkpoint_sha256: str | None = None,
    replay_mode: str = "core",
    support_path: Sequence[str] | str = (),
    verification_ref: str | None = None,
    evidence_version: int | None = None,
    holdout_identity: HoldoutIdentity | None = None,
    is_executed: bool = True,
    outcome: EpisodeOutcome | None = None,
    write_spy: WriteSpy | None = None,
) -> BoundProvenanceSkill:
    """Bind an admitted skill and episode manifest into an immutable outer provenance wrapper.

    Follows INV J9 1 through INV J9 4:
    - Verifies existing skill schema and evidence.
    - Computes and seals episode_manifest_sha256.
    - Validates exact build, content, schema, and source_kind against holdout identity.
    - Distinguishes replay modes: core, grounding, and branch.
    - Checks missing branches: unsupported until reset and actual execution.
    - Checks economic success: proposals, refusals, navigation, and unverified terminals disallowed.
    """
    if not isinstance(skill, dict):
        raise ProvenanceRejected("skill must be a dict")

    # Validate existing skill schema using skills.admit or required keys
    # Do not mutate the existing skill dictionary schema
    missing_skill_keys = [k for k in skills.REQUIRED if k not in skill]
    if missing_skill_keys:
        raise ProvenanceRejected(f"skill schema missing required keys: {missing_skill_keys}")

    skill_copy = copy.deepcopy(skill)

    # Resolve manifest fields
    if isinstance(episode_manifest, EpisodeManifest):
        manifest_id = episode_manifest.manifest_id
        seed = episode_manifest.seed
        build_id = episode_manifest.build_id
        content_id = episode_manifest.content_id
        schema_id = episode_manifest.schema_id
        source_kind = episode_manifest.source_kind
        manifest_trace_sha = episode_manifest.trace_sha256
        manifest_init_cp = episode_manifest.initial_checkpoint_sha256
        manifest_branches = episode_manifest.support_branches
    elif isinstance(episode_manifest, Mapping):
        manifest_id = str(episode_manifest.get("manifest_id", "manifest"))
        seed = _check_int(episode_manifest["seed"], "manifest.seed")
        build_id = _check_str(episode_manifest["build_id"], "manifest.build_id")
        content_id = _check_str(episode_manifest["content_id"], "manifest.content_id")
        schema_id = _check_str(episode_manifest["schema_id"], "manifest.schema_id")
        source_kind = _check_str(episode_manifest["source_kind"], "manifest.source_kind")
        manifest_trace_sha = _check_sha256(episode_manifest["trace_sha256"], "manifest.trace_sha256")
        manifest_init_cp = _check_sha256(
            episode_manifest["initial_checkpoint_sha256"], "manifest.initial_checkpoint_sha256"
        )
        manifest_branches = tuple(episode_manifest.get("support_branches", ()))
    else:
        raise ProvenanceRejected("episode_manifest must be EpisodeManifest or mapping")

    # Compute sealed manifest SHA-256
    manifest_sha256 = canonical_manifest_sha256(episode_manifest)

    # Invariant INV J9 1: Holdout identity verification
    if holdout_identity is not None:
        if build_id != holdout_identity.build_id:
            raise ProvenanceRejected(
                f"INV J9 1 violation: build_id mismatch (expected {holdout_identity.build_id!r}, got {build_id!r})"
            )
        if content_id != holdout_identity.content_id:
            raise ProvenanceRejected(
                f"INV J9 1 violation: content_id mismatch (expected {holdout_identity.content_id!r}, got {content_id!r})"
            )
        if schema_id != holdout_identity.schema_id:
            raise ProvenanceRejected(
                f"INV J9 1 violation: schema_id mismatch (expected {holdout_identity.schema_id!r}, got {schema_id!r})"
            )
        if source_kind != holdout_identity.source_kind:
            raise ProvenanceRejected(
                f"INV J9 1 violation: source_kind mismatch (expected {holdout_identity.source_kind!r}, got {source_kind!r})"
            )
        if holdout_identity.pinned_manifest_sha256 and manifest_sha256 != holdout_identity.pinned_manifest_sha256:
            raise ProvenanceRejected(
                "INV J9 1 violation: pinned_manifest_sha256 mismatch"
            )
        if holdout_identity.pinned_checkpoint_sha256 and manifest_init_cp != holdout_identity.pinned_checkpoint_sha256:
            raise ProvenanceRejected(
                "INV J9 1 violation: pinned_checkpoint_sha256 mismatch"
            )

    # Trace and checkpoint identities
    resolved_trace_sha = manifest_trace_sha if trace_sha256 is None else _check_sha256(trace_sha256, "trace_sha256")
    resolved_init_cp = manifest_init_cp if initial_checkpoint_sha256 is None else _check_sha256(initial_checkpoint_sha256, "initial_checkpoint_sha256")

    if trace_sha256 is not None and trace_sha256 != manifest_trace_sha:
        raise ProvenanceRejected("Provided trace_sha256 conflicts with sealed manifest")
    if initial_checkpoint_sha256 is not None and initial_checkpoint_sha256 != manifest_init_cp:
        raise ProvenanceRejected("Provided initial_checkpoint_sha256 conflicts with sealed manifest")

    # Invariant INV J9 2: Replay support mode
    if replay_mode not in REPLAY_MODES:
        raise ProvenanceRejected(f"replay_mode must be one of {sorted(REPLAY_MODES)}, got {replay_mode!r}")

    # Canonicalize support path
    if isinstance(support_path, str):
        norm_support_path = (support_path,) if support_path else ()
    elif isinstance(support_path, (list, tuple)):
        norm_support_path = tuple(str(p) for p in support_path)
    else:
        raise ProvenanceRejected("support_path must be a string or sequence of strings")

    # Branch traversal support check:
    # "Missing branch is unsupported until reset and actual execution; no invented reward or failure."
    is_supported = True
    if replay_mode == "branch":
        if not norm_support_path:
            is_supported = False
        elif not is_executed:
            is_supported = False
        elif manifest_branches and not any(p in manifest_branches for p in norm_support_path):
            is_supported = False

    # Evidence version
    skill_ev = skill_copy.get("evidence", {})
    if evidence_version is None:
        resolved_version = skill_ev.get("version", 1) if isinstance(skill_ev, dict) else 1
    else:
        resolved_version = _check_int(evidence_version, "evidence_version", min_val=1)

    # Verification ref
    if verification_ref is None:
        if isinstance(skill_ev, dict) and skill_ev.get("verified_by"):
            resolved_vref = f"verified_by:{skill_ev['verified_by']}"
        else:
            resolved_vref = f"verified:replay:{replay_mode}"
    else:
        resolved_vref = _check_str(verification_ref, "verification_ref")

    # Invariant INV J9 3: Economic success & outcome
    economic_success = False
    if outcome is not None:
        if not is_supported:
            economic_success = False
        else:
            economic_success = outcome.economic_success

    if write_spy is not None:
        write_spy.check_data(manifest_sha256, "manifest_sha256")
        write_spy.check_data(resolved_trace_sha, "trace_sha256")
        write_spy.check_data(resolved_init_cp, "initial_checkpoint_sha256")

    record = ProvenanceRecord(
        episode_manifest_sha256=manifest_sha256,
        trace_sha256=resolved_trace_sha,
        initial_checkpoint_sha256=resolved_init_cp,
        build_id=build_id,
        content_id=content_id,
        schema_id=schema_id,
        source_kind=source_kind,
        replay_mode=replay_mode,
        support_path=norm_support_path,
        verification_ref=resolved_vref,
        evidence_version=resolved_version,
    )

    manifest_summary = {
        "manifest_id": manifest_id,
        "seed": seed,
        "build_id": build_id,
        "content_id": content_id,
        "schema_id": schema_id,
        "source_kind": source_kind,
    }

    return BoundProvenanceSkill(
        skill=skill_copy,
        provenance=record,
        is_supported=is_supported,
        economic_success=economic_success,
        evidence_version=resolved_version,
        outcome=outcome,
        manifest_summary=manifest_summary,
    )


# @anchor:wa.remember.episode_provenance.evaluate_gate_g_j9_1
def evaluate_gate_g_j9_1(
    bound: BoundProvenanceSkill,
    holdout: HoldoutIdentity,
    *,
    expected_seed: int | None = None,
) -> dict[str, Any]:
    """Evaluate Acceptance Gate G-J9-1: Exact provenance and holdout identity.

    - Positive control: pinned episode and checkpoint match holdout.
    - Negative contrast: same seed with foreign build accepted? REJECTED.
    """
    if not isinstance(bound, BoundProvenanceSkill):
        raise ProvenanceRejected("bound must be an instance of BoundProvenanceSkill")
    if not isinstance(holdout, HoldoutIdentity):
        raise ProvenanceRejected("holdout must be an instance of HoldoutIdentity")

    prov = bound.provenance
    summary = bound.manifest_summary

    build_matches = (prov.build_id == holdout.build_id)
    content_matches = (prov.content_id == holdout.content_id)
    schema_matches = (prov.schema_id == holdout.schema_id)
    source_matches = (prov.source_kind == holdout.source_kind)

    manifest_matches = True
    if holdout.pinned_manifest_sha256:
        manifest_matches = (prov.episode_manifest_sha256 == holdout.pinned_manifest_sha256)

    checkpoint_matches = True
    if holdout.pinned_checkpoint_sha256:
        checkpoint_matches = (prov.initial_checkpoint_sha256 == holdout.pinned_checkpoint_sha256)

    seed_matches = True
    if expected_seed is not None:
        seed_matches = (summary.get("seed") == expected_seed)

    passed = (
        build_matches
        and content_matches
        and schema_matches
        and source_matches
        and manifest_matches
        and checkpoint_matches
        and seed_matches
        and bound.is_supported
    )

    reason = "PASS" if passed else (
        "foreign_build" if not build_matches else
        "content_mismatch" if not content_matches else
        "schema_mismatch" if not schema_matches else
        "source_kind_mismatch" if not source_matches else
        "manifest_mismatch" if not manifest_matches else
        "checkpoint_mismatch" if not checkpoint_matches else
        "seed_mismatch" if not seed_matches else
        "unsupported_branch"
    )

    return {
        "gate": "G-J9-1",
        "status": "PASS" if passed else "FAIL",
        "passed": passed,
        "build_matches": build_matches,
        "content_matches": content_matches,
        "schema_matches": schema_matches,
        "source_matches": source_matches,
        "manifest_matches": manifest_matches,
        "checkpoint_matches": checkpoint_matches,
        "reason": reason,
    }


# @anchor:wa.remember.episode_provenance.promote_skill
def promote_skill(
    bound: BoundProvenanceSkill,
    *,
    verify_replay: bool = True,
    expected_holdout: HoldoutIdentity | None = None,
) -> str:
    """Promote a skill to permanent storage.

    INV J9 4: Promotion requires public replay and skill admission plus exact
    source/build/checkpoint provenance.
    """
    if not isinstance(bound, BoundProvenanceSkill):
        raise ProvenanceRefused("Promotion requires BoundProvenanceSkill")

    if not bound.is_supported:
        raise ProvenanceRefused("Cannot promote skill on unsupported episode branch")

    prov = bound.provenance
    if not prov.build_id or not prov.source_kind or not prov.initial_checkpoint_sha256:
        raise ProvenanceRefused("Exact source/build/checkpoint provenance required for promotion")

    if expected_holdout is not None:
        gate_res = evaluate_gate_g_j9_1(bound, expected_holdout)
        if not gate_res["passed"]:
            raise ProvenanceRefused(f"Promotion failed Gate G-J9-1 holdout verification: {gate_res['reason']}")

    ev = bound.skill.get("evidence", {})
    if verify_replay:
        if not isinstance(ev, dict) or ev.get("reproduced") is not True:
            raise ProvenanceRefused("Promotion requires verified public replay (evidence.reproduced=True)")
        if not ev.get("verified_by"):
            raise ProvenanceRefused("Promotion requires evidence.verified_by")
        runs = ev.get("runs", 1)
        if type(runs) is not int or runs < 2:
            raise ProvenanceRefused("Promotion requires at least 2 replay runs")

    # Admit through existing skill admission API without modifying schema
    return skills.admit(bound.skill)


# @anchor:wa.remember.episode_provenance.EpisodeProvenanceIndex
class EpisodeProvenanceIndex:
    """Outer episode index providing atomic transactional storage.

    INV J9 4: Serializes by episode, skill, and evidence version.
    Conflicting support or stale completion refuses atomically.
    """
    def __init__(self):
        self._entries: dict[tuple[str, str, int], BoundProvenanceSkill] = {}
        self._latest_versions: dict[tuple[str, str], int] = {}
        self._branch_support: dict[tuple[str, str], tuple[str, ...]] = {}

    def commit(self, bound: BoundProvenanceSkill) -> tuple[str, str, int]:
        """Atomically commit a bound provenance record to the index."""
        if not isinstance(bound, BoundProvenanceSkill):
            raise ProvenanceRefused("Only BoundProvenanceSkill can be indexed")

        manifest_sha = bound.provenance.episode_manifest_sha256
        skill_id = bound.skill_id
        version = bound.evidence_version
        support_path = bound.provenance.support_path

        key_pair = (manifest_sha, skill_id)
        full_key = (manifest_sha, skill_id, version)

        # 1. Stale completion check
        latest_ver = self._latest_versions.get(key_pair)
        if latest_ver is not None and version <= latest_ver:
            raise ProvenanceConflict(
                f"Stale completion refused: incoming version {version} <= current version {latest_ver} for {key_pair}"
            )

        # 2. Conflicting support check
        existing_path = self._branch_support.get(key_pair)
        if existing_path is not None and existing_path != support_path:
            raise ProvenanceConflict(
                f"Conflicting support refused: existing path {existing_path} != new path {support_path}"
            )

        # Atomic write
        self._entries[full_key] = bound
        self._latest_versions[key_pair] = version
        self._branch_support[key_pair] = support_path

        return full_key

    def get(self, manifest_sha: str, skill_id: str, version: int) -> BoundProvenanceSkill | None:
        return self._entries.get((manifest_sha, skill_id, version))

    def has_entry(self, manifest_sha: str, skill_id: str, version: int) -> bool:
        return (manifest_sha, skill_id, version) in self._entries

    def count(self) -> int:
        return len(self._entries)

    def list_entries(self) -> list[BoundProvenanceSkill]:
        return list(self._entries.values())
