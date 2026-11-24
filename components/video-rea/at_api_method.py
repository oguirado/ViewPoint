# SPDX-License-Identifier: AGPL-3.0-only
"""In-memory attribution of authored AT and API fixture observations."""

from collections import Counter
from collections.abc import Iterable
from math import isfinite
from types import GeneratorType


_SOURCES = ("at", "api")
_STATUSES = ("observed", "absent", "denied")
_MISSING = object()
_METADATA_FIELDS = ("episode_id", "action_id", "source", "signal", "status")
_ROW_FIELDS = {*_METADATA_FIELDS, "value"}
_OBSERVATION_COLLECTIONS = (list, tuple, GeneratorType, type(iter(())), type(iter([])))


def _require_id(value: object, field: str) -> None:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field} must be a nonempty trimmed string")


def _value_key(value: object) -> tuple[type, object]:
    if type(value) not in (str, bool, int, float, type(None)):
        raise ValueError("observed value must be a JSON scalar")
    if type(value) is float and not isfinite(value):
        raise ValueError("observed number must be finite")
    return type(value), value


def _metadata(row: dict[str, object]) -> tuple[object, ...]:
    return tuple(row.get(field) for field in _METADATA_FIELDS)


def _has_unknown_keys(row):
    return any(type(key) is not str or key not in _ROW_FIELDS for key in row)


def _check_metadata(row, expected):
    if _has_unknown_keys(row) or _metadata(row) != expected:
        raise ValueError("observation metadata changed before payload access")


def check_attribution(
    *,
    episode_id: str,
    action_id: str,
    expected_signals: Iterable[str],
    observations: Iterable[dict[str, object]],
    available_sources: Iterable[str] = _SOURCES,
) -> dict[str, object]:
    """Check observations for one authored action occurrence.

    Each plain dict row declares episode_id, action_id, source (at or api), signal, and
    status (observed, absent, or denied). An observed row has a scalar value,
    which may explicitly be null. Expected signal names define coverage, so
    omitted rows and explicit absence both produce an absent source status.

    Denial applies to its source and signal for this occurrence and takes
    precedence over rows from that source. Payloads for those rows are ignored.
    Sources that supply conflicting permitted values yield conflict. A signal
    with matching permitted values yields observed. An explicit absence and an
    observation from the same source conflict. Without permitted values,
    the result is denied if either source was denied, or absent otherwise.
    The report carries input row indexes for permitted witnesses and includes
    a value only where the permitted witnesses agree.

    An explicitly unbound source is reported as unavailable. Conflicts fail
    the fixture; missing evidence makes it incomplete. These results do not
    certify a live source connection.

    Raise ValueError for malformed metadata, out-of-scope rows, and malformed
    permitted payloads.
    """
    _require_id(episode_id, "episode_id")
    _require_id(action_id, "action_id")
    if type(available_sources) not in (list, tuple):
        raise ValueError("available_sources must be a collection of source names")
    availability = set()
    for source in available_sources:
        if type(source) is not str or source not in _SOURCES or source in availability:
            raise ValueError("invalid or duplicate available source")
        availability.add(source)
    if type(expected_signals) not in (list, tuple):
        raise ValueError("expected_signals must be a collection of names")
    signals = {}
    for signal in expected_signals:
        _require_id(signal, "expected signal")
        if signal in signals:
            raise ValueError("duplicate expected signal")
        signals[signal] = {source: [] for source in _SOURCES}
    if not signals:
        raise ValueError("at least one expected signal is required")

    if type(observations) not in _OBSERVATION_COLLECTIONS:
        raise ValueError("observations must be a collection of metadata records")

    witnesses = []
    denied = set()
    absent_indexes = {}
    for index, row in enumerate(observations):
        if type(row) is not dict:
            raise ValueError("each observation must be a plain dict")
        if _has_unknown_keys(row):
            raise ValueError("unexpected observation field")
        metadata = _metadata(row)
        row_episode_id, row_action_id, source, signal, status = metadata
        _require_id(row_episode_id, "observation episode_id")
        _require_id(row_action_id, "observation action_id")
        if (row_episode_id, row_action_id) != (episode_id, action_id):
            raise ValueError("observation belongs to a different episode or action")
        if type(source) is not str or source not in _SOURCES:
            raise ValueError("unknown observation source")
        if source not in availability:
            raise ValueError("observation from an unavailable source")
        if type(signal) is not str or signal not in signals:
            raise ValueError("unexpected observation signal")
        if type(status) is not str or status not in _STATUSES:
            raise ValueError("unknown observation status")
        witnesses.append((row, metadata))
        if status == "denied":
            denied.add((signal, source))
        elif status == "observed":
            signals[signal][source].append((index, row))
        else:
            absent_indexes.setdefault((signal, source), []).append(index)

    for row, metadata in witnesses:
        _check_metadata(row, metadata)

    results = {}
    for signal, source_rows in signals.items():
        sources = {}
        values = {}
        explicit_conflict = False
        for source, rows in source_rows.items():
            if source not in availability:
                sources[source] = {"status": "unavailable"}
                continue
            if (signal, source) in denied:
                sources[source] = {"status": "denied"}
                continue
            if not rows:
                sources[source] = {"status": "absent"}
                continue
            source_values = {}
            expected = (episode_id, action_id, source, signal, "observed")
            for _, row in rows:
                _check_metadata(row, expected)
                value = row.get("value", _MISSING)
                _check_metadata(row, expected)
                if value is _MISSING:
                    raise ValueError("observed row is missing value")
                source_values.setdefault(_value_key(value), value)
            absence = absent_indexes.get((signal, source))
            if absence:
                explicit_conflict = True
                sources[source] = {
                    "status": "conflict",
                    "observation_indexes": sorted(absence + [index for index, _ in rows]),
                }
                continue
            for key, value in source_values.items():
                values.setdefault(key, value)
            sources[source] = {
                "status": "observed" if len(source_values) == 1 else "conflict",
                "observation_indexes": [index for index, _ in rows],
            }

        result = {"sources": sources}
        if explicit_conflict or len(values) > 1:
            result["status"] = "conflict"
        elif values:
            result.update(status="observed", value=next(iter(values.values())))
        else:
            result["status"] = (
                "denied" if any(source["status"] == "denied" for source in sources.values())
                else "absent"
            )
        results[signal] = result

    for row, metadata in witnesses:
        _check_metadata(row, metadata)
    counts = Counter(signal["status"] for signal in results.values())
    if counts["conflict"]:
        verdict = "fail"
    elif counts["observed"] == len(results):
        verdict = "pass"
    else:
        verdict = "incomplete"
    return {
        "schema": "rea.at_api_attribution/v1",
        "episode_id": episode_id,
        "action_id": action_id,
        "verdict": verdict,
        "coverage": {
            "expected_count": len(results),
            "observed_count": counts["observed"],
            "absent_count": counts["absent"],
            "denied_count": counts["denied"],
            "conflict_count": counts["conflict"],
        },
        "signals": results,
    }
