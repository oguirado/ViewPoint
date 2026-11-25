"""Synthetic public certificates and an isolated memory-only Unix signer."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import os
from pathlib import Path
import resource
import socket
import subprocess
import tempfile
import threading
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ...contracts.signer import decode_sign_request
from ...contracts.validation import ContractError
from ..tls import FIELD_LENGTH, LENGTH, PEERCRED, Signer, TLSContext


DNS = "retention.test"
NATIVE = Path(__file__).resolve().parents[1] / "native.c"


def name(common_name):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


class MemorySigner:
    def __init__(self, path):
        self.path = path
        self.keys = {}
        self.requests = []
        self.mutation = None
        self.uid = os.getuid()
        self.stop = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(os.fspath(path))
        path.chmod(0o600)
        self.listener.listen()
        self.listener.settimeout(0.05)
        self.thread = threading.Thread(target=self._serve)
        self.thread.start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                sock, _ = self.listener.accept()
            except TimeoutError:
                continue
            with sock:
                sock.settimeout(1)
                try:
                    self._reply(sock)
                except (OSError, ContractError):
                    continue

    def _reply(self, sock):
        _, uid, _ = PEERCRED.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEERCRED.size))
        if uid != self.uid:
            return
        with sock.makefile("rb") as stream:
            frame = stream.read(329)
        nonce, handle, digest = decode_sign_request(frame)
        self.requests.append(handle)
        key = self.keys.get(handle)
        signature = b"" if key is None else key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        payload = bytes((1, int(key is None))) + nonce + FIELD_LENGTH.pack(len(signature)) + signature
        frame = LENGTH.pack(len(payload)) + payload
        if self.mutation:
            frame = self.mutation(frame)
        sock.sendall(frame)

    def close(self):
        self.stop.set()
        self.thread.join(3)
        if self.thread.is_alive():
            raise RuntimeError("test signer did not stop")
        self.listener.close()
        self.path.unlink(missing_ok=True)
        self.keys.clear()


class TLSFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, hard = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (0, hard))
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.library = Path(cls.directory.name) / "native.so"
        subprocess.run(
            ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
             "-Wl,-z,relro,-z,now", "-o", cls.library, NATIVE, "-lssl", "-lcrypto"],
            check=True,
        )

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.signer = MemorySigner(self.root / "signer")
        self.addCleanup(self.signer.close)
        self.now = datetime.now(UTC)
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = name("offline-test-ca")
        self.ca = (
            self.certificate(ca_name, ca_name, self.ca_key.public_key())
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                           critical=True)
            .sign(self.ca_key, hashes.SHA256())
        )
        self.trust = self.root / "trust.pem"
        self.crl()
        self.server_cert, self.server_chain = self.leaf("server", server=True)
        self.reader_cert, self.reader_chain = self.leaf("reader")
        self.other_cert, self.other_chain = self.leaf("other")

    def certificate(self, subject, issuer, public_key):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(public_key)
                .serial_number(x509.random_serial_number())
                .not_valid_before(self.now - timedelta(minutes=1))
                .not_valid_after(self.now + timedelta(hours=1)))

    def leaf(self, handle, *, server=False, dns=DNS, eku=True):
        key = ec.generate_private_key(ec.SECP256R1())
        self.signer.keys[handle] = key
        san = x509.DNSName(dns) if server else x509.UniformResourceIdentifier("urn:test:" + handle)
        builder = (self.certificate(name(handle), self.ca.subject, key.public_key())
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([san]), critical=False))
        if eku:
            oid = ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH
            builder = builder.add_extension(x509.ExtendedKeyUsage([oid]), critical=False)
        cert = builder.sign(self.ca_key, hashes.SHA256())
        chain = self.root / (handle + ".pem")
        chain.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return cert, chain

    def crl(self, *revoked):
        builder = (x509.CertificateRevocationListBuilder().issuer_name(self.ca.subject)
                   .last_update(self.now - timedelta(minutes=1))
                   .next_update(self.now + timedelta(hours=1)))
        for cert in revoked:
            entry = (x509.RevokedCertificateBuilder().serial_number(cert.serial_number)
                     .revocation_date(self.now).build())
            builder = builder.add_revoked_certificate(entry)
        crl = builder.sign(self.ca_key, hashes.SHA256())
        pending = self.trust.with_suffix(".new")
        pending.write_bytes(self.ca.public_bytes(serialization.Encoding.PEM)
                            + crl.public_bytes(serialization.Encoding.PEM))
        pending.replace(self.trust)

    def tls(self, handle, chain, *, server=False, dns=DNS, uid=None, trust=None):
        signer = Signer(self.signer.path, os.getuid() if uid is None else uid, handle)
        ctx = TLSContext(self.library, chain, trust or self.trust, dns, signer, server=server,
                         library_sha256=sha256(self.library.read_bytes()).hexdigest(),
                         chain_sha256=sha256(chain.read_bytes()).hexdigest())
        self.addCleanup(ctx.close)
        return ctx

    @contextmanager
    def pair(self, client, server):
        left, right = socket.socketpair()
        results = []

        def accept():
            try:
                results.append(server.wrap(right, timeout=1))
            except OSError as error:
                results.append(error)
                right.close()

        thread = threading.Thread(target=accept)
        thread.start()
        outgoing = None
        try:
            outgoing = client.wrap(left, timeout=1)
            thread.join(3)
            if isinstance(results[0], OSError):
                raise results[0]
            yield outgoing, results[0]
        finally:
            thread.join(3)
            if outgoing is not None:
                outgoing.close()
            else:
                left.close()
            if results and not isinstance(results[0], OSError):
                results[0].close()
