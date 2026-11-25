"""Synthetic REA exercises the real CLI, signer, wire and persistent store."""

from contextlib import contextmanager
from functools import cache
from hashlib import sha256
from json import dumps, loads
import os
from pathlib import Path
import signal
import socket
from subprocess import DEVNULL, Popen, TimeoutExpired, run
import sys
from time import monotonic, sleep
import unittest
from uuid import uuid4

from cryptography.hazmat.primitives import hashes

from ..contracts.validation import ContractValidator
from ..contracts.wire import record_bytes
from ..transport.client import Client, Endpoint, RemoteError
from ..transport.tests.fixtures import DNS, TLSFixtures
from ..transport.tls import TLSError
from tests.retention_store.test_store import (
    AUTHORITY, CLAIM_PROOF, FINISH_PROOF, INSPECTOR, PROFILE_BODY, RECOVER, RUNNER,
    START_PROOF, TENANT, Fixture, recording_binding,
)


DENIAL = "fixture/denial"
OTHER_TENANT = "tenant/denial-control"
OTHER_AUTHORITY = "fixture/other-authority"
ROLES = {
    "authority": (AUTHORITY.principal, TENANT, ("admit",)),
    "runner": (RUNNER.principal, TENANT, ("start", "claim", "upload", "finish")),
    "inspector": (INSPECTOR.principal, TENANT, ("inspect",)),
    "recovery": (RECOVER.principal, TENANT, ("recover",)),
    "denial": (DENIAL, OTHER_TENANT, ("inspect",)),
    "other-authority": (OTHER_AUTHORITY, OTHER_TENANT, ("admit",)),
}


def digest(raw):
    return sha256(raw).hexdigest()


def witness(kind, actor, admission, value):
    if actor.principal != admission["executor_id"]:
        raise ValueError("test witness principal differs from admission")
    if kind == "start" and admission["request_id"] == "fixture/witness/invalid":
        raise ValueError("test witness is invalid")
    if kind == "start" and admission["request_id"] == "fixture/witness/unavailable":
        raise OSError("test witness source is unavailable")
    return {"start": START_PROOF, "claim": CLAIM_PROOF, "finish": FINISH_PROOF}[kind]


def verify_witness(kind, actor, admission, record, evidence):
    return evidence == witness(kind, actor, admission, record)


@cache
def profile_validator():
    return Fixture().profile


def validate_profile(admission, profile, terminal):
    return profile_validator()(admission, profile, terminal)


def crash_worker(config, approval):
    from .__main__ import open_server, read_config

    def fault(label, checkpoint):
        if (label, checkpoint) == ("end", "before_rename"):
            signal.raise_signal(signal.SIGKILL)

    with open_server(read_config(config), read_config(approval), fault=fault):
        signal.pause()


class REAFlow:
    def attempt(self, transport, *, recording=None, finish=True,
                tenant=None, authority_role="authority", abort_before=None):
        tenant = self.tenant if tenant is None else tenant
        attempt = uuid4().hex
        admission, start = self.fixture.admission(
            attempt, request_id="fixture/request/" + attempt, executor=self.runner_principal,
        )
        if abort_before == "admission":
            self.abort_body(transport, authority_role, attempt, abort_before)
        with self.client(transport, authority_role).session(tenant) as authority:
            self.assertEqual(authority.admit(admission), admission)
            self.assertEqual(authority.admit(admission), admission)
        with self.client(transport, "runner").session(tenant) as runner:
            if abort_before == "start":
                self.abort_body(transport, "runner", attempt, abort_before)
            ack = runner.start(attempt, start, request_sha256=admission["request_sha256"])
            if abort_before == "launch-claim":
                self.abort_body(transport, "runner", attempt, abort_before)
            self.assertEqual(runner.claim(ack), ack)
            profile = runner.upload(attempt, "profile", len(PROFILE_BODY), digest(PROFILE_BODY), PROFILE_BODY)
            objects = [dict(profile, role="rea_profile")]
            media = None
            if recording is not None:
                meta = runner.upload(attempt, "recording", len(recording), digest(recording), recording)
                objects.append(dict(meta, role="recording", side="candidate"))
                media = {"candidate": recording_binding(meta["sha256"])}
            artifact = self.fixture.index(admission, objects)
            terminal = self.fixture.terminal(ack, artifact, indexed=True, media=media,
                                             request_id=admission["request_id"])
            if finish:
                if abort_before == "finish":
                    self.abort_body(transport, "runner", attempt, abort_before)
                result = runner.finish(attempt, terminal, artifact, start_sha256=ack["start_sha256"])
                self.assertEqual(runner.finish(attempt, terminal, artifact, start_sha256=ack["start_sha256"]), result)
        return admission, start, ack, terminal, artifact, objects

    def finish(self, transport, attempt_data, *, changed=False):
        admission, _, ack, terminal, artifact, _ = attempt_data
        if changed:
            artifact += b" "
            revised = loads(terminal)
            revised["artifact"].update(bytes=len(artifact), sha256=digest(artifact))
            terminal = record_bytes(revised)
        with self.client(transport, "runner").session(self.tenant) as runner:
            return runner.finish(admission["attempt_id"], terminal, artifact, start_sha256=ack["start_sha256"])

    def assert_status(self, status, action):
        with self.assertRaises(RemoteError) as error:
            action()
        self.assertEqual(error.exception.status, status)

    def readback(self, transport, attempt_data, *, recording=None):
        admission, start, _, terminal, artifact, objects = attempt_data
        attempt = admission["attempt_id"]
        with self.client(transport, "inspector").session(self.tenant) as inspector:
            inspection = inspector.inspect(attempt)
            self.assertEqual(inspection["admission"], admission)
            self.assertEqual(inspection["state"], "committed")
            self.assertEqual(record_bytes(inspection["start"]), start)
            self.assertEqual(record_bytes(inspection["terminal"]), terminal)
            self.assertEqual(inspection["start_sha256"], digest(start))
            self.assertEqual(inspection["terminal_sha256"], digest(terminal))
            self.assertEqual(inspector.receipt(inspection["receipt_ref"]), inspection)
            stored = inspector.artifact(attempt, inspection["artifact_ref"], length=len(artifact),
                                        digest=digest(artifact))
            self.assertEqual(stored, artifact)
            self.assertEqual(loads(stored)["objects"], objects)
            payloads = (PROFILE_BODY,) if recording is None else (PROFILE_BODY, recording)
            for item, expected in zip(objects, payloads, strict=True):
                received = bytearray()
                offsets = range(0, item["bytes"], 1 << 20)
                for offset in offsets:
                    page = inspector.object_page(attempt, item["object_ref"], offset=offset)
                    self.assertEqual((page.total_bytes, page.sha256, page.offset),
                                     (item["bytes"], item["sha256"], offset))
                    received += page.data
                self.assertEqual((len(received), digest(received)), (item["bytes"], item["sha256"]))
                self.assertEqual(received, expected)
        return inspection

class SyntheticREA(REAFlow, TLSFixtures):
    tenant = TENANT
    runner_principal = RUNNER.principal

    def setUp(self):
        super().setUp()
        self.fixture = Fixture()
        self.frozen_dir = os.environ["RETENTION_FROZEN_DIR"]
        self.validator = ContractValidator(self.frozen_dir)
        self.credential_context = {}
        creds, grants = [], []
        for handle, (principal, tenant, verbs) in ROLES.items():
            cert, chain = self.leaf(handle)
            self.credential_context[handle] = self.tls(handle, chain)
            creds.append(dict(fingerprint=cert.fingerprint(hashes.SHA256()).hex(),
                              uri_san="urn:test:" + handle, principal=principal))
            grants.extend(dict(principal=principal, tenant=tenant, verb=verb, scope={}) for verb in verbs)
        grants.extend(dict(principal=RUNNER.principal, tenant=OTHER_TENANT, verb=verb, scope={})
                      for verb in ROLES["runner"][2])
        self.store = self.root / "store"
        self.store.mkdir(mode=0o700)
        self.unix = self.root / "retention.sock"
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            self.port = reserve.getsockname()[1]
        self.endpoints = {
            "unix": Endpoint(unix=self.unix),
            "tcp_tls": Endpoint(ip="127.0.0.1", port=self.port),
        }
        self.acl = self.root / "acl.json"
        self.write(self.acl, dict(revision="fixture/acl", credentials=creds, grants=grants, comparisons=[]))
        self.config = dict(
            unix_socket=str(self.unix), tcp_bind_ip="127.0.0.1", tcp_port=self.port,
            server_dns_name=DNS, server_principal="fixture/server", server_cert_id="fixture/cert",
            provider_module_id="fixture/provider", public_chain=str(self.server_chain),
            trust_mount=str(self.trust), signer_socket=str(self.signer.path), key_handle="server",
            store_root=str(self.store), policy_revision="fixture/policy", acl_revision="fixture/acl",
        )
        policy = self.fixture.policy
        self.approval = dict(
            config=self.config.copy(), unix_uid=os.getuid(), unix_gid=os.getgid(), unix_mode=0o600,
            signer_uid=os.getuid(), library=str(self.library), library_sha256=digest(self.library.read_bytes()),
            chain_sha256=digest(self.server_chain.read_bytes()), client_networks=["127.0.0.1/32"],
            frozen_dir=self.frozen_dir, acl_file=str(self.acl), witness_module=__name__,
            policy=dict(object_bytes=policy.object_bytes, attempt_bytes=policy.attempt_bytes,
                        tenant_bytes=policy.tenant_bytes, snapshot_bytes=policy.snapshot_bytes,
                        retention_seconds=int(policy.retention.total_seconds()),
                        purge_window_seconds=int(policy.purge_window.total_seconds()),
                        snapshot_lifetime_seconds=int(policy.snapshot_lifetime.total_seconds())),
        )
        self.config_file = self.root / "config.json"
        self.approval_file = self.root / "approval.json"
        self.write(self.config_file, self.config)
        self.write(self.approval_file, self.approval)

    @staticmethod
    def write(path, document):
        path.write_text(dumps(document), encoding="ascii")

    def client(self, transport, role):
        return Client(self.credential_context[role], self.endpoints[transport], self.validator)

    def ready(self, transport, role):
        principal, tenant, verbs = ROLES[role]
        with self.client(transport, role).session(tenant) as client:
            for verb in verbs:
                client.ready(verb, principal=principal, policy_revision="fixture/policy", acl_revision="fixture/acl")

    @contextmanager
    def server(self, *, crash=False, local_only=False):
        if crash:
            args = [
                "-c", "from retention_service.server.test_e2e import crash_worker; "
                "import sys; crash_worker(*sys.argv[1:])",
                self.config_file, self.approval_file,
            ]
        else:
            args = ["-m", "retention_service.server", "--config", self.config_file, "--approval", self.approval_file]
            if local_only:
                args.append("--local-only")
        with Popen([sys.executable, "-B", *args], stdout=DEVNULL) as process:
            try:
                deadline = monotonic() + 10
                while not self.unix.exists():
                    if process.poll() is not None or monotonic() >= deadline:
                        self.fail("server did not bind the Unix endpoint")
                    sleep(0.02)
                while True:
                    try:
                        self.ready("unix", "authority")
                    except OSError:
                        if process.poll() is not None or monotonic() >= deadline:
                            self.fail("server did not reach authenticated readiness")
                        sleep(0.02)
                    else:
                        break
                yield process
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        raise

    def abort_body(self, transport, role, attempt, route):
        method = "POST" if route == "launch-claim" else "PUT"
        content = "Content-Type: application/json\r\n"
        if route == "start":
            content = (
                "Content-Type: application/octet-stream\r\n"
                f"Retention-SHA256: {digest(b'{}')}\r\n"
            )
        with self.client(transport, role).session(TENANT) as client:
            deadline = monotonic() + client.timeout
            head = (
                f"{method} /retention/v1/attempts/{attempt}/{route} HTTP/1.1\r\n"
                f"Host: {DNS}\r\nRetention-Tenant: {TENANT}\r\n"
                f"{content}Content-Length: 2\r\n\r\n"
            ).encode("ascii")
            client.tls.send(head + b"{", deadline)
            client.tls.socket.shutdown(socket.SHUT_WR)
            # Wait for the server to handle EOF before probing on a fresh connection.
            try:
                client.tls.read(4096, deadline)
            except TLSError:
                pass
        self.ready(transport, role)

    def denial(self, transport, inspection):
        attempt = inspection["attempt_id"]
        control = self.attempt(transport, tenant=OTHER_TENANT, authority_role="other-authority")[0]
        self.ready(transport, "denial")
        with self.client(transport, "denial").session(TENANT) as denied:
            self.assert_status(404, lambda: denied.inspect(attempt))
            self.assert_status(404, lambda: denied.receipt(inspection["receipt_ref"]))
        with self.client(transport, "denial").session(OTHER_TENANT) as denied:
            self.assert_status(404, lambda: denied.inspect(attempt))
            own = denied.inspect(control["attempt_id"])
            self.assertEqual((own["state"], own["admission"]), ("committed", control))

    def recovery(self, transport, pending):
        admission, start, ack, _, _, _ = pending
        attempt = admission["attempt_id"]
        with self.client(transport, "inspector").session(TENANT) as inspector:
            before = inspector.inspect(attempt)
            self.assertEqual((before["state"], before["terminal"], before["launch_claimed"]), ("unknown", None, True))
            self.assertEqual(record_bytes(before["start"]), start)
        with self.client(transport, "recovery").session(TENANT) as recovery:
            page = recovery.recovery({"limit": 2})
            raw = recovery.recovery_item(page["manifest_item"]).data
            self.assertEqual(digest(raw), page["manifest_sha256"])
            manifest = loads(raw)
            disposition = next(item["disposition"] for item in manifest["attempts"] if item["attempt_id"] == attempt)
            self.assertEqual(disposition, "terminal_commit_interrupted")
            self.assertIsNotNone(page["next"])
            self.assertEqual(recovery.recovery({"limit": 2, "cursor": page["next"]})["snapshot"], page["snapshot"])
        self.assert_status(409, lambda: self.finish(transport, pending, changed=True))
        with self.client(transport, "runner").session(TENANT) as runner:
            self.assert_status(409, lambda: runner.claim(ack))
        self.finish(transport, pending)

    def exercise(self, transport):
        recording = b"synthetic-rea-recording" * 52_000
        with self.server():
            for role in ("authority", "runner", "inspector", "recovery"):
                self.ready(transport, role)
            complete = self.attempt(transport, recording=recording)
            before = self.readback(transport, complete, recording=recording)
            self.assert_status(409, lambda: self.finish(transport, complete, changed=True))
            self.denial(transport, before)
            pending = self.attempt(transport, finish=False)
        with self.server(crash=True) as process:
            with self.assertRaises(TLSError):
                self.finish(transport, pending)
            self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
        with self.server():
            self.assertEqual(self.readback(transport, complete, recording=recording), before)
            self.recovery(transport, pending)
            self.readback(transport, pending)

    def test_unix(self):
        self.exercise("unix")

    def test_tcp_tls(self):
        self.exercise("tcp_tls")

    def test_truncated_write_bodies_leave_store_ready(self):
        for transport in self.endpoints:
            for route in ("admission", "start", "launch-claim", "finish"):
                with self.subTest(transport=transport, route=route), self.server():
                    data = self.attempt(transport, abort_before=route)
                    self.readback(transport, data)

    def test_revoked_upload_discards_staging(self):
        transport = "tcp_tls"
        prefix, suffix = b"a" * 65_536, b"b" * 65_536
        payload = prefix + suffix
        with self.server():
            admission, *_ = self.attempt(transport, finish=False)
            attempt = admission["attempt_id"]
            staging, = self.store.glob(f"tenants/*/attempts/{attempt}/staging")
            original = loads(self.acl.read_bytes())
            revoked = dict(original, revision="fixture/revoked", grants=[
                grant for grant in original["grants"]
                if (grant["principal"], grant["tenant"], grant["verb"]) != (RUNNER.principal, TENANT, "upload")
            ])
            changed = self.acl.with_suffix(".new")

            def chunks():
                yield prefix
                deadline = monotonic() + 3
                while not any(path.stat().st_size > 0 for path in staging.glob(".upload.*/bytes")):
                    if monotonic() >= deadline:
                        self.fail("upload did not write staging bytes before revocation")
                    sleep(0.01)
                self.write(changed, revoked)
                changed.replace(self.acl)
                yield suffix

            with self.client(transport, "runner").session(TENANT) as runner:
                self.assert_status(404, lambda: runner.upload(
                    attempt, "revoked", len(payload), digest(payload), chunks(),
                ))
            self.assertEqual(list(staging.iterdir()), [])
            self.write(changed, original)
            changed.replace(self.acl)
            self.ready(transport, "runner")
            with self.client(transport, "runner").session(TENANT) as runner:
                meta = runner.upload(attempt, "revoked", len(payload), digest(payload), payload)
                self.assertEqual(runner.upload(attempt, "revoked", len(payload), digest(payload), payload), meta)

    def test_witness_failures_return_status_and_leave_store_ready(self):
        for transport in self.endpoints:
            for reason, status in (("invalid", 422), ("unavailable", 503)):
                with self.subTest(transport=transport, reason=reason), self.server():
                    attempt = uuid4().hex
                    admission, start = self.fixture.admission(attempt, request_id=f"fixture/witness/{reason}")
                    with self.client(transport, "authority").session(TENANT) as authority:
                        authority.admit(admission)
                    with self.client(transport, "runner").session(TENANT) as runner:
                        self.assert_status(status, lambda: runner.start(
                            attempt, start, request_sha256=admission["request_sha256"],
                        ))
                    with self.client(transport, "inspector").session(TENANT) as inspector:
                        self.assert_status(404, lambda: inspector.inspect(attempt))
                    self.ready(transport, "runner")
                    self.readback(transport, self.attempt(transport))

    def test_comparison_rechecks_each_source_grant(self):
        transport = "tcp_tls"
        with self.server():
            sources = {side: self.attempt(transport) for side in ("reference", "candidate")}
            source_ids = {side: self.readback(transport, data)["attempt_id"]
                          for side, data in sources.items()}
            attempt = uuid4().hex
            admission, started = self.fixture.compare_admission(attempt, source_ids)
            start = record_bytes(started)
            acl = loads(self.acl.read_bytes())
            with self.client(transport, "authority").session(TENANT) as authority:
                self.assert_status(404, lambda: authority.admit(admission))
            acl["comparisons"].append(dict(
                writer=RUNNER.principal, inspector=INSPECTOR.principal, tenant=TENANT,
                destination={"attempt_id": attempt},
                **{side: {"attempt_id": identity} for side, identity in source_ids.items()},
            ))
            grant = next(rule for rule in acl["grants"] if rule["principal"] == INSPECTOR.principal)
            acl["grants"].remove(grant)
            scoped = [dict(grant, scope={"attempt_id": identity})
                      for identity in (*source_ids.values(), attempt)]
            acl["grants"].extend(scoped)
            changed = self.acl.with_suffix(".new")
            self.write(changed, acl)
            changed.replace(self.acl)
            with self.client(transport, "authority").session(TENANT) as authority:
                self.assertEqual(authority.admit(admission), admission)
            with self.client(transport, "runner").session(TENANT) as runner:
                ack = runner.start(attempt, start, request_sha256=admission["request_sha256"])
                profile = runner.upload(attempt, "profile", len(PROFILE_BODY), digest(PROFILE_BODY), PROFILE_BODY)
                objects = [dict(profile, role="rea_profile")]
                artifact = self.fixture.index(admission, objects)
                terminal = record_bytes(self.fixture.compare_terminal(ack, artifact))
                data = (admission, start, ack, terminal, artifact, objects)
                acl["grants"].remove(scoped[1])
                acl["revision"] = "fixture/revoked"
                self.write(changed, acl)
                changed.replace(self.acl)
                self.assert_status(404, lambda: self.finish(transport, data))
                acl["grants"].append(scoped[1])
                acl["revision"] = "fixture/restored"
                self.write(changed, acl)
                changed.replace(self.acl)
                self.finish(transport, data)
            self.readback(transport, data)

    def test_local_only_requires_matching_approval_and_disables_tcp(self):
        command = [sys.executable, "-B", "-m", "retention_service.server", "--config", self.config_file,
                   "--approval", self.approval_file, "--local-only"]
        self.assertEqual(run(command, stdout=DEVNULL, stderr=DEVNULL, timeout=8).returncode, 1)
        self.approval["config"]["local_only"] = True
        self.write(self.approval_file, self.approval)
        with self.server(local_only=True):
            self.ready("unix", "inspector")
            with self.assertRaises(ConnectionRefusedError):
                self.endpoints["tcp_tls"].connect(1)


if __name__ == "__main__":
    unittest.main()
