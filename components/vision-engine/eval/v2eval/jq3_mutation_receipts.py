"""Immutable vision mutation receipt records and pure receipt reducer (G-R1-2).

Binds every vision mutation receipt to:
- source_digest (64-char lowercase hex SHA-256 of source file bytes)
- qualified_ast_scope (e.g. 'BaselineFlickerVeto.update', never a bare ordinal)
- original_expression (unparsed AST source expression or anchor literal)
- structural_selector (AST structural path within the containing expression)
- transformation (explicit AST mutation operator description)
- positive_control_passed (strict bool result of running assertion on unmutated code)
- synthetic_rgb_digest (64-char lowercase hex SHA-256 of synthetic RGB frames)

Invariants enforced:
- INV-R1-1: a passing original assertion and exactly matched anchor precede every kill.
- INV-R1-2: import errors, failed positives and equivalent sites never enter the killed numerator.
- INV-R1-3: pure schema and reducer only; no shared mutation executor and no test-helper imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Iterable, Mapping, Sequence
import numpy as np


OUTCOME_KILLED = "killed"
OUTCOME_SURVIVED = "survived"
OUTCOME_EQUIVALENT = "equivalent"
OUTCOME_MISSING_ANCHOR = "missing-anchor"
OUTCOME_MISSING_ANCHOR_ALT = "missing_anchor"
OUTCOME_INFRASTRUCTURE = "infrastructure"

CANONICAL_OUTCOMES = (
    OUTCOME_KILLED,
    OUTCOME_SURVIVED,
    OUTCOME_EQUIVALENT,
    OUTCOME_MISSING_ANCHOR,
    OUTCOME_INFRASTRUCTURE,
)

VALID_OUTCOMES = frozenset(
    CANONICAL_OUTCOMES + (OUTCOME_MISSING_ANCHOR_ALT,)
)

LOCAL_REPOSITORY = "vision-engine"
HEX64_CHARS = frozenset("0123456789abcdef")


class MutationReceiptError(ValueError):
    """Base error for invalid mutation receipts or reduction violations."""


class UnboundDigestError(MutationReceiptError):
    """Raised when a source or synthetic RGB digest is malformed or unbound."""


class DuplicateMutationSiteError(MutationReceiptError):
    """Raised when duplicate mutation sites or mutant IDs appear in a batch."""


class InvalidKillReceiptError(MutationReceiptError):
    """Raised when a receipt violates INV-R1-1 or INV-R1-2 kill preconditions."""


def normalize_outcome(outcome: str) -> str:
    """Normalize outcome spelling while keeping all five categories distinct."""
    if not isinstance(outcome, str):
        raise MutationReceiptError("outcome must be a string")
    if outcome == OUTCOME_MISSING_ANCHOR_ALT:
        return OUTCOME_MISSING_ANCHOR
    if outcome not in VALID_OUTCOMES:
        raise MutationReceiptError(f"unsupported mutation outcome: {outcome!r}")
    return outcome


def validate_sha256_hex(value: object, field_name: str) -> str:
    """Validate that value is a 64-character lowercase hexadecimal SHA-256 digest."""
    if not isinstance(value, str) or len(value) != 64 or any(c not in HEX64_CHARS for c in value):
        raise UnboundDigestError(
            f"{field_name} must be a 64-character lowercase hex SHA-256 digest"
        )
    return value


def _validate_nonempty_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MutationReceiptError(f"{field_name} must be a non-empty string")
    return value


def _reject_bare_ordinal(value: str, field_name: str) -> None:
    stripped = value.strip()
    if stripped.isdigit() or (stripped.startswith("#") and stripped[1:].isdigit()):
        raise MutationReceiptError(
            f"{field_name} must be a structural or qualified identifier, not a bare ordinal"
        )


def sha256_bytes(payload: bytes) -> str:
    """Return the 64-character lowercase hex SHA-256 digest of raw bytes."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    return hashlib.sha256(bytes(payload)).hexdigest()


def source_digest_for_path(path: str | Path) -> str:
    """Compute the exact SHA-256 hex digest of a source file on disk."""
    p = Path(path)
    return sha256_bytes(p.read_bytes())


def synthetic_rgb_digest(frames: np.ndarray | Sequence[np.ndarray]) -> str:
    """Compute a deterministic SHA-256 digest over synthetic uint8 RGB frame(s).

    Accepts either a single HxWx3 uint8 ndarray or a non-empty sequence of
    HxWx3 uint8 ndarrays. Validates shape and dtype before hashing.
    """
    if isinstance(frames, np.ndarray):
        frame_list = [frames]
    elif isinstance(frames, (tuple, list)) and len(frames) > 0:
        frame_list = list(frames)
    else:
        raise UnboundDigestError("synthetic RGB frames must be a non-empty ndarray or sequence")

    hasher = hashlib.sha256()
    hasher.update(f"count={len(frame_list)};".encode("ascii"))
    for idx, frame in enumerate(frame_list):
        if (
            not isinstance(frame, np.ndarray)
            or frame.dtype != np.uint8
            or frame.ndim != 3
            or frame.shape[2] != 3
            or min(frame.shape[:2]) < 1
        ):
            raise UnboundDigestError(
                f"synthetic frame {idx} must be a non-empty HxWx3 uint8 ndarray"
            )
        contiguous = np.ascontiguousarray(frame, dtype=np.uint8)
        hasher.update(
            f"frame={idx}:{contiguous.shape[0]}x{contiguous.shape[1]}x{contiguous.shape[2]};".encode(
                "ascii"
            )
        )
        hasher.update(contiguous.tobytes(order="C"))
    return hasher.hexdigest()


@dataclass(frozen=True)
class AstSiteKey:
    """Canonical structural identity of an AST mutation site."""

    module_path: str
    qualified_ast_scope: str
    containing_expression: str
    original_expression: str
    structural_selector: str
    transformation: str

    def __post_init__(self) -> None:
        _validate_nonempty_str(self.module_path, "module_path")
        _validate_nonempty_str(self.qualified_ast_scope, "qualified_ast_scope")
        _validate_nonempty_str(self.containing_expression, "containing_expression")
        _validate_nonempty_str(self.original_expression, "original_expression")
        _validate_nonempty_str(self.structural_selector, "structural_selector")
        _validate_nonempty_str(self.transformation, "transformation")
        _reject_bare_ordinal(self.qualified_ast_scope, "qualified_ast_scope")
        _reject_bare_ordinal(self.structural_selector, "structural_selector")


@dataclass(frozen=True)
class VisionMutationReceipt:
    """Immutable, validated receipt for a single AST mutation trial."""

    mutant_id: str
    module_path: str
    category: str
    source_digest: str
    qualified_ast_scope: str
    original_expression: str
    structural_selector: str
    transformation: str
    positive_control_passed: bool
    synthetic_rgb_digest: str
    outcome: str
    containing_expression: str = ""
    anchor_matched: bool = True
    mutated_expression: str = ""
    lineno: int = 0
    declared: bool = True
    repository: str = LOCAL_REPOSITORY
    equivalent_justification: str | None = None
    failure_detail: str | None = None

    def __post_init__(self) -> None:
        _validate_nonempty_str(self.mutant_id, "mutant_id")
        _validate_nonempty_str(self.module_path, "module_path")
        _validate_nonempty_str(self.category, "category")
        validate_sha256_hex(self.source_digest, "source_digest")
        _validate_nonempty_str(self.qualified_ast_scope, "qualified_ast_scope")
        _reject_bare_ordinal(self.qualified_ast_scope, "qualified_ast_scope")
        _validate_nonempty_str(self.original_expression, "original_expression")
        _validate_nonempty_str(self.structural_selector, "structural_selector")
        _reject_bare_ordinal(self.structural_selector, "structural_selector")
        _validate_nonempty_str(self.transformation, "transformation")
        validate_sha256_hex(self.synthetic_rgb_digest, "synthetic_rgb_digest")
        _validate_nonempty_str(self.repository, "repository")

        if type(self.positive_control_passed) is not bool:
            raise MutationReceiptError("positive_control_passed must be a strict bool")
        if type(self.anchor_matched) is not bool:
            raise MutationReceiptError("anchor_matched must be a strict bool")
        if type(self.declared) is not bool:
            raise MutationReceiptError("declared must be a strict bool")
        if type(self.lineno) is not int or self.lineno < 0:
            raise MutationReceiptError("lineno must be a nonnegative int")

        if not self.containing_expression:
            object.__setattr__(self, "containing_expression", self.original_expression)
        else:
            _validate_nonempty_str(self.containing_expression, "containing_expression")

        canonical_outcome = normalize_outcome(self.outcome)
        object.__setattr__(self, "outcome", canonical_outcome)

        # INV-R1-1 & INV-R1-2: Enforce outcome consistency with control and anchor checks.
        if canonical_outcome == OUTCOME_KILLED:
            if not self.positive_control_passed:
                raise InvalidKillReceiptError(
                    "INV-R1-1/INV-R1-2: failed positive control cannot enter killed outcome"
                )
            if not self.anchor_matched:
                raise InvalidKillReceiptError(
                    "INV-R1-1: unmatched anchor cannot enter killed outcome"
                )
        elif canonical_outcome == OUTCOME_SURVIVED:
            if not self.positive_control_passed:
                raise MutationReceiptError(
                    "INV-R1-2: failed positive control must be recorded as infrastructure, not survived"
                )
            if not self.anchor_matched:
                raise MutationReceiptError(
                    "INV-R1-1: unmatched anchor must be recorded as missing-anchor, not survived"
                )
        elif canonical_outcome == OUTCOME_EQUIVALENT:
            if not self.positive_control_passed or not self.anchor_matched:
                raise MutationReceiptError(
                    "equivalent outcome requires passing positive control and matched anchor"
                )
            if (
                not isinstance(self.equivalent_justification, str)
                or not self.equivalent_justification.strip()
            ):
                raise MutationReceiptError(
                    "equivalent outcome requires a non-empty equivalent_justification"
                )
        elif canonical_outcome == OUTCOME_MISSING_ANCHOR:
            if self.anchor_matched:
                raise MutationReceiptError(
                    "missing-anchor outcome requires anchor_matched=False"
                )

    @property
    def site_key(self) -> AstSiteKey:
        """Return the structural AST site key for duplicate detection."""
        return AstSiteKey(
            module_path=self.module_path,
            qualified_ast_scope=self.qualified_ast_scope,
            containing_expression=self.containing_expression,
            original_expression=self.original_expression,
            structural_selector=self.structural_selector,
            transformation=self.transformation,
        )

    @property
    def anchor_key(self) -> tuple[str, str, str, str, str]:
        """Return the tuple key identifying the AST site and transformation."""
        return (
            self.module_path,
            self.qualified_ast_scope,
            self.containing_expression,
            self.structural_selector,
            self.transformation,
        )

    @property
    def is_killed(self) -> bool:
        return (
            self.outcome == OUTCOME_KILLED
            and self.positive_control_passed
            and self.anchor_matched
            and self.declared
            and self.repository == LOCAL_REPOSITORY
        )


def classify_trial_outcome(
    *,
    positive_control_passed: bool,
    anchor_matched: bool,
    mutant_exception: BaseException | None,
    is_equivalent: bool = False,
    compile_or_import_error: BaseException | None = None,
) -> str:
    """Classify trial observations into one of the five distinct outcomes.

    Enforces INV-R1-1 and INV-R1-2:
    - Unmatched or non-unique anchor -> 'missing-anchor'
    - Failed positive control -> 'infrastructure' (never 'killed' even if mutant raised AssertionError)
    - Compile/import error -> 'infrastructure'
    - Equivalent site with passing assertion -> 'equivalent'
    - AssertionError from mutant behavioral check -> 'killed'
    - No exception from mutant behavioral check -> 'survived'
    - Any other exception (TypeError, ValueError, RuntimeError, TimeoutError, etc.) -> 'infrastructure'
    """
    if type(positive_control_passed) is not bool or type(anchor_matched) is not bool:
        raise MutationReceiptError("control and anchor flags must be strict booleans")
    if not anchor_matched:
        return OUTCOME_MISSING_ANCHOR
    if not positive_control_passed:
        return OUTCOME_INFRASTRUCTURE
    if compile_or_import_error is not None:
        return OUTCOME_INFRASTRUCTURE
    if is_equivalent:
        if mutant_exception is None:
            return OUTCOME_EQUIVALENT
        return OUTCOME_INFRASTRUCTURE
    if mutant_exception is None:
        return OUTCOME_SURVIVED
    if isinstance(mutant_exception, AssertionError):
        return OUTCOME_KILLED
    return OUTCOME_INFRASTRUCTURE


@dataclass(frozen=True)
class ReceiptReductionReport:
    """Immutable aggregate report produced by the pure receipt reducer."""

    killed: int
    survived: int
    equivalent: int
    missing_anchor: int
    infrastructure: int
    total_receipts: int
    declared_non_equivalent_denominator: int
    evaluated_denominator: int
    killed_numerator: int
    mutation_score: float
    outcome_counts: Mapping[str, int]
    by_module: Mapping[str, Mapping[str, int]]
    receipts: tuple[VisionMutationReceipt, ...] = field(repr=False)

    @property
    def missing_anchor_count(self) -> int:
        return self.missing_anchor


def reduce_vision_receipts(
    receipts: Iterable[VisionMutationReceipt],
    *,
    expected_source_digests: Mapping[str, str] | None = None,
    bound_rgb_digests: Iterable[str] | None = None,
    declared_site_ids: Iterable[str] | None = None,
) -> ReceiptReductionReport:
    """Pure reducer over validated VisionMutationReceipt records (G-R1-2).

    Validates all bindings and rejects:
    - non-VisionMutationReceipt objects
    - undeclared sites (receipt.declared is False or not in declared_site_ids)
    - foreign repository receipts (receipt.repository != 'vision-engine')
    - unbound or mismatched source digests or synthetic RGB digests
    - duplicate mutant_ids or duplicate AST site keys
    """
    if expected_source_digests is not None:
        normalized_sources = {
            _validate_nonempty_str(k, "module_path"): validate_sha256_hex(v, f"source_digest[{k}]")
            for k, v in expected_source_digests.items()
        }
    else:
        normalized_sources = None

    if bound_rgb_digests is not None:
        allowed_rgb = frozenset(
            validate_sha256_hex(d, "bound_rgb_digest") for d in bound_rgb_digests
        )
        if not allowed_rgb:
            raise UnboundDigestError("bound_rgb_digests cannot be empty when specified")
    else:
        allowed_rgb = None

    if declared_site_ids is not None:
        allowed_sites = frozenset(
            _validate_nonempty_str(s, "declared_site_id") for s in declared_site_ids
        )
    else:
        allowed_sites = None

    validated_list: list[VisionMutationReceipt] = []
    seen_ids: set[str] = set()
    seen_sites: set[AstSiteKey] = set()
    inferred_module_digests: dict[str, str] = {}

    for item in receipts:
        if not isinstance(item, VisionMutationReceipt):
            raise MutationReceiptError("all reduced items must be VisionMutationReceipt instances")
        if item.repository != LOCAL_REPOSITORY:
            raise MutationReceiptError(
                f"foreign repository receipt {item.mutant_id!r} ({item.repository!r}) "
                "cannot enter local vision-engine reduction"
            )
        if not item.declared:
            raise MutationReceiptError(
                f"undeclared mutation site {item.mutant_id!r} cannot be aggregated"
            )
        if allowed_sites is not None and item.mutant_id not in allowed_sites:
            raise MutationReceiptError(
                f"mutant_id {item.mutant_id!r} is not in declared_site_ids"
            )

        if normalized_sources is not None:
            expected_digest = normalized_sources.get(item.module_path)
            if expected_digest is None:
                raise UnboundDigestError(
                    f"module_path {item.module_path!r} has no bound source digest"
                )
            if item.source_digest != expected_digest:
                raise UnboundDigestError(
                    f"source_digest mismatch for {item.module_path!r}: "
                    f"got {item.source_digest}, expected {expected_digest}"
                )
        else:
            prev_digest = inferred_module_digests.get(item.module_path)
            if prev_digest is not None and prev_digest != item.source_digest:
                raise UnboundDigestError(
                    f"inconsistent source_digest for {item.module_path!r} within reduction batch"
                )
            inferred_module_digests[item.module_path] = item.source_digest

        if allowed_rgb is not None and item.synthetic_rgb_digest not in allowed_rgb:
            raise UnboundDigestError(
                f"synthetic_rgb_digest {item.synthetic_rgb_digest} for {item.mutant_id!r} "
                "is not bound to any registered synthetic frame fixture"
            )

        if item.mutant_id in seen_ids:
            raise DuplicateMutationSiteError(f"duplicate mutant_id: {item.mutant_id!r}")
        if item.site_key in seen_sites:
            raise DuplicateMutationSiteError(
                f"duplicate AST mutation site in {item.module_path}:{item.qualified_ast_scope} "
                f"({item.structural_selector})"
            )

        seen_ids.add(item.mutant_id)
        seen_sites.add(item.site_key)
        validated_list.append(item)

    # Sort deterministically so reduction is strictly input-order-independent.
    ordered = tuple(
        sorted(
            validated_list,
            key=lambda r: (
                r.module_path,
                r.qualified_ast_scope,
                r.structural_selector,
                r.original_expression,
                r.transformation,
                r.mutant_id,
            ),
        )
    )

    counts = {
        OUTCOME_KILLED: 0,
        OUTCOME_SURVIVED: 0,
        OUTCOME_EQUIVALENT: 0,
        OUTCOME_MISSING_ANCHOR: 0,
        OUTCOME_INFRASTRUCTURE: 0,
    }
    by_mod: dict[str, dict[str, int]] = {}

    for r in ordered:
        mod_bucket = by_mod.setdefault(
            r.module_path,
            {
                OUTCOME_KILLED: 0,
                OUTCOME_SURVIVED: 0,
                OUTCOME_EQUIVALENT: 0,
                OUTCOME_MISSING_ANCHOR: 0,
                OUTCOME_INFRASTRUCTURE: 0,
                "declared_non_equivalent_denominator": 0,
                "killed_numerator": 0,
            },
        )
        # INV-R1-1 & INV-R1-2 double-guard inside reducer:
        # Only receipts with positive_control_passed=True, anchor_matched=True, and outcome=='killed'
        # may increment killed_numerator.
        if r.outcome == OUTCOME_KILLED:
            if not (r.positive_control_passed and r.anchor_matched):
                raise InvalidKillReceiptError(
                    f"INV-R1-1/INV-R1-2 violation on {r.mutant_id!r}"
                )
            counts[OUTCOME_KILLED] += 1
            mod_bucket[OUTCOME_KILLED] += 1
            mod_bucket["killed_numerator"] += 1
            mod_bucket["declared_non_equivalent_denominator"] += 1
        elif r.outcome == OUTCOME_SURVIVED:
            counts[OUTCOME_SURVIVED] += 1
            mod_bucket[OUTCOME_SURVIVED] += 1
            mod_bucket["declared_non_equivalent_denominator"] += 1
        elif r.outcome == OUTCOME_EQUIVALENT:
            counts[OUTCOME_EQUIVALENT] += 1
            mod_bucket[OUTCOME_EQUIVALENT] += 1
        elif r.outcome == OUTCOME_MISSING_ANCHOR:
            counts[OUTCOME_MISSING_ANCHOR] += 1
            mod_bucket[OUTCOME_MISSING_ANCHOR] += 1
            mod_bucket["declared_non_equivalent_denominator"] += 1
        elif r.outcome == OUTCOME_INFRASTRUCTURE:
            counts[OUTCOME_INFRASTRUCTURE] += 1
            mod_bucket[OUTCOME_INFRASTRUCTURE] += 1
            mod_bucket["declared_non_equivalent_denominator"] += 1

    killed = counts[OUTCOME_KILLED]
    survived = counts[OUTCOME_SURVIVED]
    equivalent = counts[OUTCOME_EQUIVALENT]
    missing_anchor = counts[OUTCOME_MISSING_ANCHOR]
    infrastructure = counts[OUTCOME_INFRASTRUCTURE]
    total = len(ordered)
    declared_non_equiv = killed + survived + missing_anchor + infrastructure
    evaluated = killed + survived
    score = (killed / declared_non_equiv) if declared_non_equiv > 0 else 0.0

    outcome_counts = {
        OUTCOME_KILLED: killed,
        OUTCOME_SURVIVED: survived,
        OUTCOME_EQUIVALENT: equivalent,
        OUTCOME_MISSING_ANCHOR: missing_anchor,
        OUTCOME_MISSING_ANCHOR_ALT: missing_anchor,
        OUTCOME_INFRASTRUCTURE: infrastructure,
    }

    return ReceiptReductionReport(
        killed=killed,
        survived=survived,
        equivalent=equivalent,
        missing_anchor=missing_anchor,
        infrastructure=infrastructure,
        total_receipts=total,
        declared_non_equivalent_denominator=declared_non_equiv,
        evaluated_denominator=evaluated,
        killed_numerator=killed,
        mutation_score=score,
        outcome_counts=outcome_counts,
        by_module={k: dict(v) for k, v in sorted(by_mod.items())},
        receipts=ordered,
    )
