"""Process-local demand owner. Only the trusted service loop may claim work.

One instance covers one producer on one surface. The owner explicitly declares
which interests that producer can serve. Supply a lease admission check and a
synchronous drain callback that returns True after the producer acknowledges stop
(or raises on uncertainty). Draining a P3 stream can use its retained pause.
This coordinator never issues stream tokens, input authority or goal credit.
"""
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, RLock, TIMEOUT_MAX

from schemas.validation import validate_contract


class DemandError(RuntimeError):
    """The requested change to a demand, delivery or producer was refused."""


@dataclass(frozen=True, eq=False)
class Work:
    """One reserved credit and its reference. Identity fences stale completions."""

    surface_id: str
    demand_id: str
    consumer: str
    interest: str
    purpose: str
    lease: str
    sequence: int
    reference: str


@dataclass
class _Subscription:
    record: dict
    handle: object
    active: bool = True
    sequence: int = 0
    outstanding: int = 0


class DemandCoordinator:
    def __init__(self, surface_id, *, interests, authorize, drain):
        self._interests = frozenset(interests)
        if not self._interests or not self._interests <= {"DISPLAY", "VERIFIED"}:
            raise ValueError("invalid producer interests")
        if not isinstance(surface_id, str) or not surface_id:
            raise ValueError("surface identity required")
        if not callable(authorize) or not callable(drain):
            raise ValueError("lease admission and producer drain callbacks required")
        self._surface_id = surface_id
        self._authorize = authorize
        self._drain = drain
        self._lock = RLock()
        self._ready = {interest: Condition(self._lock) for interest in self._interests}
        self._subscriptions = {}
        self._by_handle = {}
        self._queues = {interest: deque() for interest in self._interests}
        self._claimed = set()
        self._borrows = {}
        self._state = "idle"

    @contextmanager
    def register(self, demand):
        """Recorder-compatible subscription. Exiting fences new publications.

        Previously admitted work remains claimable until delivered. The opaque
        handle identifies this particular registration even if its ID is reused.
        The owner supplies an admission check for the exact lease and consumer.
        """
        if not isinstance(demand, dict):
            raise DemandError("a demand record is required")
        record = demand.copy()
        validate_contract("vp.demand/v1", record)
        if record["surface_id"] != self._surface_id or record["interest"] not in self._interests:
            raise DemandError("incompatible producer")
        if self._authorize(record.copy()) is not True:
            raise DemandError("NO_LEASE")
        sub = _Subscription(record, object())
        with self._lock:
            if self._state in ("draining", "drain_failed"):
                raise DemandError("producer drain unsettled")
            if record["demand_id"] in self._subscriptions:
                raise DemandError("demand already registered")
            self._subscriptions[record["demand_id"]] = sub
            self._by_handle[sub.handle] = sub
            self._state = "in_use"
        try:
            yield sub.handle
        finally:
            with self._lock:
                sub.active = False
                del self._by_handle[sub.handle]
                drain = self._retire_if_settled(sub)
            if drain:
                self._finish_drain()

    def publish(self, handle, sequence, reference):
        """Reserve a credit and publish a work reference before waking readers.

        Only claim() hands out the delivery token. Sequences start at one per
        registration. A retry cannot reserve twice.
        A refused publication consumes neither sequence nor credit.
        """
        if type(sequence) is not int or not isinstance(reference, str) or not reference:
            raise DemandError("invalid work")
        with self._lock:
            sub = self._by_handle.get(handle)
            if sub is None:
                raise DemandError("NO_LEASE")
            if sequence != sub.sequence + 1:
                raise DemandError("unexpected work sequence")
            if sub.outstanding >= sub.record["credits"]:
                raise DemandError("credits exhausted")
            interest = sub.record["interest"]
            work = Work(self._surface_id, sub.record["demand_id"], sub.record["consumer"],
                        interest, sub.record["purpose"], sub.record["lease"], sequence, reference)
            sub.outstanding += 1
            sub.sequence = sequence
            self._queues[interest].append(work)
            self._ready[interest].notify_all()

    def claim(self, interest, *, budget, timeout):
        """Return up to budget admitted items. Zero timeout is a nonblocking poll.

        The service loop calls again after spending its budget. Checking the
        predicate under the publication lock closes the check-to-park race.
        The bounded wait rechecks the queue even if the last hint was lost.
        Only a VERIFIED claim can supply verified work for a lifecycle wake.
        """
        if interest not in self._interests or type(budget) is not int or budget <= 0:
            raise DemandError("invalid service budget or interest")
        if type(timeout) not in (int, float) or not 0 <= timeout <= TIMEOUT_MAX:
            raise DemandError("finite nonnegative timeout required")
        with self._lock:
            queue = self._queues[interest]
            self._ready[interest].wait_for(lambda: bool(queue), timeout)
            claimed = tuple(queue.popleft() for _ in range(min(budget, len(queue))))
            self._claimed.update(claimed)
            return claimed

    @contextmanager
    def borrow(self, work, size_bytes):
        """Pin a claimed delivery's byte span through the borrowing scope.

        This is a per-delivery pin, not an additional physical allocation. The
        producer's allocation remains charged by its own resource accountant.
        """
        if type(size_bytes) is not int or size_bytes <= 0:
            raise DemandError("invalid borrow size")
        with self._lock:
            if work not in self._claimed or work in self._borrows:
                raise DemandError("delivery unavailable for borrow")
            self._borrows[work] = size_bytes
        try:
            yield
        finally:
            with self._lock:
                del self._borrows[work]

    def complete(self, work):
        """Acknowledge finished delivery and return its reserved credit."""
        with self._lock:
            if work not in self._claimed or work in self._borrows:
                raise DemandError("delivery unclaimed, borrowed or already settled")
            self._claimed.remove(work)
            sub = self._subscriptions[work.demand_id]
            sub.outstanding -= 1
            drain = self._retire_if_settled(sub)
        if drain:
            self._finish_drain()

    def _retire_if_settled(self, sub):
        if sub.active or sub.outstanding:
            return False
        del self._subscriptions[sub.record["demand_id"]]
        if self._subscriptions:
            return False
        self._state = "draining"
        return True

    def _finish_drain(self):
        # Do not hold the coordinator lock across an external stop/ack.
        try:
            if self._drain() is not True:
                raise DemandError("producer drain unacknowledged")
        except BaseException:
            with self._lock:
                self._state = "drain_failed"
            raise
        with self._lock:
            self._state = "idle"

    def release(self):
        """Verify settlement, or retry an unacknowledged producer drain."""
        with self._lock:
            if self._subscriptions:
                raise DemandError("producer has outstanding obligations")
            if self._state == "idle":
                return
            if self._state != "drain_failed":
                raise DemandError("producer drain unsettled")
            self._state = "draining"
        self._finish_drain()

    def snapshot(self):
        """Trusted diagnostic view, taken at a single point in queue order."""
        with self._lock:
            demands = []
            available = dict.fromkeys(sorted(self._interests), 0)
            for demand_id in sorted(self._subscriptions):
                sub = self._subscriptions[demand_id]
                demands.append(dict(sub.record, active=sub.active, outstanding=sub.outstanding))
                if sub.active:
                    available[sub.record["interest"]] += sub.record["credits"] - sub.outstanding
            return dict(surface_id=self._surface_id, state=self._state, demands=demands,
                        available=available, queued={k: len(v) for k, v in self._queues.items()},
                        claimed=len(self._claimed), borrowed_bytes=sum(self._borrows.values()))
