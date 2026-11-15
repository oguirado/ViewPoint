# @module {"id":"wa.learn.proto1_capabilities","layer":2,"owner":"LEARN","deps":[],"book":"HB-WA-s4-learn"}
"""Offline, fail-closed proto1 unittest capability admission and shard execution.

This is deliberately separate from the GUI perception ladder. No availability
probe, subprocess, network or external test-helper dependency runs at admission.
An external launcher is injected *only* after the entire manifest passes the
capability preflight. The launcher reports actual case IDs; its exit status alone
is never treated as evidence that tests ran.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence

SCHEMA = "proto1.suite-manifest/v1"
PLANNER_VERSION = "wa-proto1-capabilities:1"
BLOCKED_EXIT = 4
CLASS_NAMES = frozenset({"pure", "ram", "capability"})
LIVE_ONLY = frozenset({
    "live_gui", "x_server", "display", "desktop_session", "screen_capture",
    "pointer", "pixels", "vision_model", "accessibility",
})
MANIFEST_FIELDS = (
    "schema", "repo", "source_commit", "suite_sha256", "duration_ledger_sha256",
    "planner_version", "seed", "requested_shards", "selected_case_ids",
    "capabilities", "shards", "manifest_sha256",
)
MODULE_FIELDS = (
    "module", "source_path", "source_sha256", "case_ids", "capability_class",
    "estimated_duration_ns", "required_capabilities", "exclusive_resources",
)
SHARD_FIELDS = (
    "shard_id", "index", "resource_class", "modules", "case_ids",
    "estimated_duration_ns", "required_capabilities", "exclusive_resources",
    "gate_argv", "expected_outputs", "field_sha256",
)
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ContractRefusal(ValueError):
    """Deterministic, machine-readable refusal; nothing has been launched."""

    exit_code = BLOCKED_EXIT

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class BlockedObligation:
    module: str
    capability: str
    reason: str


@dataclass(frozen=True)
class Admission:
    exit_code: int
    manifest: dict[str, Any] | None
    obligations: tuple[BlockedObligation, ...]
    selected_case_ids: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.exit_code == 0 and self.manifest is not None and not self.obligations


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    success: bool
    executed_case_ids: tuple[str, ...]
    launched_shards: tuple[str, ...]
    obligations: tuple[BlockedObligation, ...] = ()
    refusal: str | None = None


@dataclass(frozen=True)
class _Module:
    module: str
    source_path: str
    source_sha256: str
    case_ids: tuple[str, ...]
    capability_class: str
    estimated_duration_ns: int
    required_capabilities: tuple[str, ...]
    exclusive_resources: tuple[str, ...]


def _fail(code: str, detail: str) -> None:
    raise ContractRefusal(code, detail)


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        _fail("INVALID_INTEGER", name)
    return value


def _text(value: Any, name: str, regex: re.Pattern[str] | None = None) -> str:
    if type(value) is not str or not value or (regex and not regex.fullmatch(value)):
        _fail("MALFORMED_DECLARATION", name)
    return value


def _digest(value: Any, name: str, regex: re.Pattern[str] = _HEX64) -> str:
    if type(value) is not str or not regex.fullmatch(value):
        _fail("INVALID_DIGEST", name)
    return value


def _list(value: Any, name: str, *, allow_empty: bool = True,
          regex: re.Pattern[str] = _TOKEN) -> tuple[str, ...]:
    if type(value) not in (list, tuple) or (not allow_empty and not value):
        _fail("MALFORMED_DECLARATION", name)
    result = tuple(_text(item, name, regex) for item in value)
    if len(result) != len(set(result)):
        _fail("DUPLICATE_DECLARATION", name)
    return result


def _canonical_bytes(value: Any) -> bytes:
    """RFC-like canonical byte representation: sorted, compact UTF-8 JSON."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validate_modules(modules: Sequence[Mapping[str, Any]]) -> tuple[_Module, ...]:
    if type(modules) not in (list, tuple) or not modules:
        _fail("MALFORMED_DECLARATION", "modules")
    fields = set(MODULE_FIELDS)
    result: list[_Module] = []
    seen_modules: set[str] = set()
    seen_cases: set[str] = set()
    for raw in modules:
        if type(raw) is not dict or set(raw) != fields:
            _fail("MALFORMED_DECLARATION", "module fields")
        name = _text(raw["module"], "module", _NAME)
        source_path = _text(raw["source_path"], "source_path")
        parts = source_path.split("/")
        if (source_path.startswith("/") or "\\" in source_path or
                any(x in ("", ".", "..") for x in parts) or
                not source_path.endswith(".py")):
            _fail("MALFORMED_DECLARATION", "source_path")
        source_sha = _digest(raw["source_sha256"], "source_sha256")
        case_ids = _list(raw["case_ids"], "case_ids", allow_empty=False, regex=_NAME)
        if any(not item.startswith(name + ".") for item in case_ids):
            _fail("MALFORMED_DECLARATION", f"case ownership: {name}")
        kind = raw["capability_class"]
        if type(kind) is not str or kind not in CLASS_NAMES:
            _fail("MALFORMED_DECLARATION", f"capability_class: {name}")
        estimate = _integer(raw["estimated_duration_ns"], "estimated_duration_ns")
        caps = _list(raw["required_capabilities"], "required_capabilities")
        exclusive = _list(raw["exclusive_resources"], "exclusive_resources")
        if (kind == "capability") != bool(caps):
            _fail("CAPABILITY_CLASS_MISMATCH", name)
        if name in seen_modules:
            _fail("DUPLICATE_DECLARATION", f"module: {name}")
        if any(case in seen_cases for case in case_ids):
            _fail("DUPLICATE_DECLARATION", f"case identity: {name}")
        seen_modules.add(name)
        seen_cases.update(case_ids)
        result.append(_Module(name, source_path, source_sha, case_ids, kind,
                              estimate, caps, exclusive))
    return tuple(sorted(result, key=lambda m: m.module))


def _validate_evidence(evidence: Mapping[str, bool | None]) -> dict[str, bool | None]:
    if type(evidence) is not dict:
        _fail("MALFORMED_EVIDENCE", "capabilities map")
    result: dict[str, bool | None] = {}
    for name, value in evidence.items():
        if type(name) is not str or not _TOKEN.fullmatch(name):
            _fail("MALFORMED_EVIDENCE", "capability name")
        if value is not None and type(value) is not bool:
            _fail("MALFORMED_EVIDENCE", name)
        result[name] = value
    return result


def _profile(value: str) -> str:
    if value not in ("offline", "live") or type(value) is not str:
        _fail("MALFORMED_DECLARATION", "profile")
    return value


def _obligations(modules: Sequence[_Module], evidence: Mapping[str, bool | None],
                 profile: str) -> tuple[BlockedObligation, ...]:
    blocked: list[BlockedObligation] = []
    for module in modules:
        for cap in module.required_capabilities:
            if profile == "offline" and cap in LIVE_ONLY:
                reason = "OFFLINE_LIVE_REQUIRED"
            elif cap not in evidence or evidence[cap] is None:
                reason = "MISSING_CAPABILITY_EVIDENCE"
            elif evidence[cap] is False:
                reason = "CAPABILITY_UNAVAILABLE"
            else:
                continue
            blocked.append(BlockedObligation(module.module, cap, reason))
    return tuple(sorted(blocked, key=lambda o: (o.module, o.capability)))


def _components(modules: Sequence[_Module]) -> list[tuple[_Module, ...]]:
    """Overlapping exclusive resources must share a sequential shard."""
    roots = list(range(len(modules)))

    def find(i: int) -> int:
        while roots[i] != i:
            roots[i] = roots[roots[i]]
            i = roots[i]
        return i

    by_resource: dict[str, int] = {}
    for i, module in enumerate(modules):
        for resource in module.exclusive_resources:
            if resource in by_resource:
                roots[find(i)] = find(by_resource[resource])
            else:
                by_resource[resource] = i
    groups: dict[int, list[_Module]] = {}
    for i, module in enumerate(modules):
        groups.setdefault(find(i), []).append(module)
    return [tuple(sorted(group, key=lambda m: m.module)) for group in groups.values()]


def _shard_modules(modules: Sequence[_Module], requested_shards: int,
                   seed: int) -> list[tuple[_Module, ...]]:
    if requested_shards > len(modules):
        _fail("INFEASIBLE_SHARDS", "requested_shards exceeds whole modules")
    groups = _components(modules)
    if len(groups) < requested_shards:
        _fail("EXCLUSIVE_RESOURCE_CONFLICT", "insufficient independent resource groups")
    def key(group: tuple[_Module, ...]) -> tuple[int, str]:
        names = [m.module for m in group]
        return (-sum(m.estimated_duration_ns for m in group),
                _sha([seed, names]))

    buckets: list[list[_Module]] = [[] for _ in range(requested_shards)]
    durations = [0] * requested_shards
    for group in sorted(groups, key=key):
        # First fill every shard, then greedily balance duration. Deterministic.
        i = min(range(requested_shards), key=lambda idx: (
            bool(buckets[idx]), durations[idx], idx))
        buckets[i].extend(group)
        durations[i] += sum(m.estimated_duration_ns for m in group)
    if any(not b for b in buckets):
        _fail("INFEASIBLE_SHARDS", "empty shard")
    return [tuple(sorted(b, key=lambda m: m.module)) for b in buckets]


def _suite_hash(modules: Sequence[_Module]) -> str:
    cases = sorted(c for m in modules for c in m.case_ids)
    sources = sorted([[m.source_path, m.source_sha256] for m in modules])
    classes = sorted([[m.module, m.capability_class] for m in modules])
    return _sha({"cases": cases, "sources": sources, "capability_classes": classes})


def _module_entry(m: _Module) -> dict[str, Any]:
    return {
        "module": m.module, "source_path": m.source_path,
        "source_sha256": m.source_sha256, "case_ids": sorted(m.case_ids),
        "capability_class": m.capability_class,
        "estimated_duration_ns": m.estimated_duration_ns,
        "required_capabilities": sorted(m.required_capabilities),
        "exclusive_resources": sorted(m.exclusive_resources),
    }


def _sealed_shard(index: int, modules: Sequence[_Module]) -> dict[str, Any]:
    names = [m.module for m in modules]
    cases = sorted(c for m in modules for c in m.case_ids)
    kinds = {m.capability_class for m in modules}
    shard: dict[str, Any] = {
        "shard_id": f"shard-{index:04d}",
        "index": index,
        "resource_class": next(iter(kinds)) if len(kinds) == 1 else "mixed",
        "modules": [_module_entry(m) for m in modules],
        "case_ids": cases,
        "estimated_duration_ns": sum(m.estimated_duration_ns for m in modules),
        "required_capabilities": sorted({c for m in modules for c in m.required_capabilities}),
        "exclusive_resources": sorted({r for m in modules for r in m.exclusive_resources}),
        "gate_argv": ["python3", "-m", "unittest", *names],
        "expected_outputs": ["unittest:success", f"case-count:{len(cases)}"],
    }
    shard["field_sha256"] = _sha(shard)
    return shard


# @anchor:wa.learn.proto1capabilities.planmanifest
def plan_manifest(*, repo: str, source_commit: str, duration_ledger_sha256: str,
                  modules: Sequence[Mapping[str, Any]], selected_case_ids: Sequence[str],
                  capability_evidence: Mapping[str, bool | None], requested_shards: int,
                  seed: int, profile: str = "offline") -> Admission:
    """Validate *everything*, then admit entire modules or retain named obligations.

    Missing/unavailable capabilities and offline-live requirements return exit 4
    with no manifest. Invalid declarations/configurations raise ContractRefusal.
    """
    _text(repo, "repo", _REPO)
    _digest(source_commit, "source_commit", _HEX40)
    _digest(duration_ledger_sha256, "duration_ledger_sha256")
    _integer(seed, "seed")
    _integer(requested_shards, "requested_shards", positive=True)
    _profile(profile)
    inspected = _validate_modules(modules)
    evidence = _validate_evidence(capability_evidence)
    selected = _list(selected_case_ids, "selected_case_ids", allow_empty=False,
                     regex=_NAME)
    want = set(selected)
    all_cases = {c for m in inspected for c in m.case_ids}
    if not want <= all_cases:
        _fail("UNKNOWN_CASE_ID", "selection includes undiscovered case")
    for m in inspected:
        subset = want.intersection(m.case_ids)
        if subset and len(subset) != len(m.case_ids):
            _fail("PARTIAL_MODULE_SELECTION", m.module)
    chosen = tuple(m for m in inspected if set(m.case_ids) <= want)
    if not chosen:
        _fail("INFEASIBLE_SHARDS", "no selected modules")
    blocked = _obligations(chosen, evidence, profile)
    chosen_cases = tuple(sorted(want))
    if blocked:
        return Admission(BLOCKED_EXIT, None, blocked, chosen_cases)
    allocations = _shard_modules(chosen, requested_shards, seed)
    shards = [_sealed_shard(i, m) for i, m in enumerate(allocations)]
    used_caps = sorted({cap for m in chosen for cap in m.required_capabilities})
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "repo": repo,
        "source_commit": source_commit,
        "suite_sha256": _suite_hash(chosen),
        "duration_ledger_sha256": duration_ledger_sha256,
        "planner_version": PLANNER_VERSION,
        "seed": seed,
        "requested_shards": requested_shards,
        "selected_case_ids": list(chosen_cases),
        "capabilities": {name: evidence[name] for name in used_caps},
        "shards": shards,
    }
    manifest["manifest_sha256"] = _sha(manifest)
    validate_manifest(manifest)
    return Admission(0, manifest, (), chosen_cases)


# @anchor:wa.learn.proto1capabilities.validatemanifest
def validate_manifest(manifest: Mapping[str, Any]) -> None:
    """Verify sealed field layouts and partition before any launcher use."""
    if type(manifest) is not dict or tuple(manifest) != MANIFEST_FIELDS:
        _fail("INVALID_MANIFEST", "manifest fields/order")
    if manifest["schema"] != SCHEMA:
        _fail("INVALID_MANIFEST", "schema")
    _text(manifest["repo"], "repo", _REPO)
    _digest(manifest["source_commit"], "source_commit", _HEX40)
    _digest(manifest["suite_sha256"], "suite_sha256")
    _digest(manifest["duration_ledger_sha256"], "duration_ledger_sha256")
    _text(manifest["planner_version"], "planner_version", _TOKEN)
    _integer(manifest["seed"], "seed")
    shards_n = _integer(manifest["requested_shards"], "requested_shards", positive=True)
    selected = _list(manifest["selected_case_ids"], "selected_case_ids",
                     allow_empty=False, regex=_NAME)
    if list(selected) != sorted(selected):
        _fail("INVALID_MANIFEST", "unsorted selected_case_ids")
    capabilities = _validate_evidence(manifest["capabilities"])
    if any(value is not True for value in capabilities.values()):
        _fail("INVALID_MANIFEST", "capability evidence not available")
    shards = manifest["shards"]
    if type(shards) is not list or len(shards) != shards_n:
        _fail("INVALID_MANIFEST", "shard count")
    all_cases: list[str] = []
    all_modules: set[str] = set()
    all_module_data: list[_Module] = []
    all_capabilities: set[str] = set()
    owned_resources: set[str] = set()
    for index, shard in enumerate(shards):
        if type(shard) is not dict or tuple(shard) != SHARD_FIELDS:
            _fail("INVALID_MANIFEST", f"shard {index} fields/order")
        if shard["shard_id"] != f"shard-{index:04d}" or (
                type(shard["index"]) is not int or shard["index"] != index):
            _fail("INVALID_MANIFEST", f"shard {index} identity")
        if shard["resource_class"] not in ("pure", "ram", "capability", "mixed"):
            _fail("INVALID_MANIFEST", "resource_class")
        specs = _validate_modules(shard["modules"])
        names = [m.module for m in specs]
        if shard["modules"] != [_module_entry(m) for m in specs]:
            _fail("INVALID_MANIFEST", "module declaration order")
        cases = _list(shard["case_ids"], "case_ids", allow_empty=False, regex=_NAME)
        expected_cases = sorted(c for m in specs for c in m.case_ids)
        if list(cases) != expected_cases:
            _fail("COVERAGE_MISMATCH", "shard module/case mismatch")
        expected_class = (specs[0].capability_class if len({m.capability_class for m in specs}) == 1
                          else "mixed")
        if shard["resource_class"] != expected_class:
            _fail("INVALID_MANIFEST", "shard capability class")
        expected_ns = sum(m.estimated_duration_ns for m in specs)
        _integer(shard["estimated_duration_ns"], "estimated_duration_ns")
        if shard["estimated_duration_ns"] != expected_ns:
            _fail("INVALID_MANIFEST", "shard estimate")
        required = _list(shard["required_capabilities"], "required_capabilities")
        exclusive = _list(shard["exclusive_resources"], "exclusive_resources")
        if list(required) != sorted({c for m in specs for c in m.required_capabilities}):
            _fail("INVALID_MANIFEST", "shard capability aggregation")
        if list(exclusive) != sorted({r for m in specs for r in m.exclusive_resources}):
            _fail("INVALID_MANIFEST", "shard exclusive resources")
        argv = shard["gate_argv"]
        expected = shard["expected_outputs"]
        if argv != ["python3", "-m", "unittest", *names] or expected != [
                "unittest:success", f"case-count:{len(cases)}"]:
            _fail("INVALID_MANIFEST", "runner invocation/output")
        if all_modules.intersection(names) or owned_resources.intersection(exclusive):
            _fail("EXCLUSIVE_RESOURCE_CONFLICT", "duplicate module or cross-shard resource")
        all_modules.update(names)
        all_module_data.extend(specs)
        owned_resources.update(exclusive)
        all_cases.extend(cases)
        all_capabilities.update(required)
        _digest(shard["field_sha256"], "field_sha256")
        if shard["field_sha256"] != _sha({k: shard[k] for k in SHARD_FIELDS[:-1]}):
            _fail("INVALID_FIELD_DIGEST", shard["shard_id"])
    if len(all_cases) != len(set(all_cases)) or sorted(all_cases) != list(selected):
        _fail("COVERAGE_MISMATCH", "selected cases differ from shard cases")
    if manifest["suite_sha256"] != _suite_hash(all_module_data):
        _fail("INVALID_SUITE_DIGEST", "suite identity")
    if set(capabilities) != all_capabilities:
        _fail("INVALID_MANIFEST", "declared capability set mismatch")
    _digest(manifest["manifest_sha256"], "manifest_sha256")
    if manifest["manifest_sha256"] != _sha({k: manifest[k] for k in MANIFEST_FIELDS[:-1]}):
        _fail("INVALID_MANIFEST_DIGEST", "manifest seal")


# @anchor:wa.learn.proto1capabilities.serializemanifest
def serialize_manifest(manifest: Mapping[str, Any]) -> bytes:
    """Emit UTF-8 JSON without newline in the *contractual* field order."""
    validate_manifest(manifest)
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def parse_manifest(raw: bytes) -> dict[str, Any]:
    """Parse exactly one JSON object, refusing duplicate fields and all floats."""
    if type(raw) is not bytes:
        _fail("INVALID_MANIFEST", "manifest bytes")
    try:
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for k, v in items:
                if k in out:
                    _fail("INVALID_MANIFEST", f"duplicate key: {k}")
                out[k] = v
            return out
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                              parse_float=lambda s: _fail("INVALID_INTEGER", s),
                              parse_constant=lambda s: _fail("INVALID_MANIFEST", s))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractRefusal("INVALID_MANIFEST", "invalid JSON/UTF-8") from exc
    validate_manifest(manifest)
    return manifest


# @anchor:wa.learn.proto1capabilities.executemanifest
def execute_manifest(manifest: Mapping[str, Any], *,
                     capability_evidence: Mapping[str, bool | None], profile: str,
                     launcher: Callable[[tuple[str, ...], tuple[str, ...], tuple[str, ...]],
                                        Mapping[str, Any]]) -> RunResult:
    """Fail-closed preflight of *all* shards, then delegate to external runner.

    The launcher receives argv, expected outputs, exact case IDs and must return
    {"returncode": exact_int, "case_ids": sequence_of_case_ids}. A successful
    return code without complete case coverage is never success.
    """
    validate_manifest(manifest)
    evidence = _validate_evidence(capability_evidence)
    _profile(profile)
    requirements = tuple(
        m for shard in manifest["shards"] for m in _validate_modules(shard["modules"])
    )
    blocked = _obligations(requirements, evidence, profile)
    if blocked:
        return RunResult(BLOCKED_EXIT, False, (), (), blocked)
    launched: list[str] = []
    executed: list[str] = []
    for shard in manifest["shards"]:
        launched.append(shard["shard_id"])
        result = launcher(tuple(shard["gate_argv"]), tuple(shard["expected_outputs"]),
                          tuple(shard["case_ids"]))
        if (type(result) is not dict or type(result.get("returncode")) is not int or
                type(result.get("case_ids")) not in (tuple, list) or
                any(type(c) is not str for c in result["case_ids"])):
            return RunResult(BLOCKED_EXIT, False, tuple(executed), tuple(launched),
                             refusal="INVALID_RUNNER_RESULT")
        reported = tuple(result["case_ids"])
        if result["returncode"] != 0:
            return RunResult(1, False, tuple(executed + list(reported)), tuple(launched),
                             refusal="CHILD_FAILED")
        if reported != tuple(shard["case_ids"]):
            return RunResult(BLOCKED_EXIT, False, tuple(executed + list(reported)),
                             tuple(launched), refusal="RUNNER_CASE_MISMATCH")
        executed.extend(reported)
    if sorted(executed) != manifest["selected_case_ids"]:
        return RunResult(BLOCKED_EXIT, False, tuple(executed), tuple(launched),
                         refusal="COVERAGE_MISMATCH")
    return RunResult(0, True, tuple(executed), tuple(launched))
