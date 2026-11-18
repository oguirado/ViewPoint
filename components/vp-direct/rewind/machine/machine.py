"""Trusted in-process U4a controller. Agent transports must not expose owner hooks.

Each proposed edit is a durable, single-use dispatch claim. The controller's
executor owns execute/judge/mark and the broker owns restore and its readback.
No external effect runs in a database transaction or on journal replay.
"""
from contextlib import closing, contextmanager
import json
import sqlite3

from schemas.validation import canonical_bytes, validate_contract
from . import transitions as tr
from rewind.blender_rw1 import CheckpointResult


class GoalMachine:
    def __init__(self, path, goal_id):
        """Reopen a previously created goal. The path is in owner-controlled storage."""
        self.path = path
        self.goal_id = goal_id

    @contextmanager
    def _db(self, *, write=False):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @classmethod
    def create(cls, path, *, goal_id, session_id, binding, checkpoint, score,
               steps, duplicates, epsilon, action_quantum, observation_id, generation):
        """Admit a scored p0; attempt and rewind budgets come from the pinned receipt.

        epsilon is in U5 score units (integer ppm). action_quantum is a caller-
        selected positive decimal string, pinned for this goal.
        """
        tr.require(isinstance(action_quantum, str), "BAD_INTENT")
        data = dict(goal_id=goal_id, session_id=session_id, binding=binding, checkpoint=checkpoint,
                    score=score, steps=steps, duplicates=duplicates, epsilon=epsilon,
                    action_quantum=action_quantum, observation_id=observation_id, generation=generation)
        machine = cls(path, goal_id)
        if isinstance(checkpoint, CheckpointResult):
            data.update(checkpoint=checkpoint.receipt, evidence=checkpoint.goal_evidence())
        state = tr.create(**data)
        with machine._db(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS goals (id TEXT PRIMARY KEY, state BLOB NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS events
                       (seq INTEGER PRIMARY KEY, goal TEXT NOT NULL, op TEXT NOT NULL, data BLOB NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS goal_events ON events(goal,seq)")
            try:
                db.execute("INSERT INTO goals VALUES (?,?)", (goal_id, canonical_bytes(state)))
            except sqlite3.IntegrityError as exc:
                raise tr.Refused("REQUEST_ID_CONFLICT") from exc
            db.execute("INSERT INTO events(goal,op,data) VALUES (?,?,?)",
                       (goal_id, "create", canonical_bytes(data)))
        return machine

    def _load(self, db):
        row = db.execute("SELECT state FROM goals WHERE id=?", (self.goal_id,)).fetchone()
        tr.require(row is not None, "NO_LEASE")
        return json.loads(row[0])

    def status(self):
        with self._db() as db:
            return self._load(db)

    def replay(self):
        """Recompute the goal from committed inputs in one consistent read snapshot."""
        with self._db() as db:
            current = self._load(db)
            state = None
            for op, raw in db.execute("SELECT op,data FROM events WHERE goal=? ORDER BY seq",
                                      (self.goal_id,)):
                data = json.loads(raw)
                if op == "create" and state is None:
                    state = tr.create(**data)
                else:
                    tr.apply(state, op, data)
            tr.require(state == current, "RESTORE_MISMATCH")
            return state

    def _record(self, op, *, return_state=False, **data):
        with self._db(write=True) as db:
            s = self._load(db)
            tr.require(s["phase"] != tr.STOP, "NO_LEASE")
            result = tr.apply(s, op, data)
            db.execute("UPDATE goals SET state=? WHERE id=?", (canonical_bytes(s), self.goal_id))
            db.execute("INSERT INTO events(goal,op,data) VALUES (?,?,?)",
                       (self.goal_id, op, canonical_bytes(data)))
        return (result, s) if return_state else result

    def propose(self, attempt, action, *, observation, touches=(), supersedes=None, retry=False):
        """EXECUTING grants one dispatch of the entire batch. A retry from REJECTED
        selects a novel alternative and reserves a rewind before any new dispatch.
        """
        tr.require(isinstance(touches, (tuple, list)) and all(map(tr.identifier, touches)), "BAD_INTENT")
        touches = sorted(set(touches))
        policy = self.status()
        tr.require(policy["phase"] != tr.STOP, "NO_LEASE")
        branch = tr.action_hash(action, touches, policy["quantum"])
        return self._record("proposal", attempt=attempt, branch=branch, touches=touches,
                            observation=observation, supersedes=supersedes, retry=retry)

    def judge(self, attempt, post_state_sha256, quality):
        """Pass the trusted U5 QualityResult for the independently pinned checkpoint capture."""
        return self._record("judge", attempt=attempt, post_state=post_state_sha256,
                            post_digest=quality.scene_digest, score=quality.score,
                            predicates=[p.frame_not_blank for p in quality.predicates])

    def commit(self, attempt, checkpoint_receipt, *, revised_steps=None):
        data = {}
        if isinstance(checkpoint_receipt, CheckpointResult):
            data["evidence"] = checkpoint_receipt.goal_evidence()
            checkpoint_receipt = checkpoint_receipt.receipt
        return self._record("commit", attempt=attempt, receipt=checkpoint_receipt,
                            revised_steps=revised_steps, **data)

    def abandon(self):
        """Trusted executor: no novel alternative, or a definitely ended attempt with no mark."""
        return self._record("abandon")

    def run_restore(self, broker_restore):
        """Consume a claim before calling the trusted broker port exactly once.

        A process death leaves restore_claim set. Resolve that claim using the
        broker's independently recovered receipt or failure, never by resending.
        """
        claim, state = self._record("claim_restore", return_state=True)
        try:
            receipt = broker_restore(claim, state)
        except Exception as exc:
            error = "RESTORE_TIMEOUT" if isinstance(exc, TimeoutError) else getattr(exc, "code", "UNCERTAIN_OUTCOME")
            return self.finish_restore(claim, error=error)
        return self.finish_restore(claim, receipt=receipt)

    def finish_restore(self, claim, *, receipt=None, error=None):
        # A malformed backend payload must still close the outstanding claim.
        data = {}
        try:
            if isinstance(receipt, CheckpointResult):
                data["evidence"] = receipt.goal_evidence()
                receipt = receipt.receipt
            canonical_bytes(receipt)
        except (AttributeError, TypeError, ValueError, KeyError, tr.ValidationError, OverflowError, RecursionError):
            receipt, error = None, "RESTORE_MISMATCH"
        if error is not None and not tr.identifier(error):
            error = "UNCERTAIN_OUTCOME"
        return self._record("restored", claim=claim, receipt=receipt, error=error, **data)

    def observed(self, observation_id, generation):
        return self._record("observe", observation=observation_id, generation=generation)

    def verified_step(self, step, observation_id):
        """Trusted verifier hook after qualified post-state evidence."""
        return self._record("verified", step=step, observation=observation_id)

    def uncertain(self, attempt):
        return self._record("uncertain", attempt=attempt)

    def reconcile(self, attempt, effect, observation_id, generation):
        """Only the trusted observation-only reconciler may report an effect."""
        return self._record("reconcile", attempt=attempt, effect=effect,
                            observation=observation_id, generation=generation)

    def human_lease(self, broker_record, *, observation_id=None, generation=None):
        """Consume U3a's take/return record after S4 fences or revalidates the lease."""
        tr.require(broker_record.get("rec") == "control" and broker_record.get("by") == "broker",
                   "NO_LEASE")
        lease = broker_record["lease"]
        tr.require(broker_record.get("op") == ("take" if lease["state"] == "HUMAN" else "return"),
                   "NO_LEASE")
        return self._record("human", lease=lease, observation=observation_id, generation=generation)

    def begin_rest(self, intent):
        return self._record("begin_rest", intent=intent)

    def slept(self, receipt, *, generation, authority_epoch, external=False, boundary=None):
        """P4 supplies the completed drain receipt after closing agent admissions."""
        validate_contract("vp.transition_receipt/v1", receipt)
        if receipt["from"] == "ASLEEP" and receipt["to"] == "OFF":
            tr.require(receipt["complete"] and receipt["fresh_observation_id"] is None
                       and "stream" in receipt["acks"] and "app" in receipt["acks"], "NO_LEASE")
            return self._record("rest_off", generation=generation, authority_epoch=authority_epoch, boundary=boundary)
        tr.require(receipt["complete"] and receipt["from"] == "ACTIVE" and
                   receipt["to"] in ("ASLEEP", "OFF"), "NO_LEASE")
        tr.require(type(external) is bool, "BAD_INTENT")
        return self._record("sleep", external=external, target=receipt["to"],
                            generation=generation, authority_epoch=authority_epoch, boundary=boundary)

    def claim_external_wake(self, token):
        """Bind a verified delivery to its single acquisition flight in one transaction."""
        return self._record("claim_wake", token=token)

    def admit_external_work(self, token):
        """P2 owner hook after publishing admitted work. Notifications never call this hook."""
        return self._record("admit_work", token=token)

    def acknowledge_delivery(self, token):
        return self._record("delivered", token=token)

    def settle_external_work(self, token):
        return self._record("settle_work", token=token)

    def request_wake(self, *, external_token=None):
        """An integer flight number authorizes one acquisition; a string is a disposition."""
        return self._record("wake", token=external_token)

    def awakened(self, receipt, *, binding, generation, flight, reopen=False, boundary=None):
        """P4 owner hook after revalidating authority and acquiring a new stream token."""
        s = self.status()
        validate_contract("vp.surface_binding/v1", binding)
        validate_contract("vp.transition_receipt/v1", receipt)
        tr.require(all(binding[k] == s[k] for k in
                       ("owner", "incarnation", "authority_epoch", "surface_id", "endpoint")), "NO_LEASE")
        tr.require(receipt["complete"] and receipt["from"] == "WAKING" and receipt["to"] == "ACTIVE" and
                   receipt["fresh_observation_id"], "STALE_FRAME")
        return self._record("awake", observation=receipt["fresh_observation_id"],
                            generation=generation, reopen=reopen, authority_epoch=binding["authority_epoch"],
                            flight=flight, boundary=boundary)

    def failed_wake(self, flight, *, boundary=None):
        return self._record("failed_wake", flight=flight, boundary=boundary)
