# @module {"id":"wa.perceive.browser","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.ax","wa.state.schema","wa.js.inpage.viewport"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.browser - rung 2 for web pages: CDP session -> pruned AX -> STATE.

Only the STATE leaves this module; raw AX/HTML never does.
"""
import json
import threading
from pathlib import Path

from . import ax
from ..state import schema

INPAGE_DIR = Path(__file__).resolve().parents[2] / "js" / "inpage"

# Shared value contract and proposed immutable records
try:
    from wender.perceive.session_registry import (
        ActionRequest,
        ActorLease,
        ChangeResult,
        FocusLease,
        FrameRef,
        Handle,
        Observation,
        PerceptionResult,
        Receipt,
        StreamSpec,
        StreamToken,
        TargetRecord,
        Ticket,
        WindowDescription,
        WindowFrames,
    )
except ModuleNotFoundError as _err:
    if getattr(_err, "name", None) not in (
        None,
        "wender.perceive.session_registry",
        "session_registry",
    ):
        raise
    from dataclasses import dataclass
    import numpy

    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    @dataclass(frozen=True)
    class TargetRecord:
        handle: Handle
        transport: object
        owned: bool
        epoch: int
        session_id: str | None
        attachment_owned: bool
        closed: bool

    @dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str

    @dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: FrameRef

    @dataclass(frozen=True)
    class Receipt:
        action_id: str
        handle: Handle
        status: str
        reason: str | None

    @dataclass(frozen=True)
    class ActionRequest:
        handle: Handle
        action_id: str
        epoch: int
        payload: dict
        cost: int
        deadline_ns: int

    @dataclass(frozen=True)
    class ActorLease:
        handle: Handle
        owner: str
        generation: int
        nonce: bytes
        expires_ns: int

    @dataclass(frozen=True)
    class FocusLease:
        lease: ActorLease
        domain: str
        epoch: int

    @dataclass(frozen=True)
    class StreamSpec:
        width: int
        height: int
        slots: int
        quota_bytes: int

    @dataclass(frozen=True)
    class StreamToken:
        handle: Handle
        epoch: int

    @dataclass(frozen=True)
    class Ticket:
        action_id: str
        handle: Handle

    @dataclass(frozen=True)
    class ChangeResult:
        handle: Handle
        epoch: int
        settled: bool
        claims: numpy.ndarray
        ops: dict

    @dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Observation | None
        error: str | None

    @dataclass(frozen=True)
    class WindowFrames:
        handle: Handle
        epoch: int
        frames: tuple
        frame_seqs: tuple
        dpr_ppm: int

    @dataclass(frozen=True)
    class WindowDescription:
        handle: Handle
        epoch: int
        frame_seqs: tuple
        descriptors: tuple
        regions: tuple
        error: str | None


SUPPORTED_TARGET_KINDS = frozenset(
    {"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"}
)


# @anchor:wa.perceive.browser.HandleError
class HandleError(ValueError):
    """Base error for handle resolution and validation failures."""
    pass


# @anchor:wa.perceive.browser.StaleGenerationError
class StaleGenerationError(HandleError):
    """Raised when a handle has a stale generation."""
    pass


# @anchor:wa.perceive.browser.EpochChangedError
class EpochChangedError(HandleError):
    """Raised when the target epoch changed before returning."""
    pass


# @anchor:wa.perceive.browser.SessionDisagreementError
class SessionDisagreementError(HandleError):
    """Raised when explicit session_id disagrees with resolved handle session."""
    pass


# @anchor:wa.perceive.browser.AmbiguousDiscoveryError
class AmbiguousDiscoveryError(HandleError):
    """Raised when handle discovery or resolution is ambiguous."""
    pass


# @anchor:wa.perceive.browser.SharedSessionError
class SharedSessionError(HandleError):
    """Raised when sibling browser handles reuse a connection's session cursor."""
    pass


# @anchor:wa.perceive.browser.PerceptionFocusError
class PerceptionFocusError(HandleError):
    """Raised when a registry claims focus while resolving perception."""
    pass


_SESSION_BINDING_LOCK = threading.Lock()


def _bind_session_handle(cdp, session_id, handle):
    """Keep session ownership stable across sibling calls on one connection.

    Bind on first use, including for registries that expose only resolve(). The
    connection owns this metadata, so it expires with the transport. Generations
    of the same canonical tab may reuse its session after registry validation.
    """
    identity = (handle.kind, handle.canonical_id)
    with _SESSION_BINDING_LOCK:
        bindings = getattr(cdp, "_wender_browser_session_handles", None)
        if bindings is None:
            bindings = {}
            cdp._wender_browser_session_handles = bindings
        incumbent = bindings.get(session_id)
        if incumbent is not None and incumbent != identity:
            raise SharedSessionError(
                f"Session {session_id!r} is already bound to sibling handle {incumbent!r}"
            )
        bindings[session_id] = identity


def _check_perception_focus(registry):
    if getattr(registry, "active_focus_leases", ()):
        raise PerceptionFocusError("Registry claimed a focus lease during browser perception")


# @anchor:wa.perceive.browser.inpage_script
def inpage_script(name):
    return (INPAGE_DIR / name).read_text(encoding="utf-8")


def _quad_box(quad):
    xs, ys = quad[0::2], quad[1::2]
    x0, y0 = int(round(min(xs))), int(round(min(ys)))
    return [x0, y0, max(0, int(round(max(xs))) - x0), max(0, int(round(max(ys))) - y0)]


def _resolve_session(cdp, session_id=None, handle=None, registry=None):
    """Resolve session_id and validate explicit Handle via registry / tab-manager."""
    if handle is None:
        if session_id is None:
            raise ValueError("Either session_id or handle must be provided")
        return session_id, None, None

    # Handle must be an immutable Handle or duck-typed with kind, canonical_id, generation
    if not hasattr(handle, "kind") or not hasattr(handle, "canonical_id") or not hasattr(handle, "generation"):
        raise ValueError(f"Invalid handle specification: {handle!r}")

    if handle.kind not in SUPPORTED_TARGET_KINDS:
        raise ValueError(f"Unsupported handle kind: {handle.kind!r}")

    if handle.kind != "cdp_tab":
        raise ValueError(f"Browser perception requires cdp_tab handle, got {handle.kind!r}")

    # Fall back to cdp.registry if registry is omitted
    if registry is None:
        registry = getattr(cdp, "registry", None)

    resolved_session = None
    initial_epoch = None
    record = None

    if registry is not None:
        # Check ambiguous discovery flag or error
        if getattr(registry, "ambiguous", False):
            raise AmbiguousDiscoveryError("Ambiguous handle discovery in registry")

        tab_mgr = getattr(registry, "tab_manager", None) or getattr(cdp, "tab_manager", None)

        if hasattr(registry, "resolve"):
            record = registry.resolve(handle)
        elif hasattr(registry, "get_record"):
            record = registry.get_record(handle)
        elif hasattr(registry, "get"):
            record = registry.get(handle)
        elif hasattr(registry, "__getitem__"):
            try:
                record = registry[handle]
            except KeyError:
                record = None
        elif callable(registry):
            record = registry(handle)

        if getattr(record, "ambiguous", False):
            raise AmbiguousDiscoveryError("Ambiguous target record resolved")

        if record is None and tab_mgr is not None:
            if hasattr(tab_mgr, "resolve"):
                record = tab_mgr.resolve(handle)
            elif hasattr(tab_mgr, "session_for_handle"):
                resolved_session = tab_mgr.session_for_handle(handle)
            elif hasattr(tab_mgr, "session_mapping") and isinstance(tab_mgr.session_mapping, dict):
                resolved_session = tab_mgr.session_mapping.get(handle)

        if record is None and resolved_session is None:
            raise ValueError(f"Handle not found in registry: {handle!r}")

        if record is not None:
            if isinstance(record, str):
                resolved_session = record
                initial_epoch = None
            else:
                if getattr(record, "closed", False):
                    raise ValueError(f"Target record for handle {handle!r} is closed")

                rec_handle = getattr(record, "handle", handle)
                rec_gen = getattr(rec_handle, "generation", None)
                if rec_gen is not None and handle.generation < rec_gen:
                    raise StaleGenerationError(
                        f"Stale handle generation {handle.generation} < {rec_gen}"
                    )

                resolved_session = getattr(record, "session_id", None)
                initial_epoch = getattr(record, "epoch", None)

        # TabManager session mapping corroboration
        if tab_mgr is not None:
            tab_session = None
            if hasattr(tab_mgr, "session_for_handle"):
                tab_session = tab_mgr.session_for_handle(handle)
            elif hasattr(tab_mgr, "session_mapping") and isinstance(tab_mgr.session_mapping, dict):
                tab_session = tab_mgr.session_mapping.get(handle)

            if tab_session is not None:
                if resolved_session is not None and resolved_session != tab_session:
                    raise SessionDisagreementError(
                        f"TabManager session {tab_session!r} disagrees with registry session {resolved_session!r}"
                    )
                resolved_session = tab_session
    else:
        if session_id is not None:
            resolved_session = session_id
        else:
            raise ValueError("Registry is required to resolve explicit handle without session_id")

    if not resolved_session:
        raise ValueError(f"Could not resolve session_id for handle {handle!r}")

    if session_id is not None and session_id != resolved_session:
        raise SessionDisagreementError(
            f"Explicit session_id {session_id!r} disagrees with resolved handle session {resolved_session!r}"
        )

    _check_perception_focus(registry)
    _bind_session_handle(cdp, resolved_session, handle)
    return resolved_session, initial_epoch, record


def _check_epoch_and_generation(handle, registry, initial_epoch, initial_record):
    """Validate handle generation and epoch before returning from perception helper."""
    if handle is None or registry is None:
        return

    current_record = None
    if hasattr(registry, "resolve"):
        current_record = registry.resolve(handle)
    elif hasattr(registry, "get_record"):
        current_record = registry.get_record(handle)
    elif hasattr(registry, "get"):
        current_record = registry.get(handle)
    elif hasattr(registry, "__getitem__"):
        try:
            current_record = registry[handle]
        except KeyError:
            current_record = None
    elif callable(registry):
        current_record = registry(handle)

    _check_perception_focus(registry)

    if current_record is not None:
        rec_handle = getattr(current_record, "handle", handle)
        cur_gen = getattr(rec_handle, "generation", None)
        if cur_gen is not None and handle.generation < cur_gen:
            raise StaleGenerationError(
                f"Stale handle generation {handle.generation} < {cur_gen} before returning"
            )

        cur_epoch = getattr(current_record, "epoch", None)
        if initial_epoch is not None and cur_epoch is not None and cur_epoch != initial_epoch:
            raise EpochChangedError(
                f"Epoch changed during perception: initial {initial_epoch} != current {cur_epoch}"
            )


# @anchor:wa.perceive.browser.boxes
def boxes(cdp, session_id=None, nodes=(), *, handle=None, registry=None):
    """Border boxes (integer CSS px) of emitted nodes via DOM.getBoxModel; null if none."""
    if registry is None:
        registry = getattr(cdp, "registry", None)
    resolved_session, initial_epoch, initial_record = _resolve_session(
        cdp, session_id=session_id, handle=handle, registry=registry
    )
    cdp.send("DOM.getDocument", {"depth": 0}, session_id=resolved_session)
    out = {}
    for n in nodes:
        bid = n.get("backend_id")
        if bid is None:
            continue
        try:
            m = cdp.send("DOM.getBoxModel", {"backendNodeId": bid}, session_id=resolved_session)
        except Exception:  # node has no layout box (display:none, document, ...)
            continue
        q = (m.get("model") or {}).get("border")
        if q and len(q) == 8:
            out[n["id"]] = _quad_box(q)
    _check_epoch_and_generation(handle, registry, initial_epoch, initial_record)
    return out


# @anchor:wa.perceive.browser.page_state
def page_state(cdp, session_id=None, with_boxes=True, *, handle=None, registry=None):
    """Rung 2 page perception: resolve explicit handle or session_id, prune AX, build STATE, release AX."""
    if registry is None:
        registry = getattr(cdp, "registry", None)
    resolved_session, initial_epoch, initial_record = _resolve_session(
        cdp, session_id=session_id, handle=handle, registry=registry
    )
    raw = ax.raw_tree(cdp, resolved_session)
    failed = False
    try:
        nodes = ax.prune(raw)
        info = cdp.evaluate(resolved_session, "JSON.stringify([location.href, document.title])")
        url, title = json.loads(info)
        vp = json.loads(
            cdp.evaluate(
                resolved_session,
                inpage_script("viewport.js") + "\nJSON.stringify(wenderViewport(window))",
            )
        )
        bx = (
            boxes(
                cdp,
                session_id=resolved_session,
                nodes=nodes,
                handle=handle,
                registry=registry,
            )
            if with_boxes
            else {}
        )
        _check_epoch_and_generation(handle, registry, initial_epoch, initial_record)
        state = schema.build_state(
            nodes,
            kind="cdp",
            url=url,
            title=ax.normalize_name(title),
            ax_sha256=ax.snapshot_sha(nodes, True),
            ax_stable_sha256=ax.snapshot_sha(nodes, False),
            prune={"name": ax.PRUNE_NAME, "version": ax.PRUNE_VERSION},
            raw_count=len(raw),
            boxes=bx,
            viewport=vp,
            actionable_roles=ax.ACTIONABLE_ROLES,
        )
        _check_epoch_and_generation(handle, registry, initial_epoch, initial_record)
    except BaseException:
        failed = True
        raise
    finally:
        try:
            # release's False result remains best-effort; an unexpected exception
            # propagates only when there is no original construction failure.
            ax.release(cdp, resolved_session)
        except BaseException:
            if not failed:
                raise
    return state
