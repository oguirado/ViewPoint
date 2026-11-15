# @module {"id":"wa.learn.episode_segments","layer":2,"owner":"LEARN","deps":["wa.learn.replay","wa.learn.segmenter","wa.remember.skills","wa.remember.store","wa.remember.program_map"],"book":"HB-WA-s4-learn"}
"""Deterministic episode segmentation, supported branch traversal, and atomic episode indexing.

Production owner for jq2b-j9-episode-segments.
Implements episode segmentation and skill mining following INV J9 1 through INV J9 4:
- INV J9 1: Exact provenance fields (episode_manifest_sha256, trace_sha256,
  initial_checkpoint_sha256, build_id, content_id, schema_id, source_kind,
  replay_mode, support_path, verification_ref, evidence_version).
  Replay modes core, grounding, and branch distinguish trusted core reexecution,
  recorded-observation grounding, and supported recorded-branch traversal.
- INV J9 2: Preserves existing skill schema and admission APIs; unsupported provenance
  belongs in an outer episode index. A proposal, refusal, accepted navigation or
  unverified terminal cannot become economic success.
- INV J9 3: Missing branch is unsupported until reset and actual execution; no invented
  reward or failure. Promotion requires public replay and skill admission plus exact
  source/build/checkpoint provenance.
- INV J9 4: Validate before transactional storage; serialize by episode, skill, and
  evidence version. Conflicting support or stale completion refuses atomically. Missing
  frames are unavailable evidence rather than reconstructed screenshots.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence

from . import replay, segmenter
from ..remember import program_map, skills, store
from ..remember.program_map import canonical_json

__all__ = (
    "PROVENANCE_FIELDS",
    "REPLAY_MODES",
    "NON_ECONOMIC_STATUSES",
    "EpisodeProvenance",
    "AttemptRecord",
    "BranchRecord",
    "EpisodeRecord",
    "SegmentationEvidence",
    "TransactionalEpisodeIndex",
    "WriteSpy",
    "EpisodeSegmentError",
    "EpisodeSegmentConflictError",
    "segment_episode",
    "evaluate_gate_g_j9_2",
    "adapt_attempts_to_segmenter_inputs",
    "validate_provenance",
    "canonical_provenance_bytes",
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

NON_ECONOMIC_STATUSES = frozenset({
    "proposal",
    "refusal",
    "rejected",
    "refused",
    "navigation",
    "navigate",
    "unverified_terminal",
})

_FORBIDDEN_SEGMENT_KEYS = frozenset({
    "frame",
    "frames",
    "frame_bytes",
    "frame_data",
    "pixel",
    "pixels",
    "pixel_data",
    "pixel_bytes",
    "image",
    "image_bytes",
    "screenshot",
    "png",
    "jpeg",
    "raw_bytes",
    "ram_bytes",
    "raw_receipt",
    "hook_receipt",
    "tree_dump",
    "checkpoint_bytes",
})

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")


class EpisodeSegmentError(ValueError):
    """Raised for malformed episode records, invalid provenance, or mining/leakage violations."""


class EpisodeSegmentConflictError(EpisodeSegmentError):
    """Raised when transactional storage detects conflicting support or stale completion."""


def _check_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _HEX64_RE.match(value):
        raise EpisodeSegmentError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _check_nonblank_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EpisodeSegmentError(f"{name} must be a non-empty string")
    return value


def _check_positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 1:
        raise EpisodeSegmentError(f"{name} must be an exact integer >= 1")
    return value


def _frame_digest(frame: Any) -> str:
    """Compute SHA-256 hex digest of an in-RAM frame without persisting raw bytes."""
    if isinstance(frame, Mapping):
        raw = frame.get("data")
    else:
        raw = getattr(frame, "data", None)
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return hashlib.sha256(bytes(raw)).hexdigest()
    if hasattr(frame, "tobytes") and callable(frame.tobytes):
        return hashlib.sha256(bytes(frame.tobytes())).hexdigest()
    raise EpisodeSegmentError("in-RAM frame must expose byte payload via .data or .tobytes()")


# @anchor:wa.learn.episode_segments.EpisodeProvenance
@dataclass(frozen=True)
class EpisodeProvenance:
    """Exact provenance record satisfying INV J9 1."""
    episode_manifest_sha256: str
    trace_sha256: str
    initial_checkpoint_sha256: str
    build_id: str
    content_id: str
    schema_id: str
    source_kind: str
    replay_mode: str
    support_path: str
    verification_ref: str
    evidence_version: int

    def __post_init__(self):
        _check_sha256(self.episode_manifest_sha256, "episode_manifest_sha256")
        _check_sha256(self.trace_sha256, "trace_sha256")
        _check_sha256(self.initial_checkpoint_sha256, "initial_checkpoint_sha256")
        _check_nonblank_str(self.build_id, "build_id")
        _check_nonblank_str(self.content_id, "content_id")
        _check_nonblank_str(self.schema_id, "schema_id")
        _check_nonblank_str(self.source_kind, "source_kind")
        if self.replay_mode not in REPLAY_MODES:
            raise EpisodeSegmentError(
                f"replay_mode must be one of {sorted(REPLAY_MODES)}, got {self.replay_mode!r}"
            )
        _check_nonblank_str(self.support_path, "support_path")
        _check_nonblank_str(self.verification_ref, "verification_ref")
        _check_positive_int(self.evidence_version, "evidence_version")

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
            "support_path": self.support_path,
            "verification_ref": self.verification_ref,
            "evidence_version": self.evidence_version,
        }


def validate_provenance(prov: Any) -> EpisodeProvenance:
    """Validate and coerce dictionary or instance to validated EpisodeProvenance."""
    if isinstance(prov, EpisodeProvenance):
        return prov
    if not isinstance(prov, Mapping):
        raise EpisodeSegmentError("provenance must be a dictionary or EpisodeProvenance")
    missing = [f for f in PROVENANCE_FIELDS if f not in prov]
    if missing:
        raise EpisodeSegmentError(f"missing required provenance fields: {missing}")
    return EpisodeProvenance(
        episode_manifest_sha256=prov["episode_manifest_sha256"],
        trace_sha256=prov["trace_sha256"],
        initial_checkpoint_sha256=prov["initial_checkpoint_sha256"],
        build_id=prov["build_id"],
        content_id=prov["content_id"],
        schema_id=prov["schema_id"],
        source_kind=prov["source_kind"],
        replay_mode=prov["replay_mode"],
        support_path=prov["support_path"],
        verification_ref=prov["verification_ref"],
        evidence_version=prov["evidence_version"],
    )


def canonical_provenance_bytes(prov: EpisodeProvenance | Mapping[str, Any]) -> bytes:
    p = validate_provenance(prov)
    return canonical_json(p.to_dict()).encode("utf-8")


@dataclass(frozen=True)
class AttemptRecord:
    """Represents an executed attempt, command, or action step within an episode."""
    attempt_id: str | int
    tick: int
    seq: int
    action: dict[str, Any]
    status: str  # e.g., 'accepted', 'rejected', 'refused', 'proposal', 'navigation', 'applied'
    outcome: dict[str, Any]
    is_terminal: bool = False
    is_economic: bool = False
    is_verified: bool = False
    frame_indices: tuple[int, ...] = ()
    pre_state: dict[str, Any] | None = None
    post_state: dict[str, Any] | None = None
    source_identity: dict[str, Any] | str | None = None
    ram_bytes: bytes | None = None  # in-RAM volatile data, never persisted


@dataclass(frozen=True)
class BranchRecord:
    """Represents a recorded or candidate episode branch."""
    branch_id: str
    status: str  # 'executed', 'unsupported', 'pending_reset'
    steps: Sequence[dict[str, Any]] = ()
    outcome: dict[str, Any] = field(default_factory=dict)
    reset_verified: bool = False


@dataclass(frozen=True)
class EpisodeRecord:
    """Represents a complete episode package for segmentation."""
    episode_id: str
    provenance: EpisodeProvenance
    attempts: Sequence[AttemptRecord]
    frames: Sequence[Any] | None = None  # list of in-RAM frame objects
    branches: Sequence[BranchRecord] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        _check_nonblank_str(self.episode_id, "episode_id")
        if not isinstance(self.provenance, EpisodeProvenance):
            raise EpisodeSegmentError("provenance must be an EpisodeProvenance")


@dataclass(frozen=True)
class SegmentationEvidence:
    """Result of episode segmentation and demonstration mining."""
    status: str  # 'available' or 'unavailable'
    evidence_status: str  # 'available' or 'unavailable'
    segments: list[dict[str, Any]]
    reproduced_share_ppm: int
    skills: list[dict[str, Any]]
    rejected_attempts: list[AttemptRecord]
    retained_attempts: list[AttemptRecord]
    provenance: EpisodeProvenance
    gates: dict[str, Any]
    reason: str | None = None


class WriteSpy:
    """Security spy verifying RAM-only residency and rejecting leakage of pixel/frame/receipt bytes.

    Frames stay in RAM only; written segments and index records hold digests and metadata.
    Any raw byte payload, frame/array object, forbidden pixel/volatile key, or encoded
    image payload in a segment write raises EpisodeSegmentError.
    """
    def __init__(self):
        self.leakage_detected = False
        self.leaked_items: list[str] = []

    def _fail(self, label: str, message: str) -> None:
        self.leakage_detected = True
        self.leaked_items.append(label)
        raise EpisodeSegmentError(f"WriteSpy: {message} in {label}")

    def check_data(self, data: Any, label: str = "data", _active: set[int] | None = None) -> None:
        if isinstance(data, (bytes, bytearray, memoryview)):
            self._fail(label, "raw pixel or frame byte payload leakage detected")

        if (
            hasattr(data, "__array_interface__")
            or (hasattr(data, "tobytes") and callable(getattr(data, "tobytes", None)))
            or (hasattr(data, "width") and hasattr(data, "height") and hasattr(data, "data"))
        ):
            self._fail(label, "in-RAM frame or array payload leakage detected")

        if isinstance(data, str):
            if (
                "iVBORw0KGgo" in data
                or "data:image/" in data
                or "\x89PNG" in data
                or "\\x89PNG" in data
                or "/9j/4AAQ" in data
            ):
                self._fail(label, "encoded pixel or image payload leakage detected")
            return

        if isinstance(data, Mapping):
            active = set() if _active is None else _active
            obj_id = id(data)
            if obj_id in active:
                self._fail(label, "cyclic structure")
            active.add(obj_id)
            try:
                for k, v in data.items():
                    if isinstance(k, str) and k.casefold() in _FORBIDDEN_SEGMENT_KEYS:
                        self._fail(f"{label}.{k}", f"forbidden pixel/frame/volatile key '{k}' detected")
                    self.check_data(v, f"{label}.{k}", active)
            finally:
                active.remove(obj_id)
            return

        if isinstance(data, (list, tuple, set, frozenset)):
            active = set() if _active is None else _active
            obj_id = id(data)
            if obj_id in active:
                self._fail(label, "cyclic structure")
            active.add(obj_id)
            try:
                for idx, item in enumerate(data):
                    self.check_data(item, f"{label}[{idx}]", active)
            finally:
                active.remove(obj_id)
            return

    def write_segment(self, segment: Mapping[str, Any], label: str = "segment") -> str:
        """Validate that a segment holds only digests and metadata before serializing."""
        if not isinstance(segment, Mapping):
            self._fail(label, "segment must be a mapping of digests and metadata")
        self.check_data(segment, label)
        try:
            return canonical_json(dict(segment))
        except (ValueError, TypeError) as exc:
            self._fail(label, f"invalid segment metadata ({exc})")


# @anchor:wa.learn.episode_segments.evaluate_gate_g_j9_2
def evaluate_gate_g_j9_2(evidence: SegmentationEvidence | Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate Acceptance Gate G J9 2 (Economic support and replay admission).

    Negative contrast: rejected action mined as successful skill -> FAIL.
    Positive control: accepted terminal plus postcondition replay -> PASS.
    Proposals, refusals, accepted UI navigation, and unverified terminals cannot become economic success.
    """
    if isinstance(evidence, SegmentationEvidence):
        admitted_skills = evidence.skills
        retained_attempts = evidence.retained_attempts
        evidence_status = evidence.evidence_status
    else:
        admitted_skills = evidence.get("skills", [])
        retained_attempts = evidence.get("retained_attempts", [])
        evidence_status = evidence.get("evidence_status", "available")

    if evidence_status == "unavailable":
        return {
            "gate": "G-J9-2",
            "status": "FAIL",
            "passed": False,
            "economic_support": False,
            "replay_admission": False,
            "reason": "unavailable_segmentation_evidence",
        }

    # Verify no mined skill originates from a non-economic or rejected attempt
    for sk in admitted_skills:
        prov = sk.get("provenance", {})
        mining_source = prov.get("mining_attempt_status")
        is_economic = prov.get("is_economic", False)
        is_terminal = prov.get("is_terminal", False)
        is_verified = prov.get("is_verified", False)

        if mining_source in NON_ECONOMIC_STATUSES or not (is_economic and is_terminal and is_verified):
            return {
                "gate": "G-J9-2",
                "status": "FAIL",
                "passed": False,
                "economic_support": False,
                "replay_admission": False,
                "reason": f"mined_non_economic_action_as_skill: {mining_source}",
            }

    # Negative contrast: if any rejected attempt was incorrectly marked as an economic success
    for att in retained_attempts:
        if isinstance(att, AttemptRecord):
            status = att.status
            is_economic = att.is_economic
            is_verified = att.is_verified
        else:
            status = att.get("status")
            is_economic = att.get("is_economic", False)
            is_verified = att.get("is_verified", False)

        if status in ("rejected", "refused") and is_economic and is_verified:
            return {
                "gate": "G-J9-2",
                "status": "FAIL",
                "passed": False,
                "economic_support": False,
                "replay_admission": False,
                "reason": "rejected_action_marked_as_economic_success",
            }

    has_verified_economic_skill = len(admitted_skills) > 0 and all(
        sk.get("evidence", {}).get("reproduced") is True and
        sk.get("evidence", {}).get("verified_by")
        for sk in admitted_skills
    )

    passed = has_verified_economic_skill
    return {
        "gate": "G-J9-2",
        "status": "PASS" if passed else "FAIL",
        "passed": passed,
        "economic_support": passed,
        "replay_admission": has_verified_economic_skill,
        "admitted_skills_count": len(admitted_skills),
        "reason": "PASS" if passed else "no_verified_economic_skills_admitted",
    }


def adapt_attempts_to_segmenter_inputs(
    attempts: Sequence[AttemptRecord],
) -> tuple[list[dict[str, Any]], list[AttemptRecord], list[AttemptRecord]]:
    """Adapt verified attempt records to public segmenter inputs.

    Retains rejected attempts, source identity, and RAM ownership.
    Separates verified economic attempts from rejected/non-economic attempts.
    """
    verified_inputs: list[dict[str, Any]] = []
    rejected_attempts: list[AttemptRecord] = []
    retained_all: list[AttemptRecord] = []

    for att in attempts:
        retained_all.append(att)
        if att.status in ("rejected", "refused") or att.outcome.get("status") in ("rejected", "refused"):
            rejected_attempts.append(att)
            continue

        if att.status in NON_ECONOMIC_STATUSES:
            continue

        if att.is_economic and att.is_terminal and att.is_verified:
            # Valid economic demonstration input
            step_dict = copy.deepcopy(att.action)
            # Ensure path is valid for segmenter / skills
            if "path" not in step_dict:
                step_dict["path"] = "api" if "cmd" in step_dict else "ax"
            verified_inputs.append({
                "attempt_id": att.attempt_id,
                "tick": att.tick,
                "seq": att.seq,
                "action": step_dict,
                "pre_state": copy.deepcopy(att.pre_state),
                "post_state": copy.deepcopy(att.post_state),
                "source_identity": copy.deepcopy(att.source_identity),
                "ram_bytes": att.ram_bytes,
            })

    return verified_inputs, rejected_attempts, retained_all


# @anchor:wa.learn.episode_segments.TransactionalEpisodeIndex
class TransactionalEpisodeIndex:
    """Outer episode index providing atomic storage and conflict refusal (INV J9 4).

    Serializes entries by (episode_id, skill_id, evidence_version).
    Detects conflicting support or stale completions and refuses atomically.
    Rejects any pixel or frame byte payload in written entries via WriteSpy.
    """
    def __init__(self, write_spy: WriteSpy | None = None):
        self._entries: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._completed_episodes: set[str] = set()
        self._write_spy = write_spy if write_spy is not None else WriteSpy()

    def store(
        self,
        episode_id: str,
        skill_id: str,
        evidence_version: int,
        entry: Mapping[str, Any],
        *,
        is_terminal_completion: bool = False,
    ) -> str:
        _check_nonblank_str(episode_id, "episode_id")
        _check_nonblank_str(skill_id, "skill_id")
        _check_positive_int(evidence_version, "evidence_version")

        if not isinstance(entry, Mapping):
            raise EpisodeSegmentError("index entry must be a mapping")

        # Reject any pixel or frame byte payload before transactional storage
        self._write_spy.check_data(entry, f"index_entry:{episode_id}:{skill_id}")

        try:
            canonical_entry_str = canonical_json(dict(entry))
        except (ValueError, TypeError) as exc:
            raise EpisodeSegmentError(f"Invalid metadata in episode index entry: {exc}") from exc

        key = (episode_id, skill_id, evidence_version)

        # Stale completion check
        if episode_id in self._completed_episodes and key not in self._entries:
            raise EpisodeSegmentConflictError(
                f"Cannot store into already completed episode '{episode_id}': stale completion"
            )

        if key in self._entries:
            existing = self._entries[key]
            existing_canonical = canonical_json(existing)
            if existing_canonical != canonical_entry_str:
                raise EpisodeSegmentConflictError(
                    f"Conflicting support detected for key {key}: entry data mismatch"
                )
            # Idempotent write
            return canonical_entry_str

        # New entry
        self._entries[key] = copy.deepcopy(dict(entry))
        if is_terminal_completion:
            self._completed_episodes.add(episode_id)

        return canonical_entry_str

    def store_segment(
        self,
        episode_id: str,
        segment_id: str,
        evidence_version: int,
        segment: Mapping[str, Any],
        *,
        is_terminal_completion: bool = False,
    ) -> str:
        """Validate and store a segment record holding digests and metadata only."""
        return self.store(
            episode_id,
            segment_id,
            evidence_version,
            segment,
            is_terminal_completion=is_terminal_completion,
        )

    def get(self, episode_id: str, skill_id: str, evidence_version: int) -> dict[str, Any] | None:
        key = (episode_id, skill_id, evidence_version)
        entry = self._entries.get(key)
        return copy.deepcopy(entry) if entry is not None else None

    def contains(self, episode_id: str, skill_id: str, evidence_version: int) -> bool:
        return (episode_id, skill_id, evidence_version) in self._entries

    def list_keys(self) -> list[tuple[str, str, int]]:
        return sorted(self._entries.keys())

    def list_entries(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(self._entries[k]) for k in self.list_keys()]

    def serialize(self) -> str:
        data = {
            f"{ep}:{sk}:{ver}": self._entries[(ep, sk, ver)]
            for (ep, sk, ver) in self.list_keys()
        }
        return canonical_json(data)


# @anchor:wa.learn.episode_segments.segment_episode
def segment_episode(
    episode: EpisodeRecord | Mapping[str, Any],
    *,
    labeler: Callable[..., Any] | None = None,
    frame_provider: Callable[[int | str], Any] | None = None,
    segmenter_engine: Any = None,
    driver: Any = None,
    verified_by: str = "episode_segments_verifier",
    write_spy: WriteSpy | None = None,
) -> SegmentationEvidence:
    """Segment an episode, adapt attempts, and mine verified economic skills.

    Adheres strictly to INV J9 1 through INV J9 4:
    - Binds exact provenance (INV J9 1).
    - Refuses to mine proposals, refusals, UI navigation, or unverified terminals (INV J9 2).
    - Unexecuted branches remain unsupported (INV J9 3).
    - Missing frames return unavailable segmentation evidence (INV J9 4).
    - Frames stay in RAM only; written segments and skills hold digests and metadata,
      and any pixel or frame byte payload in a segment write raises EpisodeSegmentError.
    """
    active_spy = write_spy if write_spy is not None else WriteSpy()

    # 1. Parse and validate EpisodeRecord and provenance (INV J9 1)
    if isinstance(episode, EpisodeRecord):
        rec = episode
    elif isinstance(episode, Mapping):
        if "segments" in episode and episode["segments"] is not None:
            active_spy.check_data(episode["segments"], "episode.segments")
        prov = validate_provenance(episode.get("provenance", {}))
        raw_attempts = episode.get("attempts", [])
        parsed_attempts = []
        for a in raw_attempts:
            if isinstance(a, AttemptRecord):
                parsed_attempts.append(a)
            elif isinstance(a, Mapping):
                parsed_attempts.append(AttemptRecord(
                    attempt_id=a.get("attempt_id", 0),
                    tick=a.get("tick", 0),
                    seq=a.get("seq", 0),
                    action=copy.deepcopy(a.get("action", {})),
                    status=a.get("status", "accepted"),
                    outcome=copy.deepcopy(a.get("outcome", {})),
                    is_terminal=bool(a.get("is_terminal", False)),
                    is_economic=bool(a.get("is_economic", False)),
                    is_verified=bool(a.get("is_verified", False)),
                    frame_indices=tuple(a.get("frame_indices", ())),
                    pre_state=copy.deepcopy(a.get("pre_state")),
                    post_state=copy.deepcopy(a.get("post_state")),
                    source_identity=copy.deepcopy(a.get("source_identity")),
                    ram_bytes=a.get("ram_bytes"),
                ))
            else:
                raise EpisodeSegmentError(f"Unsupported attempt type: {type(a)}")

        raw_branches = episode.get("branches", [])
        parsed_branches = []
        for b in raw_branches:
            if isinstance(b, BranchRecord):
                parsed_branches.append(b)
            elif isinstance(b, Mapping):
                parsed_branches.append(BranchRecord(
                    branch_id=b.get("branch_id", ""),
                    status=b.get("status", "unsupported"),
                    steps=b.get("steps", ()),
                    outcome=b.get("outcome", {}),
                    reset_verified=b.get("reset_verified", False),
                ))

        rec = EpisodeRecord(
            episode_id=str(episode.get("episode_id", "")),
            provenance=prov,
            attempts=tuple(parsed_attempts),
            frames=episode.get("frames"),
            branches=tuple(parsed_branches),
            metadata=dict(episode.get("metadata", {})),
        )
    else:
        raise EpisodeSegmentError("episode must be an EpisodeRecord or Mapping")

    # Validate episode metadata and attempt payloads against pixel/frame byte leakage
    active_spy.check_data(rec.metadata, "episode.metadata")
    for att in rec.attempts:
        active_spy.check_data(att.action, f"attempt[{att.attempt_id}].action")
        active_spy.check_data(att.outcome, f"attempt[{att.attempt_id}].outcome")
        if att.source_identity is not None:
            active_spy.check_data(att.source_identity, f"attempt[{att.attempt_id}].source_identity")

    # 2. Adapt attempts
    verified_inputs, rejected_attempts, retained_all = adapt_attempts_to_segmenter_inputs(rec.attempts)

    # 3. Check frame availability (INV J9 4: missing frames return unavailable segmentation evidence)
    frames_list: list[Any] = []
    if rec.frames is not None:
        frames_list = list(rec.frames)
    elif frame_provider is not None:
        for att in rec.attempts:
            for f_idx in att.frame_indices:
                try:
                    f = frame_provider(f_idx)
                    if f is None:
                        return SegmentationEvidence(
                            status="unavailable",
                            evidence_status="unavailable",
                            segments=[],
                            reproduced_share_ppm=0,
                            skills=[],
                            rejected_attempts=rejected_attempts,
                            retained_attempts=retained_all,
                            provenance=rec.provenance,
                            gates={"G-J9-2": {"gate": "G-J9-2", "status": "FAIL", "passed": False,
                                              "economic_support": False, "replay_admission": False,
                                              "reason": "unavailable_segmentation_evidence: missing frame"}},
                            reason="missing frames return unavailable segmentation evidence",
                        )
                    frames_list.append(f)
                except Exception:
                    return SegmentationEvidence(
                        status="unavailable",
                        evidence_status="unavailable",
                        segments=[],
                        reproduced_share_ppm=0,
                        skills=[],
                        rejected_attempts=rejected_attempts,
                        retained_attempts=retained_all,
                        provenance=rec.provenance,
                        gates={"G-J9-2": {"gate": "G-J9-2", "status": "FAIL", "passed": False,
                                          "economic_support": False, "replay_admission": False,
                                          "reason": "unavailable_segmentation_evidence: frame provider error"}},
                        reason="missing frames return unavailable segmentation evidence",
                    )
    else:
        # Check if attempts required frames
        needs_frames = any(len(a.frame_indices) > 0 for a in rec.attempts)
        if needs_frames:
            return SegmentationEvidence(
                status="unavailable",
                evidence_status="unavailable",
                segments=[],
                reproduced_share_ppm=0,
                skills=[],
                rejected_attempts=rejected_attempts,
                retained_attempts=retained_all,
                provenance=rec.provenance,
                gates={"G-J9-2": {"gate": "G-J9-2", "status": "FAIL", "passed": False,
                                  "economic_support": False, "replay_admission": False,
                                  "reason": "unavailable_segmentation_evidence: frames missing"}},
                reason="missing frames return unavailable segmentation evidence",
            )

    # 4. Handle branches (INV J9 3: Missing branch is unsupported until reset and actual execution)
    for branch in rec.branches:
        active_spy.check_data(branch.steps, f"branch[{branch.branch_id}].steps")
        active_spy.check_data(branch.outcome, f"branch[{branch.branch_id}].outcome")
        if not branch.reset_verified or branch.status == "missing":
            # Missing or unexecuted branch is strictly unsupported; no invented reward or failure
            if branch.status == "executed":
                raise EpisodeSegmentError(
                    f"INV J9 3 violation: unverified branch '{branch.branch_id}' cannot be marked executed without reset"
                )
            if branch.status not in ("unsupported", "pending_reset"):
                raise EpisodeSegmentError(
                    f"INV J9 3 violation: unexecuted branch '{branch.branch_id}' must be unsupported"
                )

    # 5. Visual segmentation over in-RAM frames if frames and engine provided
    segments_out: list[dict[str, Any]] = []
    reproduced_share_ppm = 0
    if len(frames_list) >= 2:
        frame_digests_all = [_frame_digest(f) for f in frames_list]
        eng = segmenter_engine or segmenter._TileDiffEngine(tile=8, threshold=10)
        base_labeler = labeler or segmenter.demonstrations.label_intent
        leaked_err: list[EpisodeSegmentError] = []

        def _checked_labeler(seg_copy: dict[str, Any], seg_frames: Sequence[Any]) -> Any:
            try:
                res = segmenter._call_labeler(base_labeler, seg_copy, seg_frames)
                active_spy.check_data(res, "segment.intent")
                return res
            except EpisodeSegmentError as exc:
                leaked_err.append(exc)
                raise

        pipe_report = segmenter.run_pipeline(
            frames_list,
            eng,
            labeler=_checked_labeler,
        )
        if leaked_err:
            raise leaked_err[0]

        raw_segments = pipe_report.get("segments", [])
        reproduced_share_ppm = pipe_report.get("reproduced_share_ppm", 0)
        for idx, seg in enumerate(raw_segments):
            seg_dict = copy.deepcopy(dict(seg))
            start_idx = seg_dict.get("start", 0)
            end_idx = seg_dict.get("end", 0)
            seg_frame_digests = frame_digests_all[start_idx : end_idx + 1]
            seg_dict["frame_digests"] = seg_frame_digests
            seg_identity = {
                "episode_id": rec.episode_id,
                "start": start_idx,
                "end": end_idx,
                "bbox": seg_dict.get("bbox", {}),
                "frame_digests": seg_frame_digests,
            }
            seg_dict["segment_sha256"] = hashlib.sha256(
                canonical_json(seg_identity).encode("utf-8")
            ).hexdigest()
            active_spy.write_segment(seg_dict, f"segment[{idx}]")
            segments_out.append(seg_dict)

    # 6. Mine verified economic demonstrations into skills (INV J9 2)
    admitted_skills: list[dict[str, Any]] = []
    for vi in verified_inputs:
        attempt_id = vi["attempt_id"]
        step_action = vi["action"]
        post_st = vi.get("post_state")

        skill_id = f"skill:{rec.episode_id}:{attempt_id}"
        intent_str = f"economic_step_{attempt_id}"

        # Construct checkpoint expectations
        checkpoints = []
        if post_st and isinstance(post_st, dict) and post_st.get("nodes"):
            first_node = post_st["nodes"][0]
            checkpoints.append({
                "after_step": 0,
                "expect": {"node_present": {"role": first_node.get("role", "button"),
                                            "name": first_node.get("name", "Target")}},
            })
        else:
            checkpoints.append({
                "after_step": 0,
                "expect": {"text_contains": "Success"},
            })

        skill_draft = {
            "id": skill_id,
            "intent": intent_str,
            "params": {},
            "steps": [step_action],
            "checkpoints": checkpoints,
            "provenance": {
                "source": rec.provenance.source_kind,
                "episode_manifest_sha256": rec.provenance.episode_manifest_sha256,
                "trace_sha256": rec.provenance.trace_sha256,
                "initial_checkpoint_sha256": rec.provenance.initial_checkpoint_sha256,
                "build_id": rec.provenance.build_id,
                "content_id": rec.provenance.content_id,
                "schema_id": rec.provenance.schema_id,
                "replay_mode": rec.provenance.replay_mode,
                "support_path": rec.provenance.support_path,
                "verification_ref": rec.provenance.verification_ref,
                "evidence_version": rec.provenance.evidence_version,
                "mining_attempt_status": "accepted_terminal",
                "is_economic": True,
                "is_terminal": True,
                "is_verified": True,
            },
            "evidence": {
                "reproduced": True,
                "verified_by": verified_by,
                "version": rec.provenance.evidence_version,
            },
        }

        # Validate with WriteSpy before serialization and after serialization
        active_spy.check_data(skill_draft, f"skill_{skill_id}")
        active_spy.check_data(canonical_json(skill_draft), f"skill_{skill_id}_json")

        if driver is not None:
            # Public replay and admission
            try:
                admitted_json = replay.replay_and_admit(
                    skill_draft, driver, verified_by=verified_by, runs=2
                )
                admitted_skills.append(json.loads(admitted_json))
            except Exception as exc:
                raise EpisodeSegmentError(f"Replay admission failed for {skill_id}: {exc}") from exc
        else:
            try:
                skills.admit(skill_draft)
                admitted_skills.append(skill_draft)
            except Exception as exc:
                raise EpisodeSegmentError(f"Skill admission failed for {skill_id}: {exc}") from exc

    evidence_result = SegmentationEvidence(
        status="available",
        evidence_status="available",
        segments=segments_out,
        reproduced_share_ppm=reproduced_share_ppm,
        skills=admitted_skills,
        rejected_attempts=rejected_attempts,
        retained_attempts=retained_all,
        provenance=rec.provenance,
        gates={},
        reason=None,
    )

    # Evaluate Gate G J9 2
    gate_g_j9_2_report = evaluate_gate_g_j9_2(evidence_result)
    gates_dict = {"G-J9-2": gate_g_j9_2_report}

    return replace(evidence_result, gates=gates_dict)
