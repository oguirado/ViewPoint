"""Pure, deterministic proto1 suite-manifest planner (no execution or I/O).

The host supplies an explicitly sealed inventory, measured duration samples,
resource slots, live capabilities, and an actual *byte snapshot* of each source.
Capsules execute gate_argv only; this planner neither executes nor registers jobs.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re


SCHEMA = "proto1.suite-manifest/v1"
PLANNER_VERSION = "p1-va-lpt/v1"
_MAX_NS = (1 << 63) - 1
_PY_PART = r"[A-Za-z_][A-Za-z_0-9]*"
_PY_MODULE = re.compile(rf"{_PY_PART}(?:\.{_PY_PART})*")
_TEST_PATH = re.compile(rf"tests/(?:{_PY_PART}/)*{_PY_PART}\.py")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_LABEL = re.compile(r"[a-z][a-z0-9_-]*")
_REQUEST_KEYS = frozenset((
    "repo", "source_commit", "seed", "requested_shards", "selected_case_ids",
    "inventory", "suite_sha256", "duration_ledger", "duration_ledger_sha256",
    "available_capabilities", "shard_slots",
))
_MODULE_KEYS = frozenset((
    "module", "path", "source_sha256", "case_ids", "resource_class",
    "required_capabilities", "exclusive_resources",
))
_SAMPLE_KEYS = frozenset(("sample_id", "module", "duration_ns", "source_sha256"))
_SLOT_KEYS = frozenset(("slot_id", "resource_class", "capabilities", "exclusive_resources"))
_MANIFEST_KEYS = (
    "schema", "repo", "source_commit", "suite_sha256", "duration_ledger_sha256",
    "planner_version", "seed", "requested_shards", "selected_case_ids",
    "capabilities", "shards", "manifest_sha256",
)
_SHARD_KEYS = (
    "shard_id", "index", "resource_class", "modules", "case_ids",
    "estimated_duration_ns", "required_capabilities", "exclusive_resources",
    "gate_argv", "expected_outputs", "field_sha256",
)


class ManifestRefusal(ValueError):
    """Typed admission refusal. No manifest has been emitted or state changed."""

    def __init__(self, code: str, detail: str, blocked_case_ids: tuple[str, ...] = ()) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.blocked_case_ids = blocked_case_ids


@dataclass(frozen=True)
class ManifestPlan:
    """Immutable admission result and independently useful scheduling bounds."""

    manifest: bytes
    longest_module_lower_bound_ns: int
    average_load_lower_bound_ns: int
    makespan_lower_bound_ns: int
    estimated_makespan_ns: int
    oversized_modules: tuple[str, ...]


def _refuse(code: str, message: str) -> None:
    raise ManifestRefusal(code, message)


def _pairs_unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _refuse("E_DUPLICATE_KEY", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_float(_value: str) -> None:
    _refuse("E_TYPE", "floats and non-integer JSON numbers are forbidden")


def _parse_json(value: object) -> dict[str, object]:
    if type(value) is dict:
        return value
    if type(value) is bytes:
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            _refuse("E_JSON", "input is not UTF-8")
    if type(value) is not str:
        _refuse("E_TYPE", "request must be a dict or UTF-8 JSON text")
    try:
        parsed = json.loads(value, object_pairs_hook=_pairs_unique,
                            parse_float=_reject_float, parse_constant=_reject_float)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ManifestRefusal):
            raise
        _refuse("E_JSON", "invalid JSON syntax")
    if type(parsed) is not dict:
        _refuse("E_TYPE", "top-level JSON must be an object")
    return parsed


def _json_domain(obj: object) -> None:
    if type(obj) in (str, int):
        if type(obj) is str:
            try:
                obj.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                _refuse("E_TYPE", "non-UTF-8 Unicode string")
        return
    if type(obj) is list:
        for value in obj:
            _json_domain(value)
        return
    if type(obj) is dict:
        for key, value in obj.items():
            if type(key) is not str:
                _refuse("E_TYPE", "JSON object keys must be strings")
            _json_domain(key)
            _json_domain(value)
        return
    _refuse("E_TYPE", "JSON domain permits only objects, arrays, strings and exact integers")


def _json_bytes(obj: object, *, sort_keys: bool) -> bytes:
    _json_domain(obj)
    return json.dumps(obj, sort_keys=sort_keys, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(obj: object) -> str:
    """Sorted, compact, integer-only UTF-8 JSON without a final newline."""
    return hashlib.sha256(_json_bytes(obj, sort_keys=True)).hexdigest()


def _fields(value: object, fields: frozenset[str], context: str) -> dict[str, object]:
    if type(value) is not dict:
        _refuse("E_TYPE", f"{context} must be a JSON object")
    unknown = set(value) - fields
    if unknown:
        _refuse("E_UNKNOWN_FIELD", f"{context}: {sorted(unknown)}")
    missing = fields - set(value)
    if missing:
        _refuse("E_MISSING_FIELD", f"{context}: {sorted(missing)}")
    return value


def _string(value: object, context: str) -> str:
    if type(value) is not str:
        _refuse("E_TYPE", f"{context} must be a string")
    return value


def _label(value: object, context: str) -> str:
    result = _string(value, context)
    if _LABEL.fullmatch(result) is None:
        _refuse("E_TYPE", f"{context} is not a canonical label")
    return result


def _sha(value: object, context: str) -> str:
    digest = _string(value, context)
    if _SHA256.fullmatch(digest) is None:
        _refuse("E_TYPE", f"{context} must be lowercase SHA-256")
    return digest


def _integer(value: object, context: str, lo: int, hi: int) -> int:
    if type(value) is not int:
        _refuse("E_TYPE", f"{context} must be an exact integer")
    if not lo <= value <= hi:
        _refuse("E_RANGE", f"{context} must be in [{lo}, {hi}]")
    return value


def _list(value: object, context: str) -> list[object]:
    if type(value) is not list:
        _refuse("E_TYPE", f"{context} must be a JSON array")
    return value


def _labels(value: object, context: str) -> list[str]:
    labels = [_label(item, context) for item in _list(value, context)]
    if len(labels) != len(set(labels)):
        _refuse("E_DUPLICATE_VALUE", f"{context} has duplicates")
    return sorted(labels)


def _module_name(value: object) -> str:
    name = _string(value, "module")
    if _PY_MODULE.fullmatch(name) is None or not name.startswith("tests."):
        _refuse("E_PATH", "noncanonical unittest module")
    return name


def _case_ids(value: object, *, parent: str | None = None) -> list[str]:
    ids = []
    for candidate in _list(value, "case_ids"):
        case = _string(candidate, "case_id")
        if (_PY_MODULE.fullmatch(case) is None or
                len(case.split(".")) < 4 or
                not case.split(".")[-1].startswith("test_")):
            _refuse("E_CASE_COVERAGE", f"invalid unittest case: {case}")
        if parent is not None:
            parts = case.split(".")
            if ".".join(parts[:-2]) != parent:
                _refuse("E_CASE_COVERAGE", f"foreign case for {parent}: {case}")
        ids.append(case)
    if len(ids) != len(set(ids)):
        _refuse("E_DUPLICATE_CASE", "duplicate case identifier")
    return sorted(ids)


def _validate_inventory(raw: object, sources: Mapping[str, bytes]) -> tuple[list[dict], list[str]]:
    modules: list[dict] = []
    seen_modules: set[str] = set()
    seen_paths: set[str] = set()
    all_cases: set[str] = set()
    for index, item in enumerate(_list(raw, "inventory")):
        mod = _fields(item, _MODULE_KEYS, f"inventory[{index}]")
        module = _module_name(mod["module"])
        path = _string(mod["path"], "path")
        if _TEST_PATH.fullmatch(path) is None or module != path[:-3].replace("/", "."):
            _refuse("E_PATH", f"noncanonical path/module pair: {path}")
        if module in seen_modules or path in seen_paths:
            _refuse("E_DUPLICATE_MODULE", f"duplicate module/path: {module}")
        seen_modules.add(module)
        seen_paths.add(path)
        source_sha = _sha(mod["source_sha256"], "source_sha256")
        cases = _case_ids(mod["case_ids"], parent=module)
        if not cases:
            _refuse("E_EMPTY", f"module has no discovered cases: {module}")
        if all_cases.intersection(cases):
            _refuse("E_DUPLICATE_CASE", f"cases repeated in modules: {module}")
        all_cases.update(cases)
        modules.append({
            "module": module, "path": path, "source_sha256": source_sha,
            "case_ids": cases,
            "resource_class": _label(mod["resource_class"], "resource_class"),
            "required_capabilities": _labels(mod["required_capabilities"], "required_capabilities"),
            "exclusive_resources": _labels(mod["exclusive_resources"], "exclusive_resources"),
        })
    if not modules:
        _refuse("E_EMPTY", "discovery produced no modules")
    if not isinstance(sources, Mapping):
        _refuse("E_TYPE", "source_files must be a mapping of current raw bytes")
    if set(sources) != seen_paths:
        _refuse("E_SOURCE_SNAPSHOT", "source snapshot paths must exactly match inventory")
    for module in modules:
        source = sources[module["path"]]
        if type(source) is not bytes:
            _refuse("E_TYPE", f"source snapshot must contain bytes: {module['path']}")
        if hashlib.sha256(source).hexdigest() != module["source_sha256"]:
            _refuse("E_SOURCE_MISMATCH", f"source differs from seal: {module['path']}")
    return sorted(modules, key=lambda item: item["module"]), sorted(all_cases)


def _samples(raw: object, inventory: list[dict]) -> tuple[list[dict], dict[str, int]]:
    ledger = _fields(raw, frozenset(("samples",)), "duration_ledger")
    names = {module["module"]: module["source_sha256"] for module in inventory}
    observed: dict[str, list[int]] = defaultdict(list)
    seen_ids: set[str] = set()
    samples = []
    for index, item in enumerate(_list(ledger["samples"], "samples")):
        record = _fields(item, _SAMPLE_KEYS, f"samples[{index}]")
        sample_id = _label(record["sample_id"], "sample_id")
        module = _module_name(record["module"])
        duration = _integer(record["duration_ns"], "duration_ns", 1, _MAX_NS)
        source_sha = _sha(record["source_sha256"], "sample.source_sha256")
        if sample_id in seen_ids:
            _refuse("E_DUPLICATE_SAMPLE", f"duplicate sample_id: {sample_id}")
        seen_ids.add(sample_id)
        if module not in names or names[module] != source_sha:
            _refuse("E_LEDGER_SOURCE", f"sample does not bind to inventory: {module}")
        observed[module].append(duration)
        samples.append({"sample_id": sample_id, "module": module,
                        "duration_ns": duration, "source_sha256": source_sha})
    if set(observed) != set(names):
        _refuse("E_DURATION_MISSING", f"missing duration for: {sorted(set(names) - set(observed))}")
    estimates = {}
    for module, values in observed.items():
        ordered = sorted(values)
        middle = len(ordered) // 2
        estimates[module] = (ordered[middle] if len(ordered) % 2 else
                             (ordered[middle - 1] + ordered[middle] + 1) // 2)
    return sorted(samples, key=lambda item: (item["module"], item["sample_id"])), estimates


def _slots(raw: object, count: int) -> list[dict]:
    slots: list[dict] = []
    used_ids: set[str] = set()
    claimed_exclusives: set[str] = set()
    for index, item in enumerate(_list(raw, "shard_slots")):
        slot = _fields(item, _SLOT_KEYS, f"shard_slots[{index}]")
        slot_id = _label(slot["slot_id"], "slot_id")
        if slot_id in used_ids:
            _refuse("E_DUPLICATE_SLOT", f"duplicate slot_id: {slot_id}")
        used_ids.add(slot_id)
        exclusive = _labels(slot["exclusive_resources"], "slot.exclusive_resources")
        if claimed_exclusives.intersection(exclusive):
            _refuse("E_RESOURCE_CONFLICT", "physical exclusive resource owned by multiple slots")
        claimed_exclusives.update(exclusive)
        slots.append({"slot_id": slot_id,
                      "resource_class": _label(slot["resource_class"], "slot.resource_class"),
                      "capabilities": _labels(slot["capabilities"], "slot.capabilities"),
                      "exclusive_resources": exclusive})
    if len(slots) != count:
        _refuse("E_SHARDS", "requested_shards must exactly match explicit slot count")
    return sorted(slots, key=lambda item: item["slot_id"])


def _choose_slot(candidates: list[int], loads: list[int]) -> int:
    """LPT deterministic tie break: current load, then canonical slot index."""
    return min(candidates, key=lambda index: (loads[index], index))


def build_manifest(request: dict | str | bytes, *, source_files: Mapping[str, bytes]) -> ManifestPlan:
    """Admit a fully pinned plan or raise ManifestRefusal (no side effects).

    Raw bytes in source_files MUST be read by the host from the current source
    checkout, not from the inventory's claimed hashes or an untrusted cache.
    """
    data = _fields(_parse_json(request), _REQUEST_KEYS, "request")
    _json_domain(data)
    if data["repo"] != "vision-engine":
        _refuse("E_REPO", "repo must be vision-engine")
    commit = _string(data["source_commit"], "source_commit")
    if _COMMIT.fullmatch(commit) is None:
        _refuse("E_TYPE", "source_commit must be lowercase 40-character hex")
    seed = _integer(data["seed"], "seed", 0, _MAX_NS)
    count = _integer(data["requested_shards"], "requested_shards", 1, 64)
    claimed_suite = _sha(data["suite_sha256"], "suite_sha256")
    claimed_ledger = _sha(data["duration_ledger_sha256"], "duration_ledger_sha256")
    selected = _case_ids(data["selected_case_ids"])
    inventory, discovered = _validate_inventory(data["inventory"], source_files)
    if not selected:
        _refuse("E_EMPTY", "selected discovery is empty")
    if selected != discovered:
        _refuse("E_CASE_COVERAGE", "selected_case_ids must exactly equal all discovered case IDs")
    if count > len(inventory):
        _refuse("E_SHARDS", "cannot give every shard an indivisible module")
    suite_object = {"repo": "vision-engine", "source_commit": commit,
                    "selected_case_ids": selected, "modules": inventory}
    if _digest(suite_object) != claimed_suite:
        _refuse("E_SUITE_SEAL", "suite inventory, paths or source hashes differ from seal")
    samples, estimates = _samples(data["duration_ledger"], inventory)
    if _digest({"samples": samples}) != claimed_ledger:
        _refuse("E_LEDGER_SEAL", "actual duration sample records differ from ledger seal")
    available = _labels(data["available_capabilities"], "available_capabilities")
    slots = _slots(data["shard_slots"], count)
    unavailable_cases = sorted({case for mod in inventory
                                if not set(mod["required_capabilities"]).issubset(available)
                                for case in mod["case_ids"]})
    if unavailable_cases:
        raise ManifestRefusal("E_CAPABILITY_BLOCKED", "live required capability is unavailable",
                              tuple(unavailable_cases))
    loads = [0] * count
    assigned: list[list[dict]] = [[] for _ in slots]
    module_order = sorted(inventory, key=lambda mod: (-estimates[mod["module"]], mod["module"]))
    for module in module_order:
        candidates = [index for index, slot in enumerate(slots)
                      if slot["resource_class"] == module["resource_class"]
                      and set(module["required_capabilities"]).issubset(slot["capabilities"])
                      and set(module["exclusive_resources"]).issubset(slot["exclusive_resources"])]
        if not candidates:
            _refuse("E_RESOURCE_INCOMPATIBLE", f"no compatible slot for {module['module']}")
        slot_index = _choose_slot(candidates, loads)
        assigned[slot_index].append(module)
        loads[slot_index] += estimates[module["module"]]
        if loads[slot_index] > _MAX_NS:
            _refuse("E_RANGE", "aggregate shard duration exceeds int64")
    if any(not modules for modules in assigned):
        _refuse("E_SHARDS_EMPTY", "at least one requested shard is empty")
    shards = []
    for index, modules in enumerate(assigned):
        shard_id = f"shard-{index:03d}"
        names = sorted(mod["module"] for mod in modules)
        cases = sorted(case for mod in modules for case in mod["case_ids"])
        required = sorted({cap for mod in modules for cap in mod["required_capabilities"]})
        exclusive = sorted({res for mod in modules for res in mod["exclusive_resources"]})
        shard = {
            "shard_id": shard_id,
            "index": index,
            "resource_class": slots[index]["resource_class"],
            "modules": names,
            "case_ids": cases,
            "estimated_duration_ns": loads[index],
            "required_capabilities": required,
            "exclusive_resources": exclusive,
            "gate_argv": ["python3", "-m", "unittest", *names],
            "expected_outputs": [f"artifacts/p1_va/{shard_id}/receipt.json"],
        }
        shard["field_sha256"] = _digest(shard)
        shards.append(shard)
    flattened = [case for shard in shards for case in shard["case_ids"]]
    if sorted(flattened) != selected or len(flattened) != len(set(flattened)):
        _refuse("E_CASE_COVERAGE", "partition is not exact and disjoint")
    longest = max(estimates.values())
    total = sum(estimates.values())
    average_lower = (total + count - 1) // count
    plan = {
        "schema": SCHEMA,
        "repo": "vision-engine",
        "source_commit": commit,
        "suite_sha256": claimed_suite,
        "duration_ledger_sha256": claimed_ledger,
        "planner_version": PLANNER_VERSION,
        "seed": seed,
        "requested_shards": count,
        "selected_case_ids": selected,
        "capabilities": available,
        "shards": shards,
    }
    plan["manifest_sha256"] = _digest(plan)
    return ManifestPlan(
        manifest=_json_bytes(plan, sort_keys=False),
        longest_module_lower_bound_ns=longest,
        average_load_lower_bound_ns=average_lower,
        makespan_lower_bound_ns=max(longest, average_lower),
        estimated_makespan_ns=max(loads),
        oversized_modules=tuple(sorted(mod for mod, duration in estimates.items()
                                       if duration > average_lower)),
    )


def verify_manifest(manifest: bytes, request: dict | str | bytes, *,
                    source_files: Mapping[str, bytes]) -> ManifestPlan:
    """Re-admit against authoritative inputs; require identical signed-domain bytes."""
    if type(manifest) is not bytes:
        _refuse("E_TYPE", "manifest must be UTF-8 bytes")
    parsed = _parse_json(manifest)
    if set(parsed) != set(_MANIFEST_KEYS):
        _refuse("E_MANIFEST_TAMPER", "manifest fields differ from schema")
    if type(parsed["shards"]) is not list:
        _refuse("E_MANIFEST_TAMPER", "shards must be an array")
    for shard in parsed["shards"]:
        if type(shard) is not dict or set(shard) != set(_SHARD_KEYS):
            _refuse("E_MANIFEST_TAMPER", "shard fields differ from schema")
    expected = build_manifest(request, source_files=source_files)
    if manifest != expected.manifest:
        _refuse("E_MANIFEST_TAMPER", "manifest is not the exact canonical admitted plan")
    return expected
