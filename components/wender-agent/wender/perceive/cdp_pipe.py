# @module {"id":"wa.perceive.cdp_pipe","layer":1,"owner":"PERCEIVE","deps":["wa.act.hosted_guard"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.cdp_pipe - minimal Chromium DevTools Protocol client over --remote-debugging-pipe.

No TCP port is opened: Chromium reads commands on fd 3 and writes replies/events on fd 4,
each message a JSON object terminated by a NUL byte. Stdlib only.
"""
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager, nullcontext

from wender.act.hosted_guard import (
    HOSTED_READONLY_CDP_METHODS,
    assert_actuation_allowed,
    is_readonly_cdp_method,
)

DEFAULT_TIMEOUT_S = 15  # [TARGET] per-command ceiling; not a measured latency
EVENT_BUFFER = 1024     # bounded: events and retained replies are hostile external data
MAX_DEFRAMER_BYTES = 16 * 1024 * 1024  # [TARGET, unit decision] 16 MiB cap on unframed CDP reply stream
CHROME_ENV = "CHROME_BIN"

# @anchor:wa.perceive.cdp_pipe.CdpError
class CdpError(Exception):
    """A CDP command failed, timed out, or the browser went away."""

# @anchor:wa.perceive.cdp_pipe.WriterFence
class WriterFence:
    """Shared, atomic STOP gate. Scheduler admission and all CDP writes share this gate.

    The gate is held only for a transport write, never while waiting for a reply.
    stop() serializes with writers, so no write can begin after STOP returns.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._stopped = False

    @contextmanager
    def writing(self):
        with self._lock:
            if self._stopped:
                raise CdpError("STOP: writer fence closed")
            yield

    def stop(self):
        with self._lock:
            self._stopped = True

    def permits(self):
        with self._lock:
            return not self._stopped


@contextmanager
def _callable_writer_guard(predicate):
    if not predicate():
        raise CdpError("STOP: writer fence closed")
    yield


# @anchor:wa.perceive.cdp_pipe.frame
def frame(msg):
    """Encode one CDP message as NUL-terminated compact JSON bytes."""
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\0"


# @anchor:wa.perceive.cdp_pipe.Deframer
class Deframer:
    """Incremental splitter for the NUL-framed reply stream."""

    def __init__(self, max_bytes=MAX_DEFRAMER_BYTES):
        self.max_bytes = max_bytes
        self._buf = b""

    def feed(self, data):
        self._buf += data
        out = []
        while True:
            i = self._buf.find(b"\0")
            if i < 0:
                if len(self._buf) > self.max_bytes:
                    self._buf = b""
                    raise CdpError("CDP deframer buffer overflow")
                return out
            chunk, self._buf = self._buf[:i], self._buf[i + 1:]
            if len(chunk) > self.max_bytes:
                self._buf = b""
                raise CdpError("CDP deframer frame exceeds maximum size")
            if chunk:
                out.append(json.loads(chunk.decode("utf-8")))


# @anchor:wa.perceive.cdp_pipe.chrome_args
def chrome_args(user_data_dir, headless=True, url="about:blank"):
    args = [
        "--remote-debugging-pipe",
        "--user-data-dir=" + user_data_dir,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--metrics-recording-only",
        "--mute-audio",
    ]
    if headless:
        args.append("--headless=new")
    args.append(url)
    return args


# @anchor:wa.perceive.cdp_pipe._rmtree_profile
def _rmtree_profile(path):
    """Remove temporary profile directory without invoking os.close via dir_fd."""
    if not path:
        return
    use_fd = getattr(shutil, "_use_fd_functions", False)
    try:
        shutil._use_fd_functions = False
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
    finally:
        shutil._use_fd_functions = use_fd

# @anchor:wa.perceive.cdp_pipe.CdpPipe
class CdpPipe:
    """One browser connection, monotonically unique request IDs and scoped event lanes.

    _replies retains at most EVENT_BUFFER diagnostics for unissued unsolicited
    identifiers. Issued, timed-out or retired identifiers are never replayable.
    Events have explicit bounded session lanes; overflow fences just that lane.
    """

    def __init__(self, chrome_bin=None, headless=True, timeout=DEFAULT_TIMEOUT_S,
                 *legacy_clock, clock=None, writer_fence=None):
        if legacy_clock:
            if len(legacy_clock) != 1 or clock is not None:
                raise TypeError("clock supplied twice or too many positional arguments")
            clock = legacy_clock[0]
        self.chrome_bin = chrome_bin or os.environ.get(CHROME_ENV)
        if not self.chrome_bin:
            raise CdpError("CHROME_BIN is not set and no chrome_bin was given")
        if clock is not None and not callable(clock):
            raise TypeError("clock must return monotonic nanoseconds")
        self.headless = headless
        self.timeout = timeout
        self.clock = clock
        self.writer_fence = writer_fence
        self.proc = None
        self._proc_reaped = False
        self.user_data_dir = None
        self._next_id = 0
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._replies = {}
        self._pending = set()
        self.events = deque(maxlen=EVENT_BUFFER)
        self._closed = False
        self._w = None
        self._r = None
        self._reader = None
        self._connection_incarnation = 0
        self._request_info = {}
        self._tombstones = {}
        self._events_by_session = {}
        self._lane_errors = {}
        self._session_incarnations = {}
        self._target_sessions = {}

    def _now_ns(self):
        clk = getattr(self, "clock", None) or getattr(self, "_clock", None)
        now = clk() if callable(clk) else time.monotonic_ns()
        if type(now) is not int:
            raise TypeError("clock must return monotonic nanoseconds as an integer")
        return now

    def _now(self):
        """Legacy seconds helper; timeout conversion itself always uses nanoseconds."""
        return self._now_ns() / 1_000_000_000

    def _deadline_ns(self, seconds):
        return self._now_ns() + max(0, int(float(seconds) * 1_000_000_000))

    def _state_locked(self):
        """Support legacy callers that construct a minimal client via __new__."""
        if getattr(self, "_pending", None) is None:
            self._pending = set()
        if getattr(self, "_request_info", None) is None:
            self._request_info = {}
        if getattr(self, "_tombstones", None) is None:
            self._tombstones = {}
        if getattr(self, "_events_by_session", None) is None:
            self._events_by_session = {}
        if getattr(self, "_lane_errors", None) is None:
            self._lane_errors = {}
        if getattr(self, "_session_incarnations", None) is None:
            self._session_incarnations = {}
        if getattr(self, "_target_sessions", None) is None:
            self._target_sessions = {}
        if getattr(self, "_connection_incarnation", None) is None:
            self._connection_incarnation = 0

    def _pending_set(self):
        self._state_locked()
        return self._pending

    def _writer_guard(self):
        fence = getattr(self, "writer_fence", None)
        if fence is None:
            return nullcontext()
        writing = getattr(fence, "writing", None)
        if callable(writing):
            return writing()
        if hasattr(fence, "__enter__") and hasattr(fence, "__exit__"):
            return fence
        if callable(fence):
            return _callable_writer_guard(fence)
        raise TypeError("writer_fence must provide writing(), context management or a predicate")

    def _write_command(self, message):
        os.write(self._w, frame(message))

    def _expire_tombstones_locked(self):
        self._state_locked()
        now = self._now_ns()
        for rid, until in tuple(self._tombstones.items()):
            if until <= now:
                del self._tombstones[rid]
        while len(self._tombstones) > EVENT_BUFFER * 4:
            self._tombstones.pop(next(iter(self._tombstones)))

    def _tombstone_locked(self, rid):
        self._state_locked()
        self._expire_tombstones_locked()
        self._tombstones[rid] = self._now_ns() + 60_000_000_000
        while len(self._tombstones) > EVENT_BUFFER * 4:
            self._tombstones.pop(next(iter(self._tombstones)))

    def _fence_lane_locked(self, session_id, reason):
        self._state_locked()
        self._lane_errors[session_id] = str(reason)
        self._session_incarnations[session_id] = (
            self._session_incarnations.get(session_id, 0) + 1
        )
        for rid, info in tuple(self._request_info.items()):
            if info[1] == session_id:
                self._pending.discard(rid)
                self._replies.pop(rid, None)
                self._tombstone_locked(rid)
        self._cv.notify_all()

    def fence_session(self, session_id, reason="STOP"):
        """Local, non-I/O fence for one session; siblings keep their replies/events."""
        if not isinstance(session_id, str) or not session_id:
            raise CdpError("session id required for a scoped fence")
        # Serialize with a command writer: no session write may begin after
        # this per-session STOP has returned. No remote cleanup is sent.
        with self._lock:
            with self._cv:
                self._fence_lane_locked(session_id, reason)

    def _store_reply_locked(self, msg):
        self._state_locked()
        rid = msg.get("id")
        if type(rid) is not int or rid < 0:
            return
        self._expire_tombstones_locked()
        if rid in self._tombstones:
            return
        pending = self._pending
        if rid in pending:
            meta = self._request_info.get(rid)
            if meta is not None:
                connection, sid, incarnation, deadline_ns = meta
                if (connection != self._connection_incarnation
                        or incarnation != self._session_incarnations.get(sid, 0)
                        or sid in self._lane_errors
                        or self._now_ns() > deadline_ns
                        or ("sessionId" in msg and msg["sessionId"] != sid)):
                    self._tombstone_locked(rid)
                    return
            if rid in self._replies:
                return
        elif rid <= self._next_id:
            # Already issued, consumed or timed out on this connection.
            return
        else:
            # Diagnostic compatibility for unsolicited, never-issued IDs; bounded.
            if rid in self._replies:
                del self._replies[rid]
        while len(self._replies) >= EVENT_BUFFER:
            stale = next((k for k in self._replies if k not in pending), None)
            if stale is None:
                return  # All remaining slots belong to live waiters.
            del self._replies[stale]
        self._replies[rid] = msg

    def _store_event_locked(self, msg):
        self._state_locked()
        sid = msg.get("sessionId")
        params = msg.get("params") or {}
        method = msg.get("method")
        if method in ("Target.detachedFromTarget", "Inspector.detached"):
            affected = params.get("sessionId") or sid
            if affected:
                self._fence_lane_locked(affected, method)
        elif method in ("Target.targetCrashed", "Inspector.targetCrashed"):
            targets = self._target_sessions.get(params.get("targetId"), ())
            for affected in tuple(targets):
                self._fence_lane_locked(affected, "target crash")
            if sid:
                self._fence_lane_locked(sid, "target crash")
        if sid in self._lane_errors:
            return
        lane = self._events_by_session.setdefault(sid, deque())
        if len(lane) >= EVENT_BUFFER:
            self._fence_lane_locked(sid, "session event lane overflow")
            return
        if len(self.events) >= EVENT_BUFFER:
            # At the connection mirror bound, fence the lane retaining the
            # greatest number of unread events, not an innocent new sibling.
            victim = max(self._events_by_session, key=lambda k: len(self._events_by_session[k]))
            self._fence_lane_locked(victim, "session event lane overflow")
            self._events_by_session.pop(victim, None)
            self.events = deque(
                (ev for ev in self.events if ev.get("sessionId") != victim),
                maxlen=EVENT_BUFFER,
            )
            if sid == victim:
                return
            lane = self._events_by_session.setdefault(sid, deque())
        lane.append(msg)
        self.events.append(msg)

    def _remove_mirrored_event_locked(self, event):
        for idx, candidate in enumerate(self.events):
            if candidate is event:
                del self.events[idx]
                return

    def _pop_event_locked(self, method, session_id):
        self._state_locked()
        if session_id is not None:
            lane = self._events_by_session.get(session_id, ())
            for idx, event in enumerate(lane):
                if event.get("method") == method:
                    del lane[idx]
                    self._remove_mirrored_event_locked(event)
                    return event
        # Deterministic legacy any-session scan and old test-injected self.events.
        for idx, event in enumerate(self.events):
            if (event.get("method") == method
                    and event.get("sessionId") not in self._lane_errors
                    and (session_id is None or event.get("sessionId") == session_id)):
                del self.events[idx]
                lane = self._events_by_session.get(event.get("sessionId"), ())
                for j, candidate in enumerate(lane):
                    if candidate is event:
                        del lane[j]
                        break
                return event
        return None

    def _retire_session_locked(self, session_id):
        self._fence_lane_locked(session_id, "session detached")
        self._events_by_session.pop(session_id, None)
        self.events = deque(
            (ev for ev in self.events if ev.get("sessionId") != session_id),
            maxlen=EVENT_BUFFER,
        )

    # -- lifecycle -------------------------------------------------------
    def start(self):
        if (
            (self.proc is not None and not getattr(self, "_proc_reaped", False) and self.proc.poll() is None)
            or self._w is not None
            or self._r is not None
            or (self._reader is not None and self._reader.is_alive())
        ):
            raise CdpError("CdpPipe is already running")

        user_data_dir = None
        owned_fds = []
        proc = None
        try:
            # Short dir under /tmp: Chromium creates unix sockets there (108-byte path limit).
            user_data_dir = tempfile.mkdtemp(prefix="wa", dir="/tmp")
            self.user_data_dir = user_data_dir
            cmd_r, cmd_w = os.pipe()     # we write commands -> chrome fd 3
            owned_fds.extend([cmd_r, cmd_w])
            rep_r, rep_w = os.pipe()     # chrome fd 4 -> we read replies
            owned_fds.extend([rep_r, rep_w])

            def _child_fds():
                a = fcntl.fcntl(cmd_r, fcntl.F_DUPFD, 10)
                b = fcntl.fcntl(rep_w, fcntl.F_DUPFD, 10)
                os.dup2(a, 3)
                os.dup2(b, 4)

            proc = subprocess.Popen(
                [self.chrome_bin] + chrome_args(user_data_dir, self.headless),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=False, preexec_fn=_child_fds)
            self.proc = proc
            self._proc_reaped = False

            os.close(cmd_r)
            owned_fds.remove(cmd_r)
            os.close(rep_w)
            owned_fds.remove(rep_w)

            self._w = cmd_w
            self._r = rep_r
            with self._cv:
                self._state_locked()
                self._connection_incarnation += 1
                self._closed = False
                self._pending.clear()
                self._request_info.clear()
                self._replies.clear()
                self._events_by_session.clear()
                self._lane_errors.clear()
                self._session_incarnations.clear()
                self._target_sessions.clear()
                self.events.clear()
            reader = threading.Thread(target=self._read_loop, name="cdp-pipe-reader", daemon=True)
            reader.start()
            self._reader = reader
            return self
        except Exception:
            self._w = None
            self._r = None
            self._reader = None
            seen_fds = set()
            for fd in list(owned_fds):
                owned_fds.remove(fd)
                if fd is not None and fd not in seen_fds:
                    seen_fds.add(fd)
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if proc is not None:
                self.proc = None
                self._proc_reaped = True
                try:
                    if proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            if user_data_dir is not None:
                self.user_data_dir = None
                _rmtree_profile(user_data_dir)
            raise

    def _read_loop(self):
        d = Deframer()
        try:
            while True:
                r_fd = self._r
                if r_fd is None:
                    break
                data = os.read(r_fd, 65536)
                if not data:
                    break
                for msg in d.feed(data):
                    if not isinstance(msg, dict):
                        continue
                    with self._cv:
                        if "id" in msg:
                            self._store_reply_locked(msg)
                        else:
                            self._store_event_locked(msg)
                        self._cv.notify_all()
        except CdpError as exc:
            with self._cv:
                self._state_locked()
                fenced_sids = set(self._session_incarnations) | set(self._events_by_session)
                for info in self._request_info.values():
                    fenced_sids.add(info[1])
                fenced_sids.add(None)
                for sid in fenced_sids:
                    self._fence_lane_locked(sid, str(exc))
        except (OSError, ValueError, TypeError):
            pass
        finally:
            with self._cv:
                self._closed = True
                self._cv.notify_all()

    def close(self):
        try:
            proc = self.proc
            if proc is not None and not getattr(self, "_proc_reaped", False):
                self._proc_reaped = True
                alive = False
                try:
                    alive = proc.poll() is None
                except Exception:
                    alive = False
                if alive:
                    try:
                        self.send("Browser.close", timeout=5)
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()      # only the process this client started
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            pass
                    except Exception:
                        pass
        finally:
            w_fd, r_fd = self._w, self._r
            self._w = None
            self._r = None
            self._reader = None
            closed_fds = set()
            for fd in (w_fd, r_fd):
                if fd is not None and fd not in closed_fds:
                    closed_fds.add(fd)
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            udd = self.user_data_dir
            self.user_data_dir = None
            if udd:
                _rmtree_profile(udd)
            with self._cv:
                self._closed = True
                self._cv.notify_all()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False

    # -- protocol --------------------------------------------------------
    # @anchor:wa.perceive.cdp_pipe.CdpPipe.send
    def send(self, method, params=None, session_id=None, timeout=None):
        if not is_readonly_cdp_method(method):
            assert_actuation_allowed("CdpPipe.send:%s" % (method,))
        eff_timeout = self.timeout if timeout is None else timeout
        deadline_ns = self._deadline_ns(eff_timeout)
        mid = None
        # The scheduler/fence lock precedes the connection write lock. No
        # registry or metadata lock is held during transport I/O.
        with self._writer_guard():
            with self._lock:
                with self._cv:
                    self._state_locked()
                    if self._closed:
                        raise CdpError("CDP connection is closed")
                    if session_id in self._lane_errors:
                        raise CdpError("session lane fenced: %s" % self._lane_errors[session_id])
                    pending = self._pending_set()
                    if len(pending) >= EVENT_BUFFER:
                        raise CdpError("active command capacity exhausted")
                    self._next_id += 1
                    mid = self._next_id
                    self._replies.pop(mid, None)
                    pending.add(mid)
                    self._request_info[mid] = (
                        self._connection_incarnation, session_id,
                        self._session_incarnations.get(session_id, 0), deadline_ns,
                    )
                msg = {"id": mid, "method": method, "params": params or {}}
                if session_id:
                    msg["sessionId"] = session_id
                try:
                    self._write_command(msg)
                except Exception as exc:
                    with self._cv:
                        pending.discard(mid)
                        self._request_info.pop(mid, None)
                        self._replies.pop(mid, None)
                        self._tombstone_locked(mid)
                        self._cv.notify_all()
                    raise CdpError("CDP write failed: %s" % exc) from exc
        completed = False
        with self._cv:
            try:
                while mid not in self._replies:
                    if session_id in self._lane_errors:
                        raise CdpError("session lane fenced: %s" % self._lane_errors[session_id])
                    if self._closed:
                        raise CdpError("browser closed transport during %s" % method)
                    left_ns = deadline_ns - self._now_ns()
                    if left_ns <= 0:
                        raise CdpError("timeout waiting for %s" % method)
                    self._cv.wait(left_ns / 1_000_000_000)
                reply = self._replies.pop(mid)
                completed = True
            finally:
                self._pending_set().discard(mid)
                self._request_info.pop(mid, None)
                self._replies.pop(mid, None)
                if not completed:
                    self._tombstone_locked(mid)
        if "error" in reply:
            raise CdpError("%s: %s" % (method, (reply["error"] or {}).get("message")))
        result = reply.get("result", {})
        if method == "Target.attachToTarget" and isinstance(result, dict):
            sid = result.get("sessionId")
            target_id = (params or {}).get("targetId")
            if isinstance(sid, str) and sid:
                with self._cv:
                    self._state_locked()
                    self._session_incarnations[sid] = self._session_incarnations.get(sid, 0) + 1
                    self._lane_errors.pop(sid, None)
                    self._events_by_session.pop(sid, None)
                    if isinstance(target_id, str) and target_id:
                        self._target_sessions.setdefault(target_id, set()).add(sid)
        if method == "Target.detachFromTarget" and isinstance(params, dict):
            sid = params.get("sessionId")
            if isinstance(sid, str) and sid:
                with self._cv:
                    self._retire_session_locked(sid)
                    for sessions in self._target_sessions.values():
                        sessions.discard(sid)
        return result

    # @anchor:wa.perceive.cdp_pipe.CdpPipe.wait_event
    def wait_event(self, method, session_id=None, timeout=None):
        eff_timeout = self.timeout if timeout is None else timeout
        deadline_ns = self._deadline_ns(eff_timeout)
        with self._cv:
            while True:
                self._state_locked()
                if session_id is not None and session_id in self._lane_errors:
                    raise CdpError("session lane fenced: %s" % self._lane_errors[session_id])
                event = self._pop_event_locked(method, session_id)
                if event is not None:
                    return event
                if self._closed:
                    raise CdpError("browser closed transport waiting for %s" % method)
                left_ns = deadline_ns - self._now_ns()
                if left_ns <= 0:
                    raise CdpError("timeout waiting for event %s" % method)
                self._cv.wait(left_ns / 1_000_000_000)

    # -- helpers ---------------------------------------------------------
    # @anchor:wa.perceive.cdp_pipe.CdpPipe.attach_page
    def attach_page(self):
        """Attach (flatten) to the first page target; return its sessionId."""
        targets = self.send("Target.getTargets").get("targetInfos", [])
        pages = [t for t in targets if t.get("type") == "page"]
        if pages:
            tid = pages[0]["targetId"]
        else:
            tid = self.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        sid = self.send("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        self.send("Page.enable", session_id=sid)
        return sid

    # @anchor:wa.perceive.cdp_pipe.CdpPipe.navigate
    def navigate(self, session_id, url, timeout=None):
        if not isinstance(session_id, str) or not session_id:
            raise CdpError("navigate requires a non-empty session_id")
        deadline_ns = self._deadline_ns(self.timeout if timeout is None else timeout)
        with self._cv:
            self._state_locked()
            if session_id in self._lane_errors:
                raise CdpError("session lane fenced: %s" % self._lane_errors[session_id])
            # Stale loads are purged only for this session, preserving sibling
            # events in both the live lane and legacy any-session mirror.
            self.events = deque(
                (ev for ev in self.events
                 if not (ev.get("method") == "Page.loadEventFired"
                         and ev.get("sessionId") == session_id)),
                maxlen=EVENT_BUFFER,
            )
            lane = self._events_by_session.get(session_id)
            if lane is not None:
                self._events_by_session[session_id] = deque(
                    ev for ev in lane if ev.get("method") != "Page.loadEventFired"
                )
        remaining = max(0.0, (deadline_ns - self._now_ns()) / 1_000_000_000)
        res = self.send("Page.navigate", {"url": url}, session_id=session_id,
                        timeout=remaining)
        if not isinstance(res, dict):
            raise CdpError("navigate returned invalid response")
        if res.get("errorText"):
            raise CdpError("navigate failed: %s" % res["errorText"])
        expected_loader = res.get("loaderId")
        while True:
            remaining = max(0.0, (deadline_ns - self._now_ns()) / 1_000_000_000)
            ev = self.wait_event("Page.loadEventFired", session_id=session_id,
                                 timeout=remaining)
            ev_loader = ev.get("loaderId", (ev.get("params") or {}).get("loaderId"))
            if expected_loader is not None and ev_loader is not None and ev_loader != expected_loader:
                continue
            break
        return res

    # @anchor:wa.perceive.cdp_pipe.CdpPipe.evaluate
    def evaluate(self, session_id, expression, timeout=None):
        res = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True},
                        session_id=session_id, timeout=timeout)
        if res.get("exceptionDetails"):
            raise CdpError("evaluate raised: %s" % res["exceptionDetails"].get("text"))
        return res.get("result", {}).get("value")
