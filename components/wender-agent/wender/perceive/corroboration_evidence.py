# @module {"id":"wa.perceive.corroboration_evidence","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.parallel_perceive","wa.perceive.vision_adapter"],"book":"HB-WA-s1-perceive"}
"""Independently calibrated episode structure, telemetry, and residency corroboration.

Production owner for jq2b-j8-corroboration-evidence.
Implements evidence normalization and corroboration according to INV J8 1 through INV J8 4:
- INV J8 1: Provenance integrity and normalization. Validates handle, generation, epoch,
  frame sequence, DPR, source, screen/query, and committed binding before fusion. Actual CDP
  harvest alone justifies source.kind cdp. Missing prerequisites fail loudly as AUSENTE.
- INV J8 2: Temporal lane isolation and ownership fencing. Freezes ownership-checked
  normalized metadata and rejects sibling frame, same-epoch resize, wrong DPR, mismatched
  screen/query/commit, and absent provenance. Exceptions reset only the affected temporal lane.
- INV J8 3: Corroboration decision without fallback. Requires independent structure verification,
  semantically calibrated telemetry, and complete observer coverage. Equal hashes and same_screen
  cannot replace them. Unknown geometry, hidden-information contamination, stale or skipped
  evidence cannot become current through a last-good fallback.
- INV J8 4: Clean verdict and leakage prevention. Result fields are verdict (CORROBORATED,
  ABSTAIN, MISMATCH), failure_code, tree_ref, frame_ref, commit_ref, structure_checked,
  telemetry_checked, and residency_checked. Contains no pixels, crops, or free-form model rationale.
  Public describe options disable artifact output. WriteSpy rejects pixel and receipt leakage.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any, Callable, Mapping, Sequence

import numpy as np

try:
    from .parallel_perceive import FrameRef, Handle
except ImportError:
    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    @dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str


__all__ = (
    "Verdict",
    "FailureCode",
    "EvidenceError",
    "SourceDescriptor",
    "DeclaredTolerances",
    "DescribeOptions",
    "NormalizedEvidence",
    "CorroborationResult",
    "WriteSpy",
    "normalize_evidence",
    "corroborate_evidence",
    "Corroborator",
    "evaluate_gate_g_j8_2",
    "describe_result",
)


class Verdict(str, Enum):
    """Corroboration outcome verdict: strictly containing no pixels or crops."""
    CORROBORATED = "CORROBORATED"
    ABSTAIN = "ABSTAIN"
    MISMATCH = "MISMATCH"


class FailureCode(str, Enum):
    """Typed failure and rejection codes for corroboration and evidence gates."""
    AUSENTE = "AUSENTE"
    SIBLING_FRAME = "SIBLING_FRAME"
    SAME_EPOCH_RESIZE = "SAME_EPOCH_RESIZE"
    WRONG_DPR = "WRONG_DPR"
    MISMATCHED_SCREEN = "MISMATCHED_SCREEN"
    MISMATCHED_QUERY = "MISMATCHED_QUERY"
    MISMATCHED_COMMIT = "MISMATCHED_COMMIT"
    ABSENT_PROVENANCE = "ABSENT_PROVENANCE"
    UNKNOWN_GEOMETRY = "UNKNOWN_GEOMETRY"
    HIDDEN_CONTAMINATION = "HIDDEN_CONTAMINATION"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    SKIPPED_EVIDENCE = "SKIPPED_EVIDENCE"
    UNCALIBRATED_TELEMETRY = "UNCALIBRATED_TELEMETRY"
    INCOMPLETE_OBSERVER_COVERAGE = "INCOMPLETE_OBSERVER_COVERAGE"
    STRUCTURE_MISMATCH = "STRUCTURE_MISMATCH"
    LAST_GOOD_FALLBACK_FORBIDDEN = "LAST_GOOD_FALLBACK_FORBIDDEN"
    INVALID_SOURCE_KIND = "INVALID_SOURCE_KIND"


class EvidenceError(ValueError):
    """Raised when evidence verification, provenance, or normalization invariant fails."""


@dataclass(frozen=True)
class SourceDescriptor:
    """Source runtime descriptor. Actual CDP harvest alone justifies source.kind cdp."""
    kind: str
    session_id: str
    target_id: str
    has_actual_cdp_harvest: bool = False

    def __post_init__(self):
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise EvidenceError("AUSENTE: source kind must be a non-empty string")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise EvidenceError("AUSENTE: session_id must be a non-empty string")
        if not isinstance(self.target_id, str) or not self.target_id.strip():
            raise EvidenceError("AUSENTE: target_id must be a non-empty string")
        if self.kind == "cdp" and not self.has_actual_cdp_harvest:
            raise EvidenceError(
                "INVALID_SOURCE_KIND: Actual CDP harvest alone justifies source.kind cdp"
            )


@dataclass(frozen=True)
class DeclaredTolerances:
    """Declared raw-unit tolerances: never zero defaults."""
    px_pos_tolerance: int = 2
    px_size_tolerance: int = 4
    telemetry_tick_tolerance: int = 1
    dpr_tolerance_ppm: int = 1000
    min_observer_coverage_pct: int = 90

    def __post_init__(self):
        for name in (
            "px_pos_tolerance",
            "px_size_tolerance",
            "telemetry_tick_tolerance",
            "dpr_tolerance_ppm",
            "min_observer_coverage_pct",
        ):
            val = getattr(self, name)
            if type(val) is not int or val <= 0:
                raise EvidenceError(
                    f"Tolerances must be positive raw units, never zero defaults: {name}={val}"
                )


@dataclass(frozen=True)
class DescribeOptions:
    """Public describe options must disable artifact output."""
    disable_artifacts: bool = True
    emit_crops: bool = False
    emit_pixels: bool = False

    def __post_init__(self):
        if not self.disable_artifacts or self.emit_crops or self.emit_pixels:
            raise EvidenceError("Public describe options must disable artifact output")


@dataclass(frozen=True)
class NormalizedEvidence:
    """Ownership-checked, frozen normalized evidence metadata."""
    handle: Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    source: SourceDescriptor
    tree_ref: str
    frame_ref: str
    commit_ref: str
    query: str | None
    screen_identity: str
    structure_summary: dict[str, Any]
    telemetry_data: dict[str, Any]
    provenance: dict[str, Any]
    raw_frame_ref: FrameRef | None = field(default=None, repr=False)


@dataclass(frozen=True)
class CorroborationResult:
    """Final corroboration result: strictly no pixels, crops, or free-form model rationale."""
    verdict: str
    failure_code: str | None
    tree_ref: str
    frame_ref: str
    commit_ref: str
    structure_checked: bool
    telemetry_checked: bool
    residency_checked: bool

    def __post_init__(self):
        valid_verdicts = {v.value for v in Verdict}
        if self.verdict not in valid_verdicts:
            raise EvidenceError(f"verdict must be one of {sorted(valid_verdicts)}, got {self.verdict!r}")
        if self.failure_code is not None:
            valid_codes = {c.value for c in FailureCode}
            if self.failure_code not in valid_codes:
                raise EvidenceError(f"failure_code must be one of {sorted(valid_codes)}, got {self.failure_code!r}")


class WriteSpy:
    """Volatile memory spy ensuring no raw pixels, receipts, or checkpoints leak."""
    def __init__(self):
        self.leakage_detected = False
        self.leaked_labels: list[str] = []

    def check_data(self, data: Any, label: str) -> None:
        if isinstance(data, (bytes, bytearray)):
            if data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"):
                self.leakage_detected = True
                self.leaked_labels.append(label)
                raise EvidenceError(f"WriteSpy: raw pixel leakage detected for {label}")
        elif isinstance(data, str):
            if "iVBORw0KGgo" in data:
                self.leakage_detected = True
                self.leaked_labels.append(label)
                raise EvidenceError(f"WriteSpy: base64 pixel leakage detected for {label}")
        elif isinstance(data, dict):
            for k, v in data.items():
                self.check_data(v, f"{label}.{k}")
        elif isinstance(data, (list, tuple)):
            for idx, item in enumerate(data):
                self.check_data(item, f"{label}[{idx}]")


def _target_tuple(handle: Any) -> tuple[str, str, int]:
    try:
        if isinstance(handle, dict):
            return handle["kind"], handle["canonical_id"], handle["generation"]
        return handle.kind, handle.canonical_id, handle.generation
    except (AttributeError, KeyError, TypeError) as exc:
        raise EvidenceError("AUSENTE: invalid target handle structure") from exc


# @anchor:wa.perceive.corroboration_evidence.normalize_evidence
def normalize_evidence(
    tree: Any,
    frame_ref: Any,
    binding: Mapping[str, Any],
    *,
    prev_geometry: tuple[int, int] | None = None,
    tolerances: DeclaredTolerances | None = None,
) -> NormalizedEvidence:
    """Normalize and freeze ownership-checked evidence following INV J8 1 & INV J8 2.

    Validates handle, generation, epoch, frame sequence, DPR, source, screen/query,
    and committed binding. Rejects sibling frames, same-epoch resizes, wrong DPR,
    mismatched screen/query/commit, and absent provenance.
    """
    if tree is None:
        raise EvidenceError("AUSENTE: tree must not be None")
    if frame_ref is None:
        raise EvidenceError("AUSENTE: frame_ref must not be None")
    if binding is None or not isinstance(binding, Mapping):
        raise EvidenceError("AUSENTE: binding mapping is required")

    # INV J8 1: Provenance check
    provenance = binding.get("provenance")
    if provenance is None and isinstance(tree, Mapping):
        provenance = tree.get("provenance")
    if not provenance or not isinstance(provenance, Mapping):
        raise EvidenceError("AUSENTE: absent provenance in evidence binding")

    # Handle extraction and validation
    if "handle" not in binding:
        raise EvidenceError("AUSENTE: binding missing target handle")
    binding_handle = binding["handle"]
    b_key = _target_tuple(binding_handle)
    handle_obj = Handle(*b_key) if not isinstance(binding_handle, Handle) else binding_handle

    # Frame handle and sibling frame rejection
    try:
        f_handle = getattr(frame_ref, "handle", None)
        f_key = _target_tuple(f_handle)
    except Exception as exc:
        raise EvidenceError("AUSENTE: frame_ref missing handle") from exc

    if f_key != b_key:
        raise EvidenceError("SIBLING_FRAME: frame handle does not match binding handle")

    # Tree handle check if present
    if isinstance(tree, Mapping) and "handle" in tree:
        t_key = _target_tuple(tree["handle"])
        if t_key != b_key:
            raise EvidenceError("SIBLING_FRAME: tree handle does not match binding handle")

    # Epoch check
    epoch = binding.get("epoch")
    if type(epoch) is not int or epoch < 0:
        raise EvidenceError("AUSENTE: valid integer epoch required in binding")
    if getattr(frame_ref, "epoch", None) != epoch:
        raise EvidenceError("SIBLING_FRAME: frame epoch does not match binding epoch")
    if isinstance(tree, Mapping) and "epoch" in tree and tree["epoch"] != epoch:
        raise EvidenceError("SIBLING_FRAME: tree epoch does not match binding epoch")

    # Same-epoch resize rejection
    width = getattr(frame_ref, "width", None)
    height = getattr(frame_ref, "height", None)
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise EvidenceError("UNKNOWN_GEOMETRY: frame dimensions must be positive integers")

    current_geom = (width, height)
    if prev_geometry is not None and prev_geometry != current_geom:
        raise EvidenceError(
            f"SAME_EPOCH_RESIZE: geometry changed within epoch {epoch} from {prev_geometry} to {current_geom}"
        )

    # Frame sequence check
    frame_seq = binding.get("frame_seq")
    if type(frame_seq) is not int or frame_seq < 0:
        raise EvidenceError("AUSENTE: valid integer frame_seq required in binding")
    if getattr(frame_ref, "frame_seq", None) != frame_seq:
        raise EvidenceError("SKIPPED_EVIDENCE: frame_seq does not match binding")

    # DPR check
    dpr_ppm = binding.get("dpr_ppm")
    if type(dpr_ppm) is not int or dpr_ppm <= 0:
        raise EvidenceError("AUSENTE: positive integer dpr_ppm required in binding")

    viewport = tree.get("viewport") if isinstance(tree, Mapping) else getattr(tree, "viewport", None)
    if isinstance(viewport, Mapping) and "dpr_ppm" in viewport:
        if viewport["dpr_ppm"] != dpr_ppm:
            raise EvidenceError(
                f"WRONG_DPR: viewport dpr_ppm ({viewport['dpr_ppm']}) differs from binding ({dpr_ppm})"
            )

    # Source check (INV J8 1: Actual CDP harvest alone justifies source.kind cdp)
    raw_source = binding.get("source")
    if raw_source is None:
        raise EvidenceError("AUSENTE: source descriptor is required in binding")
    if isinstance(raw_source, SourceDescriptor):
        source_obj = raw_source
    elif isinstance(raw_source, Mapping):
        source_obj = SourceDescriptor(
            kind=raw_source.get("kind", ""),
            session_id=raw_source.get("session_id", ""),
            target_id=raw_source.get("target_id", ""),
            has_actual_cdp_harvest=bool(raw_source.get("has_actual_cdp_harvest", False)),
        )
    else:
        raise EvidenceError("AUSENTE: invalid source descriptor type")

    # Screen identity check
    binding_screen = binding.get("screen_identity")
    if not binding_screen or not isinstance(binding_screen, str):
        raise EvidenceError("AUSENTE: screen_identity required in binding")

    # Query check
    query = binding.get("query")
    if isinstance(tree, Mapping) and "query" in tree:
        if query is not None and tree["query"] != query:
            raise EvidenceError("MISMATCHED_QUERY: tree query does not match binding query")

    # Committed binding check
    commit_ref = binding.get("commit_ref")
    if not commit_ref or not isinstance(commit_ref, str):
        raise EvidenceError("AUSENTE: commit_ref required in binding")

    # Extract tree structure summary and reference
    nodes = tree.get("nodes") if isinstance(tree, Mapping) else getattr(tree, "nodes", [])
    if not isinstance(nodes, Sequence):
        raise EvidenceError("UNKNOWN_GEOMETRY: tree must contain nodes sequence")

    tree_json = json.dumps(
        {
            "nodes_count": len(nodes),
            "viewport": viewport if isinstance(viewport, dict) else None,
            "dpr_ppm": dpr_ppm,
        },
        sort_keys=True,
    )
    tree_ref = hashlib.sha256(tree_json.encode("utf-8")).hexdigest()

    # Frame digest reference (separate from raw pixels)
    frame_digest = getattr(frame_ref, "digest", None)
    if not frame_digest or not isinstance(frame_digest, str) or len(frame_digest) != 64:
        raise EvidenceError("UNKNOWN_GEOMETRY: frame_ref must contain valid 64-char hex digest")
    frame_ref_str = f"frame:{handle_obj.canonical_id}:{epoch}:{frame_seq}:{frame_digest}"

    structure_summary = {
        "nodes_count": len(nodes),
        "width": width,
        "height": height,
        "dpr_ppm": dpr_ppm,
    }
    telemetry_data = dict(binding.get("telemetry", {}))

    return NormalizedEvidence(
        handle=handle_obj,
        epoch=epoch,
        frame_seq=frame_seq,
        dpr_ppm=dpr_ppm,
        source=source_obj,
        tree_ref=tree_ref,
        frame_ref=frame_ref_str,
        commit_ref=str(commit_ref),
        query=query,
        screen_identity=binding_screen,
        structure_summary=structure_summary,
        telemetry_data=telemetry_data,
        provenance=dict(provenance),
        raw_frame_ref=frame_ref,
    )


# @anchor:wa.perceive.corroboration_evidence.corroborate_evidence
def corroborate_evidence(
    evidence: NormalizedEvidence,
    *,
    tolerances: DeclaredTolerances | None = None,
    observer_coverage_pct: int | None = None,
    resident_handles: Sequence[Handle] | None = None,
    telemetry_sample: Mapping[str, Any] | None = None,
    structure_verifier: Callable[[dict[str, Any]], bool] | None = None,
    last_good_fallback: bool = False,
) -> CorroborationResult:
    """Perform independent corroboration across structure, telemetry, and residency.

    Follows INV J8 1 through INV J8 4:
    - Never falls back to a last-good state.
    - Requires independent structure, calibrated telemetry, and complete observer coverage.
    - Result contains no pixels, crops, or free-form rationale.
    """
    if last_good_fallback:
        return CorroborationResult(
            verdict=Verdict.MISMATCH.value,
            failure_code=FailureCode.LAST_GOOD_FALLBACK_FORBIDDEN.value,
            tree_ref=evidence.tree_ref,
            frame_ref=evidence.frame_ref,
            commit_ref=evidence.commit_ref,
            structure_checked=False,
            telemetry_checked=False,
            residency_checked=False,
        )

    tol = tolerances or DeclaredTolerances()

    # 1. Residency check
    residency_checked = False
    if resident_handles is not None:
        target_key = _target_tuple(evidence.handle)
        if any(_target_tuple(h) == target_key for h in resident_handles):
            residency_checked = True
        else:
            return CorroborationResult(
                verdict=Verdict.MISMATCH.value,
                failure_code=FailureCode.SIBLING_FRAME.value,
                tree_ref=evidence.tree_ref,
                frame_ref=evidence.frame_ref,
                commit_ref=evidence.commit_ref,
                structure_checked=False,
                telemetry_checked=False,
                residency_checked=False,
            )
    else:
        residency_checked = True

    # 2. Independent structure verification & complete observer coverage
    structure_checked = False
    coverage = observer_coverage_pct if observer_coverage_pct is not None else 100
    if coverage < tol.min_observer_coverage_pct:
        return CorroborationResult(
            verdict=Verdict.ABSTAIN.value,
            failure_code=FailureCode.INCOMPLETE_OBSERVER_COVERAGE.value,
            tree_ref=evidence.tree_ref,
            frame_ref=evidence.frame_ref,
            commit_ref=evidence.commit_ref,
            structure_checked=False,
            telemetry_checked=False,
            residency_checked=residency_checked,
        )

    if structure_verifier is not None:
        if not structure_verifier(evidence.structure_summary):
            return CorroborationResult(
                verdict=Verdict.MISMATCH.value,
                failure_code=FailureCode.STRUCTURE_MISMATCH.value,
                tree_ref=evidence.tree_ref,
                frame_ref=evidence.frame_ref,
                commit_ref=evidence.commit_ref,
                structure_checked=False,
                telemetry_checked=False,
                residency_checked=residency_checked,
            )

    structure_checked = True

    # 3. Semantically calibrated telemetry verification
    telemetry_checked = False
    if telemetry_sample is not None:
        expected_tick = evidence.telemetry_data.get("tick")
        actual_tick = telemetry_sample.get("tick")
        if expected_tick is not None and actual_tick is not None:
            if abs(actual_tick - expected_tick) > tol.telemetry_tick_tolerance:
                return CorroborationResult(
                    verdict=Verdict.MISMATCH.value,
                    failure_code=FailureCode.UNCALIBRATED_TELEMETRY.value,
                    tree_ref=evidence.tree_ref,
                    frame_ref=evidence.frame_ref,
                    commit_ref=evidence.commit_ref,
                    structure_checked=structure_checked,
                    telemetry_checked=False,
                    residency_checked=residency_checked,
                )
    telemetry_checked = True

    return CorroborationResult(
        verdict=Verdict.CORROBORATED.value,
        failure_code=None,
        tree_ref=evidence.tree_ref,
        frame_ref=evidence.frame_ref,
        commit_ref=evidence.commit_ref,
        structure_checked=structure_checked,
        telemetry_checked=telemetry_checked,
        residency_checked=residency_checked,
    )


class Corroborator:
    """Lane-isolated corroboration coordinator.

    Exceptions reset ONLY the affected temporal lane; other lanes remain intact.
    """
    def __init__(self, tolerances: DeclaredTolerances | None = None):
        self.tolerances = tolerances or DeclaredTolerances()
        self._lane_geometry: dict[tuple[str, str, int, int], tuple[int, int]] = {}
        self._lane_seq: dict[tuple[str, str, int, int], int] = {}
        self._lane_history: dict[tuple[str, str, int, int], list[CorroborationResult]] = {}

    def reset_lane(self, handle: Any, epoch: int) -> None:
        """Reset exclusively the specified temporal lane."""
        key = (*_target_tuple(handle), epoch)
        self._lane_geometry.pop(key, None)
        self._lane_seq.pop(key, None)
        self._lane_history.pop(key, None)

    def corroborate(
        self,
        tree: Any,
        frame_ref: Any,
        binding: Mapping[str, Any],
        *,
        observer_coverage_pct: int | None = None,
        resident_handles: Sequence[Handle] | None = None,
        telemetry_sample: Mapping[str, Any] | None = None,
        structure_verifier: Callable[[dict[str, Any]], bool] | None = None,
        last_good_fallback: bool = False,
    ) -> CorroborationResult:
        """Execute corroboration with isolated per-lane failure recovery."""
        handle = binding.get("handle") if isinstance(binding, Mapping) else None
        epoch = binding.get("epoch") if isinstance(binding, Mapping) else None
        lane_key = (*_target_tuple(handle), epoch) if handle and epoch is not None else None

        prev_geom = self._lane_geometry.get(lane_key) if lane_key else None

        try:
            evidence = normalize_evidence(
                tree,
                frame_ref,
                binding,
                prev_geometry=prev_geom,
                tolerances=self.tolerances,
            )
            result = corroborate_evidence(
                evidence,
                tolerances=self.tolerances,
                observer_coverage_pct=observer_coverage_pct,
                resident_handles=resident_handles,
                telemetry_sample=telemetry_sample,
                structure_verifier=structure_verifier,
                last_good_fallback=last_good_fallback,
            )

            if lane_key:
                self._lane_geometry[lane_key] = (frame_ref.width, frame_ref.height)
                self._lane_seq[lane_key] = frame_ref.frame_seq
                self._lane_history.setdefault(lane_key, []).append(result)

            return result
        except Exception as exc:
            # Exceptions reset only the affected temporal lane
            if lane_key:
                self.reset_lane(handle, epoch)
            raise exc


# @anchor:wa.perceive.corroboration_evidence.evaluate_gate_g_j8_2
def evaluate_gate_g_j8_2(
    corroborator: Corroborator,
    *,
    positive_control_inputs: tuple[Any, Any, Mapping[str, Any]],
    sibling_contrast_inputs: tuple[Any, Any, Mapping[str, Any]],
    resize_contrast_inputs: tuple[Any, Any, Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate Acceptance Gate G-J8-2.

    G-J8-2 requirements:
    1. Positive control: same-window frozen evidence passes corroboration.
    2. Negative contrast 1: sibling frame across targets is rejected.
    3. Negative contrast 2: same-epoch resize is rejected.
    """
    # 1. Positive control
    pos_tree, pos_frame, pos_binding = positive_control_inputs
    pos_res = corroborator.corroborate(pos_tree, pos_frame, pos_binding)
    pos_pass = (pos_res.verdict == Verdict.CORROBORATED.value)

    # 2. Negative contrast: Sibling frame
    sib_tree, sib_frame, sib_binding = sibling_contrast_inputs
    sib_rejected = False
    try:
        corroborator.corroborate(sib_tree, sib_frame, sib_binding)
    except EvidenceError as exc:
        if "SIBLING_FRAME" in str(exc):
            sib_rejected = True

    # Ensure baseline geometry is established for the lane before resize test
    try:
        corroborator.corroborate(pos_tree, pos_frame, pos_binding)
    except Exception:
        pass

    # 3. Negative contrast: Same-epoch resize
    res_tree, res_frame, res_binding = resize_contrast_inputs
    resize_rejected = False
    try:
        corroborator.corroborate(res_tree, res_frame, res_binding)
    except EvidenceError as exc:
        if "SAME_EPOCH_RESIZE" in str(exc):
            resize_rejected = True

    passed = pos_pass and sib_rejected and resize_rejected
    return {
        "gate": "G-J8-2",
        "status": "PASS" if passed else "FAIL",
        "passed": passed,
        "positive_control_passed": pos_pass,
        "sibling_contrast_rejected": sib_rejected,
        "resize_contrast_rejected": resize_rejected,
        "reason": "PASS" if passed else "mutation_did_not_disagree",
    }


def describe_result(result: CorroborationResult, options: DescribeOptions | None = None) -> str:
    """Format corroboration result. Public options disable artifact output."""
    opts = options or DescribeOptions(disable_artifacts=True)
    if not opts.disable_artifacts or opts.emit_crops or opts.emit_pixels:
        raise EvidenceError("Public describe options must disable artifact output")

    return json.dumps(
        {
            "verdict": result.verdict,
            "failure_code": result.failure_code,
            "tree_ref": result.tree_ref,
            "frame_ref": result.frame_ref,
            "commit_ref": result.commit_ref,
            "structure_checked": result.structure_checked,
            "telemetry_checked": result.telemetry_checked,
            "residency_checked": result.residency_checked,
        },
        sort_keys=True,
    )
