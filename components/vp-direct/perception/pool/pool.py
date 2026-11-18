"""Pool-local leases, frame history, and the result publication fence."""

from dataclasses import dataclass
from hashlib import sha256
import math
from threading import Lock

from schemas.validation import validate_contract

from .transport import DescribeResult, Frame, VisionWorker, VServeClient, VServeError


class PoolError(RuntimeError):
    """A pool admission, lifetime, or result check failed."""


@dataclass(frozen=True)
class TransportDiagnostics:
    last_transport: str | None = None
    last_vserve_error: str | None = None
    last_fallback_reason: str | None = None


@dataclass(frozen=True)
class PoolResult:
    description: DescribeResult
    diagnostics: TransportDiagnostics


@dataclass(frozen=True)
class PoolUsage:
    active_sessions: int
    retiring_sessions: int
    inflight: int
    partitions: int
    reserved_bytes: int


@dataclass
class _Partition:
    worker: VisionWorker
    users: int = 0
    closing: bool = False


class PoolSession:
    """A fresh local handle for one lifetime of a bound session."""

    def __init__(self, pool, slot, session_id, binding, partition_key, partition):
        self._pool = pool
        self._slot = slot
        self._session_id = session_id
        self._binding = binding
        self._partition_key = partition_key
        self._partition = partition
        self._previous = None
        self._history_scope = None
        self._inflight_bytes = 0
        self._diagnostics = TransportDiagnostics()

    @property
    def diagnostics(self):
        with self._pool._lock:
            return self._diagnostics

    def describe(self, frame, *, masks=()):
        """Describe an RGB24 Frame from a trusted in-process caller."""
        if not isinstance(frame, Frame):
            raise ValueError("an in-memory RGB24 Frame is required")
        return self._pool._describe(self, lambda: frame, frame.width, frame.height, masks)

    def describe_observation(self, stream, observation, *, width, height, masks=()):
        """Read a bound P3 observation after admission and recheck it at publication."""
        token = observation.token
        for field in ("endpoint", "surface_id", "incarnation", "owner", "authority_epoch"):
            if getattr(token, field) != self._binding[field]:
                raise PoolError("observation does not belong to the session binding")

        def load_frame():
            return Frame(width, height, stream.read(token, observation.observation_id))

        def is_current():
            status = stream.status()
            return (status["state"] == "ACTIVE"
                    and status["stream_generation"] == token.generation
                    and status["fresh_observation_id"] == observation.observation_id)

        return self._pool._describe(
            self, load_frame, width, height, masks,
            scope=(token.generation, token.nonce), is_current=is_current,
            expected_digest=observation.frame_sha256,
        )

    def close(self):
        """Fence publication immediately. A pending call keeps its reservation."""
        with self._pool._lock:
            self._pool._retire(self)


class PerceptionPool:
    """Share workers only within one owner and trust domain.

    The byte limit covers one history reservation per session and the input of
    each admitted call. It is a frame budget, independent of backend RSS.
    Callers supply already-authorized surface bindings and trust domains.
    """

    def __init__(self, ve_root, *, max_sessions, max_frame_bytes, max_reserved_bytes,
                 transport, timeout=2.0, allow_fallback=False, worker_factory=VisionWorker):
        if type(max_sessions) is not int or not 1 <= max_sessions <= 16:
            raise ValueError("max_sessions must be within the VServe session ceiling")
        if type(max_frame_bytes) is not int or not 0 < max_frame_bytes <= VServeClient.FRAME_CAP:
            raise ValueError("max_frame_bytes must be within the VServe frame ceiling")
        if type(max_reserved_bytes) is not int or max_reserved_bytes < max_frame_bytes:
            raise ValueError("max_reserved_bytes must cover one history reservation")
        if (transport not in ("vserve", "c2") or type(allow_fallback) is not bool
                or (allow_fallback and transport != "vserve")):
            raise ValueError("explicit transport and compatible fallback policy required")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self._max_frame_bytes = max_frame_bytes
        self._max_reserved_bytes = max_reserved_bytes
        self._transport = transport
        self._allow_fallback = allow_fallback
        self._new_worker = lambda: worker_factory(
            ve_root, timeout=timeout, transport=transport, allow_fallback=allow_fallback,
        )
        self._lock = Lock()
        self._slots = [None] * max_sessions
        self._partitions = {}
        self._retiring = set()
        self._closed = False
        self._inflight = 0
        self._reserved_bytes = 0

    def status(self):
        with self._lock:
            return PoolUsage(sum(client is not None for client in self._slots),
                             len(self._retiring), self._inflight, len(self._partitions),
                             self._reserved_bytes)

    def open_session(self, session_id, *, binding, trust_domain):
        """Issue a fresh handle. Names are unique within an active partition."""
        binding = dict(binding)
        validate_contract("vp.surface_binding/v1", binding)
        if any(type(name) is not str or not name.strip() for name in (session_id, trust_domain)):
            raise ValueError("nonempty session_id and trust_domain required")
        key = binding["owner"], trust_domain
        with self._lock:
            if self._closed:
                raise PoolError("pool is closed")
            if any(client is not None and client._partition_key == key
                   and client._session_id == session_id for client in self._slots):
                raise PoolError("session already active in this partition")
            if None not in self._slots:
                raise PoolError("session capacity exhausted")
            self._check_budget(self._max_frame_bytes)
            partition = self._partitions.get(key)
            if partition is None:
                partition = self._partitions[key] = _Partition(self._new_worker())
            if partition.closing:
                raise PoolError("partition shutdown has not settled")
            slot = self._slots.index(None)
            session = PoolSession(self, slot, session_id, binding, key, partition)
            self._slots[slot] = session
            partition.users += 1
            self._reserved_bytes += self._max_frame_bytes
            return session

    def close(self):
        """Close admission and fence all clients, including those still draining."""
        with self._lock:
            self._closed = True
            error = None
            clients = (*self._retiring, *(client for client in self._slots if client is not None))
            for client in clients:
                try:
                    self._retire(client)
                except Exception as exc:
                    error = exc
            if error is not None:
                raise PoolError("worker shutdown has not settled") from error

    def _check_budget(self, size):
        if self._reserved_bytes + size > self._max_reserved_bytes:
            raise PoolError("frame budget exhausted")

    def _require_live(self, client):
        if self._slots[client._slot] is not client:
            raise PoolError("session is retired")

    def _retire(self, client):
        if self._slots[client._slot] is client:
            self._slots[client._slot] = None
            client._previous = client._history_scope = None
            self._retiring.add(client)
        if client in self._retiring and not client._inflight_bytes:
            self._settle(client)

    def _settle(self, client):
        partition = client._partition
        if partition.users == 1:
            partition.closing = True
            partition.worker.close()
            del self._partitions[client._partition_key]
        partition.users -= 1
        self._retiring.remove(client)
        self._reserved_bytes -= self._max_frame_bytes

    def _describe(self, client, load_frame, width, height, masks, *, scope=None,
                  is_current=None, expected_digest=None):
        if any(type(dim) is not int or not 0 < dim <= 8192 for dim in (width, height)):
            raise ValueError("invalid RGB24 dimensions")
        size = width * height * 3
        with self._lock:
            self._require_live(client)
            if size > self._max_frame_bytes:
                raise PoolError("frame exceeds session budget")
            if client._inflight_bytes or self._inflight == len(self._slots):
                raise PoolError("call capacity exhausted")
            self._check_budget(size)
            self._inflight += 1
            self._reserved_bytes += size
            client._inflight_bytes = size
            previous = client._previous
            if previous is not None and (client._history_scope != scope
                                         or (previous.width, previous.height) != (width, height)):
                previous = None

        diagnostics = TransportDiagnostics()
        try:
            frame = load_frame()
            Frame.__post_init__(frame)
            digest = sha256(frame.data).hexdigest()
            if expected_digest is not None and digest != expected_digest:
                raise PoolError("observation digest mismatch")
            diagnostics = TransportDiagnostics(last_transport=self._transport)
            worker = client._partition.worker
            try:
                description = worker.describe(self._transport, frame, previous, masks)
            except VServeError as exc:
                reason = str(exc)
                diagnostics = TransportDiagnostics(self._transport, reason)
                if self._transport != "vserve" or not self._allow_fallback or not exc.unavailable:
                    raise
                with self._lock:
                    self._require_live(client)
                diagnostics = TransportDiagnostics("c2", reason, reason)
                description = worker.describe("c2", frame, previous, masks)
            if (not isinstance(description, DescribeResult) or description.schema != "ve.cu_describe/v1"
                    or description.frame_sha256 != digest
                    or (description.width, description.height) != (width, height)
                    or (description.changed is None) != (previous is None)):
                raise PoolError("description does not match the supplied frame or history")
            with self._lock:
                self._require_live(client)
                if is_current is not None and not is_current():
                    raise PoolError("observation retired before publication")
                client._previous, client._history_scope = frame, scope
                client._diagnostics = diagnostics
                return PoolResult(description, diagnostics)
        except Exception:
            with self._lock:
                self._require_live(client)
                client._diagnostics = diagnostics
            raise
        finally:
            # Drop references to consumed frames before returning their reservation.
            previous = frame = load_frame = None
            with self._lock:
                client._inflight_bytes = 0
                self._inflight -= 1
                self._reserved_bytes -= size
                if client in self._retiring:
                    self._settle(client)
