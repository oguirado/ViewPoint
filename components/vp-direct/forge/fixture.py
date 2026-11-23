"""Pinned fixture dispatch and independently recorded attempts for C6."""

import hashlib
import inspect
import json
import os
from pathlib import Path
import re

from jsonschema import ValidationError

from control.core.control import Dispatcher, digest
from schemas.validation import canonical_bytes, validate_contract


HASH = re.compile(r"[0-9a-f]{64}")
F1_SITE = Path(__file__).resolve().parents[1] / "fixtures" / "site"


class ForgeError(ValueError):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def append_row(stream, row):
    stream.write(canonical_bytes(row))
    stream.flush()
    os.fsync(stream.fileno())


def pinned_request(step, parent_digest, index, context, allowed):
    """Materialize a literal child call from the candidate and fixture pins."""
    args = step["args"]
    call = {"op": step["op"], "args": args}
    if digest(call) not in allowed:
        raise ForgeError("BLOCKED_SOURCE", "step is outside the admitted F1 calls")
    for field in ("session_id", "capsule_id"):
        if field in args and args[field] != context.get(field):
            raise ForgeError("BLOCKED_SOURCE", "step does not target the fixture")
    if not any(context.get(field) for field in ("session_id", "capsule_id")):
        raise ForgeError("BLOCKED_SOURCE", "fixture target is absent")
    request = dict(call, schema="vp.control.req/v1", dry_run=False, context=dict(context),
                   request_id="c6-" + digest([parent_digest, index]))
    try:
        validate_contract("vp.control.req/v1", request)
    except ValidationError as error:
        raise ForgeError("FORGE_SCHEMA_INVALID", "literal child arguments do not match the frozen request") from error
    return request


class FixtureExecutor:
    """A provisioned C1 dispatcher with a fixed fixture identity and exact call allowlist."""

    def __init__(self, dispatcher, identity, *, context, allowed, source_root, site_pins, executor_pins):
        if not isinstance(dispatcher, Dispatcher) or identity.caller != "agent":
            raise ForgeError("BLOCKED_SOURCE", "a provisioned fixture agent and C1 dispatcher are required")
        self.dispatcher = dispatcher
        self.identity = identity
        self.context = dict(context, caller=identity.caller, agent_instance=identity.agent_instance)
        self.allowed = frozenset(digest(call) for call in allowed)
        self.root = Path(source_root).resolve()
        self.site_pins = dict(site_pins)
        self.executor_pins = dict(executor_pins)

    def _verify_pins(self, pins, *, site=False):
        if not pins:
            raise ForgeError("BLOCKED_SOURCE", "missing fixture or executor source pins")
        for name, sha in pins.items():
            try:
                path = self._source_path(name)
                if (site and not path.is_relative_to(F1_SITE)
                        or not isinstance(sha, str) or not HASH.fullmatch(sha) or file_sha256(path) != sha):
                    raise ValueError(name)
            except (OSError, ValueError) as error:
                raise ForgeError("BLOCKED_SOURCE", "fixture or executor source pin did not verify") from error

    def _source_path(self, name):
        path = (self.root / name).resolve(strict=True)
        if path.relative_to(self.root).as_posix() != name or not path.is_file():
            raise ValueError("source is outside its bound root")
        return path

    def verify_citations(self, citations):
        for citation in citations:
            try:
                data = self._source_path(citation["path"]).read_bytes()
                if (hashlib.sha256(data).hexdigest() != citation["sha256"]
                        or not 1 <= citation["line_start"] <= citation["line_end"] <= len(data.splitlines())):
                    raise ValueError("stale citation")
            except (OSError, ValueError):
                raise ForgeError("BLOCKED_SOURCE", "publication citation did not verify") from None

    def fingerprint(self, policies):
        """Bind the actual C1 handlers and gates to the provisioned source pins."""
        self._verify_pins(self.site_pins, site=True)
        self._verify_pins(self.executor_pins)
        callbacks = [type(self.dispatcher), *self.dispatcher._gates]
        for tool, policy in policies:
            binding = policy.get("binding")
            handler = (self.dispatcher._tools.get(tool) if binding is None else
                       self.dispatcher._bindings.get((binding["kind"], binding["name"])))
            if handler is None:
                raise ForgeError("BLOCKED_SOURCE", "no fixture handler for a child operation")
            callbacks.append(handler.execute)
        names = []
        for callback in callbacks:
            try:
                name = Path(inspect.getfile(callback)).resolve().relative_to(self.root).as_posix()
                if name not in self.executor_pins:
                    raise ValueError(name)
            except (TypeError, ValueError) as error:
                raise ForgeError("BLOCKED_SOURCE", "dispatch code has no verified source pin") from error
            names.append([name, callback.__qualname__])
        return {
            "site": dict(self.site_pins), "executor": dict(self.executor_pins), "callbacks": names,
            "context": dict(self.context), "allowed": sorted(self.allowed),
            "policies_sha256": digest(policies),
        }

    def request(self, step, parent_digest, index):
        return pinned_request(step, parent_digest, index, self.context, self.allowed)

    def dispatch(self, request, index, spool):
        """Record the dispatch boundary before entering C1. A torn attempt stays unavailable."""
        start = {"index": index, "request_id": request["request_id"], "request_sha256": digest(request)}
        append_row(spool, dict(start, phase="started"))
        try:
            reply = self.dispatcher.dispatch_mailbox(request, self.identity)
            validate_contract("vp.control.reply/v1", reply)
            if (reply["request_id"], reply["op"], reply["effective_request_sha256"]) != (
                    request["request_id"], request["op"], start["request_sha256"]):
                raise ForgeError("BLOCKED_SOURCE", "unbound control reply")
        except (ValidationError, KeyError, OSError, ValueError, TypeError):
            append_row(spool, dict(start, phase="unavailable"))
            return None
        phase = "verified" if reply["ok"] else "unavailable"
        append_row(spool, dict(start, phase=phase, reply_sha256=digest(reply), code=reply["code"]))
        return reply if reply["ok"] else None


def read_spool(path):
    try:
        with path.open("rb") as stream:
            rows = []
            for line in stream:
                row = json.loads(line)
                if canonical_bytes(row) != line:
                    raise ValueError("noncanonical or torn attempt")
                rows.append(row)
            return rows
    except (OSError, ValueError, TypeError) as error:
        raise ForgeError("BLOCKED_SOURCE", "unreadable attempt spool") from error


def verify_attempts(rows, requests, replies):
    """Compare the execution spool to independently reconstructed requests and observed replies."""
    if len(rows) != 2 * len(requests) or len(replies) != len(requests):
        raise ForgeError("BLOCKED_SOURCE", "attempt count differs from the ordered observations")
    for index, (request, reply) in enumerate(zip(requests, replies, strict=True)):
        start = {"index": index, "request_id": request["request_id"], "request_sha256": digest(request)}
        first, final = rows[2 * index:2 * index + 2]
        success = reply is not None and final == dict(
            start, phase="verified", reply_sha256=digest(reply), code=reply["code"],
        )
        if first != dict(start, phase="started") or not success:
            raise ForgeError("BLOCKED_SOURCE", "missing, unavailable or misbound attempt")
