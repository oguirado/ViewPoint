# @module {"id":"wa.perceive.cdp_port","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.cdp_pipe"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.cdp_port - attach to an ALREADY RUNNING, user-owned Chrome over its local DevTools port.

Same interface as CdpPipe (send / wait_event / attach_page / navigate / evaluate), so drivers and the executor
work unchanged. The browser belongs to the user: close() only detaches the sessions this client attached and
closes the socket; it never sends Browser.close. Loopback only (127.0.0.1 / localhost / ::1).
"""
from collections import deque
from dataclasses import dataclass
import json
import threading
import urllib.request
from urllib.parse import urlparse, urlsplit

from .cdp_pipe import (
    CdpError,
    CdpPipe,
    DEFAULT_TIMEOUT_S,
    EVENT_BUFFER,
    HOSTED_READONLY_CDP_METHODS,
    is_readonly_cdp_method,
)

LOOPBACK = ("127.0.0.1", "localhost", "::1")


# @anchor:wa.perceive.cdp_port.CdpTabError
class CdpTabError(CdpError, ValueError):
    """Tab selection, origin validation, or ambiguity error in CdpPort.ensure_tab."""


# @anchor:wa.perceive.cdp_port.normalize_https_origin
def normalize_https_origin(url):
    """Validate an explicit HTTPS URL without credentials and return ('https', host_lower, port)."""
    if not isinstance(url, str) or not url or any(ch.isspace() for ch in url):
        raise CdpTabError("HTTPS URL must be a non-empty string without whitespace")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise CdpTabError("invalid URL or port: %s" % exc)
    if parsed.scheme != "https" or not url.lower().startswith("https://"):
        raise CdpTabError("site requires an explicit https:// origin, got %r" % (url,))
    if not parsed.hostname:
        raise CdpTabError("HTTPS URL requires a valid hostname: %r" % (url,))
    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        raise CdpTabError("credentials in URL are forbidden: %r" % (url,))
    eff_port = 443 if port is None else int(port)
    if eff_port <= 0 or eff_port > 65535:
        raise CdpTabError("invalid effective port %r" % (eff_port,))
    return ("https", parsed.hostname.lower(), eff_port)


def _canonical_https_url(url):
    origin = normalize_https_origin(url)
    parsed = urlsplit(url)
    base = "https://%s:%d" % (origin[1], origin[2])
    rest = parsed.path or ""
    if parsed.query:
        rest += "?" + parsed.query
    if parsed.fragment:
        rest += "#" + parsed.fragment
    return origin, base + rest


def _matches_site_prefix(target_url, url_prefix, prefix_origin=None, canon_prefix=None):
    if prefix_origin is None or canon_prefix is None:
        prefix_origin, canon_prefix = _canonical_https_url(url_prefix)
    try:
        target_origin, canon_target = _canonical_https_url(target_url)
    except CdpTabError:
        return False
    if target_origin != prefix_origin:
        return False
    return canon_target.startswith(canon_prefix)


# @anchor:wa.perceive.cdp_port.TabHandle
@dataclass(frozen=True)
class TabHandle:
    """Handle returned by CdpPort.ensure_tab recording target, session, and ownership."""

    target_id: str
    session_id: str
    target_owned: bool
    session_owned: bool = True
    url_prefix: str = ""
    open_url: str = ""
    origin: tuple = ()
    _port: object = None

    @property
    def target(self):
        return self.target_id

    @property
    def targetId(self):
        return self.target_id

    @property
    def session(self):
        return self.session_id

    @property
    def sessionId(self):
        return self.session_id

    @property
    def tab_owned(self):
        return self.target_owned

    @property
    def created(self):
        return self.target_owned

    @property
    def reused(self):
        return not self.target_owned

    @property
    def target_ownership(self):
        return "broker" if self.target_owned else "caller"

    @property
    def tab_ownership(self):
        return "broker" if self.target_owned else "caller"

    @property
    def session_ownership(self):
        return "broker" if self.session_owned else "caller"

    def as_dict(self):
        return {
            "target_id": self.target_id,
            "targetId": self.target_id,
            "target": self.target_id,
            "session_id": self.session_id,
            "sessionId": self.session_id,
            "session": self.session_id,
            "target_owned": self.target_owned,
            "tab_owned": self.target_owned,
            "created": self.target_owned,
            "reused": not self.target_owned,
            "session_owned": self.session_owned,
            "target_ownership": self.target_ownership,
            "tab_ownership": self.tab_ownership,
            "session_ownership": self.session_ownership,
            "url_prefix": self.url_prefix,
            "open_url": self.open_url,
            "origin": self.origin,
        }

    def __getitem__(self, key):
        return self.as_dict()[key]

    def get(self, key, default=None):
        return self.as_dict().get(key, default)

    def __contains__(self, key):
        return key in self.as_dict()

    def keys(self):
        return self.as_dict().keys()

    def items(self):
        return self.as_dict().items()

    def values(self):
        return self.as_dict().values()

    def close(self):
        """Detach only this handle's owned session from the port, preserving caller pages."""
        if self._port is not None and self.session_owned:
            self._port.detach_session(self.session_id)


# @anchor:wa.perceive.cdp_port.CdpPort
class CdpPort(CdpPipe):
    """One websocket to the browser endpoint, many flattened sessions."""

    def __init__(self, port=9222, host="127.0.0.1", timeout=DEFAULT_TIMEOUT_S,
                 *legacy_clock, clock=None, writer_fence=None):
        if legacy_clock:
            if len(legacy_clock) != 1 or clock is not None:
                raise TypeError("clock supplied twice or too many positional arguments")
            clock = legacy_clock[0]
        if host not in LOOPBACK:
            raise CdpError("CdpPort attaches to loopback only, got %r" % (host,))
        self.host, self.port, self.timeout = host, int(port), timeout
        if clock is not None and not callable(clock):
            raise TypeError("clock must return monotonic nanoseconds")
        self.clock = clock
        self.writer_fence = writer_fence
        self._next_id = 0
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._replies = {}
        self._pending = set()
        self._connection_incarnation = 0
        self._request_info = {}
        self._tombstones = {}
        self._events_by_session = {}
        self._lane_errors = {}
        self._session_incarnations = {}
        self._target_sessions = {}
        self.events = deque(maxlen=EVENT_BUFFER)
        self._closed = False
        self._ws = None
        self._reader = None
        self._attached = []
        self._owned_targets = []
        self._caller_sessions = set()
        self.proc = None
        self.user_data_dir = None

    def start(self):
        import websocket  # websocket-client; imported lazily so the pipe path stays stdlib-only
        with urllib.request.urlopen("http://%s:%d/json/version" % (self.host, self.port), timeout=5) as r:
            url = json.load(r)["webSocketDebuggerUrl"]
        if urlparse(url).hostname not in LOOPBACK:
            raise CdpError("browser endpoint is not loopback: %r" % (url,))
        self._ws = websocket.create_connection(url, timeout=self.timeout, suppress_origin=True)
        self._ws.settimeout(None)
        with self._cv:
            self._state_locked()
            self._connection_incarnation += 1
            self._closed = False
            self._pending.clear()
            self._request_info.clear()
            self._replies.clear()
            self._events_by_session.clear()
            self._lane_errors.clear()
            self._session_incarnations.clear()
            self._target_sessions.clear()
            self.events.clear()
        self._reader = threading.Thread(target=self._read_loop, name="cdp-port-reader", daemon=True)
        self._reader.start()
        return self

    def _read_loop(self):
        try:
            while True:
                ws = self._ws
                if ws is None:
                    break
                data = ws.recv()
                if not data:
                    break
                msg = json.loads(data)
                if not isinstance(msg, dict):
                    continue
                with self._cv:
                    if "id" in msg:
                        self._store_reply_locked(msg)
                    else:
                        self._store_event_locked(msg)
                    self._cv.notify_all()
        except Exception:
            pass
        finally:
            with self._cv:
                self._closed = True
                self._cv.notify_all()

    def _write_command(self, message):
        ws = self._ws
        if ws is None:
            raise CdpError("WebSocket is not connected")
        ws.send(json.dumps(message, separators=(",", ":"), ensure_ascii=False))

    def send(self, method, params=None, session_id=None, timeout=None):
        """Shared connection-wide identities, deadlines, tombstones and write fence."""
        return super().send(method, params=params, session_id=session_id, timeout=timeout)

    # @anchor:wa.perceive.cdp_port.CdpPort.attach_url
    def attach_url(self, url_prefix):
        """Attach (flatten) to the first existing page whose URL starts with url_prefix; return its sessionId."""
        targets = self.send("Target.getTargets").get("targetInfos", [])
        pages = [t for t in targets if t.get("type") == "page" and t.get("url", "").startswith(url_prefix)]
        if not pages:
            raise CdpError("no open page starts with %r" % (url_prefix,))
        sid = self.send("Target.attachToTarget", {"targetId": pages[0]["targetId"], "flatten": True})["sessionId"]
        self._attached.append(sid)
        self.send("Page.enable", session_id=sid)
        return sid

    # @anchor:wa.perceive.cdp_port.CdpPort.ensure_tab
    def ensure_tab(self, url_prefix, open_url, selected_target=None, timeout=None):
        """Open or reuse a unique matching page target on the same HTTPS origin, attach, enable, and navigate.

        Validates HTTPS origin, lowercase hostname, effective port, credentials rejection, and same-origin
        open_url before sending any CDP command. Matches only page targets on the configured origin and
        prefix, rejecting deceptive hosts and non-page targets. Reuses a unique match without creating a tab,
        rejects multiple matches without a valid explicit selected_target, and rejects an invalid explicit
        hint without fallback creation. On a true miss, creates once at open_url, attaches flattened,
        records acknowledged ownership immediately, enables Page, and directly navigates on that session
        with correlated load observation.
        """
        prefix_origin, canon_prefix = _canonical_https_url(url_prefix)
        route_origin, _ = _canonical_https_url(open_url)
        if route_origin != prefix_origin:
            raise CdpTabError("requested route is outside the configured site origin")
        if selected_target is not None and (not isinstance(selected_target, str) or not selected_target.strip()):
            raise CdpTabError("explicit target is not a matching page")

        targets_reply = self.send("Target.getTargets")
        targets = targets_reply.get("targetInfos", []) if isinstance(targets_reply, dict) else []
        matches = [
            t for t in targets
            if isinstance(t, dict)
            and t.get("type") == "page"
            and isinstance(t.get("targetId"), str)
            and bool(t.get("targetId"))
            and _matches_site_prefix(t.get("url", ""), url_prefix, prefix_origin, canon_prefix)
        ]

        if selected_target is not None:
            matches = [t for t in matches if t.get("targetId") == selected_target]
            if len(matches) != 1:
                raise CdpTabError("explicit target is not a matching page")
        elif len(matches) > 1:
            raise CdpTabError("AMBIGUOUS_TARGET: explicit selection required")

        if matches:
            target_id = matches[0]["targetId"]
            target_owned = False
        else:
            created = self.send("Target.createTarget", {"url": open_url})
            tid = created.get("targetId") if isinstance(created, dict) else None
            if (
                not isinstance(tid, str)
                or not tid
                or any(isinstance(t, dict) and t.get("targetId") == tid for t in targets)
            ):
                raise CdpTabError("creation outcome has no fresh target identity")
            target_id = tid
            target_owned = True
            if not hasattr(self, "_owned_targets") or self._owned_targets is None:
                self._owned_targets = []
            self._owned_targets.append(target_id)

        attached = self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        sid = attached.get("sessionId") if isinstance(attached, dict) else None
        if (
            not isinstance(sid, str)
            or not sid
            or sid in self._attached
            or sid in getattr(self, "_caller_sessions", ())
        ):
            raise CdpTabError("attach outcome has no fresh session identity")
        self._attached.append(sid)

        self.send("Page.enable", session_id=sid)
        self.navigate(sid, open_url, timeout=timeout)

        return TabHandle(
            target_id=target_id,
            session_id=sid,
            target_owned=target_owned,
            session_owned=True,
            url_prefix=url_prefix,
            open_url=open_url,
            origin=prefix_origin,
            _port=self,
        )

    def attach_page(self):
        sid = super().attach_page()
        self._attached.append(sid)
        return sid

    def detach_session(self, session_id):
        """Detach only an acknowledged helper session, without bypassing STOP."""
        if not isinstance(session_id, str) or not session_id:
            return
        if session_id in getattr(self, "_caller_sessions", ()):
            return
        if session_id not in self._attached:
            return
        try:
            self.send("Target.detachFromTarget", {"sessionId": session_id}, timeout=5)
        except Exception:
            # Keep ownership evidence if transport cleanup was blocked/uncertain.
            return
        self._attached = [s for s in self._attached if s != session_id]

    def close(self):
        """Only detach owned sessions and close this socket; never Browser.close.

        A stopped shared fence also defers socket cleanup, retaining the owned
        attachment list for explicit reconciliation rather than bypassing STOP.
        """
        caller_sessions = getattr(self, "_caller_sessions", ()) or ()
        for sid in tuple(dict.fromkeys(self._attached)):
            if sid not in caller_sessions:
                self.detach_session(sid)
        try:
            with self._writer_guard():
                ws = self._ws
                if ws is not None:
                    ws.close()
                self._ws = None
                self._reader = None
        except CdpError:
            return
        with self._cv:
            self._closed = True
            self._cv.notify_all()
