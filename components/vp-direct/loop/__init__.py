"""Goal-scoped, resumable script/vision replay for reviewed U2b workflows."""
from copy import deepcopy
from contextlib import contextmanager
import fcntl
from hashlib import sha256
from pathlib import Path
from time import monotonic_ns

from recorder.store.memoryweb_store import canonical_bytes
from schemas.validation import validate_contract
from rewind.machine import Refused
from rewind.machine.transitions import STOP, require

from . import replay
from .goal import LoopGoal, apply_loop
from .owners import ProjectedOwners


class ReplayLoop:
    """Trusted composition root, never an agent-supplied callback table.

    open_owner provisions a fresh ReplayOwner for the exact projected document
    in a private run corpus. authorize consumes the site's pace/risk admission
    for this action and returns its effective class. target_for translates the
    bound R2 plan into a qualified AX target (or None). assess returns the U5
    quality and post-state digest. checkpoint marks once after the judge accepts. restore
    is U4a's broker port. no_effect optionally supplies a positive, exclusive
    AX predicate for the *specific* flight (absence of a postcondition is
    insufficient). effect_witness supplies a positive predicate independently
    bound to this claim's effect. Neither predicate may be inferred from quiet
    pixels. A missing witness leaves that outcome unavailable. All these ports
    belong to the service owner.
    """

    def __init__(self, goal, store, lifecycle, *, open_owner, authorize, vision,
                 assess, checkpoint, restore, session_for, target_for=replay.selector_from_plan, no_effect=None, effect_witness=None):
        self.goal, self.store, self.lifecycle = goal, store, lifecycle
        self.open_owner, self.authorize, self.vision = open_owner, authorize, vision
        self.target_for, self.assess, self.restore = target_for, assess, restore
        self.checkpoint = checkpoint
        self.session_for = session_for
        self.no_effect = no_effect
        self.effect_witness = effect_witness

    @contextmanager
    def _exclusive(self):
        # A crashed process releases the lock. Its persisted claim survives.
        suffix = sha256(self.goal.goal_id.encode()).hexdigest()
        path = Path(str(Path(self.goal.path).resolve(strict=True)) + "." + suffix + ".lock")
        with path.open("ab") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused("HOLD") from None
            yield

    def bind(self, site, workflow_id, revision):
        """Bind once to an admitted immutable revision and the existing U4a goal."""
        with self._exclusive():
            state = self.goal.replay()
            doc = self.store.load_workflow(site, workflow_id, revision)["workflow"]
            with self.open_owner(doc) as owner:
                replay.check_owner(owner, doc)
                replay.validate(doc, owner)
            require(state["session_id"] == self.lifecycle.session_id and all(
                self.lifecycle.binding[k] == state[k] for k in
                ("surface_id", "incarnation", "owner", "endpoint", "authority_epoch")), "NO_LEASE")
            return self.goal.transition(state, "bind", site=site, workflow=workflow_id,
                                        revision=revision, steps=len(doc["steps"]))

    def _session(self, state):
        # U2b's owner translates these clicks to coordinates. Re-read the S1
        # binding before acquisition and at the transport boundary.
        session = self.session_for(state["session_id"])
        validate_contract("vp.session/v1", session)
        require(session["display"].startswith("xpra:") and int(session["display"][5:]) > 0,
                "DISPLAY_UNSUPPORTED")
        require(session["session_id"] == state["session_id"] and session["incarnation"] == state["incarnation"]
                and session["capsule_id"] == state["checkpoint"]["capsule_id"] and session["state"] == "AGENT"
                and session["lease"] == dict(holder=state["owner"], authority_epoch=state["authority_epoch"]),
                "NO_LEASE")
        return replay.pin([session["capsule_id"], session["incarnation"], session["display"], session["window"]])

    def _restore(self, state):
        if state["restore_claim"] is not None:
            raise Refused("UNCERTAIN_OUTCOME")
        result = self.goal.run_restore(self.restore)
        if result == STOP:
            self.goal.deliver_handoff(self.lifecycle.broker)
        return result

    def advance(self):
        """Perform at most one forward dispatch or one observe-only reconciliation."""
        with self._exclusive():
            state = self.goal.status()
            phase = state["phase"]
            require(phase not in (STOP, "HUMAN_NEEDED") and not state["human"]
                    and state["perception"] == "ACTIVE" and not state["q"], "NO_LEASE")
            session_pin = self._session(state)
            if phase in ("REWINDING", "CLEANUP"):
                return self._restore(state)
            lane = state["loop"]
            if state["n"] == state["i"]:
                return "COMPLETE"
            # A broker handback cannot clear an unresolved R1 flight.
            if phase == "READY" and lane["claim"] is not None:
                self.goal.transition(state, "stop", reason="UNCERTAIN_OUTCOME")
                return self._restore(self.goal.status())
            require(self.lifecycle.session_id == state["session_id"] and all(
                self.lifecycle.binding[k] == state[k] for k in
                ("surface_id", "incarnation", "owner", "endpoint", "authority_epoch")), "NO_LEASE")
            status = self.lifecycle.vp_perception_call(dict(op="status", session_id=state["session_id"], reason="replay"))
            require(status["complete"] and status["to"] == "ACTIVE" and
                    status["fresh_observation_id"], "STALE_FRAME")
            observation = status["fresh_observation_id"]
            workflow = self.store.load_workflow(lane["site"], lane["workflow"], lane["revision"])["workflow"]
            target = lane.get("next_target")
            if (lane["failed_step_seq"] is not None and not lane["claim"] and target is None
                    and state["t"] < state["T"]):
                target = self._plan(state, observation, workflow["steps"][state["i"]])
            kind = "acquisition" if lane["claim"] else "act"
            return self.lifecycle.run_agent(kind, observation,
                lambda: self._advance(state, observation, workflow, target, session_pin))

    def _plan(self, s, observation, step):
        # R2 control may wait for a concurrent P4 drain. Never hold a P4
        # activity across start/stop, or that drain would wait on itself.
        scope = dict(caller="agent", agent_instance=s["owner"], session_id=s["session_id"],
                     authority_epoch=s["authority_epoch"], incarnation=s["incarnation"])
        def control(op, args):
            request = dict(schema="vp.control.req/v1", request_id=f"{s['goal_id']}-{step['seq']}-{op}",
                           op="vp_vision_" + op, context=scope, args=args, dry_run=False)
            outcome = self.vision.execute(request)
            require(outcome.ok, outcome.code)
            return outcome.result
        goal = (f"Find the control for step {step['seq']} of {s['loop']['workflow']} "
                f"in goal {s['goal_id']} to satisfy {step['verify']['predicate']['bindings'][0]['value']}.")
        started = control("start", dict(session_id=s["session_id"], goal=goal, steps=1))
        try:
            require(started["observation_id"] == observation, "STALE_FRAME")
            analysis = self.vision.analyze(observation)
            require(analysis["observation_id"] == observation, "STALE_FRAME")
            return self.target_for(deepcopy(step), analysis)
        finally:
            control("stop", {})

    def _advance(self, s, observation, workflow, target, session_pin):
        require(self._session(s) == session_pin, "identity_mismatch")
        lane = s["loop"]
        step = workflow["steps"][s["i"]]
        if lane["claim"]:
            if s["phase"] == "JUDGING":
                workflow["steps"][s["i"]]["target"] = lane["claim"]["target"]
                return self._mark(s, workflow)
            if s["phase"] in ("SCRIPT_RUNNING", "EXECUTING"):
                self.goal.transition(s, "uncertain")
                s = self.goal.status()
            if s["phase"] == "RECONCILING":
                # Restart during the only authorized read pass.
                return self._handoff(s)
            return self._reconcile(s, workflow)
        if lane["failed_step_seq"] is not None:
            require(lane["failed_step_seq"] == step["seq"], "STALE_FRAME")
            if s["t"] >= s["T"] or target is None:
                return self._stop(s)
            step["target"] = deepcopy(target)
        revision = replay.pin(workflow)
        attempt = revision + ".a1"
        doc = replay.projection(workflow, s["i"])
        with self.open_owner(doc) as owner:
            self._admit_owner(owner, doc, workflow, s)
            evidence = observation
            if target is not None:
                owner.check("step", doc, owner.scope, owner.authority)
                start = monotonic_ns()
                sensed = owner.sense(start + 5_000_000_000)
                require(sensed["frame"]["surface_id"] == s["surface_id"] and
                        start <= sensed["capture_start"] <= monotonic_ns(), "STALE_FRAME")
                evidence = replay.pin([observation, sensed["frame"], sensed["capture_start"]])
                if s["phase"] == "REJECTED":
                    return self.goal.transition(s, "retry", revision=revision, attempt=attempt,
                                                evidence=evidence, target=target)
                if s["alternative"] is not None and s["i"]:
                    prefix = workflow["steps"][s["i"] - 1]["verify"]["predicate"]
                    try:
                        replay.semantics(owner, doc, [prefix], start, start + 5_000_000_000, s["surface_id"])
                    except Exception:
                        return self._stop(s, reason="RESTORE_MISMATCH")
            risk = self.authorize(s, deepcopy(step))
            require(risk in ("observar", "leer", "preparar", "enviar", "publicar",
                             "destructivo", "irreversible"), "NO_LEASE")
            result = self.goal.transition(s, "claim", revision=revision, target=target,
                                          attempt=attempt, risk=risk, observation=evidence, session_pin=session_pin)
            if result not in ("SCRIPT_RUNNING", "EXECUTING"):
                return result
            flight = self.goal.status()
            require(flight["authority_epoch"] == s["authority_epoch"] and not flight["human"] and
                    flight["phase"] == result, "NO_LEASE")
            owner.backend = replay.DispatchFence(owner.backend, self.goal, flight, self.lifecycle, observation,
                session_check=lambda: self._session(flight) == session_pin)
            try:
                outcome = replay.run_one(owner, doc, s["surface_id"])
            except Exception:
                # The owner may have submitted input before losing its receipt.
                outcome = "UNCERTAIN_OUTCOME"
            if outcome == "FAILED_STEP":
                return self.goal.transition(flight, "failed", seq=step["seq"])
            if outcome == "HUMAN_NEEDED":
                return self._handoff(flight)
            if outcome == "VERIFIED":
                return self._verify(flight, owner, doc, workflow)
            return self.goal.transition(flight, "uncertain")

    def _admit_owner(self, owner, doc, workflow, s):
        replay.check_owner(owner, doc)
        replay.validate(workflow, owner)
        owned = owner.backend.call("ownership")
        require(owned["capsules"] == [s["checkpoint"]["capsule_id"]], "NO_LEASE")

    def _handoff(self, s):
        result = self.goal.transition(s, "handoff")
        self.goal.deliver_handoff(self.lifecycle.broker)
        return result

    def _stop(self, s, reason="ALL_REJECTED"):
        self.goal.transition(s, "stop", reason=reason)
        return self._restore(self.goal.status())

    def _verify(self, s, owner, doc, workflow, *, window=None, witness=None):
        start = monotonic_ns()
        deadline = start + 5_000_000_000 if window is None else window
        predicates = [doc["steps"][0]["verify"]["predicate"], *doc["oracle"]["postconditions"]]
        if witness is not None:
            predicates.insert(0, witness)
        try:
            require(self._session(s) == s["loop"]["claim"]["session_pin"], "identity_mismatch")
            evidence = replay.semantics(owner, doc, predicates, start, deadline, s["surface_id"])
            require(self._session(s) == s["loop"]["claim"]["session_pin"], "identity_mismatch")
        except Exception:
            if window is not None:
                return None
            return self.goal.transition(s, "uncertain")
        claim = s["loop"]["claim"]
        if claim["attempt"] is None:
            return self.goal.transition(s, "verified", evidence=evidence)
        quality, post_state = self.assess(s, evidence)
        judge = dict(attempt=claim["attempt"], post_state=post_state,
                     post_digest=quality.scene_digest, score=quality.score,
                     predicates=[p.frame_not_blank for p in quality.predicates])
        result = self.goal.transition(s, "assessed", judge=judge, evidence=evidence)
        if result != "JUDGING":
            return result
        return self._mark(self.goal.status(), workflow)

    def _mark(self, state, workflow):
        self.goal.transition(state, "mark")
        claimed = self.goal.status()
        require(claimed["phase"] == "JUDGING" and not claimed["human"] and
                claimed["authority_epoch"] == state["authority_epoch"] and
                self._session(claimed) == claimed["loop"]["claim"]["session_pin"], "NO_LEASE")
        receipt = self.checkpoint(claimed)
        return self._finish_mark(claimed, workflow, receipt)

    def finish_checkpoint(self, recovered_receipt):
        """Trusted readback after a lost mark reply; never send another mark."""
        with self._exclusive():
            s = self.goal.status()
            require(s["phase"] == "JUDGING" and not s["human"], "NO_LEASE")
            lane = s["loop"]
            doc = self.store.load_workflow(lane["site"], lane["workflow"], lane["revision"])["workflow"]
            doc["steps"][s["i"]]["target"] = lane["claim"]["target"]
            return self._finish_mark(s, doc, recovered_receipt)

    def _finish_mark(self, state, workflow, receipt):
        claim = state["loop"]["claim"]
        require(self._session(state) == claim["session_pin"] and replay.pin(workflow) == claim["revision"],
                "identity_mismatch")
        fields = dict(checkpoint=receipt, evidence=claim["evidence"])
        # Validate the backend receipt before making the immutable draft visible.
        # Saving content is idempotent if the process dies before the ledger commit.
        apply_loop(deepcopy(state), "verified", fields)
        self.store.save_workflow(canonical_bytes(workflow))
        return self.goal.transition(state, "verified", **fields)

    def _reconcile(self, s, workflow):
        self.goal.transition(s, "read")
        reading = self.goal.status()
        claim = reading["loop"]["claim"]
        if claim["epoch"] != reading["authority_epoch"] or self._session(reading) != claim["session_pin"]:
            return self._handoff(reading)
        if claim["target"] is not None:
            workflow["steps"][s["i"]]["target"] = claim["target"]
        require(replay.pin(workflow) == claim["revision"], "SEAL_MISMATCH")
        doc = replay.projection(workflow, s["i"])
        deadline = monotonic_ns() + 30_000_000_000
        try:
            return self._read_effect(reading, workflow, doc, deadline)
        except Exception:
            # The read claim survives owner failures. No further pass or write
            # is admissible if evidence or authority was lost.
            if self.goal.status() != reading:
                raise
            return self._handoff(reading)

    def _read_effect(self, reading, workflow, doc, deadline):
        claim = reading["loop"]["claim"]
        step = deepcopy(workflow["steps"][reading["i"]])
        with self.open_owner(doc) as owner:
            owner.backend = replay.ReadOnlyBackend(owner.backend)
            self._admit_owner(owner, doc, workflow, reading)
            owner.validate_corpus()
            owner.check("step", doc, owner.scope, owner.authority)
            if claim["risk"] not in ("observar", "leer", "preparar"):
                return self._handoff(reading)
            witness = self.effect_witness(reading, step) if self.effect_witness else None
            if witness is not None:
                result = self._verify(reading, owner, doc, workflow, window=deadline, witness=witness)
                if result is not None:
                    return result
            if self.no_effect:
                predicate = self.no_effect(reading, step)
                if predicate is not None:
                    try:
                        replay.semantics(owner, doc, [predicate], monotonic_ns(), deadline, reading["surface_id"])
                    except Exception:
                        pass
                    else:
                        require(self._session(reading) == claim["session_pin"], "identity_mismatch")
                        return self.goal.transition(reading, "failed", seq=claim["seq"])
        return self._handoff(reading)


__all__ = ["LoopGoal", "ProjectedOwners", "ReplayLoop"]
