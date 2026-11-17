"""Trusted, single-writer authority for a broker-owned runtime directory.

The session authority registers witnessed endpoint aliases. An agent receives
only a provisioned socket (see port.py). The executor durably marks dispatch
BEFORE a possible send and supplies its verified outcome or UNKNOWN. Only the
trusted reconciler may clear uncertainty using correlated observation evidence.
"""

from collections import defaultdict
from copy import deepcopy
from functools import wraps
import os
import re
import threading
import time
import uuid

from schemas.validation import validate_contract
from vendor.broker.broker import MAX_DROPS, QUEUE_MAX, EVENT_ID_RE
from .journal import BrokerError, Journal


def serialized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if os.getpid() != self._journal.pid:
            raise BrokerError("NO_LEASE")
        with self._mutex:
            if self._journal.file.closed:
                raise BrokerError("NO_LEASE")
            return method(self, *args, **kwargs)
    return guarded


def identifier(value):
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise BrokerError("BAD_INTENT")
    return value


def check_epoch(actual, expected):
    if type(actual) is not int or actual != expected:
        raise BrokerError("NO_LEASE")


class Broker:
    def __init__(self, rt):
        self._mutex = threading.RLock()
        self._bindings = {}
        self._aliases = {}
        self._sessions = {}
        self._locks = {}
        self._perception = {}
        self._events = {}
        self._attempted = set()
        self._tasks = {}
        self._queues = defaultdict(list)
        self._ports = []
        self._journal = Journal(rt)
        try:
            for row in self._journal.replay():
                self._apply(row)
            # A lost executor connection is never evidence of no effect.
            for lock in list(self._locks.values()):
                if lock["status"] != "UNKNOWN":
                    self.quarantine(lock["agent_id"], lock["lock_id"])
            for session_id in self._sessions:
                self._close_stale_queue(session_id)
        except BaseException:
            self._journal.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        from socket import SHUT_RDWR
        if os.getpid() != self._journal.pid:
            raise BrokerError("NO_LEASE")
        with self._mutex:
            for port in self._ports:
                try:
                    port.shutdown(SHUT_RDWR)
                except OSError:
                    pass
            self._ports.clear()
            self._journal.close()

    def _write(self, rec, **fields):
        # The stored projection and returned receipt must not share mutable state.
        row = self._journal.append(deepcopy(dict(fields, rec=rec, ts_ms=time.time_ns() // 10**6)))
        try:
            self._apply(row)
        except BaseException:
            self._journal.file.close()
            raise
        return deepcopy(row)

    def _apply(self, row):
        kind = row["rec"]
        if kind == "control":
            self._apply_control(row)
        elif kind == "enqueue":
            event = row["event_id"]
            self._events[event] = dict(row, status="queued", drops=0)
            self._queues[row["agent_id"]].append(event)
            if "tm" in row:
                self._tasks[row["microtask_id"]] = event
        elif kind == "turn":
            lock = self._locks[row["surface_id"]]
            lock["status"] = "sent"
            self._attempted.add(lock["request_id"])
            if row["event_id"] in self._events:
                event = self._events[row["event_id"]]
                self._queues[event["agent_id"]].remove(row["event_id"])
                event["status"] = "sent"
        elif kind == "close":
            event = self._events[row["event_id"]]
            queue = self._queues[event["agent_id"]]
            if event["status"] == "queued":
                queue.remove(row["event_id"])
            event.update(status=row["verdict"], drops=row["drops"])
            if row["requeued"]:
                event["status"] = "queued"
                queue.append(row["event_id"])
            if row.get("lock_id"):
                surface = row["surface_id"]
                if row["verdict"] == "OUTCOME_UNCERTAIN":
                    self._locks[surface]["status"] = "UNKNOWN"
                else:
                    self._locks.pop(surface)
        elif kind != "HUMAN_NEEDED":
            raise BrokerError("unknown broker record")

    def _apply_control(self, row):
        op = row["op"]
        if op == "bind":
            b = row["binding"]
            validate_contract("vp.surface_binding/v1", b)
            surface, session = b["surface_id"], row["session_id"]
            self._bindings[surface] = (session, b)
            self._aliases.update(dict.fromkeys(row["aliases"], surface))
            self._sessions.setdefault(session, {"owner": b["owner"], "state": row["lease_state"],
                                                 "epoch": b["authority_epoch"]})
        elif op in ("take", "return"):
            lease = row["lease"]
            validate_contract("vp.human_lease/v1", lease)
            session = lease["session_id"]
            self._sessions[session].update(state=lease["state"], epoch=lease["epoch_after"])
            for bound_session, binding in self._bindings.values():
                if bound_session == session:
                    binding["authority_epoch"] = lease["epoch_after"]
            for lock in self._locks.values():
                if lock["session_id"] == session:
                    lock["status"] = "UNKNOWN"
                    self._attempted.add(lock["request_id"])
        elif op == "perception":
            state = row["perception"]
            validate_contract("vp.perception_state/v1", state)
            self._perception[state["surface_id"]] = state
        elif op == "acquire":
            self._locks[row["surface_id"]] = dict(row, status="held")
        elif op == "quarantine":
            self._attempted.add(self._locks[row["surface_id"]]["request_id"])
            self._locks[row["surface_id"]]["status"] = "UNKNOWN"
        elif op in ("release", "reconcile"):
            self._locks.pop(row["surface_id"])
            if op == "reconcile":
                event = self._events.get(row["request_id"])
                if event:
                    if event["status"] == "queued":
                        self._queues[event["agent_id"]].remove(row["request_id"])
                    event["status"] = row["verdict"]
        else:
            raise BrokerError("unknown broker control")

    @serialized
    def bind(self, session_id, binding, aliases=(), *, lease_state=None):
        """Register trusted aliases. First registration requires explicit session authority."""
        identifier(session_id)
        binding = deepcopy(binding)
        validate_contract("vp.surface_binding/v1", binding)
        surface = binding["surface_id"]
        if isinstance(aliases, str):
            raise BrokerError("BAD_INTENT")
        names = sorted({surface, binding["endpoint"], *(identifier(a) for a in aliases)})
        existing = self._bindings.get(surface)
        if existing and existing != (session_id, binding):
            raise BrokerError("identity_mismatch")
        if any(self._aliases.get(a, surface) != surface for a in names):
            raise BrokerError("identity_mismatch")
        session = self._sessions.get(session_id)
        if session and (binding["owner"], binding["authority_epoch"]) != (session["owner"], session["epoch"]):
            raise BrokerError("NO_LEASE")
        if session:
            if lease_state is not None and lease_state != session["state"]:
                raise BrokerError("NO_LEASE")
            lease_state = session["state"]
        elif lease_state not in ("AGENT", "HUMAN"):
            raise BrokerError("NO_LEASE")
        return self._write("control", op="bind", session_id=session_id, binding=binding,
                           aliases=names, lease_state=lease_state)

    def _target(self, target):
        surface = self._aliases.get(target)
        if surface is None:
            raise BrokerError("identity_mismatch")
        return self._bindings[surface]

    @serialized
    def binding(self, target):
        return deepcopy(self._target(target)[1])

    @serialized
    def lease(self, session_id):
        session = self._sessions.get(session_id)
        if session is None:
            raise BrokerError("NO_LEASE")
        return {"state": session["state"], "holder": session["owner"] if session["state"] == "AGENT" else "operator",
                "authority_epoch": session["epoch"]}

    def _admit(self, agent_id, target, incarnation, authority_epoch):
        session, b = self._target(target)
        if b["incarnation"] != incarnation:
            raise BrokerError("identity_mismatch")
        if self._sessions[session]["state"] != "AGENT" or agent_id != b["owner"]:
            raise BrokerError("NO_LEASE")
        check_epoch(authority_epoch, b["authority_epoch"])
        return session, b["surface_id"]

    @serialized
    def perception_transition(self, state):
        state = deepcopy(state)
        validate_contract("vp.perception_state/v1", state)
        surface = state["surface_id"]
        session, b = self._target(surface)
        if (surface, session, b["incarnation"]) != (b["surface_id"], state["session_id"], state["incarnation"]):
            raise BrokerError("identity_mismatch")
        check_epoch(state["authority_epoch"], b["authority_epoch"])
        old = self._perception.get(surface)
        if old and (old["stream_generation"] > state["stream_generation"] or
                    old["since_mono_ns"] > state["since_mono_ns"]):
            raise BrokerError("STALE_FRAME")
        return self._write("control", op="perception", perception=state)

    @serialized
    def perception_state(self, target):
        return deepcopy(self._perception.get(self._target(target)[1]["surface_id"]))

    @serialized
    def human_needed(self, agent_id, session_id, goal_id, reason):
        session = self._sessions.get(session_id)
        if not session or session["owner"] != agent_id:
            raise BrokerError("NO_LEASE")
        if reason not in ("login", "captcha", "2fa", "stuck", "operator"):
            raise BrokerError("BAD_INTENT")
        return self._write("HUMAN_NEEDED", session_id=session_id, goal_id=identifier(goal_id),
                           reason=reason, raised_by=agent_id)

    @serialized
    def human_lease(self, session_id, state, authority_epoch, reason):
        """Operator port only. Call after operator authentication at the host boundary."""
        session = self._sessions.get(session_id)
        if not session or state not in ("AGENT", "HUMAN") or state == session["state"]:
            raise BrokerError("NO_LEASE")
        check_epoch(authority_epoch, session["epoch"])
        locks = [l for l in self._locks.values() if l["session_id"] == session_id]
        if state == "AGENT" and locks:
            raise BrokerError("UNCERTAIN_OUTCOME")
        lease = {"schema": "vp.human_lease/v1", "session_id": session_id, "state": state,
                 "by": "broker", "requested_by": "operator", "reason": reason,
                 "epoch_before": authority_epoch, "epoch_after": authority_epoch + 1,
                 "outstanding": [{"request_id": l["request_id"], "disposition": "UNCERTAIN_OUTCOME"}
                                 for l in locks]}
        validate_contract("vp.human_lease/v1", lease)
        row = self._write("control", op="take" if state == "HUMAN" else "return", lease=lease)
        self._close_stale_queue(session_id)
        return row

    def _close_stale_queue(self, session_id):
        # The fence is already durable. A restart finishes any remaining closes.
        # A locked request is reconciled separately and cannot be classified unsent.
        session = self._sessions[session_id]
        held = {lock["request_id"] for lock in self._locks.values()}
        for event_id in tuple(self._queues[session["owner"]]):
            event = self._events[event_id]
            if (event["session_id"] == session_id and event["authority_epoch"] != session["epoch"]
                    and event_id not in held):
                self._close_event(event, "NO_LEASE", event["drops"], False)

    @serialized
    def enqueue(self, agent_id, event_id, target, incarnation, authority_epoch, tm=None):
        session, surface = self._admit(agent_id, target, incarnation, authority_epoch)
        if not EVENT_ID_RE.fullmatch(event_id):
            raise BrokerError("BAD_INTENT")
        if event_id in self._events or event_id in self._attempted or any(l["request_id"] == event_id for l in self._locks.values()):
            raise BrokerError("DUPLICATE_EVENT")
        if len(self._queues[agent_id]) >= QUEUE_MAX:
            raise BrokerError("QUEUE_FULL")
        fields = {}
        if tm is not None:
            tm = deepcopy(tm)
            validate_contract("tm/v1", tm)
            if tm["task_id"].split(".")[1] != agent_id or not tm["microtask_id"].startswith(tm["task_id"] + ".m"):
                raise BrokerError("TM_ID_MISMATCH")
            if tm["microtask_id"] in self._tasks:
                raise BrokerError("DUPLICATE_EVENT")
            if any(dep not in self._tasks for dep in tm["after"]):
                raise BrokerError("TM_DEP_UNKNOWN")
            fields.update(tm=tm, microtask_id=tm["microtask_id"], task_id=tm["task_id"])
        return self._write("enqueue", event_id=event_id, agent_id=agent_id, surface_id=surface,
                           session_id=session, incarnation=incarnation, authority_epoch=authority_epoch, **fields)

    def _can_requeue(self, event, drops):
        return event["status"] == "queued" and drops < MAX_DROPS

    @serialized
    def drop(self, event_id, verdict):
        """Admission refusal, before dispatch. Never recycle a possibly sent turn."""
        if verdict not in ("STALE_FRAME", "TURN_TIMEOUT", "ACTUATOR_BUSY"):
            raise BrokerError("BAD_INTENT")
        event = self._events[event_id]
        if event["status"] != "queued" or self._queues[event["agent_id"]][0] != event_id:
            raise BrokerError("NO_LEASE")
        if any(l["request_id"] == event_id for l in self._locks.values()):
            raise BrokerError("NO_LEASE")
        drops = event["drops"] + int(verdict != "ACTUATOR_BUSY")
        return self._close_event(event, verdict, drops, self._can_requeue(event, drops))

    def _close_event(self, event, verdict, drops, requeued, **fields):
        stamps = {k: event[k] for k in ("task_id", "microtask_id") if k in event}
        return self._write("close", agent_id=event["agent_id"], event_id=event["event_id"],
                           surface_id=event["surface_id"], verdict=verdict, drops=drops,
                           requeued=requeued, **stamps, **fields)

    def _check_queue(self, request_id, agent_id, surface, incarnation, epoch):
        event = self._events.get(request_id)
        if not event:
            return
        if (event["agent_id"], event["surface_id"], event["incarnation"], event["authority_epoch"]) != (
                agent_id, surface, incarnation, epoch):
            raise BrokerError("NO_LEASE")
        if event["status"] != "queued" or self._queues[agent_id][0] != request_id:
            raise BrokerError("NO_LEASE")
        for dep in event.get("tm", {}).get("after", []):
            if self._events[self._tasks[dep]]["status"] not in ("VERIFIED", "STATE_VERIFIED", "VERIFIED_WITH_SPILL"):
                raise BrokerError("MICROTASK_BUSY")

    @serialized
    def try_acquire(self, agent_id, target, incarnation, authority_epoch, request_id):
        identifier(request_id)
        session, surface = self._admit(agent_id, target, incarnation, authority_epoch)
        if request_id in self._attempted:
            raise BrokerError("DUPLICATE_EVENT")
        self._check_queue(request_id, agent_id, surface, incarnation, authority_epoch)
        if surface in self._locks or any(l["request_id"] == request_id for l in self._locks.values()):
            raise BrokerError("NO_LEASE")
        return self._write("control", op="acquire", surface_id=surface, session_id=session,
                           request_id=request_id, agent_id=agent_id, incarnation=incarnation,
                           authority_epoch=authority_epoch, lock_id=uuid.uuid4().hex)["lock_id"]

    @serialized
    def lock_status(self, target):
        """Read-only supervisor view, including acquisition identity for recovery."""
        return deepcopy(self._locks.get(self._target(target)[1]["surface_id"]))

    def _held(self, agent_id, lock_id):
        for lock in self._locks.values():
            if lock["lock_id"] == lock_id and lock["agent_id"] == agent_id:
                return lock
        raise BrokerError("NO_LEASE")

    @serialized
    def dispatch(self, agent_id, lock_id):
        lock = self._held(agent_id, lock_id)
        self._admit(agent_id, lock["surface_id"], lock["incarnation"], lock["authority_epoch"])
        if lock["status"] != "held":
            raise BrokerError("NO_LEASE")
        event_id = lock["request_id"]
        self._check_queue(event_id, agent_id, lock["surface_id"], lock["incarnation"], lock["authority_epoch"])
        event = self._events.get(event_id, {})
        stamps = {k: event[k] for k in ("task_id", "microtask_id") if k in event}
        return self._write("turn", agent_id=agent_id, event_id=event_id, surface_id=lock["surface_id"],
                           lock_id=lock_id, **stamps)

    @serialized
    def release(self, agent_id, lock_id):
        lock = self._held(agent_id, lock_id)
        if lock["status"] != "held":
            raise BrokerError("UNCERTAIN_OUTCOME")
        return self._write("control", op="release", surface_id=lock["surface_id"], lock_id=lock_id)

    @serialized
    def quarantine(self, agent_id, lock_id):
        lock = self._held(agent_id, lock_id)
        event = self._events.get(lock["request_id"])
        if event and event["status"] in ("sent", "queued"):
            return self._close_event(event, "OUTCOME_UNCERTAIN", event["drops"], False, lock_id=lock_id)
        return self._write("control", op="quarantine", surface_id=lock["surface_id"], lock_id=lock_id)

    @serialized
    def finish(self, agent_id, lock_id, verdict):
        """Trusted executor result. A timeout after permission to send is UNKNOWN."""
        lock = self._held(agent_id, lock_id)
        if lock["status"] != "sent":
            raise BrokerError("NO_LEASE")
        if verdict in ("UNKNOWN", "TURN_TIMEOUT", "INPUT_UNCERTAIN", "OUTCOME_UNCERTAIN", "UNCERTAIN_OUTCOME"):
            return self.quarantine(agent_id, lock_id)
        if verdict not in ("VERIFIED", "STATE_VERIFIED", "VERIFIED_WITH_SPILL", "JOB_ERROR"):
            raise BrokerError("BAD_INTENT")
        event = self._events.get(lock["request_id"])
        if event:
            return self._close_event(event, verdict, event["drops"], False, lock_id=lock_id)
        return self._write("control", op="release", surface_id=lock["surface_id"],
                           lock_id=lock_id, verdict=verdict, request_id=lock["request_id"])

    @serialized
    def reconcile(self, lock_id, request_id, verdict, observation_sha256):
        """Trusted verifier attests a positively observed effect or absence of effect."""
        lock = next((l for l in self._locks.values() if l["lock_id"] == lock_id), None)
        if not lock or lock["status"] != "UNKNOWN" or lock["request_id"] != request_id:
            raise BrokerError("NO_LEASE")
        if verdict not in ("VERIFIED", "NO_EFFECT") or not re.fullmatch("[a-f0-9]{64}", observation_sha256):
            raise BrokerError("UNCERTAIN_OUTCOME")
        event = self._events.get(request_id, {})
        stamps = {k: event[k] for k in ("event_id", "task_id", "microtask_id") if k in event}
        return self._write("control", op="reconcile", surface_id=lock["surface_id"], lock_id=lock_id,
                           request_id=request_id, verdict=verdict, observation_sha256=observation_sha256,
                           **stamps)

    @serialized
    def agent_port(self, agent_id):
        from .port import provision
        return provision(self, identifier(agent_id))
