# @module {"id":"wa.learn.script_metrics","layer":2,"owner":"LEARN","deps":["wa.learn.script_policy"],"book":"HB-WA-s4-learn"}
"""Exact bounded aggregates; missing provider evidence remains missing.

Origin: scripts_mesh/build/spec/s9_w.md. These metrics confer no authority.
"""
from dataclasses import dataclass as _dataclass
from fractions import Fraction as _Fraction
from .script_policy import expected_cost as _expected_cost
from .script_policy import _Immutable


def _need(value, code):
    if value is not True:
        raise ValueError(code)


# @anchor:wa.learn.script_metrics.CostEvidence
@_dataclass(frozen=True, slots=True, repr=False)
class CostEvidence(_Immutable):
    key: tuple
    unit: str
    numerator: int | None
    denominator: int | None
    provenance: str | None
    reason: str | None

    # @anchor:wa.learn.script_metrics.CostEvidence.value
    def value(self):
        if self.numerator is None or self.denominator is None or self.provenance is None:
            _need(bool(self.reason), 'METRIC_MISSING_REASON')
            return None
        _need(len(self.key) == 6 and bool(self.unit) and bool(self.provenance)
              and type(self.numerator) is int and self.numerator >= 0
              and type(self.denominator) is int and self.denominator > 0, 'METRIC_COUNTER')
        return _Fraction(self.numerator, self.denominator)


# @anchor:wa.learn.script_metrics.ProviderUsage
@_dataclass(frozen=True, slots=True, repr=False)
class ProviderUsage(_Immutable):
    key: tuple
    provider_call: bool
    prefix_equal: bool | None
    prefix_eligible: bool | None
    input_tokens: int | None
    cached_input_tokens: int | None
    provenance: str | None
    reason: str | None


# @anchor:wa.learn.script_metrics.RequestTiming
@_dataclass(frozen=True, slots=True, repr=False)
class RequestTiming(_Immutable):
    key: tuple
    arrival_ns: int | None
    queue_end_ns: int | None
    startup_end_ns: int | None
    acquisition_end_ns: int | None
    send_end_ns: int | None
    settle_end_ns: int | None
    seal_ns: int | None
    deadline_ns: int
    censored: bool
    provenance: str | None
    reason: str | None


# @anchor:wa.learn.script_metrics.GateResult
@_dataclass(frozen=True, slots=True, repr=False)
class GateResult(_Immutable):
    gate: str
    fixture: str
    expected: str
    actual: str
    reason: str
    observed_inputs: int
    outstanding_owners: tuple
    provenance: str


# @anchor:wa.learn.script_metrics.cache_metrics
def cache_metrics(rows, *, key, max_rows):
    _need(type(max_rows) is int and max_rows > 0, 'METRIC_BOUND')
    calls = equal = eligible = cached = tokens = 0
    missing = False
    equal_missing = eligible_missing = False
    for index, row in enumerate(rows):
        _need(index < max_rows and type(row) is ProviderUsage and row.key == key, 'METRIC_COHORT')
        _need(type(row.provider_call) is bool, 'METRIC_CALL')
        if not row.provider_call:
            _need(row.input_tokens is None and row.cached_input_tokens is None, 'SCRIPT_PROVIDER_USAGE')
            continue
        calls += 1
        _need(row.prefix_equal is None or type(row.prefix_equal) is bool, 'PREFIX_EVIDENCE')
        _need(row.prefix_eligible is None or type(row.prefix_eligible) is bool, 'PREFIX_EVIDENCE')
        equal_missing |= row.prefix_equal is None
        eligible_missing |= row.prefix_eligible is None
        equal += row.prefix_equal is True
        eligible += row.prefix_eligible is True
        if (row.input_tokens is None or row.cached_input_tokens is None or row.provenance is None):
            _need(bool(row.reason), 'METRIC_MISSING_REASON')
            missing = True
            continue
        _need(type(row.input_tokens) is int and type(row.cached_input_tokens) is int
              and 0 <= row.cached_input_tokens <= row.input_tokens and bool(row.provenance), 'METRIC_COUNTER')
        cached += row.cached_input_tokens
        tokens += row.input_tokens
    return {'provider_calls': calls, 'prefix_equal_calls': None if equal_missing else equal,
            'eligible_prefix_calls': None if eligible_missing else eligible,
            'prefix_reason': 'PREFIX_MISSING' if equal_missing or eligible_missing else None,
            'provider_cached_fraction': None if missing or not tokens else _Fraction(cached, tokens),
            'reason': 'USAGE_MISSING' if missing else 'NO_INPUT_TOKENS' if not tokens else None}


# @anchor:wa.learn.script_metrics.latency_metrics
def latency_metrics(rows, *, key, max_rows):
    _need(type(max_rows) is int and max_rows > 0, 'METRIC_BOUND')
    count = misses = censored = 0
    total = 0
    missing = False
    for index, row in enumerate(rows):
        _need(index < max_rows and type(row) is RequestTiming and row.key == key, 'METRIC_COHORT')
        count += 1
        times = (row.arrival_ns, row.queue_end_ns, row.startup_end_ns, row.acquisition_end_ns,
                 row.send_end_ns, row.settle_end_ns, row.seal_ns)
        known = tuple(t for t in times if t is not None)
        _need(all(type(t) is int and t >= 0 for t in known)
              and all(a <= b for a, b in zip(known, known[1:]))
              and type(row.deadline_ns) is int and row.deadline_ns >= 0, 'METRIC_TIMING')
        if row.censored:
            censored += 1
        if None in times or row.provenance is None or row.censored:
            _need(bool(row.reason), 'METRIC_MISSING_REASON')
            missing = True
        else:
            total += row.seal_ns - row.arrival_ns
        if row.seal_ns is not None and row.seal_ns > row.deadline_ns:
            misses += 1
    return {'requests': count, 'censored': censored, 'deadline_misses': misses,
            'deadline_denominator': count,
            'mean_arrival_to_seal_ns': None if missing or not count else _Fraction(total, count),
            'reason': 'TIMING_INCOMPLETE' if missing else 'NO_REQUESTS' if not count else None}


# @anchor:wa.learn.script_metrics.compare_routes
def compare_routes(routes, *, eligible, pair_key):
    """Comparable safe candidates only; unknown cost cannot win as zero."""
    ranked = []
    seen = set()
    for lane, model, settings in routes:
        _need(lane not in seen, 'DUPLICATE_ROUTE')
        seen.add(lane)
        if lane not in eligible:
            continue
        _need(settings == pair_key, 'UNPAIRED_ROUTE')
        if ranked:
            _need(model.metric == ranked[0][2], 'INCOMPATIBLE_METRIC')
        cost = _expected_cost(model)
        if cost is not None:
            ranked.append((cost, lane, model.metric))
    return tuple((lane, cost) for cost, lane, _ in sorted(ranked))
