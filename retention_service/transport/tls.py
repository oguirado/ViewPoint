"""The shared signer-backed TLS adapter for both sides of both endpoints."""

import ctypes as ct
from hashlib import file_digest
from dataclasses import dataclass
import os
from pathlib import Path
from select import poll, POLLIN
import socket
import stat
from struct import Struct
import time

from ..contracts.signer import decode_sign_reply
from ..contracts.validation import ContractError


PEERCRED = Struct("3i")
LENGTH = Struct(">I")
FIELD_LENGTH = Struct(">H")
SIGNER = ct.CFUNCTYPE(ct.c_int, ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_size_t))
OPENSSL = {
    "vp_context": (ct.c_void_p, ct.c_char_p, ct.c_char_p, ct.c_char_p, ct.c_int, SIGNER),
    "vp_context_free": (None, ct.c_void_p),
    "vp_own_identity": (ct.c_int, ct.c_void_p, ct.c_char_p, ct.c_char_p, ct.c_int),
    "vp_connect": (ct.c_void_p, ct.c_void_p, ct.c_int, ct.c_char_p, ct.c_char_p, ct.c_int, ct.c_int),
    "vp_current": (ct.c_int, ct.c_void_p, ct.c_char_p, ct.c_char_p, ct.c_int),
    "vp_peer_digest": (ct.c_int, ct.c_void_p, ct.c_void_p),
    "vp_peer_uri": (ct.c_int, ct.c_void_p, ct.c_char_p, ct.c_size_t),
    "vp_read": (ct.c_int, ct.c_void_p, ct.c_void_p, ct.c_int, ct.c_int),
    "vp_write": (ct.c_int, ct.c_void_p, ct.c_char_p, ct.c_int, ct.c_int),
    "vp_close": (None, ct.c_void_p),
    "vp_diagnostic": (ct.c_size_t, ct.c_void_p, ct.c_size_t),
}


class TLSError(OSError):
    """TLS authentication, signer or application I/O failed."""


def _exact(sock, size, deadline):
    data = bytearray()
    while len(data) < size:
        sock.settimeout(_remaining(deadline))
        piece = sock.recv(size - len(data))
        if not piece:
            raise TLSError("signer frame was truncated")
        data.extend(piece)
    return bytes(data)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TLSError("transport deadline expired")
    return remaining


@dataclass(frozen=True)
class Signer:
    path: Path
    uid: int
    handle: str
    timeout: float = 2.0

    def __post_init__(self):
        if not self.path.is_absolute() or not 0 <= self.uid < 2**31:
            raise ValueError("signer requires an absolute socket path and expected UID")
        if not 1 <= len(self.handle.encode("ascii")) <= 256 or not 0 < self.timeout <= 30:
            raise ValueError("invalid signer handle or timeout")

    def sign(self, digest):
        info = self.path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.uid or info.st_mode & stat.S_IWOTH:
            raise TLSError("signer socket ownership check failed")
        if len(digest) != 32:
            raise TLSError("signer requires one SHA-256 digest")
        handle = self.handle.encode("ascii")
        nonce = os.urandom(32)
        payload = b"\x01\x01" + nonce + FIELD_LENGTH.pack(len(handle)) + handle + digest
        deadline = time.monotonic() + self.timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_remaining(deadline))
            sock.connect(os.fspath(self.path))
            _, uid, _ = PEERCRED.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEERCRED.size))
            if uid != self.uid:
                raise TLSError("signer peer UID mismatch")
            sock.settimeout(_remaining(deadline))
            sock.sendall(LENGTH.pack(len(payload)) + payload)
            sock.shutdown(socket.SHUT_WR)
            prefix = _exact(sock, LENGTH.size, deadline)
            (size,) = LENGTH.unpack(prefix)
            if not 36 <= size <= 108:
                raise TLSError("signer reply size outside bounds")
            reply = _exact(sock, size, deadline)
            sock.settimeout(_remaining(deadline))
            if sock.recv(1):
                raise TLSError("unexpected signer reply suffix")
        signature = decode_sign_reply(prefix + reply, nonce)
        if signature is None:
            raise TLSError("signer refused the selected handle")
        return signature


class TLSContext:
    """Use a built native adapter, public chain and public PEM CA/CRL bundle."""

    def __init__(self, library, chain, trust, hostname, signer, *, server, library_sha256, chain_sha256):
        self.chain_path = Path(chain)
        self.trust_path = Path(trust)
        self.library_path = Path(library)
        if not all(path.is_absolute() for path in (self.chain_path, self.trust_path, self.library_path)):
            raise ValueError("TLS adapter and public credential paths must be absolute")
        self.signer = signer
        self.server = bool(server)
        self.hostname = hostname.encode("idna")
        if not self.hostname or b"\0" in self.hostname:
            raise ValueError("expected server DNS SAN is required")
        self.trust = os.fsencode(self.trust_path)
        with self.library_path.open("rb") as library_file, self.chain_path.open("rb") as chain_file:
            self.library_sha256 = file_digest(library_file, "sha256").hexdigest()
            self.chain_sha256 = file_digest(chain_file, "sha256").hexdigest()
        if (self.library_sha256, self.chain_sha256) != (library_sha256, chain_sha256):
            raise TLSError("TLS adapter or public chain differs from its approved digest")
        self.native = ct.CDLL(os.fspath(self.library_path))
        for name, (result, *parameters) in OPENSSL.items():
            function = getattr(self.native, name)
            function.restype = result
            function.argtypes = parameters

        @SIGNER
        def callback(digest, output, size):
            try:
                signature = signer.sign(ct.string_at(digest, 32))
            except (OSError, ContractError):
                return 0
            ct.memmove(output, signature, len(signature))
            size[0] = len(signature)
            return 1

        self._signer_callback = callback
        self.pointer = self.native.vp_context(
            os.fsencode(self.chain_path), self.trust, self.hostname, self.server, callback,
        )
        if not self.pointer:
            raise self.error("TLS identity setup or startup challenge failed")

    def error(self, message):
        diagnostic = ct.create_string_buffer(256)
        if self.native.vp_diagnostic(diagnostic, len(diagnostic)):
            message += " (" + diagnostic.value.decode("ascii", "replace") + ")"
        return TLSError(message)

    def wrap(self, sock, *, timeout=5.0):
        if not self.pointer or not 0 < timeout <= 60:
            raise ValueError("expected open TLS context and bounded timeout")
        sock.setblocking(False)
        pointer = self.native.vp_connect(
            self.pointer, sock.fileno(), self.trust, self.hostname, self.server,
            max(1, int(timeout * 1000)),
        )
        if not pointer:
            raise self.error("mutual TLS handshake failed")
        return TLSConnection(self, sock, pointer)

    def confirm_identity(self):
        return bool(self.pointer and self.native.vp_own_identity(
            self.pointer, self.trust, self.hostname, self.server,
        ))

    def close(self):
        if self.pointer:
            self.native.vp_context_free(self.pointer)
            self.pointer = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class TLSConnection:
    def __init__(self, context, sock, pointer):
        self.context = context
        self.socket = sock
        self.pointer = pointer
        self.native = context.native

    def current(self):
        return bool(self.pointer and self.native.vp_current(
            self.pointer, self.context.trust, self.context.hostname, self.context.server,
        ))

    def fingerprint(self):
        digest = ct.create_string_buffer(32)
        if not self.pointer or not self.native.vp_peer_digest(self.pointer, digest):
            raise TLSError("verified peer certificate unavailable")
        return digest.raw.hex()

    def has_uri(self, uri):
        encoded = uri.encode("ascii")
        return bool(self.pointer and self.native.vp_peer_uri(self.pointer, encoded, len(encoded)))

    def read(self, size, deadline):
        if not self.pointer:
            raise TLSError("TLS connection is closed")
        size = min(size, 65536)
        buffer = ct.create_string_buffer(size)
        received = self.native.vp_read(
            self.pointer, buffer, size, max(1, int(_remaining(deadline) * 1000)),
        )
        if received < 0:
            raise self.context.error("TLS read failed")
        return buffer.raw[:received]

    def readable(self):
        if not self.pointer:
            raise TLSError("TLS connection is closed")
        reader = poll()
        reader.register(self.socket, POLLIN)
        return bool(reader.poll(0))

    def send(self, raw, deadline):
        if not self.pointer:
            raise TLSError("TLS connection is closed")
        sent = 0
        while sent < len(raw):
            chunk = raw[sent:sent + 65536]
            result = self.native.vp_write(
                self.pointer, chunk, len(chunk), max(1, int(_remaining(deadline) * 1000)),
            )
            if result <= 0:
                raise self.context.error("TLS write failed")
            sent += result

    def close(self):
        if self.pointer:
            self.native.vp_close(self.pointer)
            self.pointer = None
            self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
