"""Custody of the private X server, browser, and fixture-only endpoints."""

from base64 import b64decode
from contextlib import ExitStack, contextmanager
import errno
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from threading import Thread
import time
from urllib.parse import urlsplit
from uuid import uuid4

DIRECT = Path(__file__).resolve().parents[2]
SITE = DIRECT / "fixtures" / "site"
WORK = Path("/work")
PAGES = frozenset({"/index.html", "/timer.html", "/login.html", "/chat.html", "/form.html"})
X_SOCKET_DIR = Path("/tmp/.X11-unix")
SCREEN_WIDTH, SCREEN_HEIGHT = 1920, 1080
# Xvfb stores 24-bit depth as 32-bit pixels. Allow 4 KiB for XWD metadata and palette.
MAX_XWD_BYTES = SCREEN_WIDTH * SCREEN_HEIGHT * 4 + 4096


class Refused(RuntimeError):
    """A private-fixture prerequisite failed."""


def require(condition, message):
    if not condition:
        raise Refused(message)


def display_number(value):
    match = re.fullmatch(r":([1-9][0-9]{1,3})", value)
    require(match is not None and int(match[1]) >= 90, "a local display at or above :90 is required")
    return int(match[1])


def choose_displays(requested, ambient, count, *, tmp=Path("/tmp"), unix=Path("/proc/net/unix")):
    require(not ambient, "ambient DISPLAY is set; start QD with DISPLAY unset")
    endpoints = {line.split()[-1] for line in unix.read_text().splitlines() if line.split()}

    def free(number):
        endpoint = tmp / ".X11-unix" / f"X{number}"
        lock = tmp / f".X{number}-lock"
        return (not os.path.lexists(endpoint) and not os.path.lexists(lock)
                and str(endpoint) not in endpoints and f"@{endpoint}" not in endpoints)

    chosen = []
    if requested is not None:
        number = display_number(requested)
        require(free(number), "requested X display is already occupied")
        chosen.append(number)
    for number in range(90, 10000):
        if len(chosen) == count:
            return [f":{number}" for number in chosen]
        if number not in chosen and free(number):
            chosen.append(number)
    require(len(chosen) == count, "insufficient free private X display numbers")
    return [f":{number}" for number in chosen]


def choose_display(requested, ambient, **paths):
    return choose_displays(requested, ambient, 1, **paths)[0]


def process_stat(pid):
    fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return fields[0], int(fields[19])


def process_start(pid):
    state, start = process_stat(pid)
    require(state not in {"Z", "X"}, "private process has exited")
    return start


def eventually(probe, reason, *, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.05)
    raise Refused(reason)


def remove_private_tree(path):
    def remove():
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            if os.path.lexists(path):
                raise
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY:
                raise
            return False
        return True

    eventually(remove, "private directory remained nonempty after cleanup retries", timeout=1)


def run(argv, *, env=None, input=None):
    return subprocess.run(argv, env=env, input=input, capture_output=True, check=True, timeout=5)


def listener_owned(port, pid):
    rows = run(["ss", "-H", "-ltnp", f"sport = :{port}"]).stdout.decode().splitlines()
    if len(rows) != 1:
        return False
    fields = rows[0].split()
    return (len(fields) > 4 and fields[3] == f"127.0.0.1:{port}"
            and set(re.findall(r"\bpid=(\d+)\b", rows[0])) == {str(pid)})


def loopback_url(url):
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise Refused("invalid fixture URL") from exc
    require(parts.scheme == "http" and port and parts.netloc == f"127.0.0.1:{port}"
            and parts.path in PAGES and not parts.query and not parts.fragment
            and url == f"http://127.0.0.1:{port}{parts.path}", "URL is outside the fixture allowlist")
    return port, parts.path


def get_http(port, path, limit, *, fixture=False):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        require(response.status == 200 and (not fixture or response.getheader("X-Fixture-Server") == "vp-direct"),
                "local endpoint did not return the expected response")
        content = response.read(limit + 1)
        require(len(content) <= limit, "local response exceeded its bound")
        return content
    finally:
        connection.close()


def verify_fixture(url):
    port, path = loopback_url(url)
    expected = (SITE / path[1:]).read_bytes()
    require(get_http(port, path, len(expected), fixture=True) == expected, "fixture bytes changed")
    return sha256(expected).hexdigest()


def socket_identity(path):
    info = path.lstat()
    require(stat.S_ISSOCK(info.st_mode), "private display socket has the wrong type")
    return info.st_dev, info.st_ino


def read_diagnostic(error):
    size = os.fstat(error.fileno()).st_size
    return os.pread(error.fileno(), 1024, max(0, size - 1024)).decode(errors="replace").strip()


class Child:
    def __init__(self, argv, env, stack):
        self.error = stack.enter_context(tempfile.TemporaryFile(dir="/tmp"))
        self.proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=self.error)
        stack.callback(self.close)
        self.start = process_start(self.proc.pid)

    @property
    def pid(self):
        return self.proc.pid

    def live(self):
        try:
            return self.proc.poll() is None and process_start(self.pid) == self.start
        except (OSError, Refused):
            return False

    def diagnostic(self):
        return read_diagnostic(self.error)

    def close(self):
        # An unreaped direct child cannot have its PID recycled.
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)


class BrowserProcess:
    def __init__(self, argv, env, error):
        supervisor = Path(__file__).with_name("browser_process.py")
        self.proc = subprocess.Popen([sys.executable, "-IB", str(supervisor), *argv], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error)

    def identify(self):
        with self.proc.stdout as report:
            require(select.select([report], [], [], 3)[0], "Chrome supervisor did not report a PID")
            pid = report.readline().strip()
        require(pid.isdigit() and int(pid) > 1, "Chrome supervisor did not launch the browser")
        self.pid = int(pid)
        self.start = process_start(self.pid)

    def live(self):
        try:
            return self.proc.poll() is None and process_start(self.pid) == self.start
        except (OSError, Refused):
            return False

    def close(self):
        self.proc.stdin.close()
        require(self.proc.wait(timeout=8) == 0, "Chrome supervisor could not drain the owned process tree")


class Display:
    def __init__(self, name, *, size=(SCREEN_WIDTH, SCREEN_HEIGHT), private_paths=False):
        self.name = name
        self.number = display_number(name)
        self.size = size
        self.private_paths = private_paths
        self.env = dict(os.environ, DISPLAY=name, XAUTHORITY=str(WORK / "Xauthority"))
        self.socket = X_SOCKET_DIR / f"X{self.number}"
        self.lock = Path("/tmp") / f".X{self.number}-lock"
        self.xpra = None

    def start(self, stack, *, framebuffer=False):
        require(not os.path.lexists(self.lock) and not os.path.lexists(self.socket),
                "private X endpoint is occupied")
        run(["xauth", "-q", "-f", self.env["XAUTHORITY"]], env=self.env,
            input=f"add {self.name} MIT-MAGIC-COOKIE-1 {secrets.token_hex(16)}\n".encode())
        suffix = f"-{self.number}" if self.private_paths else ""
        self.frame_dir = Path("/dev/shm") / f"framebuffer{suffix}"
        fb = []
        if framebuffer:
            self.frame_dir.mkdir(mode=0o700)
            fb = ["-fbdir", str(self.frame_dir)]
        width, height = self.size
        self.server = Child(["Xvfb", self.name, "-screen", "0", f"{width}x{height}x24",
                             "-nocursor", *fb, "-extension", "GLX", "-nolisten", "tcp", "-auth", self.env["XAUTHORITY"]], self.env, stack)
        self.endpoint = eventually(self.probe_server, "owned Xvfb did not create its X11 endpoint")
        def ready():
            try:
                return self.command("xdpyinfo")
            except subprocess.CalledProcessError:
                return None

        eventually(ready, "owned Xvfb did not accept authenticated X11")
        self.start_wm(stack)

    def probe_server(self):
        if not self.server.live():
            raise Refused(f"owned Xvfb exited: {self.server.diagnostic()}")
        try:
            exe = os.readlink(Path("/proc") / str(self.server.pid) / "exe")
        except FileNotFoundError:
            return None
        require(Path(exe).name == "Xvfb", "X server executable changed")
        try:
            before = self.lock.lstat()
            require(stat.S_ISREG(before.st_mode), "X display lock has the wrong type")
            owner = self.lock.read_text().strip()
            after = self.lock.lstat()
            if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino) or owner != str(self.server.pid):
                raise Refused("X display lock owner changed")
            return socket_identity(self.socket)
        except FileNotFoundError:
            return None

    def verify(self, value):
        if value not in (self.name, self.label):
            return False
        try:
            if self.probe_server() != self.endpoint:
                return False
            if value != self.name:
                return self.xpra is not None and self.xpra.live() and (
                    socket_identity(self.xpra_socket) == self.xpra_endpoint)
            return True
        except (AttributeError, OSError, Refused):
            return False

    @property
    def label(self):
        return f"xpra{self.name}" if self.xpra is not None else self.name

    def command(self, *argv):
        require(self.verify(self.name), "X connection refused without the owned endpoint")
        return run(argv, env=self.env).stdout

    def start_wm(self, stack):
        argv = ["openbox", "--sm-disable"] if self.private_paths else ["openbox"]
        wm = Child(argv, self.env, stack)

        def wm_ready():
            if not wm.live():
                raise Refused(f"Openbox exited on {self.name} ({wm.proc.returncode}): {wm.diagnostic()}")
            return b"window id # 0x" in self.command("xprop", "-root", "_NET_SUPPORTING_WM_CHECK")

        eventually(wm_ready, "Openbox did not acquire the private display")
        self.command("wmctrl", "-n", "2")

    def park(self, xid):
        self.command("wmctrl", "-i", "-r", xid, "-t", "1")
        eventually(lambda: self.hidden(xid), "fixture did not move off the active workspace")

    def hidden(self, xid):
        window = self.command("xprop", "-id", xid, "-notype", "_NET_WM_DESKTOP")
        root = self.command("xprop", "-root", "-notype", "_NET_CURRENT_DESKTOP")
        workspace = re.fullmatch(rb"_NET_WM_DESKTOP = ([0-9]+)\s*", window)
        active = re.fullmatch(rb"_NET_CURRENT_DESKTOP = ([0-9]+)\s*", root)
        require(workspace and active, "private workspace identity is unavailable")
        return workspace[1] != active[1]

    def start_xpra(self, stack):
        require(self.verify(self.name), "Xpra needs the owned Xvfb endpoint")
        sockets = WORK / (f"xpra-{self.number}" if self.private_paths else "xpra")
        sockets.mkdir(mode=0o700)
        self.xpra = Child(["xpra", "shadow", self.name, "--daemon=no", "--attach=no",
                           "--socket-dir=" + str(sockets), "--socket-dirs=" + str(sockets), "--socket-permissions=600",
                           "--dbus-launch=", "--mdns=no", "--pulseaudio=no", "--webcam=no",
                           "--notifications=no", "--speaker=off", "--microphone=off",
                           "--systemd-run=no", "--mmap=no"], self.env, stack)

        def ready():
            if not self.xpra.live():
                raise Refused(f"private Xpra shadow exited: {self.xpra.diagnostic()}")
            for candidate in sockets.iterdir():
                if candidate.is_socket() and candidate.name.endswith(f"-{self.number}"):
                    return candidate
            return None

        self.xpra_socket = eventually(ready, "private Xpra shadow did not publish its socket")
        self.xpra_endpoint = socket_identity(self.xpra_socket)

    def start_bus(self, stack):
        require(self.verify(self.name), "AT-SPI needs the owned Xvfb endpoint")
        runtime_dir = Path(self.env["XDG_RUNTIME_DIR"]) if self.private_paths else WORK / "run"
        socket_path = runtime_dir / "session-bus" if self.private_paths else WORK / "session-bus"
        address = f"unix:path={socket_path}"
        session = Child(["dbus-daemon", "--session", "--nofork", "--nopidfile",
                         f"--address={address}"], self.env, stack)
        self.env["DBUS_SESSION_BUS_ADDRESS"] = address
        def ready():
            require(session.live(), "private D-Bus session exited")
            return socket_path.is_socket()
        eventually(ready, "private D-Bus session did not start")
        launcher = Child(["/usr/libexec/at-spi-bus-launcher", "--launch-immediately"], self.env, stack)
        a11y = ["gdbus", "call", "--address", address, "--dest", "org.a11y.Bus",
                "--object-path", "/org/a11y/bus", "--method"]

        def bus_address():
            require(launcher.live() and session.live(), "private accessibility launcher exited")
            try:
                owner = run(["gdbus", "call", "--address", address, "--dest", "org.freedesktop.DBus", "--object-path",
                             "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.GetConnectionUnixProcessID",
                             "org.a11y.Bus"], env=self.env).stdout
            except subprocess.CalledProcessError:
                return None
            require(re.fullmatch(fr"\(uint32 {launcher.pid},\)\s*", owner.decode()),
                    "accessibility service did not belong to the owned launcher")
            reply = run([*a11y, "org.a11y.Bus.GetAddress"], env=self.env)
            prefix = re.escape(os.fsencode(runtime_dir / "at-spi") + b"/")
            match = re.fullmatch(rb"\('unix:path=(" + prefix + rb"[^,';]+)(?:,guid=[a-f0-9]+)?',\)\s*",
                                 reply.stdout)
            require(match and Path(os.fsdecode(match[1])).is_socket(), "AT-SPI address is outside the private runtime")
            return reply.stdout.decode().split("'")[1]

        atspi_address = eventually(bus_address, "AT-SPI bus did not start")
        # Chrome's forced renderer AX tree still needs the toolkit bridge enabled on this session.
        run([*a11y, "org.freedesktop.DBus.Properties.Set", "org.a11y.Status", "IsEnabled", "<true>"], env=self.env)
        enabled = run([*a11y, "org.freedesktop.DBus.Properties.Get", "org.a11y.Status", "IsEnabled"], env=self.env)
        require(enabled.stdout.strip() == b"(<true>,)", "private AT-SPI status could not be enabled")
        # Activate the registry on the validated accessibility socket before starting Chrome.
        run(["gdbus", "call", "--address", atspi_address, "--dest", "org.a11y.atspi.Registry",
             "--object-path", "/org/a11y/atspi/accessible/root", "--method", "org.freedesktop.DBus.Peer.Ping"],
            env=self.env)
        self.env["AT_SPI_BUS_ADDRESS"] = atspi_address
        self.bus_pids = (session.pid, launcher.pid)


class Page:
    """A local owned page target with a latched navigation boundary."""

    def __init__(self, browser, address, target_id):
        self.browser = browser
        self.address = address
        self.target_id = target_id
        self.wire = None
        self.last_id = 0
        self.changed = False
        self.document = None

    def connect(self):
        from websocket import create_connection

        require(listener_owned(self.browser.port, self.browser.child.pid), "CDP listener owner changed")
        self.wire = create_connection(self.address, timeout=4, suppress_origin=True,
                                      http_no_proxy=["127.0.0.1"])
        try:
            self.call("Page.enable")
            def loaded():
                tree = self.call("Page.getFrameTree")["frameTree"]
                return tree["frame"]["url"] == self.browser.url and self.read_bool(
                    "document.readyState === 'complete'")

            eventually(loaded, "owned fixture document did not finish loading the pinned URL")
            self.document = self.frame()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.wire is not None:
            self.wire.close(timeout=0.2)
            self.wire = None

    def call(self, method, params=None):
        self.last_id += 1
        self.wire.send(json.dumps({"id": self.last_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            reply = json.loads(self.wire.recv())
            if reply.get("id") == self.last_id:
                require("result" in reply, f"owned CDP command failed: {method}")
                return reply["result"]
            if self.document is not None and reply.get("method") in {
                "Page.frameNavigated", "Page.navigatedWithinDocument", "Page.frameAttached", "Page.frameDetached",
                "Page.frameStartedNavigating", "Page.frameRequestedNavigation",
            }:
                self.changed = True
        raise Refused("private CDP response timed out")

    def frame(self):
        tree = self.call("Page.getFrameTree")["frameTree"]
        frame = tree["frame"]
        require(not tree.get("childFrames"), "unexpected private fixture subframe")
        require(not self.changed, "private fixture navigation detected")
        require(frame["url"] == self.browser.url, "private fixture did not load the pinned URL")
        result = frame["id"], frame["loaderId"]
        require(self.document is None or result == self.document, "fixture loader changed")
        return result

    def read_bool(self, expression):
        reply = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return not reply.get("exceptionDetails") and reply["result"].get("value") is True

    def screenshot(self):
        return b64decode(self.call("Page.captureScreenshot", {"format": "png"})["data"], validate=True)


class Browser:
    def __init__(self, display, url, stack, *, kiosk=False):
        self.display, self.url = display, url
        self.fixture_digest = verify_fixture(url)
        require(display.verify(display.name), "Chrome needs the owned Xvfb endpoint")
        self.error = stack.enter_context(tempfile.TemporaryFile(dir="/tmp"))
        self.profile = Path(tempfile.mkdtemp(prefix="chrome-", dir=WORK))
        self.child = None
        stack.callback(self.close)
        self.port = self.free_port()
        args = ["/opt/google/chrome/chrome", f"--user-data-dir={self.profile}", "--ozone-platform=x11",
                "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={self.port}",
                "--force-renderer-accessibility", "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-component-update", "--disable-sync",
                "--disable-breakpad", "--disable-crash-reporter", "--disable-gpu", "--no-proxy-server",
                "--password-store=basic", "--use-mock-keychain", "--disable-dev-shm-usage",
                "--window-position=40,40", "--window-size=1024,768",
                *(["--kiosk", "--force-device-scale-factor=1"] if kiosk else []), "--new-window", url]
        self.child = BrowserProcess(args, display.env, self.error)
        self.child.identify()
        eventually(self.ready, "Chrome did not publish a private CDP listener")
        self.xid = eventually(self.find_window, "could not identify a unique owned Chrome X window")
        self.target = eventually(self.find_target, "could not identify the private fixture page target")
        self.page = Page(self, self.target["webSocketDebuggerUrl"], self.target["id"])
        self.page.connect()
        stack.callback(self.page.close)
        self.cdp_window = self.page.call("Browser.getWindowForTarget")["windowId"]
        self.incarnation = uuid4().hex
        self.witness = sha256(json.dumps([display.name, display.server.pid, display.server.start,
                                          display.endpoint, self.child.pid, self.child.start, self.xid,
                                          self.target["id"], self.cdp_window, self.incarnation]).encode()).hexdigest()
        self.check()

    def close(self):
        if self.child is not None:
            self.child.close()
        remove_private_tree(self.profile)

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def ready(self):
        if not self.child.live():
            raise Refused(f"owned Chrome exited before readiness: {read_diagnostic(self.error)}")
        return listener_owned(self.port, self.child.pid)

    def json(self, path):
        require(self.ready(), "CDP endpoint owner changed")
        return json.loads(get_http(self.port, path, 1024 * 1024))

    def websocket_address(self, address, endpoint):
        match = re.fullmatch(rf"ws://127\.0\.0\.1:{self.port}/devtools/{endpoint}/[a-zA-Z0-9-]+", address)
        require(match, "CDP returned an unbound websocket endpoint")

    def find_target(self):
        pages = [entry for entry in self.json("/json/list") if entry.get("type") == "page"]
        if len(pages) != 1 or pages[0].get("url") != self.url:
            return None
        target = pages[0]
        self.websocket_address(target["webSocketDebuggerUrl"], "page")
        require(target["webSocketDebuggerUrl"].rsplit("/", 1)[1] == target["id"], "CDP target ID mismatch")
        return target

    def find_window(self):
        argv = ("xprop", "-root", "-notype", "_NET_CLIENT_LIST")
        xids = re.findall(r"0x[0-9a-f]+", self.display.command(*argv).decode())
        owned = []
        for xid in xids:
            props = self.display.command("xprop", "-id", xid, "-notype", "_NET_WM_PID",
                                         "_NET_WM_WINDOW_TYPE").decode().splitlines()
            if (f"_NET_WM_PID = {self.child.pid}" in props
                    and "_NET_WM_WINDOW_TYPE = _NET_WM_WINDOW_TYPE_NORMAL" in props):
                owned.append(xid)
        require(len(owned) <= 1, "ambiguous Chrome window")
        return next(iter(owned), None)

    def check(self, *, document=True):
        require(self.display.verify(self.display.label) and self.ready() and self.find_window() == self.xid,
                "private browser identity changed")
        if document:
            self.page.frame()
            require(self.page.call("Browser.getWindowForTarget")["windowId"] == self.cdp_window,
                    "private CDP window changed")

    def geometry(self):
        rect = self.display.command("xwininfo", "-id", self.xid).decode()
        values = re.findall(r"^\s*(?:Absolute upper-left [XY]|Width|Height):\s*(-?[0-9]+)\s*$",
                            rect, re.MULTILINE)
        require(len(values) == 4, "private window geometry unavailable")
        layout = self.page.call("Page.getLayoutMetrics")["cssVisualViewport"]
        return tuple(map(int, values)), (int(layout["clientWidth"]), int(layout["clientHeight"]))


@contextmanager
def fixture_site():
    from fixtures.fixture_server import make_handler

    handler = make_handler(SITE)
    handler.log_message = lambda *_: None
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join()


@contextmanager
def open_browser(display, url, *, kiosk=False):
    with ExitStack() as stack:
        browser = Browser(display, url, stack, kiosk=kiosk) if kiosk else Browser(display, url, stack)
        yield browser
    require(not browser.profile.exists(), "disposable Chrome profile was retained")
