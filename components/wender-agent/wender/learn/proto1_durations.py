# @module {"id":"wa.learn.proto1_durations","layer":2,"owner":"LEARN","deps":[],"book":"PROTO1_DESIGN"}
"""Source-pinned, fail-closed unittest duration evidence, with no I/O publication.

A sample's SHA-256 is an *integrity check*, not an attestation of machine
identity or genuine clocks. ``collect_unittest_baseline`` alone measures both
real process CPU and monotonic wall nanoseconds. A consumer must supply separate
capability evidence when constructing a ledger; sample claims are not enough.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

SAMPLE_SCHEMA = "proto1.duration-sample/v1"
LEDGER_SCHEMA = "proto1.duration-ledger/v1"
SAMPLE_FIELDS = (
    "schema", "repo", "source_commit", "module", "case_count", "elapsed_ns",
    "cpu_ns", "outcome", "capabilities", "sample_sha256",
)
CLOCK_CAPABILITIES = ("cpu_process_time_ns", "wall_monotonic_ns")
_PIN = re.compile(r"[0-9a-f]{40}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")

__all__ = (
    "SAMPLE_SCHEMA", "LEDGER_SCHEMA", "SAMPLE_FIELDS", "CLOCK_CAPABILITIES",
    "DurationRefusal", "DurationSample", "DurationLedger", "DurationPlan",
    "seal_duration_sample", "parse_duration_sample_json", "validate_duration_sample",
    "canonical_duration_sample_bytes", "collect_unittest_baseline",
    "build_duration_ledger", "canonical_duration_ledger_bytes", "balance_duration_plan",
)


class DurationRefusal(ValueError):
    """Fail-closed refusal, with a machine-readable, stable ``code``."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DurationSample:
    schema: str
    repo: str
    source_commit: str
    module: str
    case_count: int
    elapsed_ns: int
    cpu_ns: int
    outcome: str
    capabilities: tuple[str, ...]
    sample_sha256: str


@dataclass(frozen=True)
class DurationLedger:
    repo: str
    source_commit: str
    required_modules: tuple[str, ...]
    samples: tuple[DurationSample, ...]
    capability_evidence: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    case_count: int
    elapsed_ns: int
    cpu_ns: int
    identity: str


@dataclass(frozen=True)
class DurationPlan:
    ledger_identity: str
    groups: tuple[tuple[str, ...], ...]
    elapsed_ns: tuple[int, ...]
    case_count: tuple[int, ...]


def _fail(code):
    raise DurationRefusal(code)


def _text(value, code, pattern=None):
    if type(value) is not str or not value or (pattern and not pattern.fullmatch(value)):
        _fail(code)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise DurationRefusal(code) from exc
    return value


def _int(value, name, positive=False):
    if type(value) is not int:
        _fail("INVALID_" + name.upper())
    if value < (1 if positive else 0):
        _fail("NEGATIVE_" + name.upper() if value < 0 else "INVALID_" + name.upper())
    return value


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _caps(value):
    if type(value) not in (tuple, list) or not value:
        _fail("INVALID_CAPABILITIES")
    if any(type(x) is not str or not x or x.strip() != x for x in value):
        _fail("INVALID_CAPABILITIES")
    result = tuple(value)
    if len(result) != len(set(result)) or result != tuple(sorted(result)):
        _fail("INVALID_CAPABILITIES")
    for cap in result:
        _text(cap, "INVALID_CAPABILITIES")
    if not set(CLOCK_CAPABILITIES).issubset(result):
        _fail("UNMEASURED_CLOCK")
    return result


def _plain_sample(sample):
    return {name: list(getattr(sample, name)) if name == "capabilities"
            else getattr(sample, name) for name in SAMPLE_FIELDS}


def _payload(dct):
    return _json_bytes({name: dct[name] for name in SAMPLE_FIELDS[:-1]})


def _from_dict(record, *, check_digest):
    if isinstance(record, Mapping) and "elapsed_ns" not in record:
        _fail("DURATION_MISSING")
    if not isinstance(record, Mapping) or set(record) != set(SAMPLE_FIELDS):
        _fail("INVALID_SAMPLE_FIELDS")
    if record["schema"] != SAMPLE_SCHEMA:
        _fail("INVALID_SAMPLE_SCHEMA")
    repo = _text(record["repo"], "INVALID_REPO", _REPO)
    pin = _text(record["source_commit"], "INVALID_SOURCE_PIN", _PIN)
    module = _text(record["module"], "INVALID_MODULE", _MODULE)
    count = _int(record["case_count"], "case_count", positive=True)
    if "elapsed_ns" not in record:  # A missing duration must never become zero.
        _fail("DURATION_MISSING")
    elapsed = _int(record["elapsed_ns"], "elapsed_ns")
    cpu = _int(record["cpu_ns"], "cpu_ns")
    if record["outcome"] != "PASS":
        _fail("PROBE_FAILED")
    caps = _caps(record["capabilities"])
    digest = _text(record["sample_sha256"], "INVALID_SAMPLE_DIGEST", _DIGEST)
    sample = DurationSample(SAMPLE_SCHEMA, repo, pin, module, count,
                            elapsed, cpu, "PASS", caps, digest)
    if check_digest and hashlib.sha256(_payload(_plain_sample(sample))).hexdigest() != digest:
        _fail("SAMPLE_DIGEST_MISMATCH")
    return sample


def seal_duration_sample(*, repo, source_commit, module, case_count,
                         elapsed_ns, cpu_ns, outcome="PASS",
                         capabilities=CLOCK_CAPABILITIES):
    """Make canonical JSON-compatible data; caller-supplied clocks are NOT measured."""
    record = dict(zip(SAMPLE_FIELDS, (
        SAMPLE_SCHEMA, repo, source_commit, module, case_count, elapsed_ns,
        cpu_ns, outcome, list(capabilities) if type(capabilities) in (tuple, list)
        else capabilities, "0" * 64,
    )))
    sample = _from_dict(record, check_digest=False)
    record = _plain_sample(sample)
    record["sample_sha256"] = hashlib.sha256(_payload(record)).hexdigest()
    return record


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def parse_duration_sample_json(data):
    """Strict UTF-8 parser: duplicate keys fail at every nesting depth."""
    if type(data) is bytes:
        try:
            data = data.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise DurationRefusal("INVALID_JSON") from exc
    if type(data) is not str:
        _fail("INVALID_JSON")
    try:
        parsed = json.loads(data, object_pairs_hook=_unique_pairs,
                            parse_constant=lambda _: _fail("INVALID_JSON"))
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, DurationRefusal):
            raise
        raise DurationRefusal("INVALID_JSON") from exc
    return _from_dict(parsed, check_digest=True)


def validate_duration_sample(record, *, repo, source_commit, module=None):
    """Return an immutable, digest-verified sample under a required source pin."""
    _text(repo, "INVALID_REPO", _REPO)
    _text(source_commit, "INVALID_SOURCE_PIN", _PIN)
    if module is not None:
        _text(module, "INVALID_MODULE", _MODULE)
    if type(record) in (str, bytes):
        sample = parse_duration_sample_json(record)
    elif isinstance(record, DurationSample):
        sample = _from_dict(_plain_sample(record), check_digest=True)
    else:
        # Check presence before shape, so elapsed_ns omission has an exact refusal.
        if isinstance(record, Mapping) and "elapsed_ns" not in record:
            _fail("DURATION_MISSING")
        sample = _from_dict(record, check_digest=True)
    if sample.repo != repo or sample.source_commit != source_commit:
        _fail("STALE_SOURCE_PIN")
    if module is not None and sample.module != module:
        _fail("WRONG_MODULE")
    return sample


def canonical_duration_sample_bytes(record):
    """Full ordered JSON sample, including SHA, without any whitespace."""
    if type(record) in (str, bytes):
        sample = parse_duration_sample_json(record)
    elif isinstance(record, DurationSample):
        sample = _from_dict(_plain_sample(record), check_digest=True)
    else:
        if isinstance(record, Mapping) and "elapsed_ns" not in record:
            _fail("DURATION_MISSING")
        sample = _from_dict(record, check_digest=True)
    return _json_bytes(_plain_sample(sample))


def collect_unittest_baseline(test_case, *, repo, source_commit, module):
    """Run a real TestCase subclass and capture monotonic and process CPU ns.

    No clock injection, fixture filesystem, network, skipped tests or hidden
    failed/expected-failure baselines are supported. Raw failure output stays
    in local RAM; this API never releases it as duration evidence.
    """
    _text(repo, "INVALID_REPO", _REPO)
    _text(source_commit, "INVALID_SOURCE_PIN", _PIN)
    _text(module, "INVALID_MODULE", _MODULE)
    if not isinstance(test_case, type) or not issubclass(test_case, unittest.TestCase):
        _fail("INVALID_TEST_CASE")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_case)
    count = suite.countTestCases()
    _int(count, "case_count", positive=True)
    stdout = io.StringIO()
    runner = unittest.TextTestRunner(stream=stdout, verbosity=0)
    start_wall, start_cpu = time.monotonic_ns(), time.process_time_ns()
    result = runner.run(suite)
    stop_cpu, stop_wall = time.process_time_ns(), time.monotonic_ns()
    if (result.testsRun != count or not result.wasSuccessful()
            or result.errors or result.failures or result.skipped
            or result.expectedFailures or result.unexpectedSuccesses):
        _fail("PROBE_FAILED")
    if stop_wall < start_wall or stop_cpu < start_cpu:
        _fail("CLOCK_REGRESSION")
    return seal_duration_sample(
        repo=repo, source_commit=source_commit, module=module,
        case_count=count, elapsed_ns=stop_wall - start_wall,
        cpu_ns=stop_cpu - start_cpu, capabilities=CLOCK_CAPABILITIES,
    )


def _capability_evidence(value, modules, samples):
    """Only explicit external declarations can authorize a ledger."""
    if not isinstance(value, Mapping) or set(value) != set(modules):
        _fail("CAPABILITY_EVIDENCE_MISSING")
    indexed = {s.module: s for s in samples}
    output = []
    for module in modules:
        evidence = value[module]
        if not isinstance(evidence, Mapping):
            _fail("CAPABILITY_EVIDENCE_MISSING")
        if set(evidence) != set(indexed[module].capabilities):
            _fail("CAPABILITY_EVIDENCE_MISSING")
        rows = []
        for capability in indexed[module].capabilities:
            reason = _text(evidence[capability], "CAPABILITY_EVIDENCE_MISSING")
            if not reason.strip():
                _fail("CAPABILITY_EVIDENCE_MISSING")
            rows.append((capability, reason))
        output.append((module, tuple(rows)))
    return tuple(output)


def _ledger_body(ledger):
    return {
        "schema": LEDGER_SCHEMA,
        "repo": ledger.repo, "source_commit": ledger.source_commit,
        "required_modules": list(ledger.required_modules),
        "samples": [_plain_sample(sample) for sample in ledger.samples],
        "capability_evidence": {m: dict(pairs) for m, pairs in ledger.capability_evidence},
        "case_count": ledger.case_count, "elapsed_ns": ledger.elapsed_ns,
        "cpu_ns": ledger.cpu_ns,
    }


def _check_ledger(ledger):
    if not isinstance(ledger, DurationLedger):
        _fail("INVALID_LEDGER")
    # Integrity is checked again before plan acceptance or ledger serialization.
    if tuple(sorted(ledger.required_modules)) != ledger.required_modules:
        _fail("LEDGER_INTEGRITY")
    if len(ledger.samples) != len(ledger.required_modules):
        _fail("LEDGER_INTEGRITY")
    if tuple(s.module for s in ledger.samples) != ledger.required_modules:
        _fail("LEDGER_INTEGRITY")
    for sample in ledger.samples:
        try:
            validate_duration_sample(sample, repo=ledger.repo,
                                     source_commit=ledger.source_commit)
        except DurationRefusal as exc:
            raise DurationRefusal("LEDGER_INTEGRITY") from exc
    if (ledger.case_count != sum(x.case_count for x in ledger.samples)
            or ledger.elapsed_ns != sum(x.elapsed_ns for x in ledger.samples)
            or ledger.cpu_ns != sum(x.cpu_ns for x in ledger.samples)):
        _fail("LEDGER_INTEGRITY")
    expected = hashlib.sha256(_json_bytes(_ledger_body(ledger))).hexdigest()
    if ledger.identity != expected:
        _fail("LEDGER_INTEGRITY")
    return ledger


def build_duration_ledger(records, *, repo, source_commit, required_modules,
                          declared_capabilities):
    """Conserve all required samples; do not admit incomplete or stale corpora."""
    _text(repo, "INVALID_REPO", _REPO)
    _text(source_commit, "INVALID_SOURCE_PIN", _PIN)
    if type(required_modules) not in (tuple, list) or not required_modules:
        _fail("INVALID_REQUIRED_MODULES")
    modules = tuple(required_modules)
    if any(type(m) is not str for m in modules) or len(modules) != len(set(modules)):
        _fail("INVALID_REQUIRED_MODULES")
    for module in modules:
        _text(module, "INVALID_REQUIRED_MODULES", _MODULE)
    modules = tuple(sorted(modules))
    if type(records) not in (tuple, list) or not records:
        _fail("DURATION_MISSING")
    by_module = {}
    for record in records:
        sample = validate_duration_sample(record, repo=repo, source_commit=source_commit)
        if sample.module in by_module:
            _fail("DUPLICATE_SAMPLE")
        by_module[sample.module] = sample
    if set(by_module) != set(modules):
        _fail("DURATION_MISSING")
    samples = tuple(by_module[m] for m in modules)
    evidence = _capability_evidence(declared_capabilities, modules, samples)
    ledger = DurationLedger(repo, source_commit, modules, samples, evidence,
                            sum(s.case_count for s in samples),
                            sum(s.elapsed_ns for s in samples),
                            sum(s.cpu_ns for s in samples), "")
    return DurationLedger(repo, source_commit, modules, samples, evidence,
                          ledger.case_count, ledger.elapsed_ns, ledger.cpu_ns,
                          hashlib.sha256(_json_bytes(_ledger_body(ledger))).hexdigest())


def canonical_duration_ledger_bytes(ledger):
    """Integrity-checked deterministic serialization; never writes a ledger."""
    _check_ledger(ledger)
    return _json_bytes({**_ledger_body(ledger), "identity": ledger.identity})


def balance_duration_plan(ledger, *, workers=2):
    """Least-loaded deterministic bin assignment using genuine sample durations.

    This is an in-memory proposal, never an actuation or published acceptance.
    """
    _check_ledger(ledger)
    _int(workers, "workers", positive=True)
    groups = [[] for _ in range(workers)]
    elapsed = [0] * workers
    cases = [0] * workers
    for sample in sorted(ledger.samples, key=lambda s: (-s.elapsed_ns, s.module)):
        dest = min(range(workers), key=lambda i: (elapsed[i], i))
        groups[dest].append(sample.module)
        elapsed[dest] += sample.elapsed_ns
        cases[dest] += sample.case_count
    if sum(elapsed) != ledger.elapsed_ns or sum(cases) != ledger.case_count:
        _fail("LEDGER_INTEGRITY")
    return DurationPlan(ledger.identity, tuple(tuple(g) for g in groups),
                        tuple(elapsed), tuple(cases))
