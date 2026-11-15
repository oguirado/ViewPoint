# @module {"id":"wa.perceive.atspi","layer":1,"owner":"PERCEIVE","deps":[],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.atspi - rung 2 for desktop programs: does this window have an AT-SPI tree?

ORIGIN: ported and adapted from the operator's own code,
OpenFresnel companions/superficies/atspi.py (Spanish original).
Talks to the accessibility bus through `gdbus` (no pyatspi / python-dbus needed); the bus
address is read from the X11 root property AT_SPI_BUS. The window <-> tree link is by PID
only and is imperfect (multi-process programs); it is declared via `link`.
Port fix: the origin indexed the lazy index with `idx[p]`, which it does not support; here
`idx.get(p)` is used.
v0 scope: discovery and per-window summary only. Walking the tree into a STATE is AUSENTE.
"""
import os
import re
import subprocess

ROOT_PATH = "/org/a11y/atspi/accessible/root"
_RE_PAIR = re.compile(r"\('(:[0-9.]+)',\s*(?:objectpath\s*)?'([^']+)'\)")
_RE_UINT = re.compile(r"\(uint32 (\d+),\)")
_RE_INT = re.compile(r"<(-?\d+)>")
_RE_STR = re.compile(r"<'(.*)'>")


# @anchor:wa.perceive.atspi.bus_address
def bus_address(display=None):
    if os.environ.get("AT_SPI_BUS_ADDRESS"):
        return os.environ["AT_SPI_BUS_ADDRESS"]
    env = dict(os.environ)
    if display:
        env["DISPLAY"] = display
    try:
        p = subprocess.run(["xprop", "-root", "AT_SPI_BUS"], capture_output=True, text=True,
                           timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    m = re.search(r'AT_SPI_BUS\(STRING\) = "(.*)"', p.stdout)
    return m.group(1) if m else None


def _call(address, dest, path, method, *args, timeout=10):
    cmd = ["gdbus", "call", "--address", address, "--dest", dest, "--object-path", path,
           "--method", method] + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


# @anchor:wa.perceive.atspi.parse_children
def parse_children(raw):
    return _RE_PAIR.findall(raw or "")


def _detail(call, address, bus, path, timeout):
    get = "org.freedesktop.DBus.Properties.Get"
    n = _RE_INT.search(call(address, bus, path, get, "org.a11y.atspi.Accessible", "ChildCount", timeout=timeout) or "")
    s = _RE_STR.search(call(address, bus, path, get, "org.a11y.atspi.Accessible", "Name", timeout=timeout) or "")
    k = _RE_STR.search(call(address, bus, path, get, "org.a11y.atspi.Application", "ToolkitName", timeout=timeout) or "")
    return {"name": s.group(1) if s else None, "n_children": int(n.group(1)) if n else None,
            "toolkit": k.group(1) if k else None}


# @anchor:wa.perceive.atspi.Index
class Index:
    """Lazy pid -> AT-SPI application index (pass 1 resolves PIDs only)."""

    def __init__(self, display=None, timeout=10, address=None, call=_call):
        self.timeout = timeout
        self._call = call
        self.address = address or bus_address(display)
        self.by_pid = {}
        self._cache = {}
        if not self.address:
            return
        raw = call(self.address, "org.a11y.atspi.Registry", ROOT_PATH,
                   "org.a11y.atspi.Accessible.GetChildren", timeout=timeout)
        for bus, path in parse_children(raw):
            pid = _RE_UINT.search(call(self.address, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                       "org.freedesktop.DBus.GetConnectionUnixProcessID", bus,
                                       timeout=timeout) or "")
            if pid:
                self.by_pid.setdefault(int(pid.group(1)), []).append((bus, path))

    def __bool__(self):
        return bool(self.by_pid)

    def __contains__(self, pid):
        return pid in self.by_pid

    def get(self, pid):
        """The connection of that PID with the MOST children."""
        if pid in self._cache:
            return self._cache[pid]
        best = None
        for bus, path in self.by_pid.get(pid, []):
            d = _detail(self._call, self.address, bus, path, self.timeout)
            d.update({"bus": bus, "path": path, "pid": pid})
            if best is None or (d["n_children"] or 0) > (best["n_children"] or 0):
                best = d
        self._cache[pid] = best
        return best


# @anchor:wa.perceive.atspi.window_summary
def window_summary(pid, idx, family_pids=None):
    """`tree`: True (positive child count), False (zero), None (unknown count or no link)."""
    if pid is None:
        return {"available": bool(idx), "tree": None, "link": "no_pid", "reason": "window has no _NET_WM_PID"}
    app, link = idx.get(pid), "by_pid"
    if app is None:
        for p in family_pids or ():
            if p in idx:
                app, link = idx.get(p), "by_family_pid"
                break
    if app is None:
        return {"available": bool(idx), "tree": None, "link": "no_match",
                "reason": "no AT-SPI client declares this PID or one of its family"}
    count = app["n_children"]
    tree = None if count is None or count < 0 else count > 0
    return {"available": True, "tree": tree, "link": link, "bus": app["bus"],
            "pid_atspi": app["pid"], "name": app["name"], "n_children": app["n_children"],
            "toolkit": app["toolkit"], "rung": 2 if tree else 4}
