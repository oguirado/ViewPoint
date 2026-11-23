"""Agent-side checks against the broker's folded plan."""

from .projection import MicrotaskError, check_supersedes, check_tm


def strip_tm(intent):
    """Copy the base intent for a validator that does not accept the tm extension."""
    return {key: value for key, value in intent.items() if key != "tm"}


def refusal_code(errors):
    return errors[0].split(":", 1)[0] if errors else None


def validate_planned_intent(intent, projection, *, validate_base, retry_of=None):
    """Return ordered `CODE: detail` errors without changing the projection.

    Supply the lane's base intent validator. A caller initiating a retry
    supplies the failed microtask ID as retry_of. A retry already declared
    in the broker plan must retain that same supersedes link.
    """
    if not isinstance(intent, dict):
        return ["CONTRACT: expected an intent object"]
    errors = []
    try:
        _check_planned_intent(intent, projection, retry_of)
    except MicrotaskError as exc:
        errors.append(str(exc))
    errors.extend(f"CONTRACT: {error}" for error in validate_base(strip_tm(intent)))
    return errors


def _check_planned_intent(intent, projection, retry_of):
    tm = intent.get("tm")
    if not isinstance(tm, dict) or tm.get("microtask_id") is None:
        raise MicrotaskError("MICROTASK_MISSING", "planned intent requires tm.microtask_id")
    check_tm(intent)
    mid, tid, supersedes = tm["microtask_id"], tm["task_id"], tm["supersedes"]
    if retry_of is not None and (retry_of != supersedes or retry_of == mid):
        raise MicrotaskError("TM_BAD_SUPERSEDE", mid)
    check_supersedes(projection, mid, tid, supersedes)

    task = projection.tasks.get(tid)
    if task is None:
        raise MicrotaskError("PLAN_MISSING", tid)
    step = projection.mt.get(mid)
    if step is None:
        raise MicrotaskError("MICROTASK_UNKNOWN", mid)
    if supersedes != step["supersedes"]:
        raise MicrotaskError("TM_BAD_SUPERSEDE", mid)
    if tm["after"] != step["after"]:
        raise MicrotaskError("TM_ID_MISMATCH", "intent dependencies differ from the plan")
    event_id = intent.get("event_id")
    if isinstance(event_id, str) and projection.by_event.get(event_id, mid) != mid:
        raise MicrotaskError("TM_ID_REUSED", event_id)
    bound_event = step["event_id"]
    if task["closed"] or step["state"] not in ("pending", "queued") or (
        bound_event is not None and bound_event != event_id
    ):
        raise MicrotaskError("MICROTASK_NOT_OPEN", mid)
    for other in projection.mt.values():
        if (other["agent_id"] == intent["agent_id"] and other["microtask_id"] != mid
                and (other["state"] in ("queued", "acting")
                     or other["state"] == "blocked"
                     and other["prev_state"] in ("queued", "acting")
                     and str(other["reason"]).startswith("DEP_FAILED:"))):
            raise MicrotaskError("MICROTASK_BUSY", other["microtask_id"])
    if not all(projection.satisfied(dep) for dep in step["after"]):
        raise MicrotaskError("TM_DEP_UNMET", mid)
