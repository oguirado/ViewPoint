"""Qualify passive operations against a continuously armed focus subscription."""

from dataclasses import dataclass
from time import monotonic_ns

from jsonschema import ValidationError

from schemas.validation import validate_contract


class CoverageLost(RuntimeError):
    """The event source could not establish continuous coverage."""


class FocusRefused(RuntimeError):
    def __init__(self, code, reason, *, trace=None):
        super().__init__(reason)
        self.code = code
        self.trace = trace


@dataclass(frozen=True)
class Readback:
    events: tuple[dict, ...]
    continuous: bool


class FocusMonitor:
    """Consume a trusted source with arm(), finish() and context-manager methods.

    The caller supplies the operation's effective qualification. This monitor
    checks its focus effects and does not grant an operation or input lease.
    """

    def __init__(self, source, *, clock=monotonic_ns):
        self._source = source
        self._clock = clock

    def run_passive(self, surface_id, operation, *, qualified):
        if qualified is not True:
            raise FocusRefused("HUMAN_NEEDED", "operation requires an explicit human turn")

        dispatched = False
        try:
            with self._source as source:
                active_xid = source.arm()
                start = self._clock()
                initial = {"kind": "_NET_ACTIVE_WINDOW", "mono_ns": start, "xid": active_xid}
                trace = {
                    "schema": "vp.focus_trace/v1", "surface_id": surface_id,
                    "start_mono_ns": start, "end_mono_ns": start,
                    "continuous": False, "events": [initial],
                }
                validate_contract("vp.focus_trace/v1", trace)
                dispatched = True
                try:
                    result = operation()
                finally:
                    readback = source.finish()
                    trace["end_mono_ns"] = self._clock()
                    trace["continuous"] = readback.continuous
                    trace["events"].extend(readback.events)
                    validate_contract("vp.focus_trace/v1", trace)
                    if not trace["continuous"] or trace["end_mono_ns"] <= start:
                        raise FocusRefused("UNCERTAIN_OUTCOME", "focus coverage is incomplete", trace=trace)
                    if any(
                        event["kind"] in {"FocusIn", "FocusOut"}
                        or (event["kind"] == "_NET_ACTIVE_WINDOW" and event["xid"] != active_xid)
                        for event in readback.events
                    ):
                        raise FocusRefused("UNCERTAIN_OUTCOME", "unsolicited focus transition", trace=trace)
        except (CoverageLost, OSError, ValidationError, KeyError):
            code = "UNCERTAIN_OUTCOME" if dispatched else "HUMAN_NEEDED"
        else:
            return result, trace
        # Raise outside the handler so the validation exception is not attached.
        raise FocusRefused(code, "continuous focus evidence unavailable")
