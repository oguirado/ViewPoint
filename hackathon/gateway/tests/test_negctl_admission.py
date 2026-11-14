# Negative-control admission tests for hackathon/gateway/tests/test_negctl_admission.py
#
# Package items this file relies on:
#   hackathon/gateway/app.py: Application._admit, Application.plan, Application.begin_shutdown,
#       Application.inflight_count, Application._release_admission, _extract_client_ip,
#       build_server, GatewayError, MAX_REQUEST_BYTES (8192), error codes DEMO_TOKEN_REQUIRED,
#       DAILY_CAP_EXCEEDED, RATE_LIMITED, PLAN_BUSY, SHUTTING_DOWN, UNSUPPORTED_MEDIA_TYPE,
#       LENGTH_REQUIRED, REQUEST_TOO_LARGE, BAD_REQUEST, REPLAY_NOT_FOUND
#   hackathon/gateway/gemini_client.py: GeminiCallError (code GEMINI_PROVIDER_ERROR)
#   hackathon/gateway/replay_store.py: ReplayStore, ReplayNotFound
#   hackathon/gateway/plan_schema.py: validate_plan (exercised through Application.plan)
#   hackathon/gateway/tests/test_h06b_fixes.py: import/sys.path setup copied from there
#   Env vars: DEMO_TOKEN, TRUST_PROXY, PLAN_QUOTA_FILE, REPLAY_PINS, REPLAY_REQUIRE_PIN,
#       PLAN_DAILY_CAP, PLAN_RATE_CAPACITY, PLAN_RATE_REFILL, PLAN_MAX_INFLIGHT
#
# NOTE (not asserted): _read_json_body checks Content-Type with a substring test
# (`"application/json" in content_type.lower()`), so a value such as
# "text/plain; x=application/json" is accepted. No test here depends on that.
from __future__ import annotations

import email.message
import io
import json
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
from gemini_client import GeminiCallError  # noqa: E402
from replay_store import ReplayNotFound, ReplayStore  # noqa: E402

_STRIP = {"REPLAY_PINS", "REPLAY_REQUIRE_PIN", "DEMO_TOKEN", "TRUST_PROXY",
          "PLAN_QUOTA_FILE", "KEEPALIVE_IDLE", "MAX_WORKERS", "SOCKET_TIMEOUT",
          "PLAN_DAILY_CAP", "PLAN_RATE_CAPACITY", "PLAN_RATE_REFILL",
          "PLAN_MAX_INFLIGHT", "DEFAULT_REPLAY_ID", "DRAIN_TIMEOUT"}
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
IP_A = "203.0.113.1"
IP_B = "203.0.113.2"


class CountingClient:
    """Stub Gemini client that records every billed call."""

    configured = True
    model = "stub-model"

    def __init__(self, error=None):
        self.calls = 0
        self.error = error
        self._lock = threading.Lock()

    def generate_plan(self, replay):
        with self._lock:
            self.calls += 1
        if self.error is not None:
            raise self.error
        return {"summary": "s", "reasoning": "r",
                "steps": [{"title": "t", "detail": "d", "engine_step_refs": []}]}


def _app(client, **kw):
    kw.setdefault("rate_capacity", 1000.0)
    kw.setdefault("rate_refill_per_sec", 1000.0)
    kw.setdefault("daily_cap", 1000)
    kw.setdefault("max_inflight", 8)
    with mock.patch.dict(os.environ, _ENV, clear=True):
        store = ReplayStore(GATEWAY / "replay", GATEWAY / "static")
        return app_mod.Application(store, None, client, "",
                                   log_stream=io.StringIO(), quota_file="", **kw)


def _headers(*pairs):
    msg = email.message.Message()
    for key, value in pairs:
        msg[key] = value
    return msg


class _GatewayServer:
    def __init__(self, client, **kw):
        self._env = mock.patch.dict(os.environ, _ENV, clear=True)
        self._env.start()
        try:
            self.server = app_mod.build_server(
                "127.0.0.1", 0, gemini_client=client, log_stream=io.StringIO(),
                quota_file="", **kw)
        except Exception:
            self._env.stop()
            raise
        self.port = self.server.server_address[1]
        self.application = self.server.application
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()

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


def _parse(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ", 2)[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.strip().lower().decode("latin-1")] = value.strip().decode("latin-1")
    return status, headers, body


def _request(port, headers, body=b""):
    lines = [b"POST /api/plan HTTP/1.1", b"Host: x"] + list(headers) + [b"Connection: close"]
    data = b"\r\n".join(lines) + b"\r\n\r\n" + body
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(data)
        raw = _read_until_eof(sock)
    return _parse(raw)


def _json_post(port, payload, *extra):
    return _request(
        port,
        [b"Content-Type: application/json", b"Content-Length: %d" % len(payload), *extra],
        payload,
    )


def _code(body):
    return json.loads(body.decode("utf-8"))["error"]["code"]


class _Base(unittest.TestCase):
    def refused(self, fn, *args, **kwargs):
        with self.assertRaises(app_mod.GatewayError) as ctx:
            fn(*args, **kwargs)
        return ctx.exception

    def assert_untouched(self, application, client):
        self.assertEqual(client.calls, 0)
        self.assertEqual(application._daily_counts, {})
        self.assertEqual(application._buckets, {})
        self.assertEqual(application.inflight_count(), 0)


class AdmissionUnitTests(_Base):
    def test_token_missing_empty_or_inexact_refused_and_open_mode_control(self):
        # Protects _admit(): the `if self.demo_token:` block and its
        # `hmac.compare_digest(...)` -> 401 DEMO_TOKEN_REQUIRED branch.
        client = CountingClient()
        application = _app(client, demo_token="secret")
        for supplied in (None, "", "wrong", "secre", "secret1", "SECRET", " secret"):
            with self.subTest(supplied=supplied):
                exc = self.refused(application.plan, None, client_ip=IP_A, demo_token=supplied)
                self.assertEqual(int(exc.status), 401)
                self.assertEqual(exc.code, "DEMO_TOKEN_REQUIRED")
        # Refusals commit nothing: no daily count, no bucket, no slot, no billed call.
        self.assert_untouched(application, client)
        # Control: with no token configured the same missing token is admitted.
        open_client = CountingClient()
        open_app = _app(open_client)
        self.assertTrue(open_app.plan(None, client_ip=IP_A, demo_token=None)["ok"])
        self.assertEqual(open_client.calls, 1)

    def test_daily_cap_boundary_refuses_next_and_burns_nothing(self):
        # Protects _admit(): `if used >= self.daily_cap` -> 429 DAILY_CAP_EXCEEDED,
        # which must sit before the bucket/daily commit lines.
        client = CountingClient()
        application = _app(client, daily_cap=2)
        application.plan(None, client_ip=IP_A)
        application.plan(None, client_ip=IP_A)
        bucket_before = application._buckets[IP_A]
        exc = self.refused(application.plan, None, client_ip=IP_A)
        self.assertEqual(int(exc.status), 429)
        self.assertEqual(exc.code, "DAILY_CAP_EXCEEDED")
        self.assertEqual(exc.retry_after, 3600)
        self.assertEqual(client.calls, 2)
        self.assertEqual(sum(application._daily_counts.values()), 2)
        self.assertEqual(application._buckets[IP_A], bucket_before)
        self.assertEqual(application.inflight_count(), 0)

    def test_rate_bucket_exhaustion_is_per_key_and_not_counted_as_daily_use(self):
        # Protects _admit(): `if tokens < 1.0` -> 429 RATE_LIMITED and the
        # per-key lookup `self._buckets.get(client_ip ...)`.
        client = CountingClient()
        application = _app(client, rate_capacity=2.0, rate_refill_per_sec=1e-9)
        application.plan(None, client_ip=IP_A)
        application.plan(None, client_ip=IP_A)
        exc = self.refused(application.plan, None, client_ip=IP_A)
        self.assertEqual(int(exc.status), 429)
        self.assertEqual(exc.code, "RATE_LIMITED")
        self.assertGreaterEqual(exc.retry_after, 1)
        self.assertEqual(client.calls, 2)
        self.assertEqual(sum(application._daily_counts.values()), 2)
        application.plan(None, client_ip=IP_B)  # a different key is unaffected
        self.assertEqual(client.calls, 3)

    def test_bucket_refill_boundary_and_capacity_clamp(self):
        # Protects _admit(): `tokens = min(self.rate_capacity, tokens +
        # (now - last) * self.rate_refill_per_sec)`.
        client = CountingClient()
        application = _app(client, rate_capacity=3.0, rate_refill_per_sec=1.0)
        application._buckets[IP_A] = (0.0, time.monotonic() - 0.1)  # ~0.1 token
        exc = self.refused(application.plan, None, client_ip=IP_A)
        self.assertEqual(exc.code, "RATE_LIMITED")
        self.assertTrue(1 <= exc.retry_after <= 2)
        self.assertEqual(client.calls, 0)
        application._buckets[IP_A] = (0.0, time.monotonic() - 100.0)  # long idle
        application.plan(None, client_ip=IP_A)
        self.assertEqual(client.calls, 1)
        # Clamped to capacity (3.0) before the one-token charge.
        self.assertAlmostEqual(application._buckets[IP_A][0], 2.0, places=2)

    def test_inflight_cap_refuses_without_commit_or_stealing_the_slot(self):
        # Protects _admit(): `if self._inflight_n >= self.max_inflight` -> 429
        # PLAN_BUSY, placed before the commit lines.
        client = CountingClient()
        application = _app(client, max_inflight=1)
        application._admit(IP_A, None)  # holds the only slot
        bucket_before = application._buckets[IP_A]
        counts_before = dict(application._daily_counts)
        exc = self.refused(application._admit, IP_A, None)
        self.assertEqual(int(exc.status), 429)
        self.assertEqual(exc.code, "PLAN_BUSY")
        self.assertEqual(exc.retry_after, 1)
        self.assertEqual(application._buckets[IP_A], bucket_before)
        self.assertEqual(application._daily_counts, counts_before)
        exc = self.refused(application.plan, None, client_ip=IP_A)
        self.assertEqual(exc.code, "PLAN_BUSY")
        self.assertEqual(client.calls, 0)
        # The refused request must not release the slot it never held.
        self.assertEqual(application.inflight_count(), 1)
        application._release_admission()
        self.assertEqual(application.inflight_count(), 0)
        self.assertTrue(application.plan(None, client_ip=IP_A)["ok"])
        self.assertEqual(client.calls, 1)

    def test_slot_released_when_provider_fails(self):
        # Protects plan(): the `finally: self._release_admission()` block.
        for error in (GeminiCallError("boom"), RuntimeError("unexpected")):
            with self.subTest(error=type(error).__name__):
                client = CountingClient(error=error)
                application = _app(client, max_inflight=1)
                exc = self.refused(application.plan, None, client_ip=IP_A)
                self.assertEqual(int(exc.status), 502)
                self.assertEqual(client.calls, 1)
                self.assertEqual(application.inflight_count(), 0)
                client.error = None  # the single slot must be free again
                self.assertTrue(application.plan(None, client_ip=IP_A)["ok"])
                self.assertEqual(client.calls, 2)

    def test_unknown_or_empty_replay_id_never_consumes_admission(self):
        # Protects plan(): `replay = self.store.get(replay_id)` placed before
        # `self._admit(...)`, and the `replay_id == ""` -> 400 branch.
        client = CountingClient()
        application = _app(client)
        for bad in ("zz-negctl-missing-replay", "Bad ID!", "../etc/passwd"):
            with self.subTest(replay_id=bad):
                with self.assertRaises(ReplayNotFound):
                    application.plan(bad, client_ip=IP_A)
        exc = self.refused(application.plan, "", client_ip=IP_A)
        self.assertEqual(int(exc.status), 400)
        self.assertEqual(exc.code, "BAD_REQUEST")
        self.assert_untouched(application, client)

    def test_shutdown_refuses_with_503_before_any_billed_call(self):
        # Protects _admit(): `if not self._accepting` -> 503 SHUTTING_DOWN.
        client = CountingClient()
        application = _app(client)
        application.begin_shutdown()
        self.assertFalse(application.is_accepting())
        exc = self.refused(application.plan, None, client_ip=IP_A)
        self.assertEqual(int(exc.status), 503)
        self.assertEqual(exc.code, "SHUTTING_DOWN")
        self.assertEqual(exc.retry_after, 5)
        self.assert_untouched(application, client)

    def test_extract_client_ip_fallbacks_and_normalisation(self):
        # Protects _extract_client_ip(): the `_valid_ip` fallbacks to peer_ip /
        # "unknown" and the `str(ipaddress.ip_address(...))` normalisation.
        peer = "10.0.0.1"
        self.assertEqual(app_mod._extract_client_ip(_headers(), peer, trust_proxy=True), peer)
        self.assertEqual(app_mod._extract_client_ip(None, peer, trust_proxy=True), peer)
        for empty in ("", "   "):
            h = _headers(("X-Forwarded-For", empty))
            self.assertEqual(app_mod._extract_client_ip(h, peer, trust_proxy=True), peer)
        self.assertEqual(app_mod._extract_client_ip(None, "", trust_proxy=False), "unknown")
        self.assertEqual(app_mod._extract_client_ip(_headers(), "", trust_proxy=True), "unknown")
        # Case/zero-padding variants of one address map to the same bucket key.
        a = _headers(("X-Forwarded-For", "2001:DB8:0:0:0:0:0:1"))
        b = _headers(("X-Forwarded-For", "2001:db8::1"))
        self.assertEqual(app_mod._extract_client_ip(a, peer, trust_proxy=True), "2001:db8::1")
        self.assertEqual(app_mod._extract_client_ip(b, peer, trust_proxy=True), "2001:db8::1")


@unittest.skipUnless(SOCKETS, NOSOCK)
class AdmissionHttpTests(_Base):
    def _server(self, client, **kw):
        srv = _GatewayServer(client, **kw)
        self.addCleanup(srv.close)
        return srv

    def test_spoofed_xff_ignored_without_trust_proxy(self):
        # Protects _extract_client_ip(): `if not trust_proxy or headers is None`
        # -> peer. Rotating X-Forwarded-For must not mint new buckets.
        client = CountingClient()
        srv = self._server(client, rate_capacity=1.0, rate_refill_per_sec=1e-9,
                           trust_proxy=False)
        status, _, _ = _json_post(srv.port, b"{}", b"X-Forwarded-For: 198.51.100.1")
        self.assertEqual(status, 200)
        status, headers, body = _json_post(srv.port, b"{}", b"X-Forwarded-For: 198.51.100.2")
        self.assertEqual(status, 429)
        self.assertEqual(_code(body), "RATE_LIMITED")
        self.assertGreaterEqual(int(headers["retry-after"]), 1)
        self.assertEqual(client.calls, 1)

    def test_trusted_proxy_buckets_on_last_hop_not_client_prefix(self):
        # Protects _extract_client_ip(): `.split(",")[-1]` (last hop only).
        client = CountingClient()
        srv = self._server(client, rate_capacity=1.0, rate_refill_per_sec=1e-9,
                           trust_proxy=True)
        status, _, _ = _json_post(srv.port, b"{}", b"X-Forwarded-For: 1.1.1.1, 203.0.113.9")
        self.assertEqual(status, 200)
        # Same last hop, different client-controlled prefix: same bucket.
        status, _, body = _json_post(srv.port, b"{}", b"X-Forwarded-For: 2.2.2.2, 203.0.113.9")
        self.assertEqual(status, 429)
        self.assertEqual(_code(body), "RATE_LIMITED")
        # Different last hop: separate bucket, admitted.
        status, _, _ = _json_post(srv.port, b"{}", b"X-Forwarded-For: 2.2.2.2, 203.0.113.10")
        self.assertEqual(status, 200)
        self.assertEqual(client.calls, 2)

    def test_body_size_boundary_8192_accepted_8193_refused(self):
        # Protects _read_json_body(): `if length > MAX_REQUEST_BYTES` -> 413.
        client = CountingClient()
        srv = self._server(client)
        prefix = b'{"replay_id":"x"}'
        at_limit = prefix + b" " * (app_mod.MAX_REQUEST_BYTES - len(prefix))
        self.assertEqual(len(at_limit), app_mod.MAX_REQUEST_BYTES)
        status, _, body = _json_post(srv.port, at_limit)
        self.assertEqual(status, 404)  # passed the size gate, unknown replay id
        self.assertEqual(_code(body), "REPLAY_NOT_FOUND")
        status, headers, body = _request(
            srv.port,
            [b"Content-Type: application/json",
             b"Content-Length: %d" % (app_mod.MAX_REQUEST_BYTES + 1)],
        )
        self.assertEqual(status, 413)
        self.assertEqual(_code(body), "REQUEST_TOO_LARGE")
        self.assertEqual(headers.get("connection", "").lower(), "close")
        self.assertEqual(client.calls, 0)
        self.assertEqual(srv.application._daily_counts, {})

    def test_malformed_content_length_variants_refused(self):
        # Protects _read_json_body(): the chunked, duplicate Content-Length,
        # missing Content-Length and negative/non-numeric length branches.
        client = CountingClient()
        srv = self._server(client)
        ctype = b"Content-Type: application/json"
        cases = [
            ("missing", [ctype], 411, "LENGTH_REQUIRED"),
            ("negative", [ctype, b"Content-Length: -1"], 400, "BAD_REQUEST"),
            ("non-numeric", [ctype, b"Content-Length: abc"], 400, "BAD_REQUEST"),
            ("duplicate", [ctype, b"Content-Length: 2", b"Content-Length: 2"], 400, "BAD_REQUEST"),
            ("chunked", [ctype, b"Transfer-Encoding: chunked"], 400, "BAD_REQUEST"),
        ]
        for name, headers, want_status, want_code in cases:
            with self.subTest(case=name):
                status, _, body = _request(srv.port, headers)
                self.assertEqual(status, want_status)
                self.assertEqual(_code(body), want_code)
        self.assertEqual(client.calls, 0)
        self.assertEqual(srv.application._daily_counts, {})
        self.assertEqual(srv.application.inflight_count(), 0)

    def test_missing_or_non_json_content_type_refused_before_admission(self):
        # Protects _read_json_body(): the `"application/json" in content_type`
        # check -> 415, which runs before any admission accounting.
        client = CountingClient()
        srv = self._server(client)
        variants = [
            [],  # header absent entirely
            [b"Content-Type: text/html"],
            [b"Content-Type: multipart/form-data; boundary=x"],
        ]
        for extra in variants:
            with self.subTest(extra=extra):
                status, _, body = _request(srv.port, extra + [b"Content-Length: 0"])
                self.assertEqual(status, 415)
                self.assertEqual(_code(body), "UNSUPPORTED_MEDIA_TYPE")
        self.assertEqual(client.calls, 0)
        self.assertEqual(srv.application._daily_counts, {})
        self.assertEqual(srv.application._buckets, {})

    def test_malformed_body_shapes_refused_without_billing(self):
        # Protects _read_json_body() (JSON decode / dict checks) and _route_post():
        # the unknown-field set difference and the replay_id type/empty checks.
        client = CountingClient()
        srv = self._server(client)
        bodies = [
            b"{nope",
            b"[]",
            b'"str"',
            b"\xff\xfe",
            b'{"replay_id":123}',
            b'{"replay_id":""}',
            b'{"replay_id":"zz-x","extra":1}',
            b'{"other":1}',
        ]
        for payload in bodies:
            with self.subTest(payload=payload):
                status, _, body = _json_post(srv.port, payload)
                self.assertEqual(status, 400)
                self.assertEqual(_code(body), "BAD_REQUEST")
        self.assertEqual(client.calls, 0)
        self.assertEqual(srv.application._daily_counts, {})
        self.assertEqual(srv.application._buckets, {})
        self.assertEqual(srv.application.inflight_count(), 0)


if __name__ == "__main__":
    unittest.main()
