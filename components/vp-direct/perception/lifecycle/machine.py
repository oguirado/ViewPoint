"""Trusted owner of one agent-exclusive P3 stream on a P2 surface.

The shared source (recording, display and delivery) belongs to P2. Supply an
agent demand on that source and a separate, pull-driven P3 reader. No shared
producer is stopped by this owner. All agent jobs enter through run_agent and
finish their callbacks synchronously. drain_agent must also obtain an actual
ack from any delegated workers. runtime inspects the live app and the
agent-exclusive process/charge scope. qualified checks effective capture
capability for the pinned binding. These are provisioned ports, never wire args.
"""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import Condition, Lock, Thread, local
from time import monotonic_ns
from typing import TYPE_CHECKING

from bus.broker import BrokerError
from .demand import RecoverableDemandCoordinator
from control.core.control import Handler, Outcome
from schemas.validation import canonical_bytes, validate_contract

if TYPE_CHECKING:
    from perception.stream import Observation


class LifecycleError(RuntimeError):
    """A refusal. A claimed external delivery, if present, remains owned by P2."""

    def __init__(self, code, *, work=None):
        super().__init__(code)
        self.code = code
        self.work = work


@dataclass(frozen=True)
class RuntimeEvidence:
    """Observed app incarnation, agent-exclusive processes and resident charge."""
    app_incarnation: str
    exclusive_pids: tuple
    resident_bytes: int


@dataclass(frozen=True)
class TransitionEvidence:
    """Evidence captured by P4 at the committed transition boundary."""

    binding: dict
    session_id: str
    prior_state: dict
    broker_records: tuple
    demand_snapshot: dict
    agent_demand: dict
    stream_before: dict
    stream_after: dict
    runtime: RuntimeEvidence
    previous_observation: Observation | None = None
    observation: Observation | None = None
    reap: dict | None = None
    qualified: bool | None = None
    agent_drained: bool | None = None


class _ReapEvidence:
    """Observe the raw backend reply through this owner's P3 capture port."""

    def __init__(self, capture):
        self._capture = capture
        self._observing = local()

    def __getattr__(self, name):
        return getattr(self._capture, name)

    def Reap(self, *args, **kwargs):
        result = self._capture.Reap(*args, **kwargs)
        if hasattr(self._observing, "replies"):
            self._observing.replies.append(deepcopy(result))
        return result

    def observe(self, operation):
        replies = self._observing.replies = []
        try:
            result = operation()
            require(len(replies) <= 1, "HOLD")
            return result, replies[0] if replies else None
        finally:
            del self._observing.replies


ACTIVITIES = frozenset(("acquisition", "analysis", "ocr", "inference", "decision", "act"))
WAKE_NS = 3_000_000_000
_PROVISION = Lock()


def require(condition, code):
    if not condition:
        raise LifecycleError(code)


class PerceptionLifecycle:
    """Own lifecycle transitions and optionally publish their trusted evidence.

    The publication hook runs synchronously under transition ownership after
    broker commit. A lost acknowledgement keeps the receipt available for an
    idempotent retry. Goal-bound owners commit the publication evidence with
    the goal transition so a replacement can complete the same publication.
    After a login notice, confirm_privacy_clear must read current, content-free
    A2 metadata for the binding and explicitly confirm that it is clear.
    """

    def __init__(self, broker, session_id, binding, demand, agent_demand, stream, *,
                 qualified, drain_agent, runtime, clock=monotonic_ns, recover_goal=None, recover_observation=None,
                 confirm_quiescent=None, on_transition=None, confirm_privacy_clear=None, privacy_timeline=None):
        validate_contract("vp.surface_binding/v1", binding)
        validate_contract("vp.demand/v1", agent_demand)
        self.binding = deepcopy(binding)
        self.session_id = session_id
        self.surface = binding["surface_id"]
        require(agent_demand["surface_id"] == self.surface == demand.snapshot()["surface_id"]
                and agent_demand["consumer"] == binding["owner"]
                and agent_demand["interest"] == "VERIFIED", "identity_mismatch")
        self.broker, self.demand, self.stream = broker, demand, stream
        self._agent_demand = deepcopy(agent_demand)
        self._qualified, self._drain_agent, self._runtime = qualified, drain_agent, runtime
        self._clock = clock
        self._transition = Lock()
        self._admission = Condition()
        self._admitting = False
        self._privacy_held = False
        self._confirm_privacy_clear = confirm_privacy_clear
        self._privacy_timeline = privacy_timeline
        self._confirmed_privacy_boundary = (None, None)
        self._login_notice_cursor = (None, 0, None)
        self._running = 0
        self._goal = None
        self._work_goals = {}
        self._delivered = set()
        self._subscription = None
        self._observation = None
        self._late = None
        self._failures = 0
        self._generation = 0
        self._rest_start = None
        self._rest_reap = None
        self._issued_ids = set()
        self._state = "OFF"
        self._settled = False
        self._on_transition = on_transition
        self._pending_publication = None
        self._publication_key = None
        self._reap_evidence = None
        require(on_transition is None or callable(on_transition), "HOLD")
        require(broker.registered_binding(session_id, self.surface) == self.binding, "identity_mismatch")
        # A replacement owner must reconcile the previous stream, work claims
        # and goal ledger. A fresh Python instance cannot prove their closure.
        with _PROVISION:
            handback = self._recover_privacy(confirm_privacy_clear)
            self._authority()
            durable = isinstance(demand, RecoverableDemandCoordinator)
            require(not durable or not demand.lifecycle_bound, "HOLD")
            if on_transition is not None:
                # P3 exposes scrub readback in status(), but drops the raw
                # reply. Observe the owned backend without changing P3.
                capture = stream._capture
                self._reap_evidence = (capture if isinstance(capture, _ReapEvidence)
                                       else _ReapEvidence(capture))
                stream._capture = self._reap_evidence
            if recover_goal is not None:
                self._recover(recover_goal, recover_observation, confirm_quiescent, handback)
            else:
                self._start_without_goal(handback, recover_observation)
            if durable:
                demand.lifecycle_bound = True
        self._settled = True

    def _start_without_goal(self, handback, recover_observation):
        prior = self.broker.perception_state(self.surface)
        if prior is not None:
            require(handback and prior["authority_epoch"] < self.binding["authority_epoch"], "HOLD")
            if prior["state"] == "ACTIVE":
                # A bare constructor must never discard a goal or its pending
                # publication. Only the old owner can attest a settled pre-goal row.
                _, _, pregoal = self._login_fence(self.broker)
                require(pregoal == sha256(canonical_bytes(prior)).hexdigest()
                        and self.demand.snapshot()["state"] == "idle", "HOLD")
                self._adopt_returned_stream(handback, recover_observation, prior)
                self._issued_ids.add(self._observation.observation_id)
                return
            # A single initial OFF row has never issued a flight or a publication.
            require(prior["state"] == "OFF" and prior["stream_generation"] == 0, "HOLD")
            with self._broker_rows() as rows:
                require(sum(row.get("perception", {}).get("surface_id") == self.surface
                            for row in rows) == 1, "HOLD")
        self._verify_off()
        self._record("OFF", self.demand.snapshot())

    def _recover(self, goal, recover_observation, confirm_quiescent, handback):
        """Join the goal and broker at an acknowledged boundary. Never re-dispatch on replay."""
        require(isinstance(self.demand, RecoverableDemandCoordinator), "HOLD")
        ledger = goal.replay()
        require(ledger["phase"] == "READY" and not ledger["human"] and ledger["session_id"] == self.session_id
                and all(ledger[k] == self.binding[k] for k in
                        ("surface_id", "endpoint", "owner", "authority_epoch", "incarnation")), "HOLD")
        if (handback and ledger["perception"] == "ACTIVE"
                and handback.observation_id in ledger["observations"] and ledger["observation"]
                and ledger["generation"] >= handback.generation
                and ledger.get("active_boundary", {}).get("record", {}).get("authority_epoch")
                    != self.binding["authority_epoch"]):
            self._recover_handback(goal, ledger, handback, recover_observation)
            return
        if ledger.get("pending_rest"):
            self._recover_drain(goal, ledger, recover_observation)
            return
        boundary = ledger.get("rest_boundary")
        current_record = self.broker.perception_state(self.surface)
        if ledger["perception"] == "WAKING":
            # An issued flight may still own a producer. Only its independent
            # owner can confirm termination. This path never calls stream.wake.
            require(boundary and boundary["source"] == self.demand.source_id
                    and ledger["flight"] == boundary["flight"] + 1 and current_record
                    and (current_record == boundary["record"] or
                         (current_record["state"] == "WAKING" and all(
                             current_record[k] == boundary["record"][k] for k in
                             ("authority_epoch", "incarnation", "session_id", "surface_id", "stream_generation"))))
                    and callable(confirm_quiescent)
                    and confirm_quiescent(ledger["flight"], deepcopy(self.binding)) is True, "HOLD")
            self._goal = goal
            self._last_record = current_record
            self._generation = ledger["generation"]
            self._failures = ledger["wake_failures"]
            self._resume_agent_if_present()
            self._failed(ledger["flight"], boundary["record"]["state"])
            ledger = goal.replay()
            boundary = ledger["rest_boundary"]
            current_record = self._last_record
        ack = ledger.get("active_boundary") if ledger["perception"] == "ACTIVE" else boundary
        require(ack and current_record in (ack["prior"], ack["record"]), "HOLD")
        record = ack["record"]
        require(boundary and record and ledger["perception"] in ("ASLEEP", "OFF", "ACTIVE", "QUARANTINED")
                and record["state"] == ledger["perception"] and record["session_id"] == self.session_id
                and record["authority_epoch"] == ledger["authority_epoch"]
                and record["incarnation"] == ledger["incarnation"]
                and record["stream_generation"] == ledger["generation"]
                and boundary["source"] == self.demand.source_id, "HOLD")
        claims = self.demand.recovery_claims()
        for token, state in ledger["work"].items():
            if state != "settled":
                require(token in ledger.get("work_flights", {})
                        and (token in claims or state == "delivered"), "HOLD")
        self._generation = ledger["generation"]
        self._failures = ledger["wake_failures"]
        self._goal = goal
        self._last_record = record
        self._state = record["state"]
        if self._state == "ACTIVE":
            require(ack["flight"] == ledger["flight"] and ack["stream"] == self.stream.status()
                    and callable(recover_observation)
                    and self._qualified(deepcopy(self.binding)) is True, "HOLD")
            obs = recover_observation()
            self._prove_observation(obs, self._generation - 1)
            require(obs.observation_id == ledger["observation"] and
                    obs.stream_generation == self._generation, "STALE_FRAME")
            self._resource()
            self._authority()
            self._observation = obs
        else:
            require(boundary["flight"] == ledger["flight"] and boundary["stream"] == self.stream.status()
                    and not any(d["demand_id"] == self._agent_demand["demand_id"]
                                for d in self.demand.snapshot()["demands"]), "HOLD")
            if self._state == "OFF":
                self._verify_off()
        publication_key = self._unacknowledged_publication(ack, current_record)
        row = None
        if current_record != record:
            row = self._record(self._state, self.demand.snapshot(), record=record)
        if publication_key is not None:
            self._recover_publication(ack, row, publication_key)
        if self._state == "ACTIVE":
            self._subscription = self.demand.resume_subscription(self._agent_demand)
            self._subscription.__enter__()
            self._admitting = True
        self._issued_ids = set(ledger["observations"])
        self.demand.recover_unbound_claim(ledger)
        self._work_goals = {w: (goal, token) for token, w in claims.items()
                            if ledger["work"].get(token) in ("admitted", "consumed", "delivered")}

    def _recover_handback(self, goal, ledger, handback, recover_observation):
        # U3a and U4a already agreed on this epoch and observation. Independently
        # verify the surviving P3 token before admitting work against that goal.
        record = self.broker.perception_state(self.surface)
        taken, _ = self.broker.history(self.session_id)
        require(record and taken and record["session_id"] == self.session_id
                and record["incarnation"] == self.binding["incarnation"]
                and record["state"] in ("ASLEEP", "ACTIVE")
                and taken["lease"]["epoch_before"] <= record["authority_epoch"] <= ledger["authority_epoch"]
                and ledger["checkpoint"]["authority_epoch"] <= taken["lease"]["epoch_before"]
                and (record["state"] == "ACTIVE" or self._on_transition is None)
                and not ledger.get("pending_rest") and not ledger["u"] and not ledger["q"]
                and all(state == "settled" for state in ledger["work"].values())
                and not self.demand.recovery_claims(), "HOLD")
        boundary = ledger.get("active_boundary")
        if boundary:
            # A handback cannot acknowledge a missing P7 publication.
            require("publication" in boundary and (boundary["publication"] is None or
                    self.demand.publication_acknowledged(sha256(canonical_bytes(boundary)).hexdigest())),
                    "HOLD")
        else:
            require(ledger["flight"] == 0 and not ledger.get("rest_boundary"), "HOLD")
        self._adopt_returned_stream(handback, recover_observation, record, goal=goal, ledger=ledger)
        self._goal = goal
        self._issued_ids = set(ledger["observations"]) | {self._observation.observation_id}
        self._failures = ledger["wake_failures"]

    def _adopt_returned_stream(self, handback, recover_observation, record, *, goal=None, ledger=None):
        require(callable(recover_observation) and self._qualified(deepcopy(self.binding)) is True, "HOLD")
        before = self.stream.status()["fresh_observation_id"]
        obs = recover_observation()
        floor = max(record["stream_generation"], handback.generation)
        require(obs and obs.stream_generation >= floor, "STALE_FRAME")
        # U3a may return a CDP or AT-SPI observation. A distinct P3 witness
        # must be issued after clear A2, or already be the one U4a reconciled.
        expected = ledger["observation"] if ledger else handback.observation_id
        if expected != handback.observation_id:
            require(obs.observation_id == expected and obs.stream_generation == ledger["generation"], "STALE_FRAME")
        elif obs.observation_id == handback.observation_id:
            require(obs.stream_generation == handback.generation, "STALE_FRAME")
        else:
            require(obs.observation_id != before and (not ledger or obs.observation_id not in ledger["observations"]),
                    "STALE_FRAME")
        require(self._privacy_boundary(self.broker) == self._confirmed_privacy_boundary, "HUMAN_NEEDED")
        self._authority()
        self._prove_observation(obs, floor - 1)
        self._resource()
        self._authority()
        require(self._privacy_boundary(self.broker) == self._confirmed_privacy_boundary, "HUMAN_NEEDED")
        self._generation = obs.stream_generation
        if isinstance(self.demand, RecoverableDemandCoordinator):
            self._resume_agent_if_present()
        if self._subscription is None:
            self._subscription = self.demand.register(self._agent_demand)
            self._subscription.__enter__()
        try:
            if goal is not None and expected != obs.observation_id:
                require(goal.observed(obs.observation_id, obs.stream_generation) == "READY", "HOLD")
            if record["authority_epoch"] == self.binding["authority_epoch"]:
                require(record["state"] == "ACTIVE" and record["stream_generation"] == self._generation, "HOLD")
                self._last_record = record
                self._state = "ACTIVE"
            else:
                self._record("ACTIVE", self.demand.snapshot())
        except BaseException:
            self._cancel_demand()
            raise
        self._observation = obs
        self._admitting = True

    def _recover_drain(self, goal, ledger, recover_observation):
        intent = ledger["pending_rest"]
        record = self.broker.perception_state(self.surface)
        require(not intent.get("publication_required") or self._on_transition is not None, "HOLD")
        require(record == intent["record"] and intent["source"] == self.demand.source_id
                and intent["flight"] == ledger["flight"], "HOLD")
        old = intent["stream"]
        current = self.stream.status()
        require(current["stream_generation"] == old["stream_generation"]
                and (current == old or
                     (current["state"] == "ASLEEP" and current["ring_id"] == old["ring_id"]
                      and current["reserved_bytes"] == old["reserved_bytes"]) or
                     (current["state"] == "OFF" and current["last_scrubbed_ring"] == old["ring_id"])),
                "HOLD")
        obs = None
        if intent["retiring"] is not None:
            require(callable(recover_observation), "HOLD")
            obs = recover_observation()
            require(obs is not None and obs.observation_id == ledger["observation"] and
                    sha256(obs.token.nonce.encode()).hexdigest() == intent["retiring"], "STALE_FRAME")
        self._observation = obs
        self._rest_start = old, obs
        self._last_record, self._state = record, record["state"]
        self._generation = ledger["generation"]
        self._issued_ids = set(ledger["observations"])
        self._failures = ledger["wake_failures"]
        self._goal = goal
        self._resume_agent_if_present()
        self._rest(intent["target"])
        self.demand.recover_unbound_claim(goal.replay())

    def _resume_agent_if_present(self):
        if any(d["demand_id"] == self._agent_demand["demand_id"] and d["active"]
               for d in self.demand.snapshot()["demands"]):
            self._subscription = self.demand.resume_subscription(self._agent_demand)
            self._subscription.__enter__()

    def reconcile_verified(self, token, confirm_delivery):
        """Settle a recovered claim only on independent delivery/disposition evidence.

        The callback is a trusted readback, never a sender. A durable delivery
        acknowledgement survives a crash on either side of P2 credit release.
        """
        with self._transition:
            self._authority()
            require(isinstance(self.demand, RecoverableDemandCoordinator) and self._goal, "HOLD")
            ledger = self._goal.replay()
            require(token in ledger.get("work_flights", {}), "NO_LEASE")
            if ledger["work"][token] == "settled":
                return "SETTLED"
            work = self.demand.recovery_claims().get(token)
            if ledger["work"][token] != "delivered":
                require(work is not None and confirm_delivery(work, ledger["work_flights"][token]) is True, "HOLD")
                self._goal.acknowledge_delivery(token)
            if work is not None:
                self.demand.complete(work)
                self._work_goals.pop(work, None)
            return self._goal.settle_external_work(token)

    def _authority(self, broker=None):
        broker = self.broker if broker is None else broker
        if isinstance(self.demand, RecoverableDemandCoordinator):
            self.demand.check_owner()
        current = broker.binding(self.surface)
        require(current == self.binding, "identity_mismatch")
        lease = broker.lease(self.session_id)
        require(lease == dict(state="AGENT", holder=current["owner"],
                              authority_epoch=current["authority_epoch"]), "NO_LEASE")

    def _resource(self):
        sample = self._runtime()
        require(isinstance(sample, RuntimeEvidence)
                and sample.app_incarnation == self.binding["incarnation"]
                and type(sample.exclusive_pids) is tuple
                and all(type(pid) is int and pid > 0 for pid in sample.exclusive_pids)
                and type(sample.resident_bytes) is int and sample.resident_bytes >= 0, "HOLD")
        return sample

    def _verify_off(self):
        sample = self._resource()
        status = self.stream.status()
        require(not sample.exclusive_pids and status["state"] == "OFF"
                and status["reserved_bytes"] == 0 and status["ring_id"] is None
                and status["fresh_observation_id"] is None, "HOLD")

    def _record_data(self, state, snapshot):
        obligations = set(self.binding["obligations"])
        obligations.update(row["demand_id"] for row in snapshot["demands"])
        return dict(schema="vp.perception_state/v1", session_id=self.session_id, surface_id=self.surface,
                    state=state, incarnation=self.binding["incarnation"], authority_epoch=self.binding["authority_epoch"],
                    stream_generation=self._generation, since_mono_ns=self._clock(), obligations=sorted(obligations))

    def _record(self, state, snapshot, *, record=None):
        row = self.broker.perception_transition(record if record is not None else self._record_data(state, snapshot))
        self._last_record = row["perception"]
        self._state = state
        return row

    def _observe_reap(self, operation):
        if self._reap_evidence is None:
            return operation(), None
        return self._reap_evidence.observe(operation)

    def _prepare_transition(self, receipt, *, prior, records, snapshot, old, final, sample,
                            old_observation=None, reap=None, waking=False, drained=False):
        if self._on_transition is None:
            return
        proof = TransitionEvidence(
            binding=self.binding, session_id=self.session_id, prior_state=prior, broker_records=records,
            demand_snapshot=snapshot, agent_demand=self._agent_demand, stream_before=old,
            stream_after=final, runtime=sample, previous_observation=old_observation,
            observation=self._observation, reap=reap, qualified=True if waking else None,
            agent_drained=True if drained else None,
        )
        self._pending_publication = deepcopy((receipt, proof))

    def _bind_publication(self, boundary):
        # Committed atomically with the goal charge, before the final broker row.
        boundary["publication"] = None
        if self._pending_publication is not None:
            receipt, proof = self._pending_publication
            evidence = asdict(proof)
            if proof.observation is not None:
                # A live token must come back from the surviving producer.
                evidence["observation"] = sha256(canonical_bytes(evidence["observation"])).hexdigest()
            boundary["publication"] = dict(receipt=receipt, evidence=evidence)
            self._publication_key = sha256(canonical_bytes(boundary)).hexdigest()

    def _finish_transition(self, row):
        if self._pending_publication is not None:
            receipt, proof = self._pending_publication
            self._pending_publication = receipt, replace(proof, broker_records=(*proof.broker_records, row))
        return self._acknowledge_transition()

    def _unacknowledged_publication(self, boundary, current_record):
        if boundary.get("publication") is None:
            # Older boundaries cannot prove whether a configured publisher ran.
            require(self._on_transition is None or "publication" in boundary, "HOLD")
            return None
        key = sha256(canonical_bytes(boundary)).hexdigest()
        if self.demand.publication_acknowledged(key):
            require(current_record == boundary["record"], "HOLD")
            return None
        require(self._on_transition is not None, "HOLD")
        return key

    def _recover_publication(self, boundary, row, key):
        from perception.stream import Observation, StreamToken

        saved = boundary["publication"]
        fields = dict(saved["evidence"])
        require(fields["binding"] == self.binding and fields["session_id"] == self.session_id
                and fields["agent_demand"] == self._agent_demand, "HOLD")
        if fields["observation"] is not None:
            require(self._observation is not None and fields["observation"] ==
                    sha256(canonical_bytes(asdict(self._observation))).hexdigest(), "STALE_FRAME")
            fields["observation"] = self._observation
        previous = fields["previous_observation"]
        if previous is not None:
            fields["previous_observation"] = Observation(**dict(previous, token=StreamToken(**previous["token"])))
        runtime = fields["runtime"]
        fields["runtime"] = RuntimeEvidence(**dict(runtime, exclusive_pids=tuple(runtime["exclusive_pids"])))
        fields["broker_records"] = tuple(fields["broker_records"])
        if row is None:
            row = self._committed_broker_row(boundary["record"])
        self._publication_key = key
        self._pending_publication = saved["receipt"], TransitionEvidence(**fields)
        self._finish_transition(row)

    @contextmanager
    def _broker_rows(self, broker=None, *, start=0):
        broker = self.broker if broker is None else broker
        # Read the descriptor that the owner verified at open under its lock.
        with broker._mutex:
            file = broker._journal.file
            offset = file.tell()

            def rows(history):
                for line in history:
                    require(line.endswith(b"\n"), "HOLD")
                    yield json.loads(line)

            try:
                with open(file.fileno(), "rb", closefd=False) as history:
                    history.seek(start)
                    yield rows(history)
            finally:
                file.seek(offset)

    def _committed_broker_row(self, record):
        # P7's replay key includes the sequence and timestamp of the original row.
        with self._broker_rows() as rows:
            require(self.broker.perception_state(self.surface) == record, "HOLD")
            matched = None
            for event in rows:
                if event.get("perception") == record:
                    require(matched is None, "HOLD")
                    matched = event
            require(matched is not None, "HOLD")
            return matched

    def _last_login_notice(self, broker):
        # Every live admission consults this append-only journal. Revisit
        # only its new rows, and restart at byte zero on broker replacement.
        with broker._mutex:
            journal, start, notice = self._login_notice_cursor
            if journal is not broker._journal:
                start, notice = 0, None
            with self._broker_rows(broker, start=start) as rows:
                for event in rows:
                    if (event["rec"] == "HUMAN_NEEDED" and event.get("reason") == "login"
                            and event.get("session_id") == self.session_id):
                        notice = event["seq"]
                self._login_notice_cursor = (broker._journal, broker._journal.file.tell(), notice)
            return notice

    def _login_fence(self, broker, *, remember=False, gap=None, pregoal=None):
        # HUMAN_NEEDED requires an actual goal. Store a session-scoped fence
        # beside the broker journal for pre-goal holds and recorder-only gaps.
        # Both clocks matter: a broker sequence has no defined ordering against
        # a U2a timestamp. Compare each only to its own handback evidence.
        with broker._mutex:
            journal = broker._journal
            # A broker opened with a relative runtime path may outlive a chdir.
            journal_path = Path(f"/proc/self/fd/{journal.file.fileno()}").resolve(strict=True)
            path = journal_path.with_name("lifecycle-privacy.sqlite")
            writing = remember or gap is not None
            if not writing:
                try:
                    path.stat()
                except FileNotFoundError:
                    return None, None, None
            mode = "rwc" if writing else "ro"
            with closing(sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True)) as db, db:
                if writing:
                    db.execute("CREATE TABLE IF NOT EXISTS login_hold "
                               "(session TEXT PRIMARY KEY, seq INTEGER, gap_ns INTEGER, pregoal TEXT)")
                    db.execute("INSERT OR IGNORE INTO login_hold(session) VALUES (?)", (self.session_id,))
                if remember:
                    db.execute("UPDATE login_hold SET seq=?, pregoal=? WHERE session=?",
                               (journal.seq, pregoal, self.session_id))
                if gap is not None:
                    db.execute("UPDATE login_hold SET gap_ns=? WHERE session=? AND (gap_ns IS NULL OR gap_ns < ?)",
                               (gap, self.session_id, gap))
                row = db.execute("SELECT seq, gap_ns, pregoal FROM login_hold WHERE session=?", (self.session_id,)).fetchone()
                return row if row is not None else (None, None, None)

    def _privacy_boundary(self, broker):
        gap = None
        if self._privacy_timeline is not None:
            # The provisioned recorder belongs to this surface and capsule.
            try:
                timeline = self._privacy_timeline()
                validate_contract("rec.timeline/v1", timeline)
                # Closing a recording gap advances the freshness boundary.
                # Only handback and clear A2 can authorize admission again.
                gap = max((event["mono_ns"] for event in timeline["events"]
                           if event["incarnation"] == self.binding["incarnation"]
                           and event.get("op") in ("privacy_gap_start", "privacy_gap_end")), default=None)
            except Exception as exc:
                raise LifecycleError("HUMAN_NEEDED") from exc
        with broker._mutex:
            broker.lease(self.session_id)  # Verify the journal owner before reading its descriptor.
            notice = self._last_login_notice(broker)
            try:
                marker, gap, _ = self._login_fence(broker, gap=gap)
            except (sqlite3.Error, OSError) as exc:
                raise LifecycleError("HUMAN_NEEDED") from exc
        return max((seq for seq in (notice, marker) if seq is not None), default=None), gap

    def _recover_privacy(self, confirm_clear, broker=None):
        broker = self.broker if broker is None else broker
        boundary = self._privacy_boundary(broker)
        notice, gap = boundary
        if notice is None and gap is None:
            return None
        _, returned = broker.history(self.session_id)
        require(returned and (notice is None or returned["seq"] > notice)
                and returned["lease"]["epoch_after"] == self.binding["authority_epoch"]
                and callable(confirm_clear), "HUMAN_NEEDED")
        try:
            fresh = broker.returned_observation(self.session_id)
        except BrokerError as exc:
            raise LifecycleError("HUMAN_NEEDED") from exc
        require(fresh.session_id == self.session_id and fresh.surface_id == self.surface
                and fresh.incarnation == self.binding["incarnation"]
                and fresh.authority_epoch == self.binding["authority_epoch"]
                and (gap is None or fresh.captured_mono_ns > gap), "HUMAN_NEEDED")
        try:
            clear = confirm_clear(deepcopy(self.binding))
        except Exception:
            raise LifecycleError("HUMAN_NEEDED") from None
        require(clear is True, "HUMAN_NEEDED")
        self._authority(broker)
        require(self._privacy_boundary(broker) == boundary, "HUMAN_NEEDED")
        self._confirmed_privacy_boundary = boundary
        return fresh

    def _acknowledge_transition(self):
        pending = self._pending_publication
        if pending is not None:
            try:
                self._authority()
                receipt, proof = pending
                require(proof.broker_records, "HOLD")
                committed = proof.broker_records[-1]["perception"]
                require(committed["state"] == receipt["to"]
                        and self.broker.perception_state(self.surface) == committed, "HOLD")
                self._on_transition(*deepcopy(pending))
                if self._publication_key is not None:
                    self.demand.acknowledge_publication(self._publication_key)
            except Exception as exc:
                raise LifecycleError("HOLD") from exc
            self._pending_publication = None
            self._publication_key = None
        with self._admission:
            self._admitting = self._state == "ACTIVE" and not self._privacy_held
            self._settled = True
        return None if pending is None else deepcopy(pending[0])

    def retry_transition_publication(self):
        """Retry only the pending receipt, with its original transition evidence."""
        with self._transition:
            require(self._pending_publication is not None, "HOLD")
            return self._acknowledge_transition()

    def _receipt(self, before, after, snapshot, *, acks, drained=(), retired=(), stopped=(), scrubbed=None):
        status = self.stream.status()
        sample = self._resource()
        if after == "ACTIVE":
            require(self._observation is not None and status["state"] == "ACTIVE"
                    and status["fresh_observation_id"] == self._observation.observation_id, "STALE_FRAME")
        if after == "OFF":
            require(not sample.exclusive_pids and status["state"] == "OFF"
                    and status["ring_id"] is None and status["reserved_bytes"] == 0, "HOLD")
        receipt = {
            "schema": "vp.transition_receipt/v1", "from": before, "to": after,
            "demand_snapshot_sha256": sha256(canonical_bytes(snapshot)).hexdigest(),
            "drained": list(drained), "retained": [row["demand_id"] for row in snapshot["demands"]],
            "producers_stopped": list(stopped), "tokens_retired": list(retired),
            "scrub": {"buffers": [scrubbed] if scrubbed else [], "canary_ok": True if scrubbed else None},
            "fresh_observation_id": self._observation.observation_id if after == "ACTIVE" else None,
            "charges": dict(resident_bytes=sample.resident_bytes, reserved_bytes=status["reserved_bytes"]),
            "acks": list(acks), "complete": True, "not_verified": [],
        }
        validate_contract("vp.transition_receipt/v1", receipt)
        return receipt, status, sample

    def bind_goal(self, machine):
        """Attach U4a's durable goal to the already acquired observation."""
        with self._transition:
            self._require_privacy_clear()
            self._authority()
            s = machine.replay()
            require(self._state == "ACTIVE" and self._settled and self._goal is None
                    and s["session_id"] == self.session_id and s["perception"] == "ACTIVE"
                    and not s["human"] and s["phase"] == "READY"
                    and all(s[key] == self.binding[key] for key in
                            ("owner", "incarnation", "authority_epoch", "endpoint", "surface_id"))
                    and s["generation"] == self._generation
                    and s["observation"] == self._observation.observation_id, "NO_LEASE")
            with self._admission:
                self._require_privacy_clear()
                self._goal = machine

    def reconnect_broker(self, replacement, goal):
        """At an acknowledged rest boundary, reconnect surviving P2/P3 to replayed owners.

        No claim, queue or token is reconstructed. Losing the P2/P3 owner or
        either side of an unfinished transition requires independent reconciliation.
        """
        with self._transition:
            # The old broker must be closed. Read durable evidence from the
            # replacement in _recover_privacy before inspecting any resources.
            with self._admission:
                require(not self._privacy_held, "HUMAN_NEEDED")
            try:
                self.broker.perception_state(self.surface)
            except BrokerError as exc:
                require(str(exc) == "NO_LEASE", "HOLD")
            else:
                raise LifecycleError("HOLD")
            candidate = replacement.registered_binding(self.session_id, self.surface)
            require(all(candidate[key] == self.binding[key] for key in ("owner", "incarnation", "endpoint")),
                    "identity_mismatch")
            try:
                self._recover_privacy(self._confirm_privacy_clear, replacement)
            except LifecycleError as exc:
                if exc.code == "HUMAN_NEEDED":
                    with self._admission:
                        self._privacy_held = True
                        self._admitting = False
                raise
            require(self._settled and self._state in ("ASLEEP", "OFF")
                    and self._goal is not None and not self._work_goals, "HOLD")
            require(replacement.perception_state(self.surface) == self._last_record
                    and replacement.binding(self.surface) == self.binding, "identity_mismatch")
            replayed = goal.replay()
            require(replayed == self._goal.status() and replayed["perception"] == self._state
                    and replayed["generation"] == self._generation and not replayed["human"]
                    and replayed["phase"] == "READY", "HOLD")
            require(replacement.lease(self.session_id) == dict(state="AGENT", holder=self.binding["owner"],
                    authority_epoch=self.binding["authority_epoch"]), "NO_LEASE")
            self._status()
            self.broker, self._goal = replacement, goal

    def hold_for_privacy(self, *, before_notice=False):
        """Fence this owner after a bound privacy refusal.

        An admitted observer may call this without waiting for its own drain.
        The hold grants no human lease and makes no stream or sleep claim.
        """
        with self._admission, self.broker._mutex:
            try:
                if self.broker.lease(self.session_id)["state"] == "HUMAN":
                    # S5 can complete the transfer inside the capture callback.
                    # Preserve its refusal and only record evidence for the
                    # epoch that actually relinquished this session.
                    taken, _ = self.broker.history(self.session_id)
                    if (self._privacy_held or not taken
                            or taken["lease"]["epoch_before"] != self.binding["authority_epoch"]):
                        return
                    before_notice = True
                else:
                    self._authority()
                if before_notice or self._goal is None:
                    pregoal = None
                    if self._goal is None and self._settled and self._pending_publication is None:
                        pregoal = sha256(canonical_bytes(self._last_record)).hexdigest()
                    self._login_fence(self.broker, remember=True, pregoal=pregoal)
                else:
                    notice = self._last_login_notice(self.broker)
                    _, returned = self.broker.history(self.session_id)
                    if notice is None or (returned is not None and returned["seq"] > notice):
                        self.broker.human_needed(
                            self.binding["owner"], self.session_id, self._goal.goal_id, "login")
            finally:
                self._privacy_held = True
                self._admitting = False

    def _require_privacy_clear(self):
        with self._admission:
            if not self._privacy_held and self._privacy_boundary(self.broker) != self._confirmed_privacy_boundary:
                self._privacy_held = True
                self._admitting = False
            require(not self._privacy_held, "HUMAN_NEEDED")

    def run_agent(self, kind, observation_id, perform):
        """Hold admission through the last attributable side effect of perform()."""
        with self._admission:
            self._require_privacy_clear()
            require(self._admitting and kind in ACTIVITIES, "NO_LEASE")
            self._authority()
            require(observation_id == self._observation.observation_id, "STALE_FRAME")
            if self._goal is not None:
                goal = self._goal.status()
                require(not goal["human"] and not goal["q"] and not goal.get("pending_rest")
                        and goal["perception"] == "ACTIVE"
                        and goal["phase"] != "HARD_STOP_ESCALATED", "NO_LEASE")
            self._running += 1
        try:
            return perform()
        finally:
            with self._admission:
                self._running -= 1
                self._admission.notify_all()

    def read_frame(self, observation_id):
        """Read the admitted RAM observation under the same drain fence as analysis."""
        def read():
            frame = self.stream.read(self._observation.token, observation_id)
            self._require_privacy_clear()
            return frame

        return self.run_agent("acquisition", observation_id, read)

    def _cancel_demand(self):
        if self._subscription is not None:
            subscription, self._subscription = self._subscription, None
            subscription.__exit__(None, None, None)

    def _demand_drained(self):
        snapshot = self.demand.snapshot()
        if snapshot["state"] == "drain_failed":
            self.demand.release()
            snapshot = self.demand.snapshot()
        require(snapshot["state"] not in ("draining", "drain_failed")
                and all(row["demand_id"] != self._agent_demand["demand_id"]
                        for row in snapshot["demands"]), "HOLD")
        return snapshot

    def _rest(self, target):
        before = self._state
        prior = self._last_record
        require(before in ("ACTIVE", "ASLEEP", "OFF"), "HOLD")
        require(target != "ASLEEP" or before != "OFF", "HOLD")
        if self._goal is not None and isinstance(self.demand, RecoverableDemandCoordinator):
            ledger = self._goal.status()
            intent = ledger.get("pending_rest")
            if intent is None:
                obs = self._observation
                intent = dict(source=self.demand.source_id, target=target, record=self._last_record,
                              publication_required=self._on_transition is not None,
                              flight=ledger["flight"], stream=self.stream.status(),
                              retiring=sha256(obs.token.nonce.encode()).hexdigest() if obs else None)
            require(intent["target"] == target, "HOLD")
            self._goal.begin_rest(intent)
        with self._admission:
            self._admitting = False
            self._settled = False
            self._admission.notify_all()
            self._admission.wait_for(lambda: self._running == 0)
        # Failed or absent acknowledgements leave the admission fence closed.
        self._cancel_demand()
        require(self._drain_agent(deepcopy(self.binding)) is True, "HOLD")
        self._demand_drained()
        current = self.stream.status()
        if self._rest_start is None:
            self._rest_start = current, self._observation
        old, obs = self._rest_start
        if target == "OFF":
            _, reap = self._observe_reap(self.stream.close)
            if reap is not None:
                self._rest_reap = reap
            self._verify_off()
        else:
            if current["state"] == "ACTIVE":
                require(obs is not None, "STALE_FRAME")
                self.stream.pause(obs.token)
            paused = self.stream.status()
            require(paused["state"] == "ASLEEP" and paused["fresh_observation_id"] is None
                    and old["ring_id"] and paused["ring_id"] == old["ring_id"]
                    and paused["reserved_bytes"] == old["reserved_bytes"] > 0, "HOLD")
        self._authority()
        final = self.stream.status()
        if target == "OFF" and old["ring_id"]:
            require(final["last_scrubbed_ring"] == old["ring_id"], "HOLD")
        self._observation = None
        retired = [obs.token.nonce] if obs is not None else []
        snapshot = self._demand_drained()
        receipt, status, sample = self._receipt(
            before, target, snapshot, acks=["agent-drain", "demand", "stream", "app"],
            drained=[self._agent_demand["demand_id"]], retired=retired,
            stopped=[old["ring_id"]] if old["state"] == "ACTIVE" else [],
            scrubbed=old["ring_id"] if target == "OFF" else None,
        )
        record = self._record_data(target, snapshot)
        self._prepare_transition(receipt, prior=prior, records=(), snapshot=snapshot, old=old,
                                 final=status, sample=sample, old_observation=obs,
                                 reap=self._rest_reap, drained=True)
        if self._goal is not None:
            boundary = None
            if isinstance(self.demand, RecoverableDemandCoordinator):
                boundary = dict(source=self.demand.source_id, flight=self._goal.status()["flight"],
                                stream=final, prior=self._last_record, record=record,
                                demand_snapshot_sha256=receipt["demand_snapshot_sha256"])
                self._bind_publication(boundary)
            result = self._goal.slept(receipt, boundary=boundary, generation=self._generation,
                                      authority_epoch=self.binding["authority_epoch"])
            require(result == target, "HOLD")
        row = self._record(target, snapshot, record=record)
        self._rest_reap = None
        self._rest_start = None
        self._finish_transition(row)
        return receipt

    def _prove_observation(self, obs, prior_generation):
        status = self.stream.status()
        require(obs is not None and status["state"] == "ACTIVE" and status["ring_id"]
                and status["reserved_bytes"] > 0 and obs.observation_id
                and obs.observation_id not in self._issued_ids
                and obs.observation_id == status["fresh_observation_id"]
                and obs.stream_generation == status["stream_generation"] > prior_generation
                and all(getattr(obs.token, key) == self.binding[key] for key in
                        ("incarnation", "endpoint", "surface_id", "owner", "authority_epoch")),
                "STALE_FRAME")
        self._require_privacy_clear()
        pixels = self.stream.read(obs.token, obs.observation_id)
        require(pixels and sha256(pixels).hexdigest() == obs.frame_sha256, "STALE_FRAME")

    def _failed(self, flight, previous):
        """An unproved cleanup holds the fence. Never acknowledge a late acquisition."""
        self._observation = None
        self._failures += 1
        try:
            self._cancel_demand()
            self._demand_drained()
            self.stream.close()
            self._verify_off()
            rest = "QUARANTINED" if self._failures >= 2 else previous
            snapshot = self.demand.snapshot()
            if self._goal is not None and previous != "QUARANTINED":
                rest = "QUARANTINED" if self._failures >= 2 else "ASLEEP"
                record = self._record_data(rest, snapshot)
                boundary = None
                if isinstance(self.demand, RecoverableDemandCoordinator):
                    boundary = dict(source=self.demand.source_id, flight=flight, publication=None,
                                    stream=self.stream.status(), prior=self._last_record, record=record,
                                    demand_snapshot_sha256=sha256(canonical_bytes(snapshot)).hexdigest())
                disposition = self._goal.failed_wake(flight, boundary=boundary)
                require(disposition == rest, "HOLD")
            else:
                record = self._record_data(rest, snapshot)
        except Exception:
            self._record("QUARANTINED", self.demand.snapshot())
            raise
        self._record(rest, snapshot, record=record)
        self._settled = True

    def _capture(self, prior_generation, deadline):
        self._authority()
        require(self._qualified(deepcopy(self.binding)) is True, "NO_LEASE")
        require(self._clock() < deadline, "HOLD")
        self._require_privacy_clear()
        obs, reap = self._observe_reap(self.stream.wake)
        self._prove_observation(obs, prior_generation)
        self._authority()
        require(self._qualified(deepcopy(self.binding)) is True, "NO_LEASE")
        self._resource()
        require(self._clock() < deadline, "HOLD")
        return obs, reap

    def _wake(self, work=None, *, reopen=False):
        self._require_privacy_clear()
        previous = self._state
        require(self._settled and (previous in ("OFF", "ASLEEP", "ACTIVE") or
                                   (previous == "QUARANTINED" and reopen)), "HOLD")
        flight = None
        if reopen:
            require(self._goal is not None and self._goal.status()["perception"] == "QUARANTINED", "HOLD")
            self._verify_off()
            flight = self._goal.status()["flight"]
        elif self._goal is not None:
            token = None
            if work is not None:
                token = (self.demand.work_token(work) if isinstance(self.demand, RecoverableDemandCoordinator)
                         else sha256(canonical_bytes(asdict(work))).hexdigest())
                flight = self._goal.claim_external_wake(token)
                self._work_goals[work] = (self._goal, token)
            else:
                flight = self._goal.request_wake()
            if type(flight) is not int:
                require(flight == "ACTIVE" and previous == "ACTIVE", "NO_LEASE")
        if previous == "ACTIVE":
            return self._status()
        prior_generation = self._generation
        prior = self._last_record
        old = self.stream.status()
        prior_ring = old["ring_id"]
        deadline = self._clock() + WAKE_NS
        self._settled = False
        waking = self._record("WAKING", self.demand.snapshot())
        future = Future()
        acknowledged = False

        def capture():
            try:
                future.set_result(self._capture(prior_generation, deadline))
            except BaseException as exc:
                future.set_exception(exc)
            # The requester timed out with the stream still in this thread.
            # Only this owner can settle it. No new flight can overtake it.
            with self._transition:
                if self._late is future:
                    try:
                        self._failed(flight, previous)
                    finally:
                        self._late = None

        try:
            subscription = self.demand.register(self._agent_demand)
            subscription.__enter__()
            self._subscription = subscription
            Thread(target=capture, daemon=True, name="perception-wake").start()
            try:
                obs, reap = future.result(timeout=max(0, deadline - self._clock()) / 1e9)
            except FutureTimeout:
                if not future.done():
                    self._late = future
                raise LifecycleError("HOLD") from None
            require(self._clock() < deadline, "HOLD")
            self._authority()
            require(not prior_ring or self.stream.status()["last_scrubbed_ring"] == prior_ring, "HOLD")
            self._generation = obs.stream_generation
            self._observation = obs
            self._issued_ids.add(obs.observation_id)
            snapshot = self.demand.snapshot()
            receipt, status, sample = self._receipt(
                "WAKING", "ACTIVE", snapshot, acks=["identity", "capability", "stream"], scrubbed=prior_ring)
            self._require_privacy_clear()
            require(self._clock() < deadline, "HOLD")
            record = self._record_data("ACTIVE", snapshot)
            self._prepare_transition(receipt, prior=prior, records=(waking,), snapshot=snapshot, old=old,
                                     final=status, sample=sample, reap=reap, waking=True)
            if self._goal is not None:
                boundary = (dict(prior=self._last_record, record=record, flight=flight,
                                 stream=self.stream.status())
                            if isinstance(self.demand, RecoverableDemandCoordinator) else None)
                if boundary is not None:
                    self._bind_publication(boundary)
                require(self._goal.awakened(receipt, binding=self.binding, generation=self._generation,
                                            flight=flight, reopen=reopen, boundary=boundary) == "ACTIVE", "HOLD")
                acknowledged = True
            active = self._record("ACTIVE", snapshot, record=record)
            if reopen:
                self._failures = 0
        except Exception:
            if self._late is None and not acknowledged and self._pending_publication is None:
                self._generation = prior_generation
                self._failed(flight, previous)
            raise
        self._finish_transition(active)
        return receipt

    def reopen_after_quarantine(self):
        """Trusted re-base. U4a must authorize and charge the fresh stream generation."""
        with self._transition:
            self._authority()
            require(self._state == "QUARANTINED", "HOLD")
            return self._wake(reopen=True)

    def wake_for_verified(self):
        """Consume one P2 claim. The caller completes delivery even on a refused wake.

        Returns (work, receipt) or None when nothing was published. A refusal
        carries its uncompleted claim for explicit disposition. Use complete_verified
        after delivery or disposition. Nothing in this path resends work.
        """
        with self._transition:
            self._require_privacy_clear()
            self._authority()
            require(self._settled and self._state != "QUARANTINED", "HOLD")
            if self._goal is not None and isinstance(self.demand, RecoverableDemandCoordinator):
                claim = self.demand.claim_for_wake(self._goal.goal_id)
            else:
                claim = self.demand.claim("VERIFIED", budget=1, timeout=0)
            if not claim:
                return None
            work, = claim
            try:
                require(work.consumer != self.binding["owner"], "NO_LEASE")
                return work, self._wake(work)
            except Exception as exc:
                raise LifecycleError(getattr(exc, "code", "HOLD"), work=work) from exc

    def complete_verified(self, work):
        """Acknowledge the actual delivery, then settle its goal ledger entry."""
        with self._transition:
            admission = self._work_goals.get(work)
            if work not in self._delivered:
                if admission is not None and isinstance(self.demand, RecoverableDemandCoordinator):
                    goal, token = admission
                    goal.acknowledge_delivery(token)
                self.demand.complete(work)
                if admission is not None:
                    self._delivered.add(work)
            if admission is not None:
                goal, token = admission
                goal.settle_external_work(token)
                self._delivered.remove(work)
                del self._work_goals[work]

    def _status(self):
        # Callers have checked their live (or replacement) broker. A local
        # hold may also have arrived since that check.
        with self._admission:
            require(not self._privacy_held, "HUMAN_NEEDED")
        require(self._settled and self._late is None, "HOLD")
        if self._state == "QUARANTINED":
            receipt, _, _ = self._receipt(self._state, self._state, self.demand.snapshot(), acks=["state-readback"])
            receipt.update(complete=False, not_verified=["stream reopen required"])
            return receipt
        if self._state == "ACTIVE":
            current = self.stream.status()
            require(current["state"] == "ACTIVE" and
                    current["fresh_observation_id"] == self._observation.observation_id, "STALE_FRAME")
        elif self._state == "OFF":
            self._verify_off()
        elif self._state == "ASLEEP":
            stream = self.stream.status()
            require(stream["state"] in ("ASLEEP", "OFF") and stream["fresh_observation_id"] is None, "HOLD")
        receipt, _, _ = self._receipt(self._state, self._state, self.demand.snapshot(), acks=["state-readback"])
        return receipt

    def control_handler(self):
        """C1 handler for a provisioned binding. Registration is owned by the registry."""
        return Handler(execute=self._control, preview=self._control)

    def _control(self, request):
        validate_contract("vp.control.req/v1", request)
        ctx, args = request["context"], request["args"]
        validate_contract("vp_perception_call.args/v1", args)
        if (request["op"] != "vp_perception_call" or ctx.get("session_id") != self.session_id
                or ctx.get("incarnation") != self.binding["incarnation"]
                or ctx.get("authority_epoch") != self.binding["authority_epoch"]
                or (ctx["caller"] == "agent" and ctx["agent_instance"] != self.binding["owner"])):
            return Outcome(False, "NO_LEASE")
        try:
            if request["dry_run"] and args["op"] != "status":
                result = self.vp_perception_call(dict(args, op="status"))
                result.update(complete=False, not_verified=["dry-run"])
            else:
                result = self.vp_perception_call(args)
        except (LifecycleError, BrokerError) as exc:
            return Outcome(False, str(exc))
        return Outcome(True, "OK", result)

    def vp_perception_call(self, args):
        """Dispatch validated operation arguments after the control lane admits them."""
        validate_contract("vp_perception_call.args/v1", args)
        with self._transition:
            require(args.get("surface_id", self.surface) == self.surface
                    and args.get("session_id", self.session_id) == self.session_id, "identity_mismatch")
            op = args["op"]
            if op in ("wake", "status"):
                self._require_privacy_clear()
            self._authority()
            require(self._pending_publication is None, "HOLD")
            if op == "status":
                return self._status()
            if op == "wake":
                return self._wake()
            require(self._late is None, "HOLD")
            target = "ASLEEP" if op == "sleep" else "OFF"
            if target == self._state and self._settled:
                self._require_privacy_clear()
                return self._status()
            return self._rest(target)
