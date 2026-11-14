# @module {"id":"wa.perceive.electron_cdp_bridge","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.cdp_pipe","wa.perceive.ax","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Injected Electron CDP target/session bridge.

This module never launches a browser, opens a port or infers Electron parentage.
The caller supplies a live CDP-compatible transport (CdpPipe, CdpPort, or a
strict offline fake) and, optionally, explicit parent and business records.
Only successfully read DOM *and* AX data may produce a CDP-sourced STATE.
"""

from dataclasses import dataclass
import copy
import threading

from . import ax
from .session_registry import Handle
from ..state import schema


class BridgeError(ValueError):
    """Invalid, absent or unproven CDP evidence/identity."""


class StaleTargetError(BridgeError):
    """The target generation or flattened CDP session is no longer current."""


def _token(value, label):
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise BridgeError("%s must be a nonempty canonical string" % label)
    return value


@dataclass(frozen=True)
class PageTarget:
    target_id: str
    url: str
    title: str
    parent_id: str | None = None

    def __post_init__(self):
        _token(self.target_id, "target_id")
        if not isinstance(self.url, str) or not isinstance(self.title, str):
            raise BridgeError("page url/title must be strings")
        if self.parent_id is not None:
            _token(self.parent_id, "parent_id")


@dataclass(frozen=True)
class TargetBinding:
    target_id: str
    session_id: str
    generation: int
    handle: Handle
    url: str
    title: str
    parent_id: str | None
    session_owned: bool = True
    target_owned: bool = False

    def __post_init__(self):
        _token(self.target_id, "target_id")
        _token(self.session_id, "session_id")
        if type(self.generation) is not int or self.generation < 1:
            raise BridgeError("generation must be an integer >= 1")
        if (self.handle.kind, self.handle.canonical_id, self.handle.generation) != (
            "cdp_tab", self.target_id, self.generation
        ):
            raise BridgeError("handle does not identify this target generation")
        if not isinstance(self.url, str) or not isinstance(self.title, str):
            raise BridgeError("url and title must be strings")
        if self.parent_id is not None:
            _token(self.parent_id, "parent_id")
        if type(self.session_owned) is not bool or type(self.target_owned) is not bool:
            raise BridgeError("ownership flags must be bool")


class ElectronCdpBridge:
    """Concurrent, fail-closed target owner on top of an injected CDP transport.

    Each successful attach assigns a monotonically increasing, one-based
    generation to that target. Detach/crash/re-attach retires the complete old
    generation. No object is accepted just because its sessionId still matches.
    """

    BUSINESS_SCHEMA = "wender.electron-business/v1"

    def __init__(self, transport, *, parent_by_target=None, business=None):
        if not callable(getattr(transport, "send", None)):
            raise BridgeError("transport must implement send(method, params, session_id)")
        self.transport = transport
        self._lock = threading.RLock()
        self._by_target = {}
        self._by_session = {}
        self._next_generation = {}
        self._retired_sessions = set()
        parents = {} if parent_by_target is None else parent_by_target
        if not isinstance(parents, dict):
            raise BridgeError("parent_by_target must be an explicit mapping")
        for target_id, parent in parents.items():
            _token(target_id, "parent target_id")
            _token(parent, "parent_id")
        self._parents = dict(parents)
        if business is not None and not isinstance(business, dict):
            raise BridgeError("business identity must be an injected mapping")
        self._business = copy.deepcopy(business) if business is not None else {}

    @classmethod
    def from_pipe(cls, pipe, **kwargs):
        """Bind to an already injected pipe configured for Chromium FD 3/4."""
        return cls(pipe, **kwargs)

    @classmethod
    def from_port(cls, port, **kwargs):
        """Bind to an already configured CDP port; do not open a socket here."""
        return cls(port, **kwargs)

    def discover_pages(self):
        """Discover real page targets; never turn openerId into a parent claim."""
        reply = self.transport.send("Target.getTargets", {}, session_id=None)
        if not isinstance(reply, dict) or not isinstance(reply.get("targetInfos"), list):
            raise BridgeError("Target.getTargets returned no targetInfos")
        pages = []
        seen = set()
        for info in reply["targetInfos"]:
            if not isinstance(info, dict) or info.get("type") != "page":
                continue
            tid = _token(info.get("targetId"), "targetId")
            if tid in seen:
                raise BridgeError("duplicate page targetId")
            seen.add(tid)
            pages.append(PageTarget(tid, info.get("url", ""), info.get("title", ""),
                                    self._parents.get(tid)))
        return tuple(pages)

    def _invalidate_locked(self, target_id):
        old = self._by_target.pop(target_id, None)
        if old is not None:
            self._by_session.pop(old.session_id, None)
            self._retired_sessions.add(old.session_id)
        return old

    def invalidate_target(self, target_id):
        """Locally retire a target generation (no CDP or network side effects)."""
        _token(target_id, "target_id")
        with self._lock:
            return self._invalidate_locked(target_id) is not None

    def invalidate_all(self):
        """Connection reset: fence every session without resetting generations."""
        with self._lock:
            for target_id in tuple(self._by_target):
                self._invalidate_locked(target_id)

    def _bind_locked(self, page, session_id, *, session_owned, target_owned):
        sid = _token(session_id, "flattened sessionId")
        if sid in self._by_session or sid in self._retired_sessions:
            raise BridgeError("CDP sessionId is live elsewhere or already retired")
        # Never create a generation-zero Handle: the landed registry rejects it.
        generation = self._next_generation.get(page.target_id, 1)
        handle = Handle("cdp_tab", page.target_id, generation)
        binding = TargetBinding(page.target_id, sid, generation, handle, page.url,
                                page.title, page.parent_id, session_owned, target_owned)
        self._next_generation[page.target_id] = generation + 1
        self._by_target[page.target_id] = binding
        self._by_session[sid] = binding
        return binding

    def attach_target(self, target_id):
        """Attach flattened to one discovered page, failing closed on ambiguity."""
        _token(target_id, "target_id")
        candidates = [p for p in self.discover_pages() if p.target_id == target_id]
        if len(candidates) != 1:
            raise BridgeError("target must be exactly one discovered page")
        with self._lock:
            self._invalidate_locked(target_id)  # reattachment fences the old generation
            reply = self.transport.send("Target.attachToTarget",
                                        {"targetId": target_id, "flatten": True},
                                        session_id=None)
            if not isinstance(reply, dict):
                raise BridgeError("attach did not acknowledge a flattened session")
            return self._bind_locked(candidates[0], reply.get("sessionId"),
                                     session_owned=True, target_owned=False)

    def attach_port_tab(self, url_prefix, open_url, *, selected_target=None, timeout=None):
        """Use an injected CdpPort.ensure_tab; never create an endpoint ourselves."""
        ensure = getattr(self.transport, "ensure_tab", None)
        if not callable(ensure):
            raise BridgeError("port transport must implement ensure_tab")
        with self._lock:
            tab = ensure(url_prefix, open_url, selected_target=selected_target, timeout=timeout)
            tid = _token(getattr(tab, "target_id", None), "port target_id")
            sid = _token(getattr(tab, "session_id", None), "port session_id")
            self._invalidate_locked(tid)
            page = PageTarget(tid, open_url, "", self._parents.get(tid))
            return self._bind_locked(page, sid,
                                     session_owned=getattr(tab, "session_owned", False),
                                     target_owned=getattr(tab, "target_owned", False))

    def current(self, binding):
        """Identity check includes the immutable Handle, generation and session."""
        if not isinstance(binding, TargetBinding):
            return False
        with self._lock:
            live = self._by_target.get(binding.target_id)
            return (live is binding and self._by_session.get(binding.session_id) is binding)

    def _check(self, binding):
        if not self.current(binding):
            raise StaleTargetError("target generation or flattened session is stale")

    def _send_checked(self, binding, method, params):
        self._check(binding)
        reply = self.transport.send(method, params, session_id=binding.session_id)
        self._check(binding)
        if not isinstance(reply, dict):
            raise BridgeError("invalid CDP response for %s" % method)
        # Some injected transports expose a result-level provenance marker.
        # Never accept an explicitly foreign session even if send was scoped.
        if "sessionId" in reply and reply["sessionId"] != binding.session_id:
            raise StaleTargetError("foreign-session CDP evidence rejected")
        return reply

    def read_state(self, binding):
        """Derive closed STATE only from current-session DOM and full AX responses."""
        self._check(binding)
        dom = self._send_checked(binding, "DOM.getDocument", {"depth": 0})
        root = dom.get("root")
        if (not isinstance(root, dict) or root.get("nodeType") != 9
                or type(root.get("nodeId")) is not int or root["nodeId"] <= 0):
            raise BridgeError("valid DOM.getDocument document evidence required")
        url = root.get("documentURL", binding.url)
        if not isinstance(url, str):
            raise BridgeError("DOM documentURL must be a string")
        enabled = False
        try:
            self._send_checked(binding, "Accessibility.enable", {})
            enabled = True
            ax_reply = self._send_checked(binding, "Accessibility.getFullAXTree", {})
        finally:
            if enabled and self.current(binding):
                # Release the acknowledged AX domain, never on a retired session.
                self._send_checked(binding, "Accessibility.disable", {})
        raw = ax_reply.get("nodes")
        if (not isinstance(raw, list) or not raw
                or any(not isinstance(n, dict) or not isinstance(n.get("nodeId"), str)
                       or not n["nodeId"] for n in raw)):
            raise BridgeError("full AX nodes with CDP node identities required")
        nodes = ax.prune(raw)
        if not nodes:
            raise BridgeError("AX tree contains no emitted accessibility evidence")
        state = schema.build_state(
            nodes, kind="cdp", url=url, title=ax.normalize_name(binding.title),
            ax_sha256=ax.snapshot_sha(nodes, True),
            ax_stable_sha256=ax.snapshot_sha(nodes, False),
            prune={"name": ax.PRUNE_NAME, "version": ax.PRUNE_VERSION},
            raw_count=len(raw), actionable_roles=ax.ACTIONABLE_ROLES,
        )
        self._check(binding)  # disallow racing detaches during prune / STATE build
        return state

    def target_descriptor(self, binding, *, frame_seq=0, dpr_ppm=1000000):
        state = self.read_state(binding)
        self._check(binding)
        return schema.build_target_descriptor(binding.handle, binding.generation,
                                              state, frame_seq=frame_seq,
                                              dpr_ppm=dpr_ppm)

    def business_envelope(self, binding):
        """Separate versioned identity, deliberately not a STATE/source extension."""
        self._check(binding)
        return {"schema": self.BUSINESS_SCHEMA,
                "target": {"id": binding.target_id,
                           "generation": binding.generation,
                           "session_id": binding.session_id,
                           "parent_id": binding.parent_id},
                "business": copy.deepcopy(self._business)}

    def accept_event(self, event):
        """Consume injected CDP detach/crash/reattach events with session fences."""
        if not isinstance(event, dict) or not isinstance(event.get("params", {}), dict):
            raise BridgeError("CDP event must be a dict with dict params")
        method = event.get("method")
        params = event.get("params") or {}
        sid = params.get("sessionId") or event.get("sessionId")
        tid = params.get("targetId")
        with self._lock:
            if method in ("Target.detachedFromTarget", "Inspector.detached"):
                bound = self._by_session.get(sid)
                if bound is None or (tid is not None and tid != bound.target_id):
                    return False
                return self._invalidate_locked(bound.target_id) is not None
            if method in ("Target.targetCrashed", "Inspector.targetCrashed"):
                bound = self._by_target.get(tid) if tid is not None else self._by_session.get(sid)
                if bound is None or (sid is not None and sid != bound.session_id):
                    return False
                return self._invalidate_locked(bound.target_id) is not None
            if method == "Target.attachedToTarget":
                info = params.get("targetInfo") or {}
                reattached_target = info.get("targetId") if isinstance(info, dict) else None
                bound = self._by_target.get(reattached_target)
                if bound is not None and isinstance(sid, str) and sid != bound.session_id:
                    return self._invalidate_locked(reattached_target) is not None
        return False

    def detach(self, binding):
        """Retire locally first; remotely detach only sessions we attached."""
        self._check(binding)
        with self._lock:
            self._check(binding)
            self._invalidate_locked(binding.target_id)
        if binding.session_owned:
            self.transport.send("Target.detachFromTarget", {"sessionId": binding.session_id},
                                session_id=None)
