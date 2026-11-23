"""Validate, route and fence vp-direct channel requests at the effect boundary."""

from copy import deepcopy
from dataclasses import dataclass
import json
import re
import sqlite3

from jsonschema.exceptions import ValidationError

from bus.broker.journal import BrokerError
from schemas.validation import SCHEMA_DIR, canonical_bytes, validate_contract

from .capabilities import AtspiAction, EffectiveCapabilities, SEMANTIC_BACKENDS
from .request_store import RequestStore, value_evidence


REQUEST = "vp.channel.req/v1"
REPLY = "vp.channel.reply/v1"
RISKS = ("observar", "leer", "preparar", "enviar", "publicar", "destructivo", "irreversible")
UNCERTAIN = frozenset({"INPUT_UNCERTAIN", "UNCERTAIN_OUTCOME", "OUTCOME_UNCERTAIN", "TURN_TIMEOUT", "UNKNOWN"})
_ATSPI_IDENTITY_CODES = frozenset({"ATSPI_AMBIGUOUS_NODE", "ATSPI_AMBIGUOUS_PID", "ATSPI_RETIRED_OBJECT",
                                   "ATSPI_STALE_OBSERVATION", "ATSPI_TREE_INCOMPLETE", "ATSPI_TREE_TRUNCATED"})
NO_FALLBACK = UNCERTAIN | _ATSPI_IDENTITY_CODES | {"identity_mismatch", "NO_LEASE", "OWNER", "STALE_FRAME"}
_PERCEPTION_BACKENDS = {
    "none": frozenset(),
    "cdp-atspi": SEMANTIC_BACKENDS,
    "vision": SEMANTIC_BACKENDS | {"vision"},
}

KNOWN_CODES = frozenset(json.loads((SCHEMA_DIR / "error_codes.json").read_bytes())) | UNCERTAIN | {
    "BAD_INTENT", "DUPLICATE_EVENT", "OBSERVED", "bad_shape",
    "ATSPI_ACTION_UNQUALIFIED", "ATSPI_ACTION_UNSUPPORTED", "ATSPI_ACTION_REFUSED", "ATSPI_INVALID_REQUEST",
} | _ATSPI_IDENTITY_CODES


@dataclass(frozen=True)
class TargetContext:
    """Identity and qualifications supplied by the session authority."""

    session: dict
    surface_id: str
    surface_kind: str
    app: str | None = None
    private_backends: frozenset[str] = frozenset()
    perception_level: str = "vision"


@dataclass(frozen=True)
class Observation:
    """Current AX snapshot and the trusted resolver's matches for this action."""

    session_id: str
    capsule_id: str
    surface_id: str
    incarnation: str
    authority_epoch: int
    observation_id: str
    backend: str
    ax_stable_sha256: str
    node_matches: tuple[str, ...]
    frame_sha256: str | None = None
    atspi_action: AtspiAction | None = None


class ChannelRefused(Exception):
    """A typed refusal before the backend has permission to send."""

    def __init__(self, code, fallback_from=None):
        self.code = code if code in KNOWN_CODES else "HOLD"
        self.fallback_from = fallback_from
        super().__init__(self.code)


def _reply(code, *, ok=False, fallback_from=None):
    reply = {"schema": REPLY, "ok": ok, "code": code}
    if fallback_from is not None:
        reply["fallback_from"] = fallback_from
    return reply


class ChannelCore:
    """Run in the trusted broker executor with authenticated agent identities.

    ``target_for(session_id)`` and ``observation_for(request, target)`` read the
    current authority snapshots. The latter resolves a provisioned action's
    node against the current snapshot, keeping all matches, including duplicates.
    Each backend is a side-effect-free prepare function (request, target,
    observation) returning a callable (lock_id) -> vp.channel.reply/v1. It may
    raise ChannelRefused before send. The returned callable must enforce the
    live target and epoch at the effect boundary and return verified readback.
    Non-act callables hold the broker lock for observation or transport management.
    Read-only reconciliation under UNKNOWN custody receives None and must never
    manage transport or send an action.

    ``risk_for`` supplies the trusted operation risk floor. ``authorize``
    checks the routed action, including pace and any specific operator approval,
    and returns True or a refusal code. Both are supplied by the control plane.
    """

    def __init__(self, *, broker, target_for, observation_for, backends, wl0=None,
                 request_store: RequestStore, risk_for, authorize, fallback_codes=()):
        self._broker = broker
        self._target_for = target_for
        self._observation_for = observation_for
        self._backends = dict(backends)
        self._capabilities = EffectiveCapabilities(wl0)
        self._store = request_store
        self._risk_for = risk_for
        self._authorize = authorize
        self._fallback_codes = frozenset(fallback_codes)

    def _target(self, request, agent_id):
        target = deepcopy(self._target_for(request["session_id"]))
        if not isinstance(target, TargetContext) or not target.surface_id:
            raise ChannelRefused("identity_mismatch")
        session = target.session
        validate_contract("vp.session/v1", session)
        lease = self._broker.lease(request["session_id"])
        if (session["state"] != "AGENT" or lease["state"] != "AGENT"
                or agent_id != session["lease"]["holder"] or agent_id != lease["holder"]):
            raise ChannelRefused("NO_LEASE")
        if not request["authority_epoch"] == lease["authority_epoch"] == session["lease"]["authority_epoch"]:
            raise ChannelRefused("NO_LEASE")
        for field in ("session_id", "capsule_id", "incarnation"):
            if request[field] != session.get(field):
                raise ChannelRefused("identity_mismatch")
        binding = self._broker.binding(target.surface_id)
        if (binding["surface_id"], binding["incarnation"]) != (target.surface_id, request["incarnation"]):
            raise ChannelRefused("identity_mismatch")
        if (binding["owner"], binding["authority_epoch"]) != (agent_id, request["authority_epoch"]):
            raise ChannelRefused("NO_LEASE")

        selector = request.get("target", {})
        if ("window_id" in selector and selector["window_id"] != session["window"]["xid"]
                or "cdp_target_id" in selector and selector["cdp_target_id"] != session.get("cdp_target_id")
                or selector.get("surface_kind") not in {None, "vm", target.surface_kind}):
            raise ChannelRefused("identity_mismatch")
        if target.surface_kind == "vm":
            frame = selector.get("based_on_frame")
            if request["op"] == "act" and frame is not None:
                # The resolver reads a published observation without acquiring a frame.
                observation = self._current_observation(request, target)
                if frame["sha256"] != observation.frame_sha256:
                    raise ChannelRefused("STALE_FRAME")
            # Guest transport still requires qualification, even with a matching frame.
            raise ChannelRefused("DISPLAY_UNSUPPORTED")
        if selector.get("surface_kind") == "vm":
            raise ChannelRefused("identity_mismatch")
        return target

    def _current_observation(self, request, target):
        observation = self._observation_for(deepcopy(request), deepcopy(target))
        if not isinstance(observation, Observation) or observation.surface_id != target.surface_id:
            raise ChannelRefused("STALE_FRAME")
        for field in ("session_id", "capsule_id", "incarnation", "authority_epoch", "observation_id"):
            if getattr(observation, field) != request[field]:
                raise ChannelRefused("STALE_FRAME")
        return observation

    def _observation(self, request, target):
        if request["op"] != "act":
            return None
        observation = self._current_observation(request, target)
        if request["backend"] in SEMANTIC_BACKENDS and observation.backend != request["backend"]:
            raise ChannelRefused("identity_mismatch")
        if request["require_vision_witness"] and not re.fullmatch(r"[a-f0-9]{64}", observation.frame_sha256 or ""):
            raise ChannelRefused("STALE_FRAME")

        selector = request["target"]
        if (observation.backend in SEMANTIC_BACKENDS
                and not re.fullmatch(r"[a-f0-9]{64}", observation.ax_stable_sha256)):
            raise ChannelRefused("STALE_FRAME")
        if "ax_stable_sha256" in selector and selector["ax_stable_sha256"] != observation.ax_stable_sha256:
            raise ChannelRefused("STALE_FRAME")
        if request["action"] == "navigate":
            if request["backend"] != "cdp" or "cdp_target_id" not in selector or "url" not in request:
                raise ChannelRefused("identity_mismatch")
        elif (type(observation.node_matches) is not tuple or len(observation.node_matches) != 1
              or not isinstance(observation.node_matches[0], str) or not observation.node_matches[0]):
            raise ChannelRefused("identity_mismatch")
        return observation

    def _risk(self, request, target):
        risk = request["risk_class"]
        if request["op"] == "act":
            effective = self._risk_for(deepcopy(request), deepcopy(target.session))
            if effective not in RISKS:
                raise ChannelRefused("HOLD")
            risk = max(risk, effective, key=RISKS.index)
        if RISKS.index(risk) > RISKS.index(target.session["risk_ceiling"]):
            raise ChannelRefused("NO_LEASE")
        return risk

    def _prepare(self, request, target, observation, backend):
        if (backend not in _PERCEPTION_BACKENDS.get(target.perception_level, ())
                or (request["require_vision_witness"] and target.perception_level != "vision")
                or backend not in self._backends
                or not self._capabilities.allows(backend, target, request, observation)):
            raise ChannelRefused("DISPLAY_UNSUPPORTED")
        if (backend == "vision" and request["op"] == "act"
                and not re.fullmatch(r"[a-f0-9]{64}", observation.frame_sha256 or "")):
            raise ChannelRefused("STALE_FRAME")
        routed = dict(deepcopy(request), backend=backend)
        perform = self._backends[backend](deepcopy(routed), deepcopy(target), observation)
        if not callable(perform):
            raise ChannelRefused("HOLD")
        return routed, perform

    def _plan(self, request, target, observation, risk):
        backend = request["backend"]
        act = request["op"] == "act"
        high_risk = act and RISKS.index(risk) >= RISKS.index("enviar")
        if high_risk and backend not in SEMANTIC_BACKENDS:
            raise ChannelRefused("FALLBACK_FORBIDDEN_IRREVERSIBLE")
        try:
            routed, perform = self._prepare(request, target, observation, backend)
        except ChannelRefused as error:
            if error.code in NO_FALLBACK:
                raise
            if high_risk and (error.code == "DISPLAY_UNSUPPORTED" or (backend, error.code) in self._fallback_codes):
                raise ChannelRefused("FALLBACK_FORBIDDEN_IRREVERSIBLE") from None
            if (not act or backend not in SEMANTIC_BACKENDS or request["action"] == "navigate"
                    or (backend, error.code) not in self._fallback_codes):
                raise
            try:
                routed, perform = self._prepare(request, target, observation, "vision")
            except ChannelRefused as fallback_error:
                raise ChannelRefused(fallback_error.code, backend) from None
            return routed, perform, backend
        return routed, perform, None

    def _verified_reply(self, reply, request):
        validate_contract(REPLY, reply)
        if reply["code"] not in KNOWN_CODES:
            raise ChannelRefused("HOLD")
        if request["op"] == "act":
            evidence = "frame_sha256" if request["backend"] == "vision" else "ax_sha256"
            if (not reply["ok"] or reply["code"] in UNCERTAIN or evidence not in reply
                    or request["require_vision_witness"] and "frame_sha256" not in reply):
                raise ChannelRefused("UNCERTAIN_OUTCOME")
        return {k: v for k, v in reply.items() if k not in {"fallback_from", "value_len", "value_sha256"}}

    def _quarantine(self, agent_id, lock_id):
        try:
            self._broker.quarantine(agent_id, lock_id)
        except (BrokerError, OSError):
            # The broker keeps custody and refuses new effects after a journal failure.
            pass

    def _require_lock(self, request, target, agent_id, lock_id, *, pending_request_id=None):
        lock = self._broker.lock_status(target.surface_id)
        fields = ("session_id", "surface_id", "agent_id", "incarnation", "authority_epoch", "request_id",
                  "lock_id", "status")
        expected = (request["session_id"], target.surface_id, agent_id, request["incarnation"],
                    request["authority_epoch"], pending_request_id or request["request_id"], lock_id,
                    "UNKNOWN" if pending_request_id is not None else "held")
        if not isinstance(lock, dict) or tuple(lock.get(field) for field in fields) != expected:
            raise ChannelRefused("NO_LEASE")

    def _locked_action(self, request, target, agent_id, lock_id):
        fallback_from = None
        marked = False
        try:
            observation = self._observation(request, target)
            risk = self._risk(request, target)
            routed, perform, fallback_from = self._plan(request, target, observation, risk)
            if fallback_from is not None:
                risk = max(risk, self._risk(routed, target), key=RISKS.index)
                if RISKS.index(risk) >= RISKS.index("enviar"):
                    raise ChannelRefused("FALLBACK_FORBIDDEN_IRREVERSIBLE")
            admitted = self._authorize(deepcopy(routed), deepcopy(target.session), risk)
            if admitted is not True:
                raise ChannelRefused(admitted if isinstance(admitted, str) else "NO_LEASE")
            if self._target(request, agent_id) != target or self._observation(request, target) != observation:
                raise ChannelRefused("STALE_FRAME")
            if request["dry_run"]:
                return _reply("OK", ok=True, fallback_from=fallback_from)

            # Mark before calling the only function that may cause an application effect.
            self._require_lock(request, target, agent_id, lock_id)
            marked = True
            mark = self._broker.dispatch(agent_id, lock_id)
            if not isinstance(mark, dict) or (
                mark.get("by"), mark.get("rec"), mark.get("agent_id"), mark.get("event_id"),
                mark.get("surface_id"), mark.get("lock_id"),
            ) != ("broker", "turn", agent_id, request["request_id"], target.surface_id, lock_id):
                raise ChannelRefused("UNCERTAIN_OUTCOME")
            reply = self._verified_reply(perform(lock_id), routed)
            self._broker.finish(agent_id, lock_id, "VERIFIED")
            if fallback_from is not None:
                reply["fallback_from"] = fallback_from
            return reply
        except Exception as error:
            if marked:
                self._quarantine(agent_id, lock_id)
                return _reply("UNCERTAIN_OUTCOME", fallback_from=fallback_from)
            if lock_id is not None:
                try:
                    self._broker.release(agent_id, lock_id)
                except (BrokerError, OSError):
                    self._quarantine(agent_id, lock_id)
                    return _reply("UNCERTAIN_OUTCOME", fallback_from=fallback_from)
            if isinstance(error, ChannelRefused):
                return _reply(error.code, fallback_from=error.fallback_from or fallback_from)
            if isinstance(error, BrokerError):
                return _reply(ChannelRefused(str(error)).code, fallback_from=fallback_from)
            return _reply("HOLD", fallback_from=fallback_from)

    def _passive(self, request, target, agent_id, lock_id=None, *, pending_request_id=None):
        risk = self._risk(request, target)
        routed, perform, _ = self._plan(request, target, None, risk)
        if request["dry_run"]:
            return _reply("OK", ok=True)
        if self._target(request, agent_id) != target:
            raise ChannelRefused("STALE_FRAME")
        self._require_lock(request, target, agent_id, lock_id, pending_request_id=pending_request_id)
        reply = self._verified_reply(perform(lock_id if pending_request_id is None else None), routed)
        if self._target(request, agent_id) != target:
            raise ChannelRefused("STALE_FRAME")
        self._require_lock(request, target, agent_id, lock_id, pending_request_id=pending_request_id)
        return reply

    def _execute(self, request, target, agent_id):
        act = request["op"] == "act"
        if request["dry_run"]:
            return (self._locked_action(request, target, agent_id, None) if act
                    else self._passive(request, target, agent_id))

        if request["op"] == "observe":
            custody = self._broker.lock_status(target.surface_id)
            if isinstance(custody, dict) and custody.get("status") == "UNKNOWN":
                lock_id, pending = custody.get("lock_id"), custody.get("request_id")
                if not all(isinstance(value, str) and value for value in (lock_id, pending)):
                    raise ChannelRefused("NO_LEASE")
                self._require_lock(request, target, agent_id, lock_id, pending_request_id=pending)
                return self._passive(request, target, agent_id, lock_id, pending_request_id=pending)

        lock_id = self._broker.try_acquire(agent_id, target.surface_id, request["incarnation"],
                                          request["authority_epoch"], request["request_id"])
        if not isinstance(lock_id, str) or not lock_id:
            raise ChannelRefused("NO_LEASE")
        self._require_lock(request, target, agent_id, lock_id)
        if act:
            return self._locked_action(request, target, agent_id, lock_id)
        try:
            return self._passive(request, target, agent_id, lock_id)
        finally:
            self._broker.release(agent_id, lock_id)

    def dispatch(self, request, *, agent_id):
        """Process one closed envelope; retain a pending reservation on uncertain loss."""
        try:
            validate_contract(REQUEST, request)
            canonical_bytes(request)
            if not isinstance(agent_id, str) or not agent_id:
                return _reply("NO_LEASE")
        except (ValidationError, ValueError, TypeError):
            return _reply("-32602")
        request = deepcopy(request)
        try:
            target = self._target(request, agent_id)
            if not request["dry_run"]:
                reserved, previous = self._store.reserve(agent_id, request)
                if not reserved:
                    if previous is None:
                        return _reply("UNCERTAIN_OUTCOME")
                    validate_contract(REPLY, previous)
                    return previous if previous["code"] in KNOWN_CODES else _reply("HOLD")
        except (ChannelRefused, BrokerError) as error:
            return _reply(ChannelRefused(str(error)).code)
        except (ValidationError, ValueError, OSError, sqlite3.Error):
            return _reply("HOLD")

        try:
            reply = self._execute(request, target, agent_id)
        except (ChannelRefused, BrokerError) as error:
            reply = _reply(ChannelRefused(str(error)).code)
        except Exception:
            reply = _reply("HOLD")
        reply.update(value_evidence(request))
        validate_contract(REPLY, reply)
        if not request["dry_run"]:
            try:
                self._store.complete(request["request_id"], reply)
            except sqlite3.Error:
                # Loss of the receipt never creates permission to repeat the operation.
                return _reply("UNCERTAIN_OUTCOME" if request["op"] == "act" else "HOLD")
        return reply
