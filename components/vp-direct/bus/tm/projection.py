"""Project trusted broker records using the vendored tm1 state machine."""

from copy import deepcopy

from jsonschema import ValidationError

from schemas.validation import validate_contract
from vendor.tasklight.model import schema


class MicrotaskError(ValueError):
    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code


def check_tm(record):
    try:
        validate_contract("tm/v1", record["tm"])
    except ValidationError as exc:
        field = ".".join(map(str, exc.absolute_path)) or "tm"
        raise MicrotaskError("TM_BAD_FIELD", field) from exc
    errors = schema.validate_tm(record)
    if errors:
        raise MicrotaskError(*errors[0])


def check_supersedes(model, mid, task_id, supersedes):
    if supersedes is None:
        return
    old = model.mt.get(supersedes)
    if (supersedes == mid or old is None or old["task_id"] != task_id
            or old["state"] not in schema.FAILED or old["superseded_by"] not in (None, mid)):
        raise MicrotaskError("TM_BAD_SUPERSEDE", mid)


class _Projection(schema.Model):
    def feed(self, record):
        if not isinstance(record, dict) or record.get("by") != "broker":
            raise MicrotaskError("NO_LEASE", "expected a record from the broker journal")
        record = self._check_record(record)
        changes = super().feed(record)
        if self.violations:
            error = self.violations[0]
            raise MicrotaskError(error["code"], error["detail"])
        return changes

    def _check_record(self, record):
        kind = record.get("rec")
        if kind == "enqueue":
            self._check_enqueue(record)
            return record
        if kind not in ("auction", "close", "cursor", "judge", "turn", "txn"):
            return record

        eid = self._attempt_event(record) if kind in ("judge", "txn") else record.get("event_id")
        if eid is not None and not isinstance(eid, str):
            raise MicrotaskError("TM_BAD_ID", kind)
        mid = self.by_event.get(eid)
        if kind == "turn" and eid is None:
            unstamped = {"rec": "turn", "agent_id": record.get("agent_id")}
            mid = schema.stamp_record(self, unstamped).get("microtask_id")
        if kind == "close" and mid is None and record.get("verdict") == "QUEUE_FULL":
            # This broker refusal can cite a planned entry that was never enqueued.
            pending = self.mt.get(record.get("microtask_id"))
            if pending and pending["state"] == "pending" and pending["event_id"] is None:
                mid = pending["microtask_id"]
        task_id = self.mt[mid]["task_id"] if mid is not None else None
        self._check_stamp(record, mid, task_id)
        if mid is not None and record.get("agent_id", self.mt[mid]["agent_id"]) != self.mt[mid]["agent_id"]:
            raise MicrotaskError("TM_ID_MISMATCH", kind)
        if kind == "turn" and (eid is not None or record.get("microtask_id") is not None):
            # Explicit event identity takes precedence over the legacy agent-only join.
            return dict(record, microtask_id=mid) if mid is not None else {}
        return record

    @staticmethod
    def _check_stamp(record, mid, task_id):
        if (record.get("microtask_id", mid) != mid or record.get("task_id", task_id) != task_id):
            raise MicrotaskError("TM_ID_MISMATCH", record["rec"])

    def _check_enqueue(self, record):
        if "tm" in record:
            check_tm(record)
        tm = record.get("tm", {})
        mid = tm.get("microtask_id", record.get("microtask_id"))
        parsed = schema.parse_microtask_id(mid)
        if mid is not None and parsed is None:
            raise MicrotaskError("TM_BAD_ID", "enqueue microtask_id")
        self._check_stamp(record, mid, parsed[0] if parsed else None)
        eid = record.get("event_id")
        if not isinstance(eid, str) or not eid:
            raise MicrotaskError("TM_BAD_ID", "enqueue event_id")
        if eid in self.by_event:
            raise MicrotaskError("TM_ID_REUSED", eid)
        existing = self.mt.get(mid)
        if existing is not None:
            if existing["event_id"] is not None or existing["state"] not in ("pending", "blocked"):
                raise MicrotaskError("TM_ID_REUSED", mid)
            # The task record is authoritative for planned dependencies and retry links.
            if tm.get("supersedes") != existing["supersedes"]:
                raise MicrotaskError("TM_BAD_SUPERSEDE", mid)
            if tm and tm["after"] != existing["after"]:
                raise MicrotaskError("TM_ID_MISMATCH", "enqueue dependencies differ from the plan")
            if existing["state"] == "blocked":
                raise MicrotaskError("TM_DEP_UNMET", mid)
        after = existing["after"] if existing is not None else tm.get("after", [])
        if any(dep not in self.mt for dep in after):
            raise MicrotaskError("TM_DEP_UNKNOWN", mid)
        if tm:
            check_supersedes(self, mid, tm["task_id"], tm["supersedes"])

    def _rec_close(self, record, changes):
        if record.get("verdict") == "ACTUATOR_BUSY" and record.get("requeued") is True:
            mid = self.by_event.get(record.get("event_id"))
            if mid is None or self.mt[mid]["state"] != "queued":
                raise MicrotaskError("TM_BAD_TRANSITION", "ACTUATOR_BUSY before dispatch")
            return  # U3a preserves both the queued state and the drop count.
        super()._rec_close(record, changes)

    def admissible(self, mid):
        """Return the vendored tm1 dispatch eligibility result."""
        return schema.admissible(self, mid)


def fold(records):
    """Replay a broker-owned record stream in order, or raise on a refusal.

    The caller supplies records from the broker journal. The producer stamp
    is a consistency check within that trust boundary.
    """
    projection = _Projection()
    for record in records:
        projection.feed(deepcopy(record))
    return projection
