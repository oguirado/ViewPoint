"""AST mutation adequacy gate and deterministic module/category reducer.

Reduces validated immutable VisionMutationReceipt records by module and category:
- Rejects duplicate anchors (DuplicateAnchorError)
- Rejects changed or unbound source/RGB digests (ChangedDigestError)
- Rejects unknown or unapproved exclusions (UnknownExclusionError)
- Rejects failed positive controls (FailedPositiveControlError)
- Preserves measured denominators across all declared non-equivalent local sites
- Produces deterministic, input-order-independent reductions
- Isolates foreign scheduler receipts as provenance fixtures only, never counting
  them as vision-engine local source kills.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Iterable, Mapping

from eval.v2eval.jq3_mutation_receipts import (
    LOCAL_REPOSITORY,
    OUTCOME_EQUIVALENT,
    OUTCOME_INFRASTRUCTURE,
    OUTCOME_KILLED,
    OUTCOME_MISSING_ANCHOR,
    OUTCOME_SURVIVED,
    MutationReceiptError,
    VisionMutationReceipt,
    validate_sha256_hex,
)


LOCAL_VISION_MODULE_PREFIXES = (
    "eval/",
    "visioncore/",
    "vadapt/",
    "vmem/",
    "tools/",
)


class AdequacyGateError(MutationReceiptError):
    """Base error raised when the AST mutation adequacy gate rejects an input."""


class DuplicateAnchorError(AdequacyGateError):
    """Raised when two receipts target the same AST anchor or mutant ID."""


class ChangedDigestError(AdequacyGateError):
    """Raised when a source or synthetic RGB digest changed or is inconsistent."""


class UnknownExclusionError(AdequacyGateError):
    """Raised when an equivalent exclusion is undeclared, unknown, or invalid."""


class FailedPositiveControlError(AdequacyGateError):
    """Raised when a receipt with a failed positive control enters the adequacy gate."""


class DenominatorTamperError(AdequacyGateError):
    """Raised when measured denominators do not match expected declared site counts."""


class ForeignReceiptError(AdequacyGateError):
    """Raised when a foreign scheduler receipt is disguised as a local source receipt."""


@dataclass(frozen=True)
class ForeignSchedulerReceipt:
    """Immutable provenance fixture record for a foreign scheduler mutation receipt."""

    mutant_id: str
    repository: str
    module_path: str
    category: str
    source_digest: str
    outcome: str
    scheduler_id: str = "foreign-scheduler"

    def __post_init__(self) -> None:
        if not isinstance(self.mutant_id, str) or not self.mutant_id.strip():
            raise AdequacyGateError("foreign mutant_id must be a non-empty string")
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise AdequacyGateError("foreign repository must be a non-empty string")
        if self.repository == LOCAL_REPOSITORY:
            raise ForeignReceiptError(
                "ForeignSchedulerReceipt cannot declare repository='vision-engine'"
            )
        if not isinstance(self.module_path, str) or not self.module_path.strip():
            raise AdequacyGateError("foreign module_path must be a non-empty string")
        if not isinstance(self.category, str) or not self.category.strip():
            raise AdequacyGateError("foreign category must be a non-empty string")
        validate_sha256_hex(self.source_digest, "foreign source_digest")
        if not isinstance(self.outcome, str) or not self.outcome.strip():
            raise AdequacyGateError("foreign outcome must be a non-empty string")
        if not isinstance(self.scheduler_id, str) or not self.scheduler_id.strip():
            raise AdequacyGateError("scheduler_id must be a non-empty string")


@dataclass(frozen=True)
class AdequacyBucket:
    """Immutable reduction summary for a module, category, or (module, category) slice."""

    key: str
    killed: int
    survived: int
    equivalent: int
    missing_anchor: int
    infrastructure: int
    total_sites: int
    measured_denominator: int
    killed_numerator: int
    adequacy_ratio: float
    mutant_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "killed": self.killed,
            "survived": self.survived,
            "equivalent": self.equivalent,
            "missing_anchor": self.missing_anchor,
            "infrastructure": self.infrastructure,
            "total_sites": self.total_sites,
            "measured_denominator": self.measured_denominator,
            "killed_numerator": self.killed_numerator,
            "adequacy_ratio": self.adequacy_ratio,
            "mutant_ids": list(self.mutant_ids),
        }


@dataclass(frozen=True)
class AdequacyGateReport:
    """Deterministic, immutable AST mutation adequacy gate report."""

    killed_numerator: int
    measured_denominator: int
    total_local_sites: int
    killed: int
    survived: int
    equivalent: int
    missing_anchor: int
    infrastructure: int
    adequacy_ratio: float
    gate_passed: bool
    by_module: Mapping[str, AdequacyBucket]
    by_category: Mapping[str, AdequacyBucket]
    by_module_category: Mapping[tuple[str, str], AdequacyBucket]
    local_receipts: tuple[VisionMutationReceipt, ...] = field(repr=False)
    foreign_provenance_receipts: tuple[VisionMutationReceipt | ForeignSchedulerReceipt, ...] = field(
        default=(), repr=False
    )
    foreign_provenance_count: int = 0
    report_digest: str = ""


def _is_local_vision_module_path(module_path: str) -> bool:
    if not isinstance(module_path, str) or not module_path.strip():
        return False
    norm = module_path.strip().lstrip("./")
    if ".." in norm.split("/"):
        return False
    return norm.startswith(LOCAL_VISION_MODULE_PREFIXES)


def _build_bucket(key: str, items: list[VisionMutationReceipt]) -> AdequacyBucket:
    ordered = sorted(items, key=lambda r: r.mutant_id)
    killed = sum(1 for r in ordered if r.outcome == OUTCOME_KILLED)
    survived = sum(1 for r in ordered if r.outcome == OUTCOME_SURVIVED)
    equivalent = sum(1 for r in ordered if r.outcome == OUTCOME_EQUIVALENT)
    missing_anchor = sum(1 for r in ordered if r.outcome == OUTCOME_MISSING_ANCHOR)
    infrastructure = sum(1 for r in ordered if r.outcome == OUTCOME_INFRASTRUCTURE)
    total_sites = len(ordered)
    measured_denom = killed + survived + missing_anchor + infrastructure
    ratio = (killed / measured_denom) if measured_denom > 0 else 0.0
    return AdequacyBucket(
        key=key,
        killed=killed,
        survived=survived,
        equivalent=equivalent,
        missing_anchor=missing_anchor,
        infrastructure=infrastructure,
        total_sites=total_sites,
        measured_denominator=measured_denom,
        killed_numerator=killed,
        adequacy_ratio=ratio,
        mutant_ids=tuple(r.mutant_id for r in ordered),
    )


def reduce_adequacy_receipts(
    receipts: Iterable[VisionMutationReceipt | ForeignSchedulerReceipt],
    *,
    expected_source_digests: Mapping[str, str] | None = None,
    bound_rgb_digests: Iterable[str] | None = None,
    known_exclusions: Mapping[str, str] | Iterable[str] | None = None,
    foreign_receipts: Iterable[VisionMutationReceipt | ForeignSchedulerReceipt] | None = None,
    expected_measured_denominator: int | None = None,
    expected_module_denominators: Mapping[str, int] | None = None,
    min_adequacy_ratio: float = 1.0,
    reject_foreign_in_local_stream: bool = False,
    require_all_known_exclusions_present: bool = False,
) -> AdequacyGateReport:
    """Validate and reduce mutation receipts by module and category.

    Enforces:
    - Reject failed positive controls (FailedPositiveControlError)
    - Reject duplicate anchors or mutant IDs (DuplicateAnchorError)
    - Reject changed or unbound source/RGB digests (ChangedDigestError)
    - Reject unknown or unapproved exclusions (UnknownExclusionError)
    - Preserve measured denominators (DenominatorTamperError)
    - Keep foreign scheduler receipts strictly as provenance fixtures, never
      entering vision-engine local source kills.
    """
    if expected_source_digests is not None:
        pinned_sources: dict[str, str] = {}
        for mod_path, digest in expected_source_digests.items():
            if not isinstance(mod_path, str) or not mod_path.strip():
                raise AdequacyGateError("expected_source_digests keys must be non-empty strings")
            try:
                pinned_sources[mod_path] = validate_sha256_hex(
                    digest, f"expected_source_digests[{mod_path}]"
                )
            except MutationReceiptError as exc:
                raise ChangedDigestError(str(exc)) from exc
    else:
        pinned_sources = {}

    if bound_rgb_digests is not None:
        try:
            pinned_rgb = frozenset(
                validate_sha256_hex(d, "bound_rgb_digest") for d in bound_rgb_digests
            )
        except MutationReceiptError as exc:
            raise ChangedDigestError(str(exc)) from exc
        if not pinned_rgb:
            raise ChangedDigestError("bound_rgb_digests cannot be empty when specified")
    else:
        pinned_rgb = None

    exclusion_map: dict[str, str | None] = {}
    if known_exclusions is not None:
        if isinstance(known_exclusions, Mapping):
            for k, v in known_exclusions.items():
                if not isinstance(k, str) or not k.strip():
                    raise UnknownExclusionError("exclusion mutant_id must be a non-empty string")
                if not isinstance(v, str) or not v.strip():
                    raise UnknownExclusionError(
                        f"exclusion {k!r} requires a non-empty justification"
                    )
                exclusion_map[k] = v.strip()
        elif isinstance(known_exclusions, Iterable) and not isinstance(
            known_exclusions, (str, bytes)
        ):
            for k in known_exclusions:
                if not isinstance(k, str) or not k.strip():
                    raise UnknownExclusionError("exclusion mutant_id must be a non-empty string")
                exclusion_map[k] = None
        else:
            raise UnknownExclusionError("known_exclusions must be a mapping or iterable of IDs")

    local_items: list[VisionMutationReceipt] = []
    foreign_items: list[VisionMutationReceipt | ForeignSchedulerReceipt] = []

    if foreign_receipts is not None:
        for f_item in foreign_receipts:
            if isinstance(f_item, ForeignSchedulerReceipt):
                foreign_items.append(f_item)
            elif isinstance(f_item, VisionMutationReceipt):
                if f_item.repository == LOCAL_REPOSITORY:
                    raise ForeignReceiptError(
                        f"receipt {f_item.mutant_id!r} in foreign_receipts claims local repository"
                    )
                foreign_items.append(f_item)
            else:
                raise AdequacyGateError(
                    "foreign_receipts must contain ForeignSchedulerReceipt or VisionMutationReceipt"
                )

    for item in receipts:
        if isinstance(item, ForeignSchedulerReceipt):
            if reject_foreign_in_local_stream:
                raise ForeignReceiptError(
                    f"foreign scheduler receipt {item.mutant_id!r} rejected in strict local stream"
                )
            foreign_items.append(item)
            continue
        if not isinstance(item, VisionMutationReceipt):
            raise AdequacyGateError(
                "all local receipts must be immutable VisionMutationReceipt instances"
            )
        if item.repository != LOCAL_REPOSITORY:
            if reject_foreign_in_local_stream:
                raise ForeignReceiptError(
                    f"foreign receipt {item.mutant_id!r} ({item.repository!r}) rejected in strict local stream"
                )
            foreign_items.append(item)
            continue

        # A receipt claiming local 'vision-engine' provenance must target a vision-engine module.
        if not _is_local_vision_module_path(item.module_path):
            raise ForeignReceiptError(
                f"disguised foreign module_path {item.module_path!r} on receipt {item.mutant_id!r}"
            )
        if not item.declared:
            raise UnknownExclusionError(
                f"undeclared local site {item.mutant_id!r} cannot be excluded or aggregated"
            )
        if not item.positive_control_passed:
            raise FailedPositiveControlError(
                f"failed positive control on receipt {item.mutant_id!r} rejected by adequacy gate"
            )

        local_items.append(item)

    seen_mutant_ids: set[str] = set()
    seen_anchors: set[tuple[str, str, str, str]] = set()
    seen_site_keys: set[object] = set()
    observed_module_digests: dict[str, str] = {}
    observed_equivalents: set[str] = set()

    # Sort local items deterministically before stateful checks so error messages and
    # reductions are completely independent of caller input ordering.
    canonical_local = tuple(
        sorted(
            local_items,
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

    for r in canonical_local:
        # 1. Duplicate anchor / site / ID check
        anchor_id = (
            r.module_path,
            r.qualified_ast_scope,
            r.containing_expression,
            r.structural_selector,
        )
        if r.mutant_id in seen_mutant_ids:
            raise DuplicateAnchorError(f"duplicate mutant_id: {r.mutant_id!r}")
        if r.site_key in seen_site_keys or anchor_id in seen_anchors:
            raise DuplicateAnchorError(
                f"duplicate AST anchor in {r.module_path}:{r.qualified_ast_scope} "
                f"at selector {r.structural_selector!r}"
            )
        seen_mutant_ids.add(r.mutant_id)
        seen_site_keys.add(r.site_key)
        seen_anchors.add(anchor_id)

        # 2. Digest verification (source digest and synthetic RGB digest)
        if expected_source_digests is not None:
            if r.module_path not in pinned_sources:
                raise ChangedDigestError(
                    f"module {r.module_path!r} is missing from expected_source_digests"
                )
            if r.source_digest != pinned_sources[r.module_path]:
                raise ChangedDigestError(
                    f"source_digest changed for {r.module_path!r}: "
                    f"got {r.source_digest}, expected {pinned_sources[r.module_path]}"
                )
        prev_mod_digest = observed_module_digests.get(r.module_path)
        if prev_mod_digest is not None and prev_mod_digest != r.source_digest:
            raise ChangedDigestError(
                f"inconsistent source_digest across receipts for {r.module_path!r}"
            )
        observed_module_digests[r.module_path] = r.source_digest

        if pinned_rgb is not None and r.synthetic_rgb_digest not in pinned_rgb:
            raise ChangedDigestError(
                f"unbound or changed synthetic_rgb_digest on {r.mutant_id!r}: {r.synthetic_rgb_digest}"
            )

        # 3. Equivalent exclusion verification
        if r.outcome == OUTCOME_EQUIVALENT:
            if r.mutant_id not in exclusion_map:
                raise UnknownExclusionError(
                    f"equivalent site {r.mutant_id!r} is not registered in known_exclusions"
                )
            expected_just = exclusion_map[r.mutant_id]
            if (
                expected_just is not None
                and (r.equivalent_justification or "").strip() != expected_just
            ):
                raise UnknownExclusionError(
                    f"equivalent_justification mismatch for {r.mutant_id!r}"
                )
            observed_equivalents.add(r.mutant_id)
        elif r.mutant_id in exclusion_map:
            raise UnknownExclusionError(
                f"site {r.mutant_id!r} is listed in known_exclusions but has outcome {r.outcome!r}"
            )

    if require_all_known_exclusions_present:
        missing_exclusions = set(exclusion_map) - observed_equivalents
        if missing_exclusions:
            raise UnknownExclusionError(
                f"unknown exclusion IDs not present in receipts: {sorted(missing_exclusions)!r}"
            )
    else:
        unknown_exclusion_ids = set(exclusion_map) - seen_mutant_ids
        if unknown_exclusion_ids:
            raise UnknownExclusionError(
                f"known_exclusions references unknown mutant IDs: {sorted(unknown_exclusion_ids)!r}"
            )

    # Sort foreign provenance fixtures deterministically and ensure no duplicate foreign IDs
    seen_foreign_ids: set[str] = set()
    canonical_foreign = tuple(
        sorted(
            foreign_items,
            key=lambda f: (f.repository, f.module_path, f.category, f.mutant_id),
        )
    )
    for f_rec in canonical_foreign:
        if f_rec.mutant_id in seen_foreign_ids or f_rec.mutant_id in seen_mutant_ids:
            raise DuplicateAnchorError(
                f"duplicate foreign/local mutant_id: {f_rec.mutant_id!r}"
            )
        seen_foreign_ids.add(f_rec.mutant_id)

    # Group local receipts by module, category, and (module, category)
    mod_groups: dict[str, list[VisionMutationReceipt]] = {}
    cat_groups: dict[str, list[VisionMutationReceipt]] = {}
    mod_cat_groups: dict[tuple[str, str], list[VisionMutationReceipt]] = {}

    for r in canonical_local:
        mod_groups.setdefault(r.module_path, []).append(r)
        cat_groups.setdefault(r.category, []).append(r)
        mod_cat_groups.setdefault((r.module_path, r.category), []).append(r)

    by_module = {
        mod: _build_bucket(mod, mod_groups[mod]) for mod in sorted(mod_groups)
    }
    by_category = {
        cat: _build_bucket(cat, cat_groups[cat]) for cat in sorted(cat_groups)
    }
    by_module_category = {
        mc: _build_bucket(f"{mc[0]}::{mc[1]}", mod_cat_groups[mc])
        for mc in sorted(mod_cat_groups)
    }

    overall = _build_bucket("ALL", list(canonical_local))

    if (
        expected_measured_denominator is not None
        and overall.measured_denominator != expected_measured_denominator
    ):
        raise DenominatorTamperError(
            f"measured_denominator {overall.measured_denominator} != "
            f"expected {expected_measured_denominator}"
        )

    if expected_module_denominators is not None:
        for mod, exp_denom in expected_module_denominators.items():
            actual_denom = by_module[mod].measured_denominator if mod in by_module else 0
            if actual_denom != exp_denom:
                raise DenominatorTamperError(
                    f"measured_denominator for {mod!r} is {actual_denom}, expected {exp_denom}"
                )

    gate_passed = (
        overall.measured_denominator > 0
        and overall.missing_anchor == 0
        and overall.infrastructure == 0
        and overall.survived == 0
        and overall.adequacy_ratio >= min_adequacy_ratio
    )

    canonical_payload = {
        "killed_numerator": overall.killed_numerator,
        "measured_denominator": overall.measured_denominator,
        "total_local_sites": overall.total_sites,
        "killed": overall.killed,
        "survived": overall.survived,
        "equivalent": overall.equivalent,
        "missing_anchor": overall.missing_anchor,
        "infrastructure": overall.infrastructure,
        "by_module": {k: v.to_dict() for k, v in by_module.items()},
        "by_category": {k: v.to_dict() for k, v in by_category.items()},
        "foreign_provenance_ids": [f.mutant_id for f in canonical_foreign],
    }
    digest = hashlib.sha256(
        json.dumps(canonical_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return AdequacyGateReport(
        killed_numerator=overall.killed_numerator,
        measured_denominator=overall.measured_denominator,
        total_local_sites=overall.total_sites,
        killed=overall.killed,
        survived=overall.survived,
        equivalent=overall.equivalent,
        missing_anchor=overall.missing_anchor,
        infrastructure=overall.infrastructure,
        adequacy_ratio=overall.adequacy_ratio,
        gate_passed=gate_passed,
        by_module=by_module,
        by_category=by_category,
        by_module_category=by_module_category,
        local_receipts=canonical_local,
        foreign_provenance_receipts=canonical_foreign,
        foreign_provenance_count=len(canonical_foreign),
        report_digest=digest,
    )


evaluate_ast_mutation_adequacy = reduce_adequacy_receipts
