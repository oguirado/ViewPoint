"""S2 host-browser launch and dismissal on the operator's X11 session."""

from dataclasses import dataclass
import errno
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import time
from urllib.parse import urlsplit


RECEIPT = Path(__file__).resolve().parents[2] / "config" / "wl0.json"
LOOPBACK = "127.0.0.1"
DISPLAY = ":0"
BINARIES = {
    "/opt/google/chrome/chrome": "google-chrome-stable",
    "/opt/brave.com/brave/brave": "brave-browser",
    "/usr/lib/aarch64-linux-gnu/opera-stable/opera": "opera-stable",
}
OPTIONAL_FLAGS = (
    "--force-renderer-accessibility",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)
ALLOWLIST = (
    "--user-data-dir=<profile>",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=<int>",
    *OPTIONAL_FLAGS,
)


class LaunchError(RuntimeError):
    """Local launch failure, with a frozen error code where one is defined."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Window:
    xid: str
    pid: int
    workspace: int


@dataclass
class LaunchedBrowser:
    """The owned process, session window binding and sampled focus evidence."""

    _child: object
    cdp_port: int
    window: Window
    witness: str
    focus_samples: tuple
    start_mono_ns: int
    end_mono_ns: int

    @property
    def window_binding(self):
        return {"pid": self.window.pid, "xid": self.window.xid, "witness": self.witness}

    @property
    def focus_take_observed(self):
        return any(xid == self.window.xid for _, xid in self.focus_samples)

    def focus_trace(self, surface_id):
        if not isinstance(surface_id, str) or not surface_id:
            raise ValueError("A surface ID is required for vp.focus_trace/v1")
        return {
            "schema": "vp.focus_trace/v1",
            "surface_id": surface_id,
            "start_mono_ns": self.start_mono_ns,
            "end_mono_ns": self.end_mono_ns,
            "continuous": False,
            "events": [
                {"kind": "_NET_ACTIVE_WINDOW", "mono_ns": stamp, "xid": xid}
                for stamp, xid in self.focus_samples
            ],
        }

    def stop(self):
        self._child.stop()


@dataclass(frozen=True)
class LaunchPolicy:
    agent_workspace: int
    observed_port: int

    @classmethod
    def load(cls, path=RECEIPT):
        try:
            receipt = json.loads(Path(path).read_text(encoding="utf-8"))
            first = receipt["steps"]["L0-1"]
            login = receipt["steps"]["L0-2"]
            move = receipt["steps"]["L0-3"]
            launch = receipt["focus_rules"]["launch"]
            cdp = receipt["backend_rules"]["cdp"]
            qualified = (
                receipt["schema"] == "vp.l0_receipt/v1"
                and first["status"] == login["status"] == move["status"] == "PASS"
                and first["binary"] == "/opt/google/chrome/chrome"
                and first["backend"] == "cdp"
                and first["bind_address"] == cdp["bind_address"] == LOOPBACK
                and first["readiness"] == cdp["readiness"] == "GET /json/version"
                and first["profile_mode"] == "0700"
                and first["profile_contents_read"] is cdp["read_profile_files"] is False
                and first["reuse_observed_port"] is False
                and login["login_by"] == "operator"
                and login["debugging_port_open"] is True
                and login["anti_spam_block"] is False
                and tuple(cdp["browser_allowed_flags"]) == ALLOWLIST
                and move["backend"] == "wmctrl"
                and launch["launch_then_dismiss_one_step"] is True
                and launch["document_brief_focus_take"] is True
                and type(move["agent_workspace"]) is int
                and move["agent_workspace"] >= 0
                and type(first["observed_port"]) is int
                and 1 <= first["observed_port"] <= 65535
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise LaunchError("W-L0 launch receipt is unavailable or malformed") from exc
        if not qualified:
            raise LaunchError("W-L0 launch receipt does not qualify the host launch policy")
        return cls(move["agent_workspace"], first["observed_port"])


def _profile_path(root, profile_id):
    if (
        not isinstance(profile_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", profile_id)
        or profile_id.casefold() in {"default", "system profile", "guest profile"}
        or re.fullmatch(r"profile[._-]?[0-9]+", profile_id, re.IGNORECASE)
    ):
        raise LaunchError("A dedicated provisioned profile is required", code="DEFAULT_PROFILE_REFUSED")
    root = Path(root)
    if not root.is_absolute() or root != root.resolve(strict=False):
        raise LaunchError("The provisioned profile root must be absolute and canonical")
    if root.name != "vp-profiles" and root.parts[-2:] != ("viewpoint", "profiles"):
        raise LaunchError("A dedicated provisioned profile root is required", code="DEFAULT_PROFILE_REFUSED")
    path = root / profile_id
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LaunchError("The dedicated profile directory has not been provisioned") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise LaunchError("The dedicated profile must be an owned 0700 directory")
    return path


def _argv(binary, profile, port, flags):
    return (
        binary,
        f"--user-data-dir={profile}",
        f"--remote-debugging-address={LOOPBACK}",
        f"--remote-debugging-port={port}",
        *flags,
    )


class _OwnedChild:
    def __init__(self, process, pidfd):
        self.process = process
        self.pid = process.pid
        self.pidfd = pidfd

    def alive(self):
        return self.process.poll() is None

    def stop(self):
        if self.pidfd is None:
            return
        try:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    signal.pidfd_send_signal(self.pidfd, sig)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    if sig == signal.SIGKILL:
                        raise LaunchError("The owned browser did not exit after cleanup")
        finally:
            if not self.alive():
                os.close(self.pidfd)
                self.pidfd = None


class LinuxHost:
    """Linux/X11 operations. Probes use fixed commands and metadata only."""

    def __init__(self):
        self._env = {**os.environ, "DISPLAY": DISPLAY, "LC_ALL": "C"}

    def _run(self, argv):
        return subprocess.run(
            argv, env=self._env, check=True, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=1,
        ).stdout

    def verify_binary(self, binary, package):
        resolved = Path(binary).resolve(strict=True)
        if "/snap/" in str(resolved) or resolved == Path("/usr/bin/snap"):
            raise LaunchError("Snap browser debugging is unavailable", code="SNAP_CDP_UNAVAILABLE")
        owner = self._run(("/usr/bin/dpkg-query", "-S", binary)).strip()
        if owner != f"{package}: {binary}":
            raise LaunchError("The browser path lacks its expected DEB owner")

    def allocate_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as port:
            port.bind((LOOPBACK, 0))
            return port.getsockname()[1]

    def spawn(self, argv):
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise LaunchError("PID-bound browser cleanup requires pidfd support")
        process = subprocess.Popen(
            argv, env=self._env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            pidfd = os.pidfd_open(process.pid)
        except OSError:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            raise
        return _OwnedChild(process, pidfd)

    def windows(self):
        result = []
        for line in self._run(("/usr/bin/wmctrl", "-l", "-p")).splitlines():
            # wmctrl appends host and title. Only the XID, desktop and PID are used.
            xid, desktop, pid, *_ = line.split(maxsplit=3)
            result.append(Window(hex(int(xid, 16)), int(pid), int(desktop)))
        return result

    def desktop(self):
        raw = self._run(("/usr/bin/xprop", "-root", "_NET_ACTIVE_WINDOW", "_NET_CURRENT_DESKTOP"))
        active = re.search(r"^_NET_ACTIVE_WINDOW\(WINDOW\): window id # (0x[0-9a-f]+)$", raw, re.M)
        workspace = re.search(r"^_NET_CURRENT_DESKTOP\(CARDINAL\) = ([0-9]+)$", raw, re.M)
        if not active or not workspace:
            raise LaunchError("The X11 window manager did not provide desktop evidence")
        return hex(int(active[1], 16)), int(workspace[1])

    def move(self, window, destination):
        if [item for item in self.windows() if item.xid == window.xid] != [window]:
            raise LaunchError("The window witness changed before dismissal")
        self._run(("/usr/bin/wmctrl", "-i", "-r", window.xid, "-t", str(destination)))

    def listener(self, child, port):
        """Classify current listeners by address and the direct child's socket inode."""
        owned = set()
        # stat() reads socket metadata without retrieving file-descriptor targets.
        if child.alive():
            try:
                for entry in (Path("/proc") / str(child.pid) / "fd").iterdir():
                    try:
                        metadata = entry.stat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISSOCK(metadata.st_mode):
                        owned.add(str(metadata.st_ino))
            except FileNotFoundError:
                if child.alive():
                    raise
        listeners = []
        for table in ("tcp", "tcp6"):
            with (Path("/proc/self/net") / table).open(encoding="ascii") as rows:
                next(rows)
                for row in rows:
                    fields = row.split()
                    address, number = fields[1].split(":")
                    if fields[3] == "0A" and int(number, 16) == port:
                        listeners.append((address, fields[9] in owned))
        return _listener_state(listeners)

    def debug_version(self, port, timeout):
        connection = http.client.HTTPConnection(LOOPBACK, port, timeout=timeout)
        try:
            connection.request("GET", "/json/version")
            response = connection.getresponse()
            if response.status != 200:
                return False
            body = response.read(16385)
            if len(body) > 16384:
                return False
            data = json.loads(body)
            endpoint = urlsplit(data["webSocketDebuggerUrl"])
            return (
                bool(data["Browser"])
                and endpoint.scheme == "ws"
                and endpoint.netloc == f"{LOOPBACK}:{port}"
                and endpoint.path.startswith("/devtools/browser/")
                and len(endpoint.path) > len("/devtools/browser/")
                and not endpoint.query and not endpoint.fragment
            )
        except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
            return False
        finally:
            connection.close()


def _listener_state(listeners):
    ipv4_loopback = "0100007F"
    ipv6_loopback = "00000000000000000000000001000000"
    # Chromium may try ::1 after losing the IPv4 bind race.
    if sorted(listeners) == [(ipv6_loopback, True), (ipv4_loopback, False)]:
        return "busy"
    if any(owned and address != ipv4_loopback for address, owned in listeners):
        raise LaunchError("The owned browser listener is not IPv4 loopback")
    if not listeners:
        return "pending"
    if listeners == [(ipv4_loopback, True)]:
        return "owned"
    return "busy"


class _PortCollision(Exception):
    pass


class Launcher:
    """Launch a provisioned profile under S1's exclusive profile reservation."""

    def __init__(self, profile_root, *, receipt=RECEIPT, host=None, port_attempts=3,
                 ready_timeout=10.0, poll_interval=0.05):
        if (
            type(port_attempts) is not int or port_attempts < 1
            or not 0 < poll_interval <= ready_timeout < float("inf")
        ):
            raise ValueError("Launch timing and retry bounds must be finite and positive")
        self.profile_root = profile_root
        self.policy = LaunchPolicy.load(receipt)
        self.host = LinuxHost() if host is None else host
        self.port_attempts = port_attempts
        self.ready_timeout = ready_timeout
        self.poll_interval = poll_interval

    def launch(self, binary, profile_id, *, flags=OPTIONAL_FLAGS):
        binary = os.fspath(binary)
        if binary.startswith(("/snap/", "/var/lib/snapd/")) or binary == "/usr/bin/snap":
            raise LaunchError("Snap browser debugging is unavailable", code="SNAP_CDP_UNAVAILABLE")
        if Path(binary).name in {"firefox", "firefox-bin"}:
            raise LaunchError("Firefox CDP is deferred", code="BIDI_DEFERRED")
        if binary not in BINARIES:
            raise LaunchError("Browser executable is outside the DEB allowlist")
        flags = tuple(flags)
        if any(flag not in OPTIONAL_FLAGS for flag in flags) or len(set(flags)) != len(flags):
            raise LaunchError("Browser flag is outside the allowlist or duplicates a flag")
        profile = _profile_path(self.profile_root, profile_id)
        self.host.verify_binary(binary, BINARIES[binary])
        tried = {self.policy.observed_port}
        for _ in range(self.port_attempts):
            try:
                port = self.host.allocate_port()
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                continue
            if type(port) is not int or not 1 <= port <= 65535:
                raise LaunchError("Port allocation did not provide a TCP port")
            if port in tried:
                continue
            tried.add(port)
            try:
                return self._launch_and_dismiss(_argv(binary, profile, port, flags), port)
            except _PortCollision:
                continue
        raise LaunchError("Loopback debugging port retry bound reached", code="PORT_BUSY")

    def _launch_and_dismiss(self, argv, port):
        prior_xids = {window.xid for window in self.host.windows()}
        start = time.monotonic_ns()
        active, workspace = self.host.desktop()
        destination = self.policy.agent_workspace
        if workspace == destination:
            raise LaunchError("The operator is already on the configured agent workspace")
        samples = [(time.monotonic_ns(), active)]
        child = self.host.spawn(argv)
        try:
            window = self._await_ready(child, port, prior_xids, workspace, destination, samples)
        except BaseException:
            child.stop()
            raise
        end = time.monotonic_ns()
        return LaunchedBrowser(child, port, window, f"launch:{child.pid}:{start}",
                               tuple(samples), start, end)

    def _await_ready(self, child, port, prior_xids, workspace, destination, samples):
        deadline = time.monotonic() + self.ready_timeout
        moved = None
        while time.monotonic() < deadline:
            if not child.alive():
                # The bind race can make Chromium exit before its window appears.
                if self.host.listener(child, port) == "busy":
                    raise _PortCollision
                raise LaunchError("The owned browser exited before readiness")
            active, current_workspace = self.host.desktop()
            samples.append((time.monotonic_ns(), active))
            if current_workspace != workspace:
                raise LaunchError("The operator workspace changed during launch")
            windows = [window for window in self.host.windows() if window.pid == child.pid]
            if len(windows) > 1 or any(window.xid in prior_xids for window in windows):
                raise LaunchError("The owned browser window witness is ambiguous or reused")
            candidate = windows[0] if windows else None
            if moved is not None and (candidate is None or candidate.xid != moved):
                raise LaunchError("The browser window changed during dismissal")
            if moved is None and candidate is not None:
                if not child.alive():
                    raise LaunchError("The owned browser exited before dismissal")
                self.host.move(candidate, destination)
                moved = candidate.xid
            listener = self.host.listener(child, port)
            if listener == "busy":
                raise _PortCollision
            remaining = deadline - time.monotonic()
            if (
                remaining > 0
                and candidate is not None and candidate.workspace == destination
                and active != candidate.xid and listener == "owned"
                and self.host.debug_version(port, min(remaining, 0.25))
            ):
                # Recheck after the HTTP probe, which can race with listener/window changes.
                active, current_workspace = self.host.desktop()
                samples.append((time.monotonic_ns(), active))
                listener = self.host.listener(child, port)
                if listener == "busy":
                    raise _PortCollision
                if (
                    child.alive() and listener == "owned"
                    and current_workspace == workspace and active != candidate.xid
                    and [w for w in self.host.windows() if w.pid == child.pid] == [candidate]
                    and time.monotonic() <= deadline
                ):
                    return candidate
            time.sleep(max(0, min(self.poll_interval, deadline - time.monotonic())))
        raise LaunchError("Browser readiness requires its own /json/version and dismissed window")
