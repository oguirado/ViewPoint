"""Linux Unix-socket credentials and deadline-bound HTTP byte I/O."""

import select
import socket
import struct
from time import monotonic

from .tls import TLSError


UCRED = struct.Struct("iII")


class PeerConnection:
    def __init__(self, sock, uid):
        try:
            if sock.family != socket.AF_UNIX:
                raise TLSError("local authentication requires a Unix socket")
            _, peer_uid, _ = UCRED.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, UCRED.size))
            if peer_uid != uid:
                raise TLSError("unexpected Unix peer UID")
        except BaseException:
            sock.close()
            raise
        self.socket = sock

    def current(self):
        # SO_PEERCRED records the immutable UID of this connection's original peer.
        return self.socket.fileno() != -1

    def read(self, size, deadline):
        try:
            self.socket.settimeout(self._remaining(deadline))
            return self.socket.recv(min(size, 16_384))
        except OSError as exc:
            raise TLSError("Unix peer read failed") from exc

    def send(self, raw, deadline):
        try:
            self.socket.settimeout(self._remaining(deadline))
            self.socket.sendall(raw)
        except OSError as exc:
            raise TLSError("Unix peer write failed") from exc

    def readable(self):
        poller = select.poll()
        poller.register(self.socket, select.POLLIN)
        return bool(poller.poll(0))

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("retention I/O deadline expired")
        return remaining

    def close(self):
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
