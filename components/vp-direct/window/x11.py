"""X11 presentation through the workspace-only command qualified in L0-3."""

from ctypes import CDLL, POINTER, byref, c_char_p, c_int, c_uint, c_ulong, c_void_p
import json
import os
from pathlib import Path
import re
import subprocess

from . import DesktopState, WindowRefused, _UNAVAILABLE, _xid


def _pointer():
    """Read root coordinates without injecting input."""
    try:
        x11 = CDLL("libX11.so.6")
    except OSError as error:
        raise WindowRefused(_UNAVAILABLE, "X11 pointer readback is unavailable") from error
    x11.XOpenDisplay.argtypes = [c_char_p]
    x11.XOpenDisplay.restype = c_void_p
    x11.XDefaultRootWindow.argtypes = [c_void_p]
    x11.XDefaultRootWindow.restype = c_ulong
    x11.XQueryPointer.argtypes = [
        c_void_p, c_ulong, POINTER(c_ulong), POINTER(c_ulong), POINTER(c_int),
        POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_uint),
    ]
    x11.XCloseDisplay.argtypes = [c_void_p]
    display = x11.XOpenDisplay(b":0")
    if not display:
        raise WindowRefused(_UNAVAILABLE, "cannot open the operator's X display")
    try:
        root, child = c_ulong(), c_ulong()
        x, y, window_x, window_y, mask = c_int(), c_int(), c_int(), c_int(), c_uint()
        if not x11.XQueryPointer(
            display, x11.XDefaultRootWindow(display), byref(root), byref(child),
            byref(x), byref(y), byref(window_x), byref(window_y), byref(mask),
        ):
            raise WindowRefused(_UNAVAILABLE, "no root cursor coordinates")
        return x.value, y.value
    finally:
        x11.XCloseDisplay(display)


def _property(output, pattern):
    values = re.findall(pattern, output, re.MULTILINE)
    if len(values) != 1:
        raise WindowRefused(_UNAVAILABLE, "missing or ambiguous X11 property")
    return values[0]


def _cardinal(output, name):
    return int(_property(output, rf"^{name}\(CARDINAL\) = ([0-9]{{1,10}})$"))


def _window_rows(output):
    try:
        try:
            rows = json.loads(output)
        except json.JSONDecodeError:
            rows = [json.loads(line) for line in output.splitlines() if line.strip()]
        if isinstance(rows, dict):
            rows = rows.get("windows", rows.get("rows"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError
        return rows
    except ValueError as error:
        raise WindowRefused(_UNAVAILABLE, "host window inventory is malformed") from error


class WmctrlBackend:
    """Use the existing srf-host-window witness alongside EWMH readback."""

    def __init__(self, bin_dir, *, run=subprocess.run, pointer=_pointer):
        if not bin_dir or not Path(bin_dir).is_absolute():
            raise WindowRefused(_UNAVAILABLE, "MV_SRF_BIN_DIR is unavailable")
        self._host_tool = str(Path(bin_dir) / "srf-host-window")
        self._run = run
        self._pointer = pointer

    def _command(self, *argv):
        try:
            return self._run(
                argv, env={**os.environ, "DISPLAY": ":0", "LC_ALL": "C"},
                check=True, capture_output=True, text=True, timeout=2,
            ).stdout
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise WindowRefused(_UNAVAILABLE, f"{Path(argv[0]).name} failed") from error

    def desktop(self):
        output = self._command(
            "xprop", "-root", "_NET_CURRENT_DESKTOP", "_NET_NUMBER_OF_DESKTOPS",
            "_NET_ACTIVE_WINDOW",
        )
        active = _property(output, r"^_NET_ACTIVE_WINDOW\(WINDOW\): window id # (0x[0-9a-fA-F]+)$")
        return DesktopState(
            _cardinal(output, "_NET_CURRENT_DESKTOP"),
            _cardinal(output, "_NET_NUMBER_OF_DESKTOPS"),
            _xid(active),
            self._pointer(),
        )

    def verify_window(self, binding):
        if binding.display != ":0":
            raise WindowRefused(_UNAVAILABLE, "wmctrl requires a registered :0 window")
        rows = _window_rows(self._command(self._host_tool, "list"))
        matches = [row for row in rows if _xid(row.get("xid")) == binding.xid]
        witnesses = [row for row in rows if row.get("witness") == binding.witness]
        if len(matches) != 1 or witnesses != matches:
            raise WindowRefused(_UNAVAILABLE, "registered window has no unique live witness")
        output = self._command("xprop", "-id", hex(binding.xid), "_NET_WM_PID")
        if _cardinal(output, "_NET_WM_PID") != binding.pid:
            raise WindowRefused(_UNAVAILABLE, "registered window PID changed")

    def workspace(self, binding):
        if binding.display != ":0":
            raise WindowRefused(_UNAVAILABLE, "wmctrl requires a registered :0 window")
        output = self._command("xprop", "-id", hex(binding.xid), "_NET_WM_DESKTOP")
        return _cardinal(output, "_NET_WM_DESKTOP")

    def move(self, binding, workspace):
        self._command("wmctrl", "-i", "-r", hex(binding.xid), "-t", str(workspace))
