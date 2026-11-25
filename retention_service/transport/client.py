"""Clients for owner-authenticated Unix sockets and signer-backed TLS endpoints."""

from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
import base64
import json
import os
from pathlib import Path
import re
import secrets
import socket
from urllib.parse import urlencode

from ..contracts.validation import ContractError, FROZEN_PINS, strict_json
from ..contracts import wire
from .http import BadHTTP, CONTENT_TYPE, OCTETS, Reader
from .peer import PeerConnection
from .routes import MAX_INSPECTION, MAX_UPLOAD, PREFIX
from .tls import TLSError


class RemoteError(Exception):
    def __init__(self, status):
        super().__init__(f"retention request returned HTTP {status}")
        self.status = status


@dataclass(frozen=True)
class Endpoint:
    unix: Path | None = None
    ip: str | None = None
    port: int | None = None

    def __post_init__(self):
        if self.unix is not None:
            if not self.unix.is_absolute() or self.ip is not None or self.port is not None:
                raise ValueError("expected one absolute Unix endpoint")
        else:
            address = ip_address(self.ip)
            mapped = getattr(address, "ipv4_mapped", None)
            if (address.is_unspecified or (mapped is not None and mapped.is_unspecified)
                    or type(self.port) is not int or not 1 <= self.port <= 65535):
                raise ValueError("expected one exact TCP endpoint")

    def connect(self, timeout):
        if self.unix is not None:
            family, target = socket.AF_UNIX, os.fspath(self.unix)
        else:
            address = ip_address(self.ip)
            family = socket.AF_INET if address.version == 4 else socket.AF_INET6
            target = str(address), self.port
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            if family != socket.AF_UNIX:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.connect(target)
            return sock
        except BaseException:
            sock.close()
            raise


@dataclass(frozen=True)
class ObjectPage:
    reference: str
    total_bytes: int
    sha256: str
    offset: int
    data: bytes


class Client:
    """One credential and one endpoint per client. Connections are single-user."""

    def __init__(self, tls, endpoint, validator, *, timeout=5.0):
        if not 0 < timeout <= 60 or (tls is None and endpoint.unix is None) or (tls is not None and tls.server):
            raise ValueError("expected a local Unix endpoint or client TLS context and bounded timeout")
        self.tls = tls
        self.endpoint = endpoint
        self.validator = validator
        self.timeout = timeout

    def session(self, tenant):
        self.validator.validate("Id", tenant)
        sock = self.endpoint.connect(self.timeout)
        try:
            if self.tls is None:
                connection = PeerConnection(sock, os.geteuid())
                host = "localhost"
            else:
                connection = self.tls.wrap(sock, timeout=self.timeout)
                host = self.tls.hostname.decode("ascii")
            return Session(connection, tenant, self.validator, self.timeout, host=host)
        except BaseException:
            sock.close()
            raise


class Session:
    def __init__(self, tls, tenant, validator, timeout, *, host=None):
        self.tls = tls
        self.host = host if host is not None else tls.context.hostname.decode("ascii")
        self.tenant = tenant
        self.validator = validator
        self.timeout = timeout
        self.reader = Reader(tls)

    def close(self):
        self.tls.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _exchange(self, method, path, *, payload=None, raw=None, maximum=MAX_INSPECTION):
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
            body_length = len(body)
        elif raw is not None:
            body_length, digest, body = raw
            self.validator.validate("Hash", digest)
            if type(body_length) is not int or not 0 <= body_length <= MAX_UPLOAD:
                raise ValueError("invalid outbound body length")
        else:
            body = ()
            body_length = 0
        headers = [f"Host: {self.host}", f"Retention-Tenant: {self.tenant}"]
        if payload is not None:
            headers.extend((f"Content-Type: {CONTENT_TYPE}", f"Content-Length: {body_length}"))
        if raw is not None:
            headers.extend((f"Content-Type: {OCTETS}", f"Content-Length: {body_length}",
                            f"Retention-SHA256: {digest}"))
        head = "\r\n".join((f"{method} {PREFIX}{path} HTTP/1.1", *headers, "", "")).encode("ascii")
        self.reader.begin(self.timeout)
        try:
            if not self.tls.current():
                raise TLSError("server identity is no longer valid")
            sent = self._send_request(head, body, body_length)
            result = self.reader.head()
            if result is None:
                raise TLSError("retention response was lost")
            line, fields = result
            match = re.fullmatch(rb"HTTP/1\.1 ([1-5][0-9]{2}) [ -~]+", line)
            if match is None:
                raise BadHTTP("invalid retention status line")
            status = int(match[1])
            if status == 204:
                length = fields.length(maximum=0)
            else:
                length = fields.length(required=True, maximum=maximum if 200 <= status < 300 else 1024)
            data = self.reader.body(length)
            if not self.tls.current():
                raise TLSError("server identity is no longer valid")
        except BaseException:
            self.close()
            raise
        if status not in {200, 201, 204}:
            if status != 404 or body_length:
                self.close()
            if status == 404 and (
                data != wire.DENIAL_BODY or fields.one("content-type") != CONTENT_TYPE
                or fields.one("cache-control") != "no-store"
            ):
                self.close()
                raise BadHTTP("invalid denial response")
            raise RemoteError(status)
        if sent != body_length:
            self.close()
            raise BadHTTP("success returned before the complete request")
        return status, fields, data

    def _send_request(self, head, body, length):
        deadline = self.reader.deadline
        if isinstance(body, bytes) and len(head) + length <= 16384:
            self.tls.send(head + body, deadline)
            return length
        self.tls.send(head, deadline)
        sent = 0
        for piece in (body,) if isinstance(body, bytes) else body:
            if not isinstance(piece, bytes) or sent + len(piece) > length:
                raise ValueError("upload exceeds declared length")
            for offset in range(0, len(piece), 16384):
                if self.tls.readable():
                    return sent
                chunk = piece[offset:offset + 16384]
                self.tls.send(chunk, deadline)
                sent += len(chunk)
        if sent != length:
            raise ValueError("upload does not match declared length")
        return sent

    def _json(self, method, path, form, **kwargs):
        status, fields, data = self._exchange(method, path, **kwargs)
        if status not in {200, 201} or fields.one("content-type") != CONTENT_TYPE:
            raise BadHTTP("expected retention JSON response")
        value = strict_json(data)
        self.validator.validate(form, value)
        return value

    def _attempt(self, attempt):
        self.validator.validate("Attempt", attempt)
        return "/attempts/" + attempt

    def _path(self, form, value):
        self.validator.validate(form, value)
        return "/" + value

    def ready(self, verb, *, principal, policy_revision, acl_revision):
        probe = {"challenge": secrets.token_hex(32), "verb": verb}
        self.validator.validate("Probe", probe)
        value = self._json("POST", "/ready", "Ready", payload=probe)
        expected = {**probe, "tenant": self.tenant, "principal": principal, "policy_revision": policy_revision,
                    "acl_revision": acl_revision, "record_pin": FROZEN_PINS[1][2], "index_pin": FROZEN_PINS[2][2]}
        if value != expected:
            raise ContractError("readiness proof differs from the expected authority or pins")
        return value

    def admit(self, admission):
        wire.verify_admission(admission, self.validator)
        value = self._json("PUT", self._attempt(admission["attempt_id"]) + "/admission", "Admission",
                           payload=admission)
        if value != admission:
            raise ContractError("admission acknowledgement mismatch")
        return value

    def start(self, attempt, raw, *, request_sha256):
        digest = sha256(raw).hexdigest()
        if len(raw) > wire.MAX_RECORD_BYTES:
            raise ContractError("start exceeds record limit")
        value = self._json("PUT", self._attempt(attempt) + "/start", "StartAck",
                           raw=(len(raw), digest, raw))
        if value != {"attempt_id": attempt, "start_sha256": digest, "request_sha256": request_sha256}:
            raise ContractError("start acknowledgement mismatch")
        return value

    def claim(self, acknowledgement):
        self.validator.validate("StartAck", acknowledgement)
        value = self._json("POST", self._attempt(acknowledgement["attempt_id"]) + "/launch-claim",
                           "StartAck", payload=acknowledgement)
        if value != acknowledgement:
            raise ContractError("claim acknowledgement mismatch")
        return value

    def upload(self, attempt, upload, length, digest, chunks):
        path = self._attempt(attempt) + "/uploads" + self._path("PathId", upload)
        value = self._json("PUT", path, "ObjectMeta", raw=(length, digest, chunks))
        if (value["bytes"], value["sha256"]) != (length, digest):
            raise ContractError("upload acknowledgement mismatch")
        return value

    def finish(self, attempt, terminal, artifact, *, start_sha256):
        if len(terminal) > wire.MAX_RECORD_BYTES or len(artifact) > wire.MAX_ARTIFACT_BYTES:
            raise ContractError("finish exceeds evidence limits")
        envelope = {key: base64.b64encode(raw).decode("ascii") for key, raw in
                    (("terminal_b64", terminal), ("artifact_b64", artifact))}
        value = self._json("PUT", self._attempt(attempt) + "/finish", "FinishAck", payload=envelope)
        expected = {"attempt_id": attempt, "start_sha256": start_sha256,
                    "terminal_sha256": sha256(terminal).hexdigest()}
        if value != expected:
            raise ContractError("finish acknowledgement mismatch")
        return value

    def _inspection(self, path, *, attempt=None, receipt=None):
        value = self._json("GET", path, "Inspection")
        if value["tenant"] != self.tenant or (attempt is not None and value["attempt_id"] != attempt):
            raise ContractError("inspection scope mismatch")
        if receipt is not None and (value["state"] != "committed" or value["receipt_ref"] != receipt):
            raise ContractError("receipt binding mismatch")
        wire.verify_admission(value["admission"], self.validator)
        if value["admission"]["attempt_id"] != value["attempt_id"]:
            raise ContractError("inspection admission attempt mismatch")
        for form in ("start", "terminal"):
            record = value[form]
            if record is not None and (
                record["attempt_id"] != value["attempt_id"]
                or sha256(wire.record_bytes(record)).hexdigest() != value[form + "_sha256"]
            ):
                raise ContractError("inspection record digest mismatch")
        if value["terminal"] is not None and value["terminal"]["start_sha256"] != value["start_sha256"]:
            raise ContractError("terminal does not bind the inspected start")
        return value

    def inspect(self, attempt):
        return self._inspection(self._attempt(attempt) + "/inspect", attempt=attempt)

    def receipt(self, reference):
        return self._inspection("/receipts" + self._path("ReceiptRef", reference), receipt=reference)

    def artifact(self, attempt, reference, *, length, digest):
        path = self._attempt(attempt) + "/artifacts" + self._path("ArtRef", reference)
        status, fields, data = self._exchange("GET", path, maximum=wire.MAX_ARTIFACT_BYTES)
        if (status != 200 or fields.one("content-type") != OCTETS
                or len(data) != length or sha256(data).hexdigest() != digest):
            raise ContractError("artifact bytes differ from the verified terminal")
        return data

    def hold(self, attempt, hold, *, release=False):
        path = self._attempt(attempt) + "/holds" + self._path("PathId", hold)
        status, _, data = self._exchange("DELETE" if release else "PUT", path, maximum=0)
        if status != 204 or data:
            raise BadHTTP("expected empty hold acknowledgement")

    def recovery(self, query):
        self.validator.validate("RecoveryQuery", query)
        return self._json("POST", "/recovery/pages", "RecoveryPage", payload=query)

    def _page(self, path, reference, offset, limit):
        if type(offset) is not int or type(limit) is not int:
            raise ValueError("offset and limit must be integers")
        wire.page_parameters((str(offset),), (str(limit),))
        query = urlencode({"offset": offset, "limit": limit})
        status, fields, data = self._exchange("GET", path + "?" + query, maximum=limit)
        if status != 200 or fields.one("content-type") != OCTETS:
            raise BadHTTP("expected retention octet page")
        ref, size, digest, page_offset, page_bytes, page_digest = (
            fields.one(name) for name in wire.PAGE_HEADERS
        )
        self.validator.validate("Hash", digest)
        size, actual_offset, bytes_on_page = map(_unsigned, (size, page_offset, page_bytes))
        if (ref != reference or actual_offset != offset or bytes_on_page != len(data)
                or (offset >= size and (size != 0 or offset != 0))
                or len(data) != min(limit, max(0, size - offset)) or sha256(data).hexdigest() != page_digest):
            raise ContractError("page metadata or octets differ from the requested range")
        return ObjectPage(ref, size, digest, actual_offset, data)

    def object_page(self, attempt, reference, *, offset=0, limit=wire.MAX_PAGE_BYTES):
        path = self._attempt(attempt) + "/objects" + self._path("ObjRef", reference)
        return self._page(path, reference, offset, limit)

    def recovery_item(self, reference, *, offset=0, limit=wire.MAX_PAGE_BYTES):
        path = "/recovery/items" + self._path("ObjRef", reference)
        return self._page(path, reference, offset, limit)


def _unsigned(value):
    if value is None or not re.fullmatch(r"0|[1-9][0-9]{0,15}", value) or int(value) > wire.MAX_OFFSET:
        raise ContractError("invalid page integer")
    return int(value)
