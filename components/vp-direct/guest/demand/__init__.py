"""Managed guest demand and agent drain, for the trusted producer owner.

The P2 coordinator owns delivery, borrows and producer stop. The guest owner
supplies custody, agent authority, peer acknowledgements and activity evidence.
These Python types are in-process interfaces.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from time import monotonic_ns

from guest.custody import CustodyRefused
from perception.demand import DemandCoordinator, DemandError
from schemas.validation import validate_contract


MIN_REST_NS = 60_000_000_000
PARTICIPANTS = frozenset({"guest", "devices", "surface"})


@dataclass(frozen=True, eq=False)
class DrainTicket:
    """Unique within the owner process and bound to one custody lifetime."""

    reservation_id: str
    workload_id: str
    surface_id: str
    incarnation: str
    authority_epoch: int


@dataclass(frozen=True)
class ParticipantAck:
    """The named peer fenced agent work and settled its admitted requests."""

    ticket: DrainTicket
    participant: str


@dataclass(frozen=True)
class RestEvidence:
    """Provider evidence covering an uninterrupted interval of guest traffic."""

    ticket: DrainTicket
    from_mono_ns: int
    through_mono_ns: int
    agent_requests: int


@dataclass(frozen=True)
class ParticipantStates:
    guest: str
    devices: str
    surface: str


class GuestDemand(DemandCoordinator):
    """A per-guest P2 producer, with a separate fence for agent traffic.

    The owner binds authorize() to the exact P2 consumer and lease and supplies
    the lifecycle's agent consumer ID. Use agent_request() around synchronous
    agent work through its final side effect. Peers account for independently
    queued work before returning their acknowledgements. The owner cancels the
    agent subscription and settles admitted deliveries before acknowledging.
    """

    def __init__(self, guest, *, custody, reservation_id, interests, authorize,
                 agent_consumer, authorize_agent, stop_producer, read_states, clock=monotonic_ns):
        self._guest = deepcopy(guest)
        validate_contract("vp.guest/v1", self._guest)
        if self._guest["kind"] == "external_viewer":
            raise DemandError("OWNER")
        if not isinstance(agent_consumer, str) or not agent_consumer:
            raise ValueError("the authorized agent consumer ID is required")
        if not all(map(callable, (authorize_agent, stop_producer, read_states, clock))):
            raise ValueError("guest authority, producer stop, state provider and clock are required")
        self._custody, self._reservation_id = custody, reservation_id
        self._agent_consumer, self._authorize_agent = agent_consumer, authorize_agent
        self._read_states, self._clock = read_states, clock
        self._agent_lock = RLock()
        self._agent_state = "ACTIVE"
        self._agent_handles = set()
        self._inflight = 0
        self._ticket = self._ack_ns = None
        self._require_custody()

        def drain_producer():
            self._require_custody()
            return stop_producer()

        super().__init__(self._guest["surface_id"], interests=interests,
                         authorize=authorize, drain=drain_producer)

    def _require_custody(self):
        try:
            reservation = self._custody.status(self._reservation_id, deepcopy(self._guest))
        except CustodyRefused as exc:
            raise DemandError(exc.code) from exc
        if not reservation.held:
            raise DemandError("OWNER")

    def _require_agent(self):
        if self._agent_state != "ACTIVE" or self._authorize_agent() is not True:
            raise DemandError("NO_LEASE")

    @contextmanager
    def register(self, demand):
        if not isinstance(demand, dict):
            raise DemandError("a demand record is required")
        demand = demand.copy()
        agent = demand.get("consumer") == self._agent_consumer
        handle = None
        try:
            with ExitStack() as subscription:
                with self._agent_lock:
                    self._require_custody()
                    if agent:
                        self._require_agent()
                    handle = subscription.enter_context(super().register(demand))
                    if agent:
                        self._agent_handles.add(handle)
                yield handle
        finally:
            if agent and handle is not None:
                with self._agent_lock:
                    self._agent_handles.remove(handle)

    def publish(self, handle, sequence, reference):
        with self._agent_lock:
            self._require_custody()
            if handle in self._agent_handles:
                self._require_agent()
            super().publish(handle, sequence, reference)

    @contextmanager
    def agent_request(self):
        with self._agent_lock:
            self._require_agent()
            self._require_custody()
            self._inflight += 1
        try:
            yield
        finally:
            with self._agent_lock:
                self._inflight -= 1

    def status(self):
        """Read actual peer states independently of the agent drain state."""
        with self._agent_lock:
            self._require_custody()
            states = self._read_states()
            if not isinstance(states, ParticipantStates):
                raise DemandError("participant states unavailable")
            return dict(guest=states.guest, devices=states.devices, surface=states.surface,
                        agent=self._agent_state)

    def begin_drain(self):
        """Fence new agent work before asking the participants to drain."""
        with self._agent_lock:
            self._require_custody()
            if self._ticket is None:
                self._ticket = DrainTicket(self._reservation_id, self._guest["workload_id"],
                                           self._guest["surface_id"], self._guest["incarnation"],
                                           self._guest["authority_epoch"])
                self._agent_state = "DRAINING"
            return self._ticket

    def _require_ticket(self, ticket):
        self._require_custody()
        if self._ticket is None or ticket is not self._ticket:
            raise DemandError("stale or foreign drain ticket")

    def acknowledge(self, ticket, acks):
        """Set the rest window's origin after P2 and all peers agree.

        Retrying an acknowledged ticket returns the original timestamp.
        """
        with self._agent_lock:
            self._require_ticket(ticket)
            if self._ack_ns is not None:
                return self._ack_ns
            if not isinstance(acks, (list, tuple)):
                raise DemandError("participant drain unacknowledged")
            acks = tuple(acks)
            if (any(not isinstance(ack, ParticipantAck) or ack.ticket is not ticket
                    or not isinstance(ack.participant, str) for ack in acks)
                    or len(acks) != len(PARTICIPANTS)
                    or {ack.participant for ack in acks} != PARTICIPANTS):
                raise DemandError("participant drain unacknowledged")
            snapshot = self.snapshot()
            if (self._inflight or snapshot["state"] not in ("idle", "in_use")
                    or any(row["consumer"] == self._agent_consumer for row in snapshot["demands"])):
                raise DemandError("agent work unsettled")
            self._ack_ns = self._clock()
            self._agent_state = "DRAINED"
            return self._ack_ns

    def verify_rest(self, evidence):
        """Require coverage of the acknowledged window from the guest observer."""
        with self._agent_lock:
            if not isinstance(evidence, RestEvidence):
                raise DemandError("agent request evidence unavailable")
            self._require_ticket(evidence.ticket)
            if self._ack_ns is None:
                raise DemandError("drain unacknowledged")
            if (type(evidence.from_mono_ns) is not int or type(evidence.through_mono_ns) is not int
                    or evidence.from_mono_ns != self._ack_ns
                    or not self._ack_ns + MIN_REST_NS <= evidence.through_mono_ns <= self._clock()):
                raise DemandError("insufficient post-ack coverage")
            if type(evidence.agent_requests) is not int or evidence.agent_requests != 0:
                raise DemandError("agent request absence unproven")
            return evidence

    def resume_admission(self, ticket):
        """Retire the drain ticket under the lifecycle owner's authority."""
        with self._agent_lock:
            self._require_ticket(ticket)
            if self._ack_ns is None or self._authorize_agent() is not True:
                raise DemandError("NO_LEASE")
            self._ticket = self._ack_ns = None
            self._agent_state = "ACTIVE"
