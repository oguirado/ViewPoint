"""The retention wire routes and typed boundary to the durable store."""

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import ContextManager, Protocol
import re

from ..contracts.validation import ContractError, FROZEN_PINS, strict_json
from ..contracts import wire
from .access import Access, Denied, Scope
from .http import BadHTTP, CONTENT_TYPE, OCTETS


PREFIX = "/retention/v1"
MAX_UPLOAD = 1 << 30
MAX_JSON = 32_768
MAX_INSPECTION = 2 * wire.MAX_RECORD_BYTES + MAX_JSON


@dataclass(frozen=True)
class Route:
    operation: str
    verb: str
    patterns: tuple
    request: str | None
    response: str | None
    maximum: int = MAX_JSON


ROUTES = {
    ("POST", "ready"): Route("ready", "", ("ready",), "Probe", "Ready"),
    ("PUT", "admission"): Route("admission", "admit", ("attempts", ":Attempt", "admission"), "Admission", "Admission"),
    ("PUT", "start"): Route("start", "start", ("attempts", ":Attempt", "start"), OCTETS, "StartAck", wire.MAX_RECORD_BYTES),
    ("POST", "launch-claim"): Route("claim", "claim", ("attempts", ":Attempt", "launch-claim"), "StartAck", "StartAck"),
    ("PUT", "uploads"): Route("upload", "upload", ("attempts", ":Attempt", "uploads", ":PathId"), OCTETS, "ObjectMeta", MAX_UPLOAD),
    ("GET", "objects"): Route("object", "inspect", ("attempts", ":Attempt", "objects", ":ObjRef"), None, "page"),
    ("PUT", "finish"): Route("finish", "finish", ("attempts", ":Attempt", "finish"), "Finish", "FinishAck", wire.MAX_FINISH_BYTES),
    ("GET", "inspect"): Route("inspect", "inspect", ("attempts", ":Attempt", "inspect"), None, "Inspection"),
    ("GET", "receipts"): Route("receipt", "inspect", ("receipts", ":ReceiptRef"), None, "Inspection"),
    ("GET", "artifacts"): Route("artifact", "inspect", ("attempts", ":Attempt", "artifacts", ":ArtRef"), None, OCTETS),
    ("PUT", "holds"): Route("hold", "hold", ("attempts", ":Attempt", "holds", ":PathId"), None, None),
    ("DELETE", "holds"): Route("release", "hold", ("attempts", ":Attempt", "holds", ":PathId"), None, None),
    ("POST", "pages"): Route("recovery", "recover", ("recovery", "pages"), "RecoveryQuery", "RecoveryPage"),
    ("GET", "items"): Route("recovery_item", "recover", ("recovery", "items", ":ObjRef"), None, "page"),
}


@dataclass(frozen=True)
class Command:
    route: Route
    attempt: str | None
    reference: str | None
    query: str | None
    recovery_query: dict | None = None


def decode_route(method, target, validator):
    path, mark, query = target.partition("?")
    if not path.startswith(PREFIX + "/"):
        raise Denied
    parts = path.removeprefix(PREFIX + "/").split("/")
    label = parts[2] if parts[0] == "attempts" and len(parts) >= 3 else parts[0]
    if parts[0] == "recovery" and len(parts) >= 2:
        label = parts[1]
    route = ROUTES.get((method, label))
    if route is None or len(parts) != len(route.patterns):
        raise Denied
    attempt = reference = None
    for value, pattern in zip(parts, route.patterns):
        if not pattern.startswith(":"):
            if value != pattern:
                raise Denied
            continue
        try:
            validator.validate(pattern[1:], value)
        except ContractError:
            raise Denied from None
        if pattern == ":Attempt":
            attempt = value
        else:
            reference = value
    return Command(route, attempt, reference, query if mark else None)


def paging(query):
    if query is None:
        return wire.page_parameters()
    values = {"offset": [], "limit": []}
    for parameter in query.split("&"):
        key, equals, value = parameter.partition("=")
        if not equals or key not in values:
            raise BadHTTP("invalid paging parameter")
        values[key].append(value)
    try:
        return wire.page_parameters(**values)
    except ContractError:
        raise BadHTTP("invalid paging parameter") from None


class Conflict(Exception):
    pass


class Unavailable(Exception):
    pass


class Integrity(Exception):
    pass


class Unsatisfiable(Exception):
    pass


@dataclass(frozen=True)
class Page:
    reference: str
    object_bytes: int
    object_sha256: str
    offset: int
    data: bytes

    def headers(self, validator, expected_ref, requested):
        offset, limit = requested
        validator.validate("Size", self.object_bytes)
        validator.validate("Hash", self.object_sha256)
        if self.object_bytes == 0:
            expected_length = 0
            offset_valid = offset == 0
        else:
            expected_length = min(limit, self.object_bytes - offset)
            offset_valid = offset < self.object_bytes
        if (self.reference != expected_ref or self.offset != offset or not offset_valid
                or len(self.data) != expected_length):
            raise Integrity
        return tuple(zip(wire.PAGE_HEADERS, (
            self.reference, str(self.object_bytes), self.object_sha256, str(self.offset),
            str(len(self.data)), sha256(self.data).hexdigest(),
        )))


@dataclass(frozen=True)
class Outcome:
    status: int
    body: dict | bytes | Page | None = None


class Upload:
    """Verify the exact octets as the store writes to private staging."""

    def __init__(self, reader, length, digest):
        self.reader = reader
        self.length = length
        self.digest = digest
        self.verified = False
        self.started = False

    def receive(self, write):
        if self.started:
            raise RuntimeError("upload stream was already consumed")
        self.started = True
        digest = sha256()
        for raw in self.reader.chunks(self.length):
            digest.update(raw)
            write(raw)
        if digest.hexdigest() != self.digest:
            raise ContractError("upload digest mismatch")
        self.verified = True


class Transaction(Protocol):
    """Scope resolution uses authorized index metadata, without opening payloads."""

    def scope(self) -> Scope: ...

    def compare(self) -> tuple[Scope, Scope] | None:
        """Resolve the durable compare admission's ordered scopes without opening payloads."""
        ...

    def sources(self, admission: dict) -> tuple[Scope, Scope]:
        """Resolve ordered admission scopes inside this tenant without opening payloads."""
        ...

    def execute(self, value, access: Access) -> Outcome:
        """Verify graphs, witnesses and octets, then recheck access before publication."""
        ...


class Backend(Protocol):
    def ready(self, access: Access, verb: str) -> bool: ...

    def transaction(self, access: Access, command: Command) -> ContextManager[Transaction]:
        """Scope lookup, object ownership, snapshot validity and operation share the store lock."""
        ...


def framing(headers, route):
    length = headers.length(required=route.request is not None, maximum=route.maximum)
    content_type = headers.one("content-type")
    digest = headers.one(wire.DIGEST_HEADER)
    if route.request == OCTETS:
        if content_type != OCTETS or digest is None or re.fullmatch("[a-f0-9]{64}", digest) is None:
            raise BadHTTP("missing octet-stream framing")
    elif route.request is not None:
        if content_type != CONTENT_TYPE or digest is not None:
            raise BadHTTP("invalid JSON headers")
    elif length or digest is not None:
        raise BadHTTP("unexpected request body")
    return length, digest


def json_request(reader, length, name, validator):
    value = strict_json(reader.body(length))
    validator.validate(name, value)
    return value


def invoke(backend, access, command, headers, reader, validator):
    route = command.route
    # Recovery tokens come from a closed request envelope. Backend scope
    # resolution checks the cursor against this principal and tenant.
    if route.operation == "recovery":
        length, _ = framing(headers, route)
        command = replace(command, recovery_query=json_request(reader, length, route.request, validator))

    with backend.transaction(access, command) as transaction:
        scope = transaction.scope()
        if scope is None or (command.attempt is not None and scope.attempt_id != command.attempt):
            raise Denied
        if route.operation != "admission" or scope.request_id is not None:
            access.require(route.verb, scope)

        sources = None
        comparison_writer = None
        length, digest = framing(headers, route)
        page = None
        value = command.recovery_query
        if route.response == "page":
            value = page = paging(command.query)
        elif command.query is not None:
            raise BadHTTP("unexpected query")
        if headers.all("range") or any(key.startswith("if-") for key in headers.values):
            raise BadHTTP("unsupported conditional or range header")

        if route.operation == "admission":
            value = json_request(reader, length, "Admission", validator)
            wire.verify_admission(value, validator)
            if value["attempt_id"] != command.attempt:
                raise ContractError("admission and route attempt differ")
            target = Scope(value["request_id"], command.attempt)
            access.require(route.verb, target)
            scope = target
            if value["operation"] == "compare":
                comparison_writer = value["executor_id"]
                sources = transaction.sources(value)
                reference, candidate = sources
                if (reference.attempt_id, candidate.attempt_id) != (
                    value["reference_attempt_id"], value["candidate_attempt_id"],
                ):
                    raise Denied
                access.comparison("admit", target, reference, candidate, writer=comparison_writer)
        elif route.operation == "start":
            raw = reader.body(length)
            start = wire.decode_start(raw, content_length=length, digest=digest, validator=validator)
            if start["attempt_id"] != command.attempt or start["executor_id"] != access.principal:
                raise Denied
            value = raw
        elif route.operation == "upload":
            value = Upload(reader, length, digest)
        elif route.operation == "finish":
            sources = transaction.compare()
            if sources is not None:
                comparison_writer = access.principal
                access.comparison("finish", scope, *sources, writer=comparison_writer)
            value = wire.decode_finish(reader.body(length), validator)
            if strict_json(value[0])["attempt_id"] != command.attempt:
                raise ContractError("terminal and route attempt differ")
        elif route.operation == "claim":
            value = json_request(reader, length, "StartAck", validator)
            if value["attempt_id"] != command.attempt:
                raise ContractError("claim and route attempt differ")

        outcome = transaction.execute(value, access)
        if isinstance(value, Upload):
            if not isinstance(outcome.body, dict) or not value.verified or (
                outcome.body.get("bytes"), outcome.body.get("sha256")
            ) != (value.length, value.digest):
                raise Integrity
        access.require(route.verb, scope)
        if sources is not None:
            access.comparison(route.verb, scope, *sources, writer=comparison_writer)
        return outcome, page


def ready(backend, access, probe, policy_revision, healthy, identity_ready):
    acl = access.current()
    if not any(acl.rules(access.principal, access.tenant, probe["verb"])):
        raise Denied
    if (not healthy() or not identity_ready()
            or backend.ready(access, probe["verb"]) is not True or not healthy()):
        raise Unavailable
    final = access.current()
    if final.revision != acl.revision:
        if not any(final.rules(access.principal, access.tenant, probe["verb"])):
            raise Denied
        raise Unavailable
    return Outcome(200, {
        **probe, "tenant": access.tenant, "principal": access.principal,
        "record_pin": FROZEN_PINS[1][2], "index_pin": FROZEN_PINS[2][2],
        "policy_revision": policy_revision, "acl_revision": acl.revision,
    })
