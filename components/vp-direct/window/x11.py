"""X11 presentation through the workspace-only command qualified in L0-3."""

from ctypes import CDLL, POINTER, byref, c_char_p, c_int, c_uint, c_ulong, c_void_p
import json
import os
from pathlib import Path
import re
from stat import S_ISSOCK
import subprocess

from . import DesktopState, WindowRefused, _UNAVAILABLE, _xid


_PROC = Path("/proc")
_X11_SOCKETS = Path("/tmp/.X11-unix")


def _owned_process_start(pid):
    """Read the start time in /proc clock ticks for a same-user process."""
    process = _PROC / str(pid)
    try:
        if type(pid) is not int or pid <= 0 or process.stat().st_uid != os.geteuid():
            raise ValueError
        start = int((process / "stat").read_text().rsplit(")", 1)[1].split()[19])
        if start <= 0:
            raise ValueError
        return start
    except (IndexError, OSError, ValueError) as error:
        raise WindowRefused(_UNAVAILABLE, "private process identity is unavailable") from error


def _server_identity(display, pid):
    """Identify the private listeners from Linux metadata without connecting."""
    process = _PROC / str(pid)
    socket_path = _X11_SOCKETS / f"X{display[1:]}"
    try:
        start = _owned_process_start(pid)
        if (process / "exe").resolve(strict=True).name != "Xvfb":
            raise ValueError
        node = socket_path.lstat()
        if not S_ISSOCK(node.st_mode) or node.st_uid != os.geteuid():
            raise ValueError
        owned = set()
        for descriptor in (process / "fd").iterdir():
            try:
                owned.add(os.readlink(descriptor))
            except FileNotFoundError:
                continue

        # Xlib may use either the filesystem or the abstract Unix listener.
        pathname = str(socket_path)
        listeners = {}
        for line in (_PROC / "net" / "unix").read_text().splitlines():
            fields = line.split(maxsplit=7)
            if (len(fields) != 8 or fields[7] not in (pathname, "@" + pathname)
                    or int(fields[3], 16) & 0x10000 == 0):
                continue
            endpoint, inode = fields[7], fields[6]
            if fields[4:6] != ["0001", "01"] or endpoint in listeners or f"socket:[{inode}]" not in owned:
                raise ValueError
            listeners[endpoint] = inode
        if pathname not in listeners or _owned_process_start(pid) != start:
            raise ValueError
        return start, node.st_dev, node.st_ino, tuple(sorted(listeners.items()))
    except (IndexError, OSError, ValueError) as error:
        raise WindowRefused(_UNAVAILABLE, "private X server identity is unavailable") from error


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

    def __init__(self, bin_dir, *, run=subprocess.run, pointer=None,
                 display=None, server_pid=None, server_start_ticks=None, xauthority=None):
        if not bin_dir or not Path(bin_dir).is_absolute():
            raise WindowRefused(_UNAVAILABLE, "MV_SRF_BIN_DIR is unavailable")
        self._host_tool = str(Path(bin_dir) / "srf-host-window")
        self._run = run
        self._display = ":0"
        self._registry_display = ":0"
        self._pointer = _pointer if pointer is None else pointer
        self._server = None
        if display is None:
            if server_pid is not None or server_start_ticks is not None or xauthority is not None:
                raise WindowRefused(_UNAVAILABLE, "the private X display must be specified")
            return
        if (not isinstance(display, str) or not re.fullmatch(r":[1-9][0-9]*", display)
                or type(server_pid) is not int or server_pid <= 0
                or type(server_start_ticks) is not int or server_start_ticks <= 0
                or not isinstance(xauthority, (str, Path)) or not Path(xauthority).is_absolute()
                or not callable(pointer) or pointer is _pointer):
            raise WindowRefused(_UNAVAILABLE, "private display, server identity, authority and pointer are required")
        self._display = display
        self._registry_display = f"xpra:{display[1:]}"
        self._xauthority = str(xauthority)
        self._server_pid = server_pid
        self._server = _server_identity(display, server_pid)
        if self._server[0] != server_start_ticks:
            raise WindowRefused(_UNAVAILABLE, "private X server start time changed")

    def check_private_display(self):
        """Recheck the pinned endpoint before opening a private X connection."""
        if self._server is None or _server_identity(self._display, self._server_pid) != self._server:
            raise WindowRefused(_UNAVAILABLE, "private X server identity changed or was not pinned")

    def _command(self, *argv):
        if self._server is None:
            env = {**os.environ, "DISPLAY": self._display, "LC_ALL": "C"}
        else:
            self.check_private_display()
            env = {"PATH": os.defpath, "LC_ALL": "C", "DISPLAY": self._display,
                   "XAUTHORITY": self._xauthority}
        try:
            return self._run(
                argv, env=env,
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
        if self._server is not None:
            self.check_private_display()
        return DesktopState(
            _cardinal(output, "_NET_CURRENT_DESKTOP"),
            _cardinal(output, "_NET_NUMBER_OF_DESKTOPS"),
            _xid(active),
            self._pointer(),
        )

    def _check_binding_display(self, binding):
        if binding.display != self._registry_display:
            raise WindowRefused(_UNAVAILABLE, f"wmctrl requires a registered {self._registry_display} window")

    def verify_window(self, binding):
        self._check_binding_display(binding)
        argv = (self._host_tool, "list")
        if self._server is not None:
            argv += ("--display", self._display, "--xauthority", self._xauthority)
        rows = _window_rows(self._command(*argv))
        matches = [row for row in rows if _xid(row.get("xid")) == binding.xid]
        witnesses = [row for row in rows if row.get("witness") == binding.witness]
        if len(matches) != 1 or witnesses != matches:
            raise WindowRefused(_UNAVAILABLE, "registered window has no unique live witness")
        output = self._command("xprop", "-id", hex(binding.xid), "_NET_WM_PID")
        if _cardinal(output, "_NET_WM_PID") != binding.pid:
            raise WindowRefused(_UNAVAILABLE, "registered window PID changed")

    def workspace(self, binding):
        self._check_binding_display(binding)
        output = self._command("xprop", "-id", hex(binding.xid), "_NET_WM_DESKTOP")
        return _cardinal(output, "_NET_WM_DESKTOP")

    def move(self, binding, workspace):
        if self._server is not None:
            self._check_binding_display(binding)
        self._command("wmctrl", "-i", "-r", hex(binding.xid), "-t", str(workspace))
