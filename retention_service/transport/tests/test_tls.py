from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from hashlib import sha256
import os
import resource
import socket
import ssl
import subprocess
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization

from ..tls import Signer, TLSContext, TLSError
from .fixtures import DNS, TLSFixtures


class TLSFixtureSafety(unittest.TestCase):
    def test_fixture_disables_core_dumps_before_build(self):
        class Probe(TLSFixtures):
            pass

        _, hard = resource.getrlimit(resource.RLIMIT_CORE)
        with patch.object(resource, "setrlimit") as limit:
            def assert_limit_before_build(*args, **kwargs):
                limit.assert_called_once_with(resource.RLIMIT_CORE, (0, hard))

            with patch.object(subprocess, "run", side_effect=assert_limit_before_build):
                try:
                    Probe.setUpClass()
                finally:
                    Probe.doClassCleanups()


class NativeTLS(TLSFixtures):
    def test_provider_authenticates_both_peers(self):
        server = self.tls("server", self.server_chain, server=True)
        client = self.tls("reader", self.reader_chain)
        self.assertEqual(self.signer.requests, ["server", "reader"])
        with self.pair(client, server) as (outgoing, incoming):
            self.assertEqual(self.signer.requests, ["server", "reader", "server", "reader"])
            self.assertTrue(outgoing.current())
            self.assertTrue(incoming.current())
            self.assertTrue(incoming.has_uri("urn:test:reader"))
            self.assertFalse(incoming.has_uri("urn:test:other"))
            deadline = time.monotonic() + 1
            outgoing.send(b"test", deadline)
            self.assertEqual(incoming.read(4, deadline), b"test")

    def test_signer_ownership_handle_and_certificate_binding(self):
        for handle, uid in (("missing", os.getuid()), ("other", os.getuid()), ("reader", os.getuid() + 1)):
            with self.subTest(handle=handle, uid=uid), self.assertRaises(TLSError):
                self.tls(handle, self.reader_chain, uid=uid)
        self.assertEqual(self.signer.requests, ["missing", "other"])

    def test_adapter_and_public_chain_are_pinned_before_signing(self):
        signer = Signer(self.signer.path, os.getuid(), "reader")
        correct_library = sha256(self.library.read_bytes()).hexdigest()
        correct_chain = sha256(self.reader_chain.read_bytes()).hexdigest()
        for library, chain in ((correct_library, "0" * 64), ("0" * 64, correct_chain)):
            with self.assertRaisesRegex(TLSError, "approved digest"):
                TLSContext(self.library, self.reader_chain, self.trust, "retention.test", signer,
                           server=False, library_sha256=library, chain_sha256=chain)
        self.assertEqual(self.signer.requests, [])

    def test_signer_denies_requesting_uid(self):
        self.signer.uid = os.getuid() + 1
        with self.assertRaises(TLSError):
            self.tls("reader", self.reader_chain)
        self.assertEqual(self.signer.requests, [])

    def test_signer_loss_after_startup(self):
        server = self.tls("server", self.server_chain, server=True)
        client = self.tls("reader", self.reader_chain)
        self.signer.path.unlink()
        with self.assertRaises(TLSError), self.pair(client, server):
            self.fail("handshake succeeded after signer loss")

    def test_signature_reply_rejects_nonce_suffix_length_and_version(self):
        for mutation in (lambda b: b[:6] + bytes((b[6] ^ 1,)) + b[7:],
                         lambda b: b + b"\0", lambda b: b[:-1],
                         lambda b: b[:4] + b"\x02" + b[5:]):
            self.signer.mutation = mutation
            with self.assertRaises(TLSError):
                self.tls("reader", self.reader_chain)

    def test_digest_must_be_sha256_sized(self):
        with self.tls("reader", self.reader_chain) as context, self.assertRaises(TLSError):
            context.signer.sign(sha256(b"abc").digest()[:-1])

    def test_wrong_dns_and_missing_eku(self):
        server = self.tls("server", self.server_chain, server=True)
        client = self.tls("reader", self.reader_chain, dns="wrong.test")
        with self.assertRaises(TLSError), self.pair(client, server):
            self.fail("wrong hostname succeeded")
        _, chain = self.leaf("missing-eku", eku=False)
        with self.assertRaises(TLSError):
            self.tls("missing-eku", chain)

    def test_server_rejects_peer_certificates_without_client_auth_eku(self):
        server = self.tls("server", self.server_chain, server=True)
        _, missing_chain = self.leaf("peer-missing-eku", eku=False)
        _, wrong_chain = self.leaf("peer-server-auth", server=True)
        for handle, chain, accepted in (
            ("reader", self.reader_chain, True),
            ("peer-missing-eku", missing_chain, False),
            ("peer-server-auth", wrong_chain, False),
        ):
            with self.subTest(handle=handle):
                client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                client.minimum_version = ssl.TLSVersion.TLSv1_3
                client.load_verify_locations(cafile=self.trust)
                self.load_standard_client_key(client, handle, chain)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    left, right = socket.socketpair()
                    with left, right:
                        left.settimeout(2)
                        incoming = pool.submit(server.wrap, right, timeout=2)
                        if accepted:
                            with client.wrap_socket(left, server_hostname=DNS), incoming.result(3) as peer:
                                self.assertTrue(peer.has_uri("urn:test:reader"))
                        else:
                            with suppress(ssl.SSLError, ConnectionError), client.wrap_socket(left, server_hostname=DNS):
                                pass
                            with self.assertRaises(TLSError), incoming.result(3):
                                self.fail("server accepted the peer EKU")

    def load_standard_client_key(self, client, handle, chain):
        # SSLContext takes a path. A pipe supplies the key without a PEM file.
        key = self.signer.keys[handle].private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        )
        reader, writer = os.pipe()
        with os.fdopen(reader, "rb") as source:
            with os.fdopen(writer, "wb") as sink:
                sink.write(key)
            client.load_cert_chain(chain, f"/proc/self/fd/{source.fileno()}")

    def test_new_and_reused_connections_check_current_revocation(self):
        server = self.tls("server", self.server_chain, server=True)
        client = self.tls("reader", self.reader_chain)
        with self.pair(client, server) as (_, incoming):
            self.assertTrue(incoming.current())
            self.crl(self.reader_cert)
            self.assertFalse(incoming.current())
        with self.assertRaises(TLSError), self.pair(client, server):
            self.fail("revoked client succeeded")
