# SPDX-License-Identifier: AGPL-3.0-only
"""Inputs supplied by the host after resolving and verifying retained references."""

import re
from dataclasses import dataclass


DIGEST = re.compile(r'[0-9a-f]{64}')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}')


class Rejected(ValueError):
    """An evidence gate that leaves the outcome unknown."""

    def __init__(self, reason, detail, *, finding=None):
        super().__init__(detail)
        self.reason = reason
        self.finding = finding


@dataclass(frozen=True, kw_only=True)
class TimelinePolicy:
    kinds: frozenset[str]
    required_kinds: frozenset[str]
    late_tolerance_ms: int
    integer_milliseconds: bool

    def receipt_fields(self):
        return {
            'late_tolerance_ms': self.late_tolerance_ms,
            'late_bound_inclusive': True,
            'early_tolerance_ms': 0,
            'equal_time_precedence': 'none',
        }


@dataclass(frozen=True, kw_only=True)
class SpatialPolicy:
    decoder_ref: str
    max_mismatch_ppm: int
    max_channel_delta: int
    region: tuple[int, int, int, int] | None
    frame_indices: tuple[int, ...] | None


@dataclass(frozen=True, kw_only=True)
class Policy:
    id: str
    version: str
    sha256: str
    comparator_id: str
    comparator_version: str
    checks: tuple[str, ...]
    determinism_modes: frozenset[str]
    repeatable_effects_required: bool
    contrast_ids: tuple[str, ...]
    negative_control_ids: tuple[str, ...]
    timeline: TimelinePolicy | None = None
    spatial: SpatialPolicy | None = None
    waiver_source: str | None = None
    waiver_scope: str | None = None

    def pin(self):
        return {'id': self.id, 'version': self.version, 'sha256': self.sha256}


@dataclass(frozen=True, kw_only=True)
class Timeline:
    record: dict
    clock_verified: bool
    complete: bool
    ordinal_prefix_verified: bool
    clock_domain: str | None = None
    common_anchor: str | None = None


@dataclass(frozen=True, kw_only=True)
class Media:
    recording_ref: str
    record: dict


@dataclass(frozen=True, kw_only=True)
class Behaviour:
    cases: tuple[dict, ...]
    complete: bool


@dataclass(frozen=True, kw_only=True)
class Episode:
    tape_sha256: str
    timeline: Timeline | None = None
    media: Media | None = None
    behaviour: Behaviour | None = None
    episode_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class Comparison:
    """Verified evidence, policy and attempt identity from the host resolver.

    A shared clock anchor certifies a common elapsed-time coordinate system.
    The resolver supplies the ID of the attempt opened before dispatch. A
    synthetic invocation may omit it and retain an artifact without a receipt.
    """

    policy: Policy
    reference: Episode
    candidate: Episode
    exchange_valid: bool
    evidence_bound: bool
    custody_verified: bool
    lineage_verified: bool
    fresh: bool
    determinism_mode: str
    effects_repeatable: bool
    perception_ceiling: str
    vision_authorized: bool
    producer: dict
    oracle_independent: bool
    contrast_outcomes: dict[str, bool]
    negative_controls: tuple[dict, ...]
    waivers: tuple[dict, ...] = ()
    control_reason: str | None = None
    slot_result: dict | None = None
    slot_reported_outcome: str | None = None
    attempt_id: str | None = None
    request_sha256: str | None = None
    reference_attempt_id: str | None = None
    candidate_attempt_id: str | None = None


@dataclass(frozen=True)
class Finding:
    check_id: str
    kind: str
    outcome: str
    detail: str
    reason: str | None = None
    divergence: dict | None = None
    diagnostics: dict | None = None

    def receipt_fields(self):
        check = {
            'check_id': self.check_id, 'kind': self.kind,
            'outcome': self.outcome, 'detail': self.detail[:2000],
        }
        if self.divergence is not None:
            check['first_divergence'] = self.divergence
        return check


def unsigned(value):
    return type(value) is int and value >= 0


def is_digest(value):
    return type(value) is str and DIGEST.fullmatch(value) is not None


def is_identifier(value):
    return type(value) is str and IDENTIFIER.fullmatch(value) is not None


def check_time_base(base):
    if (type(base) is not dict or base.keys() != {'num', 'den'}
            or any(type(item) is not int or item <= 0 for item in base.values())):
        raise Rejected('missing_clock_binding', 'The time base must have positive integer terms.')
    return base['num'], base['den']


def exact_ms(time):
    return None if time is None else [time.numerator, time.denominator]


def divergence_times(reference, candidate):
    result = {}
    if reference is not None:
        result['reference_t_ms'] = reference.numerator // reference.denominator
    if candidate is not None:
        result['candidate_t_ms'] = candidate.numerator // candidate.denominator
    if reference is not None and candidate is not None:
        delta = candidate - reference
        if delta.denominator == 1:
            result['delta_ms'] = delta.numerator
        else:
            result['note'] = f'Exact candidate-minus-reference delta is {delta} ms.'
    return result
