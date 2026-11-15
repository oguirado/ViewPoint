# @module {"id":"wa.state.holdings_snapshot","layer":0,"owner":"STATE","deps":["wa.state.schema","wa.state.diff"],"book":"HB-WA-a0-arch"}
"""Volatile, validated Holdings episode snapshots and bounded executed-tick evidence.

The core has no backend imports. Its only durable representation is a strict,
source-text-free metadata trail; full STATE and Godot sidecars stay in RAM.
"""
from dataclasses import dataclass, fields
import hashlib
import json
import re
from collections.abc import Mapping

from wender.state import schema
from wender.state.diff import diff_states

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX16 = re.compile(r"[0-9a-f]{16}\Z")
_SIDECAR_SCHEMA = "holdings.godot.uitree/v1"
_POLICIES = {"godot": "godot-path", "cdp": "cdp-fingerprint"}


class SnapshotError(ValueError):
    """AUSENTE: missing, unverifiable, stale or inconsistent snapshot evidence."""


def _fail(reason):
    raise SnapshotError("AUSENTE: " + reason)


def _int(value, label, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        _fail(label + " must be a configured non-negative integer" + (" > 0" if positive else ""))
    return value


def _label(value, label):
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(label + " must be a nonempty identifier")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        _fail(label + " contains invalid UTF-8")
    return value


def _hash(value, label, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        _fail(label + " must be a lowercase SHA-256 or explicit null")
    return value


def _sha(text):
    return hashlib.sha256(text.encode("utf-8", "strict")).hexdigest()


def _canonical(value, label):
    try:
        return schema.dumps(value)
    except (TypeError, ValueError, UnicodeError, OverflowError) as exc:
        _fail(label + " is not canonical integer JSON: " + str(exc))


def _result_errors(callback, *values):
    result = callback(*values)
    if result is None or result is True or result == [] or result == ():
        return
    if result is False:
        _fail("injected validator rejected input")
    if isinstance(result, (list, tuple)) and result:
        _fail("injected validator: " + "; ".join(str(x) for x in result))
    _fail("injected validator returned an unsupported result")


@dataclass(frozen=True)
class FrozenSnapshot:
    """Immutable internal canonical JSON; .base/.tree return independent RAM copies."""
    _base_json: str
    _sidecar_json: str | None
    base_sha256: str
    ax_stable_sha256: str
    simulation_state_sha256: str | None
    sidecar_sha256: str | None
    presentation_sha256: str | None
    identity_policy: str
    source_tick: int
    cadence: str
    screen_id: str
    binding_ref: str
    entity_id: str

    @property
    def base(self):
        return json.loads(self._base_json)

    @property
    def tree(self):
        return None if self._sidecar_json is None else json.loads(self._sidecar_json)

    def metadata(self):
        """Only approved wrapper fields; no node text, raw receipts or attributes."""
        return {k: getattr(self, k) for k in (
            "base_sha256", "ax_stable_sha256", "simulation_state_sha256",
            "sidecar_sha256", "presentation_sha256", "identity_policy",
            "source_tick", "cadence", "screen_id", "binding_ref")}


def _check_freshness(tree, binding, tick, screen):
    limits = binding.get("cadence_limits")
    queries = binding.get("query_bindings")
    prior = binding.get("prior_daily_close")
    if not isinstance(limits, Mapping) or not isinstance(queries, Mapping) or not isinstance(prior, Mapping):
        _fail("configured cadence_limits, query_bindings and prior_daily_close mappings required")
    for cadence, max_age in limits.items():
        _label(cadence, "cadence_limits key")
        _int(max_age, "cadence_limits age")
    for node_id, entry in tree["nodes"].items():
        if "as_of_tick" not in entry:
            continue
        as_of = _int(entry["as_of_tick"], "summary as_of_tick")
        cadence = _label(entry.get("refresh_cadence"), "refresh_cadence")
        if cadence not in limits:
            _fail("summary cadence not configured")
        if as_of > tick:
            _fail("summary from future tick")
        query = entry.get("query")
        if query is not None:
            if node_id not in queries or _canonical(queries[node_id], "expected query") != _canonical(query, "summary query"):
                _fail("summary query identity mismatch")
        if tick - as_of <= limits[cadence]:
            continue
        evidence = prior.get(node_id)
        expected = {"as_of_tick": as_of, "cadence": cadence,
                    "query": query, "screen_id": screen,
                    "binding_ref": binding["binding_ref"]}
        if cadence != "daily_close" or not isinstance(evidence, Mapping) or dict(evidence) != expected:
            _fail("stale summary without matching declared prior daily-close evidence")


def freeze_snapshot(base, *, sidecar=None, binding, validators):
    """Validate first, then capture independently copied RAM state and metadata.

    ``binding`` must supply tick/cadence/identity, configured freshness limits,
    expected query identities and any declared prior-close evidence. ``validators``
    is an injected mapping; the Godot pair validator is mandatory for Godot.
    """
    if not isinstance(binding, Mapping) or not isinstance(validators, Mapping):
        _fail("binding and validators mappings required")
    if not isinstance(base, dict):
        _fail("base must be a STATE object")
    try:
        errors = schema.validate_state(base)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        _fail("base schema validation failed: " + str(exc))
    if errors:
        _fail("invalid base STATE: " + "; ".join(errors))
    base_json = _canonical(base, "base")
    base_sha = _sha(base_json)
    ax_stable = _hash(base["ax_stable_sha256"], "ax_stable_sha256")
    kind = base["source"]["kind"]
    if kind not in _POLICIES:
        _fail("unsupported source identity policy: " + str(kind))
    policy = _POLICIES[kind]
    if binding.get("identity_policy") != policy:
        _fail("identity policy incompatible with source")
    tick = _int(binding.get("source_tick"), "source_tick")
    cadence = _label(binding.get("cadence"), "cadence")
    screen = _label(binding.get("screen_id"), "screen_id")
    if screen != _label(binding.get("expected_screen_id"), "expected_screen_id"):
        _fail("screen identity mismatch")
    entity = _label(binding.get("entity_id"), "entity_id")
    if entity != _label(binding.get("expected_entity_id"), "expected_entity_id"):
        _fail("entity identity mismatch")
    ref = _label(binding.get("binding_ref"), "binding_ref")
    simulation = _hash(binding.get("simulation_state_sha256"), "simulation_state_sha256", nullable=True)
    presentation = _hash(binding.get("presentation_sha256"), "presentation_sha256", nullable=True)
    if simulation is not None and simulation in (base_sha, ax_stable):
        _fail("simulation hash collides with distinct base/accessibility hash domain")
    if presentation is not None and presentation in (base_sha, ax_stable, simulation):
        _fail("presentation hash collides with distinct state/hash domain")
    extra_validator = validators.get("base")
    if extra_validator is not None:
        if not callable(extra_validator):
            _fail("base validator must be callable")
        _result_errors(extra_validator, base)
    tree_json = None
    tree_sha = None
    if kind == "godot":
        if not isinstance(sidecar, dict) or sidecar.get("schema") != _SIDECAR_SCHEMA:
            _fail("reviewed Godot sidecar required; Electron metadata is unsupported")
        if set(sidecar) != {"schema", "base_sha256", "nodes"} or sidecar["base_sha256"] != base_sha:
            _fail("base/sidecar binding mismatch")
        pair_validator = validators.get("pair")
        if not callable(pair_validator):
            _fail("Godot pair validator not injected")
        _result_errors(pair_validator, base, sidecar)
        if not isinstance(sidecar["nodes"], dict):
            _fail("sidecar nodes mapping required")
        for node_id, entry in sidecar["nodes"].items():
            if not isinstance(entry, dict) or entry.get("screen_id") != screen:
                _fail("sidecar screen identity mismatch")
            path = entry.get("control_path")
            if not isinstance(path, str) or not _HEX16.fullmatch(node_id) or _sha(path)[:16] != node_id:
                _fail("Godot path identity mismatch")
        _check_freshness(sidecar, binding, tick, screen)
        tree_json = _canonical(sidecar, "Godot sidecar")
        tree_sha = _sha(tree_json)
    else:
        if sidecar is not None:
            _fail("unreviewed Electron/CDP sidecar is unsupported")
        fingerprint = _hash(binding.get("fingerprint"), "CDP fingerprint")
        if fingerprint != _hash(binding.get("expected_fingerprint"), "expected CDP fingerprint"):
            _fail("CDP fingerprint identity mismatch")
    return FrozenSnapshot(base_json, tree_json, base_sha, ax_stable, simulation,
                          tree_sha, presentation, policy, tick, cadence, screen, ref, entity)


def difference_metadata(before, after, *, max_ids):
    """Stable-ID sorted, bounded, text-free projection of the real STATE diff."""
    _int(max_ids, "max_ids", positive=True)
    if not isinstance(before, FrozenSnapshot) or not isinstance(after, FrozenSnapshot):
        _fail("frozen snapshots required")
    if (before.identity_policy, before.screen_id, before.entity_id, before.binding_ref) != (
            after.identity_policy, after.screen_id, after.entity_id, after.binding_ref):
        _fail("snapshot identity or binding changed")
    full = diff_states(before.base, after.base)
    added = sorted(full["added"])
    removed = sorted(full["removed"])
    changed = sorted({c["id"] for c in full["changed"]} | set(full["layout_only"]))
    if len(added) + len(removed) + len(changed) > max_ids:
        _fail("semantic diff exceeds configured ID bound")
    return {"added_ids": tuple(added), "removed_ids": tuple(removed),
            "changed_ids": tuple(changed), "semantic_diff_sha256": full["diff_sha256"]}


@dataclass(frozen=True)
class TrailEntry:
    """Approved, bounded metadata only; no STATE, sidecar, frames or receipts."""
    source_tick: int
    base_sha256: str
    ax_stable_sha256: str
    simulation_state_sha256: str | None
    sidecar_sha256: str | None
    presentation_sha256: str | None
    identity_policy: str
    cadence: str
    screen_id: str
    binding_ref: str
    added_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]
    changed_ids: tuple[str, ...]
    semantic_diff_sha256: str | None
    previous_entry_sha256: str | None
    entry_sha256: str

    def record(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _entry_digest(record):
    return _sha(_canonical({k: v for k, v in record.items() if k != "entry_sha256"}, "trail entry"))


def validate_trail(trail, *, max_ticks, max_ids):
    """Validate complete hash-linked executed-tick trail before any append/export."""
    _int(max_ticks, "max_ticks", positive=True)
    _int(max_ids, "max_ids", positive=True)
    if not isinstance(trail, tuple) or len(trail) > max_ticks:
        _fail("trail must be bounded tuple")
    previous = None
    for entry in trail:
        if not isinstance(entry, TrailEntry):
            _fail("invalid trail entry")
        record = entry.record()
        if record["previous_entry_sha256"] != (previous.entry_sha256 if previous else None):
            _fail("invalid preceding parent hash")
        if previous and entry.source_tick != previous.source_tick + 1:
            _fail("executed ticks must be contiguous")
        if sum(len(record[k]) for k in ("added_ids", "removed_ids", "changed_ids")) > max_ids:
            _fail("diff metadata exceeds configured ID bound")
        for key in ("added_ids", "removed_ids", "changed_ids"):
            seq = record[key]
            if not isinstance(seq, tuple) or tuple(sorted(set(seq))) != seq or any(not _HEX16.fullmatch(x) for x in seq):
                _fail("invalid stable-ID ordering in trail")
        if _entry_digest(record) != entry.entry_sha256:
            _fail("trail entry digest mismatch")
        previous = entry
    return True


def append_executed_tick(trail, snapshot, *, previous_snapshot=None, max_ticks, max_diff_ids):
    """Append exactly one executed tick, including an unchanged observation."""
    validate_trail(trail, max_ticks=max_ticks, max_ids=max_diff_ids)
    if len(trail) >= max_ticks:
        _fail("executed-tick trail capacity reached")
    if not isinstance(snapshot, FrozenSnapshot):
        _fail("snapshot must be frozen")
    if trail:
        last = trail[-1]
        if previous_snapshot is None or not isinstance(previous_snapshot, FrozenSnapshot):
            _fail("preceding frozen snapshot required")
        if (last.source_tick, last.base_sha256, last.sidecar_sha256) != (
                previous_snapshot.source_tick, previous_snapshot.base_sha256, previous_snapshot.sidecar_sha256):
            _fail("preceding parent snapshot mismatch")
        if snapshot.source_tick != last.source_tick + 1:
            _fail("missing or duplicate executed tick")
        diff = difference_metadata(previous_snapshot, snapshot, max_ids=max_diff_ids)
    else:
        if previous_snapshot is not None:
            _fail("first executed tick has no preceding parent")
        diff = {"added_ids": (), "removed_ids": (), "changed_ids": (), "semantic_diff_sha256": None}
    data = dict(snapshot.metadata(), **diff)
    data["previous_entry_sha256"] = trail[-1].entry_sha256 if trail else None
    data["entry_sha256"] = _entry_digest(data)
    entry = TrailEntry(**data)
    return trail + (entry,)


def export_trail(trail, *, max_ticks, max_diff_ids):
    """Persistable integer JSON containing ONLY approved bounded metadata."""
    validate_trail(trail, max_ticks=max_ticks, max_ids=max_diff_ids)
    return _canonical([entry.record() for entry in trail], "trail")
