"""Adapter for session-exclusive U1b/U1c executors and the provisioned perception owner."""
from time import monotonic_ns
from uuid import uuid4

from . import FreshObservation, require


def _request(op, session):
    target = {"window_id": session["window"]["xid"]}
    if session["backend"] == "cdp":
        target["cdp_target_id"] = session["cdp_target_id"]
    return {"schema": "vp.channel.req/v1", "request_id": uuid4().hex, "op": op,
            "session_id": session["session_id"], "capsule_id": session["capsule_id"],
            "authority_epoch": session["lease"]["authority_epoch"], "incarnation": session["incarnation"],
            "backend": session["backend"], "dry_run": False, "risk_class": "leer",
            "require_vision_witness": False, "target": target}


class SessionRuntime:
    """Own every channel handle for this session, including auxiliary AT-SPI.

    open_executor(mode, session) provisions the specified mode and bound handle.
    Its CDP session reader may use the candidate for reconnect/observe only.
    All mutating calls still require U1a's normal broker lock and effect guard.
    retain and generation are supplied by the owner of the agent subscription.
    """

    def __init__(self, session_id, *, executors, open_executor, qualify, retain, reconcile,
                 observation_id, generation, attachment_readback, clock=monotonic_ns):
        require(set(executors) <= {"atspi", "cdp"}, "HOLD")
        self.session_id = session_id
        self._executors = dict(executors)
        self._open = open_executor
        self.reauthorize, self.retain, self.reconcile = qualify, retain, reconcile
        self._readback = attachment_readback
        self._last = observation_id
        self._generation = generation
        self._clock = clock

    def last_observation_id(self):
        return self._last()

    def attachments(self):
        actual = self._readback()
        require(type(actual) is tuple and set(actual) <= {"cdp", "atspi"}, "HOLD")
        return tuple(sorted(actual))

    def detach(self):
        errors = []
        closed = []
        for kind, executor in tuple(self._executors.items()):
            try:
                if kind == "cdp":
                    executor.detach()
                else:
                    try:
                        executor.detach(self.session_id)
                    finally:
                        executor.close()
            except Exception as exc:
                errors.append(exc)
            else:
                closed.append(kind)
        attached = self.attachments()
        for kind in closed:
            if kind not in attached:
                del self._executors[kind]
        if errors:
            raise errors[0]
        require(not attached, "HOLD")

    def reconnect(self, mode, session):
        kind = session["backend"]
        require(not self._executors and kind in ("cdp", "atspi")
                and session["session_id"] == self.session_id, "NO_LEASE")
        executor = self._open(mode, session)
        require(executor is not None, "HOLD")
        self._executors[kind] = executor
        if kind == "cdp":
            executor.attach_target(_request("attach", session))

    def observe(self, session, binding):
        kind = session["backend"]
        request = _request("observe", session)
        started = self._clock()
        if kind == "cdp":
            observed = self._executors[kind].observe(request)
            require(observed and observed.target_id == session["cdp_target_id"], "identity_mismatch")
            identifier = observed.id
        else:
            reply, tree = self._executors[kind].observe(request, session=session)
            require(reply.get("ok") is True and tree and tree["atspi_capture"] == "polling_only", "HOLD")
            identifier = tree["observation_id"]
        return FreshObservation(session["session_id"], binding["surface_id"], binding["incarnation"],
                                binding["authority_epoch"], identifier, started, self._generation())


def retain_agent_interest(session, binding, *, demand, stream, withdraw, drain):
    """S4 retain port for a P2 subscription backed by a P3 RAM stream.

    withdraw closes this agent's subscription. P2 owns the last-consumer pause.
    drain waits for this agent's already admitted work. Other consumers can keep
    using the producer throughout the human interval.
    """
    before = stream.status()
    require(session["state"] == "HUMAN" and before["state"] in ("ACTIVE", "ASLEEP")
            and before["ring_id"] and before["reserved_bytes"] > 0, "HOLD")
    withdraw()
    require(drain() is True, "HOLD")
    snapshot = demand.snapshot()
    after = stream.status()
    require(snapshot["surface_id"] == binding["surface_id"]
            and snapshot["state"] in ("idle", "in_use")
            and all(row["consumer"] != binding["owner"] for row in snapshot["demands"])
            and after["ring_id"] == before["ring_id"]
            and after["reserved_bytes"] == before["reserved_bytes"]
            and after["stream_generation"] == before["stream_generation"], "HOLD")
    if snapshot["demands"]:
        require(after["state"] == "ACTIVE", "HOLD")
    else:
        require(after["state"] == "ASLEEP" and after["fresh_observation_id"] is None, "HOLD")
    return dict(schema="vp.perception_state/v1", state="ASLEEP", session_id=session["session_id"],
                surface_id=binding["surface_id"], incarnation=binding["incarnation"],
                authority_epoch=binding["authority_epoch"], stream_generation=after["stream_generation"],
                obligations=[d["demand_id"] for d in snapshot["demands"]], since_mono_ns=monotonic_ns())
