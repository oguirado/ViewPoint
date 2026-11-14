"""Exact proto1 duration evidence and fail-closed cold-start unittest collection.

Public API (Python 3, standard library only):

* canonical_bytes(value) -> bytes; decode_canonical(raw) -> object
* make_sample(*, repo, source_commit, module, case_count, elapsed_ns,
  cpu_ns, outcome, capabilities) -> dict
* validate_sample(record, *, repo, source_commit) -> detached dict
* make_ledger(samples, *, repo, source_commit, required_modules) -> dict
* validate_ledger(ledger, *, repo, source_commit, required_modules) -> dict
* lookup_duration(ledger, module, *, repo, source_commit,
  required_modules, available_capabilities) -> exact elapsed_ns integer
* probe_module(module, *, repo, source_commit, capabilities) -> ProbeResult
* probe_module_cold(module, *, repo, source_commit, capabilities,
  module_root=None) -> ProbeResult (fresh Python interpreter)
* collect_cold_start(requirements, *, repo, source_commit,
  available_capabilities, module_root=None) -> DurationJournal
* DurationJournal(repo, source_commit).record(probe_result),
  .admit(required_modules), .lookup(module, available_capabilities)

A module is a canonical dotted ``tests.test_*`` Python module, never a
filesystem path. Capabilities are explicit, sorted, unique lowercase tokens.
Only the actual unittest probe issues process-clock observations; callers may
make deterministic test vectors, but such arithmetic fixtures are NOT evidence
of host timings. A SHA-256 seal establishes byte identity, not clock provenance.
Only ProbeResult objects minted by the real probe can enter DurationJournal.
The journal retains unsuccessful observations with reasons and cannot seal them
as planning baselines. No planner, scheduler, receipt, or sibling is imported.

The proto1.duration-ledger/v1 wire schema defined here is a foundation for a
future A-manifest consumer; no suite-manifest/v1 is emitted or accepted here.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping
import unittest


SAMPLE_SCHEMA = "proto1.duration-sample/v1"
LEDGER_SCHEMA = "proto1.duration-ledger/v1"
REPOSITORY = "vision-engine"
SAMPLE_FIELDS = (
    "schema", "repo", "source_commit", "module", "case_count",
    "elapsed_ns", "cpu_ns", "outcome", "capabilities", "sample_sha256",
)
LEDGER_FIELDS = (
    "schema", "repo", "source_commit", "required_modules", "samples",
    "ledger_sha256",
)
_OUTCOMES = frozenset(("passed", "failed", "censored"))
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA64 = re.compile(r"[0-9a-f]{64}\Z")
_MODULE = re.compile(r"tests\.test_[A-Za-z0-9_]+\Z")
_CAP = re.compile(r"[a-z][a-z0-9_-]*\Z")
_OBSERVED_PROBE = object()


class DurationRefusal(ValueError):
    """A typed, non-successful admission or lookup (inspect ``code``)."""

    def __init__(self, code: str, message: str, *, obligations: tuple[str, ...] = ()) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.obligations = obligations


def _refuse(code: str, message: str) -> None:
    raise DurationRefusal(code, message)


def _check_json(value: Any) -> None:
    """Strict integer-only JSON tree: notably bool is never an integer."""
    if value is None:
        return
    if type(value) is bool:
        _refuse("E_JSON_TYPE", "boolean value is forbidden")
    if type(value) is int:
        return
    if type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise DurationRefusal("E_JSON_TYPE", "invalid Unicode string") from exc
        return
    if type(value) is list or type(value) is tuple:
        for item in value:
            _check_json(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _refuse("E_JSON_TYPE", "JSON keys must be strings")
            _check_json(key)
            _check_json(item)
        return
    _refuse("E_JSON_TYPE", "non-integer numeric or unsupported JSON value")


def canonical_bytes(value: Any) -> bytes:
    """UTF-8 JSON, sorted keys, compact, exact integers, and NO newline."""
    _check_json(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":")).encode("utf-8")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _refuse("E_DUPLICATE_KEY", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_float(_: str) -> Any:
    _refuse("E_JSON_TYPE", "floating-point JSON is forbidden")


def decode_canonical(raw: bytes | str) -> Any:
    """Reject duplicate keys, invalid types, and noncanonical input bytes."""
    if type(raw) is str:
        try:
            raw_bytes = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise DurationRefusal("E_JSON_TYPE", "invalid Unicode input") from exc
    elif type(raw) is bytes:
        raw_bytes = raw
    else:
        _refuse("E_JSON_TYPE", "JSON input must be bytes or text")
    try:
        decoded = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DurationRefusal("E_JSON_TYPE", "invalid UTF-8 bytes") from exc
    try:
        obj = json.loads(decoded, object_pairs_hook=_unique_pairs,
                         parse_float=_reject_float, parse_constant=_reject_float)
    except json.JSONDecodeError as exc:
        raise DurationRefusal("E_JSON_SYNTAX", "invalid JSON") from exc
    if canonical_bytes(obj) != raw_bytes:
        _refuse("E_NONCANONICAL_JSON", "JSON must be compact, sorted and newline-free")
    return obj


def _check_source(repo: str, source_commit: str) -> None:
    if type(repo) is not str or repo != REPOSITORY:
        _refuse("E_REPO", "repository must be vision-engine")
    if type(source_commit) is not str or _SHA40.fullmatch(source_commit) is None:
        _refuse("E_SOURCE_COMMIT", "source commit must be 40 lowercase hex characters")


def _check_module(module: Any) -> str:
    if type(module) is not str or _MODULE.fullmatch(module) is None:
        _refuse("E_MODULE_PATH", "module must be canonical tests.test_* dotted notation")
    return module


def _capabilities(value: Iterable[str]) -> list[str]:
    if type(value) is not list and type(value) is not tuple:
        _refuse("E_CAPABILITIES", "capabilities must be a list or tuple")
    caps = list(value)
    if any(type(cap) is not str or _CAP.fullmatch(cap) is None for cap in caps):
        _refuse("E_CAPABILITIES", "invalid capability token")
    if len(caps) != len(set(caps)):
        _refuse("E_CAPABILITIES", "capability tokens must be unique")
    return sorted(caps)


def _check_ordered_capabilities(value: Any) -> list[str]:
    if type(value) is not list:
        _refuse("E_CAPABILITIES", "wire capabilities must be an array")
    caps = _capabilities(value)
    if caps != value:
        _refuse("E_CAPABILITIES", "wire capabilities must be sorted")
    return caps


def _check_modules(required_modules: Iterable[str]) -> list[str]:
    if type(required_modules) is not tuple and type(required_modules) is not list:
        _refuse("E_MODULE_SET", "required_modules must be an explicit array")
    modules = [_check_module(m) for m in required_modules]
    if not modules or len(modules) != len(set(modules)):
        _refuse("E_MODULE_SET", "required_modules must be nonempty and unique")
    return sorted(modules)


def _check_sha(value: Any, field_name: str) -> None:
    if type(value) is not str or _SHA64.fullmatch(value) is None:
        _refuse("E_DIGEST_TYPE", f"{field_name} must be 64 lowercase hex characters")


def _seal(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def make_sample(*, repo: str, source_commit: str, module: str,
                case_count: int, elapsed_ns: int, cpu_ns: int,
                outcome: str, capabilities: Iterable[str]) -> dict[str, Any]:
    """Build one sealed record; deterministic inputs are NOT live measurements."""
    _check_source(repo, source_commit)
    _check_module(module)
    if type(case_count) is not int or case_count <= 0:
        _refuse("E_CASE_COUNT", "case_count must be a positive exact integer")
    for name, timing in (("elapsed_ns", elapsed_ns), ("cpu_ns", cpu_ns)):
        if timing is None:
            _refuse("E_DURATION_MISSING", f"{name} is missing")
        if type(timing) is not int:
            _refuse("E_TIME_TYPE", f"{name} must be an exact integer")
        if timing < 0:
            _refuse("E_TIME_NEGATIVE", f"{name} must be nonnegative")
    if type(outcome) is not str or outcome not in _OUTCOMES:
        _refuse("E_OUTCOME", "outcome must be passed, failed, or censored")
    caps = _capabilities(capabilities)
    payload = dict(zip(SAMPLE_FIELDS[:-1], (
        SAMPLE_SCHEMA, repo, source_commit, module, case_count,
        elapsed_ns, cpu_ns, outcome, caps,
    )))
    return {**payload, "sample_sha256": _seal(payload)}


def _keys(record: Any, expected: tuple[str, ...]) -> None:
    if type(record) is not dict or set(record) != set(expected):
        _refuse("E_KEYS", "record has missing or unknown fields")


def validate_sample(record: Mapping[str, Any], *, repo: str,
                    source_commit: str) -> dict[str, Any]:
    """Revalidate every field and content digest without mutating the caller."""
    _check_source(repo, source_commit)
    if type(record) is not dict:
        _refuse("E_KEYS", "sample must be an object")
    if "elapsed_ns" not in record or "cpu_ns" not in record:
        _refuse("E_DURATION_MISSING", "elapsed_ns or cpu_ns absent")
    _keys(record, SAMPLE_FIELDS)
    if record["schema"] != SAMPLE_SCHEMA:
        _refuse("E_SCHEMA", "wrong duration sample schema")
    if record["repo"] != repo:
        _refuse("E_REPO", "wrong sample repository")
    if record["source_commit"] != source_commit:
        _refuse("E_SOURCE_SEAL", "sample belongs to a different source commit")
    _check_ordered_capabilities(record["capabilities"])
    rebuilt = make_sample(repo=repo, source_commit=source_commit,
                          module=record["module"], case_count=record["case_count"],
                          elapsed_ns=record["elapsed_ns"], cpu_ns=record["cpu_ns"],
                          outcome=record["outcome"], capabilities=record["capabilities"])
    _check_sha(record["sample_sha256"], "sample_sha256")
    if rebuilt["sample_sha256"] != record["sample_sha256"]:
        _refuse("E_SAMPLE_HASH", "sample digest does not match actual record")
    return rebuilt


def make_ledger(samples: Iterable[Mapping[str, Any]], *, repo: str,
                source_commit: str, required_modules: Iterable[str]) -> dict[str, Any]:
    """Atomically seal only fully covered successful modules, never zero-fill."""
    _check_source(repo, source_commit)
    required = _check_modules(required_modules)
    if type(samples) is not list and type(samples) is not tuple:
        _refuse("E_SAMPLES", "samples must be an explicit array")
    found: dict[str, dict[str, Any]] = {}
    for raw in samples:
        sample = validate_sample(raw, repo=repo, source_commit=source_commit)
        key = sample["module"]
        if key in found and sample != found[key]:
            _refuse("E_DUPLICATE_CONFLICT", f"conflicting observations for {key}")
        found[key] = sample
    missing = sorted(set(required) - set(found))
    if missing:
        _refuse("E_SAMPLE_ABSENT", "no measured sample for: " + ",".join(missing))
    if set(found) != set(required):
        _refuse("E_MODULE_SET", "observations contain modules outside requested coverage")
    failures = sorted(k for k, sample in found.items() if sample["outcome"] != "passed")
    if failures:
        _refuse("E_UNSUCCESSFUL", "failed or censored duration probes: " + ",".join(failures))
    payload = {
        "schema": LEDGER_SCHEMA, "repo": repo, "source_commit": source_commit,
        "required_modules": required,
        "samples": [found[module] for module in required],
    }
    return {**payload, "ledger_sha256": _seal(payload)}


def validate_ledger(ledger: Mapping[str, Any], *, repo: str,
                    source_commit: str, required_modules: Iterable[str]) -> dict[str, Any]:
    """Check the outer seal AND all nested sample identities and outcomes."""
    _check_source(repo, source_commit)
    required = _check_modules(required_modules)
    _keys(ledger, LEDGER_FIELDS)
    if ledger["schema"] != LEDGER_SCHEMA:
        _refuse("E_SCHEMA", "wrong duration ledger schema")
    if ledger["repo"] != repo:
        _refuse("E_REPO", "wrong ledger repository")
    if ledger["source_commit"] != source_commit:
        _refuse("E_SOURCE_SEAL", "ledger belongs to a different source commit")
    if ledger["required_modules"] != required:
        _refuse("E_MODULE_SET", "ledger coverage differs from requested coverage")
    rebuilt = make_ledger(ledger["samples"], repo=repo,
                          source_commit=source_commit, required_modules=required)
    _check_sha(ledger["ledger_sha256"], "ledger_sha256")
    if ledger["samples"] != rebuilt["samples"] or ledger["ledger_sha256"] != rebuilt["ledger_sha256"]:
        _refuse("E_LEDGER_HASH", "ledger body or digest is not canonical")
    return rebuilt


def lookup_duration(ledger: Mapping[str, Any], module: str, *, repo: str,
                    source_commit: str, required_modules: Iterable[str],
                    available_capabilities: Iterable[str]) -> int:
    """Return an exact measured baseline or explicitly refuse the obligation."""
    _check_module(module)
    valid = validate_ledger(ledger, repo=repo, source_commit=source_commit,
                            required_modules=required_modules)
    available = set(_capabilities(available_capabilities))
    for sample in valid["samples"]:
        if sample["module"] == module:
            blocked = sorted(set(sample["capabilities"]) - available)
            if blocked:
                raise DurationRefusal("E_CAPABILITY_UNAVAILABLE",
                                      "unmet live capabilities for " + module,
                                      obligations=tuple(blocked))
            return sample["elapsed_ns"]
    _refuse("E_SAMPLE_ABSENT", f"missing successful sample: {module}")


@dataclass(frozen=True)
class ProbeResult:
    """Unittest execution evidence; ``reason`` is never a hash field."""

    sample: dict[str, Any]
    reason: str | None
    _provenance: object = field(default=None, repr=False, compare=False)


def probe_module(module: str, *, repo: str, source_commit: str,
                 capabilities: Iterable[str]) -> ProbeResult:
    """Run a real module once, including cold import/discovery, using two clocks.

    No artificial test success: skipped/xfail/ran-less probes are censored.
    Failures remain evidence records, never a successful scheduling baseline.
    """
    _check_source(repo, source_commit)
    _check_module(module)
    caps = _capabilities(capabilities)
    start_elapsed = time.monotonic_ns()
    start_cpu = time.process_time_ns()
    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    expected = suite.countTestCases()
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=0).run(suite)
    elapsed_ns = time.monotonic_ns() - start_elapsed
    cpu_ns = time.process_time_ns() - start_cpu
    if result.testsRun <= 0:
        _refuse("E_NO_CASES", "the unittest module executed no cases")
    failures = len(result.failures)
    errors = len(result.errors)
    skipped = len(result.skipped)
    expected_failures = len(result.expectedFailures)
    unexpected_successes = len(result.unexpectedSuccesses)
    if result.testsRun != expected or skipped or expected_failures:
        outcome = "censored"
    elif failures or errors or unexpected_successes or not result.wasSuccessful():
        outcome = "failed"
    else:
        outcome = "passed"
    reason = None
    if outcome != "passed":
        reason = (f"ran={result.testsRun}; expected={expected}; failures={failures}; "
                  f"errors={errors}; skipped={skipped}; expected_failures={expected_failures}; "
                  f"unexpected_successes={unexpected_successes}")
    sample = make_sample(repo=repo, source_commit=source_commit, module=module,
                         case_count=result.testsRun, elapsed_ns=elapsed_ns,
                         cpu_ns=cpu_ns, outcome=outcome, capabilities=caps)
    return ProbeResult(sample, reason, _OBSERVED_PROBE)


# A fresh Python interpreter is essential for a *cold* import/discovery probe.
# The child executes the production probe_module, never a synthetic timing stub.
# STDOUT is a single canonical data message; extra output fails admission.
_COLD_CHILD = """import contextlib
import io
import json
import sys
# Import the owned production module before adding the fixture/module root.
sys.path.insert(0, sys.argv[5])
from eval.v2eval.proto1_durations import canonical_bytes, probe_module
sys.path.insert(0, sys.argv[6])
with contextlib.redirect_stdout(io.StringIO()):
    observation = probe_module(sys.argv[1], repo=sys.argv[2],
                               source_commit=sys.argv[3],
                               capabilities=json.loads(sys.argv[4]))
sys.stdout.buffer.write(canonical_bytes({"sample": observation.sample,
                                         "reason": observation.reason}))
"""


def probe_module_cold(module: str, *, repo: str, source_commit: str,
                      capabilities: Iterable[str],
                      module_root: str | Path | None = None) -> ProbeResult:
    """Measure one real unittest module in a fresh Python subprocess.

    The clocks cover cold import/discovery and test execution, not Python
    interpreter startup. A subprocess crash cannot mint a success record.
    ``module_root`` selects a checkout/test-fixture working directory; it is
    not a module identifier, and is never included in a digest.
    """
    _check_source(repo, source_commit)
    _check_module(module)
    caps = _capabilities(capabilities)
    if module_root is None:
        root = Path.cwd()
    elif isinstance(module_root, (str, os.PathLike)):
        root = Path(module_root)
    else:
        _refuse("E_EXECUTION_ROOT", "module_root must be a filesystem directory")
    if not root.is_dir():
        _refuse("E_EXECUTION_ROOT", "module_root is not a directory")
    root = root.resolve()
    # The owned production module is imported from the same checkout as this
    # file even when temporary source-only unit fixture modules reside in root.
    package_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(package_root) + (os.pathsep + inherited if inherited else "")
    argv = [sys.executable, "-c", _COLD_CHILD, module, repo,
            source_commit, json.dumps(caps, separators=(",", ":")),
            str(package_root), str(root)]
    try:
        process = subprocess.run(argv, cwd=str(root), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 check=False)
    except OSError as exc:
        raise DurationRefusal("E_PROBE_EXEC", "fresh interpreter failed to start") from exc
    if process.returncode != 0:
        _refuse("E_PROBE_EXEC", f"fresh interpreter exited {process.returncode}")
    message = decode_canonical(process.stdout)
    if type(message) is not dict or set(message) != {"sample", "reason"}:
        _refuse("E_PROBE_PROTOCOL", "fresh interpreter returned an invalid envelope")
    sample = validate_sample(message["sample"], repo=repo,
                             source_commit=source_commit)
    reason = message["reason"]
    if sample["outcome"] == "passed":
        if reason is not None:
            _refuse("E_PROBE_REASON", "passed sample returned a failure reason")
    elif type(reason) is not str or not reason:
        _refuse("E_PROBE_REASON", "unsuccessful sample has no reason")
    return ProbeResult(sample, reason, _OBSERVED_PROBE)


class DurationJournal:
    """Append-only actual probe journal; admission is transactional."""

    def __init__(self, repo: str, source_commit: str) -> None:
        _check_source(repo, source_commit)
        self.repo = repo
        self.source_commit = source_commit
        self._observations: list[dict[str, Any]] = []
        self._reasons: list[str | None] = []
        self._ledger: dict[str, Any] | None = None
        self.admitted_ledgers = 0

    @property
    def observations(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(self._observations))

    @property
    def failed_records(self) -> tuple[tuple[dict[str, Any], str], ...]:
        return tuple((copy.deepcopy(record), reason)
                     for record, reason in zip(self._observations, self._reasons)
                     if reason is not None)

    @property
    def successful_samples(self) -> int:
        return 0 if self._ledger is None else len(self._ledger["samples"])

    def record(self, probe: ProbeResult) -> None:
        if self._ledger is not None:
            _refuse("E_LEDGER_SEALED", "cannot append after admission")
        if type(probe) is not ProbeResult or probe._provenance is not _OBSERVED_PROBE:
            _refuse("E_PROVENANCE", "record requires actual unittest execution")
        sample = validate_sample(probe.sample, repo=self.repo,
                                 source_commit=self.source_commit)
        if sample["outcome"] == "passed":
            if probe.reason is not None:
                _refuse("E_PROBE_REASON", "passed observation cannot have failure reason")
        elif type(probe.reason) is not str or not probe.reason:
            _refuse("E_PROBE_REASON", "unsuccessful observation needs a reason")
        self._observations.append(sample)
        self._reasons.append(probe.reason)

    def admit(self, required_modules: Iterable[str]) -> dict[str, Any]:
        if self._ledger is not None:
            _refuse("E_LEDGER_SEALED", "ledger was already admitted")
        ledger = make_ledger(self._observations, repo=self.repo,
                             source_commit=self.source_commit,
                             required_modules=required_modules)
        self._ledger = ledger
        self.admitted_ledgers += 1
        return copy.deepcopy(ledger)

    def lookup(self, module: str, *, available_capabilities: Iterable[str]) -> int:
        if self._ledger is None:
            _refuse("E_LEDGER_UNADMITTED", "no successful ledger was admitted")
        return lookup_duration(self._ledger, module, repo=self.repo,
                               source_commit=self.source_commit,
                               required_modules=self._ledger["required_modules"],
                               available_capabilities=available_capabilities)


def collect_cold_start(requirements: Mapping[str, Iterable[str]], *, repo: str,
                       source_commit: str, available_capabilities: Iterable[str],
                       module_root: str | Path | None = None) -> DurationJournal:
    """Collect exactly one live unittest observation for each declared module.

    All capability obligations are validated before any test execution. The
    returned journal preserves failures; a later ``admit`` fails closed.
    """
    _check_source(repo, source_commit)
    if type(requirements) is not dict or not requirements:
        _refuse("E_MODULE_SET", "nonempty module/capability mapping required")
    modules = _check_modules(list(requirements))
    available = set(_capabilities(available_capabilities))
    caps_by_module = {module: _capabilities(requirements[module]) for module in modules}
    blocked = tuple(sorted({cap for caps in caps_by_module.values()
                            for cap in caps if cap not in available}))
    if blocked:
        raise DurationRefusal("E_CAPABILITY_UNAVAILABLE", "unmet cold-start obligations",
                              obligations=blocked)
    journal = DurationJournal(repo, source_commit)
    for module in modules:
        journal.record(probe_module_cold(module, repo=repo,
                                         source_commit=source_commit,
                                         capabilities=caps_by_module[module],
                                         module_root=module_root))
    return journal
