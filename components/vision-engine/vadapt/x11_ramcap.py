"""D11 bounded, trusted Xvfb capture into retained RAM rings.

Only xfb decodes XWD. See docs/spec/d11_x11_ram_capture_spec.md for the
Python lifecycle, configuration trust boundary and JSON-line protocol.
"""
from dataclasses import dataclass, field
import errno
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from types import MappingProxyType
import uuid

import numpy as np

from vadapt import ramws, rtring, xfb


class CaptureError(Exception):
    """A fail-closed capture/configuration/control error with a stable .code."""

    def __init__(self, code, detail=""):
        self.code = code
        super().__init__(code + (" " + detail if detail else ""))


def _positive(value, name, maximum=None, code="E_CONFIG"):
    if type(value) is not int or value <= 0 or (maximum is not None and value > maximum):
        raise CaptureError(code, name + " must be a positive exact integer in range")
    return value


def _name(value, code="E_CONFIG"):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:
        raise CaptureError(code, "invalid source/session identifier")
    return value


@dataclass(frozen=True)
class SourceConfig:
    """Trusted operator assertion; never constructed from a control request.

    root is an absolute RAM directory; path is relative without aliases.
    nocursor_evidence attests the source was launched with -nocursor.
    """

    root: str
    path: str
    width: int
    height: int
    nocursor_evidence: str
    kind: str = "xvfb-xwd"

    def __post_init__(self):
        try:
            root, rel = os.fspath(self.root), os.fspath(self.path)
        except TypeError as exc:
            raise CaptureError("E_SOURCE_PATH", "root/path must be text paths") from exc
        if (type(root) is not str or not os.path.isabs(root) or "\0" in root
                or ".." in root.split("/") or "." in root.split("/")):
            raise CaptureError("E_SOURCE_PATH", "absolute unaliased root required")
        if (type(rel) is not str or "\0" in rel or os.path.isabs(rel)
                or any(p in ("", ".", "..") for p in rel.split("/"))):
            raise CaptureError("E_SOURCE_PATH", "relative unaliased source required")
        if self.kind != "xvfb-xwd":
            raise CaptureError("E_SOURCE_KIND", "only exact xvfb-xwd is supported")
        if type(self.nocursor_evidence) is not str or not self.nocursor_evidence.strip():
            raise CaptureError("E_NOCURSOR", "trusted -nocursor evidence required")
        _positive(self.width, "width", xfb.MAX_SIDE)
        _positive(self.height, "height", xfb.MAX_SIDE)
        object.__setattr__(self, "root", os.path.normpath(root))
        object.__setattr__(self, "path", rel)


def _directory(path):
    """Walk an absolute directory with no symlink traversal, including parents."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in Path(path).parts[1:]:
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _ram_descriptor(fd):
    # /proc resolves the already-open object, not a second untrusted pathname.
    if ramws.is_ram_fs("/proc/self/fd/%d" % fd) not in ramws.RAM_FS:
        raise CaptureError("E_RAM_NOT_TMPFS", "opened storage is not verified tmpfs/ramfs")


def _open_source(config):
    directory = leaf = None
    try:
        directory = _directory(config.root)
        _ram_descriptor(directory)
        parts = config.path.split("/")
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                          dir_fd=directory)
            os.close(directory)
            directory = nxt
            _ram_descriptor(directory)
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                       dir_fd=directory)
        _ram_descriptor(leaf)
        if not stat.S_ISREG(os.fstat(leaf).st_mode):
            raise CaptureError("E_SOURCE_KIND", "source must be a regular XWD file")
        result, leaf = leaf, None
        return result
    except OSError as exc:
        code = "E_SOURCE_PATH" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else "E_SOURCE_IO"
        raise CaptureError(code, "cannot open configured source") from exc
    finally:
        if leaf is not None:
            os.close(leaf)
        if directory is not None:
            os.close(directory)


def _identity(fd):
    st = os.fstat(fd)
    return st.st_dev, st.st_ino, st.st_size


class _Source:
    def __init__(self, config, max_bytes, tries):
        self.config, self.max_bytes, self.tries = config, max_bytes, tries
        self.fd = _open_source(config)
        try:
            self.identity = _identity(self.fd)
        except BaseException:
            self.close()
            raise
        self.layout = None

    def check(self):
        other = None
        try:
            if self.fd is None or _identity(self.fd) != self.identity:
                raise CaptureError("E_SOURCE_CHANGED", "source descriptor identity/size changed")
            other = _open_source(self.config)
            if _identity(other) != self.identity:
                raise CaptureError("E_SOURCE_CHANGED", "configured source was replaced or resized")
            _ram_descriptor(self.fd)
            if self.layout is not None and os.pread(self.fd, xfb.MIN_HEADER, 0) != self.layout:
                raise CaptureError("E_SOURCE_GEOMETRY", "source XWD header/layout changed")
        except CaptureError as exc:
            if exc.code == "E_SOURCE_IO":
                raise CaptureError("E_SOURCE_CHANGED", "configured source disappeared") from exc
            raise
        finally:
            if other is not None:
                os.close(other)

    def read(self, guard):
        def before_read():
            guard()
            self.check()

        raw = xfb.read_screen_fd(self.fd, max_bytes=self.max_bytes, tries=self.tries,
                                 width=self.config.width, height=self.config.height,
                                 before_read=before_read)
        before_read()
        layout = raw[:xfb.MIN_HEADER]
        if self.layout is not None and layout != self.layout:
            raise CaptureError("E_SOURCE_GEOMETRY", "source XWD header/layout changed")
        self.layout = layout
        return raw

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)


def fbread(source, *, max_bytes, tries=xfb.TORN_TRIES, cancelled=None):
    """Return a bounded, validated raw XWD; never write a frame file."""
    if not isinstance(source, SourceConfig):
        raise CaptureError("E_CONFIG", "SourceConfig required")
    _positive(max_bytes, "max_bytes")
    if type(tries) is not int or tries < 2:
        raise CaptureError("E_CONFIG", "tries must be an exact integer >= 2")
    if cancelled is not None and not callable(cancelled):
        raise CaptureError("E_CONFIG", "cancelled must be callable")

    def guard():
        if cancelled is not None and cancelled():
            raise CaptureError("E_CANCELLED")

    guard()
    handle = _Source(source, max_bytes, tries)
    try:
        return handle.read(guard)
    finally:
        handle.close()


@dataclass
class _Session:
    session_id: str
    alias: str
    params: tuple
    config: SourceConfig
    size: int
    expires_ns: int
    interval_ns: int
    cancel: object
    next_due_ns: int = 0
    state: str = "starting"
    last_error: str = None
    frames: int = 0
    source: object = None
    workspace: object = None
    writer: object = None
    reader: object = None
    thread: object = None
    ring_path: str = None
    lock: object = field(default_factory=threading.RLock)


def _workspace(session_id, size):
    return ramws.open_run("sessions", uuid.uuid4().hex, size, tool="x11_ramcap")


def _wait(event, timeout_ns):
    # All deadline arithmetic is integer nanoseconds; Event.wait accepts seconds.
    bounded_ns = min(timeout_ns, int(threading.TIMEOUT_MAX) * 1_000_000_000)
    return event.wait(bounded_ns / 1_000_000_000)


class RamCapture:
    """Own bounded retained rings. All configuration and injected seams are trusted.

    Stop retains storage; Reap performs retryable cleanup. A borrowed view must
    be dropped (including derived slices) before its ring can be reaped.
    """

    def __init__(self, sources, *, max_frame_bytes, max_session_bytes, max_total_bytes,
                 max_sessions, ring_slots, interval_ns, ttl_ns, tries=xfb.TORN_TRIES,
                 workspace_factory=None, clock=time.monotonic_ns, wait=None,
                 cancel_factory=threading.Event):
        for key, value in (("max_frame_bytes", max_frame_bytes), ("max_session_bytes", max_session_bytes),
                           ("max_total_bytes", max_total_bytes), ("max_sessions", max_sessions),
                           ("interval_ns", interval_ns), ("ttl_ns", ttl_ns)):
            _positive(value, key)
        _positive(ring_slots, "ring_slots", (1 << 32) - 1)
        if type(tries) is not int or tries < 2:
            raise CaptureError("E_CONFIG", "tries must be an exact integer >= 2")
        try:
            configured = dict(sources)
        except (TypeError, ValueError) as exc:
            raise CaptureError("E_CONFIG", "source mapping required") from exc
        if not configured:
            raise CaptureError("E_CONFIG", "at least one trusted source is required")
        for alias, config in configured.items():
            _name(alias)
            if not isinstance(config, SourceConfig):
                raise CaptureError("E_CONFIG", "source values must be SourceConfig")
        self.sources = MappingProxyType(configured)
        self.max_frame_bytes, self.max_session_bytes = max_frame_bytes, max_session_bytes
        self.max_total_bytes, self.max_sessions = max_total_bytes, max_sessions
        self.ring_slots, self.interval_ns, self.ttl_ns, self.tries = ring_slots, interval_ns, ttl_ns, tries
        self.workspace_factory = _workspace if workspace_factory is None else workspace_factory
        self.clock, self.wait, self.cancel_factory = clock, _wait if wait is None else wait, cancel_factory
        if not all(callable(f) for f in (self.workspace_factory, self.clock, self.wait, self.cancel_factory)):
            raise CaptureError("E_CONFIG", "injected seams must be callable")
        self._sessions = {}
        self._lock, self._clock_lock = threading.RLock(), threading.Lock()
        self._last_clock = None
        self._closed = False

    def _now(self):
        with self._clock_lock:
            now = self.clock()
            if type(now) is not int or now < 0 or (self._last_clock is not None and now < self._last_clock):
                raise CaptureError("E_CLOCK", "clock must return monotonic nonnegative integer nanoseconds")
            self._last_clock = now
            return now

    def _get(self, session_id):
        _name(session_id)
        with self._lock:
            return self._sessions.get(session_id)

    @staticmethod
    def _metadata(s):
        return {"session_id": s.session_id, "source": s.alias, "state": s.state,
                "ring_path": s.ring_path, "ring_bytes": s.size, "frames": s.frames,
                "expires_ns": s.expires_ns, "next_due_ns": s.next_due_ns,
                "last_error": s.last_error}

    def _guard(self, s):
        if s.cancel.is_set():
            raise CaptureError("E_CANCELLED")
        if self._now() >= s.expires_ns:
            s.state = "expired"
            s.cancel.set()
            raise CaptureError("E_EXPIRED")

    @staticmethod
    def _workspace_root(ws):
        try:
            root = os.fspath(ws.root)
            if (type(root) is not str or not os.path.isabs(root) or "\0" in root
                    or any(p in (".", "..") for p in root.split("/"))):
                raise CaptureError("E_RAM_ESCAPE", "absolute workspace root required")
            fd = _directory(root)
            try:
                _ram_descriptor(fd)
            finally:
                os.close(fd)
            return os.path.realpath(root)
        except OSError as exc:
            raise CaptureError("E_RAM_ESCAPE", "unsafe workspace root") from exc

    @classmethod
    def _check_workspace(cls, ws):
        try:
            root = cls._workspace_root(ws)
            path = os.fspath(ws.path(rtring.REL))
            if os.path.realpath(path) != os.path.join(root, rtring.REL):
                raise CaptureError("E_RAM_ESCAPE", "ring path escapes its workspace")
            parent = _directory(os.path.dirname(path))
            try:
                _ram_descriptor(parent)
            finally:
                os.close(parent)
            if os.path.lexists(path):
                raise CaptureError("E_RAM_ESCAPE", "ring target already exists")
        except OSError as exc:
            raise CaptureError("E_RAM_ESCAPE", "unsafe workspace path") from exc

    def Start(self, session_id, source, *, slots=None, interval_ns=None, ttl_ns=None, background=True):
        """Create and initially publish a session, or return an identical retained session."""
        _name(session_id)
        _name(source)
        slots = self.ring_slots if slots is None else _positive(slots, "slots")
        interval_ns = self.interval_ns if interval_ns is None else _positive(interval_ns, "interval_ns")
        ttl_ns = self.ttl_ns if ttl_ns is None else _positive(ttl_ns, "ttl_ns")
        if type(background) is not bool:
            raise CaptureError("E_CONFIG", "background must be bool")
        if slots > self.ring_slots or interval_ns < self.interval_ns or ttl_ns > self.ttl_ns:
            raise CaptureError("E_CONFIG", "request exceeds configured slots/cadence/lifetime policy")
        params = (source, slots, interval_ns, ttl_ns, background)
        with self._lock:
            if self._closed:
                raise CaptureError("E_CLOSED")
            if source not in self.sources:
                raise CaptureError("E_SOURCE_UNKNOWN")
            old = self._sessions.get(session_id)
            if old is not None:
                if old.params != params or old.state == "cleanup_pending":
                    raise CaptureError("E_SESSION_CONFLICT")
                with old.lock:
                    if old.state == "running" and self._now() >= old.expires_ns:
                        old.state = "expired"
                        old.cancel.set()
                    return self._metadata(old)
            config = self.sources[source]
            size = rtring.ring_bytes(slots, config.width, config.height)
            if (size > min(self.max_session_bytes, sys.maxsize) or self.SizeBytes() + size > self.max_total_bytes
                    or len(self._sessions) >= self.max_sessions):
                raise CaptureError("E_BUDGET", "session/ring budget exceeded")
            cancel = self.cancel_factory()
            if not all(callable(getattr(cancel, n, None)) for n in ("set", "is_set", "wait")):
                raise CaptureError("E_CONFIG", "cancellation event must provide set/is_set/wait")
            s = _Session(session_id, source, params, config, size, self._now() + ttl_ns,
                         interval_ns, cancel)
            # A failed rollback remains registered and charged until a later Reap succeeds.
            self._sessions[session_id] = s
            try:
                self._guard(s)
                s.source = _Source(config, self.max_frame_bytes, self.tries)
                raw = s.source.read(lambda: self._guard(s))
                rgb = xfb.rgb24(raw)
                workspace = self.workspace_factory(session_id, size)
                # An invalid factory root is never owned, closed, or charged on rollback.
                new_root = self._workspace_root(workspace)
                for configured in self.sources.values():
                    source_path = os.path.realpath(os.path.join(configured.root, configured.path))
                    if source_path == new_root or source_path.startswith(new_root + os.sep):
                        raise CaptureError("E_RAM_ESCAPE", "workspace cleanup must not contain a source")
                for other in self._sessions.values():
                    if other is not s and other.workspace is not None:
                        old_root = os.path.realpath(other.workspace.root)
                        if (old_root == new_root or old_root.startswith(new_root + os.sep)
                                or new_root.startswith(old_root + os.sep)):
                            # Refuse ownership before cleanup can reach another live session.
                            raise CaptureError("E_RAM_ESCAPE", "session workspaces must not overlap")
                s.workspace = workspace
                self._check_workspace(s.workspace)
                s.writer = rtring.RingWriter(s.workspace, slots, config.width, config.height)
                s.ring_path = str(s.writer.path)
                s.reader = rtring.RingReader(s.writer.path, root=s.workspace.root)
                s.source.check()
                self._guard(s)
                s.writer.publish(np.frombuffer(rgb, dtype=np.uint8).reshape(config.height, config.width, 3))
                s.frames = 1
                s.next_due_ns = self._now() + interval_ns
                s.state = "running"
                result = self._metadata(s)
                if background:
                    s.thread = threading.Thread(target=self._run, args=(s,), name="x11-" + session_id, daemon=True)
                    try:
                        s.thread.start()
                    except BaseException:
                        s.thread = None
                        raise
                return result
            except BaseException as exc:
                s.last_error = getattr(exc, "code", "E_CAPTURE_IO")
                s.cancel.set()
                s.state = "cleanup_pending"
                if self._cleanup(s):
                    self._sessions.pop(session_id, None)
                if isinstance(exc, (CaptureError, xfb.XwdError, ramws.RamError)):
                    raise
                if isinstance(exc, (OSError, ValueError, RuntimeError, MemoryError)):
                    raise CaptureError("E_CAPTURE_IO", "session initialization failed") from exc
                raise

    def _capture(self, s, should_wait):
        while True:
            with s.lock:
                if s.state != "running":
                    raise CaptureError("E_SESSION_STOPPED", "session is not running")
                attempted = False
                try:
                    self._guard(s)
                    now = self._now()
                    if now < s.next_due_ns:
                        if not should_wait:
                            return None
                        delay = min(s.next_due_ns, s.expires_ns) - now
                    else:
                        attempted = True
                        raw = s.source.read(lambda: self._guard(s))
                        rgb = xfb.rgb24(raw)
                        s.source.check()
                        self._guard(s)
                        if s.frames >= (1 << 64) - 1:
                            raise CaptureError("E_BUDGET", "ring render index exhausted")
                        index = s.writer.publish(np.frombuffer(rgb, dtype=np.uint8).reshape(
                            s.config.height, s.config.width, 3))
                        s.frames = index + 1
                        s.last_error = None
                        return s.reader.acquire(index)
                except (CaptureError, xfb.XwdError, ramws.RamError, OSError, ValueError, MemoryError) as exc:
                    s.last_error = getattr(exc, "code", "E_CAPTURE_IO")
                    if s.last_error == "E_CANCELLED" and s.state == "running":
                        s.state = "stopped"
                    if s.last_error not in ("E_TORN", "E_CANCELLED", "E_EXPIRED"):
                        s.state = "failed"
                        s.cancel.set()
                    if isinstance(exc, (CaptureError, xfb.XwdError, ramws.RamError)):
                        raise
                    raise CaptureError("E_CAPTURE_IO", "capture failed") from exc
                finally:
                    # Completion-relative cadence; a not-due poll never moves the deadline.
                    if attempted:
                        s.next_due_ns = self._now() + s.interval_ns
            # Never hold the session lock while waiting: Stop and idempotent Start stay prompt.
            self.wait(s.cancel, delay)

    def capture_once(self, session_id, *, wait=False):
        """Publish one due frame; return a borrowed read-only Frame, or None when not due."""
        if type(wait) is not bool:
            raise CaptureError("E_CONFIG", "wait must be bool")
        s = self._get(session_id)
        if s is None:
            raise CaptureError("E_SESSION_UNKNOWN")
        return self._capture(s, wait)

    def _run(self, s):
        while not s.cancel.is_set():
            try:
                frame = self._capture(s, True)
                del frame  # workers never retain a borrowed ring view
            except (CaptureError, xfb.XwdError, ramws.RamError) as exc:
                if exc.code != "E_TORN":
                    break
        with s.lock:
            if s.state == "running":
                s.state = "stopped"

    def Stop(self, session_id):
        """Cancel and join production, retaining the ring and full charge. Idempotent."""
        s = self._get(session_id)
        if s is None:
            return False
        self._stop_session(s)
        return True

    @staticmethod
    def _stop_session(s):
        """Stop this object, even if another session later reuses its identifier."""
        s.cancel.set()
        if s.thread is not None and s.thread is not threading.current_thread():
            s.thread.join()
        with s.lock:
            if s.source is not None:
                s.source.close()
                s.source = None
            if s.state in ("starting", "running"):
                s.state = "stopped"

    def SizeBytes(self, session_id=None):
        """Logical ring allocation, including stopped/expired/cleanup-pending sessions."""
        if session_id is not None:
            _name(session_id)
        with self._lock:
            if session_id is None:
                return sum(s.size for s in self._sessions.values())
            s = self._sessions.get(session_id)
            return 0 if s is None else s.size

    @staticmethod
    def _cleanup(s):
        """Return True only after all owned resources are actually released."""
        with s.lock:
            try:
                if s.source is not None:
                    s.source.close()
                    s.source = None
                if s.reader is not None:
                    s.reader.close()
                    if not s.reader._mm.closed:
                        return False
                    s.reader = None
                if s.writer is not None:
                    s.writer.close()
                    if not s.writer._mm.closed:
                        return False
                    # The bound writer marks _released before ws.release: retry a failed unlink.
                    if os.path.lexists(s.writer.path):
                        s.workspace.release(s.writer.path)
                    if os.path.lexists(s.writer.path):
                        return False
                    s.writer = None
                if s.workspace is not None:
                    s.workspace.close()
                    if os.path.lexists(s.workspace.root):
                        return False
                    s.workspace = None
                return True
            except (OSError, ramws.RamError, CaptureError, BufferError) as exc:
                s.last_error = getattr(exc, "code", "E_CLEANUP")
                return False

    def Reap(self, session_id=None, *, force=False):
        """Reap stopped/expired sessions, or cancel all selected sessions with force=True."""
        if session_id is not None:
            _name(session_id)
        if type(force) is not bool:
            raise CaptureError("E_CONFIG", "force must be bool")
        # Forced shutdown must still cancel/reap if an injected clock is broken.
        now = None if force else self._now()
        with self._lock:
            selected = [s for k, s in sorted(self._sessions.items()) if session_id is None or k == session_id]
        eligible = []
        retained = []
        for s in selected:
            if force or s.state != "running" or now >= s.expires_ns:
                s.cancel.set()  # cancel every selected producer before any join
                eligible.append(s)
            else:
                retained.append(s.session_id)
        reaped = []
        for s in eligible:
            self._stop_session(s)
            with s.lock:
                s.state = "cleanup_pending"
                clean = self._cleanup(s)
            if clean:
                with self._lock:
                    if self._sessions.get(s.session_id) is s:
                        del self._sessions[s.session_id]
                reaped.append(s.session_id)
            else:
                retained.append(s.session_id)
        return {"reaped": sorted(reaped), "retained": sorted(retained)}

    def close(self):
        """Prevent new sessions, cancel workers, and retry cleanup; may retain borrowed rings."""
        with self._lock:
            self._closed = True
        return self.Reap(force=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise CaptureError("E_CONTROL_SCHEMA", "duplicate JSON key")
        out[key] = value
    return out


def _constant(value):
    raise CaptureError("E_CONTROL_SCHEMA", "nonfinite JSON number")


_REQUESTS = {
    "Start": {"op", "request_id", "session_id", "source", "slots", "interval_ns", "ttl_ns"},
    "Stop": {"op", "request_id", "session_id"},
    "SizeBytes": {"op", "request_id", "session_id"},
    "Reap": {"op", "request_id", "session_id", "force"},
}


def _request(capture, obj):
    if type(obj) is not dict:
        raise CaptureError("E_CONTROL_SCHEMA")
    op = obj.get("op")
    if type(op) is not str or op not in _REQUESTS or set(obj) != _REQUESTS[op]:
        raise CaptureError("E_CONTROL_SCHEMA")
    _positive(obj["request_id"], "request_id", code="E_CONTROL_SCHEMA")
    sid = obj["session_id"]
    if sid is not None or op in ("Start", "Stop"):
        _name(sid, "E_CONTROL_SCHEMA")
    if op == "Start":
        _name(obj["source"], "E_CONTROL_SCHEMA")
        for k in ("slots", "interval_ns", "ttl_ns"):
            _positive(obj[k], k, code="E_CONTROL_SCHEMA")
        return capture.Start(sid, obj["source"], slots=obj["slots"],
                             interval_ns=obj["interval_ns"], ttl_ns=obj["ttl_ns"])
    if op == "Stop":
        return {"stopped": capture.Stop(sid)}
    if op == "SizeBytes":
        return {"bytes": capture.SizeBytes(sid)}
    if type(obj["force"]) is not bool:
        raise CaptureError("E_CONTROL_SCHEMA")
    return capture.Reap(sid, force=obj["force"])


def serve(capture, input_binary, output_text, *, max_control_line_bytes):
    """Serve bounded JSON lines. EOF or any stream failure cancels/reaps all sessions.

    The byte limit includes LF. A final unterminated line within the cap is
    accepted. Oversized lines are drained in bounded chunks, not accumulated.
    Returns close()'s reaped/retained result; EOF emits no extra reply.
    """
    _positive(max_control_line_bytes, "max_control_line_bytes", sys.maxsize - 1)
    try:
        while True:
            raw = input_binary.readline(max_control_line_bytes + 1)
            if not raw:
                break
            request_id = op = result = None
            code = "OK"
            try:
                if not isinstance(raw, bytes):
                    raise CaptureError("E_CONTROL_SCHEMA", "binary input required")
                if len(raw) > max_control_line_bytes:
                    while not raw.endswith(b"\n"):
                        raw = input_binary.readline(min(max_control_line_bytes + 1, 65536))
                        if not raw:
                            break
                    raise CaptureError("E_CONTROL_LINE", "control line exceeds configured byte cap")
                obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
                if type(obj) is dict:
                    rid, operation = obj.get("request_id"), obj.get("op")
                    request_id = rid if type(rid) is int and rid > 0 else None
                    op = operation if type(operation) is str and operation in _REQUESTS else None
                result = _request(capture, obj)
            except (UnicodeError, ValueError, TypeError, RecursionError):
                code = "E_CONTROL_SCHEMA"
            except (CaptureError, xfb.XwdError, ramws.RamError) as exc:
                code = exc.code
            except OSError:
                code = "E_CAPTURE_IO"
            output_text.write(json.dumps({"request_id": request_id, "op": op, "ok": code == "OK",
                                          "code": code, "result": result},
                                         sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            output_text.flush()
    finally:
        cleanup = capture.close()
    return cleanup


_SOURCE_OPTIONS = {"source", "source-root", "source-path", "width", "height", "nocursor-evidence",
                   "max-frame-bytes"}
_SERVE_OPTIONS = {"max-session-bytes", "max-total-bytes", "max-sessions", "ring-slots", "interval-ns",
                  "ttl-ns", "max-control-line-bytes"}


def _cli(argv):
    if not argv or argv[0] not in ("fbread", "serve"):
        raise CaptureError("E_USAGE", "expected fbread or serve")
    cmd, values, i = argv[0], {}, 1
    required = _SOURCE_OPTIONS | (_SERVE_OPTIONS if cmd == "serve" else set())
    allowed = required | {"tries", "source-kind"}
    # Parse the entire command before constructing a source or touching storage.
    while i < len(argv):
        token = argv[i]
        i += 1
        if not token.startswith("--"):
            raise CaptureError("E_USAGE", "expected named option")
        key, sep, value = token[2:].partition("=")
        if key not in allowed or key in values:
            raise CaptureError("E_USAGE", "unknown or repeated option: --" + key)
        if not sep:
            if i >= len(argv) or argv[i].startswith("--"):
                raise CaptureError("E_USAGE", "missing option value")
            value = argv[i]
            i += 1
        if not value:
            raise CaptureError("E_USAGE", "empty option value")
        values[key] = value
    if not required.issubset(values):
        raise CaptureError("E_USAGE", "missing required options: " + ",".join(sorted(required - values.keys())))
    numeric = {"width", "height", "max-frame-bytes", "tries"} | (_SERVE_OPTIONS if cmd == "serve" else set())
    values.setdefault("tries", "8")
    for key in numeric:
        value = values[key]
        if re.fullmatch(r"[1-9][0-9]*", value) is None:
            raise CaptureError("E_USAGE", "positive decimal integer required: --" + key)
        try:
            values[key] = int(value)
        except ValueError as exc:
            raise CaptureError("E_USAGE", "integer too long") from exc
    if values["tries"] < 2:
        raise CaptureError("E_USAGE", "tries must be >= 2")
    return cmd, values


def main(argv=None, *, stdin=None, stdout=None, stderr=None):
    """CLI: raw validated fbread stdout; stable JSON control replies for serve."""
    argv = sys.argv[1:] if argv is None else list(argv)
    stderr = sys.stderr if stderr is None else stderr
    try:
        cmd, options = _cli(argv)
        source = SourceConfig(options["source-root"], options["source-path"], options["width"],
                              options["height"], options["nocursor-evidence"],
                              options.get("source-kind", "xvfb-xwd"))
        _name(options["source"])
        if cmd == "fbread":
            raw = fbread(source, max_bytes=options["max-frame-bytes"], tries=options["tries"])
            output = sys.stdout.buffer if stdout is None else getattr(stdout, "buffer", stdout)
            output.write(raw)
            output.flush()
        else:
            capture = RamCapture({options["source"]: source}, max_frame_bytes=options["max-frame-bytes"],
                max_session_bytes=options["max-session-bytes"], max_total_bytes=options["max-total-bytes"],
                max_sessions=options["max-sessions"], ring_slots=options["ring-slots"],
                interval_ns=options["interval-ns"], ttl_ns=options["ttl-ns"], tries=options["tries"])
            cleanup = serve(capture, sys.stdin.buffer if stdin is None else stdin,
                            sys.stdout if stdout is None else stdout,
                            max_control_line_bytes=options["max-control-line-bytes"])
            if cleanup["retained"]:
                raise CaptureError("E_CLEANUP", "sessions remain charged for pending cleanup")
        return 0
    except (CaptureError, xfb.XwdError, ramws.RamError) as exc:
        stderr.write(json.dumps({"code": exc.code, "detail": str(exc)}, sort_keys=True) + "\n")
        return 2 if exc.code in ("E_USAGE", "E_CONFIG") else 5
    except (OSError, ValueError, MemoryError) as exc:
        stderr.write(json.dumps({"code": "E_CAPTURE_IO", "detail": type(exc).__name__}, sort_keys=True) + "\n")
        return 5


if __name__ == "__main__":
    sys.exit(main())
