"""H06b regression tests: one class per finding."""
from __future__ import annotations

import email.message
import hashlib
import io
import os
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

GATEWAY = Path(__file__).resolve().parents[1]
if str(GATEWAY) not in sys.path:
    sys.path.insert(0, str(GATEWAY))

import app as app_mod  # noqa: E402
from plan_schema import validate_plan  # noqa: E402
from replay_store import ReplayError, ReplayStore, parse_pins  # noqa: E402

_STRIP = {"REPLAY_PINS", "REPLAY_REQUIRE_PIN", "DEMO_TOKEN", "TRUST_PROXY",
          "PLAN_QUOTA_FILE", "KEEPALIVE_IDLE", "MAX_WORKERS", "SOCKET_TIMEOUT"}
_ENV = {k: v for k, v in os.environ.items() if k not in _STRIP}


def _socket_available() -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
        finally:
            s.close()
        return True
    except OSError:
        return False


SOCKETS = _socket_available()
NOSOCK = "loopback sockets unavailable in this sandbox"


class _Client:
    configured = True
    model = "stub-model"

    def generate_plan(self, replay):
        return {"summary": "s", "reasoning": "r",
                "steps": [{"title": "t", "detail": "d", "engine_step_refs": []}]}


class _StubStore:
    def all(self):
        return []

    def default_id(self):
        return "stub-replay"


def _application(**kw):
    with mock.patch.dict(os.environ, _ENV, clear=True):
        return app_mod.Application(_StubStore(), None, _Client(), "",
                                   log_stream=io.StringIO(), quota_file="", **kw)


class _Server:
    def __init__(self, **kw):
        self._env = mock.patch.dict(os.environ, _ENV, clear=True)
        self._env.start()
        try:
            self.server = app_mod.build_server(
                "127.0.0.1", 0, gemini_client=_Client(), log_stream=io.StringIO(),
                quota_file="", **kw)
        except Exception:
            self._env.stop()
            raise
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._env.stop()


def _read_until_eof(sock, limit=6.0):
    end, chunks = time.monotonic() + limit, []
    while time.monotonic() < end:
        sock.settimeout(max(0.05, end - time.monotonic()))
        try:
            data = sock.recv(65536)
        except (socket.timeout, ConnectionResetError):
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def _read_one_response(sock, limit=5.0):
    sock.settimeout(limit)
    buf = b""
    while b"\r\n\r\n" not in buf:
        data = sock.recv(65536)
        if not data:
            return buf
        buf += data
    head, _, rest = buf.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(rest) < length:
        data = sock.recv(65536)
        if not data:
            break
        rest += data
    return head + b"\r\n\r\n" + rest[:length]


SMUGGLED = b"GET /readyz HTTP/1.1\r\nHost: x\r\n\r\n"


@unittest.skipUnless(SOCKETS, NOSOCK)
class KeepAliveDesyncTests(unittest.TestCase):  # finding 1
    def setUp(self):
        self.srv = _Server()

    def tearDown(self):
        self.srv.close()

    def _one_closed(self, request, status):
        with self.srv.connect() as sock:
            sock.sendall(request)
            data = _read_until_eof(sock)
        self.assertEqual(data.count(b"HTTP/1.1 "), 1, data[:400])
        self.assertTrue(data.startswith(b"HTTP/1.1 %d" % status), data[:200])
        self.assertIn(b"connection: close", data.lower())

    def test_post_unknown_path_body_not_parsed_as_request(self):
        self._one_closed(b"POST /nope HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                         b"Content-Length: %d\r\n\r\n" % len(SMUGGLED) + SMUGGLED, 404)

    def test_get_with_body_not_parsed_as_request(self):
        self._one_closed(b"GET /healthz HTTP/1.1\r\nHost: x\r\n"
                         b"Content-Length: %d\r\n\r\n" % len(SMUGGLED) + SMUGGLED, 200)

    def test_415_closes_connection(self):
        self._one_closed(b"POST /api/plan HTTP/1.1\r\nHost: x\r\nContent-Type: text/plain\r\n"
                         b"Content-Length: %d\r\n\r\n" % len(SMUGGLED) + SMUGGLED, 415)


class ClientIpTests(unittest.TestCase):  # finding 2
    @staticmethod
    def _h(*pairs):
        msg = email.message.Message()
        for k, v in pairs:
            msg[k] = v
        return msg

    def test_default_uses_peer_global_bucket(self):
        h = self._h(("X-Forwarded-For", "9.9.9.9"))
        self.assertEqual(app_mod._extract_client_ip(h, "10.0.0.1", trust_proxy=False), "10.0.0.1")

    def test_trust_proxy_uses_last_appended_entry(self):
        h = self._h(("X-Forwarded-For", "6.6.6.6, 203.0.113.7"))
        self.assertEqual(app_mod._extract_client_ip(h, "10.0.0.1", trust_proxy=True), "203.0.113.7")

    def test_multiple_xff_lines_use_last(self):
        h = self._h(("X-Forwarded-For", "203.0.113.7"), ("X-Forwarded-For", "198.51.100.2"))
        self.assertEqual(app_mod._extract_client_ip(h, "10.0.0.1", trust_proxy=True), "198.51.100.2")

    def test_x_real_ip_and_garbage_ignored(self):
        h = self._h(("X-Forwarded-For", "1.2.3.4, not-an-ip"), ("X-Real-IP", "6.6.6.6"))
        self.assertEqual(app_mod._extract_client_ip(h, "10.0.0.1", trust_proxy=True), "10.0.0.1")

    def test_global_scope_reported_and_documented(self):
        application = _application(trust_proxy=False)
        self.assertEqual(application.rate_scope, "global_peer")
        self.assertEqual(application.health()["rate_scope"], "global_peer")
        readme = (GATEWAY / "README.md").read_text(encoding="utf-8")
        self.assertIn("one global bucket", readme)
        self.assertIn("TRUST_PROXY", readme)


@unittest.skipUnless(SOCKETS, NOSOCK)
class BodyTimeoutTests(unittest.TestCase):  # finding 3
    def test_stalled_body_gets_408_and_close(self):
        srv = _Server(socket_timeout=1.0, request_deadline=1.0)
        try:
            with srv.connect() as sock:
                sock.sendall(b"POST /api/plan HTTP/1.1\r\nHost: x\r\n"
                             b"Content-Type: application/json\r\nContent-Length: 40\r\n\r\n{\"rep")
                started = time.monotonic()
                data = _read_until_eof(sock, limit=8.0)
            self.assertTrue(data.startswith(b"HTTP/1.1 408"), data[:200])
            self.assertIn(b"REQUEST_TIMEOUT", data)
            self.assertIn(b"connection: close", data.lower())
            self.assertLess(time.monotonic() - started, 3.0)
        finally:
            srv.close()


class WorkerDefaultsTests(unittest.TestCase):  # finding 4 (no socket)
    def test_default_workers_cover_cloud_run_default_concurrency(self):
        self.assertGreaterEqual(app_mod.DEFAULT_MAX_WORKERS, 80)


@unittest.skipUnless(SOCKETS, NOSOCK)
class WorkerSlotTests(unittest.TestCase):  # finding 4
    def test_idle_keepalive_connection_releases_slot(self):
        srv = _Server(max_workers=1, keepalive_idle=0.3)
        try:
            a = srv.connect()
            try:
                a.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
                self.assertTrue(_read_one_response(a).startswith(b"HTTP/1.1 200"))
                time.sleep(1.0)  # A stays open, idle on keep-alive
                with srv.connect() as b:
                    b.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                    data = _read_until_eof(b)
                self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:200])
            finally:
                a.close()
        finally:
            srv.close()


class PartialMatchTests(unittest.TestCase):  # finding 5
    STEPS = [{"step_id": "s01"}, {"step_id": "s02"}]

    def _step(self, refs):
        raw = {"summary": "s", "reasoning": "r",
               "steps": [{"title": "t", "detail": "d", "engine_step_refs": refs}]}
        return validate_plan(raw, self.STEPS)["steps"][0]

    def test_partial_reports_per_reference_membership(self):
        step = self._step(["s01", "s99"])
        self.assertEqual(step["match"], "partial")
        self.assertEqual(step["ref_matches"],
                         [{"id": "s01", "exists": True}, {"id": "s99", "exists": False}])

    def test_matched_requires_full_membership(self):
        self.assertEqual(self._step(["s01", "s02"])["match"], "matched")
        self.assertEqual(self._step(["s99"])["match"], "unmatched")

    def test_renderer_labels_partial_distinctly(self):
        js = (GATEWAY / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('partial: "some referenced IDs have no matching execution"', js)
        self.assertIn("ref.exists === true", js)
        self.assertEqual(js.count("ID exists; support unverified"), 1)


class AttestationTests(unittest.TestCase):  # finding 6
    def _store(self, **kw):
        return ReplayStore(GATEWAY / "replay", GATEWAY / "static", **kw)

    def test_unpinned_is_self_attested(self):
        for replay in self._store().all():
            self.assertEqual(replay.attestation, "self-attested")
            self.assertEqual(ReplayStore.public_summary(replay)["attestation"], "self-attested")

    def test_matching_external_pin_marks_pinned(self):
        first = self._store().all()[0]
        digest = hashlib.sha256(first.path.read_bytes()).hexdigest()
        store = self._store(pins={first.replay_id: digest})
        self.assertEqual(store.get(first.replay_id).attestation, "pinned")

    def test_mismatching_pin_refused(self):
        first = self._store().all()[0]
        with self.assertRaises(ReplayError):
            self._store(pins={first.replay_id: "0" * 64})

    def test_require_pin_refuses_unpinned(self):
        with self.assertRaises(ReplayError):
            self._store(require_pin=True)

    def test_malformed_pins_fail_closed(self):
        for text in ("noequals", "bad id=" + "0" * 64, "abc=XYZ"):
            with self.assertRaises(ReplayError):
                parse_pins(text)
        self.assertEqual(parse_pins(""), {})

    def test_ui_marks_unpinned_hash_self_attested(self):
        js = (GATEWAY / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("self-attested, not an attestation", js)


class DemoTokenTests(unittest.TestCase):  # finding 7 (LOW)
    def test_non_ascii_token_is_401_not_typeerror(self):
        application = _application(demo_token="secret")
        with self.assertRaises(app_mod.GatewayError) as ctx:
            application._admit("203.0.113.7", "s\u00e9cr\u00e9t\u00ff")
        self.assertEqual(int(ctx.exception.status), 401)
        self.assertEqual(application.inflight_count(), 0)

    def test_correct_token_still_admits(self):
        application = _application(demo_token="secret")
        application._admit("203.0.113.7", "secret")
        self.assertEqual(application.inflight_count(), 1)
        application._release_admission()

    @unittest.skipUnless(SOCKETS, NOSOCK)
    def test_non_ascii_header_over_http_is_401(self):
        srv = _Server(demo_token="secret")
        try:
            with srv.connect() as sock:
                sock.sendall(b"POST /api/plan HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                             b"Content-Length: 2\r\nX-Demo-Token: \xc3\xa9\xff\r\n"
                             b"Connection: close\r\n\r\n{}")
                data = _read_until_eof(sock)
            self.assertTrue(data.startswith(b"HTTP/1.1 401"), data[:200])
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
