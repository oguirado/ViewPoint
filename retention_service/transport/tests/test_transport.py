"""Exercise the production provider, listeners and HTTP client with a store fixture."""

from base64 import b64decode
from contextlib import contextmanager
from dataclasses import replace
import errno
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import socket
import ssl
import threading
from unittest.mock import Mock, patch

from ...contracts import wire
from ...contracts.test_contracts import load_vectors
from ...contracts.validation import ContractError, ContractValidator, strict_json
from ..access import ACL, ComparisonGrant, Credential, Denied, Grant, Scope
from ..client import Client, Endpoint, RemoteError, Session
from ..http import BadHTTP, response_head
from ..routes import Conflict, Outcome, Page, Unsatisfiable
from ..server import Approval, Server, effective_config
from ..tls import TLSError
from .fixtures import DNS, TLSFixtures

from cryptography.hazmat.primitives import hashes


VECTORS = {entry["name"]: entry["data"] for entry in load_vectors() if entry["valid"]}
INSPECTION = VECTORS["inspection-committed"]
RECORD = VECTORS["record-admission"]
COMPARE = VECTORS["compare-admission"]
LAUNCH = RECORD["attempt_id"]
ATTEMPT = COMPARE["attempt_id"]
REFERENCE = COMPARE["reference_attempt_id"]
MISSING = "9" * 32
TENANT = INSPECTION["tenant"]
OTHER = "fixture:other"
UNGRANTED = "fixture:ungranted"
RECEIPT = INSPECTION["receipt_ref"]
ARTIFACT = INSPECTION["artifact_ref"]
OBJECT = "obj_" + "4" * 32
MANIFEST = VECTORS["recovery-page-next"]["manifest_item"]
DATA = b"retention transport\0data"
START = b64decode(VECTORS["external_launch_started-canonical"])
FINISH = strict_json(b64decode(VECTORS["finish-exact-index"]))
TERMINAL = b64decode(FINISH["terminal_b64"])
ARTIFACT_DATA = b64decode(FINISH["artifact_b64"])
MANIFEST_DATA = b64decode(VECTORS["manifest-exact-bytes"])
READER = "reader"
DENIAL = "denial-reader"
AUTHORITY = "authority"
RUNNER = RECORD["executor_id"]
COMPARATOR = COMPARE["executor_id"]
RECOVERY = "recovery"
HOLDER = "holder"
POLICY = "policy-test"
REVISION = "acl-test"


def scoped(attempt):
    request = {ATTEMPT: COMPARE["request_id"], LAUNCH: RECORD["request_id"], REFERENCE: "request/reference"}
    return Scope(request[attempt], attempt)


class FixtureStore:
    """Only the transport tests use this synchronous, non-durable route double."""

    def __init__(self):
        self.lock = threading.RLock()
        self.events = []
        self.available = True
        self.inspection = INSPECTION
        self.admissions = {}
        self.claimed = set()
        self.holds = set()
        self.before_response = None

    def ready(self, access, verb):
        self.events.append(("ready", access.tenant, verb))
        return self.available

    @contextmanager
    def transaction(self, access, command):
        with self.lock:
            self.events.append(("scope", access.tenant, command.route.operation, command.attempt))
            yield FixtureTransaction(self, access, command)


class FixtureTransaction:
    def __init__(self, store, access, command):
        self.store = store
        self.access = access
        self.command = command
        self.operation = command.route.operation
        self.attempt = command.attempt
        self.reference = command.reference
        self.tenant = access.tenant

    def scope(self):
        if self.tenant == OTHER:
            if self.operation == "object" and self.attempt == LAUNCH and self.reference == OBJECT:
                return scoped(LAUNCH)
            raise Denied
        if self.operation == "receipt":
            if self.reference != RECEIPT:
                raise Denied
            return scoped(ATTEMPT)
        if self.operation == "recovery_item":
            if self.reference != MANIFEST or self.access.principal != RECOVERY:
                raise Denied
            return Scope()
        if self.operation == "recovery":
            cursor = self.command.recovery_query.get("cursor")
            if cursor not in {None, "cursor:next"} or self.access.principal != RECOVERY:
                raise Denied
            return Scope()
        if self.attempt not in {ATTEMPT, LAUNCH, REFERENCE}:
            raise Denied
        if self.operation == "object" and (self.attempt != ATTEMPT or self.reference != OBJECT):
            raise Denied
        if self.operation == "artifact" and (self.attempt != ATTEMPT or self.reference != ARTIFACT):
            raise Denied
        return scoped(self.attempt)

    def sources(self, admission):
        if self.tenant != TENANT:
            raise Denied
        try:
            return tuple(scoped(admission[key]) for key in ("reference_attempt_id", "candidate_attempt_id"))
        except KeyError:
            raise Denied from None

    def compare(self):
        return self.sources(COMPARE) if self.attempt == ATTEMPT else None

    def execute(self, value, access):
        if self.operation not in {"recovery", "recovery_item"}:
            access.require(self.command.route.verb, self.scope())
        self.store.events.append(("execute", self.tenant, self.operation, self.attempt))
        if self.store.before_response:
            self.store.before_response()
        if self.operation in {"object", "recovery_item"}:
            raw = DATA if self.operation == "object" else MANIFEST_DATA
            offset, limit = value
            if offset >= len(raw):
                raise Unsatisfiable
            return Outcome(200, Page(self.reference, len(raw), sha256(raw).hexdigest(), offset,
                                     raw[offset:offset + limit]))
        if self.operation == "artifact":
            return Outcome(200, ARTIFACT_DATA)
        if self.operation in {"inspect", "receipt"}:
            if self.attempt not in {None, ATTEMPT}:
                raise Denied
            return Outcome(200, self.store.inspection)
        if self.operation == "recovery":
            vector = "recovery-page-end" if "cursor" in value else "recovery-page-next"
            return Outcome(200, VECTORS[vector])
        if self.operation == "admission":
            old = self.store.admissions.get(self.attempt)
            if old is not None and old != value:
                raise Conflict
            self.store.admissions[self.attempt] = value
            return Outcome(201 if old is None else 200, value)
        if self.operation == "start":
            return Outcome(201, {"attempt_id": self.attempt, "start_sha256": sha256(value).hexdigest(),
                                 "request_sha256": RECORD["request_sha256"]})
        if self.operation == "claim":
            if self.attempt in self.store.claimed:
                raise Conflict
            self.store.claimed.add(self.attempt)
            return Outcome(201, value)
        if self.operation == "upload":
            with io.BytesIO() as staging:
                value.receive(staging.write)
                raw = staging.getvalue()
            return Outcome(201, {"object_ref": OBJECT, "bytes": len(raw), "sha256": sha256(raw).hexdigest()})
        if self.operation == "finish":
            terminal, artifact = value
            if (terminal, artifact) != (TERMINAL, ARTIFACT_DATA):
                raise Conflict
            return Outcome(200, {"attempt_id": self.attempt, "start_sha256": INSPECTION["start_sha256"],
                                 "terminal_sha256": sha256(terminal).hexdigest()})
        if self.operation == "hold":
            self.store.holds.add((self.attempt, self.reference))
            return Outcome(204)
        if self.operation == "release":
            self.store.holds.discard((self.attempt, self.reference))
            return Outcome(204)
        raise AssertionError(self.operation)


class Transport(TLSFixtures):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.validator = ContractValidator(os.environ["RETENTION_FROZEN_DIR"])

    def setUp(self):
        super().setUp()
        self.server_tls = self.tls("server", self.server_chain, server=True)
        self.identities = {
            READER: ("reader", self.reader_cert, self.reader_chain),
            DENIAL: ("other", self.other_cert, self.other_chain),
        }
        for principal, handle in ((AUTHORITY, "authority"), (RUNNER, "runner"), (COMPARATOR, "comparator"),
                                  (RECOVERY, "recovery"), (HOLDER, "holder")):
            self.identities[principal] = (handle, *self.leaf(handle))
        credentials = tuple(Credential(cert.fingerprint(hashes.SHA256()).hex(), "urn:test:" + handle, principal)
                            for principal, (handle, cert, _) in self.identities.items())
        grants = (
            Grant(READER, TENANT, "inspect", scoped(ATTEMPT)),
            Grant(READER, TENANT, "inspect", scoped(REFERENCE)),
            Grant(READER, TENANT, "inspect", scoped(LAUNCH)),
            Grant(DENIAL, OTHER, "inspect", scoped(LAUNCH)),
            Grant(AUTHORITY, TENANT, "admit"),
            Grant(RUNNER, TENANT, "start", scoped(LAUNCH)),
            Grant(RUNNER, TENANT, "claim", scoped(LAUNCH)),
            Grant(RUNNER, TENANT, "upload", scoped(LAUNCH)),
            Grant(RUNNER, TENANT, "finish", scoped(LAUNCH)),
            Grant(COMPARATOR, TENANT, "finish", scoped(ATTEMPT)),
            Grant(HOLDER, TENANT, "hold", scoped(ATTEMPT)),
            Grant(RECOVERY, TENANT, "recover"),
        )
        self.acl = ACL(REVISION, credentials, grants, (
            ComparisonGrant(COMPARATOR, READER, TENANT, scoped(ATTEMPT), scoped(REFERENCE), scoped(LAUNCH)),
        ))
        self.store = FixtureStore()
        self.config = {
            "unix_socket": str(self.root / "service"),
            "tcp_bind_ip": "127.0.0.1",
            "tcp_port": self.port(),
            "server_dns_name": DNS,
            "server_principal": "fixture-server",
            "server_cert_id": "fixture-server-cert",
            "public_chain": str(self.server_chain),
            "trust_mount": str(self.trust),
            "provider_module_id": "fixture-provider",
            "signer_socket": str(self.signer.path),
            "key_handle": "server",
            "store_root": str(self.root / "store"),
            "policy_revision": POLICY,
            "acl_revision": REVISION,
        }

    def port(self, family=socket.AF_INET):
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1" if family == socket.AF_INET else "::1", 0))
            return probe.getsockname()[1]

    def server(self, *, start=True, config=None, approved=None,
               client_networks=("127.0.0.1/32", "::1/128")):
        if config is None:
            config = self.config
        if approved is None:
            approved = config.copy()
        approval = Approval(approved, os.getuid(), os.getgid(), 0o600, os.getuid(),
                            self.server_tls.library_sha256, self.server_tls.chain_sha256,
                            client_networks)
        result = Server(config=config, approval=approval, tls=self.server_tls, validator=self.validator,
                        acl=lambda: self.acl, backend=self.store, timeout=3)
        if start:
            self.addCleanup(result.close)
            result.start()
        return result

    def endpoint(self, network=False):
        return (Endpoint(ip=self.config["tcp_bind_ip"], port=self.config["tcp_port"]) if network
                else Endpoint(unix=Path(self.config["unix_socket"])))

    def client(self, principal=READER, *, network=False):
        handle, _, chain = self.identities[principal]
        return Client(self.tls(handle, chain), self.endpoint(network), self.validator, timeout=3)

    def raw(self, session, line, headers=(), body=b""):
        session.reader.begin(3)
        head = "\r\n".join((f"{line} HTTP/1.1", f"Host: {DNS}", *headers, "", "")).encode()
        session.tls.send(head + body, session.reader.deadline)
        received = bytearray()
        while b"\r\n\r\n" not in received:
            chunk = session.tls.read(16384, session.reader.deadline)
            self.assertTrue(chunk)
            received.extend(chunk)
        head, raw = bytes(received).split(b"\r\n\r\n", 1)
        length = next((int(field.split(b":", 1)[1]) for field in head.split(b"\r\n")
                       if field.lower().startswith(b"content-length:")), None)
        if length is None:
            self.assertTrue(head.startswith(b"HTTP/1.1 204 "))
            length = 0
        while len(raw) < length:
            raw += session.tls.read(length - len(raw), session.reader.deadline)
        self.assertEqual(len(raw), length)
        return head + b"\r\n\r\n" + raw

    def expect_status(self, status, call, *args, **kwargs):
        with self.assertRaises(RemoteError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)

    def test_both_listeners_default_and_all_readiness_verbs(self):
        server = self.server()
        self.assertFalse(server.config["local_only"])
        roles = {"admit": AUTHORITY, "start": RUNNER, "claim": RUNNER, "upload": RUNNER,
                 "finish": COMPARATOR, "inspect": READER, "hold": HOLDER, "recover": RECOVERY}
        for network in (False, True):
            for verb, principal in roles.items():
                with self.subTest(network=network, verb=verb), self.client(principal, network=network).session(TENANT) as session:
                    first = session.ready(verb, principal=principal, policy_revision=POLICY, acl_revision=REVISION)
                    second = session.ready(verb, principal=principal, policy_revision=POLICY, acl_revision=REVISION)
                    self.assertNotEqual(first["challenge"], second["challenge"])
        self.assertEqual({event[0] for event in self.store.events}, {"ready"})

    def test_same_host_still_requires_a_client_certificate(self):
        self.server()
        missing = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        missing.load_verify_locations(cafile=self.trust)
        for network in (False, True):
            with self.subTest(network=network):
                sock = self.endpoint(network).connect(3)
                with self.assertRaises((ssl.SSLError, ConnectionError)):
                    with missing.wrap_socket(sock, server_hostname=DNS) as tls:
                        tls.sendall(b"GET /retention/v1/ready HTTP/1.1\r\nHost: retention.test\r\n\r\n")
                        payload = tls.recv(4096)
                        if not payload:
                            raise ConnectionError("no application response")
                sock.close()
        self.assertEqual(self.store.events, [])

    def test_unmapped_or_wrong_san_identity_has_no_application_response(self):
        cert, chain = self.leaf("unmapped")
        self.acl = replace(self.acl, credentials=self.acl.credentials + (
            Credential(cert.fingerprint(hashes.SHA256()).hex(), "urn:test:different", READER),
        ))
        self.server()
        context = self.tls("unmapped", chain)
        for network in (False, True):
            client = Client(context, self.endpoint(network), self.validator, timeout=3)
            with client.session(TENANT) as session:
                session.reader.begin(3)
                try:
                    session.tls.send(b"malformed\r\n\r\n", session.reader.deadline)
                    raw = session.tls.read(4096, session.reader.deadline)
                except TLSError:
                    raw = b""
                self.assertEqual(raw, b"")
        self.assertEqual(self.store.events, [])

    def test_verified_unknown_and_admission_only_inspection(self):
        self.store.inspection = VECTORS["inspection-unknown"]
        self.server()
        with self.client(AUTHORITY).session(TENANT) as authority:
            authority.admit(RECORD)
        with self.client().session(TENANT) as inspector:
            unknown = inspector.inspect(ATTEMPT)
            self.assertEqual((unknown["state"], unknown["terminal"]), ("unknown", None))
            for attempt in (LAUNCH, MISSING):
                result = self.raw(inspector, f"GET /retention/v1/attempts/{attempt}/inspect",
                                  (f"Retention-Tenant: {TENANT}",))
                self.assertEqual(result, wire.DENIAL_RESPONSE)

    def test_readiness_fails_if_signer_lost_on_a_reused_connection(self):
        self.server()
        with self.client().session(TENANT) as session:
            session.ready("inspect", principal=READER, policy_revision=POLICY, acl_revision=REVISION)
            self.signer.path.unlink()
            self.expect_status(503, session.ready, "inspect", principal=READER,
                               policy_revision=POLICY, acl_revision=REVISION)

    def test_valid_denial_reader_control_and_byte_identical_denials(self):
        self.server()
        paths = (
            f"/attempts/{ATTEMPT}/inspect",
            f"/attempts/{MISSING}/inspect",
            f"/receipts/{RECEIPT}",
            "/receipts/rcp_" + "9" * 32,
            f"/attempts/{ATTEMPT}/artifacts/{ARTIFACT}",
            f"/attempts/{ATTEMPT}/objects/{OBJECT}?offset=999999",
            f"/attempts/{ATTEMPT}/objects/{OBJECT}?offset=-1",
            f"/recovery/items/{MANIFEST}?offset=999",
        )
        for network in (False, True):
            with self.client(DENIAL, network=network).session(OTHER) as session:
                own = session.object_page(LAUNCH, OBJECT)
                self.assertEqual(own.data, DATA)
                for tenant in (TENANT, OTHER, UNGRANTED):
                    self.store.events.clear()
                    for path in paths:
                        with self.subTest(network=network, tenant=tenant, path=path):
                            raw = self.raw(session, "GET /retention/v1" + path, ("Retention-Tenant: " + tenant,))
                            self.assertEqual(raw, wire.DENIAL_RESPONSE)
                    self.assertFalse(any(event[0] == "execute" for event in self.store.events))
                    if tenant != OTHER:
                        self.assertEqual(self.store.events, [])
                payload = b'{"limit":1,"cursor":"cursor:next"}'
                raw = self.raw(session, "POST /retention/v1/recovery/pages",
                               (f"Retention-Tenant: {OTHER}", "Content-Type: application/json",
                                f"Content-Length: {len(payload)}"), payload)
                self.assertEqual(raw, wire.DENIAL_RESPONSE)

    def test_missing_duplicate_malformed_tenant_before_resource_lookup(self):
        self.server()
        for headers in ((), ("Retention-Tenant: bad tenant",),
                        (f"Retention-Tenant: {TENANT}", f"retention-tenant: {OTHER}")):
            with self.subTest(headers=headers), self.client().session(TENANT) as session:
                response = self.raw(session, f"GET /retention/v1/attempts/{ATTEMPT}/inspect", headers)
                self.assertTrue(response.startswith(b"HTTP/1.1 400 "))
        self.assertEqual(self.store.events, [])

    def test_acl_revocation_between_pages_and_during_current_response(self):
        self.server()
        with self.client(network=True).session(TENANT) as session:
            self.assertEqual(session.object_page(ATTEMPT, OBJECT, limit=4).data, DATA[:4])
            before = self.acl
            self.acl = replace(before, revision="acl-new", grants=tuple(
                grant for grant in before.grants if grant.scope != scoped(ATTEMPT)))
            self.store.events.clear()
            self.expect_status(404, session.object_page, ATTEMPT, OBJECT, offset=4)
            self.assertEqual(self.store.events, [])
            revoked = self.acl
            self.acl = before
            self.store.before_response = lambda: setattr(self, "acl", revoked)
            self.expect_status(404, session.object_page, ATTEMPT, OBJECT, offset=4)

    def test_network_certificate_rotation_and_reused_certificate_revocation(self):
        self.server()
        with self.client(network=True).session(TENANT) as old:
            old.object_page(ATTEMPT, OBJECT)
            cert, chain = self.leaf("rotated-reader")
            credential = Credential(cert.fingerprint(hashes.SHA256()).hex(), "urn:test:rotated-reader", READER)
            self.acl = replace(self.acl, revision="acl-rotated", credentials=self.acl.credentials + (credential,))
            self.crl(self.reader_cert)
            with self.assertRaises(TLSError):
                old.object_page(ATTEMPT, OBJECT)
            self.identities[READER] = ("rotated-reader", cert, chain)
            with self.client(network=True).session(TENANT) as new:
                self.assertEqual(new.object_page(ATTEMPT, OBJECT).data, DATA)

    def test_round_trip_resource_and_paging_routes(self):
        self.server()
        with self.client().session(TENANT) as inspector:
            result = inspector.inspect(ATTEMPT)
            self.assertEqual(result, inspector.receipt(RECEIPT))
            artifact = result["terminal"]["artifact"]
            self.assertEqual(inspector.artifact(ATTEMPT, ARTIFACT, length=artifact["bytes"],
                                                digest=artifact["sha256"]), ARTIFACT_DATA)
            pages = [inspector.object_page(ATTEMPT, OBJECT, offset=offset, limit=8)
                     for offset in range(0, len(DATA), 8)]
            self.assertEqual(b"".join(page.data for page in pages), DATA)
            self.assertEqual({page.sha256 for page in pages}, {sha256(DATA).hexdigest()})
            self.expect_status(416, inspector.object_page, ATTEMPT, OBJECT, offset=len(DATA))
        with self.client(RECOVERY, network=True).session(TENANT) as recovery:
            first = recovery.recovery({"limit": 1})
            self.assertEqual(first["next"], "cursor:next")
            item = recovery.recovery_item(first["manifest_item"], offset=3, limit=20)
            self.assertEqual(item.data, MANIFEST_DATA[3:23])
            self.assertIsNone(recovery.recovery({"limit": 1, "cursor": first["next"]})["next"])
            self.expect_status(404, recovery.recovery, {"limit": 1, "cursor": "foreign"})

    def test_write_routes_and_rejecting_controls(self):
        self.server()
        with self.client(AUTHORITY).session(TENANT) as authority:
            self.assertEqual(authority.admit(RECORD), RECORD)
            self.assertEqual(authority.admit(RECORD), RECORD)
        with self.client(RUNNER, network=True).session(TENANT) as runner:
            ack = runner.start(LAUNCH, START, request_sha256=RECORD["request_sha256"])
            self.assertEqual(runner.claim(ack), ack)
            self.expect_status(409, runner.claim, ack)
        with self.client(RUNNER, network=True).session(TENANT) as runner:
            good = runner.upload(LAUNCH, "upload-test", len(DATA), sha256(DATA).hexdigest(),
                                 (DATA[:2], DATA[2:]))
            self.assertEqual(good["object_ref"], OBJECT)
            self.expect_status(422, runner.upload, LAUNCH, "bad-digest", len(DATA), "0" * 64, (DATA,))
        with self.client(HOLDER).session(TENANT) as holder:
            holder.hold(ATTEMPT, "hold-test")
            self.assertIn((ATTEMPT, "hold-test"), self.store.holds)
            holder.hold(ATTEMPT, "hold-test", release=True)
            self.assertEqual(self.store.holds, set())
        with self.client(RUNNER).session(TENANT) as runner:
            self.expect_status(404, runner.admit, RECORD)

    def test_comparison_sources_are_ordered_and_both_grants_are_current(self):
        self.server()
        with self.client(AUTHORITY).session(TENANT) as authority:
            self.assertEqual(authority.admit(COMPARE), COMPARE)
        with self.client(COMPARATOR).session(TENANT) as comparator:
            self.assertEqual(comparator.finish(ATTEMPT, TERMINAL, ARTIFACT_DATA,
                                               start_sha256=INSPECTION["start_sha256"])["attempt_id"], ATTEMPT)
        with self.client(AUTHORITY).session(TENANT) as authority:
            swapped = {**COMPARE, "reference_attempt_id": LAUNCH, "candidate_attempt_id": REFERENCE}
            swapped["admission_sha256"] = sha256(wire.admission_bytes(swapped, self.validator)).hexdigest()
            self.expect_status(404, authority.admit, swapped)
        before = self.acl
        for source in (REFERENCE, LAUNCH):
            self.acl = replace(before, revision="acl-revoked", grants=tuple(
                grant for grant in before.grants if grant.scope != scoped(source) or grant.principal != READER))
            with self.client(COMPARATOR).session(TENANT) as comparator:
                self.expect_status(404, comparator.finish, ATTEMPT, TERMINAL, ARTIFACT_DATA,
                                   start_sha256=INSPECTION["start_sha256"])

    def test_comparison_admission_requires_its_executor_and_current_delegation(self):
        self.server()
        acl = self.acl
        controls = [
            ("ungranted-executor", "ungranted-principal", None),
            ("other-executor", RUNNER, None),
            ("writer-revoked", COMPARATOR, Grant(COMPARATOR, TENANT, "finish", scoped(ATTEMPT))),
            ("reference-inspector-revoked", COMPARATOR, Grant(READER, TENANT, "inspect", scoped(REFERENCE))),
            ("candidate-inspector-revoked", COMPARATOR, Grant(READER, TENANT, "inspect", scoped(LAUNCH))),
        ]
        client = self.client(AUTHORITY)
        for case, executor, revoked in controls:
            with self.subTest(case=case):
                with self.store.lock:
                    self.store.admissions.clear()
                self.acl = replace(acl, grants=tuple(grant for grant in acl.grants if grant != revoked))
                value = {**COMPARE, "executor_id": executor}
                value["admission_sha256"] = sha256(wire.admission_bytes(value, self.validator)).hexdigest()
                body = json.dumps(value).encode()
                with client.session(TENANT) as authority:
                    result = self.raw(authority, f"PUT /retention/v1/attempts/{ATTEMPT}/admission",
                                      (f"Retention-Tenant: {TENANT}", "Content-Type: application/json",
                                       f"Content-Length: {len(body)}"), body)
                self.assertEqual(result, wire.DENIAL_RESPONSE)
                self.assertEqual(self.store.admissions, {})
        self.acl = acl
        with client.session(TENANT) as authority:
            self.assertEqual(authority.admit(COMPARE), COMPARE)

    def test_finish_terminal_attempt_must_match_before_execution(self):
        self.server()
        mismatched = wire.record_bytes({**strict_json(TERMINAL), "attempt_id": LAUNCH})
        with self.client(COMPARATOR).session(TENANT) as comparator:
            self.expect_status(422, comparator.finish, ATTEMPT, mismatched, ARTIFACT_DATA,
                               start_sha256=INSPECTION["start_sha256"])
        self.assertFalse(any(event[0] == "execute" for event in self.store.events))

    def test_hold_responses_omit_content_length(self):
        self.server()
        with self.client(HOLDER).session(TENANT) as holder:
            raw = self.raw(holder, f"PUT /retention/v1/attempts/{ATTEMPT}/holds/framing",
                           (f"Retention-Tenant: {TENANT}",))
            self.assertEqual(raw, b"HTTP/1.1 204 No Content\r\nCache-Control: no-store\r\n\r\n")
            holder.hold(ATTEMPT, "framing", release=True)
        self.assertEqual(self.store.holds, set())
        with self.assertRaises(ValueError):
            response_head(204, 1, None)
        with self.assertRaises(ValueError):
            response_head(204, 0, None, [("Content-Length", "0")])

    def test_client_only_accepts_empty_204_framing(self):
        path = f"/attempts/{ATTEMPT}/holds/framing"
        for framing in (b"\r\n", b"Content-Length: 0\r\n\r\n", b"Content-Length: 1\r\n\r\nx"):
            with self.subTest(framing=framing):
                tls = Mock()
                tls.context.hostname = DNS.encode()
                tls.current.return_value = True
                tls.read.return_value = b"HTTP/1.1 204 No Content\r\n" + framing
                with Session(tls, TENANT, self.validator, 1) as session:
                    if framing.endswith(b"x"):
                        with self.assertRaises(BadHTTP):
                            session._exchange("PUT", path)
                    else:
                        status, _, body = session._exchange("PUT", path)
                        self.assertEqual((status, body), (204, b""))

    def test_revoked_cursor_and_foreign_recovery_look_identical(self):
        self.server()
        with self.client(RECOVERY).session(TENANT) as session:
            page = session.recovery({"limit": 1})
            self.acl = replace(self.acl, grants=tuple(g for g in self.acl.grants if g.verb != "recover"))
            raw = self.raw(session, f"GET /retention/v1/recovery/items/{page['manifest_item']}",
                           (f"Retention-Tenant: {TENANT}",))
            self.assertEqual(raw, wire.DENIAL_RESPONSE)

    def test_ambiguous_transfers_and_malformed_ranges_fail(self):
        self.server()
        path = f"/retention/v1/attempts/{LAUNCH}/start"
        cases = (
            ("PUT " + path, ("Transfer-Encoding: chunked", "Content-Length: 0")),
            ("PUT " + path, ("Content-Length: 0", "Content-Length: 0")),
            ("PUT " + path, ("Content-Length: 0", "Content-Type: application/octet-stream",
                             "Retention-SHA256: " + "A" * 64)),
        )
        for line, headers in cases:
            with self.client(RUNNER).session(TENANT) as session:
                raw = self.raw(session, line, (f"Retention-Tenant: {TENANT}", *headers))
                self.assertTrue(raw.startswith(b"HTTP/1.1 400 "))
        for query in ("offset=0&offset=1", "offset=9007199254740992", "offset=-1", "limit=1.2"):
            with self.client().session(TENANT) as session:
                raw = self.raw(session, f"GET /retention/v1/attempts/{ATTEMPT}/objects/{OBJECT}?{query}",
                               (f"Retention-Tenant: {TENANT}",))
                self.assertTrue(raw.startswith(b"HTTP/1.1 400 "))
        self.assertFalse(any(event[0] == "execute" for event in self.store.events))

    def test_failed_second_bind_removes_unix_listener(self):
        with socket.socket() as occupied:
            occupied.bind((self.config["tcp_bind_ip"], self.config["tcp_port"]))
            occupied.listen()
            with self.assertRaises(OSError):
                self.server()
        self.assertFalse(Path(self.config["unix_socket"]).exists())

    def test_ipv6_listener_is_v6_only_and_bind_approval_is_exact(self):
        self.config.update(tcp_bind_ip="::1", tcp_port=self.port(socket.AF_INET6))
        with self.assertRaisesRegex(ContractError, "named approval"):
            self.server(start=False, approved={**self.config, "tcp_bind_ip": "127.0.0.1"})
        server = self.server()
        self.assertEqual(server.listeners[1].getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY), 1)
        with self.client(network=True).session(TENANT) as session:
            self.assertEqual(session.object_page(ATTEMPT, OBJECT).data, DATA)

    def test_wildcards_including_mapped_wildcard_refused(self):
        for address in ("0.0.0.0", "::", "::ffff:0.0.0.0", "::ffff:0:0"):
            with self.subTest(address=address), self.assertRaisesRegex(ContractError, "wildcard"):
                effective_config(self.validator, {**self.config, "tcp_bind_ip": address})

    def test_tcp_requires_a_matching_family_client_network(self):
        for networks in ((), ("::1/128",)):
            with self.subTest(networks=networks), self.assertRaisesRegex(ContractError, "named client network"):
                self.server(start=False, client_networks=networks)

    def test_tcp_rejects_a_peer_outside_the_client_network(self):
        server = self.server(client_networks=("127.0.0.2/32",))
        with self.assertRaises(TLSError), self.client(network=True).session(TENANT):
            self.fail("excluded peer completed a TLS handshake")
        self.assertEqual(self.store.events, [])
        self.assertTrue(server.healthy())
        with self.client().session(TENANT) as session:
            self.assertEqual(session.object_page(ATTEMPT, OBJECT).data, DATA)

    def test_accept_survives_a_connection_abort(self):
        server = self.server(start=False)
        accept = socket.socket.accept
        injected = threading.Event()

        def abort_once(listener):
            if listener.family == socket.AF_INET and not injected.is_set():
                injected.set()
                raise OSError(errno.ECONNABORTED, os.strerror(errno.ECONNABORTED))
            return accept(listener)

        self.addCleanup(server.close)
        with patch.object(socket.socket, "accept", abort_once):
            server.start()
            self.assertTrue(injected.wait(3))
            with self.client().session(TENANT) as session:
                session.ready("inspect", principal=READER, policy_revision=POLICY, acl_revision=REVISION)
            with self.client(network=True).session(TENANT) as session:
                session.ready("inspect", principal=READER, policy_revision=POLICY, acl_revision=REVISION)
            self.assertTrue(server.healthy())
            self.assertTrue(server.acceptors[1].is_alive())

    def test_readiness_fails_after_listener_or_capacity_loss(self):
        server = self.server()
        self.store.available = False
        with self.client().session(TENANT) as session:
            self.expect_status(503, session.ready, "inspect", principal=READER,
                               policy_revision=POLICY, acl_revision=REVISION)
        self.store.available = True
        server.listeners[1].close()
        with self.client().session(TENANT) as session:
            self.expect_status(503, session.ready, "inspect", principal=READER,
                               policy_revision=POLICY, acl_revision=REVISION)

    def test_explicit_local_only_keeps_unix_mutual_authentication(self):
        local = {key: value for key, value in self.config.items() if key not in {"tcp_bind_ip", "tcp_port"}}
        local["local_only"] = True
        server = self.server(config=local)
        self.assertEqual([sock.family for sock in server.listeners], [socket.AF_UNIX])
        with self.client().session(TENANT) as session:
            self.assertEqual(session.object_page(ATTEMPT, OBJECT).data, DATA)
        with self.assertRaises(ConnectionError):
            self.endpoint(network=True).connect(1)
