"""Process-local timing for the optional protected-observation export."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import json
import os
import stat
import time


_PATH = os.environ.get("MV_OBS_STAGES_PATH", "")
ENABLED = os.path.isabs(_PATH)
_current = ContextVar("observe_stamps", default=None)


def now_ns():
    try:
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC)
    except OSError:
        return None


class Trace:
    def __init__(self, request_id, surface_id, session_id):
        self.request_id = request_id
        self.surface_id = surface_id if isinstance(surface_id, str) else None
        self.session_id = session_id if isinstance(session_id, str) else None
        self.frame = None
        self.failure = None
        self.rows = []

    def mark(self, stage, *, ns=None, outcome=None, reason=None):
        if ns is None:
            ns = now_ns()
        if outcome in ("refused", "timed_out", "cancelled", "stale"):
            self.failure = outcome, reason
        if ns is not None:
            self.rows.append((stage, ns, outcome, reason, self.frame))

    def returned(self, ok):
        outcome, reason = self.failure or ("refused", "observe_failed")
        self.mark("observe_returned", outcome="ok" if ok else outcome,
                  reason=None if ok else reason)

    def flush(self):
        if not self.rows:
            return
        try:
            boot, namespace = _host()
            if not boot or not namespace:
                return
            fd = os.open(_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                         os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or info.st_nlink != 1):
                    return
                for stage, ns, outcome, reason, frame in self.rows:
                    row = {
                        "stage": stage, "mono_ns": ns, "boot_id": boot,
                        "time_namespace": namespace, "pid": os.getpid(), "role": "agent",
                        "request_id": self.request_id, "attempt_id": self.request_id,
                        "frame_id": self.request_id,
                        "session_id": self.session_id, "surface_id": self.surface_id,
                        "route": "observe",
                    }
                    if frame is not None:
                        generation, digest = frame
                        row.update(generation=generation, frame_digest=digest,
                                   frame_id=f"{self.surface_id}:{generation}:{digest}")
                    if outcome is not None:
                        row["outcome"] = outcome
                    if reason is not None:
                        row["reason"] = reason
                    line = json.dumps(row, separators=(",", ":")).encode() + b"\n"
                    if len(line) <= 4096:
                        # Keep a row in a single append syscall across processes.
                        os.write(fd, line)
            finally:
                os.close(fd)
        except (OSError, ValueError):
            pass


@lru_cache(maxsize=1)
def _host():
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as boot:
        return boot.read().strip(), os.readlink("/proc/self/ns/time")


def current():
    return _current.get() if ENABLED else None


@contextmanager
def scoped(request_id, surface_id, session_id):
    trace = Trace(request_id, surface_id, session_id)
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)
        trace.flush()
