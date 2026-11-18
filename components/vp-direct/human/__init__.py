"""Broker-owned human handoff for a provisioned session.

Install handlers() behind C1's authenticated lanes. The supervisor supplies the
complete executor set, a source of effective capability and privacy decisions,
and a perception owner that can drain just the agent's interest. The registry
supplies instance identity. All current authority comes from the broker.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
from threading import local
from time import monotonic_ns
from typing import Protocol

from jsonschema import ValidationError

from bus.broker import BrokerError
from bus.broker.qualification import load_wl0
from control.core.control import Handler, Outcome
from recorder.timeline import TimelinePosition
from schemas.validation import canonical_bytes, validate_contract
from window import WindowRefused

from .authority import HandoffBroker
from .windows import FocusWitness, HandoffWindows


class HandoffRefused(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise HandoffRefused(code)


@dataclass(frozen=True)
class FreshObservation:
    session_id: str
    surface_id: str
    incarnation: str
    authority_epoch: int
    observation_id: str
    captured_mono_ns: int
    generation: int


class HandoffRuntime(Protocol):
    """Trusted, session-bound ports. Reconnection is observation-only until release."""

    def last_observation_id(self): ...  # Read cached identifier only.
    def reconcile(self, lock): ...  # Previously verified (verdict, sha256), or None.
    def detach(self): ...  # Drain and detach all CDP and AT-SPI executors.
    def attachments(self): ...  # Tuple of currently attached channel names.
    def retain(self, session, binding): ...  # Drain agent, return vp.perception_state/v1.
    def reconnect(self, mode, session): ...
    def reauthorize(self, session, binding): ...
    def observe(self, session, binding) -> FreshObservation: ...


def _identity(session):
    fields = ("session_id", "capsule_id", "incarnation", "display", "window", "backend",
              "cdp_allowed", "cdp_port", "cdp_target_id", "site_group", "profile_id", "browser")
    return sha256(canonical_bytes({k: session.get(k) for k in fields})).hexdigest()


class HumanHandoff:
    def __init__(self, *, broker, registry, session_id, surface_id, windows, runtime,
                 lineage, goal_id=None, goal=None, recorder=None, receipt=None, clock=monotonic_ns):
        self.broker, self.registry = broker, registry
        self.session_id, self.surface_id = session_id, surface_id
        self.windows, self.runtime, self.lineage = windows, runtime, lineage
        self.goal, self.goal_id, self.recorder = goal, goal_id, recorder
        self.clock = clock
        self._preparing = local()
        try:
            if receipt is None and isinstance(windows, HandoffWindows):
                projection = windows.qualification_receipt()
            else:
                projection = load_wl0(receipt)
            step = projection["steps"]["L0-6"]
            self.mode = step["reattach"]
            require(step["status"] == "PASS" and step["cdp_session_during_human"] is False
                    and self.mode in ("reconnect", "relaunch-attach")
                    and projection["focus_rules"]["human_cdp"]["reattach_mode"] == self.mode, "HOLD")
        except (ValueError, KeyError, TypeError) as exc:
            raise HandoffRefused("HOLD") from exc

    def handlers(self):
        return {op: Handler(self._wire_execute, self._wire_preview) for op in ("vp_human_take", "vp_human_return")}

    def _wire_execute(self, request):
        return self._wire(self.execute(request))

    def _wire_preview(self, request):
        return self._wire(self.preview(request))

    @staticmethod
    def _wire(outcome):
        if not outcome.ok:
            return outcome
        # legacyResult admits an empty acknowledgement. The broker and U3c
        # hold the complete human lease, and the executor owns the observation.
        return Outcome(True, outcome.code, not_verified=outcome.not_verified)

    def _session(self):
        session = self.registry.get_session(self.session_id)
        require(session is not None, "identity_mismatch")
        validate_contract("vp.session/v1", session)
        binding = self.broker.registered_binding(self.session_id, self.surface_id)
        require(session["session_id"] == self.session_id and session["state"] in ("AGENT", "HUMAN", "LOGIN_PENDING")
                and session.get("incarnation") == binding["incarnation"] and session.get("window"),
                "identity_mismatch")
        lease = self.broker.lease(self.session_id)
        return dict(session, state=lease["state"], lease={k: lease[k] for k in ("holder", "authority_epoch")}), binding

    def get_session(self, session_id):
        """Session reader for U1a/S3. Never returns agent custody during preparation."""
        require(session_id == self.session_id, "identity_mismatch")
        return self._session()[0]

    def read_executor_session(self, session_id):
        """Use as U1b's read_session. Only the synchronous supervisor read sees the next epoch."""
        require(session_id == self.session_id, "identity_mismatch")
        session, binding = self._session()
        preparing = getattr(self._preparing, "binding", None)
        if preparing and session["lease"]["holder"] is None:
            require(preparing == (_identity(session), binding), "identity_mismatch")
            session.update(state="AGENT", lease=dict(holder=binding["owner"], authority_epoch=binding["authority_epoch"]))
        return session

    def _gap(self, session, active, at):
        if self.recorder is not None:
            pos = TimelinePosition(at, session["capsule_id"], session["incarnation"])
            self.recorder.set_gap(pos, "human", active=active)

    @staticmethod
    def _event_id(row):
        return sha256(canonical_bytes(row["lease"])).hexdigest()

    def _lineage(self, row):
        self.lineage.append(agent="broker", op_id=self._event_id(row),
                            kind="vp.human_lease/v1", payload=row["lease"])

    def _has_take(self, row):
        return any(event["op_id"] == self._event_id(row) and event["agent"] == "broker"
                   and event["payload"] == row["lease"] for event in self.lineage.verify())

    def _goal(self, row, observation=None):
        if self.goal is None:
            return
        state = self.goal.status()
        lease = row["lease"]
        if state["authority_epoch"] == lease["epoch_after"] and state["human"] == (lease["state"] == "HUMAN"):
            if observation and state["observation"] != observation.observation_id:
                self.goal.observed(observation.observation_id, observation.generation)
            return
        self.goal.human_lease(row, observation_id=observation.observation_id if observation else None,
                              generation=observation.generation if observation else None)

    def _detach(self):
        self.runtime.detach()
        require(self.runtime.attachments() == (), "HOLD")

    def _retain(self, session, binding):
        state = self.runtime.retain(session, binding)
        validate_contract("vp.perception_state/v1", state)
        require(state["state"] == "ASLEEP" and state["surface_id"] == self.surface_id
                and state["session_id"] == self.session_id, "HOLD")
        self.broker.perception_transition(state)

    def _reconcile(self):
        for lock in self.broker.pending_acts(self.session_id):
            require(lock["status"] == "UNKNOWN", "UNCERTAIN_OUTCOME")
            proof = self.runtime.reconcile(lock)
            if proof is not None:
                verdict, digest = proof
                self.broker.reconcile(lock["lock_id"], lock["request_id"], verdict, digest)

    def vp_human_take(self, request):
        """Consume a normalized C1 envelope. Caller identity must be stamped by the lane."""
        return self._call(request, "vp_human_take")

    def vp_human_return(self, request):
        return self._call(request, "vp_human_return")

    def _take(self, session, reason):
        taken, returned = self.broker.history(self.session_id)
        if session["state"] == "AGENT":
            require(session["lease"]["holder"] is not None, "NO_LEASE")
            saved = self.windows.save_focus()
            require(isinstance(saved, FocusWitness) and type(saved.xid) is int and saved.xid > 0
                    and saved.pid > 0 and saved.witness, "identity_mismatch")
            old = self.runtime.last_observation_id()
            require(old is None or (type(old) is str and bool(old)), "STALE_FRAME")
            checkpoint = dict(focus=asdict(saved), identity=_identity(session), old_observation=old)
            self._gap(session, True, self.clock())
            taken = self.broker.fence(self.session_id, session["lease"]["authority_epoch"], reason, checkpoint)
        else:
            # An explicit retry may finish a fenced take. It cannot replace the
            # saved focus after the operator has started interacting.
            require(taken and not returned and taken["lease"]["reason"] == reason, "NO_LEASE")
        try:
            session, binding = self._session()
            require(session["state"] == "HUMAN" and taken.get("handoff", {}).get("identity") == _identity(session),
                    "identity_mismatch")
            self._gap(session, True, self.clock())
            self._goal(taken)
            self._reconcile()
        finally:
            self._detach()
        self._retain(session, binding)
        require(self.runtime.attachments() == (), "HOLD")
        require(self._session() == (session, binding), "identity_mismatch")
        self.windows.summon(session)
        self._lineage(taken)
        return Outcome(True, "OK", {"lease": taken["lease"]})

    def _return(self, session, binding):
        taken, _ = self.broker.history(self.session_id)
        require(taken and (session["state"] == "HUMAN" or session["lease"]["holder"] is None)
                and self._has_take(taken), "NO_LEASE")
        checkpoint = taken.get("handoff")
        require(checkpoint and checkpoint["identity"] == _identity(session), "identity_mismatch")
        self._detach()
        self._reconcile()
        require(not self.broker.pending_acts(self.session_id), "UNCERTAIN_OUTCOME")
        self.windows.dismiss(session)
        saved = FocusWitness(**checkpoint["focus"])
        self.windows.restore_focus(saved)
        require(self._session() == (session, binding), "identity_mismatch")
        lease = self.broker.prepare_return(self.session_id, binding["authority_epoch"])
        epoch = lease["epoch_after"]
        require(epoch == taken["lease"]["epoch_after"] + 1, "NO_LEASE")
        gated, binding = self._session()
        require(gated["lease"]["holder"] is None and binding["authority_epoch"] == epoch, "NO_LEASE")
        # Only the supervisor's read-only reconnect receives this projection.
        candidate = dict(gated, state="AGENT", lease=dict(holder=binding["owner"], authority_epoch=epoch))
        self._preparing.binding = (_identity(gated), binding)
        try:
            self.runtime.reconnect(self.mode, candidate)
            require(self.runtime.attachments() == (candidate["backend"],), "HOLD")
            require(self.runtime.reauthorize(candidate, binding) is True, "NO_LEASE")
            start = self.clock()
            obs = self.runtime.observe(candidate, binding)
            require(isinstance(obs, FreshObservation) and obs.session_id == self.session_id
                    and obs.surface_id == self.surface_id and obs.incarnation == binding["incarnation"]
                    and type(obs.authority_epoch) is int and obs.authority_epoch == epoch
                    and type(obs.observation_id) is str and bool(obs.observation_id)
                    and obs.observation_id != checkpoint["old_observation"]
                    and type(obs.generation) is int and obs.generation >= 0
                    and type(obs.captured_mono_ns) is int and start <= obs.captured_mono_ns <= self.clock(),
                    "STALE_FRAME")
            require(self._session() == (gated, binding), "identity_mismatch")
            self.windows.verify_focus(saved)
            returned = self.broker.record_return(lease)
            self._lineage(returned)
            self._goal(returned, obs)
            self._gap(session, False, self.clock())
            self.broker.finish_return(returned, obs)
        except BaseException:
            try:
                self._detach()
            finally:
                self._gap(session, True, self.clock())
            raise
        finally:
            del self._preparing.binding
        return Outcome(True, "OK", {"lease": returned["lease"], "fresh_observation_id": obs.observation_id})

    def _call(self, request, op, *, preview=False):
        try:
            validate_contract("vp.control.req/v1", request)
            require(request["op"] == op and op in ("vp_human_take", "vp_human_return"), "-32602")
            with self.broker.handoff_lock:
                ctx, args = request["context"], request["args"]
                session, binding = self._session()
                require(args["session_id"] == self.session_id and ctx.get("session_id") == self.session_id
                        and ctx.get("authority_epoch") == binding["authority_epoch"]
                        and ctx.get("incarnation") == binding["incarnation"]
                        and ctx.get("capsule_id", session.get("capsule_id")) == session.get("capsule_id"),
                        "NO_LEASE")
                if ctx["caller"] == "agent":
                    require(op == "vp_human_take" and session["state"] == "AGENT" and session["lease"]["holder"] == binding["owner"] and self.goal_id
                            and ctx["agent_instance"] == binding["owner"], "NO_LEASE")
                    if not (preview or request["dry_run"]):
                        self.broker.human_needed(binding["owner"], self.session_id, self.goal_id, args["reason"])
                    return Outcome(False, "HUMAN_NEEDED")
                if preview or request["dry_run"]:
                    return Outcome(True, "OK", {"state": session["state"]}, not_verified=("dry-run",))
                if op == "vp_human_take":
                    return self._take(session, args["reason"])
                return self._return(session, binding)
        except ValidationError:
            return Outcome(False, "-32602")
        except (HandoffRefused, BrokerError) as exc:
            return Outcome(False, str(exc))
        except WindowRefused as exc:
            return Outcome(False, exc.code)

    def execute(self, request):
        return self._call(request, request.get("op"))

    def preview(self, request):
        return self._call(request, request.get("op"), preview=True)
