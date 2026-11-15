# @module {"id":"wa.perceive.multiwin_perception_audit","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.parallel_perceive","wa.perceive.session_registry","wa.perceive.vision_adapter","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Multi-window perception consistency auditor over observations and result metadata.

Checks DPR, geometry epoch, frame sequence, immutable SHA-256 content digest,
and canonical window identity across concurrent windows. Emits deterministic
per-target diagnostics partitioned into stale, foreign, missing, and
disagreeing evidence. Sibling success never corroborates another target, and
structural validity alone is never treated as authenticity.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import importlib
import re

import numpy as np

from ..state import schema
from .parallel_perceive import (
    ObservationError,
    _TARGET_KINDS,
    _handle_key,
    _prepare,
    perceive_many,
)
from .vision_adapter import FakeVisionEngine, Frame, fuse_regions

try:
    from .session_registry import (
        FrameRef,
        Handle,
        Observation,
        PerceptionResult,
        SessionRegistry,
        TargetRecord,
    )
except ModuleNotFoundError as exc:
    if exc.name != __package__ + ".session_registry":
        raise

    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    @dataclass(frozen=True)
    class TargetRecord:
        handle: Handle
        transport: object
        owned: bool
        epoch: int
        session_id: str | None
        attachment_owned: bool
        closed: bool

    @dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str

    @dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: FrameRef

    @dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Observation | None
        error: str | None

    SessionRegistry = None


DIAG_STALE = "stale"
DIAG_FOREIGN = "foreign"
DIAG_MISSING = "missing"
DIAG_DISAGREEING = "disagreeing"
DIAGNOSTIC_CATEGORIES = (
    DIAG_STALE,
    DIAG_FOREIGN,
    DIAG_MISSING,
    DIAG_DISAGREEING,
)

VERDICT_AUTHENTIC = "AUTHENTIC"
VERDICT_REJECTED = "REJECTED"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_HEX_EVEN_RE = re.compile(r"^[0-9a-f]+$")
_PERCEPTION_KEYS = (
    "handle",
    "epoch",
    "frame_seq",
    "dpr_ppm",
    "screen_identity",
    "descriptor_hex",
    "regions",
    "fusion",
)


# @anchor:wa.perceive.multiwin_perception_audit.AuditConfigError
class AuditConfigError(ValueError):
    """Invalid auditor configuration or blocked unmerged collaborator."""


# @anchor:wa.perceive.multiwin_perception_audit.require_collaborator
def require_collaborator(module_name: str, symbol_name: str | None = None):
    """Lazy-import a collaborator and block on absence rather than fabricating its API."""
    if not isinstance(module_name, str) or not module_name.strip():
        raise AuditConfigError("module_name must be a non-empty string")
    try:
        mod = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name or module_name.startswith(str(exc.name) + "."):
            raise AuditConfigError(
                f"BLOCKED: collaborator module {module_name!r} is AUSENTE"
            ) from exc
        raise
    if symbol_name is not None:
        if not isinstance(symbol_name, str) or not symbol_name.strip():
            raise AuditConfigError("symbol_name must be a non-empty string")
        if not hasattr(mod, symbol_name):
            raise AuditConfigError(
                f"BLOCKED: collaborator symbol {module_name}.{symbol_name} is AUSENTE"
            )
        return getattr(mod, symbol_name)
    return mod


def _is_exact_int(value: object) -> bool:
    return type(value) is int


def _is_canonical_str(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) > 0
        and value == value.strip()
        and "\x00" not in value
    )


# @anchor:wa.perceive.multiwin_perception_audit.canonical_window_identity
def canonical_window_identity(handle: object) -> tuple[str, str, int]:
    """Validate and return canonical `(kind, canonical_id, generation)` identity."""
    if isinstance(handle, dict):
        if not all(k in handle for k in ("kind", "canonical_id", "generation")):
            raise ObservationError("handle fields missing")
        kind = handle["kind"]
        ident = handle["canonical_id"]
        generation = handle["generation"]
    elif (
        isinstance(handle, tuple)
        and len(handle) == 3
        and not hasattr(handle, "kind")
    ):
        kind, ident, generation = handle
    else:
        try:
            kind = handle.kind
            ident = handle.canonical_id
            generation = handle.generation
        except AttributeError as exc:
            raise ObservationError("handle fields missing") from exc

    if kind not in _TARGET_KINDS or not _is_canonical_str(ident):
        raise ObservationError("unsupported or empty target handle")
    if not _is_exact_int(generation) or generation < 0:
        raise ObservationError("generation must be a non-negative integer")
    if generation == 0:
        raise ObservationError("generation must be a positive integer")
    return kind, ident, generation


def _salvage_handle_key(handle: object) -> tuple[tuple[str, str, int] | None, str | None]:
    """Extract a target key even when generation is 0 so stale diagnostics attach to the target."""
    if handle is None:
        return None, "missing_handle"
    try:
        return canonical_window_identity(handle), None
    except ObservationError as exc:
        msg = str(exc)
        if msg == "generation must be a positive integer":
            if isinstance(handle, dict):
                return (handle["kind"], handle["canonical_id"], 0), "stale_zero_generation"
            if isinstance(handle, tuple) and len(handle) == 3:
                return (handle[0], handle[1], 0), "stale_zero_generation"
            return (handle.kind, handle.canonical_id, 0), "stale_zero_generation"
        return None, msg


def _make_handle_obj(key: tuple[str, str, int]) -> Handle:
    kind, ident, gen = key
    if gen >= 1:
        try:
            return Handle(kind, ident, gen)
        except Exception:
            pass
    nh = object.__new__(Handle)
    object.__setattr__(nh, "kind", kind)
    object.__setattr__(nh, "canonical_id", ident)
    object.__setattr__(nh, "generation", gen)
    return nh


# @anchor:wa.perceive.multiwin_perception_audit.verify_immutable_digest
def verify_immutable_digest(
    frame: object,
    *,
    require_readonly_array: bool = False,
) -> tuple[bool, str | None, str | None]:
    """Verify frame dimensions, RGB buffer ownership/immutability, and SHA-256 content digest.

    Returns `(ok, actual_digest, error_code)`.
    """
    if frame is None:
        return False, None, "missing_frame_ref"
    try:
        width = frame.width
        height = frame.height
        rgb = getattr(frame, "rgb", getattr(frame, "data", None))
        digest = getattr(frame, "digest", None)
    except AttributeError:
        return False, None, "missing_frame_fields"

    if not _is_exact_int(width) or not _is_exact_int(height) or width <= 0 or height <= 0:
        return False, None, "invalid_frame_dimensions"

    if isinstance(rgb, np.ndarray):
        if (
            rgb.dtype != np.uint8
            or rgb.shape != (height, width, 3)
            or not rgb.flags.c_contiguous
        ):
            return False, None, "invalid_rgb_array"
        if require_readonly_array and rgb.flags.writeable:
            raw_bytes = rgb.tobytes(order="C")
            actual = hashlib.sha256(raw_bytes).hexdigest()
            return False, actual, "mutable_frame_buffer"
        raw_bytes = rgb.tobytes(order="C")
    elif isinstance(rgb, (bytes, bytearray)):
        if require_readonly_array or isinstance(rgb, bytearray):
            if len(rgb) == height * width * 3:
                actual = hashlib.sha256(bytes(rgb)).hexdigest()
            else:
                actual = None
            return (
                False,
                actual,
                "mutable_frame_buffer"
                if len(rgb) == height * width * 3
                else "invalid_rgb_bytes_length",
            )
        if len(rgb) != height * width * 3:
            return False, None, "invalid_rgb_bytes_length"
        raw_bytes = rgb
    else:
        return False, None, "invalid_rgb_buffer_type"

    actual_digest = hashlib.sha256(raw_bytes).hexdigest()
    if digest is None:
        return False, actual_digest, "missing_frame_digest"
    if not isinstance(digest, str) or not _HEX64_RE.fullmatch(digest):
        return False, actual_digest, "invalid_digest_format"
    if digest != actual_digest:
        return False, actual_digest, "digest_mismatch"
    return True, actual_digest, None


# @anchor:wa.perceive.multiwin_perception_audit.EvidenceIssue
@dataclass(frozen=True)
class EvidenceIssue:
    """Single deterministic audit finding on a target window or batch slot."""

    category: str
    code: str
    field: str
    detail: str
    index: int = -1

    def __post_init__(self) -> None:
        if self.category not in DIAGNOSTIC_CATEGORIES:
            raise AuditConfigError(f"unsupported diagnostic category: {self.category!r}")
        if not _is_canonical_str(self.code):
            raise AuditConfigError("issue code must be a non-empty canonical string")
        if not _is_canonical_str(self.field):
            raise AuditConfigError("issue field must be a non-empty canonical string")
        if not isinstance(self.detail, str) or not self.detail:
            raise AuditConfigError("issue detail must be a non-empty string")
        if not _is_exact_int(self.index):
            raise AuditConfigError("issue index must be an exact integer")

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "code": self.code,
            "field": self.field,
            "detail": self.detail,
            "index": self.index,
        }


# @anchor:wa.perceive.multiwin_perception_audit.TargetAuditDiagnostic
@dataclass(frozen=True)
class TargetAuditDiagnostic:
    """Deterministic per-target perception audit diagnostic."""

    handle: Handle
    authentic: bool
    verdict: str
    stale: tuple
    foreign: tuple
    missing: tuple
    disagreeing: tuple
    issues: tuple
    observed_epochs: tuple
    observed_sequences: tuple
    observed_dpr_ppm: int | None
    latest_digest: str | None
    screen_identities: tuple
    item_indices: tuple

    @property
    def ok(self) -> bool:
        return self.authentic

    @property
    def categories(self) -> tuple[str, ...]:
        present = []
        if self.stale:
            present.append(DIAG_STALE)
        if self.foreign:
            present.append(DIAG_FOREIGN)
        if self.missing:
            present.append(DIAG_MISSING)
        if self.disagreeing:
            present.append(DIAG_DISAGREEING)
        return tuple(present)

    def to_dict(self) -> dict:
        return {
            "handle": {
                "kind": self.handle.kind,
                "canonical_id": self.handle.canonical_id,
                "generation": self.handle.generation,
            },
            "authentic": self.authentic,
            "verdict": self.verdict,
            "stale": list(self.stale),
            "foreign": list(self.foreign),
            "missing": list(self.missing),
            "disagreeing": list(self.disagreeing),
            "issues": [i.to_dict() for i in self.issues],
            "observed_epochs": list(self.observed_epochs),
            "observed_sequences": list(self.observed_sequences),
            "observed_dpr_ppm": self.observed_dpr_ppm,
            "latest_digest": self.latest_digest,
            "screen_identities": list(self.screen_identities),
            "item_indices": list(self.item_indices),
        }


# @anchor:wa.perceive.multiwin_perception_audit.BatchAuditReport
@dataclass(frozen=True)
class BatchAuditReport:
    """Deterministic multi-window batch consistency audit report."""

    authentic: bool
    verdict: str
    targets: tuple
    by_target: dict
    unassigned_issues: tuple
    total_observations: int
    total_results: int
    authentic_count: int
    rejected_count: int

    @property
    def ok(self) -> bool:
        return self.authentic

    def for_target(self, handle: object) -> TargetAuditDiagnostic | None:
        key, _ = _salvage_handle_key(handle)
        if key is not None and key in self.by_target:
            return self.by_target[key]
        if isinstance(handle, str):
            matches = [
                diag for k, diag in self.by_target.items() if k[1] == handle
            ]
            if len(matches) == 1:
                return matches[0]
        return None

    def to_dict(self) -> dict:
        return {
            "authentic": self.authentic,
            "verdict": self.verdict,
            "targets": [t.to_dict() for t in self.targets],
            "unassigned_issues": [i.to_dict() for i in self.unassigned_issues],
            "total_observations": self.total_observations,
            "total_results": self.total_results,
            "authentic_count": self.authentic_count,
            "rejected_count": self.rejected_count,
        }

    def to_json(self) -> str:
        return schema.dumps(self.to_dict())


class _TargetAccumulator:
    def __init__(self, key: tuple[str, str, int]) -> None:
        self.key = key
        self.handle = _make_handle_obj(key)
        self.issues: list[EvidenceIssue] = []
        self.epochs: list[int] = []
        self.sequences: list[int] = []
        self.dprs: list[int] = []
        self.digests: list[str] = []
        self.screen_identities: list[str] = []
        self.indices: list[int] = []
        self.verified_items = 0

    def add_issue(
        self,
        category: str,
        code: str,
        field: str,
        detail: str,
        index: int = -1,
    ) -> None:
        issue = EvidenceIssue(
            category=category,
            code=code,
            field=field,
            detail=detail,
            index=index,
        )
        if issue not in self.issues:
            self.issues.append(issue)

    def finalize(self) -> TargetAuditDiagnostic:
        ordered_issues = tuple(
            sorted(
                self.issues,
                key=lambda i: (
                    DIAGNOSTIC_CATEGORIES.index(i.category),
                    i.index,
                    i.code,
                    i.field,
                    i.detail,
                ),
            )
        )
        stale = tuple(
            dict.fromkeys(i.code for i in ordered_issues if i.category == DIAG_STALE)
        )
        foreign = tuple(
            dict.fromkeys(i.code for i in ordered_issues if i.category == DIAG_FOREIGN)
        )
        missing = tuple(
            dict.fromkeys(i.code for i in ordered_issues if i.category == DIAG_MISSING)
        )
        disagreeing = tuple(
            dict.fromkeys(
                i.code for i in ordered_issues if i.category == DIAG_DISAGREEING
            )
        )
        unique_dprs = list(dict.fromkeys(self.dprs))
        observed_dpr = unique_dprs[0] if len(unique_dprs) == 1 else None
        latest_digest = self.digests[-1] if self.digests else None
        authentic = (
            self.verified_items > 0
            and len(ordered_issues) == 0
            and len(self.indices) > 0
            and self.verified_items == len(self.indices)
        )
        return TargetAuditDiagnostic(
            handle=self.handle,
            authentic=authentic,
            verdict=VERDICT_AUTHENTIC if authentic else VERDICT_REJECTED,
            stale=stale,
            foreign=foreign,
            missing=missing,
            disagreeing=disagreeing,
            issues=ordered_issues,
            observed_epochs=tuple(self.epochs),
            observed_sequences=tuple(self.sequences),
            observed_dpr_ppm=observed_dpr,
            latest_digest=latest_digest,
            screen_identities=tuple(self.screen_identities),
            item_indices=tuple(self.indices),
        )


def _normalize_expectation_map(mapping: dict | None, name: str) -> dict:
    if mapping is None:
        return {}
    if not isinstance(mapping, dict):
        raise AuditConfigError(f"{name} must be a dict")
    normalized = {}
    for raw_k, val in mapping.items():
        if isinstance(raw_k, str):
            normalized[raw_k] = val
        else:
            key, err = _salvage_handle_key(raw_k)
            if key is None:
                raise AuditConfigError(f"invalid target key in {name}: {err}")
            normalized[key] = val
    return normalized


def _lookup_expectation(norm_map: dict, key: tuple[str, str, int]):
    if key in norm_map:
        return True, norm_map[key]
    if (key[0], key[1]) in norm_map:
        return True, norm_map[(key[0], key[1])]
    if key[1] in norm_map:
        return True, norm_map[key[1]]
    return False, None


def _is_perception_result_like(item: object) -> bool:
    return (
        hasattr(item, "handle")
        and hasattr(item, "observation")
        and hasattr(item, "error")
        and not hasattr(item, "frame_seq")
    )


def _snapshot_registry(registry: object) -> dict | None:
    if registry is None:
        return None
    lock = getattr(registry, "_lock", None)
    active_attr = getattr(registry, "_active", None)
    tombstones_attr = getattr(registry, "_tombstones", None)
    last_seq_attr = getattr(registry, "_last_frame_seq", None)
    latest_digest_attr = getattr(registry, "_latest_frame_digest", None)
    if not isinstance(active_attr, dict) or not isinstance(tombstones_attr, dict):
        raise AuditConfigError("registry must be a SessionRegistry instance")
    if lock is not None:
        with lock:
            return {
                "active": dict(active_attr),
                "tombstones": dict(tombstones_attr),
                "last_seq": dict(last_seq_attr or {}),
                "latest_digest": dict(latest_digest_attr or {}),
            }
    return {
        "active": dict(active_attr),
        "tombstones": dict(tombstones_attr),
        "last_seq": dict(last_seq_attr or {}),
        "latest_digest": dict(latest_digest_attr or {}),
    }


def _strip_perception_key(state: dict) -> dict:
    copied = deepcopy(state)
    copied.pop("_perception", None)
    return copied


def _audit_ax_state_dict(
    acc: _TargetAccumulator,
    state: object,
    *,
    primary_key: tuple[str, str, int],
    epoch: int | None,
    epoch_valid: bool,
    dpr: int | None,
    dpr_valid: bool,
    idx: int,
    prefix: str = "state",
) -> None:
    if not isinstance(state, dict) or not isinstance(state.get("nodes"), list):
        acc.add_issue(
            DIAG_MISSING,
            "missing_state_nodes",
            f"{prefix}.nodes",
            "AX state must be a dict containing a nodes list",
            idx,
        )
        return

    try:
        schema._no_float(state)
    except schema.StateError as exc:
        acc.add_issue(
            DIAG_DISAGREEING,
            "state_contains_float",
            prefix,
            str(exc),
            idx,
        )

    viewport = state.get("viewport")
    if isinstance(viewport, dict) and "dpr_ppm" in viewport:
        vp_dpr = viewport["dpr_ppm"]
        if not _is_exact_int(vp_dpr) or (dpr_valid and vp_dpr != dpr):
            acc.add_issue(
                DIAG_DISAGREEING,
                "viewport_dpr_mismatch",
                f"{prefix}.viewport.dpr_ppm",
                f"viewport dpr_ppm {vp_dpr!r} != observation dpr_ppm {dpr!r}",
                idx,
            )

    if "handle" in state:
        st_h_key, st_h_err = _salvage_handle_key(state["handle"])
        if st_h_key is None or st_h_err is not None or st_h_key != primary_key:
            acc.add_issue(
                DIAG_FOREIGN,
                "foreign_state_handle",
                f"{prefix}.handle",
                f"state handle {st_h_key or st_h_err} != target {primary_key}",
                idx,
            )
            acc.add_issue(
                DIAG_DISAGREEING,
                "state_handle_mismatch",
                f"{prefix}.handle",
                f"state handle does not match target {primary_key}",
                idx,
            )

    if "epoch" in state:
        st_ep = state["epoch"]
        if not _is_exact_int(st_ep):
            acc.add_issue(
                DIAG_DISAGREEING,
                "invalid_state_epoch",
                f"{prefix}.epoch",
                f"state.epoch must be int, got {st_ep!r}",
                idx,
            )
        elif epoch_valid and st_ep != epoch:
            if st_ep < epoch:
                acc.add_issue(
                    DIAG_STALE,
                    "stale_state_epoch",
                    f"{prefix}.epoch",
                    f"state.epoch {st_ep} < observation epoch {epoch}",
                    idx,
                )
            acc.add_issue(
                DIAG_DISAGREEING,
                "state_epoch_mismatch",
                f"{prefix}.epoch",
                f"state.epoch {st_ep} != observation epoch {epoch}",
                idx,
            )

    for n_idx, node in enumerate(state.get("nodes", ())):
        if not isinstance(node, dict):
            acc.add_issue(
                DIAG_DISAGREEING,
                "invalid_ax_node",
                f"{prefix}.nodes[{n_idx}]",
                "AX node must be a dict",
                idx,
            )
            continue
        if "handle" in node:
            nh_key, nh_err = _salvage_handle_key(node["handle"])
            if nh_key is None or nh_err is not None or nh_key != primary_key:
                acc.add_issue(
                    DIAG_FOREIGN,
                    "foreign_ax_node",
                    f"{prefix}.nodes[{n_idx}].handle",
                    f"AX node belongs to foreign target {nh_key or nh_err}",
                    idx,
                )
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "ax_node_handle_mismatch",
                    f"{prefix}.nodes[{n_idx}].handle",
                    f"AX node handle {nh_key} != target {primary_key}",
                    idx,
                )
        if "epoch" in node:
            nep = node["epoch"]
            if not _is_exact_int(nep):
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "invalid_ax_node_epoch",
                    f"{prefix}.nodes[{n_idx}].epoch",
                    f"AX node epoch must be int, got {nep!r}",
                    idx,
                )
            elif epoch_valid and nep != epoch:
                if nep < epoch:
                    acc.add_issue(
                        DIAG_STALE,
                        "stale_ax_node_epoch",
                        f"{prefix}.nodes[{n_idx}].epoch",
                        f"AX node epoch {nep} < observation epoch {epoch}",
                        idx,
                    )
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "ax_node_epoch_mismatch",
                    f"{prefix}.nodes[{n_idx}].epoch",
                    f"AX node epoch {nep} != observation epoch {epoch}",
                    idx,
                )


# @anchor:wa.perceive.multiwin_perception_audit.audit_multiwin_perception
def audit_multiwin_perception(
    observations=None,
    results=None,
    *,
    adapter=None,
    registry=None,
    expected_targets=None,
    expected_epochs=None,
    expected_dpr_ppm=None,
    expected_sequences=None,
    expected_digests=None,
    require_perception_metadata: bool = True,
) -> BatchAuditReport:
    """Audit multi-window observations and perception result metadata for consistency.

    Checks canonical window identity, geometry epoch, frame sequence, DPR,
    immutable SHA-256 frame content digest, adapter screen identity/regions,
    and target-bound region fusion across concurrent windows.
    """
    if type(require_perception_metadata) is not bool:
        raise AuditConfigError("require_perception_metadata must be a bool")

    norm_epochs = _normalize_expectation_map(expected_epochs, "expected_epochs")
    norm_dprs = _normalize_expectation_map(expected_dpr_ppm, "expected_dpr_ppm")
    norm_seqs = _normalize_expectation_map(expected_sequences, "expected_sequences")
    norm_digests = _normalize_expectation_map(expected_digests, "expected_digests")
    reg_snap = _snapshot_registry(registry)

    expected_target_keys: set[tuple[str, str, int]] = set()
    if expected_targets is not None:
        if isinstance(expected_targets, dict):
            iterable_targets = expected_targets.keys()
        else:
            iterable_targets = expected_targets
        for raw_t in iterable_targets:
            t_key, t_err = _salvage_handle_key(raw_t)
            if t_key is None or t_err is not None:
                raise AuditConfigError(f"invalid expected_targets entry: {t_err}")
            expected_target_keys.add(t_key)

    obs_list: list = []
    res_list: list = []
    results_supplied = results is not None

    if observations is not None and results is None:
        raw_items = list(observations)
        if raw_items and all(_is_perception_result_like(x) for x in raw_items):
            res_list = raw_items
            obs_list = [r.observation if r is not None else None for r in res_list]
            results_supplied = True
        else:
            obs_list = raw_items
            res_list = [None] * len(obs_list)
    else:
        obs_list = list(observations) if observations is not None else []
        res_list = list(results) if results is not None else []

    total_observations = len(obs_list) if observations is not None else 0
    total_results = len(res_list) if results_supplied else 0

    accumulators: dict[tuple[str, str, int], _TargetAccumulator] = {}
    unassigned_issues: list[EvidenceIssue] = []

    def get_acc(key: tuple[str, str, int]) -> _TargetAccumulator:
        if key not in accumulators:
            accumulators[key] = _TargetAccumulator(key)
        return accumulators[key]

    for exp_key in sorted(expected_target_keys):
        get_acc(exp_key)

    # First pass: collect per-slot keys and actual frame digests to detect cross-window bleed
    n_slots = max(len(obs_list), len(res_list))
    digest_owners: dict[str, set[tuple[str, str, int]]] = {}
    identity_owners: dict[str, set[tuple[str, str, int]]] = {}
    max_gen_by_dest: dict[tuple[str, str], int] = {}

    if reg_snap is not None:
        for h_obj in reg_snap["active"]:
            h_key, _ = _salvage_handle_key(h_obj)
            if h_key is not None:
                dest = (h_key[0], h_key[1])
                if h_key[2] > max_gen_by_dest.get(dest, 0):
                    max_gen_by_dest[dest] = h_key[2]
        for h_obj in reg_snap["tombstones"]:
            h_key, _ = _salvage_handle_key(h_obj)
            if h_key is not None:
                dest = (h_key[0], h_key[1])
                if h_key[2] > max_gen_by_dest.get(dest, 0):
                    max_gen_by_dest[dest] = h_key[2]

    for exp_key in expected_target_keys:
        dest = (exp_key[0], exp_key[1])
        if exp_key[2] > max_gen_by_dest.get(dest, 0):
            max_gen_by_dest[dest] = exp_key[2]

    engine = FakeVisionEngine() if adapter is None else adapter

    for idx in range(n_slots):
        obs = obs_list[idx] if idx < len(obs_list) else None
        res = res_list[idx] if idx < len(res_list) else None
        candidate = obs if obs is not None else (res.observation if res is not None else None)
        h_raw = getattr(obs, "handle", None) if obs is not None else getattr(res, "handle", None)
        h_key, _ = _salvage_handle_key(h_raw)
        if h_key is not None:
            dest = (h_key[0], h_key[1])
            if h_key[2] > max_gen_by_dest.get(dest, 0):
                max_gen_by_dest[dest] = h_key[2]
            if candidate is not None and getattr(candidate, "frame", None) is not None:
                _, actual_dig, _ = verify_immutable_digest(
                    candidate.frame, require_readonly_array=False
                )
                if actual_dig is not None:
                    digest_owners.setdefault(actual_dig, set()).add(h_key)
                try:
                    w = candidate.frame.width
                    h = candidate.frame.height
                    rgb = candidate.frame.rgb
                    raw_b = (
                        rgb.tobytes(order="C")
                        if isinstance(rgb, np.ndarray)
                        else bytes(rgb)
                    )
                    fr = Frame(w, h, raw_b)
                    sid = engine.screen_identity(fr)
                    if isinstance(sid, str):
                        identity_owners.setdefault(sid, set()).add(h_key)
                except Exception:
                    pass

    # State tracking across batch order per target and per (target, epoch) incarnation
    max_epoch_seen: dict[tuple[str, str, int], int] = {}
    last_seq_seen: dict[tuple[tuple[str, str, int], int], int] = {}
    incarnation_geom: dict[tuple[tuple[str, str, int], int], tuple[int, int]] = {}
    incarnation_dpr: dict[tuple[tuple[str, str, int], int], int] = {}
    incarnation_prev_frame: dict[tuple[tuple[str, str, int], int], Frame | None] = {}

    for idx in range(n_slots):
        has_obs_slot = idx < len(obs_list)
        has_res_slot = idx < len(res_list) if results_supplied else False
        obs = obs_list[idx] if has_obs_slot else None
        res = res_list[idx] if has_res_slot else None

        obs_key, obs_h_err = (
            _salvage_handle_key(getattr(obs, "handle", None))
            if obs is not None
            else (None, None)
        )
        res_key, res_h_err = (
            _salvage_handle_key(getattr(res, "handle", None))
            if res is not None
            else (None, None)
        )

        primary_key = obs_key if obs_key is not None else res_key
        if primary_key is None:
            err_detail = obs_h_err or res_h_err or "missing_handle"
            unassigned_issues.append(
                EvidenceIssue(
                    category=DIAG_MISSING if err_detail == "missing_handle" else DIAG_DISAGREEING,
                    code="unresolvable_target_handle",
                    field="handle",
                    detail=f"slot {idx} handle invalid: {err_detail}",
                    index=idx,
                )
            )
            continue

        acc = get_acc(primary_key)
        if idx not in acc.indices:
            acc.indices.append(idx)
        initial_issue_count = len(acc.issues)

        if obs_h_err == "stale_zero_generation" or res_h_err == "stale_zero_generation":
            acc.add_issue(
                DIAG_STALE,
                "stale_generation",
                "handle.generation",
                "handle generation must be >= 1, got 0",
                idx,
            )
            acc.add_issue(
                DIAG_DISAGREEING,
                "invalid_handle_generation",
                "handle.generation",
                "handle generation 0 violates canonical positive integer contract",
                idx,
            )

        # Check if both obs and res exist when both sequences were supplied
        if observations is not None and results_supplied:
            if not has_obs_slot or obs is None:
                acc.add_issue(
                    DIAG_MISSING,
                    "missing_observation",
                    "observation",
                    f"missing input observation at batch index {idx}",
                    idx,
                )
            if not has_res_slot or res is None:
                acc.add_issue(
                    DIAG_MISSING,
                    "missing_result",
                    "result",
                    f"missing perception result at batch index {idx}",
                    idx,
                )

        # Check handle agreement between input observation and result wrapper
        if obs is not None and res is not None:
            if obs_key is None:
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "invalid_observation_handle",
                    "observation.handle",
                    f"observation handle invalid: {obs_h_err}",
                    idx,
                )
            elif res_key is None:
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "invalid_result_handle",
                    "result.handle",
                    f"result handle invalid: {res_h_err}",
                    idx,
                )
            elif obs_key != res_key:
                acc.add_issue(
                    DIAG_FOREIGN,
                    "foreign_result_handle",
                    "result.handle",
                    f"result handle {res_key} does not match observation handle {obs_key}",
                    idx,
                )
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "result_handle_mismatch",
                    "result.handle",
                    f"observation handle {obs_key} != result handle {res_key}",
                    idx,
                )
                other_acc = get_acc(res_key)
                if idx not in other_acc.indices:
                    other_acc.indices.append(idx)
                other_acc.add_issue(
                    DIAG_FOREIGN,
                    "foreign_observation_handle",
                    "observation.handle",
                    f"result for {res_key} paired with foreign observation {obs_key}",
                    idx,
                )
                other_acc.add_issue(
                    DIAG_DISAGREEING,
                    "result_handle_mismatch",
                    "observation.handle",
                    f"result handle {res_key} != observation handle {obs_key}",
                    idx,
                )

        # Check generation staleness across batch / registry / expected_targets
        dest = (primary_key[0], primary_key[1])
        if primary_key[2] < max_gen_by_dest.get(dest, primary_key[2]):
            acc.add_issue(
                DIAG_STALE,
                "stale_generation",
                "handle.generation",
                f"generation {primary_key[2]} is superseded by generation {max_gen_by_dest[dest]}",
                idx,
            )

        if expected_target_keys and primary_key not in expected_target_keys:
            same_dest_expected = [
                k for k in expected_target_keys if (k[0], k[1]) == dest
            ]
            if same_dest_expected and all(primary_key[2] < k[2] for k in same_dest_expected):
                acc.add_issue(
                    DIAG_STALE,
                    "stale_generation",
                    "handle.generation",
                    f"handle {primary_key} predates expected target generation",
                    idx,
                )
            else:
                acc.add_issue(
                    DIAG_FOREIGN,
                    "foreign_unexpected_target",
                    "handle",
                    f"target {primary_key} is not in expected_targets",
                    idx,
                )

        # Check registry target status
        if reg_snap is not None:
            h_obj = acc.handle
            if h_obj in reg_snap["tombstones"]:
                acc.add_issue(
                    DIAG_STALE,
                    "stale_tombstoned_handle",
                    "registry",
                    f"handle {primary_key} is retired/tombstoned in SessionRegistry",
                    idx,
                )
            elif h_obj not in reg_snap["active"]:
                same_dest_active = [
                    k for k in reg_snap["active"] if (k.kind, k.canonical_id) == dest
                ]
                if same_dest_active and any(primary_key[2] < k.generation for k in same_dest_active):
                    acc.add_issue(
                        DIAG_STALE,
                        "stale_generation",
                        "registry",
                        f"handle {primary_key} generation superseded in SessionRegistry",
                        idx,
                    )
                else:
                    acc.add_issue(
                        DIAG_FOREIGN,
                        "foreign_unregistered_target",
                        "registry",
                        f"handle {primary_key} is not registered in SessionRegistry",
                        idx,
                    )

        # Inspect result wrapper receipt
        robs = None
        if res is not None:
            res_err = getattr(res, "error", None)
            robs = getattr(res, "observation", None)
            if res_err is not None:
                if not isinstance(res_err, str) or not res_err:
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "invalid_error_receipt",
                        "result.error",
                        "result.error must be a non-empty string or None",
                        idx,
                    )
                acc.add_issue(
                    DIAG_MISSING,
                    "result_error_receipt",
                    "result.error",
                    f"perception failed with error receipt: {res_err}",
                    idx,
                )
                if robs is not None:
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "ambiguous_error_and_observation",
                        "result",
                        "result contains both error receipt and non-null observation",
                        idx,
                    )
            elif robs is None:
                acc.add_issue(
                    DIAG_MISSING,
                    "missing_result_observation",
                    "result.observation",
                    "result has neither error receipt nor observation",
                    idx,
                )

        active_obs = obs if obs is not None else robs
        if active_obs is None:
            if obs is None and res is None:
                acc.add_issue(
                    DIAG_MISSING,
                    "missing_observation",
                    "observation",
                    f"no observation or result at index {idx}",
                    idx,
                )
            continue

        # Validate active_obs core fields (epoch, frame_seq, dpr_ppm, state, frame)
        epoch = getattr(active_obs, "epoch", None)
        seq = getattr(active_obs, "frame_seq", None)
        dpr = getattr(active_obs, "dpr_ppm", None)
        state = getattr(active_obs, "state", None)
        frame_ref = getattr(active_obs, "frame", None)

        epoch_valid = _is_exact_int(epoch) and epoch >= 0
        seq_valid = _is_exact_int(seq) and seq >= 0
        dpr_valid = _is_exact_int(dpr) and dpr > 0

        if not epoch_valid:
            acc.add_issue(
                DIAG_DISAGREEING,
                "invalid_epoch",
                "epoch",
                f"epoch must be a non-negative integer, got {epoch!r}",
                idx,
            )
        else:
            acc.epochs.append(epoch)

        if not seq_valid:
            acc.add_issue(
                DIAG_DISAGREEING,
                "invalid_frame_seq",
                "frame_seq",
                f"frame_seq must be a non-negative integer, got {seq!r}",
                idx,
            )
        else:
            acc.sequences.append(seq)

        if not dpr_valid:
            acc.add_issue(
                DIAG_DISAGREEING,
                "invalid_dpr_ppm",
                "dpr_ppm",
                f"dpr_ppm must be a positive integer, got {dpr!r}",
                idx,
            )
        else:
            acc.dprs.append(dpr)

        # Temporal ordering & incarnation consistency
        if epoch_valid:
            prev_max_epoch = max_epoch_seen.get(primary_key, -1)
            if epoch < prev_max_epoch:
                acc.add_issue(
                    DIAG_STALE,
                    "stale_epoch_regression",
                    "epoch",
                    f"epoch {epoch} regressed below prior epoch {prev_max_epoch} for target",
                    idx,
                )
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "epoch_regression",
                    "epoch",
                    f"epoch {epoch} < prior epoch {prev_max_epoch}",
                    idx,
                )
            else:
                max_epoch_seen[primary_key] = epoch

        inc_key = (primary_key, epoch) if epoch_valid else None
        if inc_key is not None and seq_valid:
            prev_seq = last_seq_seen.get(inc_key, -1)
            if seq <= prev_seq:
                acc.add_issue(
                    DIAG_STALE,
                    "stale_sequence",
                    "frame_seq",
                    f"non-increasing frame_seq {seq} <= {prev_seq} in incarnation",
                    idx,
                )
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "non_increasing_sequence",
                    "frame_seq",
                    f"frame_seq {seq} <= previous {prev_seq}",
                    idx,
                )
            else:
                last_seq_seen[inc_key] = seq

        if inc_key is not None and dpr_valid:
            if inc_key in incarnation_dpr and incarnation_dpr[inc_key] != dpr:
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "dpr_changed_without_epoch",
                    "dpr_ppm",
                    f"dpr_ppm changed from {incarnation_dpr[inc_key]} to {dpr} within epoch {epoch}",
                    idx,
                )
            else:
                incarnation_dpr[inc_key] = dpr

        # Check state dictionary, viewport DPR, state handle/epoch, and AX nodes
        _audit_ax_state_dict(
            acc,
            state,
            primary_key=primary_key,
            epoch=epoch,
            epoch_valid=epoch_valid,
            dpr=dpr,
            dpr_valid=dpr_valid,
            idx=idx,
            prefix="state",
        )

        # Validate FrameRef provenance, geometry, immutability, and SHA-256 digest
        actual_obs_digest = None
        if frame_ref is None:
            acc.add_issue(
                DIAG_MISSING,
                "missing_frame_ref",
                "frame",
                "observation frame reference is missing",
                idx,
            )
        else:
            fh_key, fh_err = _salvage_handle_key(getattr(frame_ref, "handle", None))
            if fh_key is None or fh_err is not None:
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "invalid_frame_handle",
                    "frame.handle",
                    f"frame.handle invalid: {fh_err}",
                    idx,
                )
            elif fh_key != primary_key:
                if (fh_key[0], fh_key[1]) == dest and fh_key[2] < primary_key[2]:
                    acc.add_issue(
                        DIAG_STALE,
                        "stale_frame_generation",
                        "frame.handle",
                        f"frame.handle generation {fh_key[2]} < {primary_key[2]}",
                        idx,
                    )
                else:
                    acc.add_issue(
                        DIAG_FOREIGN,
                        "foreign_frame_handle",
                        "frame.handle",
                        f"frame.handle {fh_key} != target {primary_key}",
                        idx,
                    )
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "frame_handle_mismatch",
                    "frame.handle",
                    f"frame.handle {fh_key} != observation handle {primary_key}",
                    idx,
                )

            f_ep = getattr(frame_ref, "epoch", None)
            if not _is_exact_int(f_ep):
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "invalid_frame_epoch",
                    "frame.epoch",
                    f"frame.epoch must be int, got {f_ep!r}",
                    idx,
                )
            elif epoch_valid and f_ep != epoch:
                if f_ep < epoch:
                    acc.add_issue(
                        DIAG_STALE,
                        "stale_frame_epoch",
                        "frame.epoch",
                        f"frame.epoch {f_ep} < observation epoch {epoch}",
                        idx,
                    )
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "frame_epoch_mismatch",
                    "frame.epoch",
                    f"frame.epoch {f_ep} != observation epoch {epoch}",
                    idx,
                )

            f_seq = getattr(frame_ref, "frame_seq", None)
            if not _is_exact_int(f_seq):
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "invalid_frame_seq",
                    "frame.frame_seq",
                    f"frame.frame_seq must be int, got {f_seq!r}",
                    idx,
                )
            elif seq_valid and f_seq != seq:
                if f_seq < seq:
                    acc.add_issue(
                        DIAG_STALE,
                        "stale_frame_seq",
                        "frame.frame_seq",
                        f"frame.frame_seq {f_seq} < observation frame_seq {seq}",
                        idx,
                    )
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "frame_sequence_mismatch",
                    "frame.frame_seq",
                    f"frame.frame_seq {f_seq} != observation frame_seq {seq}",
                    idx,
                )

            # Check geometry consistency within incarnation
            fw = getattr(frame_ref, "width", None)
            fh = getattr(frame_ref, "height", None)
            if _is_exact_int(fw) and _is_exact_int(fh) and fw > 0 and fh > 0 and inc_key is not None:
                dims = (fw, fh)
                if inc_key in incarnation_geom and incarnation_geom[inc_key] != dims:
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "geometry_changed_without_epoch",
                        "frame.dimensions",
                        f"geometry changed from {incarnation_geom[inc_key]} to {dims} in epoch {epoch}",
                        idx,
                    )
                else:
                    incarnation_geom[inc_key] = dims

            must_be_readonly = robs is not None or (
                isinstance(state, dict) and "_perception" in state
            )
            dig_ok, actual_obs_digest, dig_err = verify_immutable_digest(
                frame_ref, require_readonly_array=must_be_readonly
            )
            if not dig_ok:
                claimed_dig = getattr(frame_ref, "digest", None)
                if dig_err == "missing_frame_digest":
                    acc.add_issue(
                        DIAG_MISSING,
                        "missing_frame_digest",
                        "frame.digest",
                        "frame.digest is missing",
                        idx,
                    )
                else:
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        dig_err or "digest_mismatch",
                        "frame.digest" if "digest" in (dig_err or "") else "frame.rgb",
                        f"frame digest/buffer verification failed: {dig_err}",
                        idx,
                    )
                    if (
                        isinstance(claimed_dig, str)
                        and claimed_dig in digest_owners
                        and primary_key not in digest_owners[claimed_dig]
                    ):
                        acc.add_issue(
                            DIAG_FOREIGN,
                            "foreign_sibling_digest",
                            "frame.digest",
                            f"claimed digest belongs to sibling target {sorted(digest_owners[claimed_dig])[0]}",
                            idx,
                        )
            elif actual_obs_digest is not None:
                acc.digests.append(actual_obs_digest)

        # Cross-check input observation `obs` vs result observation `robs` when both exist
        if obs is not None and robs is not None and obs is not robs:
            robs_h_key, _ = _salvage_handle_key(getattr(robs, "handle", None))
            if robs_h_key != primary_key:
                acc.add_issue(
                    DIAG_FOREIGN,
                    "foreign_observation_handle",
                    "result.observation.handle",
                    f"result.observation.handle {robs_h_key} != {primary_key}",
                    idx,
                )
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "result_observation_handle_mismatch",
                    "result.observation.handle",
                    f"result.observation.handle {robs_h_key} != {primary_key}",
                    idx,
                )
            if getattr(robs, "epoch", None) != epoch:
                if _is_exact_int(getattr(robs, "epoch", None)) and epoch_valid and robs.epoch < epoch:
                    acc.add_issue(
                        DIAG_STALE,
                        "stale_result_epoch",
                        "result.observation.epoch",
                        f"result.observation.epoch {robs.epoch} < {epoch}",
                        idx,
                    )
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "result_epoch_mismatch",
                    "result.observation.epoch",
                    f"result.observation.epoch {getattr(robs, 'epoch', None)!r} != {epoch!r}",
                    idx,
                )
            if getattr(robs, "frame_seq", None) != seq:
                if _is_exact_int(getattr(robs, "frame_seq", None)) and seq_valid and robs.frame_seq < seq:
                    acc.add_issue(
                        DIAG_STALE,
                        "stale_result_sequence",
                        "result.observation.frame_seq",
                        f"result.observation.frame_seq {robs.frame_seq} < {seq}",
                        idx,
                    )
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "result_sequence_mismatch",
                    "result.observation.frame_seq",
                    f"result.observation.frame_seq {getattr(robs, 'frame_seq', None)!r} != {seq!r}",
                    idx,
                )
            if getattr(robs, "dpr_ppm", None) != dpr:
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "result_dpr_mismatch",
                    "result.observation.dpr_ppm",
                    f"result.observation.dpr_ppm {getattr(robs, 'dpr_ppm', None)!r} != {dpr!r}",
                    idx,
                )

            rframe = getattr(robs, "frame", None)
            if rframe is None:
                acc.add_issue(
                    DIAG_MISSING,
                    "missing_result_frame",
                    "result.observation.frame",
                    "result.observation.frame is missing",
                    idx,
                )
            else:
                rf_h_key, _ = _salvage_handle_key(getattr(rframe, "handle", None))
                if rf_h_key != primary_key:
                    acc.add_issue(
                        DIAG_FOREIGN,
                        "foreign_result_frame_handle",
                        "result.observation.frame.handle",
                        f"result.observation.frame.handle {rf_h_key} != {primary_key}",
                        idx,
                    )
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "result_frame_handle_mismatch",
                        "result.observation.frame.handle",
                        f"result.observation.frame.handle {rf_h_key} != {primary_key}",
                        idx,
                    )
                if getattr(rframe, "epoch", None) != epoch:
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "result_frame_epoch_mismatch",
                        "result.observation.frame.epoch",
                        f"result.observation.frame.epoch {getattr(rframe, 'epoch', None)!r} != {epoch!r}",
                        idx,
                    )
                if getattr(rframe, "frame_seq", None) != seq:
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "result_frame_sequence_mismatch",
                        "result.observation.frame.frame_seq",
                        f"result.observation.frame.frame_seq {getattr(rframe, 'frame_seq', None)!r} != {seq!r}",
                        idx,
                    )
                rdig_ok, actual_robs_digest, rdig_err = verify_immutable_digest(
                    rframe, require_readonly_array=True
                )
                if not rdig_ok:
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        rdig_err or "digest_mismatch",
                        "result.observation.frame",
                        f"result frame verification failed: {rdig_err}",
                        idx,
                    )
                elif (
                    actual_obs_digest is not None
                    and (
                        actual_robs_digest != actual_obs_digest
                        or rframe.digest != getattr(frame_ref, "digest", None)
                    )
                ):
                    if (
                        actual_robs_digest in digest_owners
                        and primary_key not in digest_owners[actual_robs_digest]
                    ):
                        acc.add_issue(
                            DIAG_FOREIGN,
                            "foreign_sibling_digest",
                            "result.observation.frame.digest",
                            "result frame digest matches a foreign sibling window",
                            idx,
                        )
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "input_output_digest_mismatch",
                        "result.observation.frame.digest",
                        "result frame content digest differs from input observation digest",
                        idx,
                    )

            rstate = getattr(robs, "state", None)
            _audit_ax_state_dict(
                acc,
                rstate,
                primary_key=primary_key,
                epoch=epoch,
                epoch_valid=epoch_valid,
                dpr=dpr,
                dpr_valid=dpr_valid,
                idx=idx,
                prefix="result.observation.state",
            )
            if isinstance(state, dict) and isinstance(rstate, dict):
                if _strip_perception_key(state) != _strip_perception_key(rstate):
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "state_mutation_mismatch",
                        "result.observation.state",
                        "result observation AX state differs from input observation state",
                        idx,
                    )

        # Inspect _perception metadata on perceived observation
        perceived_obs = robs if robs is not None else obs
        p_state = getattr(perceived_obs, "state", None) if perceived_obs is not None else None
        p_meta = p_state.get("_perception") if isinstance(p_state, dict) else None

        if p_meta is None:
            if require_perception_metadata and (res is None or getattr(res, "error", None) is None):
                acc.add_issue(
                    DIAG_MISSING,
                    "missing_perception_metadata",
                    "state._perception",
                    "observation lacks verified _perception metadata; structural validity is not authenticity",
                    idx,
                )
        elif not isinstance(p_meta, dict):
            acc.add_issue(
                DIAG_DISAGREEING,
                "invalid_perception_metadata",
                "state._perception",
                "_perception metadata must be a dict",
                idx,
            )
        else:
            missing_p_keys = [k for k in _PERCEPTION_KEYS if k not in p_meta]
            if missing_p_keys:
                acc.add_issue(
                    DIAG_MISSING,
                    "incomplete_perception_metadata",
                    "state._perception",
                    f"missing _perception keys: {missing_p_keys}",
                    idx,
                )
            else:
                pm_h_key, pm_h_err = _salvage_handle_key(p_meta["handle"])
                if pm_h_key is None or pm_h_err is not None or pm_h_key != primary_key:
                    if (
                        pm_h_key is not None
                        and (pm_h_key[0], pm_h_key[1]) == dest
                        and pm_h_key[2] < primary_key[2]
                    ):
                        acc.add_issue(
                            DIAG_STALE,
                            "stale_perception_generation",
                            "state._perception.handle",
                            f"_perception handle generation {pm_h_key[2]} < {primary_key[2]}",
                            idx,
                        )
                    else:
                        acc.add_issue(
                            DIAG_FOREIGN,
                            "foreign_perception_handle",
                            "state._perception.handle",
                            f"_perception handle {pm_h_key or pm_h_err} != target {primary_key}",
                            idx,
                        )
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "perception_handle_mismatch",
                        "state._perception.handle",
                        f"_perception handle does not match target {primary_key}",
                        idx,
                    )

                pm_ep = p_meta["epoch"]
                if not _is_exact_int(pm_ep) or (epoch_valid and pm_ep != epoch):
                    if _is_exact_int(pm_ep) and epoch_valid and pm_ep < epoch:
                        acc.add_issue(
                            DIAG_STALE,
                            "stale_perception_epoch",
                            "state._perception.epoch",
                            f"_perception.epoch {pm_ep} < {epoch}",
                            idx,
                        )
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "perception_epoch_mismatch",
                        "state._perception.epoch",
                        f"_perception.epoch {pm_ep!r} != {epoch!r}",
                        idx,
                    )

                pm_seq = p_meta["frame_seq"]
                if not _is_exact_int(pm_seq) or (seq_valid and pm_seq != seq):
                    if _is_exact_int(pm_seq) and seq_valid and pm_seq < seq:
                        acc.add_issue(
                            DIAG_STALE,
                            "stale_perception_sequence",
                            "state._perception.frame_seq",
                            f"_perception.frame_seq {pm_seq} < {seq}",
                            idx,
                        )
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "perception_sequence_mismatch",
                        "state._perception.frame_seq",
                        f"_perception.frame_seq {pm_seq!r} != {seq!r}",
                        idx,
                    )

                pm_dpr = p_meta["dpr_ppm"]
                if not _is_exact_int(pm_dpr) or (dpr_valid and pm_dpr != dpr):
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "perception_dpr_mismatch",
                        "state._perception.dpr_ppm",
                        f"_perception.dpr_ppm {pm_dpr!r} != {dpr!r}",
                        idx,
                    )

                pm_sid = p_meta["screen_identity"]
                if not isinstance(pm_sid, str) or not _HEX16_RE.fullmatch(pm_sid):
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "invalid_screen_identity",
                        "state._perception.screen_identity",
                        f"screen_identity must be 16 lowercase hex chars, got {pm_sid!r}",
                        idx,
                    )
                else:
                    acc.screen_identities.append(pm_sid)

                pm_desc = p_meta["descriptor_hex"]
                if pm_desc is not None and (
                    not isinstance(pm_desc, str)
                    or len(pm_desc) == 0
                    or len(pm_desc) % 2 != 0
                    or not _HEX_EVEN_RE.fullmatch(pm_desc)
                ):
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "invalid_descriptor_hex",
                        "state._perception.descriptor_hex",
                        f"descriptor_hex must be None or even-length lowercase hex, got {pm_desc!r}",
                        idx,
                    )

                pm_regions = p_meta["regions"]
                if not isinstance(pm_regions, list):
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "invalid_regions_list",
                        "state._perception.regions",
                        "_perception.regions must be a list",
                        idx,
                    )

                # Verify screen_identity, descriptor_hex, regions, and fusion against frame & state
                p_frame_ref = getattr(perceived_obs, "frame", None)
                if p_frame_ref is not None and actual_obs_digest is not None:
                    try:
                        p_rgb = p_frame_ref.rgb
                        raw_bytes = (
                            p_rgb.tobytes(order="C")
                            if isinstance(p_rgb, np.ndarray)
                            else bytes(p_rgb)
                        )
                        cur_frame = Frame(p_frame_ref.width, p_frame_ref.height, raw_bytes)
                        expected_sid = engine.screen_identity(cur_frame)
                        if pm_sid != expected_sid:
                            if (
                                isinstance(pm_sid, str)
                                and pm_sid in identity_owners
                                and primary_key not in identity_owners[pm_sid]
                            ):
                                acc.add_issue(
                                    DIAG_FOREIGN,
                                    "foreign_screen_identity",
                                    "state._perception.screen_identity",
                                    f"screen_identity {pm_sid} belongs to a foreign sibling window",
                                    idx,
                                )
                            acc.add_issue(
                                DIAG_DISAGREEING,
                                "screen_identity_mismatch",
                                "state._perception.screen_identity",
                                f"screen_identity {pm_sid!r} != expected {expected_sid!r}",
                                idx,
                            )

                        if callable(getattr(engine, "screen_fingerprint", None)):
                            expected_desc = bytes(engine.screen_fingerprint(cur_frame)).hex()
                            if pm_desc != expected_desc:
                                acc.add_issue(
                                    DIAG_DISAGREEING,
                                    "descriptor_mismatch",
                                    "state._perception.descriptor_hex",
                                    f"descriptor_hex {pm_desc!r} != expected {expected_desc!r}",
                                    idx,
                                )

                        if inc_key is not None and isinstance(pm_regions, list):
                            prev_fr = incarnation_prev_frame.get(inc_key)
                            if prev_fr is None:
                                if pm_regions != []:
                                    acc.add_issue(
                                        DIAG_FOREIGN,
                                        "foreign_temporal_predecessor",
                                        "state._perception.regions",
                                        "initial frame in incarnation has non-empty diff regions (sibling temporal bleed)",
                                        idx,
                                    )
                                    acc.add_issue(
                                        DIAG_DISAGREEING,
                                        "regions_mismatch",
                                        "state._perception.regions",
                                        "initial frame regions must be empty list",
                                        idx,
                                    )
                            else:
                                expected_regs = engine.changed_regions(prev_fr, cur_frame)
                                if pm_regions != expected_regs:
                                    acc.add_issue(
                                        DIAG_DISAGREEING,
                                        "regions_mismatch",
                                        "state._perception.regions",
                                        "changed_regions output does not match target temporal sequence",
                                        idx,
                                    )
                            incarnation_prev_frame[inc_key] = cur_frame

                        if (
                            isinstance(p_state, dict)
                            and isinstance(p_state.get("nodes"), list)
                            and isinstance(pm_regions, list)
                            and epoch_valid
                            and dpr_valid
                        ):
                            base_state = _strip_perception_key(p_state)
                            node_ids = {
                                n.get("id")
                                for n in base_state.get("nodes", ())
                                if isinstance(n, dict)
                            }
                            pm_fusion = p_meta["fusion"]
                            if isinstance(pm_fusion, dict) and isinstance(
                                pm_fusion.get("hits"), list
                            ):
                                for hit in pm_fusion["hits"]:
                                    if isinstance(hit, dict) and hit.get("node") not in node_ids:
                                        acc.add_issue(
                                            DIAG_FOREIGN,
                                            "foreign_fusion_node",
                                            "state._perception.fusion",
                                            f"fusion hit references foreign node {hit.get('node')!r}",
                                            idx,
                                        )
                            target_dict = {
                                "kind": primary_key[0],
                                "canonical_id": primary_key[1],
                                "generation": primary_key[2],
                            }
                            expected_fusion = fuse_regions(
                                {
                                    "state": base_state,
                                    "handle": target_dict,
                                    "epoch": epoch,
                                    "dpr_ppm": dpr,
                                },
                                pm_regions,
                                dpr,
                                handle=acc.handle,
                                epoch=epoch,
                            )
                            if pm_fusion != expected_fusion:
                                acc.add_issue(
                                    DIAG_DISAGREEING,
                                    "fusion_mismatch",
                                    "state._perception.fusion",
                                    "fusion output disagrees with target-bound DPR/AX state fusion",
                                    idx,
                                )
                    except Exception as exc:
                        acc.add_issue(
                            DIAG_DISAGREEING,
                            "fusion_verification_error",
                            "state._perception",
                            f"perception verification raised {type(exc).__name__}: {exc}",
                            idx,
                        )

        # Check against SessionRegistry provenance if supplied
        if reg_snap is not None and acc.handle in reg_snap["active"] and epoch_valid:
            active_rec = reg_snap["active"][acc.handle]
            if epoch < active_rec.epoch:
                acc.add_issue(
                    DIAG_STALE,
                    "stale_epoch",
                    "epoch",
                    f"observation epoch {epoch} < registry active epoch {active_rec.epoch}",
                    idx,
                )
            elif epoch > active_rec.epoch:
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "epoch_mismatch",
                    "epoch",
                    f"observation epoch {epoch} > registry active epoch {active_rec.epoch}",
                    idx,
                )
            reg_inc = (acc.handle, epoch)
            if reg_inc not in reg_snap["last_seq"]:
                acc.add_issue(
                    DIAG_MISSING,
                    "missing_registry_provenance",
                    "registry",
                    f"no frame admitted in SessionRegistry for ({primary_key}, epoch={epoch})",
                    idx,
                )
            elif seq_valid:
                reg_seq = reg_snap["last_seq"][reg_inc]
                reg_dig = reg_snap["latest_digest"].get(reg_inc)
                if seq < reg_seq:
                    acc.add_issue(
                        DIAG_STALE,
                        "stale_sequence",
                        "frame_seq",
                        f"observation frame_seq {seq} < registry latest frame_seq {reg_seq}",
                        idx,
                    )
                elif seq > reg_seq:
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "sequence_mismatch",
                        "frame_seq",
                        f"observation frame_seq {seq} > registry latest frame_seq {reg_seq}",
                        idx,
                    )
                elif actual_obs_digest is not None and reg_dig is not None and actual_obs_digest != reg_dig:
                    acc.add_issue(
                        DIAG_DISAGREEING,
                        "registry_digest_mismatch",
                        "frame.digest",
                        "observation frame digest disagrees with SessionRegistry admitted digest",
                        idx,
                    )

        # Check explicit expected_* maps
        has_exp_ep, exp_ep = _lookup_expectation(norm_epochs, primary_key)
        if has_exp_ep and epoch_valid:
            if not _is_exact_int(exp_ep) or exp_ep < 0:
                raise AuditConfigError("expected_epochs values must be non-negative integers")
            if epoch < exp_ep:
                acc.add_issue(
                    DIAG_STALE,
                    "stale_epoch",
                    "epoch",
                    f"epoch {epoch} < expected epoch {exp_ep}",
                    idx,
                )
            elif epoch > exp_ep:
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "epoch_mismatch",
                    "epoch",
                    f"epoch {epoch} != expected epoch {exp_ep}",
                    idx,
                )

        has_exp_dpr, exp_dpr = _lookup_expectation(norm_dprs, primary_key)
        if has_exp_dpr and dpr_valid:
            if not _is_exact_int(exp_dpr) or exp_dpr <= 0:
                raise AuditConfigError("expected_dpr_ppm values must be positive integers")
            if dpr != exp_dpr:
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "expected_dpr_mismatch",
                    "dpr_ppm",
                    f"dpr_ppm {dpr} != expected dpr_ppm {exp_dpr}",
                    idx,
                )

        has_exp_seq, exp_seq = _lookup_expectation(norm_seqs, primary_key)
        if has_exp_seq and seq_valid:
            if not _is_exact_int(exp_seq) or exp_seq < 0:
                raise AuditConfigError("expected_sequences values must be non-negative integers")
            if seq < exp_seq:
                acc.add_issue(
                    DIAG_STALE,
                    "stale_sequence",
                    "frame_seq",
                    f"frame_seq {seq} < expected frame_seq {exp_seq}",
                    idx,
                )
            elif seq > exp_seq:
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "sequence_mismatch",
                    "frame_seq",
                    f"frame_seq {seq} != expected frame_seq {exp_seq}",
                    idx,
                )

        has_exp_dig, exp_dig = _lookup_expectation(norm_digests, primary_key)
        if has_exp_dig and actual_obs_digest is not None:
            if not isinstance(exp_dig, str) or not _HEX64_RE.fullmatch(exp_dig):
                raise AuditConfigError("expected_digests values must be 64-char lowercase hex")
            if actual_obs_digest != exp_dig:
                if exp_dig in digest_owners and primary_key not in digest_owners[exp_dig]:
                    acc.add_issue(
                        DIAG_FOREIGN,
                        "foreign_sibling_digest",
                        "frame.digest",
                        "frame digest disagrees with expected digest and matches foreign target",
                        idx,
                    )
                acc.add_issue(
                    DIAG_DISAGREEING,
                    "expected_digest_mismatch",
                    "frame.digest",
                    "frame digest does not match expected target digest",
                    idx,
                )

        if len(acc.issues) == initial_issue_count:
            acc.verified_items += 1

    # Check expected targets that had zero observations/results in the batch
    for exp_key in sorted(expected_target_keys):
        acc = get_acc(exp_key)
        if not acc.indices:
            acc.add_issue(
                DIAG_MISSING,
                "missing_target_evidence",
                "target",
                f"expected target {exp_key} has no observations or results in batch",
                -1,
            )

    ordered_keys = sorted(accumulators.keys())
    target_diags = tuple(accumulators[k].finalize() for k in ordered_keys)
    by_target = {k: d for k, d in zip(ordered_keys, target_diags)}
    authentic_count = sum(1 for d in target_diags if d.authentic)
    rejected_count = len(target_diags) - authentic_count
    batch_authentic = (
        len(target_diags) > 0
        and rejected_count == 0
        and len(unassigned_issues) == 0
    )

    return BatchAuditReport(
        authentic=batch_authentic,
        verdict=VERDICT_AUTHENTIC if batch_authentic else VERDICT_REJECTED,
        targets=target_diags,
        by_target=by_target,
        unassigned_issues=tuple(unassigned_issues),
        total_observations=total_observations,
        total_results=total_results,
        authentic_count=authentic_count,
        rejected_count=rejected_count,
    )


# @anchor:wa.perceive.multiwin_perception_audit.perceive_and_audit
def perceive_and_audit(
    observations,
    *,
    adapter=None,
    runner=None,
    registry=None,
    expected_targets=None,
    expected_epochs=None,
    expected_dpr_ppm=None,
    expected_sequences=None,
    expected_digests=None,
) -> tuple[list, BatchAuditReport]:
    """Run `perceive_many` on `observations` and audit `(observations, results)`."""
    obs_list = list(observations)
    results = perceive_many(obs_list, adapter=adapter, runner=runner)
    report = audit_multiwin_perception(
        obs_list,
        results,
        adapter=adapter,
        registry=registry,
        expected_targets=expected_targets,
        expected_epochs=expected_epochs,
        expected_dpr_ppm=expected_dpr_ppm,
        expected_sequences=expected_sequences,
        expected_digests=expected_digests,
        require_perception_metadata=True,
    )
    return results, report


# @anchor:wa.perceive.multiwin_perception_audit.MultiWindowPerceptionAuditor
class MultiWindowPerceptionAuditor:
    """Reusable multi-window perception consistency auditor."""

    def __init__(
        self,
        *,
        adapter=None,
        registry=None,
        expected_targets=None,
        expected_epochs=None,
        expected_dpr_ppm=None,
        expected_sequences=None,
        expected_digests=None,
        require_perception_metadata: bool = True,
    ) -> None:
        if type(require_perception_metadata) is not bool:
            raise AuditConfigError("require_perception_metadata must be a bool")
        self.adapter = adapter
        self.registry = registry
        self.expected_targets = expected_targets
        self.expected_epochs = expected_epochs
        self.expected_dpr_ppm = expected_dpr_ppm
        self.expected_sequences = expected_sequences
        self.expected_digests = expected_digests
        self.require_perception_metadata = require_perception_metadata

    def audit(
        self,
        observations=None,
        results=None,
        **overrides,
    ) -> BatchAuditReport:
        params = {
            "adapter": self.adapter,
            "registry": self.registry,
            "expected_targets": self.expected_targets,
            "expected_epochs": self.expected_epochs,
            "expected_dpr_ppm": self.expected_dpr_ppm,
            "expected_sequences": self.expected_sequences,
            "expected_digests": self.expected_digests,
            "require_perception_metadata": self.require_perception_metadata,
        }
        params.update(overrides)
        return audit_multiwin_perception(observations, results, **params)

    def perceive_and_audit(
        self,
        observations,
        *,
        runner=None,
        **overrides,
    ) -> tuple[list, BatchAuditReport]:
        params = {
            "adapter": self.adapter,
            "registry": self.registry,
            "expected_targets": self.expected_targets,
            "expected_epochs": self.expected_epochs,
            "expected_dpr_ppm": self.expected_dpr_ppm,
            "expected_sequences": self.expected_sequences,
            "expected_digests": self.expected_digests,
        }
        params.update(overrides)
        return perceive_and_audit(observations, runner=runner, **params)
