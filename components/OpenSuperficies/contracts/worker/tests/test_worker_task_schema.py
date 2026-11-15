"""W8 proposed task/receipt contract: strict, stdlib-only structural fixtures.

The validator below interprets the actual adjacent JSON Schema's deliberately
small keyword subset. It refuses unsupported keywords; it is not a complete
JSON Schema implementation, an isolation boundary, or a production verifier.
Cross-document checks demonstrate the required trusted verifier invariants.
All identities, hashes, resources, and observations in fixtures are synthetic.

AMEND AW8 (ADV-W8-01..08, A1..A3). The schema is looked up next to this file
first (campaign amend layout) and then one directory up (repo layout
contracts/worker/tests/ -> contracts/worker/).

Run directly: python3 -B contracts/worker/tests/test_worker_task_schema.py
Discovery without -t: python3 -B -m unittest discover -s contracts/worker/tests
Discovery with -t . (repo root) needs the empty package markers
contracts/__init__.py, contracts/worker/__init__.py and
contracts/worker/tests/__init__.py (ADV-W8-01); without them Python 3.12
raises ImportError before loading this file. Direct entry returns
0 / 2 / 5 / 6, never 1; a gate that uses the unittest CLI must map its rc 1
to 5 (sh -c '...; rc=$?; [ "$rc" -eq 1 ] && rc=5; exit "$rc"').

AMEND3 (A4 operator security baseline + JW8-01..05): one hardened base image
(os.cu.base-image/v1) plus a per-task tmpfs clone of the verified commit;
no per-commit images; tar-over-exec text extract; inference host-only; the
launch spec refuses bind mounts, env files and the runtime socket by schema.
JW8-04: OS_CU_CANON_BRIDGE overrides the canon WEB_BRIDGE schema directory;
--no-skips turns any skipped test into rc 5 (Phase 0 host gate: 0 skips).
"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unittest


_HERE = Path(__file__).resolve().parent
SCHEMA_NAME = "os.cu.worker-task.v1.schema.json"
SCHEMA_PATH = _HERE / SCHEMA_NAME if (_HERE / SCHEMA_NAME).is_file() else _HERE.parent / SCHEMA_NAME
CANON_BRIDGE_SCHEMAS = Path(os.environ.get(
    "OS_CU_CANON_BRIDGE", "/home/user/Música/ORQUESTADOR_WAVEMIND_PROYECTOS/canon/WEB_BRIDGE/schemas"))
DEFAULT_TTL_MS = 900000
DEFAULT_OUTPUT_MAX_BYTES = 1048576
DENY_NETWORK = {"mode": "none", "allowlist": []}
NORMALIZED_EXITS = frozenset((0, 2, 3, 4, 5, 6, 7))


class ValidationError(ValueError):
    """A JSON instance or a cross-document invariant was refused."""


class Unavailable(ValidationError):
    """rc 4 `unavailable`: a prerequisite (e.g. the canon os-capsule surface) is absent."""
    rc = 4


class SchemaError(ValueError):
    """The contract uses malformed or unsupported schema features."""


def load_json(text):
    """Reject duplicate keys, floats and non-finite numbers before validation."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValidationError("duplicate JSON key: " + key)
            result[key] = value
        return result

    def non_integer(token):
        raise ValidationError("non-integer JSON number: " + token)

    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_float=non_integer, parse_constant=non_integer)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValidationError(str(error)) from error


def canonical_bytes(value):
    """Version-1 binding: UTF-8, sorted keys, compact separators, no newline."""
    def admissible(item):
        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is list:
            for child in item:
                admissible(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                admissible(child)
            return
        raise ValidationError("binding value is not integer-only JSON")

    admissible(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError) as error:
        raise ValidationError("non-UTF-8 binding value") from error


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def equal_json(left, right):
    # Python's True == 1 must not make const/enum/uniqueItems accept bools.
    return canonical_bytes(left) == canonical_bytes(right)


class SchemaSubset:
    """Strict interpreter for every validation keyword used by this schema."""

    ANNOTATIONS = frozenset(("$schema", "$id", "title", "description", "$comment"))
    KEYWORDS = ANNOTATIONS | frozenset((
        "$defs", "$ref", "type", "properties", "required", "additionalProperties",
        "items", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength",
        "minimum", "maximum", "pattern", "const", "enum", "oneOf", "allOf",
        "if", "then", "else",
    ))
    TYPES = {"object": dict, "array": list, "integer": int,
             "boolean": bool, "string": str, "null": type(None)}

    def __init__(self, schema):
        self.schema = schema
        self._audit(schema, "$schema")

    def _resolve(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(
                r"#/[\$]defs/[A-Za-z0-9_]+", reference):
            raise SchemaError("only local $defs references are supported")
        name = reference.rsplit("/", 1)[1]
        try:
            return self.schema["$defs"][name]
        except (KeyError, TypeError) as error:
            raise SchemaError("unresolved reference: " + reference) from error

    def _audit(self, schema, location):
        if type(schema) is bool:
            return
        if type(schema) is not dict:
            raise SchemaError(location + ": expected object/boolean schema")
        unknown = set(schema) - self.KEYWORDS
        if unknown:
            raise SchemaError(location + ": unsupported keywords " + repr(sorted(unknown)))
        if "$ref" in schema:
            self._resolve(schema["$ref"])
        if "type" in schema and schema["type"] not in self.TYPES:
            raise SchemaError(location + ": unsupported type")
        if "pattern" in schema:
            try:
                if not (schema["pattern"].startswith("^") and schema["pattern"].endswith("$")):
                    raise SchemaError(location + ": only anchored patterns supported")
                re.compile(schema["pattern"])
            except (TypeError, re.error) as error:
                raise SchemaError(location + ": invalid pattern") from error
        for keyword in ("minimum", "maximum", "minLength", "maxLength",
                        "minItems", "maxItems"):
            if keyword in schema and type(schema[keyword]) is not int:
                raise SchemaError(location + ": non-integer " + keyword)
        if "enum" in schema:
            if type(schema["enum"]) is not list or not schema["enum"]:
                raise SchemaError(location + ": empty/non-array enum")
            if len({canonical_bytes(item) for item in schema["enum"]}) != len(schema["enum"]):
                raise SchemaError(location + ": duplicate enum value")
        for keyword in ("properties", "$defs"):
            if keyword in schema:
                if type(schema[keyword]) is not dict:
                    raise SchemaError(location + ": invalid " + keyword)
                for key, child in schema[keyword].items():
                    self._audit(child, location + "/" + keyword + "/" + key)
        if "required" in schema:
            required = schema["required"]
            if (type(required) is not list or any(type(key) is not str for key in required)
                    or len(set(required)) != len(required)):
                raise SchemaError(location + ": invalid required")
        if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
            raise SchemaError(location + ": only boolean additionalProperties supported")
        if "uniqueItems" in schema and type(schema["uniqueItems"]) is not bool:
            raise SchemaError(location + ": invalid uniqueItems")
        for keyword in ("items", "if", "then", "else"):
            if keyword in schema:
                self._audit(schema[keyword], location + "/" + keyword)
        for keyword in ("oneOf", "allOf"):
            if keyword in schema:
                if type(schema[keyword]) is not list or not schema[keyword]:
                    raise SchemaError(location + ": invalid " + keyword)
                for index, child in enumerate(schema[keyword]):
                    self._audit(child, location + "/" + keyword + "/" + str(index))

    def validate(self, instance, definition=None):
        schema = self.schema if definition is None else self._resolve("#/$defs/" + definition)
        # Also reject values impossible in the strict JSON wire encoding.
        canonical_bytes(instance)
        self._check(instance, schema, "$", 0)

    def _matches(self, instance, schema, path, depth):
        try:
            self._check(instance, schema, path, depth)
            return True
        except ValidationError:
            return False

    def _check(self, instance, schema, path, depth):
        if depth > 128:
            raise SchemaError("schema reference/nesting limit exceeded")
        if schema is True:
            return
        if schema is False:
            raise ValidationError(path + ": false schema")
        if "$ref" in schema:
            self._check(instance, self._resolve(schema["$ref"]), path, depth + 1)
        if "type" in schema and type(instance) is not self.TYPES[schema["type"]]:
            raise ValidationError(path + ": expected " + schema["type"])
        if "const" in schema and not equal_json(instance, schema["const"]):
            raise ValidationError(path + ": const mismatch")
        if "enum" in schema and not any(equal_json(instance, option) for option in schema["enum"]):
            raise ValidationError(path + ": enum mismatch")
        if type(instance) is dict:
            properties = schema.get("properties", {})
            missing = set(schema.get("required", ())) - set(instance)
            if missing:
                raise ValidationError(path + ": missing " + repr(sorted(missing)))
            if schema.get("additionalProperties") is False and set(instance) - set(properties):
                raise ValidationError(path + ": unknown properties")
            for key, child in properties.items():
                if key in instance:
                    self._check(instance[key], child, path + "/" + key, depth + 1)
        if type(instance) is list:
            if len(instance) < schema.get("minItems", 0) or len(instance) > schema.get("maxItems", len(instance)):
                raise ValidationError(path + ": array length")
            if schema.get("uniqueItems") and len({canonical_bytes(item) for item in instance}) != len(instance):
                raise ValidationError(path + ": duplicate item")
            if "items" in schema:
                for index, child in enumerate(instance):
                    self._check(child, schema["items"], path + "/" + str(index), depth + 1)
        if type(instance) is str:
            if len(instance) < schema.get("minLength", 0) or len(instance) > schema.get("maxLength", len(instance)):
                raise ValidationError(path + ": string length")
            # All contract patterns are anchored. fullmatch avoids Python's $
            # accepting a position before a final newline in identifiers/paths.
            if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
                raise ValidationError(path + ": pattern mismatch")
        if type(instance) is int:
            if instance < schema.get("minimum", instance) or instance > schema.get("maximum", instance):
                raise ValidationError(path + ": integer range")
        if "allOf" in schema:
            for child in schema["allOf"]:
                self._check(instance, child, path, depth + 1)
        if "oneOf" in schema:
            matches = sum(self._matches(instance, child, path, depth + 1) for child in schema["oneOf"])
            if matches != 1:
                raise ValidationError(path + ": oneOf matched " + str(matches))
        if "if" in schema:
            branch = "then" if self._matches(instance, schema["if"], path, depth + 1) else "else"
            if branch in schema:
                self._check(instance, schema[branch], path, depth + 1)


try:
    SCHEMA = load_json(SCHEMA_PATH.read_text(encoding="utf-8"))
    ENGINE = SchemaSubset(SCHEMA)
except Exception as schema_load_error:
    if __name__ == "__main__":
        print("E_GATE worker_schema_load " + repr(schema_load_error), file=sys.stderr)
        raise SystemExit(5) from schema_load_error
    raise


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def unique_by(items, key):
    require(len({item[key] for item in items}) == len(items), "duplicate " + key)


def task_policy(task):
    optional = task.get("policy", {})
    return {"ttl_ms": optional.get("ttl_ms", DEFAULT_TTL_MS),
            "network": optional.get("network", DENY_NETWORK),
            "frames_root": optional.get("frames_root", "/dev/shm"),
            "output_max_bytes": optional.get("output_max_bytes", DEFAULT_OUTPUT_MAX_BYTES)}


def network_is_subset(granted, requested):
    if granted["mode"] == "none":
        return True
    if requested["mode"] == "none":
        return False
    return ({canonical_bytes(endpoint) for endpoint in granted["allowlist"]}
            <= {canonical_bytes(endpoint) for endpoint in requested["allowlist"]})


def validate_task(task):
    ENGINE.validate(task, "task")
    unique_by(task["sources"], "path")
    unique_by(task["evidence"]["requests"], "request_id")
    source_paths = {item["path"] for item in task["sources"]}
    require(all(item["path"] in source_paths for item in task["evidence"]["requests"] if "path" in item),
            "evidence request not bound to a declared source")
    if "bridge" in task:
        expected = bridge_projection(task["bridge"])
        actual = {"task_id": task["task_id"], "attempt_id": task["attempt_id"], "lease_id": task["lease_id"],
                  "repo": task["repo"], "source_paths": [item["path"] for item in task["sources"]],
                  "write_paths": task["write_paths"], "argv": task["gate"]["argv"], "cwd": task["gate"]["cwd"],
                  "requests": task["evidence"]["requests"]}
        for key, value in expected.items():
            require(equal_json(actual[key], value), "bridge projection mismatch: " + key)
    require(all(".git" not in path.split("/") for path in task["write_paths"]),
            "Git administrative paths are not write paths")
    require(task["gate"]["timeout_ms"] <= task_policy(task)["ttl_ms"], "gate exceeds task TTL")


def normalized_gate_exit(raw_exit_code, terminated_by):
    # This is the child observation's classification. A supervisor separately
    # upgrades the overall receipt to 6 when final state/cleanup is unknown.
    if terminated_by == "unknown":
        return 6
    if terminated_by in ("timeout", "cancelled", "oom", "signal"):
        return 5
    if raw_exit_code is None:
        return 3
    return 0 if raw_exit_code == 0 else 5


def validate_bundle(task, receipt, grant, now_ms, consumed_leases=frozenset()):
    """Check synthetic structure/bindings, not actual process/filesystem truth.

    A production verifier must obtain grant and observations from its trusted
    supervisor, hash real source/output/log bytes, parse/apply patches in RAM,
    inspect actual changed files and modes, and atomically fence the lease.
    Passing three mutually consistent attacker-supplied JSON documents is not
    sufficient authorization or evidence. No live commands run in these tests.
    """
    validate_task(task)
    ENGINE.validate(receipt, "receipt")
    ENGINE.validate(grant, "seat_grant")
    require(type(now_ms) is int and now_ms >= 0, "invalid trusted clock")
    for key in ("task_id", "attempt_id", "lease_id"):
        require(task[key] == receipt[key] == grant[key], "binding mismatch: " + key)
    require(receipt["repo"] == task["repo"], "receipt repository mismatch")
    require(grant["repo_commit"] == task["repo"]["commit"], "grant repository mismatch")
    require(receipt["task_sha256"] == grant["task_sha256"] == digest(task), "task digest mismatch")
    require(receipt["seat_grant_sha256"] == digest(grant), "seat grant digest mismatch")
    for key in ("seat_id", "lease_nonce", "runtime"):
        require(receipt[key] == grant[key], "grant mismatch: " + key)
    require((grant["seat_id"], grant["lease_nonce"]) not in consumed_leases, "replayed lease")
    policy = task_policy(task)
    issued, expiry, collect_until = (grant[key] for key in (
        "issued_at_ms", "expires_at_ms", "collect_until_ms"))
    require(issued < expiry <= collect_until <= expiry + 60000, "invalid lease interval")
    require(expiry - issued <= policy["ttl_ms"], "lease exceeds task TTL")
    require(receipt["started_at_ms"] >= issued, "receipt predates lease")
    require(receipt["started_at_ms"] <= receipt["finished_at_ms"] <= now_ms <= collect_until,
            "stale/future/unordered receipt")
    require(network_is_subset(grant["network"], policy["network"]), "network grant exceeds immutable request")
    if receipt["exit_code"] == 0:
        require(receipt["finished_at_ms"] <= expiry, "successful execution exceeded hard TTL")
    local = grant["runtime"]["backend"] != "web"
    if local:
        limits = grant["limits"]
        require(limits["cpuset"] == sorted(limits["cpuset"]), "cpuset is not canonical")
        require(expiry - issued <= limits["ttl_ms"] <= policy["ttl_ms"], "seat TTL mismatch")
        require(task["gate"]["timeout_ms"] <= limits["ttl_ms"], "gate exceeds seat TTL")
        require(limits["workspace_bytes"] <= limits["memory_bytes"], "workspace exceeds memory grant")
        require(grant["isolation"]["workspace_root"] != grant["isolation"]["frames_root"],
                "frame and checkout roots must be distinct")
        runtime, hardening = grant["runtime"], grant["isolation"]["hardening"]
        require(runtime["commit"] == task["repo"]["commit"], "A4.1 tmpfs clone commit differs from task pin")
        require(runtime["workspace"]["size_bytes"] == limits["workspace_bytes"], "tmpfs size differs from grant")
        require(not set(limits["cpuset"]) & set(hardening["interactive_cpus"]), "A4.3 cpuset on interactive cores")
        require((grant["network"]["mode"] == "none") == (hardening["egress"]["mode"] == "none"),
                "A4.3 egress allow-list needs operator host rules (and none needs none)")
        if runtime["backend"] == "microvm":
            require(runtime["nesting"]["outer_image_sha256"] == runtime["base_image"]["digest"],
                    "outer container is not the hardened base image")
    observed_sources = receipt["sources_observed"]
    unique_by(observed_sources, "path")
    originals = {item["path"]: item["sha256"] for item in task["sources"]}
    require(all(item["path"] in originals for item in observed_sources), "unexpected observed source")
    for item in observed_sources:
        require(originals[item["path"]] in (None, item["sha256"]), "source digest mismatch")
    if receipt["exit_code"] == 0:
        require({item["path"] for item in observed_sources} == set(originals), "incomplete source binding")
    unique_by(receipt["evidence"]["answers"], "request_id")
    requested_ids = {item["request_id"] for item in task["evidence"]["requests"]}
    answered_ids = {item["request_id"] for item in receipt["evidence"]["answers"]}
    require(answered_ids <= requested_ids, "unexpected evidence answer")
    if receipt["exit_code"] == 0:
        require(answered_ids == requested_ids, "missing evidence answer")
        answers = {item["request_id"]: item["answer"] for item in receipt["evidence"]["answers"]}
        source_digests = {item["path"]: item["sha256"] for item in observed_sources}
        for request in task["evidence"]["requests"]:
            if request["kind"] == "sha256":
                answer = answers[request["request_id"]]
                require(re.fullmatch(r"[0-9a-f]{64}", answer) is not None
                        and answer == source_digests[request["path"]], "SHA256 evidence answer mismatch")
            # Line answers may contain literal ABSENT when the requested line
            # does not exist. Exact bytes/line presence are checked by the real
            # collector; a bounded string alone is not proof of source content.
    gate = receipt["gate"]
    retained_log_bytes = 0
    if gate is not None:
        for key in ("argv", "cwd", "timeout_ms"):
            require(gate[key] == task["gate"][key], "gate contract changed: " + key)
        require(gate["exit_code"] == normalized_gate_exit(gate["raw_exit_code"], gate["terminated_by"]),
                "gate normalization mismatch")
        require(gate["duration_ms"] <= receipt["finished_at_ms"] - receipt["started_at_ms"],
                "gate duration exceeds receipt interval")
        for stream_name in ("stdout", "stderr"):
            stream = gate[stream_name]
            require(stream["retained_bytes"] <= stream["total_bytes"], "stream byte count mismatch")
            require(stream["truncated"] == (stream["retained_bytes"] < stream["total_bytes"]),
                    "stream truncation mismatch")
            require(stream["retained_bytes"] == 0 or stream["sha256"] is not None, "missing retained log digest")
            retained_log_bytes += stream["retained_bytes"]
    require(len(canonical_bytes(receipt)) + retained_log_bytes <= task["evidence"]["max_bytes"],
            "diagnostic evidence cap exceeded")
    metric = receipt["host_metric"]
    for key in ("name", "unit"):
        require(metric[key] == task["host_metric"][key], "metric binding mismatch: " + key)
    if receipt["exit_code"] == 0:
        require(not task["host_metric"]["required"] or metric["value"] is not None, "required metric unavailable")
        target = task["host_metric"]["target_max"]
        require(target is None or metric["value"] is None or metric["value"] <= target, "metric target exceeded")
    output = receipt["output"]
    if output is not None:
        require(output["base_commit"] == task["repo"]["commit"], "output base mismatch")
        unique_by(output["files"], "path")
        require(all(item["path"] in task["write_paths"] for item in output["files"]), "output outside write paths")
        output_bytes = sum(item["bytes"] for item in output["files"])
        if output["kind"] == "branch":
            require(output["repo_url"] == task["repo"]["url"], "output repository mismatch")
            require(not output["files"] or output["head_commit"] != output["base_commit"],
                    "changed branch has unchanged commit")
        else:
            output_bytes += output["patch"]["bytes"]
        require(output_bytes <= policy["output_max_bytes"], "code export cap exceeded")
    observations = receipt["observations"]
    if receipt["exit_code"] == 0 and local:
        limits, isolation = grant["limits"], grant["isolation"]
        for key in ("uid", "rootfs_read_only", "cap_drop", "no_new_privileges", "no_host_mounts",
                    "no_runtime_socket", "seccomp_profile_sha256", "workspace_root", "frames_root",
                    "swap_enabled", "core_dumps_enabled"):
            require(observations[key] == isolation[key], "isolation observation mismatch: " + key)
        for observed_key, limit_key in (("cpuset", "cpuset"), ("memory_limit_bytes", "memory_bytes"),
                                        ("pids_limit", "pids"), ("workspace_limit_bytes", "workspace_bytes")):
            require(observations[observed_key] == limits[limit_key], "resource limit mismatch: " + limit_key)
        for peak_key, limit_key in (("peak_memory_bytes", "memory_bytes"), ("peak_pids", "pids"),
                                    ("workspace_peak_bytes", "workspace_bytes")):
            require(observations[peak_key] <= limits[limit_key], "observed resource cap exceeded")
        require(observations["network"] == grant["network"], "network observation mismatch")
        hardening, probes = isolation["hardening"], observations["probes"]
        for key in ("devices", "nofile", "oom_score_adj", "daemon"):
            require(probes[key] == hardening[key], "A4 hardening observation mismatch: " + key)
        if grant["runtime"]["profile"] == "worker":
            require(observations["xpra"] == "off" and observations["x11_listeners"] == 0, "headless UI process")
        else:
            require(observations["xpra"] == "uds", "UI profile requires private UDS transport")


SOURCE_BYTES = b"synthetic source\n"
CHANGED_BYTES = b"synthetic change\n"
SOURCE_SHA = hashlib.sha256(SOURCE_BYTES).hexdigest()
CHANGE_SHA = hashlib.sha256(CHANGED_BYTES).hexdigest()
ISSUED = 1000000
NOW = ISSUED + 300


def example_task():
    return {"schema": "os.cu.worker-task/v1", "task_id": "task-example", "attempt_id": "a1",
            "lease_id": "lease-example", "repo": {"url": "https://example.invalid/team/project", "commit": "a" * 40},
            "objective": "Exercise a synthetic gate fixture; do not contact a live runtime.",
            "sources": [{"path": "src/work.go", "sha256": None}],
            "write_paths": ["src/work.go", "tests/work_test.go"],
            "gate": {"argv": ["go", "test", "./..."], "cwd": ".", "timeout_ms": 10000},
            "evidence": {"requests": [{"request_id": "source", "kind": "sha256", "path": "src/work.go"}],
                         "max_bytes": 102400},
            "consumer": {"name": "auto_ingest"},
            "host_metric": {"name": "elapsed_ms", "unit": "ms", "target_max": None, "required": False}}


def example_hardening(backend="docker"):
    return {"daemon": "rootless", "init": "tini", "read_only": True, "memswap_equals_memory": True,
            "nofile": 8192, "oom_score_adj": 800, "interactive_cpus": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
            "devices": ["/dev/kvm"] if backend == "microvm" else [], "bind_mounts": [], "env_file": None,
            "runtime_socket_mounted": False, "env_names": ["LC_ALL", "TINI_SUBREAPER", "GIT_TERMINAL_PROMPT"],
            "egress": {"mode": "none"}, "exec_allowlist_sha256": "4" * 64}


def example_probes(backend="docker"):
    return {"inspect_sha256": "3" * 64, "etc_write": "read_only", "home_admin": "absent", "egress_probe": "BLOCKED",
            "orphans_after_task": 0, "memswap_equals_memory": True, "oom_score_adj": 800, "nofile": 8192,
            "devices": ["/dev/kvm"] if backend == "microvm" else [], "bind_mounts": 0, "env_file_used": False,
            "env_secret_names": 0, "runtime_socket_visible": False, "daemon": "rootless", "inference_processes": 0}


EXAMPLE_EXTRACT = {"method": "tar-exec", "entries": 2, "bytes": 4096, "text_only": True, "mode": "0644",
                   "owner_asserted": True, "staging_residue": 0, "sha256": "2" * 64}


def example_grant(task, backend="docker", profile="worker"):
    runtime = {"backend": backend, "profile": profile, "vmm": None,
               "base_image": {"name": "os-worker-base", "digest": "e" * 64, "recipe_sha256": "9" * 64,
                              "approval_sha256": "6" * 64},
               "commit": task["repo"]["commit"],
               "workspace": {"kind": "tmpfs", "root": "/workspace",
                             "options": "rw,exec,nosuid,nodev,mode=0770,uid=10001,gid=10001",
                             "size_bytes": 134217728, "purge": "task_end", "ingest": "git-bundle-over-exec",
                             "cache": {"volume": "os_worker_cache", "target": "/opt/cache", "read_only": True,
                                       "manifest_sha256": "5" * 64}},
               "extract": "tar-exec", "inference": "host"}
    limits = {"ttl_ms": DEFAULT_TTL_MS, "cpuset": [10, 11], "memory_bytes": 268435456,
              "pids": 64, "workspace_bytes": 134217728}
    isolation = {"uid": 10001, "rootfs_read_only": True, "cap_drop": ["ALL"],
                 "no_new_privileges": True, "no_host_mounts": True, "no_runtime_socket": True,
                 "seccomp_profile_sha256": "d" * 64, "workspace_root": "/workspace/repo",
                 "frames_root": "/dev/shm/worker-lease-example/frames", "swap_enabled": False,
                 "core_dumps_enabled": False, "hardening": example_hardening(backend)}
    if backend == "web":
        runtime, limits, isolation = {"backend": "web", "profile": "web", "provider": "fixture-web-seat"}, None, None
    elif backend == "microvm":
        runtime["vmm"] = {"name": "firecracker", "version": "fixture-v1", "kernel_sha256": "f" * 64}
        runtime["nesting"] = {"outer_backend": "docker", "outer_image_sha256": "e" * 64,
                              "devices": ["/dev/kvm"], "control": "vsock_uds", "guest_arch": "aarch64",
                              "rootfs_sha256": "7" * 64, "vms_in_container": 2}
    grant = {"schema": "os.cu.worker-seat/v1", "task_id": task["task_id"], "attempt_id": task["attempt_id"],
            "lease_id": task["lease_id"], "task_sha256": digest(task), "repo_commit": task["repo"]["commit"],
            "seat_id": "seat-example", "lease_nonce": "c" * 64, "issued_at_ms": ISSUED,
            "expires_at_ms": ISSUED + DEFAULT_TTL_MS, "collect_until_ms": ISSUED + DEFAULT_TTL_MS + 60000,
            "runtime": runtime, "network": deepcopy(task_policy(task)["network"]),
            "limits": limits, "isolation": isolation}
    set_grant_network(grant, grant["network"])
    return grant


HOST_RULES = {"mode": "host_rules", "bridge": "br-osw", "rules_sha256": "1" * 64, "verify_sha256": "0" * 64}


def set_grant_network(grant, network):
    """A4.3: an allow-list grant is only valid with the operator's host-rule record (guide §6.2/§12)."""
    grant["network"] = deepcopy(network)
    if grant["isolation"] is not None:
        grant["isolation"]["hardening"]["egress"] = (
            {"mode": "none"} if network["mode"] == "none" else deepcopy(HOST_RULES))


def example_receipt(task, grant):
    observations = {"capsule_id": "capsule-example", "uid": 10001, "rootfs_read_only": True,
                    "cap_drop": ["ALL"], "no_new_privileges": True, "no_host_mounts": True,
                    "no_runtime_socket": True, "seccomp_profile_sha256": "d" * 64,
                    "cpuset": [10, 11], "memory_limit_bytes": 268435456, "peak_memory_bytes": 4096,
                    "pids_limit": 64, "peak_pids": 2, "workspace_limit_bytes": 134217728,
                    "workspace_peak_bytes": 2048, "workspace_root": "/workspace/repo",
                    "frames_root": "/dev/shm/worker-lease-example/frames", "frames_disk_bytes": 0,
                    "swap_enabled": False, "core_dumps_enabled": False,
                    "network": deepcopy(grant["network"]), "denied_network_attempts": 0, "ttl_enforced": True,
                    "xpra": "uds" if grant["runtime"]["profile"] == "worker_ui" else "off",
                    "x11_listeners": 0, "public_listeners": 0,
                    "probes": example_probes(grant["runtime"]["backend"])}
    extract = deepcopy(EXAMPLE_EXTRACT)
    cleanup = {"status": "verified", "remaining_pids": 0, "remaining_workspace_bytes": 0, "resources_released": True}
    if grant["runtime"]["backend"] == "web":
        observations, extract = None, None
        cleanup = {"status": "not_applicable", "remaining_pids": None,
                   "remaining_workspace_bytes": None, "resources_released": None}
    empty_stream = {"sha256": None, "retained_bytes": 0, "total_bytes": 0, "truncated": False}
    return {"schema": "os.cu.worker-receipt/v1", "task_id": task["task_id"], "attempt_id": task["attempt_id"],
            "lease_id": task["lease_id"], "task_sha256": digest(task), "repo": deepcopy(task["repo"]),
            "seat_id": grant["seat_id"], "lease_nonce": grant["lease_nonce"], "seat_grant_sha256": digest(grant),
            "started_at_ms": ISSUED + 100, "finished_at_ms": ISSUED + 200, "exit_code": 0, "failure_reason": None,
            "runtime": deepcopy(grant["runtime"]),
            "gate": {**deepcopy(task["gate"]), "raw_exit_code": 0, "exit_code": 0, "terminated_by": None,
                     "duration_ms": 50, "stdout": deepcopy(empty_stream), "stderr": deepcopy(empty_stream)},
            "sources_observed": [{"path": "src/work.go", "sha256": SOURCE_SHA}],
            "output": {"kind": "branch", "repo_url": task["repo"]["url"], "branch": "bridge/task-example/a1",
                       "base_commit": task["repo"]["commit"], "head_commit": "b" * 40,
                       "files": [{"path": "src/work.go", "operation": "modify", "mode": "100644",
                                  "sha256": CHANGE_SHA, "bytes": len(CHANGED_BYTES)}]},
            "evidence": {"answers": [{"request_id": "source", "answer": SOURCE_SHA}],
                         "notes": "Synthetic fixture; no live runtime was started."},
            "host_metric": {"name": "elapsed_ms", "unit": "ms", "value": None,
                            "provenance": "unavailable", "sample_count": 0},
            "observations": observations, "cleanup": cleanup, "extract": extract}


def fixture(backend="docker", profile="worker"):
    task = example_task()
    grant = example_grant(task, backend, profile)
    return task, example_receipt(task, grant), grant


def rebind(task, receipt, grant):
    """Recompute only synthetic fixture hashes after an intentional mutation."""
    grant["task_sha256"] = receipt["task_sha256"] = digest(task)
    receipt["seat_grant_sha256"] = digest(grant)


def set_at(document, path, value):
    current = document
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = value


class ValidatorBehavior(unittest.TestCase):
    def test_draft_and_closed_objects(self):
        self.assertEqual(SCHEMA["$schema"], "https://json-schema.org/draft/2020-12/schema")
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False)
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)
        walk(SCHEMA)

    def test_unsupported_keyword_and_reference_are_refused(self):
        for keyword, value in (("format", "uri"), ("not", {}), ("anyOf", [{}]),
                               ("unevaluatedProperties", False), ("minimumTypo", 1)):
            with self.subTest(keyword=keyword):
                schema = deepcopy(SCHEMA)
                schema["$defs"]["task"][keyword] = value
                with self.assertRaises(SchemaError):
                    SchemaSubset(schema)
        for reference in ("https://example.invalid/schema", "#/$defs/missing"):
            with self.subTest(reference=reference), self.assertRaises(SchemaError):
                SchemaSubset({"$ref": reference})

    def test_validator_reads_actual_required_and_minimum(self):
        task = example_task()
        del task["objective"]
        with self.assertRaises(ValidationError):
            ENGINE.validate(task)
        relaxed = deepcopy(SCHEMA)
        relaxed["$defs"]["task"]["required"].remove("objective")
        SchemaSubset(relaxed).validate(task)
        task = example_task()
        task["gate"]["timeout_ms"] = 0
        with self.assertRaises(ValidationError):
            ENGINE.validate(task)
        relaxed = deepcopy(SCHEMA)
        relaxed["$defs"]["gate"]["properties"]["timeout_ms"]["minimum"] = 0
        SchemaSubset(relaxed).validate(task)

    def test_bool_integer_const_enum_and_unique_items(self):
        for schema, value in (({"type": "integer"}, True), ({"const": 1}, True), ({"enum": [1]}, True),
                              ({"const": False}, 0), ({"enum": [False]}, 0), ({"type": "boolean"}, 1)):
            with self.subTest(schema=schema, value=value), self.assertRaises(ValidationError):
                SchemaSubset(schema).validate(value)
        SchemaSubset({"uniqueItems": True}).validate([1, True])
        with self.assertRaises(ValidationError):
            SchemaSubset({"uniqueItems": True}).validate([{"x": 1}, {"x": 1}])

    def test_oneof_requires_exactly_one_and_allof_applies_all(self):
        for schema in ({"oneOf": [{"type": "integer"}, {"minimum": 0}]},
                       {"oneOf": [{"type": "string"}, {"type": "null"}]},
                       {"allOf": [{"type": "integer"}, {"minimum": 2}]}):
            with self.subTest(schema=schema), self.assertRaises(ValidationError):
                SchemaSubset(schema).validate(1)

    def test_if_else_and_ref_siblings_are_enforced(self):
        conditional = {"if": {"const": 0}, "then": {"minimum": 1}, "else": {"maximum": 1}}
        for value in (0, 2):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                SchemaSubset(conditional).validate(value)
        SchemaSubset(conditional).validate(1)
        schema = {"$defs": {"value": {"type": "integer"}}, "$ref": "#/$defs/value", "minimum": 2}
        with self.assertRaises(ValidationError):
            SchemaSubset(schema).validate(1)

    def test_duplicate_keys_floats_nan_and_non_json_are_refused(self):
        for encoded in ('{"task_id":"a","task_id":"b"}', '{"n":1.0}', '{"n":NaN}', '{"n":Infinity}'):
            with self.subTest(encoded=encoded), self.assertRaises(ValidationError):
                load_json(encoded)
        for value in ({"x": 1.0}, {"x": float("nan")}, {1: "key"}, {"x": "\ud800"}):
            with self.subTest(value=repr(value)), self.assertRaises(ValidationError):
                canonical_bytes(value)

    def test_canonical_binding_is_order_independent_and_content_sensitive(self):
        task = example_task()
        self.assertEqual(digest(task), digest(dict(reversed(list(task.items())))))
        changed = deepcopy(task)
        changed["objective"] += " extra"
        self.assertNotEqual(digest(task), digest(changed))
        self.assertFalse(canonical_bytes(task).endswith(b"\n"))
        self.assertEqual(canonical_bytes({"text": "é", "n": 1}), b'{"n":1,"text":"\xc3\xa9"}')


class GoodExamples(unittest.TestCase):
    def test_same_task_unchanged_on_web_headless_ui_and_both_vmms(self):
        portable = canonical_bytes(example_task())
        for backend, profile, vmm in (("web", "web", None), ("docker", "worker", None),
                                      ("docker", "worker_ui", None), ("microvm", "worker", "firecracker"),
                                      ("microvm", "worker", "cloud-hypervisor")):
            with self.subTest(backend=backend, profile=profile, vmm=vmm):
                task, receipt, grant = fixture(backend, profile)
                if vmm:
                    grant["runtime"]["vmm"]["name"] = receipt["runtime"]["vmm"]["name"] = vmm
                    rebind(task, receipt, grant)
                self.assertEqual(canonical_bytes(task), portable)
                ENGINE.validate(task)
                ENGINE.validate(receipt)
                validate_bundle(task, receipt, grant, NOW)

    def test_empty_policy_is_deny_network_and_not_mutated(self):
        task = example_task()
        task["policy"] = {}
        before = canonical_bytes(task)
        grant = example_grant(task)
        receipt = example_receipt(task, grant)
        validate_bundle(task, receipt, grant, NOW)
        self.assertEqual(grant["network"], DENY_NETWORK)
        self.assertEqual(canonical_bytes(task), before)

    def test_explicit_allowlist_requires_matching_seat_and_observation(self):
        task = example_task()
        task["policy"] = {"network": {"mode": "allowlist", "allowlist": [
            {"host": "packages.example.invalid", "port": 443, "transport": "tcp"}]}}
        grant = example_grant(task)
        receipt = example_receipt(task, grant)
        validate_bundle(task, receipt, grant, NOW)

    def test_authorized_seat_may_narrow_allowlist_or_deny_all(self):
        task = example_task()
        endpoints = [{"host": "a.example.invalid", "port": 443, "transport": "tcp"},
                     {"host": "b.example.invalid", "port": 443, "transport": "tcp"}]
        task["policy"] = {"network": {"mode": "allowlist", "allowlist": endpoints}}
        for network in (DENY_NETWORK, {"mode": "allowlist", "allowlist": endpoints[:1]}):
            with self.subTest(network=network):
                grant = example_grant(task)
                set_grant_network(grant, network)
                receipt = example_receipt(task, grant)
                validate_bundle(task, receipt, grant, NOW)

    def test_source_sha_line_request_and_available_host_metric(self):
        task = example_task()
        task["sources"][0]["sha256"] = SOURCE_SHA
        task["evidence"]["requests"] = [{"request_id": "line-one", "kind": "line", "path": "src/work.go", "line": 1}]
        task["host_metric"].update(required=True, target_max=100)
        grant = example_grant(task)
        receipt = example_receipt(task, grant)
        receipt["evidence"]["answers"] = [{"request_id": "line-one", "answer": "synthetic source"}]
        receipt["host_metric"].update(value=50, provenance="host", sample_count=1)
        validate_bundle(task, receipt, grant, NOW)

    def test_patch_and_delete_manifest(self):
        task, receipt, grant = fixture()
        files = receipt["output"]["files"]
        files.append({"path": "tests/work_test.go", "operation": "delete", "mode": "100644", "sha256": None, "bytes": 0})
        receipt["output"] = {"kind": "patch", "base_commit": task["repo"]["commit"],
                             "patch": {"path": "result.patch", "sha256": "9" * 64, "bytes": 128}, "files": files}
        validate_bundle(task, receipt, grant, NOW)

    def test_no_change_result_and_empty_write_surface(self):
        task, receipt, grant = fixture()
        task["write_paths"] = []
        receipt["output"]["files"] = []
        receipt["output"]["head_commit"] = task["repo"]["commit"]
        rebind(task, receipt, grant)
        validate_bundle(task, receipt, grant, NOW)

    def test_child_exit_one_is_preserved_and_normalized_to_five(self):
        task, receipt, grant = fixture()
        receipt.update(exit_code=5, failure_reason="gate", output=None)
        receipt["gate"].update(raw_exit_code=1, exit_code=5)
        validate_bundle(task, receipt, grant, NOW)
        self.assertEqual(receipt["gate"]["raw_exit_code"], 1)

    def test_prelaunch_refusal_has_null_partial_observations(self):
        for code, reason in ((2, "contract"), (3, "setup"), (4, "unavailable"), (7, "operator_capability"), (7, "operator_recipe")):
            with self.subTest(code=code):
                task, receipt, grant = fixture()
                receipt.update(exit_code=code, failure_reason=reason, output=None, gate=None,
                               observations=None, sources_observed=[])
                receipt["evidence"]["answers"] = []
                receipt["cleanup"] = {"status": "not_applicable", "remaining_pids": None,
                                      "remaining_workspace_bytes": None, "resources_released": None}
                validate_bundle(task, receipt, grant, NOW)

    def test_failed_gate_signal_timeout_cancel_oom_keep_raw_status(self):
        for raw, reason, gate_code, failure in ((-15, "signal", 5, "gate"), (137, "timeout", 5, "timeout"),
                                               (None, "cancelled", 5, "cancelled"), (-9, "oom", 5, "oom"),
                                               (None, None, 3, "execution")):
            with self.subTest(raw=raw, reason=reason):
                task, receipt, grant = fixture()
                receipt.update(exit_code=gate_code, failure_reason=failure, output=None)
                receipt["gate"].update(raw_exit_code=raw, terminated_by=reason, exit_code=gate_code)
                validate_bundle(task, receipt, grant, NOW)

    def test_cleanup_and_storage_violations_can_be_reported_as_failure(self):
        for failure in ("indeterminate", "storage", "containment"):
            with self.subTest(failure=failure):
                task, receipt, grant = fixture()
                receipt.update(exit_code=6 if failure == "indeterminate" else 5, failure_reason=failure, output=None)
                if failure == "indeterminate":
                    receipt["cleanup"].update(status="failed", remaining_pids=1, resources_released=False)
                elif failure == "storage":
                    receipt["observations"]["frames_disk_bytes"] = 16
                    receipt["observations"]["frames_root"] = "/tmp/unsafe-frames-observed"
                else:
                    receipt["observations"]["network"] = {"mode": "host", "allowlist": []}
                    receipt["observations"]["uid"] = 0
                validate_bundle(task, receipt, grant, NOW)

    def test_unknown_child_and_cleanup_require_indeterminate_receipt(self):
        task, receipt, grant = fixture()
        receipt.update(exit_code=6, failure_reason="indeterminate", output=None)
        receipt["gate"].update(raw_exit_code=None, terminated_by="unknown", exit_code=6)
        receipt["cleanup"] = {"status": "not_observed", "remaining_pids": None,
                              "remaining_workspace_bytes": None, "resources_released": None}
        validate_bundle(task, receipt, grant, NOW)


class MustFailStructural(unittest.TestCase):
    def test_task_mutants(self):
        mutants = [
            (("repo", "commit"), "bridge/integration"), (("repo", "commit"), "a" * 39),
            (("repo", "commit"), "A" * 40), (("repo", "url"), "https://user:secret@example.invalid/team/repo"),
            (("repo", "url"), "https://example.invalid/team/../repo"), (("repo", "url"), "file:///tmp/repo"),
            (("gate", "timeout_ms"), True), (("gate", "timeout_ms"), 0), (("gate", "timeout_ms"), 1.5),
            (("gate", "argv"), "go test ./..."), (("gate", "argv"), []), (("gate", "argv"), ["go\x00test"]),
            (("gate", "cwd"), "../repo"), (("objective",), ""), (("objective",), "a" * 16385),
            (("sources",), []), (("sources", 0, "sha256"), "z" * 64),
            (("evidence", "max_bytes"), 102401), (("evidence", "requests", 0, "kind"), "execute"),
            (("evidence", "requests", 0, "line"), 1), (("host_metric", "target_max"), False),
            (("consumer", "name"), "skip-verifier"), (("policy",), {"frames_root": "/tmp"}),
            (("policy",), {"backend": "docker"}), (("policy",), {"network": {"mode": "host", "allowlist": []}}),
            (("policy",), {"network": {"mode": "allowlist", "allowlist": []}}),
            (("policy",), {"network": {"mode": "none", "allowlist": [{"host": "example.invalid", "port": 443, "transport": "tcp"}]}}),
        ]
        for path, value in mutants:
            with self.subTest(path=path, value=value):
                task = example_task()
                set_at(task, path, value)
                with self.assertRaises(ValidationError):
                    ENGINE.validate(task)

    def test_required_fields_and_unknown_keys_at_each_level(self):
        for key in SCHEMA["$defs"]["task"]["required"]:
            with self.subTest(missing=key):
                task = example_task()
                del task[key]
                with self.assertRaises(ValidationError):
                    ENGINE.validate(task)
        for path in ((), ("repo",), ("gate",), ("sources", 0), ("evidence",),
                     ("evidence", "requests", 0), ("consumer",), ("host_metric",)):
            with self.subTest(extra_at=path):
                task = example_task()
                current = task
                for key in path:
                    current = current[key]
                current["unexpected"] = True
                with self.assertRaises(ValidationError):
                    ENGINE.validate(task)

    def test_unsafe_path_mutants(self):
        for path in ("/etc/passwd", "../outside", "src/../../x", "src/./x", "src//x", "src\\x",
                     "src/%2e%2e/x", ".git/config", "src/x\x00", "src/test.py\n", "src/test.py\r\n",
                     "src/*", "src/", ".", "..", ""):
            for field in ("sources", "write_paths"):
                with self.subTest(path=path, field=field):
                    task = example_task()
                    if field == "sources":
                        task["sources"][0]["path"] = path
                    else:
                        task["write_paths"] = [path]
                    with self.assertRaises(ValidationError):
                        ENGINE.validate(task)

    def test_network_endpoint_mutants(self):
        for key, value in (("host", "*.example.invalid"), ("host", "127.0.0.1"), ("host", "::1"),
                           ("host", "EXAMPLE.invalid"), ("host", "example.invalid/escape"),
                           ("port", 0), ("port", 65536), ("port", True), ("transport", "udp")):
            with self.subTest(key=key, value=value):
                endpoint = {"host": "packages.example.invalid", "port": 443, "transport": "tcp"}
                endpoint[key] = value
                task = example_task()
                task["policy"] = {"network": {"mode": "allowlist", "allowlist": [endpoint]}}
                with self.assertRaises(ValidationError):
                    ENGINE.validate(task)

    def test_receipt_mutants(self):
        mutants = [
            (("exit_code",), 1), (("exit_code",), True), (("exit_code",), 8),
            (("gate", "exit_code"), 1), (("gate", "raw_exit_code"), 1), (("gate",), None),
            (("output",), None), (("output", "kind"), "both"), (("output", "patch"), {}),
            (("output", "branch"), "../main"), (("output", "files", 0, "mode"), "120000"),
            (("output", "files", 0, "sha256"), None), (("output", "files", 0, "bytes"), False),
            (("observations", "uid"), 0), (("observations", "uid"), True),
            (("observations", "rootfs_read_only"), False), (("observations", "cap_drop"), []),
            (("observations", "frames_root"), "/tmp/frames"), (("observations", "frames_root"), "/dev/shm/../tmp/frames"),
            (("observations", "frames_disk_bytes"), 1), (("observations", "swap_enabled"), True),
            (("observations", "core_dumps_enabled"), True), (("observations", "no_runtime_socket"), False),
            (("observations", "no_host_mounts"), False), (("observations", "public_listeners"), 1),
            (("observations", "peak_memory_bytes"), None), (("observations", "ttl_enforced"), False),
            (("cleanup", "status"), "not_observed"), (("cleanup", "remaining_pids"), 1),
            (("cleanup", "remaining_workspace_bytes"), 1), (("cleanup", "resources_released"), False),
            (("host_metric", "value"), 0), (("host_metric", "sample_count"), 1),
            (("runtime", "vmm"), {"name": "new-vmm", "version": "x", "kernel_sha256": "f" * 64}),
        ]
        for path, value in mutants:
            with self.subTest(path=path, value=value):
                _, receipt, _ = fixture()
                set_at(receipt, path, value)
                with self.assertRaises(ValidationError):
                    ENGINE.validate(receipt)

    def test_failure_cannot_advertise_success_output(self):
        _, receipt, _ = fixture()
        receipt.update(exit_code=5, failure_reason="gate")
        receipt["gate"].update(raw_exit_code=1, exit_code=5)
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)

    def test_unknown_cleanup_cannot_be_a_known_failure_or_operator_decision(self):
        for code, reason in ((3, "execution"), (5, "timeout"), (7, "operator_capability")):
            with self.subTest(code=code):
                _, receipt, _ = fixture()
                receipt.update(exit_code=code, failure_reason=reason, output=None)
                receipt["cleanup"].update(status="failed", remaining_pids=1, resources_released=False)
                with self.assertRaises(ValidationError):
                    ENGINE.validate(receipt)

    def test_receipt_required_fields_and_no_extra_success_flag(self):
        for key in SCHEMA["$defs"]["receipt"]["required"]:
            with self.subTest(missing=key):
                _, receipt, _ = fixture()
                del receipt[key]
                with self.assertRaises(ValidationError):
                    ENGINE.validate(receipt)
        _, receipt, _ = fixture()
        receipt["accepted"] = True
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)

    def test_seat_is_not_a_portable_task_or_root_receipt(self):
        _, _, grant = fixture()
        ENGINE.validate(grant, "seat_grant")
        with self.assertRaises(ValidationError):
            ENGINE.validate(grant)
        for path, value in ((("isolation", "uid"), 0), (("isolation", "frames_root"), "/tmp/fb"),
                            (("limits", "cpuset"), []), (("limits", "cpuset"), [10, 10]),
                            (("limits", "memory_bytes"), 0), (("limits", "memory_bytes"), True),
                            (("isolation", "swap_enabled"), True)):
            with self.subTest(path=path, value=value):
                _, _, changed = fixture()
                set_at(changed, path, value)
                with self.assertRaises(ValidationError):
                    ENGINE.validate(changed, "seat_grant")


class MustFailBindings(unittest.TestCase):
    def test_stale_digest_identity_repo_and_backend_mutants(self):
        for path, value in ((("task_id",), "other-task"), (("attempt_id",), "a2"), (("lease_id",), "lease-stale"),
                            (("task_sha256",), "0" * 64), (("seat_grant_sha256",), "0" * 64),
                            (("seat_id",), "other-seat"), (("lease_nonce",), "0" * 64),
                            (("repo", "commit"), "b" * 40), (("repo", "url"), "https://example.invalid/team/other"),
                            (("runtime", "base_image", "digest"), "0" * 64), (("runtime", "profile"), "worker_ui"),
                            (("gate", "argv"), ["true"]), (("gate", "cwd"), "tests"),
                            (("gate", "timeout_ms"), 9000), (("output", "base_commit"), "c" * 40),
                            (("output", "repo_url"), "https://example.invalid/team/other"),
                            (("output", "files", 0, "path"), "outside.txt"),
                            (("host_metric", "name"), "other"), (("host_metric", "unit"), "bytes")):
            with self.subTest(path=path, value=value):
                task, receipt, grant = fixture()
                set_at(receipt, path, value)
                # These mutations remain structurally valid but must not ingest.
                ENGINE.validate(receipt)
                with self.assertRaises(ValidationError):
                    validate_bundle(task, receipt, grant, NOW)

    def test_source_observation_and_evidence_completeness(self):
        for field, value in (("sources_observed", []), ("sources_observed", [{"path": "other.go", "sha256": SOURCE_SHA}]),
                             ("sources_observed", [{"path": "src/work.go", "sha256": SOURCE_SHA},
                                                   {"path": "src/work.go", "sha256": "0" * 64}])):
            with self.subTest(field=field, value=value):
                task, receipt, grant = fixture()
                receipt[field] = value
                with self.assertRaises(ValidationError):
                    validate_bundle(task, receipt, grant, NOW)
        for answers in ([], [{"request_id": "not-requested", "answer": "x"}],
                        [{"request_id": "source", "answer": "x"}, {"request_id": "source", "answer": "y"}]):
            with self.subTest(answers=answers):
                task, receipt, grant = fixture()
                receipt["evidence"]["answers"] = answers
                with self.assertRaises(ValidationError):
                    validate_bundle(task, receipt, grant, NOW)
        task, receipt, grant = fixture()
        task["sources"][0]["sha256"] = "0" * 64
        rebind(task, receipt, grant)
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, NOW)

    def test_sha_answer_must_match_observed_source_on_success(self):
        for answer in ("ABSENT", "0" * 64, SOURCE_SHA.upper(), "not-a-hash"):
            with self.subTest(answer=answer):
                task, receipt, grant = fixture()
                receipt["evidence"]["answers"][0]["answer"] = answer
                with self.assertRaises(ValidationError):
                    validate_bundle(task, receipt, grant, NOW)

    def test_duplicate_semantic_paths_and_request_ids(self):
        task = example_task()
        task["sources"].append({"path": "src/work.go", "sha256": SOURCE_SHA})
        with self.assertRaises(ValidationError):
            validate_task(task)
        task = example_task()
        task["evidence"]["requests"].append({"request_id": "source", "kind": "line", "path": "src/work.go", "line": 1})
        with self.assertRaises(ValidationError):
            validate_task(task)
        task = example_task()
        task["evidence"]["requests"][0]["path"] = "unknown.go"
        with self.assertRaises(ValidationError):
            validate_task(task)

    def test_future_stale_unordered_expired_and_replayed_receipts(self):
        for field, value in (("started_at_ms", ISSUED - 1), ("finished_at_ms", ISSUED),
                             ("finished_at_ms", NOW + 1)):
            with self.subTest(field=field, value=value):
                task, receipt, grant = fixture()
                receipt[field] = value
                with self.assertRaises(ValidationError):
                    validate_bundle(task, receipt, grant, NOW)
        task, receipt, grant = fixture()
        for now in (True, grant["collect_until_ms"] + 1):
            with self.subTest(now=now), self.assertRaises(ValidationError):
                validate_bundle(task, receipt, grant, now)
        receipt["finished_at_ms"] = grant["expires_at_ms"] + 1
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, receipt["finished_at_ms"])
        task, receipt, grant = fixture()
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, NOW, {(grant["seat_id"], grant["lease_nonce"])})

    def test_invalid_lease_interval_and_resource_grant(self):
        for path, value in ((("expires_at_ms",), ISSUED), (("collect_until_ms",), ISSUED),
                            (("collect_until_ms",), ISSUED + DEFAULT_TTL_MS + 60001),
                            (("expires_at_ms",), ISSUED + DEFAULT_TTL_MS + 1),
                            (("limits", "cpuset"), [11, 10]), (("limits", "ttl_ms"), 5000),
                            (("limits", "workspace_bytes"), 536870912),
                            (("isolation", "frames_root"), "/dev/shm/worker-lease-example/repo")):
            with self.subTest(path=path, value=value):
                task, receipt, grant = fixture()
                set_at(grant, path, value)
                rebind(task, receipt, grant)
                with self.assertRaises(ValidationError):
                    validate_bundle(task, receipt, grant, NOW)

    def test_network_grant_and_observation_cannot_escalate(self):
        allow = {"mode": "allowlist", "allowlist": [{"host": "packages.example.invalid", "port": 443, "transport": "tcp"}]}
        task, receipt, grant = fixture()
        grant["network"] = deepcopy(allow)
        receipt["observations"]["network"] = deepcopy(allow)
        rebind(task, receipt, grant)
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, NOW)
        task, receipt, grant = fixture()
        receipt["observations"]["network"] = deepcopy(allow)
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, NOW)

    def test_resource_and_headless_observation_mismatches(self):
        for field, value in (("cpuset", [12]), ("memory_limit_bytes", 536870912), ("pids_limit", 65),
                             ("workspace_limit_bytes", 268435456), ("peak_memory_bytes", 268435457),
                             ("peak_pids", 65), ("workspace_peak_bytes", 134217729),
                             ("seccomp_profile_sha256", "0" * 64), ("xpra", "uds"), ("x11_listeners", 1),
                             ("workspace_root", "/workspace/other"), ("frames_root", "/dev/shm/other/frames")):
            with self.subTest(field=field, value=value):
                task, receipt, grant = fixture()
                receipt["observations"][field] = value
                ENGINE.validate(receipt)
                with self.assertRaises(ValidationError):
                    validate_bundle(task, receipt, grant, NOW)

    def test_false_gate_normalization_is_refused_but_raw_one_is_not_lost(self):
        task, receipt, grant = fixture()
        receipt.update(exit_code=5, failure_reason="gate", output=None)
        receipt["gate"].update(raw_exit_code=1, exit_code=0)
        ENGINE.validate(receipt)
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, NOW)

    def test_missing_metrics_over_target_and_wrong_provenance(self):
        task, receipt, grant = fixture()
        task["host_metric"]["required"] = True
        rebind(task, receipt, grant)
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, NOW)
        task["host_metric"]["target_max"] = 10
        receipt["host_metric"].update(value=11, provenance="host", sample_count=1)
        rebind(task, receipt, grant)
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, NOW)
        receipt["host_metric"]["provenance"] = "unavailable"
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)

    def test_evidence_and_code_caps_are_separate_and_bounded(self):
        task, receipt, grant = fixture()
        task["evidence"]["max_bytes"] = 1024
        rebind(task, receipt, grant)
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, NOW)
        task, receipt, grant = fixture()
        task["policy"] = {"output_max_bytes": 1}
        rebind(task, receipt, grant)
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, NOW)

    def test_log_count_digest_and_truncation_must_agree(self):
        for field, value in (("retained_bytes", 1), ("total_bytes", 1), ("truncated", True)):
            with self.subTest(field=field, value=value):
                task, receipt, grant = fixture()
                receipt["gate"]["stdout"][field] = value
                with self.assertRaises(ValidationError):
                    validate_bundle(task, receipt, grant, NOW)

    def test_changed_manifest_cannot_claim_base_commit_or_duplicate_path(self):
        task, receipt, grant = fixture()
        receipt["output"]["head_commit"] = receipt["output"]["base_commit"]
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, NOW)
        task, receipt, grant = fixture()
        duplicate = deepcopy(receipt["output"]["files"][0])
        duplicate["sha256"] = "0" * 64
        receipt["output"]["files"].append(duplicate)
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, NOW)

    def test_gate_ttl_and_nested_git_write_paths_are_refused(self):
        task = example_task()
        task["gate"]["timeout_ms"] = DEFAULT_TTL_MS + 1
        with self.assertRaises(ValidationError):
            validate_task(task)
        task = example_task()
        task["write_paths"] = ["nested/.git/config"]
        with self.assertRaises(ValidationError):
            validate_task(task)



# ---------------------------------------------------------------- AMEND AW8
# Reference model of bridge_codec.go (ADV-W8-03). bridge-task-v1 stays the
# wire document: it is embedded verbatim in task["bridge"]; the worker fields
# are a deterministic projection, so the bridge task is recovered losslessly.
BRIDGE_REPO_PREFIX = "https://github.com/"


def bridge_gate_argv(gates):
    require(all("\n" not in gate and "\r" not in gate for gate in gates), "multi-line acceptance gate")
    return ["/bin/sh", "-ec", "\n".join(gates)]


def bridge_requests(requests):
    out = []
    for index, text in enumerate(requests):
        request_id = "e" + str(index)
        if text.startswith("sha256:"):
            out.append({"request_id": request_id, "kind": "sha256", "path": text[len("sha256:"):]})
        elif text.startswith("line:"):
            path, _, number = text[len("line:"):].rpartition(":")
            require(bool(path) and number.isdigit() and int(number) >= 1, "bad line evidence request")
            out.append({"request_id": request_id, "kind": "line", "path": path, "line": int(number)})
        elif text.startswith("marker:"):
            path, separator, marker = text[len("marker:"):].partition(":")
            require(bool(separator) and bool(marker), "bad marker evidence request")
            out.append({"request_id": request_id, "kind": "marker", "path": path, "text": marker})
        else:
            out.append({"request_id": request_id, "kind": "text", "text": text})
    return out


def bridge_projection(bridge):
    ENGINE.validate(bridge, "bridge_task_v1")
    commit = bridge["observed_source_commit"]
    require(commit is not None and len(commit) == 40, "bridge task source commit unresolved")
    requests = bridge_requests(bridge["evidence_requests"])
    paths = list(bridge["source_files"])
    for item in requests:
        if "path" in item and item["path"] not in paths:
            paths.append(item["path"])
    return {"task_id": bridge["task_id"], "attempt_id": bridge["attempt_id"], "lease_id": bridge["lease_id"],
            "repo": {"url": BRIDGE_REPO_PREFIX + bridge["repo"], "commit": commit}, "source_paths": paths,
            "write_paths": list(bridge["allowed_write_paths"]), "argv": bridge_gate_argv(bridge["acceptance_gates"]),
            "cwd": ".", "requests": requests}


def bridge_to_worker(bridge, objective, timeout_ms):
    view = bridge_projection(bridge)
    task = {"schema": "os.cu.worker-task/v1", "task_id": view["task_id"], "attempt_id": view["attempt_id"],
            "lease_id": view["lease_id"], "repo": view["repo"], "objective": objective,
            "sources": [{"path": path, "sha256": None} for path in view["source_paths"]],
            "write_paths": view["write_paths"],
            "gate": {"argv": view["argv"], "cwd": view["cwd"], "timeout_ms": timeout_ms},
            "evidence": {"requests": view["requests"], "max_bytes": 102400},
            "consumer": {"name": "auto_ingest"},
            "host_metric": {"name": "elapsed_ms", "unit": "ms", "target_max": None, "required": False},
            "bridge": deepcopy(bridge)}
    validate_task(task)
    return task


# JW8-01: the 9 identifiers of .wavemind/bridge/PROTOCOL.md rule 5 plus `role`.
# `surface` is never copied: a live result is encoded only for an os-capsule
# task (codec constant); any other surface yields a parity-only envelope that
# the outbox writer refuses.
BRIDGE_IDS = ("task_id", "thread", "agent", "worker_id", "attempt_id", "lease_id", "key", "base_seq",
              "done_signal", "role")
OS_SURFACE = "os-capsule"


def encode_bridge_result(task, receipt, prompt_id_line, live):
    surface = task["bridge"]["surface"]
    if live:
        if surface != OS_SURFACE:
            raise Unavailable("live encode refused: surface " + surface + " is not " + OS_SURFACE)
        return {"mode": "live", "result": receipt_to_bridge_result(task, receipt, prompt_id_line, OS_SURFACE)}
    return {"mode": "parity", "result": receipt_to_bridge_result(task, receipt, prompt_id_line, surface)}


def outbox_write(envelope):
    """Only live os-capsule results may reach the outbox; returns the canonical bytes."""
    require(envelope["mode"] == "live" and envelope["result"]["surface"] == OS_SURFACE,
            "parity-only result is never written to the outbox")
    return canonical_bytes(envelope["result"])


def receipt_to_bridge_result(task, receipt, prompt_id_line, surface):
    """Receipt -> bridge-result-v1. The receipt travels inside report_path (the
    only non-allowed_write_paths files a builder may declare are result_path and
    report_path), and its digest is bound in source_binding.evidence."""
    bridge = task["bridge"]
    receipt_sha = digest(receipt)
    binding = ("os.cu.worker-receipt/v1 task_sha256=" + receipt["task_sha256"] + " receipt_sha256=" + receipt_sha
               + " rc=" + str(receipt["exit_code"]))
    if receipt["exit_code"] == 0:
        answers = [{"request_index": int(item["request_id"][1:]), "answer": item["answer"]}
                   for item in receipt["evidence"]["answers"]]
        artifacts = [{"path": item["path"], "sha256": item["sha256"]} for item in receipt["output"]["files"]]
        evidence = prompt_id_line + "; " + binding
    else:
        answers = [{"request_index": index, "answer": "ABSENT"} for index in range(len(bridge["evidence_requests"]))]
        artifacts = []
        evidence = "BLOCKED: OS capsule attempt failed, " + binding + " reason=" + str(receipt["failure_reason"])
    artifacts += [{"path": bridge["result_path"], "sha256": None}, {"path": bridge["report_path"], "sha256": None}]
    result = {"schema_version": "bridge-result-v1", **{key: bridge[key] for key in BRIDGE_IDS}, "surface": surface,
              "source_binding": {"requested_ref": bridge["requested_ref"], "observed_commit": receipt["repo"]["commit"],
                                 "mode": "vm_clone", "evidence": evidence},
              "content": {"path": bridge["report_path"], "sha256": None},
              "artifacts": artifacts, "evidence_answers": answers}
    if receipt["output"] is not None and receipt["output"]["kind"] == "branch":
        result["branch"] = receipt["output"]["branch"]
    ENGINE.validate(result, "bridge_result_v1")
    allowed = set(bridge["allowed_write_paths"]) | {bridge["result_path"], bridge["report_path"]}
    require(all(item["path"] in allowed for item in result["artifacts"]), "artifact outside bridge surface")
    return result


def example_bridge_task():
    # Shape of .wavemind/bridge/inbox/chatgpt/os-w8-worker-bank-design/a1/task.json (synthetic values).
    return {"schema_version": "bridge-task-v1", "task_id": "os-w8-fixture", "thread": "f-os-w8-fixture",
            "agent": "jules", "worker_id": "jules-f", "attempt_id": "a1", "surface": "jules", "role": "builder",
            "repo": "team/project", "requested_ref": "bridge/integration", "observed_source_commit": "a" * 40,
            "lease_id": "lease-0123456789abcdef", "key": "os-w8-fixture.a1.jules-f", "base_seq": 0,
            "expires_at_utc": "2026-10-23T18:52:36Z", "allowed_write_paths": ["src/work.go", "tests/work_test.go"],
            "writer_branch": "bridge/os-w8-fixture/a1", "integration_target": "bridge/integration",
            "result_path": ".wavemind/bridge/outbox/jules/os-w8-fixture/a1/result.json",
            "report_path": ".wavemind/bridge/outbox/jules/os-w8-fixture/a1/report.md",
            "acceptance_gates": ["go test ./..."],
            "evidence_requests": ["sha256:src/work.go", "line:docs/W4.md:1", "marker:docs/W4.md:wave",
                                  "State the gate result."],
            "source_files": ["src/work.go"], "done_signal": "READY_FOR_COLLECTION os-w8-fixture a1"}


def bridge_fixture():
    task = bridge_to_worker(example_bridge_task(), "Bridge PROMPT.md for os-w8-fixture a1 (synthetic).", 10000)
    grant = example_grant(task)
    receipt = example_receipt(task, grant)
    receipt["sources_observed"] = [{"path": item["path"], "sha256": SOURCE_SHA} for item in task["sources"]]
    receipt["evidence"]["answers"] = [{"request_id": "e0", "answer": SOURCE_SHA},
                                      {"request_id": "e1", "answer": "# W4"},
                                      {"request_id": "e2", "answer": "# W4 wave"},
                                      {"request_id": "e3", "answer": "gate rc 0"}]
    return task, receipt, grant


# Reference model of the A4 base-image builder gates (ONE hardened aarch64 base
# image, rare build, operator approval file JW8-02). A1 per-commit images are
# superseded: repo code never enters an image.
SECRET_CONTEXT = re.compile(r"(?:^|[\s\"'=])(?:~|\$HOME|/home/[^/\s]+|/root)/\.(?:config|ssh|gnupg|aws|docker)\b"
                            r"|(?:^|[\s/])(?:operator|launch)\.token\b|\.key\b|auth\.json\b|\.beckett/token")


def recipe_admission(recipe_text, recipe_sha256, approval):
    """rc 0 build allowed; rc 5 leaking recipe refused; rc 7 operator_recipe pending."""
    if SECRET_CONTEXT.search(recipe_text) or re.search(r"(?m)^\s*(?:COPY|ADD)\s+(?:/|\.\./)", recipe_text):
        return 5
    if approval is None or approval.get("recipe_sha256") != recipe_sha256:
        return 7
    return 0


def image_digest_model(recipe_sha256, context_files):
    # Reproducible build model: digest is a function of the recipe and the
    # sorted recipe-dir context only (no clock, no host paths, no repo commit).
    return digest({"recipe": recipe_sha256,
                   "context": sorted([path, sha] for path, sha in context_files.items())})


def retain_last(tags, keep):
    return tags[-keep:] if keep > 0 else []


# Reference model of the A2 aggregator and vigia-enjambre throttle.
def aggregate(acts):
    seen, findings = set(), []
    for act in acts:
        ENGINE.validate(act, "speech_act")
        if act["act"] in ("finding", "repro") and act["repro_sha256"] not in seen:
            seen.add(act["repro_sha256"])
            findings.append(act)
    return findings


def vigia(acts, window_max=8, stuck_after=3):
    """Returns (throttled lease_ids, stuck lease_ids, duplicate lease_ids)."""
    per_lease, status_runs, owners, duplicates = {}, {}, {}, set()
    for act in acts:
        per_lease[act["lease_id"]] = per_lease.get(act["lease_id"], 0) + 1
        if act["act"] == "status":
            run = status_runs.get(act["lease_id"], 0)
            status_runs[act["lease_id"]] = run + 1 if act["status"] in ("working", "stuck") else 0
        if act["repro_sha256"] is not None:
            first = owners.setdefault(act["repro_sha256"], act["lease_id"])
            if first != act["lease_id"]:
                duplicates.add(act["lease_id"])
    throttled = {lease for lease, count in per_lease.items() if count > window_max}
    stuck = {lease for lease, run in status_runs.items() if run >= stuck_after}
    return throttled, stuck, duplicates


def admit(free_ram_bytes, floor_bytes, reservations, request_bytes, budget_bytes):
    return (free_ram_bytes - request_bytes >= floor_bytes
            and sum(reservations) + request_bytes <= budget_bytes)


def supervise(outcomes, max_restarts):
    """OTP-style one_for_one: restart on FAILED/INDETERMINATE up to max_restarts, then give up."""
    restarts = 0
    for outcome in outcomes:
        if outcome == "SUCCEEDED":
            return "SUCCEEDED", restarts
        if restarts == max_restarts:
            return "GAVE_UP", restarts
        restarts += 1
    return "RUNNING", restarts


def speech(act, lease, seq, repro=None, status=None, affected=()):
    return {"schema": "os.cu.speech-act/v1", "act": act, "wave_id": "wave-1", "task_id": "task-example",
            "attempt_id": "a1", "lease_id": lease, "seq": seq, "repro_sha256": repro,
            "affected": list(affected), "status": status, "body": ""}


def lease(state, receipt=None, kind="microvm"):
    return {"schema": "os.cu.lease/v1", "lease_id": "lease-example", "target_id": "vm-0", "target_kind": kind,
            "state": state, "restarts": 0, "receipt": receipt}


class AmendFindings(unittest.TestCase):
    def test_adv01_schema_found_in_both_layouts_and_markers_documented(self):
        self.assertTrue(SCHEMA_PATH.is_file())
        self.assertIn("contracts/worker/tests/__init__.py", __doc__)

    def test_adv02_nested_git_refused_by_schema_alone(self):
        for path in ("a/.git/config", "a/b/.git", ".git/HEAD", "x/.git/hooks/pre-commit"):
            with self.subTest(path=path):
                task = example_task()
                task["write_paths"] = [path]
                with self.assertRaises(ValidationError):
                    ENGINE.validate(task)
        task = example_task()
        task["write_paths"] = ["a/.gitignore", "a/.github/ci.yml"]
        ENGINE.validate(task)

    def test_adv02_failed_receipt_cannot_hide_storage_or_isolation_breach(self):
        cases = ((("frames_disk_bytes",), 16, "gate"), (("swap_enabled",), True, "timeout"),
                 (("core_dumps_enabled",), True, "oom"), (("uid",), 0, "gate"),
                 (("rootfs_read_only",), False, "verification"), (("public_listeners",), 1, "storage"),
                 (("frames_disk_bytes",), 16, "containment"))
        for backend in ("docker", "microvm"):
            for path, value, reason in cases:
                with self.subTest(backend=backend, path=path, reason=reason):
                    _, receipt, _ = fixture(backend)
                    receipt.update(exit_code=5, failure_reason=reason, output=None)
                    receipt["gate"].update(raw_exit_code=1, exit_code=5)
                    set_at(receipt["observations"], path, value)
                    with self.assertRaises(ValidationError):
                        ENGINE.validate(receipt)

    def test_adv02_observed_capsule_needs_verified_cleanup_unless_rc6(self):
        for code, reason in ((3, "execution"), (5, "gate"), (4, "unavailable"), (7, "operator_capability"),
                             (2, "contract")):
            with self.subTest(code=code):
                _, receipt, _ = fixture("microvm")
                receipt.update(exit_code=code, failure_reason=reason, output=None)
                receipt["gate"].update(raw_exit_code=1, exit_code=5)
                receipt["cleanup"] = {"status": "not_applicable", "remaining_pids": None,
                                      "remaining_workspace_bytes": None, "resources_released": None}
                with self.assertRaises(ValidationError):
                    ENGINE.validate(receipt)

    def test_adv03_bridge_task_round_trips_and_validates(self):
        task, receipt, grant = bridge_fixture()
        validate_bundle(task, receipt, grant, NOW)
        self.assertEqual(task["bridge"], example_bridge_task())
        result = encode_bridge_result(task, receipt, "PROMPT-ID os-w8-fixture a1 0123456789abcdef", live=False)["result"]
        for key in BRIDGE_IDS:
            self.assertEqual(result[key], task["bridge"][key])
        self.assertEqual([item["request_index"] for item in result["evidence_answers"]], [0, 1, 2, 3])
        self.assertIn(digest(receipt), result["source_binding"]["evidence"])

    def test_adv03_failed_receipt_maps_to_blocked_receipt(self):
        task, receipt, grant = bridge_fixture()
        receipt.update(exit_code=5, failure_reason="gate", output=None)
        receipt["gate"].update(raw_exit_code=1, exit_code=5)
        result = encode_bridge_result(task, receipt, "PROMPT-ID os-w8-fixture a1 0123456789abcdef", live=False)["result"]
        self.assertTrue(all(item["answer"] == "ABSENT" for item in result["evidence_answers"]))
        self.assertTrue(result["source_binding"]["evidence"].startswith("BLOCKED"))
        self.assertNotIn("branch", result)

    def test_adv03_projection_drift_is_refused(self):
        mutants = (("task_id", "other-task"), ("lease_id", "lease-other"), ("write_paths", ["src/work.go"]),
                   ("write_paths", ["src/work.go", "tests/work_test.go", "extra.go"]))
        for key, value in mutants:
            with self.subTest(key=key):
                task, _, _ = bridge_fixture()
                task[key] = value
                with self.assertRaises(ValidationError):
                    validate_task(task)
        task, _, _ = bridge_fixture()
        task["gate"]["argv"] = ["go", "test", "./..."]
        with self.assertRaises(ValidationError):
            validate_task(task)
        task, _, _ = bridge_fixture()
        task["repo"]["url"] = "https://github.com/team/other"
        with self.assertRaises(ValidationError):
            validate_task(task)

    def test_adv03_unmappable_bridge_tasks_are_refused(self):
        for key, value in (("observed_source_commit", None), ("acceptance_gates", ["go test\nrm -rf x"]),
                           ("schema_version", "bridge-task-v2"), ("surface", "os-capsule"),
                           ("evidence_requests", ["line:docs/W4.md:zero"])):
            with self.subTest(key=key):
                bridge = example_bridge_task()
                bridge[key] = value
                with self.assertRaises(ValidationError):
                    bridge_to_worker(bridge, "objective", 10000)
        bridge = example_bridge_task()
        bridge["unknown"] = 1
        with self.assertRaises(ValidationError):
            bridge_to_worker(bridge, "objective", 10000)

    def test_adv03_result_cannot_declare_artifact_outside_bridge_surface(self):
        task, receipt, _ = bridge_fixture()
        receipt["output"]["files"].append({"path": "tests/work_test.go", "operation": "modify", "mode": "100644",
                                           "sha256": CHANGE_SHA, "bytes": len(CHANGED_BYTES)})
        task["bridge"]["allowed_write_paths"] = ["src/work.go"]
        with self.assertRaises(ValidationError):
            encode_bridge_result(task, receipt, "PROMPT-ID os-w8-fixture a1 0123456789abcdef", live=False)["result"]

    def test_jw801_live_encode_of_non_os_surface_is_refused_rc4(self):
        task, receipt, grant = bridge_fixture()
        validate_bundle(task, receipt, grant, NOW)
        with self.assertRaises(Unavailable) as caught:
            encode_bridge_result(task, receipt, "PROMPT-ID os-w8-fixture a1 0123456789abcdef", live=True)
        self.assertEqual(caught.exception.rc, 4)
        parity = encode_bridge_result(task, receipt, "PROMPT-ID os-w8-fixture a1 0123456789abcdef", live=False)
        self.assertEqual(parity["mode"], "parity")
        with self.assertRaises(ValidationError):
            outbox_write(parity)
        self.assertNotIn("surface", BRIDGE_IDS)
        self.assertEqual(len(BRIDGE_IDS), 10)
        self.assertNotIn(OS_SURFACE, SCHEMA["$defs"]["bridge_result_v1"]["properties"]["surface"]["enum"])

    def test_adv03_mirror_matches_canon_when_present(self):
        for kind in ("task", "result"):
            path = CANON_BRIDGE_SCHEMAS / (kind + ".schema.json")
            if not path.is_file():
                self.skipTest("canon WEB_BRIDGE schemas not present on this host")
            canon = load_json(path.read_text(encoding="utf-8"))
            mirror = SCHEMA["$defs"]["bridge_" + kind + "_v1"]
            self.assertEqual(canonical_bytes(mirror["properties"]), canonical_bytes(canon["properties"]))
            self.assertEqual(sorted(mirror["required"]), sorted(canon["required"]))
            self.assertIs(canon.get("additionalProperties"), False)

    def test_a4_runtime_is_base_image_digest_plus_commit(self):
        # ADV-W8-04 now answered by A4.1: base_image digest + commit, no per-commit tag.
        for key, value in (("image_tag", "project@" + "a" * 40), ("image_sha256", "e" * 64),
                           ("env_recipe_sha256", "9" * 64), ("commit", "main"), ("extract", "docker-cp"),
                           ("inference", "capsule")):
            with self.subTest(key=key, value=value):
                _, receipt, _ = fixture()
                receipt["runtime"][key] = value
                with self.assertRaises(ValidationError):
                    ENGINE.validate(receipt)
        for path, value in ((("base_image", "name"), "project@" + "a" * 40), (("base_image", "digest"), "x"),
                            (("workspace", "kind"), "volume"), (("workspace", "purge"), "never"),
                            (("workspace", "cache", "read_only"), False), (("workspace", "root"), "/home/user")):
            with self.subTest(path=path):
                _, receipt, _ = fixture()
                set_at(receipt["runtime"], path, value)
                with self.assertRaises(ValidationError):
                    ENGINE.validate(receipt)
        _, receipt, _ = fixture()
        del receipt["runtime"]["base_image"]
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)

    def test_adv08_operator_reason_is_split(self):
        _, receipt, _ = fixture()
        receipt.update(exit_code=7, failure_reason="operator", output=None, gate=None, observations=None,
                       sources_observed=[])
        receipt["cleanup"] = {"status": "not_applicable", "remaining_pids": None,
                              "remaining_workspace_bytes": None, "resources_released": None}
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)
        receipt["exit_code"] = 5
        receipt["failure_reason"] = "operator_recipe"
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)


class AmendA4BaseImage(unittest.TestCase):
    RECIPE = "FROM debian@sha256:" + "1" * 64 + "\nRUN apt-get install -y golang\nCOPY . /src\n"

    def test_reproducible_digest_and_change_sensitivity(self):
        context = {"src/work.go": SOURCE_SHA}
        first = image_digest_model("9" * 64, context)
        self.assertEqual(first, image_digest_model("9" * 64, dict(context)))
        self.assertNotEqual(first, image_digest_model("9" * 64, {"src/work.go": CHANGE_SHA}))
        self.assertNotEqual(first, image_digest_model("8" * 64, context))

    def test_must_fail_secret_leaking_recipe_is_refused(self):
        approval = {"mode": "per_build", "approval_file_sha256": "6" * 64, "recipe_sha256": "9" * 64}
        self.assertEqual(recipe_admission(self.RECIPE, "9" * 64, approval), 0)
        for leak in ("COPY ~/.config/gh/hosts.yml /root/\n", "ADD /home/user/.config/x /x\n",
                     "RUN cat $HOME/.ssh/id_ed25519\n", "COPY ../outside /x\n", "COPY auth.json /x\n"):
            with self.subTest(leak=leak):
                self.assertEqual(recipe_admission(self.RECIPE + leak, "9" * 64, approval), 5)

    def test_recipe_change_without_operator_approval_is_rc7(self):
        approval = {"mode": "per_build", "approval_file_sha256": "6" * 64, "recipe_sha256": "9" * 64}
        self.assertEqual(recipe_admission(self.RECIPE + "RUN true\n", "5" * 64, approval), 7)
        self.assertEqual(recipe_admission(self.RECIPE, "9" * 64, None), 7)

    def test_base_image_record_and_keep_current_plus_rollback(self):
        record = {"schema": "os.cu.base-image/v1", "name": "os-worker-base", "arch": "aarch64",
                  "recipe_sha256": "9" * 64, "image_digest": "e" * 64, "build_context": "recipe-dir",
                  "fetch_pins_verified": True, "user_uid": 10001, "entrypoint": ["/usr/bin/tini", "-s", "--"],
                  "secret_paths_in_context": 0, "frame_bytes_in_image": 0, "repo_code_in_image": 0,
                  "approval": {"mode": "per_build", "approval_file_sha256": "6" * 64, "recipe_sha256": "9" * 64},
                  "retained_rank": 1}
        ENGINE.validate(record, "base_image_record")
        for key, value in (("arch", "x86_64"), ("secret_paths_in_context", 1), ("frame_bytes_in_image", 1),
                           ("build_context", "host-worktree"), ("repo_code_in_image", 1), ("user_uid", 0),
                           ("fetch_pins_verified", False), ("retained_rank", 3),
                           ("approval", {"mode": "standing", "approval_file_sha256": "6" * 64,
                                         "recipe_sha256": "9" * 64}),
                           ("name", "project@" + "a" * 40), ("image_tag", "project@" + "a" * 40)):
            with self.subTest(key=key):
                with self.assertRaises(ValidationError):
                    ENGINE.validate({**record, key: value}, "base_image_record")
        self.assertNotIn("env_image", SCHEMA["$defs"])
        self.assertNotIn("image_tag", SCHEMA["$defs"])
        self.assertEqual(retain_last(["t1", "t2", "t3"], 2), ["t2", "t3"])


class AmendA2Orchestrator(unittest.TestCase):
    def test_admission_by_budget_and_free_ram_floor(self):
        gib = 1073741824
        self.assertTrue(admit(20 * gib, 8 * gib, [4 * gib] * 3, 4 * gib, 16 * gib))
        self.assertFalse(admit(20 * gib, 8 * gib, [4 * gib] * 4, 4 * gib, 16 * gib))
        self.assertFalse(admit(11 * gib, 8 * gib, [], 4 * gib, 16 * gib))

    def test_supervision_restarts_then_gives_up(self):
        self.assertEqual(supervise(["FAILED", "SUCCEEDED"], 2), ("SUCCEEDED", 1))
        self.assertEqual(supervise(["FAILED", "INDETERMINATE", "FAILED", "SUCCEEDED"], 2), ("GAVE_UP", 2))

    def test_speech_acts_are_structured(self):
        ENGINE.validate(speech("finding", "l1", 0, repro="3" * 64), "speech_act")
        ENGINE.validate(speech("status", "l1", 1, status="working"), "speech_act")
        for bad in (speech("finding", "l1", 0), speech("status", "l1", 0), speech("chat", "l1", 0),
                    speech("affected", "l1", 0), {**speech("status", "l1", 0, status="done"), "free_text": "hi"}):
            with self.subTest(bad=bad["act"]):
                with self.assertRaises(ValidationError):
                    ENGINE.validate(bad, "speech_act")

    def test_findings_deduplicated_by_repro_hash(self):
        acts = [speech("finding", "l1", 0, repro="3" * 64), speech("finding", "l2", 0, repro="3" * 64),
                speech("repro", "l3", 0, repro="2" * 64)]
        self.assertEqual(len(aggregate(acts)), 2)
        self.assertEqual(vigia(acts)[2], {"l2"})

    def test_must_fail_flooding_worker_is_throttled_and_stuck_flagged(self):
        acts = [speech("status", "flood", index, status="working") for index in range(9)]
        acts += [speech("status", "calm", 0, status="done")]
        throttled, stuck, _ = vigia(acts)
        self.assertEqual(throttled, {"flood"})
        self.assertIn("flood", stuck)
        self.assertNotIn("calm", throttled | stuck)


class AmendA3Nesting(unittest.TestCase):
    def test_microvm_nested_in_docker_with_kvm_only(self):
        task, receipt, grant = fixture("microvm")
        validate_bundle(task, receipt, grant, NOW)
        for key, value in (("devices", ["/dev/kvm", "/dev/vhost-vsock"]), ("devices", []),
                           ("outer_backend", "host"), ("guest_arch", "x86_64"), ("control", "tcp"),
                           ("vms_in_container", 0)):
            with self.subTest(key=key, value=value):
                _, receipt, _ = fixture("microvm")
                receipt["runtime"]["nesting"][key] = value
                with self.assertRaises(ValidationError):
                    ENGINE.validate(receipt)
        _, receipt, _ = fixture("microvm")
        del receipt["runtime"]["nesting"]
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)

    def test_shared_lease_contract_with_wender_targets(self):
        done = {"schema": "os.cu.worker-receipt/v1", "sha256": "3" * 64}
        for kind in ("capsule", "microvm", "browser_window"):
            ENGINE.validate(lease("RUNNING", kind=kind), "scheduler_lease")
            ENGINE.validate(lease("SUCCEEDED", done, kind=kind), "scheduler_lease")
        ENGINE.validate(lease("INDETERMINATE"), "scheduler_lease")
        for bad in (lease("SUCCEEDED"), lease("RUNNING", done), lease("PAUSED"), lease("RUNNING", kind="host")):
            with self.subTest(state=bad["state"]):
                with self.assertRaises(ValidationError):
                    ENGINE.validate(bad, "scheduler_lease")

    def test_guest_network_escape_is_reported_as_containment(self):
        _, receipt, _ = fixture("microvm")
        receipt.update(exit_code=5, failure_reason="gate", output=None)
        receipt["gate"].update(raw_exit_code=1, exit_code=5)
        receipt["observations"]["public_listeners"] = 1
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)
        receipt["failure_reason"] = "containment"
        ENGINE.validate(receipt)


LAUNCH_SPEC = {"schema": "os.cu.launch-spec/v1", "image_digest": "e" * 64, "user": "10001:10001", "init": True,
               "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
               "privileged": False, "network": "none",
               "tmpfs": [{"target": "/workspace", "options": "rw,exec,nosuid,nodev,size=134217728,mode=0770"},
                         {"target": "/tmp", "options": "rw,noexec,nosuid,nodev,size=67108864"}],
               "volumes": [{"source": "os_worker_cache", "target": "/opt/cache", "read_only": True}],
               "devices": [], "memory_bytes": 268435456, "memswap_bytes": 268435456, "pids_limit": 64,
               "nofile": 8192, "oom_score_adj": 800, "cpuset": [10, 11],
               "env_names": ["LC_ALL", "TINI_SUBREAPER"], "log_driver": "none", "restart": "no", "ulimit_core": 0}


def launch_admission(spec, backend, interactive_cpus):
    """Seat-side refusal of a launch spec: rc 0 launch, rc 5 containment (must-FAIL)."""
    try:
        ENGINE.validate(spec, "launch_spec")
        require(spec["memswap_bytes"] == spec["memory_bytes"], "swap allowed (memswap != mem)")
        require(not set(spec["cpuset"]) & set(interactive_cpus), "cpuset on interactive cores")
        require(spec["devices"] == (["/dev/kvm"] if backend == "microvm" else []), "device outside microVM phase")
        require(any(item["target"] == "/workspace" for item in spec["tmpfs"]), "workspace is not tmpfs")
    except ValidationError:
        return 5
    return 0


TEXT_SUFFIXES = (".md", ".json", ".txt", ".patch", ".diff")


def extract_admission(entries, owner_ok, residue):
    """Host side of tar-over-exec: text results only, non-empty, 0644, owner asserted, no staging residue."""
    require(entries, "empty artifact (guide rc 30)")
    for name, mode, payload in entries:
        require(name.endswith(TEXT_SUFFIXES) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name) is not None,
                "non-text or nested entry")
        require(mode == 0o644, "mode not forced to 0644")
        require(b"\x00" not in payload, "binary payload")
        payload.decode("utf-8")
    require(owner_ok and residue == 0, "ownership or staging residue")
    return {"method": "tar-exec", "entries": len(entries), "bytes": sum(len(item[2]) for item in entries),
            "text_only": True, "mode": "0644", "owner_asserted": True, "staging_residue": 0,
            "sha256": hashlib.sha256(b"".join(item[2] for item in entries)).hexdigest()}


class AmendA4Security(unittest.TestCase):
    INTERACTIVE = list(range(10))

    def test_launch_spec_baseline_is_admitted(self):
        self.assertEqual(launch_admission(deepcopy(LAUNCH_SPEC), "docker", self.INTERACTIVE), 0)
        micro = {**deepcopy(LAUNCH_SPEC), "devices": ["/dev/kvm"]}
        self.assertEqual(launch_admission(micro, "microvm", self.INTERACTIVE), 0)

    def test_must_fail_bind_mount_env_file_and_docker_sock_are_refused(self):
        mutants = {
            "bind_mount": {"binds": ["/home/user:/host"]},
            "bind_as_volume": {"volumes": [{"source": "/home/user/.config", "target": "/opt/cache",
                                            "read_only": True}]},
            "env_file": {"env_file": "/home/user/.env"},
            "docker_sock_volume": {"volumes": [{"source": "/var/run/docker.sock", "target": "/var/run/docker.sock",
                                                "read_only": True}]},
            "docker_sock_mounts": {"mounts": [{"type": "bind", "source": "/var/run/docker.sock"}]},
            "secret_env": {"env_names": ["ANTHROPIC_API_KEY"]},
            "privileged": {"privileged": True}, "host_network": {"network": "host"},
            "writable_root": {"read_only": False}, "caps": {"cap_drop": []},
            "swap": {"memswap_bytes": 536870912}, "oom_low": {"oom_score_adj": 0},
            "interactive_core": {"cpuset": [3, 10]}, "kvm_in_docker_phase": {"devices": ["/dev/kvm"]},
            "tmpfs_suid": {"tmpfs": [{"target": "/workspace", "options": "rw,exec,suid,dev"}]},
            "no_workspace_tmpfs": {"tmpfs": [{"target": "/tmp", "options": "rw,noexec,nosuid,nodev"}]},
            "cache_writable": {"volumes": [{"source": "os_worker_cache", "target": "/opt/cache", "read_only": False}]},
        }
        for name, change in mutants.items():
            with self.subTest(mutant=name):
                self.assertEqual(launch_admission({**deepcopy(LAUNCH_SPEC), **change}, "docker", self.INTERACTIVE), 5)
        self.assertEqual(launch_admission(deepcopy(LAUNCH_SPEC), "microvm", self.INTERACTIVE), 5)

    def test_inference_never_inside_capsule_and_exec_allowlist_is_narrow(self):
        for verb in ("ingest", "gate", "status", "extract", "purge"):
            ENGINE.validate({"schema": "os.cu.capsule-exec/v1", "lease_id": "lease-example", "verb": verb,
                             "user": "10001:10001", "tini": True}, "capsule_exec")
        for bad in ({"verb": "shell"}, {"verb": "infer"}, {"verb": "cp"}, {"user": "0:0"}, {"env_file": "x"},
                    {"argv": ["sh", "-c", "curl"]}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    ENGINE.validate({"schema": "os.cu.capsule-exec/v1", "lease_id": "lease-example",
                                     "verb": "gate", "user": "10001:10001", "tini": True, **bad}, "capsule_exec")
        _, receipt, _ = fixture()
        receipt["observations"]["probes"]["inference_processes"] = 1
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)

    def test_grant_hardening_constants_and_kvm_only_in_microvm_phase(self):
        for path, value in ((("bind_mounts",), ["/home/user"]), (("env_file",), "/home/user/.env"),
                            (("runtime_socket_mounted",), True), (("read_only",), False),
                            (("memswap_equals_memory",), False), (("oom_score_adj",), 0), (("init",), "none"),
                            (("daemon",), "root"), (("devices",), ["/dev/kvm"]), (("env_names",), ["GH_TOKEN"]),
                            (("egress", "mode"), "open")):
            with self.subTest(path=path):
                _, _, grant = fixture()
                set_at(grant["isolation"]["hardening"], path, value)
                with self.assertRaises(ValidationError):
                    ENGINE.validate(grant, "seat_grant")
        _, _, grant = fixture("microvm")
        grant["isolation"]["hardening"]["devices"] = []
        with self.assertRaises(ValidationError):
            ENGINE.validate(grant, "seat_grant")

    def test_grant_bindings_commit_cpuset_egress_and_outer_image(self):
        cases = ((("runtime", "commit"), "b" * 40), (("limits", "cpuset"), [9, 10]),
                 (("isolation", "hardening", "egress"), {"mode": "host_rules", "bridge": "br-osw",
                                                         "rules_sha256": "1" * 64, "verify_sha256": "1" * 64}),
                 (("runtime", "workspace", "size_bytes"), 268435456))
        for path, value in cases:
            with self.subTest(path=path):
                task, receipt, grant = fixture()
                set_at(grant, path, value)
                receipt["runtime"] = deepcopy(grant["runtime"])
                receipt["observations"]["cpuset"] = grant["limits"]["cpuset"]
                rebind(task, receipt, grant)
                with self.assertRaises(ValidationError):
                    validate_bundle(task, receipt, grant, NOW)
        task, receipt, grant = fixture("microvm")
        grant["runtime"]["nesting"]["outer_image_sha256"] = "8" * 64
        receipt["runtime"] = deepcopy(grant["runtime"])
        rebind(task, receipt, grant)
        with self.assertRaises(ValidationError):
            validate_bundle(task, receipt, grant, NOW)

    def test_measured_probe_failures_must_be_containment(self):
        for key, value in (("etc_write", "writable"), ("home_admin", "present"), ("egress_probe", "OPEN"),
                           ("bind_mounts", 1), ("env_file_used", True), ("env_secret_names", 1),
                           ("runtime_socket_visible", True), ("daemon", "root"), ("memswap_equals_memory", False),
                           ("oom_score_adj", 0)):
            with self.subTest(key=key):
                _, receipt, _ = fixture()
                receipt["observations"]["probes"][key] = value
                with self.assertRaises(ValidationError):
                    ENGINE.validate(receipt)
                receipt.update(exit_code=5, failure_reason="gate", output=None, extract=None)
                receipt["gate"].update(raw_exit_code=1, exit_code=5)
                with self.assertRaises(ValidationError):
                    ENGINE.validate(receipt)
                receipt["failure_reason"] = "containment"
                ENGINE.validate(receipt)

    def test_zero_orphans_after_task_unless_indeterminate(self):
        _, receipt, _ = fixture()
        receipt["observations"]["probes"]["orphans_after_task"] = 1
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)
        receipt.update(exit_code=5, failure_reason="containment", output=None, extract=None)
        receipt["gate"].update(raw_exit_code=1, exit_code=5)
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)

    def test_hardening_observation_must_match_grant(self):
        for key, value in (("devices", ["/dev/kvm"]), ("nofile", 1024), ("oom_score_adj", 900),
                           ("daemon", "dedicated_user")):
            with self.subTest(key=key):
                task, receipt, grant = fixture()
                receipt["observations"]["probes"][key] = value
                ENGINE.validate(receipt)
                with self.assertRaises(ValidationError):
                    validate_bundle(task, receipt, grant, NOW)

    def test_extract_is_tar_exec_text_only(self):
        record = extract_admission([("report.md", 0o644, b"# report\n"), ("receipt.json", 0o644, b"{}")], True, 0)
        ENGINE.validate(record, "extract_record")
        for entries, owner_ok, residue in (([], True, 0), ([("frame.png", 0o644, b"\x89PNG")], True, 0),
                                           ([("report.md", 0o755, b"x")], True, 0),
                                           ([("report.md", 0o644, b"a\x00b")], True, 0),
                                           ([("../x.md", 0o644, b"x")], True, 0),
                                           ([("report.md", 0o644, b"x")], False, 0),
                                           ([("report.md", 0o644, b"x")], True, 1)):
            with self.subTest(entries=entries, owner_ok=owner_ok, residue=residue):
                with self.assertRaises((ValidationError, UnicodeDecodeError)):
                    extract_admission(entries, owner_ok, residue)
        _, receipt, _ = fixture()
        receipt["extract"] = None
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)
        for key, value in (("method", "docker-cp"), ("text_only", False), ("staging_residue", 1)):
            with self.subTest(key=key):
                _, receipt, _ = fixture()
                receipt["extract"][key] = value
                with self.assertRaises(ValidationError):
                    ENGINE.validate(receipt)
        _, receipt, _ = fixture("web", "web")
        receipt["extract"] = deepcopy(EXAMPLE_EXTRACT)
        with self.assertRaises(ValidationError):
            ENGINE.validate(receipt)

    def test_workspace_is_capsule_tmpfs_never_host_path(self):
        for value in ("/dev/shm/worker-lease-example/repo", "/home/user/repo", "/workspace", "/workspace/../etc"):
            with self.subTest(value=value):
                _, _, grant = fixture()
                grant["isolation"]["workspace_root"] = value
                with self.assertRaises(ValidationError):
                    ENGINE.validate(grant, "seat_grant")


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if arguments not in ([], ["--selftest-fail"], ["--no-skips"]):
        print("usage: test_worker_task_schema.py [--selftest-fail | --no-skips]", file=sys.stderr)
        return 2
    try:
        if arguments == ["--selftest-fail"]:
            def intentional_failure():
                raise AssertionError("intentional runner failure fixture; expected process rc 5")
            suite = unittest.TestSuite((unittest.FunctionTestCase(intentional_failure),))
        else:
            suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if arguments == ["--no-skips"] and result.skipped:
            print("E_GATE worker_schema skipped=" + str(len(result.skipped)), file=sys.stderr)
            return 5
        return 0 if result.wasSuccessful() else 5
    except KeyboardInterrupt:
        return 6
    except Exception as error:
        print("E_GATE worker_schema " + repr(error), file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())

# STATUS: DONE
