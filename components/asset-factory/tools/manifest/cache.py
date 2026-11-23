"""Immutable, evidence-bound F0 cache. Reuse requires lookup's current decision.

publish qualifies stored evidence using the manifest's gate names; it cannot
authorize reuse because its frozen interface has no current licence decision.
Unknown evidence families never establish PASS. Only U5 bridge verdicts carry
positive evidence here; foreign vqa.report/v1 FAIL is preserved and translated.
"""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time

from asset_factory import core

# Resource policies [TARGET]; no unbounded file read or retry/lock recovery.
MAX_JSON_BYTES = 16 * 2**20
MAX_TOTAL_BYTES = 8 * 2**30
MAX_FILES = 512
TIMEOUT_S = 30
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERDICTS = ("PASS", "FAIL", "CANNOT_SEE", "ERROR", "SKIP", "UNCLEAR", "ADVISORY", "INCOMPLETE")
_STATES = {0: "HIT_PASS", 2: "ERROR", 3: "CANNOT_SEE", 4: "STALE", 5: "HIT_REJECTED", 6: "BUDGET_EXCEEDED"}
_SCHEMAS = Path(__file__).with_name("schemas")
_SCHEMA_MAX_BYTES = 2**20


class _Refusal(Exception):
    def __init__(self, rc, reason):
        self.rc, self.reason = rc, reason


class _Budget:
    def __init__(self):
        self.deadline = time.monotonic() + TIMEOUT_S
        self.bytes = 0

    def check(self, size=0):
        self.bytes += size
        if self.bytes > MAX_TOTAL_BYTES or time.monotonic() >= self.deadline:
            raise _Refusal(6, "budget_exceeded")


def _require(condition, reason="schema_invalid"):
    if not condition:
        raise core.ContractError(reason)


def _digest(value):
    _require(isinstance(value, str) and _HASH.fullmatch(value) is not None, "invalid_digest")


def _text(value):
    _require(isinstance(value, str) and bool(value) and "\0" not in value)


def _integer_tree(value, depth=0):
    _require(depth <= 64, "nesting_limit")
    if isinstance(value, dict):
        _require(all(isinstance(k, str) for k in value))
        for child in value.values():
            _integer_tree(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _integer_tree(child, depth + 1)
    elif value is not None and type(value) not in (str, int, bool):
        raise core.ContractError("non_integer_number")


def _shape(value, schema):
    """Enforce local schemas even while the prescribed minimal stub is in use."""
    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                _shape(value, branch)
                matches += 1
            except core.ContractError:
                pass
        _require(matches == 1)
        return
    if "const" in schema:
        _require(type(value) is type(schema["const"]) and value == schema["const"])
    if "enum" in schema:
        _require(any(type(value) is type(v) and value == v for v in schema["enum"]))
    if "type" in schema:
        kinds = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
        allowed = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        _require(any(type(value) is kinds[k] for k in allowed))
    if isinstance(value, dict):
        _require(all(k in value for k in schema.get("required", [])), "missing_required")
        props = schema.get("properties", {})
        for key, child in value.items():
            if key in props:
                _shape(child, props[key])
            else:
                _require(schema.get("additionalProperties", True) is not False, "unknown_property")
    if isinstance(value, list):
        _require(len(value) >= schema.get("minItems", 0))
        for child in value:
            _shape(child, schema.get("items", {}))


def _validate(value, name):
    _integer_tree(value)
    schema = core.load_json(_SCHEMAS / (name + ".schema.json"), max_bytes=_SCHEMA_MAX_BYTES)
    # Schema metadata is descriptive; it is outside the kernel validator subset.
    shape = {k: v for k, v in schema.items() if k not in ("$id", "$schema")}
    _shape(value, shape)
    core.validate(value, shape)


def _path(root, relative):
    _text(relative)
    p = PurePosixPath(relative)
    _require(not p.is_absolute() and ".." not in p.parts and str(p) == relative, "path_escape")
    candidate = root / relative
    _require(candidate.resolve().is_relative_to(root.resolve()), "path_escape")
    return candidate


def _request(request):
    _validate(request, "cache-request")
    _require(request["slot"] in core.SLOTS, "unknown_slot")
    for key in ("provider_id", "provider_revision"):
        _text(request[key])
    _require(request["provider_revision"] not in ("main", "master", "latest", "HEAD"), "unpinned_revision")
    for key in ("provider_sha256", "toolchain_sha256", "policy_sha256"):
        _digest(request[key])
    _require(len(request["inputs"]) <= MAX_FILES)
    for item in request["inputs"]:
        _text(item["role"])
        if "text" in item:
            _text(item["text"])
        else:
            _text(item["path"])
            _text(item["media_type"])
            _digest(item["sha256"])
    _require(all(type(v) in (str, int, bool) for v in request["params"].values()), "invalid_params")


def build_identity(*, slot, inputs, provider, params, toolchain_sha256, policy_sha256):
    _require(isinstance(provider, dict) and "id" in provider and "revision" in provider, "provider_invalid")
    _integer_tree(provider)
    identity = {"schema": "af.cache-request/v1", "slot": slot, "inputs": deepcopy(inputs),
                "provider_id": provider["id"], "provider_revision": provider["revision"],
                "provider_sha256": core.sha256_json(provider), "params": deepcopy(params),
                "toolchain_sha256": toolchain_sha256, "policy_sha256": policy_sha256}
    _request(identity)
    return identity


def request_key(request):
    _request(request)
    return core.sha256_json(request)


def _result(rc, key, reason=None, manifest_hash=None, artifacts=None, miss=False):
    result = {"schema": "af.cache-result/v1", "rc": rc,
              "cache_status": "MISS" if miss else _STATES[rc],
              "verdict": None if miss else core.VERDICT[rc], "request_key": key,
              "manifest_sha256": manifest_hash, "reason_codes": [reason] if reason else [],
              "artifacts": artifacts if rc == 0 and not miss and artifacts else []}
    return result


def _manifest(manifest, request, key):
    _validate(manifest, "manifest")
    _digest(manifest["request_key"])
    if manifest["request_key"] != key or core.canonical_json(manifest["request"]) != core.canonical_json(request):
        raise _Refusal(4, "request_stale")
    _request(manifest["request"])
    _require(len(manifest["artifacts"]) + len(manifest["gate_refs"]) <= MAX_FILES)
    paths, gates = set(), set()
    for item in manifest["artifacts"] + manifest["gate_refs"]:
        _path(Path("/"), item["path"])
        _require(item["path"] not in paths and item["path"] != "manifest.json", "duplicate_path")
        paths.add(item["path"])
        _digest(item["sha256"])
    # A directory and a file cannot occupy the same payload prefix.
    _require(not any(str(parent) in paths for p in paths for parent in PurePosixPath(p).parents), "path_collision")
    for item in manifest["artifacts"]:
        _text(item["role"])
        _text(item["media_type"])
    for item in manifest["gate_refs"]:
        _text(item["gate"])
        _text(item["schema"])
        _digest(item["subject_sha256"])
        _require(item["gate"] not in gates, "duplicate_gate")
        gates.add(item["gate"])
    for parent in manifest["parent_artifacts"]:
        _text(parent["role"])
        _digest(parent["sha256"])


@contextmanager
def _source_file(path):
    """Anchor traversal to directory descriptors; never follow a swapped link."""
    absolute = Path(path).absolute()
    _require(".." not in absolute.parts, "path_escape")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:-1]:
            child_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        # NONBLOCK prevents a raced-in FIFO from blocking before fstat.
        fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            _require(stat.S_ISREG(os.fstat(fd).st_mode), "not_regular_file")
            source = os.fdopen(fd, "rb")
            fd = None
            with source:
                yield source
        finally:
            if fd is not None:
                os.close(fd)
    finally:
        os.close(directory_fd)


def _read(path, budget, *, collect=False, target=None):
    h, chunks, length = hashlib.sha256(), [], 0
    budget.check()
    with _source_file(path) as source:
        while True:
            budget.check()
            block = source.read(2**20)
            if not block:
                break
            budget.check(len(block))
            h.update(block)
            length += len(block)
            if collect:
                if length > MAX_JSON_BYTES:
                    raise _Refusal(6, "json_budget_exceeded")
                chunks.append(block)
            if target is not None:
                target.write(block)
    data = b"".join(chunks)
    budget.check()
    return h.hexdigest(), data


def _json_bytes(data):
    def pairs(items):
        out = {}
        for key, value in items:
            _require(key not in out, "duplicate_key")
            out[key] = value
        return out

    def reject(_):
        raise core.ContractError("non_integer_number")

    obj = json.loads(data, object_pairs_hook=pairs, parse_float=reject, parse_constant=reject)
    _integer_tree(obj)
    return obj


def _evidence(ref, payload, artifact_hashes):
    _require(isinstance(payload, dict) and payload.get("schema") == ref["schema"], "evidence_invalid")
    verdict, rc = payload.get("verdict"), payload.get("rc")
    _require(verdict in _VERDICTS and type(rc) is int, "evidence_invalid")
    if payload["schema"] == "vqa.report/v1":
        _require(rc in (0, 1, 2, 3), "evidence_invalid")
        _require({0: "PASS", 1: "FAIL", 2: "ERROR", 3: "INCOMPLETE"}[rc] == verdict, "evidence_rc_mismatch")
        if rc == 2:
            return 2, "upstream_error"
        if rc == 1:
            return 5, "upstream_fail"
        if ref["subject_sha256"] not in artifact_hashes:
            return 3, "subject_unbound"
        return (5, "upstream_fail") if rc == 1 else (2, "upstream_error") if rc == 2 else (3, "bridge_required")
    if payload["schema"] != "af.bridge-verdict/v1":
        return 3, "unsupported_evidence"
    expected = {"schema", "verdict", "rc", "reason_codes", "source_sha256", "profile", "receipt_file_sha256", "trace_sha256", "failed_checks"}
    _require(set(payload) == expected and rc in core.RC and core.VERDICT[rc] == verdict, "evidence_invalid")
    _require(isinstance(payload["reason_codes"], list) and all(isinstance(v, str) for v in payload["reason_codes"]))
    _require(isinstance(payload["failed_checks"], list) and all(isinstance(v, str) for v in payload["failed_checks"]))
    _require(isinstance(payload["profile"], dict) and set(payload["profile"]) == {"id", "sha256"})
    _text(payload["profile"]["id"])
    for value in (payload["source_sha256"], payload["profile"]["sha256"], payload["receipt_file_sha256"], payload["trace_sha256"]):
        if value is None:
            _require(rc != 0, "evidence_invalid")
        else:
            _digest(value)
    _require(rc != 0 or not payload["failed_checks"], "evidence_invalid")
    # Validate the whole refusal before binding; ERROR/FAIL cannot be masked by
    # incomplete subject coverage. Nullable hashes never establish PASS.
    if rc != 0:
        return rc, "upstream_" + verdict.lower()
    if ref["subject_sha256"] not in artifact_hashes:
        return 3, "subject_unbound"
    if payload["source_sha256"] != ref["subject_sha256"]:
        return 4, "source_stale"
    if ref["verdict"] != verdict:
        return 3, "gate_verdict_mismatch"
    return 0, None


def _inspect(root, request, manifest, required_gates, budget):
    hashes = {a["sha256"] for a in manifest["artifacts"]}
    outcomes, reasons = [], []
    for artifact in manifest["artifacts"]:
        budget.check()
        digest, _ = _read(_path(root, artifact["path"]), budget)
        if digest != artifact["sha256"]:
            raise _Refusal(4, "artifact_stale")
    gates = {ref["gate"]: ref for ref in manifest["gate_refs"]}
    for ref in manifest["gate_refs"]:
        budget.check()
        try:
            digest, data = _read(_path(root, ref["path"]), budget, collect=True)
        except FileNotFoundError:
            outcomes.append(3)
            reasons.append("gate_missing")
            continue
        if digest != ref["sha256"]:
            raise _Refusal(4, "evidence_stale")
        rc, reason = _evidence(ref, _json_bytes(data), hashes)
        budget.check()
        outcomes.append(rc)
        if reason:
            reasons.append(reason)
        if ref["verdict"] == "FAIL":
            outcomes.append(5)
        elif ref["verdict"] == "ERROR":
            outcomes.append(2)
        elif ref["verdict"] != "PASS":
            outcomes.append(3)
    if not required_gates or any(gate not in gates for gate in required_gates):
        outcomes.append(3)
        reasons.append("required_gate_missing")
    if not manifest["artifacts"]:
        outcomes.append(3)
        reasons.append("artifact_missing")
    if manifest["verdict"] == "FAIL":
        outcomes.append(5)
        reasons.insert(0, "stored_fail")
    elif manifest["verdict"] == "ERROR":
        outcomes.append(2)
        reasons.insert(0, "stored_error")
    elif manifest["verdict"] != "PASS":
        outcomes.append(3)
        reasons.insert(0, "stored_incomplete")
    rc = core.worst_rc(outcomes)
    # Preserve specific stale/budget codes; worst_rc deliberately collapses them.
    if rc == 3:
        rc = 4 if 4 in outcomes else 6 if 6 in outcomes else 3
    budget.check()
    return rc, reasons[0] if reasons else None


def _decision(decision, request):
    fields = {"schema", "rc", "verdict", "allowed", "provider_id", "operation", "jurisdiction", "commercial_use", "monetisable", "reason_codes", "evaluated_dependencies", "configuration_sha256", "provider_sha256", "policy_sha256"}
    _integer_tree(decision)
    _require(isinstance(decision, dict) and set(decision) == fields, "decision_invalid")
    _require(decision["schema"] == "af.licence-decision/v1", "decision_invalid")
    rc = decision["rc"]
    _require(type(rc) is int and rc in core.RC and decision["verdict"] == core.VERDICT[rc], "decision_invalid")
    _require(type(decision["allowed"]) is bool and decision["allowed"] == (rc == 0), "decision_invalid")
    _require(type(decision["commercial_use"]) is bool and type(decision["monetisable"]) is bool, "decision_invalid")
    _text(decision["jurisdiction"])
    _require(isinstance(decision["reason_codes"], list) and all(isinstance(v, str) for v in decision["reason_codes"]))
    _require(isinstance(decision["evaluated_dependencies"], list))
    for field in ("configuration_sha256", "provider_sha256", "policy_sha256"):
        _digest(decision[field])
    if any(decision[field] != request[field] for field in ("provider_sha256", "policy_sha256")) or decision["provider_id"] != request["provider_id"] or decision["operation"] != request["slot"]:
        raise _Refusal(4, "decision_stale")
    if not decision["allowed"]:
        raise _Refusal(5, "licence_refused")


def _entry(cache_root, key):
    root = Path(cache_root)
    _require(not root.is_symlink(), "path_escape")
    return _path(root, key)


def _publishing(entry):
    return os.path.lexists(entry.parent / (".publish-" + entry.name + ".lock"))


def _stored(entry, request, key, budget):
    marker = _path(entry, "manifest.json")
    if not marker.exists():
        return None, None
    digest, data = _read(marker, budget, collect=True)
    manifest = _json_bytes(data)
    _manifest(manifest, request, key)
    budget.check()
    return manifest, digest


def _failure(exc, key, manifest_hash):
    if isinstance(exc, _Refusal):
        return _result(exc.rc, key, exc.reason, manifest_hash)
    return _result(2, key, getattr(exc, "code", "io_or_schema_invalid"), manifest_hash)


_ERRORS = (_Refusal, core.ContractError, OSError, ValueError, TypeError, KeyError, RecursionError)


def lookup(cache_root, request, *, decision, required_gates):
    key, manifest_hash = None, None
    budget = _Budget()
    try:
        key = request_key(request)
        budget.check()
        _require(isinstance(required_gates, tuple) and all(isinstance(v, str) and v for v in required_gates), "required_gates_invalid")
        _require(len(set(required_gates)) == len(required_gates), "duplicate_gate")
        _decision(decision, request)
        budget.check()
        entry = _entry(cache_root, key)
        if _publishing(entry):
            budget.check()
            return _result(0, key, miss=True)
        manifest, manifest_hash = _stored(entry, request, key, budget)
        if manifest is None:
            budget.check()
            return _result(0, key, miss=True)
        rc, reason = _inspect(entry, request, manifest, required_gates, budget)
        artifacts = [dict(a, path=str(_path(entry, a["path"]))) for a in manifest["artifacts"]]
        if _publishing(entry):
            budget.check()
            return _result(0, key, miss=True)
        budget.check()
        return _result(rc, key, reason, manifest_hash, artifacts)
    except FileNotFoundError:
        return _result(4, key, "artifact_missing", manifest_hash)
    except _ERRORS as exc:
        return _failure(exc, key, manifest_hash)


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy(source, destination, expected, budget):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "xb") as target:
        digest, _ = _read(source, budget, target=target)
        target.flush()
        os.fsync(target.fileno())
    if digest != expected:
        raise _Refusal(4, "source_changed")


def publish(cache_root, request, manifest, evidence_root):
    key, manifest_hash, staging, lock = None, None, None, None
    owned_entry, prepared = None, False
    budget = _Budget()
    try:
        key = request_key(request)
        budget.check()
        _manifest(manifest, request, key)
        budget.check()
        source = Path(evidence_root).resolve(strict=True)
        entry = _entry(cache_root, key)
        # Validate ALL caller bytes before acquiring the publication lease.
        required = tuple(ref["gate"] for ref in manifest["gate_refs"])
        rc, reason = _inspect(source, request, manifest, required, budget)
        if rc in (2, 4, 6):
            raise _Refusal(rc, reason)
        for item in manifest["artifacts"] + manifest["gate_refs"]:
            _require(_path(source, item["path"]).exists(), "evidence_missing")
        entry.parent.mkdir(parents=True, exist_ok=True)
        lock_path = _path(entry.parent, ".publish-" + key + ".lock")
        try:
            lock = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            raise _Refusal(2, "publication_locked") from None
        existing, manifest_hash = _stored(entry, request, key, budget)
        if existing is not None:
            if core.canonical_json(existing) != core.canonical_json(manifest):
                raise _Refusal(2, "conflicting_publish")
            rc, reason = _inspect(entry, request, existing, required, budget)
            artifacts = [dict(a, path=str(_path(entry, a["path"]))) for a in existing["artifacts"]]
            budget.check()
            prepared = True
            return _result(rc, key, reason, manifest_hash, artifacts)
        # Incomplete crash entries are not repaired or overwritten implicitly.
        if entry.exists():
            raise _Refusal(2, "incomplete_entry")
        staging = Path(tempfile.mkdtemp(prefix=".stage-" + key + "-", dir=entry.parent))
        for item in manifest["artifacts"] + manifest["gate_refs"]:
            _copy(_path(source, item["path"]), _path(staging, item["path"]), item["sha256"], budget)
        # Re-read staged data to close source/copy drift before the commit marker.
        rc, reason = _inspect(staging, request, manifest, required, budget)
        budget.check()
        # Directory replacement is atomic; manifest.json is written last within
        # the private stage and becomes visible with its complete payload set.
        core.atomic_write_json(staging / "manifest.json", manifest)
        for directory, _, _ in os.walk(staging, topdown=False):
            budget.check()
            _sync_dir(directory)
        budget.check()
        os.replace(staging, entry)
        owned_entry = entry
        staging = None
        _sync_dir(entry.parent)
        manifest_hash = core.sha256_json(manifest)
        artifacts = [dict(a, path=str(_path(entry, a["path"]))) for a in manifest["artifacts"]]
        budget.check()
        prepared = True
        return _result(rc, key, reason, manifest_hash, artifacts)
    except _ERRORS as exc:
        return _failure(exc, key, manifest_hash)
    finally:
        cleanup_failed = False
        rollback_failed = False
        deadline_failure = None
        if staging is not None:
            try:
                shutil.rmtree(staging)
            except OSError:
                cleanup_failed = True
        if lock is not None:
            try:
                os.close(lock)
            except OSError:
                cleanup_failed = True
        if prepared:
            try:
                budget.check()
            except _Refusal as exc:
                deadline_failure = exc
                prepared = False
        # The lease hides a newly renamed entry until fsync and cleanup succeed.
        # Roll back only this transaction's directory, never an existing entry.
        if owned_entry is not None and (not prepared or cleanup_failed):
            try:
                shutil.rmtree(owned_entry)
                owned_entry = None
            except OSError:
                cleanup_failed = rollback_failed = True
        if lock is not None and not rollback_failed:
            try:
                os.unlink(lock_path)
            except OSError:
                cleanup_failed = True
                if owned_entry is not None:
                    try:
                        shutil.rmtree(owned_entry)
                    except OSError:
                        pass  # Retain the lease: lookup must stay ineligible.
        # A filesystem call can itself consume the remaining time. Never return
        # a successful qualification after a slow lease release crossed it.
        if prepared and not cleanup_failed:
            try:
                budget.check()
            except _Refusal as exc:
                deadline_failure = exc
                if owned_entry is not None:
                    try:
                        guard = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        os.close(guard)
                        shutil.rmtree(owned_entry)
                        os.unlink(lock_path)
                    except OSError:
                        cleanup_failed = True  # Retain an acquired guard on failure.
        if cleanup_failed:
            return _result(2, key, "cleanup_failed", manifest_hash)
        if deadline_failure is not None:
            return _failure(deadline_failure, key, manifest_hash)
