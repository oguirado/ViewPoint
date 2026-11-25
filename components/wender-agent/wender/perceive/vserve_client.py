# @module {"id":"wa.perceive.vserve_client","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.vision_adapter"],"book":"HB-WA-s1-perceive"}
"""Independent, bounded JSON-lines client for vision-engine's persistent server.

[TARGET, unit decision] One lifetime restart for a disconnected transport; never
retry a timeout, protocol refusal, or malformed reply. Access is serialized.
"""
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import tempfile
import threading
import time

from .vision_adapter import Frame


class VServeError(RuntimeError):
    def __init__(self, message, *, unavailable=False):
        super().__init__(message)
        self.unavailable = unavailable


# @anchor:wa.perceive.vserve_client.VServeTimeoutError
class VServeTimeoutError(VServeError, TimeoutError):
    """The request's queue or exchange deadline expired."""


class VServeClient:
    CONTROL_CAP = 65536
    REPLY_CAP = 8 * 1024 * 1024
    FRAME_CAP = 33554432

    def __init__(self, root, *, python="python3", timeout=2.0, max_restarts=1):
        self.root = Path(root).resolve()
        server = self.root / "vadapt" / "vserve.py"
        if not server.is_file() or server.is_symlink():
            raise ValueError("persistent backend requires vadapt/vserve.py")
        self._check_root()
        python = os.fspath(python)
        if not python or any(c.isspace() for c in python) or "\0" in python or python.startswith("-"):
            raise ValueError("python must name a single executable")
        executable = shutil.which(python)
        if not executable:
            raise ValueError("python executable is unavailable")
        self.python = os.path.abspath(executable)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if type(max_restarts) is not int or not 0 <= max_restarts <= 4:
            raise ValueError("max_restarts must be in 0..4")
        self.timeout, self.max_restarts = timeout, max_restarts
        self.restarts = 0
        self._proc = None
        self._seq = 0
        self._rx = bytearray()
        self._lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._closed = threading.Event()
        self.frame_cap = self.FRAME_CAP
        self.control_cap = self.CONTROL_CAP

    def _check_root(self):
        for entry in self.root.iterdir():
            name = entry.name.lower()
            if (name in ("sitecustomize", "usercustomize")
                    or name.startswith(("sitecustomize.", "usercustomize.")) or name.endswith(".pth")):
                raise ValueError("vision-engine root contains forbidden startup hook")

    def _env(self):
        self._check_root()
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ}
        env.update(PYTHONPATH=str(self.root), PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1",
                   PYTHONDONTWRITEBYTECODE="1")
        return env

    def _stop(self):
        with self._proc_lock:
            child = self._proc
        self._rx.clear()
        if child is not None:
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
            try:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                child.kill()
                try:
                    child.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    self._closed.set()
                    raise
            with self._proc_lock:
                self._proc = None

    @staticmethod
    def _unlink(path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    # @anchor:wa.perceive.vserve_client.VServeClient.cancel
    def cancel(self):
        """Refuse new work and interrupt any active exchange before close drains it."""
        with self._proc_lock:
            self._closed.set()
            if self._proc is not None and self._lock.locked():
                self._proc.kill()

    def close(self):
        with self._lock:
            self._closed.set()
            self._stop()

    def _roundtrip(self, op, fields, deadline):
        self._seq += 1
        request_id = self._seq
        wire = json.dumps(dict(fields, request_id=request_id, op=op), separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(wire) > self.control_cap:
            raise VServeError("vserve request exceeds control cap")
        child = self._proc
        # Nonblocking writes ensure even a server that stops reading respects the deadline.
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdin, selectors.EVENT_WRITE, "write")
            selector.register(child.stdout, selectors.EVENT_READ, "read")
            selector.register(child.stderr, selectors.EVENT_READ, "stderr")
            offset, stderr_bytes = 0, 0
            while True:
                if self._closed.is_set():
                    raise VServeError("vserve client is closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VServeTimeoutError("vserve timed out")
                if offset == len(wire) and b"\n" in self._rx:
                    raw, _, tail = self._rx.partition(b"\n")
                    self._rx = bytearray(tail)
                    break
                # Recheck cancellation even if a child-held pipe stays open.
                for key, _ in selector.select(min(remaining, 0.05)):
                    try:
                        if key.data == "write":
                            offset += os.write(child.stdin.fileno(), wire[offset:])
                            if offset == len(wire):
                                selector.unregister(child.stdin)
                        else:
                            block = os.read(key.fileobj.fileno(), 65536)
                            if not block:
                                if key.data == "read":
                                    raise VServeError("vserve EOF", unavailable=True)
                                selector.unregister(key.fileobj)
                            elif key.data == "read":
                                self._rx.extend(block)
                                if len(self._rx) > self.REPLY_CAP:
                                    raise VServeError("vserve reply exceeds cap")
                            else:
                                stderr_bytes += len(block)
                                if stderr_bytes > self.REPLY_CAP:
                                    raise VServeError("vserve stderr exceeds cap")
                    except BlockingIOError:
                        continue
        try:
            reply = json.loads(raw)
            if (not isinstance(reply, dict) or set(reply) != {
                    "schema", "request_id", "op", "ok", "rc", "code", "detail", "result"}
                    or reply["schema"] != "ve.vserve.reply/v1"
                    or type(reply["request_id"]) is not int or reply["request_id"] != request_id
                    or reply["op"] != op or type(reply["ok"]) is not bool
                    or type(reply["rc"]) is not int or reply["rc"] not in (0, 2, 3, 4, 5, 6)
                    or not isinstance(reply["code"], str) or not reply["code"]
                    or not isinstance(reply["detail"], (str, dict))):
                raise ValueError("reply envelope")
            if reply["ok"]:
                if (reply["rc"] != 0 or reply["code"] != "OK" or reply["detail"] not in ({}, "")
                        or not isinstance(reply["result"], dict)):
                    raise ValueError("success fields")
            else:
                if reply["rc"] == 0 or reply["code"] == "OK" or reply["result"] is not None:
                    raise ValueError("error fields")
                raise VServeError("vserve %s (rc=%s): %s" % (reply["code"], reply["rc"], str(reply["detail"])[:1000]),
                                  unavailable=reply["rc"] == 5)
            return reply["result"]
        except (ValueError, TypeError, KeyError) as exc:
            raise VServeError("invalid vserve reply: %s" % exc) from exc

    def _start(self, deadline):
        env = self._env()
        with self._proc_lock:
            if self._closed.is_set():
                raise VServeError("vserve client is closed")
            child = self._proc = subprocess.Popen(
                [self.python, "-m", "vadapt.vserve"], cwd=self.root, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        for stream in (child.stdin, child.stdout, child.stderr):
            os.set_blocking(stream.fileno(), False)
        hello = self._roundtrip("hello", {}, deadline)
        caps = hello.get("capabilities")
        if (hello.get("schema") != "ve.vserve.hello/v1" or not isinstance(caps, list)
                or "describe" not in caps or "frame_raw" not in caps
                or hello.get("raw_shm") is not True or hello.get("frame_raw") is not True
                or type(hello.get("ring")) is not bool):
            raise VServeError("vserve hello lacks raw capabilities")
        for name, maximum in (("max_control_line_bytes", self.CONTROL_CAP),
                              ("max_frame_bytes", self.FRAME_CAP), ("max_sessions", 16)):
            value = hello.get(name)
            if type(value) is not int or not 0 < value <= maximum:
                raise VServeError("invalid vserve hello budget")
        self.control_cap = hello["max_control_line_bytes"]
        self.frame_cap = hello["max_frame_bytes"]

    def describe(self, frame, prev=None, *, masks=(), marks_out=None, ocr_marks=False, deadline=None):
        deadline = min(deadline, time.monotonic() + self.timeout) if deadline is not None else time.monotonic() + self.timeout
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise VServeTimeoutError("vserve queue deadline expired")
        try:
            if self._closed.is_set():
                raise VServeError("vserve client is closed")
            while True:
                try:
                    with ExitStack() as stack:
                        fields = {"masks": masks, "ocr_marks": ocr_marks}
                        for value, key, raw_key in ((frame, "frame", "frame_raw"), (prev, "prev", "prev_raw")):
                            if value is None:
                                continue
                            if isinstance(value, Frame):
                                Frame.__post_init__(value)
                                if value.width > 8192 or value.height > 8192 or len(value.data) > self.FRAME_CAP:
                                    raise ValueError("frame exceeds vserve cap")
                                fd, path = tempfile.mkstemp(prefix="wender-vserve-", suffix=".rgb", dir="/dev/shm")
                                stack.callback(self._unlink, path)
                                with os.fdopen(fd, "wb") as stream:
                                    os.fchmod(stream.fileno(), 0o600)
                                    stream.write(value.data)
                                fields[raw_key] = "%s,%d,%d" % (path, value.width, value.height)
                            else:
                                path = Path(value).resolve()
                                if str(path).startswith("/proc/"):
                                    raise ValueError("vserve cannot receive process-local descriptors")
                                fields[key] = str(path)
                        if marks_out is not None:
                            fields["marks_out"] = str(marks_out)
                        if self._proc is None:
                            self._start(deadline)
                        for value in (frame, prev):
                            if isinstance(value, Frame) and len(value.data) > self.frame_cap:
                                raise VServeError("frame exceeds negotiated vserve cap")
                        return self._roundtrip("describe", fields, deadline)
                except (VServeError, OSError) as exc:
                    self._stop()
                    if self._closed.is_set():
                        raise VServeError("vserve client is closed") from None
                    unavailable = isinstance(exc, OSError) or isinstance(exc, VServeError) and exc.unavailable
                    if unavailable and self.restarts < self.max_restarts and time.monotonic() < deadline:
                        self.restarts += 1
                        continue
                    if isinstance(exc, VServeError):
                        raise
                    raise VServeError("vserve could not execute", unavailable=True) from exc
        finally:
            self._lock.release()
