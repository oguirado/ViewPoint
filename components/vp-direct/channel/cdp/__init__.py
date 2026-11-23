"""Registry-bound Chromium CDP channel for an already launched session.

The authority supplies read_session(session_id) and an effect_guard(request)
context manager that holds admission through dispatch. Call sync_authority at
the human-take fence. Observations and selectors are local to the backend.
"""

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException
import json
from pathlib import PurePosixPath
import re
import socket
from threading import RLock, Thread
import time
from urllib.parse import urlsplit
from uuid import uuid4

from jsonschema import ValidationError

from focus import FocusRefused
from schemas.validation import validate_contract
from wender.act import cdp_executor
from wender.perceive import ax
from wender.perceive.cdp_port import CdpError, CdpPort


_LOOPBACK = "127.0.0.1"
_MAX_VERSION_BYTES = 65536  # [TARGET] bound for the local discovery reply
_DOCUMENT_EVENTS = frozenset({
    "Page.frameNavigated", "Page.navigatedWithinDocument", "Page.documentOpened",
    "Page.frameDetached",
})
_TERMINAL_EVENTS = frozenset({
    "Target.detachedFromTarget", "Target.targetCrashed",
    "Inspector.detached", "Inspector.targetCrashed",
})


class Refused(Exception):
    """A typed refusal or an effect whose result could not be verified."""

    def __init__(self, code, *, focus_trace=None):
        self.code = code
        self.focus_trace = focus_trace
        super().__init__(code)


def _browser_url(value, port):
    if not isinstance(value, str):
        raise Refused("identity_mismatch")
    endpoint = urlsplit(value)
    if (endpoint.scheme != "ws" or endpoint.netloc != f"{_LOOPBACK}:{port}"
            or endpoint.query or endpoint.fragment
            or not re.fullmatch(r"/devtools/browser/[a-zA-Z0-9_-]+", endpoint.path)):
        raise Refused("identity_mismatch")
    return endpoint.geturl()


class _BrowserPort(CdpPort):
    """Reuse Wender's flattened sessions and correlated CDP command transport."""

    def __init__(self, port):
        super().__init__(port=port, host=_LOOPBACK, timeout=5)
        self._document_revisions = {}

    def start(self):
        import websocket  # The same websocket-client dependency as Wender.

        http = HTTPConnection(_LOOPBACK, self.port, timeout=self.timeout)
        try:
            http.request("GET", "/json/version")
            response = http.getresponse()
            if response.status != 200:
                raise Refused("identity_mismatch")
            body = response.read(_MAX_VERSION_BYTES + 1)
            if len(body) > _MAX_VERSION_BYTES:
                raise Refused("identity_mismatch")
            url = _browser_url(json.loads(body)["webSocketDebuggerUrl"], self.port)
        finally:
            http.close()

        # Use an already connected loopback socket to bypass proxy settings.
        stream = socket.create_connection((_LOOPBACK, self.port), timeout=self.timeout)
        try:
            self._ws = websocket.create_connection(
                url, socket=stream, timeout=self.timeout, suppress_origin=True, redirect_limit=0,
            )
        except websocket.WebSocketException as exc:
            stream.close()
            raise CdpError("CDP WebSocket handshake failed") from exc
        except Exception:
            stream.close()
            raise
        try:
            if self._ws.getstatus() != 101:
                raise Refused("identity_mismatch")
            self._ws.settimeout(None)
            self._reader = Thread(target=self._read_loop, name="vp-direct-cdp", daemon=True)
            self._reader.start()
        except Exception:
            self.close()
            raise
        return self

    def _store_event_locked(self, message):
        method = message.get("method")
        if method in _DOCUMENT_EVENTS:
            session_id = message.get("sessionId")
            if session_id in self._session_incarnations and session_id not in self._lane_errors:
                self._document_revisions[session_id] = self._document_revisions.get(session_id, 0) + 1
        elif method in _TERMINAL_EVENTS:
            super()._store_event_locked(message)

    def document_revision(self, session_id):
        with self._cv:
            if self._closed or session_id in self._lane_errors:
                raise Refused("identity_mismatch")
            return self._document_revisions.get(session_id, 0)

    def send(self, method, params=None, session_id=None, timeout=None):
        if method == "Browser.close":
            raise Refused("DISPLAY_UNSUPPORTED")
        if method == "Target.createTarget" and (
            session_id is not None
            or params != {"url": "about:blank", "background": True}
            or params["background"] is not True
        ):
            raise Refused("DISPLAY_UNSUPPORTED")
        return super().send(method, params=params, session_id=session_id, timeout=timeout)


def _connect(port):
    return _BrowserPort(port).start()


def _deny_effects(_request):
    raise Refused("NO_LEASE")


def _snap(executable):
    if executable is None:
        return False
    return ("snap" in PurePosixPath(executable).parts
            or "snapd" in PurePosixPath(executable).parts)


@dataclass(frozen=True)
class Observation:
    id: str
    target_id: str
    document: tuple
    ax_sha256: str
    ax_stable_sha256: str
    nodes: tuple = field(repr=False, compare=False)


class CdpBackend:
    """An attachment to one admitted S1/S2 session and its exact page target."""

    def __init__(self, session_id, read_session, *, effect_guard=_deny_effects,
                 connect=_connect, executable=None):
        self._session_id = session_id
        self._read_session = read_session
        self._effect_guard = effect_guard
        self._connect = connect
        self._executable = executable
        self._lock = RLock()
        self._port = self._attachment = self._binding = self._observation = None

    def _current_binding(self):
        session = self._read_session(self._session_id)
        if not isinstance(session, dict) or session.get("session_id") != self._session_id:
            raise Refused("identity_mismatch")
        if session.get("browser") == "firefox":
            raise Refused("BIDI_DEFERRED")
        if _snap(self._executable):
            raise Refused("SNAP_CDP_UNAVAILABLE")
        if session.get("cdp_allowed") is not True or session.get("backend") != "cdp":
            raise Refused("NO_LEASE")
        try:
            validate_contract("vp.session/v1", session)
        except ValidationError as exc:
            raise Refused("identity_mismatch") from exc
        lease = session["lease"]
        if session["state"] != "AGENT" or not lease["holder"]:
            raise Refused("NO_LEASE")
        window = session["window"]
        return (
            session["capsule_id"], session["incarnation"], lease["authority_epoch"],
            lease["holder"], session["cdp_port"], session["cdp_target_id"],
            (window["pid"], window["xid"], window["witness"]), session["display"],
        )

    def detach(self):
        """Close only this attachment and its socket, including after human take."""
        with self._lock:
            port, attachment = self._port, self._attachment
            self._port = self._attachment = self._binding = self._observation = None
            if port is not None:
                try:
                    if attachment is not None:
                        port.send("Target.detachFromTarget", {"sessionId": attachment}, timeout=1)
                except CdpError:
                    pass  # Closing our dedicated socket also detaches this session.
                finally:
                    port.close()

    def sync_authority(self):
        """Retire a live attachment when the broker revokes or replaces its binding."""
        with self._lock:
            if self._binding is None:
                return
            try:
                self._live_binding()
            except Refused:
                pass

    def _live_binding(self):
        try:
            binding = self._current_binding()
        except (Refused, OSError, LookupError):
            self.detach()
            raise
        if self._binding is not None and self._binding != binding:
            self.detach()
        return binding

    def _admit(self, request, op):
        binding = self._live_binding()
        validate_contract("vp.channel.req/v1", request)
        if request["backend"] != "cdp" or request["op"] != op:
            raise Refused("DISPLAY_UNSUPPORTED")
        capsule, incarnation, epoch, _, _, target, window, _ = binding
        hint = request.get("target", {})
        if (request["session_id"] != self._session_id or request["capsule_id"] != capsule
                or request["incarnation"] != incarnation
                or hint.get("cdp_target_id", target) != target
                or hint.get("window_id", window[1]) != window[1]):
            raise Refused("identity_mismatch")
        if request["authority_epoch"] != epoch:
            raise Refused("NO_LEASE")
        return binding

    @staticmethod
    def _require_page(port, target_id, *, attachment=None):
        # An omitted targetId asks Chrome which target owns this flattened session.
        params = {"targetId": target_id} if attachment is None else None
        reply = port.send("Target.getTargetInfo", params, session_id=attachment)
        info = reply.get("targetInfo") if isinstance(reply, dict) else None
        if not isinstance(info, dict):
            raise Refused("identity_mismatch")
        if info.get("targetId") != target_id or info.get("type") != "page":
            raise Refused("identity_mismatch")

    def attach_target(self, request, *, host=_LOOPBACK):
        """Attach the registry target. Dry run checks policy without transport."""
        with self._lock:
            binding = self._admit(request, "attach")
            if host != _LOOPBACK:
                raise Refused("identity_mismatch")
            if request["dry_run"] or self._port is not None:
                return
            port, target_id = binding[4:6]
            try:
                self._port = self._connect(port)
                self._require_page(self._port, target_id)
                self._attachment = self._port.send(
                    "Target.attachToTarget", {"targetId": target_id, "flatten": True},
                ).get("sessionId")
                if not isinstance(self._attachment, str) or not self._attachment:
                    self._attachment = None
                    raise Refused("identity_mismatch")
                self._require_page(self._port, target_id, attachment=self._attachment)
                self._send("Page.enable")
                self._send("Accessibility.enable")
                if self._current_binding() != binding:
                    raise Refused("identity_mismatch")
                self._binding = binding
            except (OSError, HTTPException, CdpError, ValueError, KeyError) as exc:
                self.detach()
                raise Refused("identity_mismatch") from exc
            except Exception:
                self.detach()
                raise

    def create_target(self, request, *, focus_monitor=None, surface_id=None, qualified=False):
        """Create a blank tab; host creation needs the caller's qualified A3 trace."""
        validate_contract("vp.channel.req/v1", request)
        guard = nullcontext() if request["dry_run"] else self._effect_guard(request)
        with guard, self._lock:
            binding = self._admit(request, "attach")
            if binding[7] == ":0" or focus_monitor is not None:
                if focus_monitor is None or not surface_id or qualified is not True:
                    raise Refused("HUMAN_NEEDED")
            if request["dry_run"]:
                return None
            if focus_monitor is None:
                return self._create_background_target(binding)
            try:
                target_id, _ = focus_monitor.run_passive(
                    surface_id, lambda: self._create_background_target(binding), qualified=qualified,
                )
            except FocusRefused as exc:
                raise Refused(exc.code, focus_trace=exc.trace) from exc
            return target_id

    def _create_background_target(self, binding):
        port = self._connect(binding[4])
        try:
            if self._current_binding() != binding:
                raise Refused("identity_mismatch")
            reply = port.send("Target.createTarget", {"url": "about:blank", "background": True})
            target_id = reply.get("targetId") if isinstance(reply, dict) else None
            if not isinstance(target_id, str) or not target_id or target_id == binding[5]:
                raise Refused("UNCERTAIN_OUTCOME")
            return target_id
        except (CdpError, OSError) as exc:
            raise Refused("UNCERTAIN_OUTCOME") from exc
        finally:
            port.close()

    def _send(self, method, params=None):
        return self._port.send(method, params, session_id=self._attachment)

    def _document(self):
        revision = self._port.document_revision(self._attachment)
        self._require_page(self._port, self._binding[5], attachment=self._attachment)
        frame = self._send("Page.getFrameTree").get("frameTree", {}).get("frame", {})
        identity = tuple(frame.get(key) for key in ("id", "loaderId", "url"))
        if (not all(isinstance(value, str) and value for value in identity)
                or revision != self._port.document_revision(self._attachment)):
            raise Refused("identity_mismatch")
        return (*identity, revision)

    def _snapshot(self):
        if self._port is None:
            raise Refused("NO_LEASE")
        try:
            document = self._document()
            raw_nodes = self._send("Accessibility.getFullAXTree").get("nodes")
            if not isinstance(raw_nodes, list) or not raw_nodes:
                raise Refused("identity_mismatch")
            nodes = ax.prune(raw_nodes)
            if not nodes or document != self._document():
                raise Refused("identity_mismatch")
        except (Refused, CdpError, TypeError, ValueError, AttributeError) as exc:
            self._observation = None
            raise Refused("identity_mismatch") from exc
        if self._live_binding() != self._binding:
            raise Refused("identity_mismatch")
        return Observation(
            uuid4().hex, self._binding[5], document, ax.snapshot_sha(nodes),
            ax.snapshot_sha(nodes, with_state=False), tuple(nodes),
        )

    def observe(self, request):
        """Keep the latest verified snapshot in RAM and return a copy to the caller."""
        with self._lock:
            self._admit(request, "observe")
            if request["dry_run"]:
                return None
            self._observation = None
            self._observation = self._snapshot()
            return deepcopy(self._observation)

    def _validate_observation(self, request):
        observation = self._observation
        if observation is None or request.get("observation_id") != observation.id:
            raise Refused("identity_mismatch")
        fresh = self._snapshot()
        hint = request.get("target", {})
        if (observation.document != fresh.document or observation.target_id != fresh.target_id
                or fresh.ax_stable_sha256 != observation.ax_stable_sha256
                or hint.get("ax_stable_sha256", fresh.ax_stable_sha256) != fresh.ax_stable_sha256):
            self._observation = None
            raise Refused("identity_mismatch")
        return observation, fresh

    def validate_observation(self, request):
        """Re-read the bound document and AX tree before reusing an observation."""
        with self._lock:
            validate_contract("vp.channel.req/v1", request)
            self._admit(request, request["op"])
            if request["dry_run"]:
                return None
            _, fresh = self._validate_observation(request)
            return fresh

    @staticmethod
    def _node(observation, node_id):
        nodes = [node for node in observation.nodes if node["id"] == node_id]
        if len(nodes) != 1 or nodes[0]["cardinality"] != 1:
            raise Refused("identity_mismatch")
        return nodes[0]

    def act(self, request, *, node_id, verify, expected_document_url=None):
        """Dispatch a semantic action once and require a trusted AX postcondition."""
        validate_contract("vp.channel.req/v1", request)
        if request["dry_run"]:
            with self._lock:
                self._admit(request, "act")
                return self._reply("OK")
        with self._effect_guard(request), self._lock:
            self._admit(request, "act")
            action = request["action"]
            if (action not in cdp_executor.JS_BY_OP or not callable(verify)
                    or request["require_vision_witness"]):
                raise Refused("DISPLAY_UNSUPPORTED")
            if action in ("type", "select") and "input_text" not in request:
                raise Refused("DISPLAY_UNSUPPORTED")
            original, fresh = self._validate_observation(request)
            before, current = self._node(original, node_id), self._node(fresh, node_id)
            backend_id = current["backend_id"]
            if (type(backend_id) is not int or backend_id <= 0
                    or before["backend_id"] != backend_id):
                raise Refused("identity_mismatch")
            if not ax.is_ready_to_act(current["state_word"]):
                raise Refused("DISPLAY_UNSUPPORTED")
            if action in ("type", "select") and current["role"] not in cdp_executor.EDITABLE_ROLES:
                raise Refused("DISPLAY_UNSUPPORTED")
            if verify(fresh.nodes) is not False:
                raise Refused("DISPLAY_UNSUPPORTED")
            try:
                resolved = self._send("DOM.resolveNode", {"backendNodeId": backend_id})
                object_id = resolved.get("object", {}).get("objectId")
                if not isinstance(object_id, str) or not object_id or self._document() != fresh.document:
                    raise Refused("identity_mismatch")
            except (CdpError, TypeError, AttributeError) as exc:
                raise Refused("identity_mismatch") from exc
            self._admit(request, "act")
            if (self._binding is None
                    or self._port.document_revision(self._attachment) != fresh.document[-1]):
                self._observation = None
                raise Refused("identity_mismatch")
            self._observation = None
            arguments = [{"value": request["input_text"]}] if action in ("type", "select") else []
            try:
                result = self._send("Runtime.callFunctionOn", {
                    "objectId": object_id, "functionDeclaration": cdp_executor.JS_BY_OP[action],
                    "arguments": arguments, "returnByValue": True,
                })
            except (CdpError, OSError):
                try:
                    self._snapshot()
                except (Refused, OSError):
                    pass  # The post-state is unavailable on a failed transport.
                return self._reply("UNCERTAIN_OUTCOME")
            try:
                return self._verify_effect(verify, fresh.document, result, expected_document_url)
            except (Refused, CdpError, OSError, TypeError, ValueError, AttributeError):
                return self._reply("UNCERTAIN_OUTCOME")

    def _verify_effect(self, verify, document, result, expected_document_url):
        deadline = time.monotonic() + 1  # [TARGET] bounded AX settling allowance
        while True:
            try:
                fresh = self._snapshot()
            except Refused:
                if expected_document_url is None or self._binding is None:
                    raise
                fresh = None  # A read straddling the specified navigation can be retried.
            if result.get("exceptionDetails"):
                return self._reply("UNCERTAIN_OUTCOME")
            if fresh is not None:
                if expected_document_url is None and fresh.document != document:
                    return self._reply("UNCERTAIN_OUTCOME")
                if (expected_document_url is None
                        or (fresh.document != document and fresh.document[2] == expected_document_url)):
                    if verify(fresh.nodes) is True:
                        return self._reply("OK", fresh)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._reply("UNCERTAIN_OUTCOME")
            time.sleep(min(0.05, remaining))

    def assert_observation(self, request, *, verify, expected_document_url=None):
        """Check a trusted AX predicate against a fresh observation of the bound page."""
        with self._lock:
            self._admit(request, "assert")
            if request["dry_run"]:
                return self._reply("OK")
            if not callable(verify):
                raise Refused("DISPLAY_UNSUPPORTED")
            fresh = self._snapshot()
            if (expected_document_url is not None and fresh.document[2] != expected_document_url):
                return self._reply("identity_mismatch")
            if verify(fresh.nodes) is True:
                return self._reply("OK", fresh)
            return self._reply("UNCERTAIN_OUTCOME")

    @staticmethod
    def _reply(code, observation=None):
        reply = {"schema": "vp.channel.reply/v1", "ok": code == "OK", "code": code}
        if observation is not None:
            reply.update(ax_sha256=observation.ax_sha256, ax_stable_sha256=observation.ax_stable_sha256)
        return reply
