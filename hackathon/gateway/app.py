#!/usr/bin/env python3
"""ViewPoint Cloud Run gateway — replay page plus server-side Gemini plan.

Scope (H06): serve a recorded ViewPoint session from ``replay/`` and a Gemini
planning panel. This service does not actuate a browser, expose a host tunnel,
or accept credentials. The replay is always labelled REPLAY, and the sample
document shipped here is labelled SAMPLE.

Run locally:  python3 app.py --host 127.0.0.1 --port 8080
Cloud Run:    the Dockerfile listens on $PORT (default 8080).
"""

from __future__ import annotations

import argparse
import hmac
import html
import ipaddress
import json
import math
import mimetypes
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import unquote, urlparse

from gemini_client import (
    MISSING_KEY_MESSAGE,
    GeminiCallError,
    GeminiInvalidResponse,
    GeminiNotConfigured,
    create_client_from_env,
)
from plan_schema import PlanValidationError, validate_plan
from replay_store import ReplayError, ReplayNotFound, ReplayStore, parse_pins

BASE_DIR = Path(__file__).resolve().parent
MAX_REQUEST_BYTES = 8_192
DEFAULT_DAILY_CAP = 200
DEFAULT_MAX_INFLIGHT = 4
DEFAULT_RATE_CAPACITY = 10.0
DEFAULT_RATE_REFILL_PER_SEC = 1.0 / 6.0
DEFAULT_SOCKET_TIMEOUT = 10.0
DEFAULT_MAX_WORKERS = 80  # >= Cloud Run default --concurrency (80); see README
DEFAULT_KEEPALIVE_IDLE = 2.0  # idle keep-alive must not pin a worker slot
DEADLINE_GRACE_SECONDS = 2.0  # hard abort fires after the soft 408 path
DEFAULT_REQUEST_DEADLINE = 10.0
MAX_BUCKETS = 5000
BUCKET_TTL_SECONDS = 3600.0
DEFAULT_QUOTA_FILE = "/tmp/viewpoint_quota.json"
PAGE_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
API_CSP = "default-src 'none'; frame-ancestors 'none'"
STATIC_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
    "frame-ancestors 'none'"
)


class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading server with a bounded worker pool (fail-fast overload)."""

    daemon_threads = True

    def __init__(
        self,
        server_address: Any,
        RequestHandlerClass: Any,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> None:
        self._worker_sem = threading.BoundedSemaphore(max(1, int(max_workers)))
        self.max_workers = max(1, int(max_workers))
        super().__init__(server_address, RequestHandlerClass)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._worker_sem.acquire(blocking=False):
            self._shed(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._worker_sem.release()
            raise

    @staticmethod
    def _shed(request: Any) -> None:
        """Fail fast without blocking the accept loop.

        Consume request bytes that already arrived so close() is less likely
        to become an RST that discards the 503 (best effort, non-blocking).
        """
        try:
            try:
                request.setblocking(False)
                request.recv(MAX_REQUEST_BYTES + 16_384)
            except OSError:
                pass
            request.settimeout(0.2)
            request.sendall(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n"
                b"Retry-After: 1\r\n"
                b"\r\n"
            )
            try:
                request.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        except Exception:
            pass
        finally:
            try:
                request.close()
            except Exception:
                pass

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_sem.release()


@dataclass(frozen=True)
class StaticAsset:
    data: bytes
    content_type: str


class StaticAssets:
    """Whitelist of files under static/; URLs never become filesystem paths."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._assets: dict[str, StaticAsset] = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.root).as_posix()
            self._assets["/static/" + relative] = StaticAsset(
                data=path.read_bytes(),
                content_type=self._content_type(path),
            )

    @staticmethod
    def _content_type(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".css":
            return "text/css; charset=utf-8"
        if suffix == ".js":
            return "text/javascript; charset=utf-8"
        if suffix == ".svg":
            return "image/svg+xml"
        guessed, _ = mimetypes.guess_type(path.name)
        return guessed or "application/octet-stream"

    def get(self, url_path: str) -> StaticAsset | None:
        return self._assets.get(url_path)


def _parse_int_env(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _parse_float_env(name: str, default: float) -> float:
    try:
        value = float(str(os.environ.get(name, "")).strip())
    except ValueError:
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return value


def _valid_positive_float(value: float | None, default: float) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number <= 0:
        return default
    return number


def _valid_positive_int(value: int | None, default: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _valid_ip(value: str) -> str | None:
    candidate = value.strip()
    if not candidate or len(candidate) > 45:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _extract_client_ip(headers: Any, peer_ip: str, *, trust_proxy: bool) -> str:
    """Rate-bucket key.

    trust_proxy False (default): the TCP peer. Behind Cloud Run that is the
    front end, so ALL clients share ONE global bucket (documented in README).
    trust_proxy True: the LAST X-Forwarded-For entry across every XFF header
    line, i.e. the hop appended by the trusted front end. Earlier entries and
    X-Real-IP are client-controlled and are never used.
    """
    if not trust_proxy or headers is None:
        return peer_ip or "unknown"
    values: list[str] = []
    try:
        get_all = getattr(headers, "get_all", None)
        if callable(get_all):
            values = [str(v) for v in (get_all("X-Forwarded-For") or [])]
        else:
            single = headers.get("X-Forwarded-For")
            values = [str(single)] if single else []
    except Exception:
        values = []
    if values:
        ip = _valid_ip(",".join(values).split(",")[-1])
        if ip:
            return ip
    return peer_ip or "unknown"


class Application:
    def __init__(
        self,
        store: ReplayStore,
        static: StaticAssets,
        client: Any,
        page_template: str,
        *,
        log_stream: TextIO,
        default_replay_id: str | None = None,
        service: str = "viewpoint-gateway",
        revision: str = "local",
        daily_cap: int | None = None,
        demo_token: str | None = None,
        rate_capacity: float | None = None,
        rate_refill_per_sec: float | None = None,
        max_inflight: int | None = None,
        quota_file: str | None = None,
        trust_proxy: bool | None = None,
    ) -> None:
        self.store = store
        self.static = static
        self.client = client
        self.page_template = page_template
        self.log_stream = log_stream
        self.service = service
        self.revision = revision
        self.summaries = [ReplayStore.public_summary(replay) for replay in store.all()]
        self.default_replay_id = (
            default_replay_id
            if default_replay_id and any(
                item["replay_id"] == default_replay_id for item in self.summaries
            )
            else store.default_id()
        )
        self.daily_cap = (
            _valid_positive_int(daily_cap, _parse_int_env("PLAN_DAILY_CAP", DEFAULT_DAILY_CAP))
            if daily_cap is not None
            else _parse_int_env("PLAN_DAILY_CAP", DEFAULT_DAILY_CAP)
        )
        env_token = os.environ.get("DEMO_TOKEN", "")
        self.demo_token = demo_token if demo_token is not None else (env_token if env_token else None)
        self.rate_capacity = (
            _valid_positive_float(rate_capacity, _parse_float_env("PLAN_RATE_CAPACITY", DEFAULT_RATE_CAPACITY))
            if rate_capacity is not None
            else _parse_float_env("PLAN_RATE_CAPACITY", DEFAULT_RATE_CAPACITY)
        )
        self.rate_refill_per_sec = (
            _valid_positive_float(rate_refill_per_sec, _parse_float_env("PLAN_RATE_REFILL", DEFAULT_RATE_REFILL_PER_SEC))
            if rate_refill_per_sec is not None
            else _parse_float_env("PLAN_RATE_REFILL", DEFAULT_RATE_REFILL_PER_SEC)
        )
        self.max_inflight = (
            _valid_positive_int(max_inflight, _parse_int_env("PLAN_MAX_INFLIGHT", DEFAULT_MAX_INFLIGHT))
            if max_inflight is not None
            else _parse_int_env("PLAN_MAX_INFLIGHT", DEFAULT_MAX_INFLIGHT)
        )
        if trust_proxy is not None:
            self.trust_proxy = bool(trust_proxy)
        else:
            self.trust_proxy = str(os.environ.get("TRUST_PROXY", "")).strip().lower() in ("1", "true", "yes", "on")
        # "global_peer": behind Cloud Run every client shares one bucket.
        self.rate_scope = "per_client_xff_last" if self.trust_proxy else "global_peer"
        if quota_file is not None:
            self._quota_file = str(quota_file) if quota_file else None
        else:
            self._quota_file = str(os.environ.get("PLAN_QUOTA_FILE", "")).strip() or None
        # Admission state (guarded by _cond).
        self._cond = threading.Condition()
        self._buckets: dict[str, tuple[float, float]] = {}
        self._daily_counts: dict[str, int] = {}
        self._inflight_n = 0
        self._active_n = 0
        self._accepting = True
        self._load_quota_file()

    # -- admission (must run BEFORE any billed Gemini call) ----------------
    def begin_shutdown(self) -> None:
        with self._cond:
            self._accepting = False
            self._cond.notify_all()

    def is_accepting(self) -> bool:
        with self._cond:
            return self._accepting

    def _load_quota_file(self) -> None:
        if not self._quota_file:
            return
        try:
            raw = Path(self._quota_file).read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return
            daily = data.get("daily_counts")
            if isinstance(daily, dict):
                for key, value in daily.items():
                    try:
                        number = int(value)
                    except (TypeError, ValueError):
                        continue
                    if number > 0:
                        self._daily_counts[str(key)] = number
            buckets = data.get("buckets")
            if isinstance(buckets, dict):
                for key, value in buckets.items():
                    try:
                        tokens, last = value
                        tokens_f = float(tokens)
                        last_f = float(last)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(tokens_f) and math.isfinite(last_f):
                        self._buckets[str(key)] = (tokens_f, last_f)
            # Buckets persist monotonic timestamps, which reset on process
            # restart. Reset them to now so refill/TTL math stays valid.
            _now = time.monotonic()
            for key in list(self._buckets):
                _tokens, _last = self._buckets[key]
                self._buckets[key] = (_tokens, _now)
            # Prune over-large persisted state on load.
            if len(self._buckets) > MAX_BUCKETS:
                for stale in sorted(self._buckets, key=lambda k: self._buckets[k][1])[: len(self._buckets) - MAX_BUCKETS]:
                    del self._buckets[stale]
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            return

    def _persist_quota_locked(self) -> None:
        if not self._quota_file:
            return
        try:
            path = Path(self._quota_file)
            tmp = path.with_suffix(".tmp")
            payload = {
                "daily_counts": dict(self._daily_counts),
                "buckets": {key: [tokens, last] for key, (tokens, last) in self._buckets.items()},
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def begin_request(self) -> None:
        with self._cond:
            self._active_n += 1

    def end_request(self) -> None:
        with self._cond:
            self._active_n = max(0, self._active_n - 1)
            self._cond.notify_all()

    def active_count(self) -> int:
        with self._cond:
            return self._active_n

    def wait_drained(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._inflight_n > 0 or self._active_n > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
            return True

    def _admit(self, client_ip: str, demo_token: str | None) -> None:
        """Reserve admission or raise GatewayError. Commits on success only."""
        with self._cond:
            if not self._accepting:
                raise GatewayError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "SHUTTING_DOWN",
                    "Server is shutting down.",
                    retry_after=5,
                )
            if self.demo_token:
                provided = demo_token if isinstance(demo_token, str) else ""
                # Compare bytes: compare_digest(str, str) raises TypeError on
                # non-ASCII, and http.server decodes headers as latin-1.
                if not hmac.compare_digest(
                    provided.encode("utf-8", "surrogatepass"),
                    str(self.demo_token).encode("utf-8", "surrogatepass"),
                ):
                    raise GatewayError(
                        HTTPStatus.UNAUTHORIZED,
                        "DEMO_TOKEN_REQUIRED",
                        "A valid demo token is required.",
                    )
            today = datetime.now(timezone.utc).date().isoformat()
            used = self._daily_counts.get(today, 0)
            if used >= self.daily_cap:
                raise GatewayError(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "DAILY_CAP_EXCEEDED",
                    "Daily planning quota exhausted.",
                    retry_after=3600,
                )
            now = time.monotonic()
            tokens, last = self._buckets.get(client_ip or "unknown", (self.rate_capacity, now))
            tokens = min(self.rate_capacity, tokens + (now - last) * self.rate_refill_per_sec)
            if tokens < 1.0:
                wait = int(((1.0 - tokens) / self.rate_refill_per_sec)) + 1 if self.rate_refill_per_sec > 0 else 60
                raise GatewayError(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "RATE_LIMITED",
                    "Too many planning requests. Try again later.",
                    retry_after=max(1, wait),
                )
            if self._inflight_n >= self.max_inflight:
                raise GatewayError(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "PLAN_BUSY",
                    "Too many planning requests in flight. Try again later.",
                    retry_after=1,
                )
            # Commit only when every check passed.
            self._buckets[client_ip or "unknown"] = (tokens - 1.0, now)
            self._daily_counts[today] = used + 1
            self._inflight_n += 1
            # Evict expired / over-capacity buckets before persisting.
            expired = [
                key
                for key, (tok, last_seen) in self._buckets.items()
                if (now - last_seen) > BUCKET_TTL_SECONDS
            ]
            for key in expired:
                del self._buckets[key]
            if len(self._buckets) > MAX_BUCKETS:
                for stale in sorted(self._buckets, key=lambda k: self._buckets[k][1])[: len(self._buckets) - MAX_BUCKETS]:
                    del self._buckets[stale]
            self._persist_quota_locked()

    def _release_admission(self) -> None:
        with self._cond:
            self._inflight_n = max(0, self._inflight_n - 1)
            self._cond.notify_all()

    def inflight_count(self) -> int:
        with self._cond:
            return self._inflight_n

    # -- logging ---------------------------------------------------------
    def log(self, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
        }
        record.update(fields)
        try:
            self.log_stream.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            self.log_stream.flush()
        except Exception:
            pass

    # -- page and API payloads ------------------------------------------
    def page(self) -> bytes:
        bootstrap = {
            "replayId": self.default_replay_id,
            "replays": self.summaries,
            "mode": "REPLAY",
            "service": self.service,
            "revision": self.revision,
        }
        serialized = json.dumps(
            bootstrap, ensure_ascii=False, separators=(",", ":")
        ).replace("<", "\\u003c")
        html_page = self.page_template.replace("__BOOTSTRAP_JSON__", serialized)
        html_page = html_page.replace("__PLAN_INITIAL__", self._initial_plan_html())
        return html_page.encode("utf-8")

    def _initial_plan_html(self) -> str:
        """Idle placeholder: the plan is generated only on explicit user action."""

        if getattr(self.client, "configured", False):
            return '<p class="plan-placeholder">Select "Generate plan" to request a new Gemini plan. Nothing is billed until you click.</p>'
        message = html.escape(MISSING_KEY_MESSAGE, quote=True)
        return (
            '<div class="error-box">'
            '<p class="error-title">Gemini key absent</p>'
            f'<p class="error-message">{message}</p>'
            '<p class="error-hint">The replay remains available. '
            "The key is never sent to this page.</p>"
            "</div>"
        )

    def replays(self) -> dict[str, Any]:
        return {"ok": True, "replays": self.summaries}

    def replay(self, replay_id: str) -> tuple[dict[str, Any], str]:
        replay = self.store.get(replay_id)
        return replay.data, replay.sha256

    def replay_attestation(self, replay_id: str) -> str:
        return getattr(self.store.get(replay_id), "attestation", "self-attested")

    def health(self) -> dict[str, Any]:
        """Liveness only: the process is up. Never claims planning works."""
        return {
            "ok": True,
            "service": self.service,
            "revision": self.revision,
            "replays": len(self.summaries),
            "gemini_configured": bool(getattr(self.client, "configured", False)),
            "model": getattr(self.client, "model", "unknown"),
            "rate_scope": self.rate_scope,
        }

    def readiness(self) -> dict[str, Any]:
        configured = bool(getattr(self.client, "configured", False))
        ready = configured and bool(self.summaries) and self.is_accepting()
        return {
            "ok": ready,
            "ready": ready,
            "service": self.service,
            "revision": self.revision,
            "replays": len(self.summaries),
            "gemini_configured": configured,
            "model": getattr(self.client, "model", "unknown"),
        }

    def plan(
        self,
        replay_id: str | None,
        *,
        client_ip: str = "unknown",
        demo_token: str | None = None,
    ) -> dict[str, Any]:
        if replay_id is None:
            replay_id = self.default_replay_id
        elif replay_id == "":
            raise GatewayError(HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "replay_id must not be empty.")
        # Resolve first: unknown replays must not consume billed admission.
        replay = self.store.get(replay_id)
        # Admission gate — strictly before any billed Gemini call.
        self._admit(client_ip, demo_token)
        started = time.monotonic()
        try:
            try:
                raw_plan = self.client.generate_plan(replay.data)
                plan = validate_plan(raw_plan, replay.data["steps"])
            except GeminiNotConfigured as exc:
                self.log(
                    "plan",
                    status="missing_key",
                    replay_id=replay.replay_id,
                    model=getattr(self.client, "model", "unknown"),
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                )
                raise GatewayError(HTTPStatus.SERVICE_UNAVAILABLE, exc.code, str(exc)) from None
            except (GeminiCallError, GeminiInvalidResponse) as exc:
                self.log(
                    "plan",
                    status="provider_error",
                    code=exc.code,
                    replay_id=replay.replay_id,
                    model=getattr(self.client, "model", "unknown"),
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                )
                raise GatewayError(HTTPStatus.BAD_GATEWAY, exc.code, str(exc)) from None
            except PlanValidationError:
                self.log(
                    "plan",
                    status="invalid_plan",
                    code="GEMINI_INVALID_RESPONSE",
                    replay_id=replay.replay_id,
                    model=getattr(self.client, "model", "unknown"),
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                )
                raise GatewayError(
                    HTTPStatus.BAD_GATEWAY,
                    "GEMINI_INVALID_RESPONSE",
                    "Gemini returned a plan that did not match the required schema; no plan is shown.",
                ) from None
            except Exception as exc:
                self.log(
                    "plan",
                    status="internal_error",
                    error_class=type(exc).__name__,
                    replay_id=replay.replay_id,
                    model=getattr(self.client, "model", "unknown"),
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                )
                raise GatewayError(
                    HTTPStatus.BAD_GATEWAY,
                    "GEMINI_INVALID_RESPONSE",
                    "Gemini planning failed; no plan is shown.",
                ) from None

            duration_ms = round((time.monotonic() - started) * 1000, 1)
            self.log(
                "plan",
                status="ok",
                replay_id=replay.replay_id,
                model=getattr(self.client, "model", "unknown"),
                duration_ms=duration_ms,
            )
            return {
                "ok": True,
                "mode": "REPLAY",
                "replay_id": replay.replay_id,
                "replay_version": replay.data["version"],
                "replay_sha256": replay.sha256,
                "replay_attestation": getattr(replay, "attestation", "self-attested"),
                "model": getattr(self.client, "model", "unknown"),
                "generated_at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "plan": plan,
                "evidence_policy": (
                    "This is a new Gemini plan. It does not authorize, replay or "
                    "reattribute the recorded actions. Each referenced id is shown "
                    "individually: an id absent from the recording is shown as "
                    "'no matching execution', and a step with only some ids present "
                    "is labelled partial. A present id means the recorded id exists; "
                    "support is unverified."
                ),
            }
        finally:
            self._release_admission()


def make_handler(application: Application, *, socket_timeout: float = DEFAULT_SOCKET_TIMEOUT, request_deadline: float | None = None, keepalive_idle: float | None = None):
    _sock_timeout = _valid_positive_float(float(socket_timeout), DEFAULT_SOCKET_TIMEOUT) if socket_timeout is not None else _parse_float_env("SOCKET_TIMEOUT", DEFAULT_SOCKET_TIMEOUT)
    _deadline = _valid_positive_float(request_deadline, _valid_positive_float(_sock_timeout, DEFAULT_REQUEST_DEADLINE)) if request_deadline is not None else _valid_positive_float(_sock_timeout, DEFAULT_REQUEST_DEADLINE)
    # ... existing _sock_timeout / _deadline lines unchanged ...
    _idle = _valid_positive_float(keepalive_idle, DEFAULT_KEEPALIVE_IDLE) if keepalive_idle is not None else _parse_float_env("KEEPALIVE_IDLE", DEFAULT_KEEPALIVE_IDLE)
    _idle = min(float(_idle), float(_sock_timeout))
    class GatewayHandler(BaseHTTPRequestHandler):
        server_version = "ViewPointGateway/1.0"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = float(_sock_timeout)
        request_deadline = float(_deadline)
        keepalive_idle = float(_idle)

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            self._handle("GET")

        def do_HEAD(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def setup(self) -> None:
            super().setup()
            self._served = 0

        def handle_one_request(self) -> None:
            self._body_consumed = False
            if getattr(self, "_served", 0) > 0:
                # Idle keep-alive: release the worker slot quickly instead of
                # holding it for the full request deadline (Cloud Run pools).
                try:
                    self.connection.settimeout(float(self.keepalive_idle))
                except OSError:
                    pass
            self._deadline_timer = None
            self._request_deadline_at = time.monotonic() + float(self.request_deadline)
            timer = threading.Timer(
                float(self.request_deadline) + DEADLINE_GRACE_SECONDS, self._deadline_abort
            )
            timer.daemon = True
            self._deadline_timer = timer
            timer.start()
            try:
                super().handle_one_request()
            finally:
                self._clear_deadline()
                self._served = getattr(self, "_served", 0) + 1

        def parse_request(self) -> bool:
            ok = super().parse_request()
            try:
                self.connection.settimeout(float(self.timeout))
            except OSError:
                pass
            return ok

        def _body_pending(self) -> bool:
            if getattr(self, "_body_consumed", False):
                return False
            headers = getattr(self, "headers", None)
            if headers is None:
                return False
            if headers.get("Transfer-Encoding"):
                return True
            try:
                values = headers.get_all("Content-Length") or []
            except Exception:
                values = []
            if len(values) > 1:
                return True
            return bool(values) and values[0].strip() != "0"

        def _body_timeout(self) -> GatewayError:
            self.close_connection = True
            return GatewayError(
                HTTPStatus.REQUEST_TIMEOUT,
                "REQUEST_TIMEOUT",
                "Request body read timed out.",
                retry_after=1,
            )

        def _clear_deadline(self) -> None:
            timer = getattr(self, "_deadline_timer", None)
            if timer is not None:
                timer.cancel()
                self._deadline_timer = None

        def _deadline_abort(self) -> None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.connection.close()
            except Exception:
                pass

        def _handle(self, method: str) -> None:
            application.begin_request()
            try:
                self._handle_inner(method)
            finally:
                application.end_request()

        def _handle_inner(self, method: str) -> None:
            try:
                path = unquote(urlparse(self.path).path)
                if method == "GET":
                    # Headers are in; GETs carry no body to acquire. The
                    # absolute deadline must not outlive request acquisition,
                    # or it would abort billed generate_plan() work.
                    self._clear_deadline()
                    self._route_get(path)
                else:
                    self._route_post(path)
            except GatewayError as exc:
                extra: dict[str, str] | None = None
                if exc.retry_after is not None:
                    extra = {"Retry-After": str(int(exc.retry_after))}
                self._send_error(exc.status, exc.code, exc.message, extra_headers=extra)
            except ReplayNotFound:
                self._send_error(
                    HTTPStatus.NOT_FOUND, "REPLAY_NOT_FOUND", "Unknown replay id."
                )
            except (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError):
                self.close_connection = True
            except Exception as exc:
                self.close_connection = True
                application.log(
                    "http_error",
                    method=method,
                    path=unquote(urlparse(self.path).path)[:200],
                    error_class=type(exc).__name__,
                )
                self._send_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "INTERNAL_ERROR",
                    "Internal gateway error.",
                )

        def _route_get(self, path: str) -> None:
            if path in ("/", "/index.html"):
                self._send_bytes(
                    HTTPStatus.OK,
                    "text/html; charset=utf-8",
                    application.page(),
                    cache="no-store",
                    csp=PAGE_CSP,
                )
                return
            if path == "/healthz":
                self._send_json(HTTPStatus.OK, application.health())
                return
            if path == "/readyz":
                body = application.readiness()
                status = HTTPStatus.OK if body.get("ready") else HTTPStatus.SERVICE_UNAVAILABLE
                self._send_json(status, body)
                return
            if path == "/api/replays":
                self._send_json(HTTPStatus.OK, application.replays())
                return
            if path == "/api/plan":
                self._send_error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    "Use POST for /api/plan.",
                    extra_headers={"Allow": "POST"},
                )
                return
            if path.startswith("/api/replay/"):
                replay_id = path[len("/api/replay/") :]
                replay, digest = application.replay(replay_id)
                self._send_json(
                    HTTPStatus.OK,
                    replay,
                    extra_headers={
                        "X-Replay-SHA256": digest,
                        "X-Replay-Attestation": application.replay_attestation(replay_id),
                    },
                )
                return
            if path.startswith("/static/"):
                asset = application.static.get(path)
                if asset is None:
                    self._send_error(
                        HTTPStatus.NOT_FOUND, "NOT_FOUND", "Static asset not found."
                    )
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    asset.content_type,
                    asset.data,
                    cache="public, max-age=300",
                    csp=STATIC_CSP,
                )
                return
            self._send_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Not found.")

        def _route_post(self, path: str) -> None:
            if path != "/api/plan":
                self._send_error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Not found.")
                return
            payload = self._read_json_body()
            unknown = set(payload) - {"replay_id"}
            if unknown:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "Unknown request field.",
                )
                return
            replay_id = payload.get("replay_id")
            if replay_id is not None and not isinstance(replay_id, str):
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "replay_id must be a string.",
                )
                return
            if replay_id == "":
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "replay_id must not be empty.",
                )
                return
            peer_ip = self.client_address[0] if self.client_address else "unknown"
            client_ip = _extract_client_ip(self.headers, peer_ip, trust_proxy=getattr(application, "trust_proxy", False))
            demo_token = self.headers.get("X-Demo-Token")
            response = application.plan(replay_id, client_ip=client_ip, demo_token=demo_token)
            self._send_json(HTTPStatus.OK, response)

        def _read_json_body(self) -> dict[str, Any]:
            content_type = self.headers.get("Content-Type", "")
            if "application/json" not in content_type.lower():
                raise GatewayError(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "UNSUPPORTED_MEDIA_TYPE",
                    "Use application/json.",
                )
            transfer_encoding = self.headers.get("Transfer-Encoding", "")
            if transfer_encoding and "chunked" in transfer_encoding.lower():
                self.close_connection = True
                raise GatewayError(
                    HTTPStatus.BAD_REQUEST,
                    "BAD_REQUEST",
                    "Chunked transfer encoding is not supported; send Content-Length.",
                )
            if len(self.headers.get_all("Content-Length") or []) > 1:
                self.close_connection = True
                raise GatewayError(
                    HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Multiple Content-Length headers."
                )
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                self.close_connection = True
                raise GatewayError(
                    HTTPStatus.LENGTH_REQUIRED,
                    "LENGTH_REQUIRED",
                    "Content-Length is required.",
                )
            try:
                length = int(length_header)
            except ValueError:
                self.close_connection = True
                raise GatewayError(
                    HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Invalid Content-Length."
                ) from None
            if length < 0:
                self.close_connection = True
                raise GatewayError(
                    HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Invalid Content-Length."
                )
            if length > MAX_REQUEST_BYTES:
                self.close_connection = True
                raise GatewayError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "REQUEST_TOO_LARGE",
                    "Request body is too large.",
                )
            if length == 0:
                self._clear_deadline()
                self._body_consumed = True
                return {}
            deadline_at = getattr(self, "_request_deadline_at", None) or (
                time.monotonic() + float(self.request_deadline)
            )
            reader = getattr(self.rfile, "read1", None) or self.rfile.read
            try:
                old_timeout = self.connection.gettimeout()
            except OSError:
                old_timeout = float(self.timeout)
            raw = bytearray()
            try:
                while len(raw) < length:
                    remaining = deadline_at - time.monotonic()
                    if remaining <= 0:
                        raise self._body_timeout()
                    self.connection.settimeout(remaining)
                    try:
                        chunk = reader(length - len(raw))
                    except (socket.timeout, TimeoutError):
                        # SocketIO is unusable after its first timeout: answer
                        # 408 now and close, instead of retrying into OSError/500.
                        raise self._body_timeout() from None
                    except (BrokenPipeError, ConnectionResetError):
                        self.close_connection = True
                        raise GatewayError(
                            HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Incomplete request body."
                        ) from None
                    if not chunk:
                        break
                    raw.extend(chunk)
            finally:
                try:
                    self.connection.settimeout(old_timeout)
                except OSError:
                    pass
            raw = bytes(raw)
            if len(raw) != length:
                self.close_connection = True
                raise GatewayError(
                    HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Incomplete request body."
                )
            self._body_consumed = True
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise GatewayError(
                    HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Invalid JSON body."
                ) from None
            if not isinstance(payload, dict):
                raise GatewayError(
                    HTTPStatus.BAD_REQUEST, "BAD_REQUEST", "Body must be a JSON object."
                )
            # Request acquisition (headers + body) is complete: disarm the
            # absolute connection deadline so it cannot abort generate_plan().
            self._clear_deadline()
            return payload

        def _send_error(
            self,
            status: int,
            code: str,
            message: str,
            *,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self._send_json(
                status,
                {"ok": False, "error": {"code": code, "message": message}},
                extra_headers=extra_headers,
            )

        def _send_json(
            self,
            status: int,
            payload: dict[str, Any],
            *,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self._send_bytes(
                status,
                "application/json; charset=utf-8",
                body,
                cache="no-store",
                csp=API_CSP,
                extra_headers=extra_headers,
            )

        def _send_bytes(
            self,
            status: int,
            content_type: str,
            body: bytes,
            *,
            cache: str,
            csp: str,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            if self._body_pending():
                # Unread body bytes would be parsed as the next request.
                self.close_connection = True
            if self.close_connection:
                self.send_header("Connection", "close")
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            # Deliberately excludes query strings, headers and bodies.
            application.log(
                "http",
                method=self.command or "-",
                path=urlparse(self.path).path[:200],
                status=code,
            )

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def log_error(self, format: str, *args: Any) -> None:
            pass

    return GatewayHandler


def build_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    gemini_client: Any | None = None,
    log_stream: TextIO | None = None,
    base_dir: Path | str = BASE_DIR,
    default_replay_id: str | None = None,
    daily_cap: int | None = None,
    demo_token: str | None = None,
    rate_capacity: float | None = None,
    rate_refill_per_sec: float | None = None,
    max_inflight: int | None = None,
    socket_timeout: float | None = None,
    max_workers: int | None = None,
    quota_file: str | None = None,
    trust_proxy: bool | None = None,
    request_deadline: float | None = None,
    keepalive_idle: float | None = None,
) -> ThreadingHTTPServer:
    base = Path(base_dir).resolve()
    log = log_stream or sys.stderr
    pins = parse_pins(os.environ.get("REPLAY_PINS", ""))
    require_pin = str(os.environ.get("REPLAY_REQUIRE_PIN", "")).strip().lower() in ("1", "true", "yes", "on")
    store = ReplayStore(base / "replay", base / "static", pins=pins, require_pin=require_pin)
    static = StaticAssets(base / "static")
    template = (base / "templates" / "index.html").read_text(encoding="utf-8")
    client = gemini_client if gemini_client is not None else create_client_from_env()
    application = Application(
        store,
        static,
        client,
        template,
        log_stream=log,
        default_replay_id=default_replay_id or os.environ.get("DEFAULT_REPLAY_ID"),
        service=os.environ.get("K_SERVICE", "viewpoint-gateway"),
        revision=os.environ.get("K_REVISION", "local"),
        daily_cap=daily_cap,
        demo_token=demo_token,
        rate_capacity=rate_capacity,
        rate_refill_per_sec=rate_refill_per_sec,
        max_inflight=max_inflight,
        quota_file=quota_file,
        trust_proxy=trust_proxy,
    )
    sock_timeout = _valid_positive_float(socket_timeout, _parse_float_env("SOCKET_TIMEOUT", DEFAULT_SOCKET_TIMEOUT)) if socket_timeout is not None else _parse_float_env("SOCKET_TIMEOUT", DEFAULT_SOCKET_TIMEOUT)
    workers = _valid_positive_int(max_workers, _parse_int_env("MAX_WORKERS", DEFAULT_MAX_WORKERS)) if max_workers is not None else _parse_int_env("MAX_WORKERS", DEFAULT_MAX_WORKERS)
    deadline = _valid_positive_float(request_deadline, float(sock_timeout)) if request_deadline is not None else _valid_positive_float(float(sock_timeout), DEFAULT_REQUEST_DEADLINE)
    handler = make_handler(application, socket_timeout=float(sock_timeout), request_deadline=float(deadline), keepalive_idle=keepalive_idle)
    server = BoundedThreadingHTTPServer((host, port), handler, max_workers=int(workers))
    server.daemon_threads = True
    server.allow_reuse_address = True
    server.application = application  # type: ignore[attr-defined]
    return server


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ViewPoint replay + Gemini gateway")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="bind address (Cloud Run container uses 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8080")),
        help="TCP port (Cloud Run sets $PORT)",
    )
    parser.add_argument(
        "--base-dir",
        default=str(BASE_DIR),
        help="directory containing replay/, static/ and templates/",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not 1 <= args.port <= 65535:
        print("viewpoint-gateway: --port must be between 1 and 65535", file=sys.stderr)
        return 2
    try:
        server = build_server(
            host=args.host,
            port=args.port,
            base_dir=args.base_dir,
        )
    except (ReplayError, ValueError, OSError) as exc:
        print(f"viewpoint-gateway: startup failed: {exc}", file=sys.stderr)
        return 2

    application: Application = server.application  # type: ignore[attr-defined]
    application.log(
        "listen",
        host=args.host,
        port=args.port,
        replays=len(application.summaries),
        gemini_configured=bool(getattr(application.client, "configured", False)),
        model=getattr(application.client, "model", "unknown"),
        rate_scope=application.rate_scope,
        max_workers=server.max_workers,
    )

    stopping = threading.Event()
    drain_timeout = _parse_float_env("DRAIN_TIMEOUT", 10.0)

    def stop(signum: int, frame: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        application.begin_shutdown()
        application.log("shutdown", signal=signum)
        # shutdown() must run outside the serve_forever thread.
        def _shutdown() -> None:
            server.shutdown()
            drained = application.wait_drained(timeout=float(drain_timeout))
            application.log("shutdown_drained", drained=drained, inflight=application.inflight_count())

        threading.Thread(target=_shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        application.begin_shutdown()
        application.wait_drained(timeout=float(drain_timeout))
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
