# @module {"id":"wa.learn.proto1_shard_manifest","layer":2,"owner":"LEARN","deps":[],"book":"HB-WA-s4-learn"}
"""Strict, deterministic proto1 unittest inventory, sealed durations and LPT plans.

This foundation module has no runner, queue, disk or transport side effects. All
measurements and capability evidence are independent, caller-supplied records.
Only emit_manifest calls an explicitly injected transport, after complete
validation against its pinned source, inventory, durations and capability slots.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest

__all__ = (
    "ManifestRefusal", "inventory_suite", "inventory_unittest_suite",
    "seal_duration_ledger", "plan_shards", "plan_lower_bounds",
    "validate_manifest", "emit_manifest", "canonical_manifest_bytes",
)

SUITE_SCHEMA = "proto1.suite-inventory/v1"
LEDGER_SCHEMA = "proto1.duration-ledger/v1"
MANIFEST_SCHEMA = "proto1.suite-manifest/v1"
PLANNER_VERSION = "lpt-indivisible-modules/v1"
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z", re.ASCII)
_PATH = re.compile(r"[A-Za-z0-9_./-]+\Z")
_CASE_KEYS = ("case_id", "module", "resource_class", "required_capabilities", "exclusive_resources", "source_path")
_SUITE_KEYS = ("schema", "repo", "source_commit", "cases", "source_files", "suite_sha256")
_LEDGER_KEYS = ("schema", "repo", "source_commit", "suite_sha256", "modules", "ledger_sha256")
_ROW_KEYS = ("module", "duration_ns", "warm_samples_ns", "cold_start_samples_ns")
_SLOT_KEYS = ("index", "resource_class", "capabilities", "exclusive_resources")
_EVIDENCE_KEYS = ("name", "status", "evidence", "source_commit")
_MANIFEST_KEYS = ("schema", "repo", "source_commit", "suite_sha256", "duration_ledger_sha256", "planner_version", "seed", "requested_shards", "selected_case_ids", "capabilities", "shards", "manifest_sha256")
_SHARD_KEYS = ("shard_id", "index", "resource_class", "modules", "case_ids", "estimated_duration_ns", "required_capabilities", "exclusive_resources", "gate_argv", "expected_outputs", "field_sha256")


class ManifestRefusal(ValueError):
    """Deterministic preflight refusal with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str) -> None:
    raise ManifestRefusal(code, message)


def _object(value, keys, label, code="INVALID_FIELD"):
    if type(value) is not dict or set(value) != set(keys):
        _refuse(code, f"{label} must have exactly {list(keys)!r}")
    return value


def _array(value, label):
    if type(value) not in (list, tuple):
        _refuse("INVALID_FIELD", f"{label} must be an array")
    return value


def _string(value, label, pattern=None):
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        _refuse("INVALID_FIELD", f"{label} must be a canonical nonempty string")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _refuse("INVALID_FIELD", f"{label} cannot be encoded as UTF-8")
    if pattern is not None and not pattern.fullmatch(value):
        _refuse("INVALID_FIELD", f"{label} has invalid syntax")
    return value


def _integer(value, label, minimum=0, code="INVALID_FIELD"):
    if type(value) is not int or value < minimum:
        _refuse(code, f"{label} must be an exact integer >= {minimum}")
    return value


def _names(values, label):
    names = [_string(item, label) for item in _array(values, label)]
    if len(names) != len(set(names)):
        _refuse("DUPLICATE_DECLARATION", f"{label} has duplicates")
    return sorted(names)


def _sha(value, label, length=64):
    return _string(value, label, _HEX40 if length == 40 else _HEX64)


def _path(value):
    path = _string(value, "source_path", _PATH)
    if path.startswith("/") or any(bit in (".", "..", "") for bit in path.split("/")):
        _refuse("INVALID_FIELD", "source_path is not a relative canonical path")
    return path


def _canonical(value):
    """No floats, booleans, object reprs, surrogate strings or non-string keys."""
    if value is None or type(value) in (str, int):
        if type(value) is str:
            value.encode("utf-8")
        return value
    if type(value) is list:
        return [_canonical(x) for x in value]
    if type(value) is dict:
        if any(type(k) is not str for k in value):
            _refuse("NON_CANONICAL_VALUE", "JSON keys must be strings")
        return {k: _canonical(v) for k, v in value.items()}
    _refuse("NON_CANONICAL_VALUE", f"unsupported canonical JSON value {type(value).__name__}")


def _bytes(value):
    try:
        return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError) as exc:
        _refuse("NON_CANONICAL_VALUE", str(exc))


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def inventory_suite(*, cases, source_files, repo, source_commit):
    """Seal independent unittest case IDs and supplied source-file SHA-256s.

    Every case declares its exact owning dotted module, relative source path,
    resource class, capability set and exclusive resource set. Modules cannot
    contain conflicting class/capability declarations or duplicate case IDs.
    Source hashes must be computed independently from actual source bytes.
    """
    repo = _string(repo, "repo", _REPO)
    source_commit = _sha(source_commit, "source_commit", 40)
    if type(source_files) is not dict or not source_files:
        _refuse("INVALID_FIELD", "source_files must be a nonempty mapping")
    sources = {}
    for path, sha in source_files.items():
        sources[_path(path)] = _sha(sha, "source digest")
    seen = set()
    normalized = []
    profiles = {}
    for item in _array(cases, "cases"):
        case = _object(item, _CASE_KEYS, "case")
        case_id = _string(case["case_id"], "case_id")
        module = _string(case["module"], "module", _MODULE)
        if not _MODULE.fullmatch(case_id) or not case_id.startswith(module + ".") or len(case_id.split(".")) < len(module.split(".")) + 2:
            _refuse("INVALID_FIELD", "case_id must identify a class and method in its module")
        if case_id in seen:
            _refuse("DUPLICATE_CASE_ID", f"case_id repeated: {case_id}")
        seen.add(case_id)
        resource_class = _string(case["resource_class"], "resource_class")
        required = _names(case["required_capabilities"], "required_capabilities")
        exclusive = _names(case["exclusive_resources"], "exclusive_resources")
        source_path = _path(case["source_path"])
        if source_path not in sources:
            _refuse("MISSING_SOURCE_DIGEST", f"missing digest for {source_path}")
        profile = (resource_class, tuple(required), tuple(exclusive), source_path)
        if module in profiles and profiles[module] != profile:
            _refuse("INCOMPATIBLE_RESOURCE_CLASS", f"conflicting module profile: {module}")
        profiles[module] = profile
        normalized.append(dict(case_id=case_id, module=module, resource_class=resource_class,
                               required_capabilities=required, exclusive_resources=exclusive,
                               source_path=source_path))
    if not normalized:
        _refuse("EMPTY_DISCOVERY", "unittest discovery returned no cases")
    normalized.sort(key=lambda x: x["case_id"])
    record = dict(schema=SUITE_SCHEMA, repo=repo, source_commit=source_commit,
                  cases=normalized, source_files=dict(sorted(sources.items())))
    return dict(record, suite_sha256=_digest(record))


def inventory_unittest_suite(*, suite, module_declarations, source_files, repo, source_commit):
    """Extract actual TestCase.id() leaves from a preloaded unittest.TestSuite.

    No test discovery/imports, test execution, fixture calls, or filesystem I/O
    happen here. The caller loads the suite and independently declares modules.
    """
    if type(module_declarations) is not dict:
        _refuse("INVALID_FIELD", "module_declarations must be a mapping")
    cases = []

    def walk(node):
        if isinstance(node, unittest.TestSuite):
            for child in node:
                walk(child)
        elif isinstance(node, unittest.TestCase):
            module = node.__class__.__module__
            if module not in module_declarations:
                _refuse("MISSING_MODULE_DECLARATION", f"no profile for {module}")
            profile = _object(module_declarations[module],
                              ("resource_class", "required_capabilities", "exclusive_resources", "source_path"),
                              "module_declaration")
            cases.append(dict(case_id=node.id(), module=module, **profile))
        else:
            _refuse("INVALID_DISCOVERY", "suite contains a non-TestCase leaf")

    walk(suite)
    return inventory_suite(cases=cases, source_files=source_files, repo=repo,
                           source_commit=source_commit)


def _validated_inventory(inventory, current_source_files):
    inv = _object(inventory, _SUITE_KEYS, "inventory")
    if inv["schema"] != SUITE_SCHEMA:
        _refuse("INVALID_SCHEMA", "unknown inventory schema")
    computed = inventory_suite(cases=inv["cases"], source_files=inv["source_files"],
                               repo=inv["repo"], source_commit=inv["source_commit"])
    if inv["suite_sha256"] != computed["suite_sha256"] or inv != computed:
        _refuse("SUITE_SEAL_MISMATCH", "inventory does not match its canonical seal")
    if type(current_source_files) is not dict or set(current_source_files) != set(inv["source_files"]):
        _refuse("STALE_SOURCE", "fresh source-file digest inventory must match")
    for path, digest in current_source_files.items():
        if _sha(digest, "current source digest") != inv["source_files"][path]:
            _refuse("STALE_SOURCE", f"file changed since suite seal: {path}")
    return computed


def _validate_measurements(measurements, modules):
    rows = {}
    for item in _array(measurements, "measurements"):
        if type(item) is not dict:
            _refuse("MISSING_DURATION", "duration record must be an object")
        missing = set(_ROW_KEYS) - set(item)
        if missing:
            _refuse("MISSING_DURATION", f"duration record missing {sorted(missing)}")
        row = _object(item, _ROW_KEYS, "duration record")
        module = _string(row["module"], "duration module", _MODULE)
        if module in rows:
            _refuse("DUPLICATE_DURATION", f"duplicate duration module {module}")
        warm = [_integer(v, "warm sample", 1) for v in _array(row["warm_samples_ns"], "warm_samples_ns")]
        cold = [_integer(v, "cold-start sample", 1) for v in _array(row["cold_start_samples_ns"], "cold_start_samples_ns")]
        if not warm or not cold:
            _refuse("MISSING_COLD_START", "positive warm and cold-start samples are mandatory")
        duration = _integer(row["duration_ns"], "duration_ns", 1, "MISSING_DURATION")
        conservative = max(sorted(warm)[len(warm)//2], sorted(cold)[len(cold)//2])
        if duration != conservative:
            _refuse("DURATION_NOT_MEASURED", f"duration_ns differs from upper-median samples for {module}")
        rows[module] = dict(module=module, duration_ns=duration,
                            warm_samples_ns=warm, cold_start_samples_ns=cold)
    if set(rows) != set(modules):
        _refuse("MISSING_DURATION", f"ledger modules differ from discovery: missing {sorted(set(modules)-set(rows))}, extra {sorted(set(rows)-set(modules))}")
    return [rows[k] for k in sorted(rows)]


def seal_duration_ledger(*, inventory, measurements):
    """Seal one measured (warm + cold-start) duration per indivisible module."""
    inv = _object(inventory, _SUITE_KEYS, "inventory")
    # Validate inventory seal even without a separate source snapshot: the caller
    # must supply fresh digests again when planning or publishing.
    _validated_inventory(inv, inv["source_files"])
    modules = {item["module"] for item in inv["cases"]}
    rows = _validate_measurements(measurements, modules)
    record = dict(schema=LEDGER_SCHEMA, repo=inv["repo"],
                  source_commit=inv["source_commit"], suite_sha256=inv["suite_sha256"],
                  modules=rows)
    return dict(record, ledger_sha256=_digest(record))


def _validated_ledger(ledger, inv):
    item = _object(ledger, _LEDGER_KEYS, "ledger")
    if item["schema"] != LEDGER_SCHEMA:
        _refuse("INVALID_SCHEMA", "unknown ledger schema")
    if any(item[key] != inv[key] for key in ("repo", "source_commit", "suite_sha256")):
        _refuse("STALE_SOURCE", "duration ledger belongs to another source/suite")
    refreshed = seal_duration_ledger(inventory=inv, measurements=item["modules"])
    if refreshed["ledger_sha256"] != item["ledger_sha256"] or refreshed != item:
        _refuse("DURATION_LEDGER_DIGEST_MISMATCH", "duration ledger seal mismatch")
    return refreshed


def _validated_slots(slots, count, source_commit):
    values = []
    if type(slots) not in (tuple, list) or len(slots) != count:
        _refuse("INFEASIBLE_SHARD_COUNT", "need exactly requested_shards worker slots")
    used_exclusive = set()
    for obj in slots:
        slot = _object(obj, _SLOT_KEYS, "worker slot")
        index = _integer(slot["index"], "slot index")
        klass = _string(slot["resource_class"], "slot resource_class")
        caps = []
        names = set()
        for token in _array(slot["capabilities"], "slot capabilities"):
            token = _object(token, _EVIDENCE_KEYS, "capability evidence")
            name = _string(token["name"], "capability")
            if name in names:
                _refuse("DUPLICATE_DECLARATION", "duplicate capability evidence")
            names.add(name)
            if token["status"] != "PASS" or not _string(token["evidence"], "capability evidence"):
                _refuse("MISSING_CAPABILITY_EVIDENCE", f"non-PASS capability {name}")
            if _sha(token["source_commit"], "capability source commit", 40) != source_commit:
                _refuse("STALE_SOURCE", f"capability evidence is stale for {name}")
            caps.append(dict(name=name, status="PASS", evidence=token["evidence"],
                             source_commit=source_commit))
        caps.sort(key=lambda x: x["name"])
        exclusive = _names(slot["exclusive_resources"], "slot exclusive_resources")
        if used_exclusive.intersection(exclusive):
            _refuse("AMBIGUOUS_EXCLUSIVE_RESOURCE", "exclusive token assigned to multiple slots")
        used_exclusive.update(exclusive)
        values.append(dict(index=index, resource_class=klass,
                           capabilities=caps, exclusive_resources=exclusive))
    values.sort(key=lambda x: x["index"])
    if [slot["index"] for slot in values] != list(range(count)):
        _refuse("INFEASIBLE_SHARD_COUNT", "worker indices must be exactly 0..requested_shards-1")
    return values


def _selected_modules(inv, selected_case_ids):
    all_cases = {case["case_id"]: case for case in inv["cases"]}
    chosen = _names(selected_case_ids, "selected_case_ids")
    if not chosen:
        _refuse("EMPTY_DISCOVERY", "no selected tests")
    if not set(chosen).issubset(all_cases):
        _refuse("UNKNOWN_CASE_ID", "selected case absent from discovery")
    chosen_set = set(chosen)
    modules = {}
    for case in inv["cases"]:
        module = case["module"]
        modules.setdefault(module, {"cases": [], "profile": case})["cases"].append(case["case_id"])
    selected = {}
    for name, info in modules.items():
        intersection = set(info["cases"]) & chosen_set
        if intersection and len(intersection) != len(info["cases"]):
            _refuse("PARTIAL_MODULE_SELECTION", f"cannot split unittest module fixtures: {name}")
        if intersection:
            selected[name] = info
    return chosen, selected


def _covers_empty_slots(empty, available_modules, compatible):
    """Deterministic bipartite matching, used only to prevent orphan shards."""
    taken = {}

    def visit(slot_index, visited):
        for name in sorted(available_modules):
            if name in visited or slot_index not in compatible[name]:
                continue
            visited.add(name)
            if name not in taken or visit(taken[name], visited):
                taken[name] = slot_index
                return True
        return False

    for slot_index in sorted(empty):
        if not visit(slot_index, set()):
            return False
    return True


def _prepare(*, inventory, ledger, slots, current_source_files, selected_case_ids, requested_shards):
    count = _integer(requested_shards, "requested_shards", 1, "INFEASIBLE_SHARD_COUNT")
    if count > 64:
        _refuse("INFEASIBLE_SHARD_COUNT", "requested_shards exceeds TARGET max 64")
    inv = _validated_inventory(inventory, current_source_files)
    dur = _validated_ledger(ledger, inv)
    cases, selected = _selected_modules(inv, selected_case_ids)
    if count > len(selected):
        _refuse("INFEASIBLE_SHARD_COUNT", "no empty shards or split modules allowed")
    workers = _validated_slots(slots, count, inv["source_commit"])
    durations = {row["module"]: row["duration_ns"] for row in dur["modules"]}
    compatible = {}
    for name, info in selected.items():
        case = info["profile"]
        compatible[name] = [slot["index"] for slot in workers
                            if case["resource_class"] == slot["resource_class"]
                            and set(case["required_capabilities"]).issubset(
                                {cap["name"] for cap in slot["capabilities"]})
                            and set(case["exclusive_resources"]).issubset(slot["exclusive_resources"])]
        if not compatible[name]:
            _refuse("INCOMPATIBLE_RESOURCE_CLASS", f"no independently evidenced slot for module {name}")
    if not _covers_empty_slots(range(count), selected, compatible):
        _refuse("INFEASIBLE_SHARD_COUNT", "cannot cover all slots with compatible whole modules")
    return inv, dur, cases, selected, workers, durations, compatible


def plan_lower_bounds(*, inventory, ledger, selected_case_ids, requested_shards,
                      current_source_files):
    """Return workload and indivisible-module lower bounds (never optimality)."""
    count = _integer(requested_shards, "requested_shards", 1, "INFEASIBLE_SHARD_COUNT")
    if count > 64:
        _refuse("INFEASIBLE_SHARD_COUNT", "requested_shards exceeds TARGET max 64")
    inv = _validated_inventory(inventory, current_source_files)
    dur = _validated_ledger(ledger, inv)
    _, selected = _selected_modules(inv, selected_case_ids)
    if count > len(selected):
        _refuse("INFEASIBLE_SHARD_COUNT", "more shards than whole modules")
    rows = {r["module"]: r["duration_ns"] for r in dur["modules"]}
    work = sum(rows[module] for module in selected)
    span = max(rows[module] for module in selected)
    average = (work + count - 1) // count
    return dict(work_ns=work, longest_module_ns=span,
                average_lower_bound_ns=average, makespan_lower_bound_ns=max(average, span))


def plan_shards(*, inventory, ledger, slots, current_source_files,
                selected_case_ids, requested_shards, seed):
    """Least-loaded compatible LPT scheduling with deterministic feasibility guard."""
    seed = _integer(seed, "seed")
    inv, dur, cases, modules, workers, durations, compatible = _prepare(
        inventory=inventory, ledger=ledger, slots=slots,
        current_source_files=current_source_files, selected_case_ids=selected_case_ids,
        requested_shards=requested_shards)
    count = requested_shards
    loads = [0] * count
    assigned = [[] for _ in range(count)]
    remaining = set(modules)
    empty = set(range(count))
    for module in sorted(modules, key=lambda name: (-durations[name], name)):
        remaining.remove(module)
        # Choose the least-loaded feasible slot; the future matching check
        # ensures a greedy choice cannot strand an empty capability-only slot.
        candidates = sorted(compatible[module], key=lambda index: (loads[index], index))
        target = next((index for index in candidates
                       if _covers_empty_slots(empty - {index}, remaining, compatible)), None)
        if target is None:
            _refuse("INFEASIBLE_SHARD_COUNT", f"cannot place {module} without an empty shard")
        assigned[target].append(module)
        loads[target] += durations[module]
        empty.discard(target)
    if empty:
        _refuse("INFEASIBLE_SHARD_COUNT", "empty shards are forbidden")
    shards = []
    for index, worker in enumerate(workers):
        names = sorted(assigned[index])
        shard_cases = sorted(c for module in names for c in modules[module]["cases"])
        requirements = sorted({cap for name in names for cap in modules[name]["profile"]["required_capabilities"]})
        exclusive = sorted({r for name in names for r in modules[name]["profile"]["exclusive_resources"]})
        shard = dict(shard_id=f"shard-{index:02d}", index=index,
                     resource_class=worker["resource_class"], modules=names,
                     case_ids=shard_cases, estimated_duration_ns=loads[index],
                     required_capabilities=requirements, exclusive_resources=exclusive,
                     gate_argv=["python3", "-m", "unittest", *names],
                     expected_outputs=["unittest:OK"])
        shards.append(dict(shard, field_sha256=_digest(shard)))
    record = dict(schema=MANIFEST_SCHEMA, repo=inv["repo"],
                  source_commit=inv["source_commit"], suite_sha256=inv["suite_sha256"],
                  duration_ledger_sha256=dur["ledger_sha256"],
                  planner_version=PLANNER_VERSION, seed=seed,
                  requested_shards=count, selected_case_ids=cases,
                  capabilities=workers, shards=shards)
    return dict(record, manifest_sha256=_digest(record))


def validate_manifest(manifest, *, inventory, ledger, slots, current_source_files):
    """Validate all seals and recompute the expected plan before any send."""
    item = _object(manifest, _MANIFEST_KEYS, "manifest")
    if item["schema"] != MANIFEST_SCHEMA:
        _refuse("INVALID_SCHEMA", "unknown manifest schema")
    for shard in _array(item["shards"], "shards"):
        entry = _object(shard, _SHARD_KEYS, "shard")
        supplied = _sha(entry["field_sha256"], "shard field digest")
        if supplied != _digest({k: v for k, v in entry.items() if k != "field_sha256"}):
            _refuse("FIELD_DIGEST_MISMATCH", "shard field_sha256 is altered")
    supplied = _sha(item["manifest_sha256"], "manifest digest")
    if supplied != _digest({k: v for k, v in item.items() if k != "manifest_sha256"}):
        _refuse("MANIFEST_DIGEST_MISMATCH", "manifest_sha256 is altered")
    expected = plan_shards(inventory=inventory, ledger=ledger, slots=slots,
                           current_source_files=current_source_files,
                           selected_case_ids=item["selected_case_ids"],
                           requested_shards=item["requested_shards"], seed=item["seed"])
    if item != expected:
        _refuse("MANIFEST_CONTENT_MISMATCH", "payload diverges from canonical deterministic plan")
    return item


def canonical_manifest_bytes(manifest, *, inventory, ledger, slots, current_source_files):
    """Return compact sorted UTF-8 manifest bytes without trailing newline."""
    return _bytes(validate_manifest(manifest, inventory=inventory, ledger=ledger,
                                    slots=slots, current_source_files=current_source_files))


def emit_manifest(manifest, *, inventory, ledger, slots, current_source_files, sender):
    """Preflight everything before a single caller-owned queue send.

    A sender is an external transport callable accepting one bytes object.
    No local queue is constructed and no allocations are performed on refusal.
    """
    if not callable(sender):
        _refuse("INVALID_FIELD", "sender must be an external callable")
    payload = canonical_manifest_bytes(manifest, inventory=inventory, ledger=ledger,
                                       slots=slots, current_source_files=current_source_files)
    sender(payload)
    return payload
