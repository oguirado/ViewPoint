"""One retention application for local Unix sockets and approved TLS listeners."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import errno
from ipaddress import ip_address, ip_network
import json
import os
from pathlib import Path
import socket
import stat
import threading

from ..contracts.validation import ContractError
from ..contracts.wire import TENANT_HEADER, tenant_header, MAX_ARTIFACT_BYTES
from .access import Access, Denied, Scope
from .peer import PeerConnection
from .http import BadHTTP, CONTENT_TYPE, OCTETS, Reader, request_line, response
from .routes import (Conflict, Integrity, MAX_INSPECTION, Page, Unsatisfiable, Unavailable,
                     decode_route, framing, invoke, json_request, ready)
from .tls import TLSError


PATH_FIELDS = ("unix_socket", "public_chain", "trust_mount", "signer_socket", "store_root")
RETRY_ACCEPT = {
    errno.EINTR, errno.EAGAIN, errno.EWOULDBLOCK, errno.ECONNABORTED, errno.EPROTO,
    errno.ENETDOWN, errno.ENOPROTOOPT, errno.EHOSTDOWN, errno.ENONET, errno.EHOSTUNREACH,
    errno.EOPNOTSUPP, errno.ENETUNREACH,
}
INVALID_LISTENER = {errno.EBADF, errno.EINVAL, errno.ENOTSOCK}
ERRORS = {
    400: b'{"error":"bad_request"}',
    409: b'{"error":"conflict"}',
    416: b'{"error":"unsatisfiable"}',
    422: b'{"error":"invalid_evidence"}',
    500: b'{"error":"integrity"}',
    503: b'{"error":"service_unavailable"}',
}


@dataclass(frozen=True)
class Approval:
    """Public values from the named operator approval, supplied independently of config."""

    config: dict
    unix_uid: int
    unix_gid: int
    unix_mode: int
    signer_uid: int
    library_sha256: str
    chain_sha256: str
    client_networks: tuple[str, ...] = ()


def effective_config(validator, config):
    effective = validator.load_config(config)
    for key in PATH_FIELDS:
        try:
            candidate = Path(effective[key])
            if not candidate.is_absolute() or "\0" in effective[key]:
                raise ValueError
            effective[key] = os.fspath(candidate.resolve())
        except (ValueError, OSError, RuntimeError):
            raise ContractError("configuration requires resolvable absolute paths") from None
    if "tcp_bind_ip" in effective:
        address = ip_address(effective["tcp_bind_ip"])
        mapped = getattr(address, "ipv4_mapped", None)
        if address.is_unspecified or (mapped is not None and mapped.is_unspecified):
            raise ContractError("wildcard TCP binding is forbidden")
        effective["tcp_bind_ip"] = str(address)
    return effective


def approved_config(validator, config, approval, tls):
    effective = effective_config(validator, config)
    if effective != effective_config(validator, approval.config):
        raise ContractError("configuration differs from named approval")
    if (not tls.server or not tls.pointer or tls.hostname.decode("ascii") != effective["server_dns_name"]
            or str(tls.chain_path.resolve()) != effective["public_chain"]
            or str(tls.trust_path.resolve()) != effective["trust_mount"]
            or str(tls.signer.path.resolve()) != effective["signer_socket"]
            or tls.signer.uid != approval.signer_uid or tls.signer.handle != effective["key_handle"]):
        raise ContractError("TLS identity differs from named approval")
    if (tls.library_sha256, tls.chain_sha256) != (approval.library_sha256, approval.chain_sha256):
        raise ContractError("TLS build or certificate differs from named approval")
    if not 0 <= approval.unix_mode <= 0o770 or approval.unix_mode & 0o007:
        raise ContractError("Unix listener permissions must exclude other users")
    return effective


class Server:
    def __init__(self, *, config, validator, acl, backend, approval=None, tls=None, local=None,
                 workers=8, timeout=5.0):
        if local is not None:
            if approval is not None or tls is not None or set(config) != {"policy_revision", "acl_revision"}:
                raise ContractError("local-open supports only generated Unix endpoints")
            self.config = {**config, "local_only": True}
            self.unix_paths = local
            self.unix_owner = (os.geteuid(), os.getegid(), 0o600)
            self.networks = ()
        else:
            if approval is None or tls is None:
                raise ContractError("TLS transport requires configuration and named approval")
            self.config = approved_config(validator, config, approval, tls)
            self.unix_paths = {Path(self.config["unix_socket"]): None}
            self.unix_owner = (approval.unix_uid, approval.unix_gid, approval.unix_mode)
            self.networks = tuple(ip_network(value) for value in approval.client_networks)
            if not self.config["local_only"] and not any(
                network.version == ip_address(self.config["tcp_bind_ip"]).version for network in self.networks
            ):
                raise ContractError("TCP listener requires a named client network")
        self.tls = tls
        self.validator = validator
        self.acl = acl
        self.backend = backend
        if self.acl().revision != self.config["acl_revision"]:
            raise ContractError("unexpected initial ACL revision")
        if not 1 <= workers <= 32 or not 0 < timeout <= 60:
            raise ValueError("workers and timeout must be bounded")
        self.timeout = timeout
        self.workers = workers
        self.path_identities = {}
        self.listeners = []
        self.acceptors = []
        self.connections = set()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.failed = threading.Event()
        self.slots = threading.BoundedSemaphore(workers)
        self.executor = None

    def _unix(self, path):
        parent = path.parent.stat()
        if parent.st_uid not in {0, os.geteuid()} or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ContractError("Unix listener requires a protected parent directory")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(os.fspath(path))
            info = path.lstat()
            self.path_identities[path] = info.st_dev, info.st_ino
            uid, gid, mode = self.unix_owner
            os.chown(path, uid, gid, follow_symlinks=False)
            path.chmod(mode)
            return sock
        except BaseException:
            sock.close()
            raise

    def _tcp(self):
        address = ip_address(self.config["tcp_bind_ip"])
        family = socket.AF_INET if address.version == 4 else socket.AF_INET6
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((str(address), self.config["tcp_port"]))
            return sock
        except BaseException:
            sock.close()
            raise

    def start(self):
        if self.executor is not None or self.stopped.is_set():
            raise RuntimeError("listener lifecycle is single use")
        try:
            for path in self.unix_paths:
                self.listeners.append(self._unix(path))
            principals = list(self.unix_paths.values())
            if not self.config["local_only"]:
                self.listeners.append(self._tcp())
                principals.append(None)
            if not self._identity_ready():
                raise TLSError("server identity failed its startup check")
            # Bind every required endpoint before accepting any request.
            for sock in self.listeners:
                sock.listen(self.workers)
                sock.settimeout(0.1)
            self.executor = ThreadPoolExecutor(max_workers=self.workers)
            for sock, principal in zip(self.listeners, principals, strict=True):
                thread = threading.Thread(target=self._accept, args=(sock, principal))
                thread.start()
                self.acceptors.append(thread)
            return self
        except BaseException:
            self.close()
            raise

    def _identity_ready(self):
        return self.tls is None or self.tls.confirm_identity()

    def healthy(self):
        expected = len(self.unix_paths) + (not self.config["local_only"])
        if self.stopped.is_set() or self.failed.is_set() or len(self.listeners) != expected:
            return False
        try:
            for path in self.unix_paths:
                info = path.lstat()
                if (not stat.S_ISSOCK(info.st_mode)
                        or (info.st_dev, info.st_ino) != self.path_identities[path]
                        or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != self.unix_owner):
                    return False
            return all(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) for sock in self.listeners)
        except OSError:
            return False

    def _accept(self, listener, principal):
        while not self.stopped.is_set():
            try:
                conn, peer = listener.accept()
            except TimeoutError:
                continue
            except OSError as error:
                if error.errno in INVALID_LISTENER:
                    if not self.stopped.is_set():
                        self.failed.set()
                    return
                if error.errno not in RETRY_ACCEPT:
                    # Back off on descriptor or buffer exhaustion and other unexpected errors.
                    self.stopped.wait(0.1)
                continue
            if listener.family != socket.AF_UNIX:
                address = ip_address(peer[0])
                if not any(address in network for network in self.networks):
                    conn.close()
                    continue
            if not self.slots.acquire(blocking=False):
                conn.close()
                continue
            with self.lock:
                if self.stopped.is_set():
                    conn.close()
                    self.slots.release()
                    return
                self.connections.add(conn)
                self.executor.submit(self._serve, conn, principal)

    def _serve(self, sock, local_principal):
        try:
            if sock.family != socket.AF_UNIX:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection = (PeerConnection(sock, self.unix_owner[0]) if self.tls is None
                          else self.tls.wrap(sock, timeout=self.timeout))
            with connection:
                reader = Reader(connection)
                fingerprint = connection.fingerprint() if self.tls is not None else None

                def identify(acl):
                    if not connection.current():
                        return None
                    if local_principal is not None:
                        return local_principal
                    return acl.identify(fingerprint, connection)

                principal = identify(self.acl())
                if principal is None:
                    return
                for _ in range(256):
                    reader.begin(self.timeout)
                    try:
                        request = reader.head()
                        if request is None:
                            return
                        line, headers = request
                        acl = self.acl()
                        if identify(acl) != principal:
                            return
                        method, target = request_line(line, headers)
                        tenant = tenant_header(headers.all(TENANT_HEADER), self.validator)
                    except (BadHTTP, ContractError):
                        if identify(self.acl()) == principal:
                            response(connection, reader.deadline, 400, ERRORS[400], CONTENT_TYPE)
                        return
                    access = Access(self.acl, identify, principal, tenant)
                    if self._exchange(connection, reader, acl, access, method, target, headers):
                        return
                    if headers.close():
                        return
        except OSError:
            # Authentication failures and ambiguous I/O have no application result.
            pass
        finally:
            sock.close()
            with self.lock:
                self.connections.discard(sock)
            self.slots.release()

    def _exchange(self, connection, reader, acl, access, method, target, headers):
        principal, tenant = access.principal, access.tenant
        close = True
        try:
            if not any(acl.rules(principal, tenant)):
                raise Denied
            command = decode_route(method, target, self.validator)
            route = command.route
            if route.operation == "ready":
                length, _ = framing(headers, route)
                if command.query is not None:
                    raise BadHTTP("unexpected query")
                probe = json_request(reader, length, "Probe", self.validator)
                outcome = ready(self.backend, access, probe, self.config["policy_revision"],
                                self.healthy, self._identity_ready)
                page = None
            else:
                if not acl.permits(principal, tenant, route.verb, Scope(attempt_id=command.attempt), partial=True):
                    raise Denied
                outcome, page = invoke(self.backend, access, command, headers, reader, self.validator)
            status, raw, content_type, fields = self._encode(command, outcome, page, tenant)
            close = False
        except Denied:
            status, raw, content_type, fields = 404, b"", None, ()
            # Without reading a denied body, only reuse an empty, unambiguous request.
            close = headers.all("content-length") not in ((), ["0"])
        except (BadHTTP, ContractError, Conflict, Unsatisfiable, Integrity, Unavailable) as error:
            status = {BadHTTP: 400, ContractError: 422, Conflict: 409, Unsatisfiable: 416,
                      Integrity: 500, Unavailable: 503}[type(error)]
            raw, content_type, fields = ERRORS[status], CONTENT_TYPE, ()
        except TLSError:
            raise
        except OSError:
            status, raw, content_type, fields = 503, ERRORS[503], CONTENT_TYPE, ()
        response(connection, reader.deadline, status, raw, content_type, fields)
        return close

    def _encode(self, command, outcome, paging, tenant):
        route = command.route
        status, value = outcome.status, outcome.body
        writes = {"admission", "start", "claim", "upload", "finish"}
        if route.response is None:
            if status != 204 or value is not None:
                raise Integrity
            return status, b"", None, ()
        if status != 200 and not (status == 201 and route.operation in writes):
            raise Integrity
        if route.response == "page":
            if not isinstance(value, Page):
                raise Integrity
            try:
                fields = value.headers(self.validator, command.reference, paging)
            except ContractError:
                raise Integrity from None
            return status, value.data, OCTETS, fields
        if route.response == OCTETS:
            if not isinstance(value, bytes) or len(value) > MAX_ARTIFACT_BYTES:
                raise Integrity
            return status, value, OCTETS, ()
        try:
            self.validator.validate(route.response, value)
        except ContractError:
            raise Integrity from None
        if "attempt_id" in value and command.attempt is not None and value["attempt_id"] != command.attempt:
            raise Integrity
        if route.response == "Inspection":
            if value["tenant"] != tenant or any(
                record["attempt_id"] != value["attempt_id"]
                for record in (value["admission"], value["start"], value["terminal"]) if record is not None
            ):
                raise Integrity
            if route.operation == "receipt" and (
                value["state"] != "committed" or value["receipt_ref"] != command.reference
            ):
                raise Integrity
        raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
        if len(raw) > MAX_INSPECTION:
            raise Integrity
        return status, raw, CONTENT_TYPE, ()

    def close(self):
        self.stopped.set()
        for listener in self.listeners:
            listener.close()
        with self.lock:
            for conn in self.connections:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        for thread in self.acceptors:
            thread.join()
        if self.executor is not None:
            self.executor.shutdown()
        for path, identity in self.path_identities.items():
            try:
                info = path.lstat()
                if (info.st_dev, info.st_ino) == identity:
                    path.unlink()
            except FileNotFoundError:
                pass
        self.path_identities.clear()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
