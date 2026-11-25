"""Exercise the default profile using temporary storage and synthetic REA evidence."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import socket
from pathlib import Path
import stat
from subprocess import DEVNULL, Popen, run
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from uuid import uuid4

from ..contracts.validation import ContractError, ContractValidator
from ..contracts.wire import DENIAL_RESPONSE, record_bytes
from ..transport.client import Client, Endpoint
from ..transport.tls import TLSError
from .__main__ import open_local_server
from .local import APP_DIR, DEFAULTS_FILE, FROZEN_DIR, WRITE_VERBS, load_local
from .test_e2e import Fixture, PROFILE_BODY, REAFlow, digest


COMMAND = [sys.executable, "-B", "-m", "retention_service.server"]
WITNESS_MODULE = "retention_service.server.test_e2e"
TRANSPORT = "unix_local_open"


class LocalREA(REAFlow, unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.state = self.root / "state"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        environment = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.state), "XDG_RUNTIME_DIR": str(self.runtime),
            "RETENTION_FROZEN_DIR": str(FROZEN_DIR),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.fixture = Fixture()
        self.validator = ContractValidator(FROZEN_DIR)
        self.primary = f"local/uid/{os.geteuid()}"
        self.tenant = self.primary
        self.control_tenant = f"{self.primary}/control"
        self.runner_principal = f"{self.primary}/writer"
        self.endpoints = {
            role: Endpoint(unix=self.runtime / APP_DIR / name)
            for role, name in (("writer", "retention.sock"), ("inspector", "inspect.sock"), ("denial", "denial.sock"))
        }

    def client(self, transport, role):
        endpoint = role if role in {"inspector", "denial"} else "writer"
        return Client(None, self.endpoints[endpoint], self.validator)

    def ready(self, transport, role):
        tenant = self.control_tenant if role == "denial" else self.tenant
        identity = role if role in {"inspector", "denial"} else "writer"
        verbs = {"writer": WRITE_VERBS + ("hold", "recover"), "inspector": ("inspect",), "denial": ("inspect",)}
        with self.client(transport, role).session(tenant) as client:
            for verb in verbs[role]:
                client.ready(verb, principal=f"{self.primary}/{identity}",
                             policy_revision="local-policy/v1", acl_revision="local-acl/v1")

    @contextmanager
    def server(self, *, witnesses=True):
        args = ["--witness-module", WITNESS_MODULE] if witnesses else []
        with Popen([*COMMAND, *args], stdout=DEVNULL) as process:
            try:
                deadline = monotonic() + 10
                while True:
                    try:
                        self.ready(TRANSPORT, "inspector")
                    except OSError:
                        if process.poll() is not None or monotonic() >= deadline:
                            self.fail("local service failed to reach authenticated readiness")
                        sleep(0.02)
                    else:
                        break
                yield process
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)

    def test_unix_local_open(self):
        recording = b"synthetic-rea-recording" * 52_000
        with self.server():
            self.ready(TRANSPORT, "writer")
            reference = self.attempt(TRANSPORT, recording=recording)
            before = self.readback(TRANSPORT, reference, recording=recording)
            candidate = self.attempt(TRANSPORT)
            self.readback(TRANSPORT, candidate)
            self.assert_status(409, lambda: self.finish(TRANSPORT, reference, changed=True))
            attempt = uuid4().hex
            source_ids = {"reference": reference[0]["attempt_id"], "candidate": candidate[0]["attempt_id"]}
            admission, started = self.fixture.compare_admission(attempt, source_ids, executor=self.runner_principal)
            raw = record_bytes(started)
            with self.client(TRANSPORT, "writer").session(self.tenant) as writer:
                self.assert_status(404, lambda: writer.inspect(reference[0]["attempt_id"]))
                writer.admit(admission)
                ack = writer.start(attempt, raw, request_sha256=admission["request_sha256"])
                profile = writer.upload(attempt, "profile", len(PROFILE_BODY), digest(PROFILE_BODY), PROFILE_BODY)
                objects = [dict(profile, role="rea_profile")]
                artifact = self.fixture.index(admission, objects)
                terminal = record_bytes(self.fixture.compare_terminal(ack, artifact))
                writer.finish(attempt, terminal, artifact, start_sha256=ack["start_sha256"])
                writer.hold(attempt, "local-control")
                inventory = writer.recovery({"limit": 10})
                manifest = writer.recovery_item(inventory["manifest_item"]).data
                self.assertEqual(digest(manifest), inventory["manifest_sha256"])
                self.assertIn(b"local-control", manifest)
                writer.hold(attempt, "local-control", release=True)
            comparison = (admission, raw, ack, terminal, artifact, objects)
            self.readback(TRANSPORT, comparison)
            pending = self.attempt(TRANSPORT, finish=False)
        with self.server():
            self.assertEqual(self.readback(TRANSPORT, reference, recording=recording), before)
            self.readback(TRANSPORT, comparison)
            with self.client(TRANSPORT, "inspector").session(self.tenant) as inspector:
                unknown = inspector.inspect(pending[0]["attempt_id"])
                self.assertEqual((unknown["state"], unknown["launch_claimed"], unknown["terminal"]),
                                 ("unknown", True, None))
            with self.client(TRANSPORT, "writer").session(self.tenant) as writer:
                self.assert_status(409, lambda: writer.claim(pending[2]))
            self.finish(TRANSPORT, pending)
            self.readback(TRANSPORT, pending)

    def test_denial_tenant_has_a_positive_control_and_constant_cross_tenant_denial(self):
        with self.server():
            data = self.attempt(TRANSPORT)
            private = self.readback(TRANSPORT, data)
            control = self.attempt(TRANSPORT, tenant=self.control_tenant)
            self.ready(TRANSPORT, "denial")
            with self.client(TRANSPORT, "denial").session(self.control_tenant) as allowed:
                visible = allowed.inspect(control[0]["attempt_id"])
                self.assertEqual(visible["admission"], control[0])
                self.assertEqual(allowed.receipt(visible["receipt_ref"]), visible)
                self.assertEqual(allowed.artifact(visible["attempt_id"], visible["artifact_ref"],
                                                 length=len(control[4]), digest=digest(control[4])), control[4])
                self.assertEqual(allowed.object_page(visible["attempt_id"], control[5][0]["object_ref"]).data,
                                 PROFILE_BODY)

            def raw_denial(tenant, path):
                with self.endpoints["denial"].connect(2) as sock:
                    sock.sendall(f"GET /retention/v1/{path} HTTP/1.1\r\nHost: localhost\r\n"
                                 f"Retention-Tenant: {tenant}\r\nConnection: close\r\n\r\n".encode("ascii"))
                    with sock.makefile("rb") as stream:
                        return stream.read()

            attempt = data[0]["attempt_id"]
            paths = (f"attempts/{attempt}/inspect", f"attempts/{uuid4().hex}/inspect",
                     f"receipts/{private['receipt_ref']}",
                     f"attempts/{attempt}/artifacts/{private['artifact_ref']}",
                     f"attempts/{attempt}/objects/{data[5][0]['object_ref']}?offset=98765")
            for tenant in (self.tenant, self.control_tenant):
                for path in paths:
                    self.assertEqual(raw_denial(tenant, path), DENIAL_RESPONSE)

    def test_kernel_peer_uid_must_match_before_http(self):
        with open_local_server(witness_module=WITNESS_MODULE) as server:
            self.ready(TRANSPORT, "inspector")
            uid, gid, mode = server.unix_owner
            # Keep the real kernel peer credentials and change the allowed UID.
            with patch.object(server, "unix_owner", (uid + 1, gid, mode)):
                with self.assertRaises(TLSError):
                    self.ready(TRANSPORT, "inspector")
            self.ready(TRANSPORT, "inspector")

    def test_first_run_is_race_safe_and_restart_keeps_defaults(self):
        barrier = Barrier(8)

        def bootstrap():
            barrier.wait()
            return load_local()

        with ThreadPoolExecutor(max_workers=8) as workers:
            deployments = list(workers.map(lambda _: bootstrap(), range(8)))
        self.assertEqual(deployments, [deployments[0]] * len(deployments))
        settings = self.state / APP_DIR / DEFAULTS_FILE[0]
        old = settings.stat()
        contents = settings.read_bytes()
        for _ in range(2):
            with self.server():
                for endpoint in self.endpoints.values():
                    info = endpoint.unix.stat()
                    self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)),
                                     (os.geteuid(), os.getegid(), 0o600))
                rejected = run(COMMAND, stdout=DEVNULL, stderr=DEVNULL, timeout=8)
                self.assertEqual(rejected.returncode, 1)
                self.ready(TRANSPORT, "inspector")
        self.assertEqual((settings.stat().st_ino, settings.stat().st_mtime_ns), (old.st_ino, old.st_mtime_ns))
        self.assertEqual(settings.read_bytes(), contents)
        settings.write_bytes(b"{}")
        with self.assertRaises(ContractError):
            load_local()

    def test_unconfigured_cli_uses_packaged_pins_and_fails_closed_for_writes(self):
        with self.server(witnesses=False):
            with self.client(TRANSPORT, "writer").session(self.tenant) as writer:
                self.assert_status(503, lambda: writer.ready(
                    "admit", principal=self.runner_principal, policy_revision="local-policy/v1",
                    acl_revision="local-acl/v1",
                ))
            with self.client(TRANSPORT, "writer").session(self.tenant) as writer:
                admission, _ = self.fixture.admission(uuid4().hex, executor=self.runner_principal)
                self.assert_status(503, lambda: writer.admit(admission))
        self.assertEqual(list((self.state / APP_DIR / "store" / "tenants").iterdir()), [])

    def test_network_requires_explicit_tls_opt_in(self):
        config = self.root / "network.json"
        config.write_text('{"tcp_bind_ip":"192.0.2.1","tcp_port":443}')
        result = run([*COMMAND, "--config", config], capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"TLS opt-in requires both", result.stderr)
        with self.assertRaises(ValueError):
            Client(None, Endpoint(ip="127.0.0.1", port=443), self.validator)

    def test_lost_claim_reply_cannot_authorize_a_second_launch(self):
        with self.server():
            attempt = uuid4().hex
            admission, start = self.fixture.admission(attempt, executor=self.runner_principal)
            with self.client(TRANSPORT, "writer").session(self.tenant) as writer:
                writer.admit(admission)
                ack = writer.start(attempt, start, request_sha256=admission["request_sha256"])
            body = record_bytes(ack)
            with self.endpoints["writer"].connect(2) as sock:
                head = (f"POST /retention/v1/attempts/{attempt}/launch-claim HTTP/1.1\r\n"
                        f"Host: localhost\r\nRetention-Tenant: {self.tenant}\r\n"
                        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n")
                sock.sendall(head.encode("ascii") + body)
                sock.shutdown(socket.SHUT_RD)
                with self.client(TRANSPORT, "inspector").session(self.tenant) as inspector:
                    deadline = monotonic() + 5
                    while not (view := inspector.inspect(attempt))["launch_claimed"]:
                        if monotonic() >= deadline:
                            self.fail("the detached claim was not published")
                        sleep(0.01)
                    self.assertEqual((view["state"], view["terminal"]), ("unknown", None))
                with self.client(TRANSPORT, "writer").session(self.tenant) as writer:
                    self.assert_status(409, lambda: writer.claim(ack))

    def test_socket_directory_falls_back_to_private_state(self):
        del os.environ["XDG_RUNTIME_DIR"]
        deployment = load_local()
        self.assertEqual(deployment.runtime, self.state / APP_DIR / "run")
        with open_local_server():
            with Client(None, Endpoint(unix=deployment.socket_path("inspector")), self.validator).session(
                self.tenant
            ) as reader:
                reader.ready("inspect", principal=f"{self.primary}/inspector",
                             policy_revision="local-policy/v1", acl_revision="local-acl/v1")

    def test_unsafe_runtime_and_occupied_paths_fail_closed(self):
        self.runtime.chmod(0o777)
        with self.assertRaises(ContractError):
            load_local()
        self.runtime.chmod(0o700)
        deployment = load_local()
        path = deployment.socket_path("writer")
        path.write_bytes(b"keep")
        with self.assertRaises(ContractError), open_local_server():
            self.fail("accepted a file in place of the Unix socket")
        self.assertEqual(path.read_bytes(), b"keep")
