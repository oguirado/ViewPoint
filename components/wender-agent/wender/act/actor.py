# @module {"id":"wa.act.actor","layer":1,"owner":"ACT","deps":[],"book":"HB-WA-s2-act"}
"""@module wa.act.actor - path selection and the single-actor rule.

Paths, most structured first: app API/MCP -> AX/DOM action -> coordinates.
A legacy ActorLock protects one caller-supplied surface; TargetLockRegistry
provides canonical, generation-fenced exclusion shared across action paths.
Readers do not take actor leases. The legacy execute stub is not a dispatcher.
"""
import threading

# @anchor:act.actor.order
ORDER = ("ax", "dom", "coordinates")
PATHS = ("api", "ax", "coordinates")  # order = preference


# @anchor:wa.act.actor.choose_path
def choose_path(available):
    """Return the most structured available path; refuse bare string iterables."""
    if isinstance(available, (str, bytes, bytearray)):
        raise TypeError("available paths must be an iterable of labels, not a bare string")
    av = set(available)  # also refuses None and unhashable members
    if any(not isinstance(p, (str, bytes)) for p in av):
        raise TypeError("available paths must contain string path labels")
    for path in PATHS:
        if path in av:
            return path
    return None


class ActorBusy(TypeError, RuntimeError):
    """Busy actor (also TypeError for a bare, unacquired context entry)."""


# @anchor:wa.act.actor.ActorLock
class ActorLock:
    """Legacy nonblocking one-surface lock; prefer TargetLockRegistry for routing.

    A separate RLock serializes the holder metadata around primitive operations.
    The primitive may be instrumented, including reentrant test hooks; holder
    remains published until primitive release really succeeds.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._metadata_lock = threading.RLock()
        self.holder = None
        self._context_ready = False
        self._entered_depth = 0

    def acquire(self, who):
        if not isinstance(who, str) or not who.strip():
            raise ValueError("actor identity must be a non-empty string")
        if not self._metadata_lock.acquire(blocking=False):
            raise ActorBusy("surface already driven by %r" % (self.holder,))
        try:
            if self.holder is not None:
                raise ActorBusy("surface already driven by %r" % (self.holder,))
            self.holder = who
            try:
                acquired = self._lock.acquire(blocking=False)
            except BaseException:
                self.holder = None
                raise
            if not acquired:
                self.holder = None
                raise ActorBusy("surface already driven by %r" % (self.holder,))
            self._context_ready = True
            return self
        finally:
            self._metadata_lock.release()

    def release(self, who=None):
        with self._metadata_lock:
            if self.holder is None:
                raise RuntimeError("release unlocked lock")
            if who is not None:
                if not isinstance(who, str) or not who.strip():
                    raise ValueError("actor identity must be a non-empty string")
                if who != self.holder:
                    raise RuntimeError("cannot release lock held by %r as %r" % (self.holder, who))
            self._lock.release()  # failure leaves metadata and exclusion unchanged
            self.holder = None
            self._context_ready = False
            self._entered_depth = 0

    def __enter__(self):
        with self._metadata_lock:
            if self.holder is None or not self._context_ready or self._entered_depth:
                raise ActorBusy("bare context entry refused; call acquire(who) first")
            self._context_ready = False
            self._entered_depth = 1
            return self

    def __exit__(self, *exc):
        if self._entered_depth:
            self.release(self.holder)
        return False


# @anchor:wa.act.actor.execute
def execute(path, action):
    raise NotImplementedError("executor for path %r is a v0 stub" % (path,))
