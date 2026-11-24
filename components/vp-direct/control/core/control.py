"""Shared normalization, capability policy and dispatch for ViewPoint control lanes."""

from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from fcntl import LOCK_EX, flock
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from threading import Lock
import time
from typing import Callable
from weakref import WeakValueDictionary

from jsonschema import ValidationError


_schemas = Path(__file__).resolve().parents[2] / "schemas"
_spec = importlib.util.spec_from_file_location("vp_control_schema_validation", _schemas / "validation.py")
_validation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_validation)

REQUEST = "vp.control.req/v1"
REPLY = "vp.control.reply/v1"
CAPABILITY = "vp.capability/v1"
canonical_bytes = _validation.canonical_bytes
validate_contract = _validation.validate_contract


def _resource(identifier):
    return _validation._store().get("urn:" + identifier.replace("/", ":"))


_request_schema = _resource(REQUEST)
_context_fields = set(_request_schema["properties"]["context"]["properties"])
_tools = frozenset(_request_schema["properties"]["op"]["enum"])
_risk_levels = _resource(CAPABILITY)["properties"]["risk_class"]["enum"]
_operator_tools = {"vp_forge_admit", "vp_identity_delete"}
_args_by_tool = {
    rule["if"]["properties"]["op"]["const"]: rule["then"]["properties"]["args"]
    for rule in _request_schema["allOf"]
}


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class InvalidRequest(ValueError):
    """A lane error for input that cannot form a frozen control reply."""

    def __init__(self, code):
        super().__init__(str(code))
        self.code = code


@dataclass(frozen=True)
class LaneIdentity:
    """Caller and instance from lane provisioning, supplied outside client input."""

    caller: str
    agent_instance: str


def _normalize(envelope, identity):
    if not isinstance(envelope, dict) or not isinstance(envelope.get("context"), dict):
        raise InvalidRequest(-32602)
    request = deepcopy(envelope)
    request["context"].update(caller=identity.caller, agent_instance=identity.agent_instance)
    tool = request.get("op")
    if isinstance(tool, str) and tool not in _tools:
        raise InvalidRequest(-32601)
    try:
        validate_contract(REQUEST, request)
        args, context = request["args"], request["context"]
        if any(args[name] != context[name]
               for name in {"session_id", "capsule_id"} & args.keys() & context.keys()):
            raise ValueError("Payload and context identify different targets")
        encoded = canonical_bytes(request)
    except ValidationError as exc:
        args = request.get("args")
        selector = args.get("op") if isinstance(args, dict) else None
        if (tuple(exc.absolute_path) == ("args", "op") and isinstance(tool, str)
                and "$ref" in _args_by_tool[tool] and isinstance(selector, str) and selector
                and _resource(f"{tool}.{selector}.args/v1") is None):
            raise InvalidRequest("ARGS_SCHEMA_ABSENT") from exc
        raise InvalidRequest(-32602) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidRequest(-32602) from exc
    return json.loads(encoded)


def _flat_args_fields(tool, selector):
    shape = _args_by_tool[tool]
    if "$ref" in shape:
        # The union itself has no properties. Use the frozen operation resource.
        shape = _resource(f"{tool}.{selector}.args/v1") or {}
    return shape.get("properties", {}).keys()


def _from_mcp(tool, params, identity, binding_context):
    if not isinstance(tool, str) or tool not in _tools:
        raise InvalidRequest(-32601)
    if not isinstance(params, dict) or {"caller", "agent_instance", "request", "context"} & params.keys():
        raise InvalidRequest(-32602)
    fields = _flat_args_fields(tool, params.get("op"))
    context = dict(binding_context)
    args = {}
    for key, value in params.items():
        if key in {"request_id", "dry_run"}:
            continue
        if key in _context_fields and key not in fields:
            if key in context and context[key] != value:
                raise InvalidRequest(-32602)
            context[key] = value
        else:
            args[key] = value
    envelope = {"schema": REQUEST, "op": tool, "args": args, "context": context}
    envelope.update({key: params[key] for key in ("request_id", "dry_run") if key in params})
    return _normalize(envelope, identity)


def _operation_row(op, row):
    return {"op": op, **{key: row[key] for key in (
        "supported", "operator_only", "risk_class", "args_schema_sha256",
        "reply_schema_sha256", "binding",
    )}}


def _has_schema_pair(tool, selector, row):
    if row is None:
        return False
    for field, suffix in (("args_schema", "args"), ("reply_schema", "result")):
        identifier = f"{tool}.{selector}.{suffix}/v1"
        if row.get(field) != identifier:
            return False
        schema = _resource(identifier)
        if schema is None or digest(schema) != row.get(field + "_sha256"):
            return False
    return True


class CapabilityRegistry:
    """Immutable snapshot built from supplied capabilities and the frozen adapter catalog."""

    def __init__(self, entries=()):
        self._adapter_ops = deepcopy(_validation._adapter_rows())
        self._entries = {}
        for entry in entries:
            entry = deepcopy(entry)
            validate_contract(CAPABILITY, entry)
            tool = entry["tool"]
            if tool in self._entries:
                raise ValueError(f"Duplicate capability {tool}")
            if tool in _operator_tools and not entry["operator_only"]:
                raise ValueError(f"{tool} is operator-only")
            if tool == "vp_factory_call" and entry["supported"] and entry["origin"] != "factory":
                raise ValueError("Factory support requires a factory capability")
            policy = self._adapter_ops.get(tool)
            if policy is not None and not entry.get("op_table"):
                raise ValueError(f"{tool} requires an op_table")
            for row in entry.get("op_table", ()):
                self._check_row(tool, row)
            self._entries[tool] = entry
        catalog_path = _schemas / "adapter_ops.json"
        catalog_bytes = catalog_path.read_bytes()
        source = {
            "path": "components/vp-direct/schemas/adapter_ops.json",
            "line_start": 1,
            "line_end": len(catalog_bytes.splitlines()),
            "sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        }
        for tool, policy in self._adapter_ops.items():
            if tool in self._entries:
                continue
            rows = [_operation_row(op, row) for op, row in policy["ops"].items()]
            for row in rows:
                if tool == "vp_factory_call":
                    row["supported"] = False
                self._check_row(tool, row)
            cli = tool.removeprefix("vp_").removesuffix("_call").replace("_", "-")
            entry = {
                "schema": CAPABILITY, "tool": tool, "json_op": tool, "cli": cli,
                "origin": "adapter", "args_schema_sha256": digest(_resource(f"{tool}.args/v1")),
                "ops": [row["op"] for row in rows], "op_table": rows,
                "supported": any(row["supported"] for row in rows),
                "operator_only": all(row["operator_only"] for row in rows),
                "risk_class": max((row["risk_class"] for row in rows), key=_risk_levels.index),
                "route": {"verbs": [], "objects": [], "argv_template": f"mvctl {cli}"},
                "source": [source],
            }
            validate_contract(CAPABILITY, entry)
            self._entries[tool] = entry

    def _check_row(self, tool, row):
        frozen = self._policy(tool, row["op"])
        if frozen is None:
            if row["supported"]:
                raise ValueError("ARGS_SCHEMA_ABSENT")
            return
        if (frozen["operator_only"] and not row["operator_only"]
                or _risk_levels.index(row["risk_class"]) < _risk_levels.index(frozen["risk_class"])):
            raise ValueError(f"Capability weakens frozen policy for {tool}.{row['op']}")
        for key in ("args_schema_sha256", "reply_schema_sha256", "binding"):
            if row[key] != frozen[key]:
                raise ValueError(f"Capability disagrees with frozen {key} for {tool}.{row['op']}")
        if row["supported"] and (not frozen["supported"] or frozen.get("refusal_only")):
            raise ValueError(f"Operation is parked: {tool}.{row['op']}")

    def _policy(self, tool, selector):
        return self._adapter_ops.get(tool, {}).get("ops", {}).get(selector)

    def get(self, tool):
        return deepcopy(self._entries.get(tool))

    def entries(self):
        return [self.get(tool) for tool in sorted(self._entries)]


_request_store_writers = WeakValueDictionary()
_request_store_writers_lock = Lock()


def _reset_request_store_writers():
    global _request_store_writers, _request_store_writers_lock
    _request_store_writers = WeakValueDictionary()
    _request_store_writers_lock = Lock()


os.register_at_fork(after_in_child=_reset_request_store_writers)


class RequestStore:
    """Persist request lineage across process restarts for all lanes sharing a path."""

    _BUSY_TIMEOUT_SECONDS = 5.0

    def __init__(self, path):
        self.path = Path(path).resolve()
        self._trace_path = os.environ.get("VP_REQUEST_STORE_TRACE")
        with self._transaction("initialize") as (db, mark):
            mark("journal_enter")
            self._enable_wal(db)
            mark("journal_return")
            mark("schema_enter")
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'requests'"
            ).fetchone()
            mark("schema_return")
            if not exists:
                mark("create_enter")
                db.execute("CREATE TABLE IF NOT EXISTS requests ("
                           "id TEXT PRIMARY KEY, digest TEXT NOT NULL, reply BLOB)")
                mark("create_return")
                # Checkpoint the new schema when the provisioning connection closes.
                db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, False)

    def _enable_wal(self, db):
        if db.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
            return
        # Journal conversion can bypass SQLite's busy handler. Serialize provisioners
        # on the directory during conversion without opening a second fd to the DB.
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            flock(directory_fd, LOCK_EX)
            mode, = db.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode != "wal":
                raise RuntimeError("Request store could not enable WAL")
        finally:
            os.close(directory_fd)

    @contextmanager
    def _transaction(self, operation, request_id=None):
        trace_path = self._trace_path
        stages = {}

        def mark(stage):
            if trace_path:
                stages[stage] = {"monotonic_ns": time.monotonic_ns(),
                                 "thread_cpu_ns": time.thread_time_ns()}

        ok = False
        try:
            with _request_store_writers_lock:
                writer = _request_store_writers.setdefault(self.path, Lock())
            mark("writer_enter")
            with writer:
                mark("writer_return")
                if operation == "initialize":
                    try:
                        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    except FileExistsError:
                        pass
                    else:
                        os.close(fd)
                mark("connect_enter")
                with closing(sqlite3.connect(self.path, timeout=self._BUSY_TIMEOUT_SECONDS)) as db:
                    mark("connect_return")
                    mark("synchronous_enter")
                    # NORMAL keeps committed WAL records across a process crash.
                    # A power loss or OS crash can roll back recent claims and replies.
                    db.execute("PRAGMA synchronous=NORMAL")
                    # Short-lived connections share the WAL between calls. Keep
                    # SQLite's automatic checkpoints and skip the last-close one.
                    db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
                    mark("synchronous_return")
                    with db:
                        if operation != "initialize":
                            mark("begin_enter")
                            db.execute("BEGIN IMMEDIATE")
                            mark("begin_return")
                        yield db, mark
                        mark("commit_enter")
                    mark("commit_return")
                mark("connection_exit")
            ok = True
        finally:
            if trace_path:
                if not ok:
                    mark("connection_exit")
                # Diagnostic I/O must leave the transaction outcome intact.
                try:
                    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                    row = {"boot_id": boot_id, "pid": os.getpid(), "request_id": request_id,
                           "operation": operation, "ok": ok, "stages": stages}
                    fd = os.open(trace_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    try:
                        os.write(fd, (json.dumps(row) + "\n").encode())
                    finally:
                        os.close(fd)
                except OSError:
                    pass

    def claim(self, request_id, request_digest):
        """Return None for a new claim, a terminal reply, or a refusal code."""
        with self._transaction("claim", request_id) as (db, mark):
            mark("insert_enter")
            inserted = db.execute(
                "INSERT INTO requests (id, digest) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
                (request_id, request_digest),
            )
            mark("insert_return")
            if inserted.rowcount:
                return None
            mark("select_enter")
            saved_digest, reply = db.execute(
                "SELECT digest, reply FROM requests WHERE id = ?", (request_id,),
            ).fetchone()
            mark("select_return")
            if saved_digest != request_digest:
                return "REQUEST_ID_CONFLICT"
            return "UNCERTAIN_OUTCOME" if reply is None else json.loads(reply)

    def complete(self, request_id, request_digest, reply):
        payload = canonical_bytes(reply)
        with self._transaction("complete", request_id) as (db, mark):
            mark("update_enter")
            updated = db.execute(
                "UPDATE requests SET reply = ? WHERE id = ? AND digest = ? AND reply IS NULL",
                (payload, request_id, request_digest),
            )
            mark("update_return")
            if updated.rowcount != 1:
                raise RuntimeError("Request claim could not be completed")


@dataclass(frozen=True)
class Outcome:
    ok: bool
    code: str
    result: dict = field(default_factory=dict)
    evidence: tuple = ()
    not_verified: tuple = ()


@dataclass(frozen=True)
class Handler:
    execute: Callable[[dict], Outcome]
    preview: Callable[[dict], Outcome] | None = None


def _reply(request, request_digest, outcome):
    response = {
        "schema": REPLY, "request_id": request["request_id"], "op": request["op"],
        "ok": outcome.ok, "code": outcome.code, "result": outcome.result if outcome.ok else {},
        "effective_request_sha256": request_digest, "evidence": list(outcome.evidence),
        "not_verified": list(outcome.not_verified),
    }
    validate_contract(REPLY, response)
    return json.loads(canonical_bytes(response))


class Dispatcher:
    """Policy callbacks receive (request, risk_class), and must explicitly allow with True."""

    def __init__(self, registry, requests, *, pace_risk, lease_epoch, tools=(), bindings=()):
        self.registry = registry
        self.requests = requests
        self._gates = (pace_risk, lease_epoch)
        self._tools = dict(tools)
        self._bindings = dict(bindings)

    def dispatch_mcp(self, tool, params, identity, *, binding_context=None):
        return self._dispatch(_from_mcp(tool, params, identity, binding_context or {}))

    def dispatch_cli(self, verb, envelope, identity):
        request = _normalize(envelope, identity)
        entry = self.registry.get(request["op"])
        if entry is None or entry["cli"] != verb:
            raise InvalidRequest(-32601)
        return self._dispatch(request)

    def dispatch_mailbox(self, envelope, identity):
        return self._dispatch(_normalize(envelope, identity))

    def _dispatch(self, request):
        tool = request["op"]
        entry = self.registry.get(tool)
        if entry is None:
            raise InvalidRequest(-32601)
        request_id, request_digest = request["request_id"], digest(request)
        policy, frozen = entry, None
        handler = self._tools.get(tool)
        if "op_table" in entry or tool in self.registry._adapter_ops:
            selector = request["args"].get("op")
            frozen = self.registry._policy(tool, selector)
            if not _has_schema_pair(tool, selector, frozen):
                return _reply(request, request_digest, Outcome(False, "ARGS_SCHEMA_ABSENT"))
            policy = next((row for row in entry.get("op_table", ()) if row["op"] == selector), None)
            if policy is None:
                return _reply(request, request_digest, Outcome(False, "HOLD"))
            binding = policy["binding"]
            handler = self._bindings.get((binding["kind"], binding["name"])) if binding["kind"] != "none" else None
        if policy["operator_only"] and request["context"]["caller"] != "operator":
            code = "FORGE_APPROVAL_REQUIRED" if tool == "vp_forge_admit" else "OWNER"
            return _reply(request, request_digest, Outcome(False, code))
        prior = self.requests.claim(request_id, request_digest)
        if isinstance(prior, dict):
            if prior["ok"]:
                if not policy["supported"] or handler is None:
                    return _reply(request, request_digest, Outcome(False, "HOLD"))
                code = self._refusal(self._gates[1], request, policy)
                if code is not None:
                    return _reply(request, request_digest, Outcome(False, code))
            return prior
        if prior is not None:
            return _reply(request, request_digest, Outcome(False, prior))
        response = self._execute(request, request_digest, policy, frozen, handler)
        self.requests.complete(request_id, request_digest, response)
        return response

    def _execute(self, request, request_digest, policy, frozen, handler):
        if not policy["supported"] or handler is None:
            return _reply(request, request_digest, Outcome(False, "HOLD"))
        run = handler.preview if request["dry_run"] else handler.execute
        if run is None:
            return _reply(request, request_digest, Outcome(False, "HOLD"))
        for gate in self._gates:
            code = self._refusal(gate, request, policy)
            if code is not None:
                return _reply(request, request_digest, Outcome(False, code))
        try:
            outcome = run(deepcopy(request))
            if frozen is not None and outcome.ok:
                validate_contract(frozen["reply_schema"], outcome.result)
            return _reply(request, request_digest, outcome)
        except Exception:
            # Once the backend was entered, a missing or invalid result cannot authorize a resend.
            return _reply(request, request_digest, Outcome(False, "UNCERTAIN_OUTCOME"))

    @staticmethod
    def _refusal(gate, request, policy):
        decision = gate(deepcopy(request), policy["risk_class"])
        if decision is True:
            return None
        return decision if isinstance(decision, str) and decision else "NO_LEASE"
