"""Passive X11 and XInput2 subscriptions on a positively bound private display."""

from contextlib import contextmanager
import ctypes as C
import os
import re
import socket
from struct import unpack
from time import monotonic_ns

from . import CoverageLost, Readback


_U8, _U16, _U32 = C.c_uint8, C.c_uint16, C.c_uint32
_PTR, _OUT = C.c_void_p, C.POINTER(C.c_void_p)
_INT = C.c_int
_PROPERTY_MASK = 1 << 22  # PropertyChange
_WINDOW_MASK = (1 << 21) | (1 << 19)  # FocusChange, SubstructureNotify
_TOPOLOGY = {16, 17, 21}  # CreateNotify, DestroyNotify, ReparentNotify
_FOCUS = {9: "FocusIn", 10: "FocusOut"}
_XI_RAW = {13, 14, 15, 16, 17, 22, 23, 24}  # Raw key, button, motion and touch
_MAX_WINDOWS = 4096  # [TARGET] bounded subscription
_MAX_EVENTS = 32768  # [TARGET] refuse beyond this many queued events per interval
_ACTIVE_WINDOW = b"_NET_ACTIVE_WINDOW"


class _Cookie(C.Structure):
    _fields_ = [("sequence", _U32)]


class _Screens(C.Structure):
    _fields_ = [("data", _PTR), ("remaining", _INT), ("index", _INT)]


class _XIMask(C.Structure):
    _fields_ = [("device", _U16), ("length", _U16), ("bits", _U32)]


def _declare(lib, name, result, *arguments):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = result, arguments
    return fn


class _XCB:
    def __init__(self):
        self.core = core = C.CDLL("libxcb.so.1")
        self.xi = xi = C.CDLL("libxcb-xinput.so.0")
        self.free = _declare(C.CDLL(None), "free", None, _PTR)
        self.extension = _U8.in_dll(xi, "xcb_input_id")

        for name, result, args in (
            ("connect", _PTR, (C.c_char_p, C.POINTER(_INT))),
            ("connection_has_error", _INT, (_PTR,)),
            ("disconnect", None, (_PTR,)),
            ("get_file_descriptor", _INT, (_PTR,)),
            ("get_setup", _PTR, (_PTR,)),
            ("setup_roots_iterator", _Screens, (_PTR,)),
            ("get_extension_data", _PTR, (_PTR, _PTR)),
            ("request_check", _PTR, (_PTR, _Cookie)),
            ("poll_for_queued_event", _PTR, (_PTR,)),
            ("change_window_attributes_checked", _Cookie, (_PTR, _U32, _U32, C.POINTER(_U32))),
            ("query_tree_children", C.POINTER(_U32), (_PTR,)),
            ("get_property_value", _PTR, (_PTR,)),
        ):
            _declare(core, "xcb_" + name, result, *args)
        for name, args in (
            ("intern_atom", (_U8, _U16, C.c_char_p)),
            ("query_tree", (_U32,)),
            ("get_property", (_U8, _U32, _U32, _U32, _U32, _U32)),
            ("get_input_focus", ()),
            ("query_pointer", (_U32,)),
        ):
            _declare(core, "xcb_" + name, _Cookie, _PTR, *args)
            _declare(core, "xcb_" + name + "_reply", _PTR, _PTR, _Cookie, _OUT)
        _declare(xi, "xcb_input_xi_query_version", _Cookie, _PTR, _U16, _U16)
        _declare(xi, "xcb_input_xi_query_version_reply", _PTR, _PTR, _Cookie, _OUT)
        _declare(xi, "xcb_input_xi_select_events_checked", _Cookie, _PTR, _U32, _U16, C.POINTER(_XIMask))


def _field(address, offset, field_type=_U32):
    return field_type.from_address(address + offset).value


def _decode_event(address, *, root, active_atom, xi_opcode):
    """Return only the fields needed by the trace. Never read XI raw payloads."""
    response = _field(address, 0, _U8)
    if response == 0 or response & 0x80 or response in _TOPOLOGY:
        return "gap", None
    if response in _FOCUS:
        return _FOCUS[response], _field(address, 4)
    if response == 28 and _field(address, 4) == root and _field(address, 8) == active_atom:
        # XCB adds full_sequence after the core event. PropertyNotify has no value.
        return "_NET_ACTIVE_WINDOW", _field(address, 32)
    if response == 35 and _field(address, 1, _U8) == xi_opcode:
        event_type = _field(address, 8, _U16)
        if event_type in _XI_RAW:
            return "XInput2", None
        if event_type in _FOCUS:
            return _FOCUS[event_type], _field(address, 24)
    return None, None


def _readback(events, *, active_before, active_after, property_barrier,
              focus_before, focus_after):
    # One notification preceding the property reply has an attributable value.
    # Multiple writes cannot be reconstructed from a single round-trip read.
    property_events = [event for event in events if event[1] == "_NET_ACTIVE_WINDOW"]
    if len(property_events) == 1:
        distance = (property_barrier - property_events[0][2]) & 0xffffffff
        property_known = 0 < distance < 0x80000000
    else:
        property_known = not property_events and active_before == active_after
    focus_seen = any(kind in {"FocusIn", "FocusOut"} for _, kind, _ in events)
    complete = property_known and (focus_before == focus_after or focus_seen)
    trace = []
    for stamp, kind, value in events:
        if kind == "gap":
            complete = False
        elif kind == "XInput2":
            trace.append({"kind": kind, "mono_ns": stamp})
        elif kind in {"FocusIn", "FocusOut"}:
            trace.append({"kind": kind, "mono_ns": stamp, "xid": hex(value)})
        elif property_known:
            trace.append({"kind": kind, "mono_ns": stamp, "xid": hex(active_after)})
    return Readback(tuple(trace), complete)


class X11FocusSource:
    """One event interval, with setup and completion fenced by X server replies."""

    def __init__(self, display, server_pid):
        if (not isinstance(display, str) or not re.fullmatch(r":[1-9][0-9]*", display)
                or type(server_pid) is not int or server_pid <= 0):
            raise CoverageLost("a private local X display and its server PID are required")
        self.display, self._server_pid = display, server_pid
        self._connection = None

    def __enter__(self):
        if self._connection is not None:
            raise CoverageLost("the X11 event source is already in use")
        try:
            self._api = _XCB()
        except (AttributeError, OSError, ValueError) as exc:
            raise CoverageLost("X11 or the XI2 client library is unavailable") from exc
        screen = _INT()
        self._connection = self._api.core.xcb_connect(self.display.encode("ascii"), C.byref(screen))
        try:
            self._check_connection()
            fd = self._api.core.xcb_get_file_descriptor(self._connection)
            with socket.socket(fileno=os.dup(fd)) as peer:
                pid, uid, _ = unpack("=iII", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid != self._server_pid or uid != os.geteuid():
                raise CoverageLost("the private X server socket peer did not match")
            setup = self._api.core.xcb_get_setup(self._connection)
            screens = self._api.core.xcb_setup_roots_iterator(setup)
            if screen.value != 0 or screens.remaining != 1:
                raise CoverageLost("the private X display requires a single screen")
            self._root = _field(screens.data, 0)
            self._baseline = None
            return self
        except BaseException:
            self._close()
            raise

    def __exit__(self, *_):
        self._close()

    def _close(self):
        if self._connection is not None:
            self._api.core.xcb_disconnect(self._connection)
            self._connection = None

    def _check_connection(self):
        if not self._connection or self._api.core.xcb_connection_has_error(self._connection):
            raise CoverageLost("the X11 event connection was lost")

    @contextmanager
    def _reply(self, name, *args, extension=False):
        lib = self._api.xi if extension else self._api.core
        cookie = getattr(lib, name)(self._connection, *args)
        error = _PTR()
        reply = getattr(lib, name + "_reply")(self._connection, cookie, C.byref(error))
        try:
            if error or not reply:
                raise CoverageLost("an X11 proof request failed")
            yield reply, cookie.sequence
        finally:
            self._api.free(error)
            self._api.free(reply)

    def _check_request(self, cookie):
        error = self._api.core.xcb_request_check(self._connection, cookie)
        if error:
            self._api.free(error)
            raise CoverageLost("the X11 subscription was rejected")
        self._check_connection()

    def _init_input(self):
        extension = self._api.core.xcb_get_extension_data(
            self._connection, C.byref(self._api.extension),
        )
        if not extension or not _field(extension, 8, _U8):
            raise CoverageLost("XInput2 is unavailable")
        self._xi_opcode = _field(extension, 9, _U8)
        with self._reply("xcb_input_xi_query_version", 2, 2, extension=True) as (reply, _):
            if (_field(reply, 8, _U16), _field(reply, 10, _U16)) < (2, 2):
                raise CoverageLost("XInput 2.2 coverage is unavailable")

    def _watch_tree(self):
        pending, watched = [self._root], set()
        while pending:
            xid = pending.pop()
            if xid in watched or len(watched) == _MAX_WINDOWS:
                raise CoverageLost("the X11 window tree is unstable or exceeds the watch budget")
            watched.add(xid)
            mask = _U32(_WINDOW_MASK)
            xi_events = set(_FOCUS)
            if xid == self._root:
                mask.value |= _PROPERTY_MASK
                xi_events.update(_XI_RAW)
            self._check_request(self._api.core.xcb_change_window_attributes_checked(
                self._connection, xid, 1 << 11, C.byref(mask),  # CWEventMask
            ))
            xi_mask = _XIMask(1, 1, sum(1 << kind for kind in xi_events))  # XIAllMasterDevices
            self._check_request(self._api.xi.xcb_input_xi_select_events_checked(
                self._connection, xid, 1, C.byref(xi_mask),
            ))
            with self._reply("xcb_query_tree", xid) as (reply, _):
                count = _field(reply, 16, _U16)
                if len(watched) + len(pending) + count > _MAX_WINDOWS:
                    raise CoverageLost("the X11 window tree exceeds the watch budget")
                children = self._api.core.xcb_query_tree_children(reply)
                pending.extend(children[:count])
        return watched

    def _property(self):
        with self._reply("xcb_get_property", 0, self._root, self._atom, 33, 0, 1) as (reply, sequence):
            # WINDOW, format 32, exactly one nonzero XID.
            if (_field(reply, 1, _U8) != 32 or _field(reply, 8) != 33
                    or _field(reply, 12) != 0 or _field(reply, 16) != 1):
                raise CoverageLost("the active-window property has no unique XID")
            xid = _field(self._api.core.xcb_get_property_value(reply), 0)
            if not xid:
                raise CoverageLost("the active-window property has no window")
            return xid, sequence

    def _focus(self):
        with self._reply("xcb_get_input_focus") as (reply, _):
            return _field(reply, 8)

    def pointer(self):
        """Read the pointer for presentation readback on this same private root."""
        self._check_connection()
        with self._reply("xcb_query_pointer", self._root) as (reply, _):
            if _field(reply, 1, _U8) != 1:
                raise CoverageLost("the pointer is outside the traced X11 screen")
            return _field(reply, 16, C.c_int16), _field(reply, 18, C.c_int16)

    def _queued(self):
        self._check_connection()
        events = []
        for _ in range(_MAX_EVENTS):
            address = self._api.core.xcb_poll_for_queued_event(self._connection)
            if not address:
                self._check_connection()
                return events
            try:
                kind, value = _decode_event(
                    address, root=self._root, active_atom=self._atom, xi_opcode=self._xi_opcode,
                )
                if kind is not None:
                    events.append((monotonic_ns(), kind, value))
            finally:
                self._api.free(address)
        raise CoverageLost("the X11 event queue exceeded the trace budget")

    def arm(self):
        self._check_connection()
        self._init_input()
        with self._reply("xcb_intern_atom", 1, len(_ACTIVE_WINDOW), _ACTIVE_WINDOW) as (reply, _):
            self._atom = _field(reply, 8)
        if not self._atom:
            raise CoverageLost("the window manager has no active-window property")
        watched = self._watch_tree()
        focus = self._focus()
        active, _ = self._property()
        if (active not in watched or focus not in watched
                or any(kind != "XInput2" for _, kind, _ in self._queued())):
            raise CoverageLost("the X11 focus baseline changed while arming")
        self._baseline = active, focus
        return hex(active)

    def finish(self):
        self._check_connection()
        if self._baseline is None:
            raise CoverageLost("the X11 event stream was not armed")
        active_before, focus_before = self._baseline
        self._baseline = None
        focus_after = self._focus()
        active_after, barrier = self._property()
        return _readback(
            self._queued(), active_before=active_before, active_after=active_after,
            property_barrier=barrier, focus_before=focus_before, focus_after=focus_after,
        )
