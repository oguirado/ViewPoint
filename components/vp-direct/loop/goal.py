"""R1 events share the U4a transaction, counters and replay order with S4/P4."""
import json

from rewind.machine import GoalMachine
from rewind.machine import transitions as tr
from schemas.validation import canonical_bytes


def apply_loop(s, event, row):
    """Pure fold. Only references to qualified documents/evidence enter the ledger."""
    tr.agent(s)
    if event == "bind":
        tr.require("loop" not in s and s["phase"] == "READY" and s["i"] == 0
                   and s["n"] == row["steps"] and tr.digest(row["revision"]), "BAD_INTENT")
        s["loop"] = dict(site=row["site"], workflow=row["workflow"], revision=row["revision"],
                         failed_step_seq=None, proposed=None, tried=[], claim=None)
        return "READY"

    lane = s["loop"]
    phase = s["phase"]
    before = tr.potential(s)
    if event == "retry":
        tr.require(phase == "REJECTED" and lane["failed_step_seq"] == s["i"] + 1, "NO_LEASE")
        if row["revision"] in lane["tried"] or row["revision"] == lane["revision"]:
            return tr.duplicate(s, "BRANCH_ALREADY_FAILED")
        tr.fresh(s, row["evidence"], s["generation"])
        result = tr.apply(s, "proposal", dict(attempt=row["attempt"], branch=row["revision"],
                          touches=[str(s["i"] + 1)], observation=s["observation"],
                          supersedes=s["supersedes"], retry=True))
        if result == "REWINDING":
            lane["next_target"] = row["target"]
        return result
    if event == "claim":
        tr.require(phase == "READY" and s["i"] < s["n"] and not s["u"]
                   and lane["claim"] is None, "NO_LEASE")
        tr.require(tr.identifier(row["observation"]) and tr.digest(row["revision"]), "BAD_INTENT")
        repair = lane["failed_step_seq"] is not None
        if repair:
            tr.require(lane["failed_step_seq"] == s["i"] + 1, "STALE_FRAME")
            if row["revision"] in lane["tried"] or row["revision"] == lane["revision"]:
                return tr.duplicate(s, "BRANCH_ALREADY_FAILED")
            if s["t"] >= s["T"]:
                tr.cleanup(s, "ALL_REJECTED")
                return "CLEANUP"
            lane.pop("next_target", None)
            lane["tried"].append(row["revision"])
            tr.fresh(s, row["observation"], s["generation"])
            result = tr.apply(s, "proposal", dict(attempt=row["attempt"], branch=row["revision"],
                              touches=[str(s["i"] + 1)], observation=s["observation"],
                              supersedes=s["supersedes"], retry=False))
            if result != "EXECUTING":
                return result
            lane["proposed"] = row["revision"]
        else:
            tr.require(row["revision"] == lane["revision"], "STALE_FRAME")
            s["phase"] = "SCRIPT_RUNNING"
        lane["claim"] = dict(seq=s["i"] + 1, epoch=s["authority_epoch"], risk=row["risk"],
                             revision=row["revision"], session_pin=row["session_pin"], target=row["target"] if repair else None,
                             attempt=row["attempt"] if repair else None)
        return s["phase"]

    if event == "stop":
        tr.require(phase in ("READY", "REJECTED") and not s["u"], "NO_LEASE")
        s["t"] = min(s["T"], s["t"] + 1)
        tr.cleanup(s, row["reason"])
        return "CLEANUP"

    claim = lane["claim"]
    tr.require(claim is not None and claim["seq"] == s["i"] + 1, "NO_LEASE")
    if event == "uncertain":
        tr.require(phase in ("SCRIPT_RUNNING", "EXECUTING"), "NO_LEASE")
        s.update(phase="UNCERTAIN_OUTCOME", u=1)
        return s["phase"]
    if event == "read":
        tr.require(phase == "UNCERTAIN_OUTCOME" and s["u"], "NO_LEASE")
        s["phase"] = "RECONCILING"
        return s["phase"]
    if event == "handoff":
        tr.require(phase in ("RECONCILING", "SCRIPT_RUNNING", "EXECUTING"), "NO_LEASE")
        s.update(t=min(s["T"], s["t"] + 1), u=0, observation=None, phase="HUMAN_NEEDED")
        if s["t"] == s["T"]:
            s.update(phase=tr.STOP, reason="UNCERTAIN_OUTCOME")
        # A handback cannot authorize resending an unresolved step.
        return s["phase"]

    tr.require(claim["epoch"] == s["authority_epoch"] and
               phase in ("SCRIPT_RUNNING", "EXECUTING", "RECONCILING", "JUDGING"), "NO_LEASE")
    s["u"] = 0
    if event == "failed":
        tr.require(row["seq"] == claim["seq"], "BAD_INTENT")
        if phase == "RECONCILING":
            tr.require(claim["risk"] in ("observar", "leer", "preparar"), "NO_LEASE")
        lane.update(failed_step_seq=row["seq"], proposed=None, claim=None)
        if claim["attempt"] is None:
            s["phase"] = "READY"
        else:
            s.update(phase="REJECTED", pending=None, observation=None, supersedes=claim["attempt"])
            if s["t"] == s["T"] or s["r"] == s["B"]:
                tr.cleanup(s, "ALL_REJECTED")
        return "FAILED_STEP"
    if event == "assessed":
        tr.require(claim["attempt"] is not None and phase in ("EXECUTING", "RECONCILING"), "NO_LEASE")
        s["phase"] = "EXECUTING"
        result = tr.apply(s, "judge", row["judge"])
        if result == "JUDGING":
            claim.update(evidence=row["evidence"], mark_claimed=False)
        else:
            lane.update(claim=None, proposed=None)
        return result
    if event == "mark":
        tr.require(phase == "JUDGING" and claim["mark_claimed"] is False, "UNCERTAIN_OUTCOME")
        claim["mark_claimed"] = True
        return "JUDGING"
    tr.require(event == "verified", "BAD_INTENT")
    if claim["attempt"] is not None:
        tr.require(phase == "JUDGING" and claim["mark_claimed"] and
                   claim["evidence"] == row["evidence"], "NO_LEASE")
        tr.apply(s, "commit", dict(attempt=claim["attempt"], receipt=row["checkpoint"],
                                   revised_steps=None))
        lane["revision"] = lane["proposed"]
    else:
        s["phase"] = "READY"
    tr.fresh(s, row["evidence"], s["generation"])
    tr.apply(s, "verified", dict(step=s["i"], observation=s["observation"]))
    lane.update(claim=None, proposed=None, failed_step_seq=None, tried=[])
    tr.require(tr.potential(s) < before, "NON_DESCENDING")
    return "VERIFIED"


class LoopGoal(GoalMachine):
    """Use this instance for the replay loop, S4's goal and P4.bind_goal()."""

    def transition(self, expected, event, **fields):
        with self._db(write=True) as db:
            state = self._load(db)
            tr.require(state["phase"] != tr.STOP and state == expected, "NO_LEASE")
            result = apply_loop(state, event, fields)
            db.execute("UPDATE goals SET state=? WHERE id=?", (canonical_bytes(state), self.goal_id))
            db.execute("INSERT INTO events(goal,op,data) VALUES (?,?,?)",
                       (self.goal_id, "loop." + event, canonical_bytes(fields)))
        return result

    def deliver_handoff(self, broker):
        """Trusted owner recovery for a stopped goal; never grants execution.

        Reserve notification before external delivery. Broker failure after a
        reservation is an uncertain delivery and needs broker-side readback.
        """
        with self._db(write=True) as db:
            state = self._load(db)
            tr.require(state["phase"] in ("HUMAN_NEEDED", tr.STOP), "NO_LEASE")
            event = db.execute("SELECT max(seq) FROM events WHERE goal=?", (self.goal_id,)).fetchone()[0]
            db.execute("""CREATE TABLE IF NOT EXISTS loop_notices
                          (event INTEGER PRIMARY KEY, broker_seq INTEGER)""")
            previous = db.execute("SELECT broker_seq FROM loop_notices WHERE event=?", (event,)).fetchone()
            if previous is not None:
                tr.require(previous[0] is not None, "UNCERTAIN_OUTCOME")
                return previous[0]
            db.execute("INSERT INTO loop_notices(event) VALUES (?)", (event,))
        receipt = broker.human_needed(state["owner"], state["session_id"], self.goal_id, "stuck")
        tr.require(receipt["by"] == "broker" and receipt["rec"] == "HUMAN_NEEDED" and
                   receipt["goal_id"] == self.goal_id and receipt["session_id"] == state["session_id"]
                   and tr.natural(receipt["seq"]) and receipt["seq"] > 0, "UNCERTAIN_OUTCOME")
        with self._db(write=True) as db:
            db.execute("UPDATE loop_notices SET broker_seq=? WHERE event=?", (receipt["seq"], event))
        return receipt["seq"]

    def replay(self):
        with self._db() as db:
            state = None
            for op, raw in db.execute("SELECT op,data FROM events WHERE goal=? ORDER BY seq",
                                      (self.goal_id,)):
                fields = json.loads(raw)
                if op == "create" and state is None:
                    state = tr.create(**fields)
                elif op.startswith("loop."):
                    apply_loop(state, op.removeprefix("loop."), fields)
                else:
                    tr.apply(state, op, fields)
            tr.require(state == self._load(db), "RESTORE_MISMATCH")
            return state
