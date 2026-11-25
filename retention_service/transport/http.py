"""Bounded HTTP/1.1 framing shared by the server and client."""

from dataclasses import dataclass
from http import HTTPStatus
import re
import time

from ..contracts.wire import DENIAL_RESPONSE, MAX_OFFSET
from .tls import TLSError


MAX_HEAD = 16_384
TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
DECIMAL = re.compile(r"0|[1-9][0-9]*")
TARGET = re.compile(rb"/[A-Za-z0-9._:@+/=?&-]*")
CONTENT_TYPE = "application/json"
OCTETS = "application/octet-stream"


class BadHTTP(ValueError):
    pass


@dataclass
class Headers:
    values: dict

    def all(self, name):
        return self.values.get(name.lower(), ())

    def one(self, name, default=None):
        values = self.all(name)
        if len(values) > 1:
            raise BadHTTP("duplicate header")
        return values[0] if values else default

    def length(self, *, required=False, maximum=MAX_OFFSET):
        value = self.one("content-length")
        if value is None and not required:
            return 0
        if value is None or len(value) > len(str(maximum)) or DECIMAL.fullmatch(value) is None:
            raise BadHTTP("invalid content length")
        length = int(value)
        if length > maximum:
            raise BadHTTP("content length exceeds limit")
        return length

    def close(self):
        return "close" in {token.strip().lower() for value in self.all("connection") for token in value.split(",")}


class Reader:
    def __init__(self, tls):
        self.tls = tls
        self.buffer = bytearray()
        self.deadline = 0

    def begin(self, timeout):
        self.deadline = time.monotonic() + timeout

    def head(self):
        while (separator := self.buffer.find(b"\r\n\r\n")) < 0:
            room = MAX_HEAD - len(self.buffer)
            if room <= 0:
                raise BadHTTP("headers exceed limit")
            chunk = self.tls.read(room, self.deadline)
            if not chunk:
                if not self.buffer:
                    return None
                raise BadHTTP("incomplete headers")
            self.buffer.extend(chunk)
        lines = bytes(self.buffer[:separator]).split(b"\r\n")
        del self.buffer[:separator + 4]
        if len(lines) > 65:
            raise BadHTTP("too many headers")
        values = {}
        for line in lines[1:]:
            key, colon, value = line.partition(b":")
            if not colon or not TOKEN.fullmatch(key):
                raise BadHTTP("invalid field name")
            if any(byte != 9 and not 32 <= byte <= 126 for byte in value):
                raise BadHTTP("invalid field value")
            values.setdefault(key.decode("ascii").lower(), []).append(value.decode("ascii").strip(" \t"))
        headers = Headers(values)
        if headers.all("transfer-encoding") or headers.all("expect") or headers.all("upgrade"):
            raise BadHTTP("unsupported HTTP framing")
        return lines[0], headers

    def chunks(self, length):
        remaining = length
        while remaining:
            if self.buffer:
                size = min(len(self.buffer), remaining)
                raw = bytes(self.buffer[:size])
                del self.buffer[:size]
            else:
                raw = self.tls.read(remaining, self.deadline)
                if not raw:
                    raise TLSError("incomplete HTTP body")
            remaining -= len(raw)
            yield raw

    def body(self, length):
        return b"".join(self.chunks(length))


def request_line(line, headers):
    fields = line.split(b" ")
    if (len(fields) != 3 or fields[2] != b"HTTP/1.1" or not TOKEN.fullmatch(fields[0])
            or not TARGET.fullmatch(fields[1]) or not headers.one("host")):
        raise BadHTTP("invalid HTTP request line or Host")
    return fields[0].decode("ascii"), fields[1].decode("ascii")


def response_head(status, length, content_type, headers=()):
    reason = HTTPStatus(status).phrase
    fields = [f"HTTP/1.1 {status} {reason}"]
    if content_type is not None:
        fields.append("Content-Type: " + content_type)
    if status == 204:
        if length != 0:
            raise ValueError("204 response must be empty")
    else:
        fields.append(f"Content-Length: {length}")
    fields.append("Cache-Control: no-store")
    for key, value in headers:
        if not TOKEN.fullmatch(key.encode("ascii")) or any(not 32 <= ord(c) <= 126 for c in value):
            raise ValueError("invalid outbound header")
        if status == 204 and key.lower() == "content-length":
            raise ValueError("204 response cannot include Content-Length")
        fields.append(f"{key}: {value}")
    return ("\r\n".join(fields) + "\r\n\r\n").encode("ascii")


def response(tls, deadline, status, body=b"", content_type=None, headers=()):
    if status == 404:
        tls.send(DENIAL_RESPONSE, deadline)
        return
    head = response_head(status, len(body), content_type, headers)
    if len(head) + len(body) <= 16384:
        tls.send(head + body, deadline)
    else:
        tls.send(head, deadline)
        tls.send(body, deadline)
