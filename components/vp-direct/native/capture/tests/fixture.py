"""Owned Xvfb fixture and the existing Python capture endpoint."""

from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from time import monotonic, sleep

SURFACE = Path(__file__).resolve().parents[1]
COMPONENTS = SURFACE.parents[2]
sys.path.insert(0, str(SURFACE))
sys.path.insert(0, str(COMPONENTS / "vision-engine"))

from vadapt import x11_ramcap, xfb

PATTERNS = (
    ("#173b81", "#e19643"),
    ("#8f1964", "#3ac6a7"),
)


class PrivateX:
    def __init__(self, width=96, height=64, *, depth=24, display=93):
        if os.environ.get("VP_CAPTURE_PRIVATE_X") != "1" or os.stat("/tmp").st_dev == os.stat("/").st_dev:
            raise RuntimeError("run this fixture through tests/private_x.sh")
        if not 90 <= display <= 65535:
            raise ValueError("fixture needs a private display number")
        self.width = width
        self.height = height
        self.depth = depth
        self.display = display
        self.environment = {
            "PATH": "/usr/bin:/bin",
            "DISPLAY": f":{display}",
            "XAUTHORITY": "/dev/null",
            "LC_ALL": "C",
        }

    def __enter__(self):
        with ExitStack() as cleanup:
            self.directory = cleanup.enter_context(TemporaryDirectory(prefix="vp-capture-", dir="/tmp"))
            read_fd, ready_fd = os.pipe()
            with os.fdopen(read_fd, "rb") as ready, TemporaryFile(dir=self.directory) as log:
                try:
                    self.process = subprocess.Popen(
                        ["Xvfb", f":{self.display}", "-displayfd", str(ready_fd),
                         "-screen", "0", f"{self.width}x{self.height}x{self.depth}", "-fbdir",
                         self.directory, "-nolisten", "tcp", "-nocursor", "-noreset", "-ac",
                         "-extension", "GLX"],
                        env=self.environment, stdin=subprocess.DEVNULL, stderr=log, pass_fds=(ready_fd,),
                    )
                    cleanup.callback(self.stop)
                finally:
                    os.close(ready_fd)
                deadline = monotonic() + 5
                reported = bytearray()
                while len(reported) < 32 and not reported.endswith(b"\n"):
                    if not select.select([ready], [], [], max(0, deadline - monotonic()))[0]:
                        raise RuntimeError("private Xvfb did not report readiness")
                    chunk = os.read(ready.fileno(), 32 - len(reported))
                    if not chunk:
                        break
                    reported.extend(chunk)
                if reported != f"{self.display}\n".encode() or self.process.poll() is not None:
                    log.seek(0)
                    detail = log.read(4096).decode(errors="replace")
                    raise RuntimeError(f"private Xvfb did not start on its requested display: {detail}")
            self._cleanup = cleanup.pop_all()
        return self

    @property
    def pid(self):
        return self.process.pid

    def pattern(self, index):
        foreground, background = PATTERNS[index]
        subprocess.run(
            ["xsetroot", "-mod", "7", "5", "-fg", foreground, "-bg", background],
            env=self.environment, timeout=5, check=True,
        )

    @contextmanager
    def stall(self):
        self.process.send_signal(signal.SIGSTOP)
        try:
            deadline = monotonic() + 2
            while os.waitid(os.P_PID, self.pid, os.WSTOPPED | os.WNOHANG) is None:
                if monotonic() >= deadline:
                    raise RuntimeError("private Xvfb did not stop for the timeout control")
                sleep(0.001)
            yield
        finally:
            self.process.send_signal(signal.SIGCONT)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)

    def __exit__(self, *_exception):
        self._cleanup.close()


class PythonCapture:
    """Use the resident source and RGB24 conversion used by RamCapture."""

    def __init__(self, surface):
        config = x11_ramcap.SourceConfig(
            surface.directory, "Xvfb_screen0", surface.width, surface.height,
            f"Xvfb process {surface.pid} -nocursor",
        )
        self.source = x11_ramcap._Source(config, surface.width * surface.height * 4 + 4096, xfb.TORN_TRIES)

    def next_frame(self):
        raw = self.source.read(lambda: None)
        pixels = xfb.rgb24(raw)
        self.source.check()
        return pixels, hashlib.sha256(pixels).digest()

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.source.close()
