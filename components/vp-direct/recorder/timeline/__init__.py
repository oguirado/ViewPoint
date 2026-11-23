"""Join recording feeds into the frozen, redacted rec.timeline/v1 contract."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import re
from threading import Lock

from jsonschema import ValidationError
from schemas.validation import validate_contract


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
_RAM_REFERENCE = re.compile(r"(?:ram:)?[A-Za-z0-9][A-Za-z0-9_.-]*")
_DEMO_ACTIONS = {
    "click": "click",
    "dblclick": "double_click",
    "drag": "drag",
    "key": "key",
    "scroll": "scroll",
    "text": "type",
    "stop": None,
}
_GAP_SOURCES = {"human": "human", "sleep": "channel", "privacy": "channel"}


class TimelineError(ValueError):
    """The input cannot be safely associated with this recording."""


@dataclass(frozen=True)
class TimelinePosition:
    mono_ns: int
    capsule_id: str
    incarnation: str


def _identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise TimelineError("recording identifiers must be opaque")


def _timestamp(value):
    if type(value) is not int or value < 0:
        raise TimelineError("invalid recording timestamp")


def _validate(contract, value):
    try:
        validate_contract(contract, value)
    except ValidationError:
        # ValidationError includes the rejected input, which may contain raw text.
        raise TimelineError(f"invalid {contract} record") from None


def _text_fields(text):
    if text is None:
        return {}
    if not isinstance(text, str):
        raise TimelineError("text must be Unicode")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise TimelineError("invalid Unicode recording text") from None
    return {"text_len": len(encoded), "text_sha256": sha256(encoded).hexdigest()}


class TimelineRecorder:
    """A capsule incarnation's timeline and its verified recording demand.

    Feed positions must use the same monotonic clock. Deliver authoritative gap
    starts before acquisition and gap ends before resuming it. A row arriving
    after a gap is also checked against the recorded intervals. Feed adapters
    share a position to correlate evidence and retain separate source rows.

    register(demand) supplies the coordinator's admission context. The RAM port
    must attest that the exact descriptor belongs to this position in live RAM.
    It receives only metadata and must not call back into this recorder.
    """

    def __init__(self, *, surface_id, capsule_id, incarnation, register, verify_ram_frame):
        for identifier in (surface_id, capsule_id, incarnation):
            _identifier(identifier)
        self._surface_id = surface_id
        self._capsule_id = capsule_id
        self._incarnation = incarnation
        self._register = register
        self._verify_ram_frame = verify_ram_frame
        self._lock = Lock()
        self._rows = []
        self._open_gaps = {}
        self._closed_gaps = []
        self._last_gap_ns = None
        self._state = "new"

    @contextmanager
    def recording(self, demand):
        demand = deepcopy(demand)
        _validate("vp.demand/v1", demand)
        if demand["surface_id"] != self._surface_id or demand["interest"] != "VERIFIED":
            raise TimelineError("recording demand must be verified and match the bound surface")
        with self._lock:
            if self._state != "new":
                raise TimelineError("recording demand is single-use")
            self._state = "registering"
        try:
            with self._register(demand):
                with self._lock:
                    self._state = "active"
                try:
                    yield self
                finally:
                    with self._lock:
                        self._state = "sealed"
        finally:
            with self._lock:
                self._state = "sealed"

    def snapshot(self):
        with self._lock:
            timeline = {
                "schema": "rec.timeline/v1",
                "events": deepcopy(sorted(self._rows, key=lambda row: row["mono_ns"])),
            }
        _validate("rec.timeline/v1", timeline)
        return timeline

    def set_gap(self, position, reason, *, active):
        """Record a content-free boundary for a human, sleep, or privacy interval."""
        if reason not in _GAP_SOURCES or type(active) is not bool:
            raise TimelineError("invalid gap transition")
        with self._lock:
            self._check_position(position)
            when = position.mono_ns
            if self._last_gap_ns is not None and when < self._last_gap_ns:
                raise TimelineError("gap transitions must be chronological")
            if active == (reason in self._open_gaps):
                return False
            if active:
                self._open_gaps[reason] = when
                self._rows = [
                    row for row in self._rows
                    if row.get("gap") or (
                        row["mono_ns"] < when and
                        ("frame" not in row or row["frame"]["captured_mono_ns"] < when)
                    )
                ]
            else:
                self._closed_gaps.append((self._open_gaps.pop(reason), when))
            self._last_gap_ns = when
            edge = "start" if active else "end"
            self._append(position, _GAP_SOURCES[reason], {"gap": True, "op": f"{reason}_gap_{edge}"})
            return True

    def add_demo(self, position, event, *, text_sha256=None, frame=None):
        """Project a DemoEvent with optional RAM capture and a counted-text digest."""
        def fields():
            _timestamp(event["mono_ns"])
            if event["mono_ns"] != position.mono_ns:
                raise TimelineError("DemoEvent timestamp disagrees with its position")
            if event.get("before") or event.get("after"):
                raise TimelineError("legacy disk-backed DemoEvent frames are refused")
            kind = event["kind"]
            if kind not in _DEMO_ACTIONS:
                raise TimelineError("unsupported DemoEvent kind")
            row = {"op": kind}
            action = _DEMO_ACTIONS[kind]
            if kind == "click" and event.get("b") == 3:
                action = "right_click"
            if action is not None:
                row["action"] = action
            if text_sha256 is not None:
                if kind != "text" or "text_len" not in event:
                    raise TimelineError("DemoEvent digest has no matching text count")
                row.update(text_len=event["text_len"], text_sha256=text_sha256)
            elif kind == "text":
                # The legacy source emits only a count. An incomplete text action
                # would be unsafe to compile as a replay step.
                row.pop("action")
            return self._with_frame(position, row, frame)

        return self._record(position, "demo", fields)

    def add_channel(self, position, request, reply):
        """Project a request and its receipt paired by the trusted channel feed."""
        def fields():
            _validate("vp.channel.req/v1", request)
            _validate("vp.channel.reply/v1", reply)
            if (request["capsule_id"], request["incarnation"]) != (self._capsule_id, self._incarnation):
                raise TimelineError("channel request disagrees with its recording binding")
            row = {"op": request["op"]}
            if reply["ok"] and not request["dry_run"] and "action" in request:
                row["action"] = request["action"]
            for field in ("value_len", "value_sha256"):
                if field in reply:
                    row[field] = reply[field]
            return row

        return self._record(position, "channel", fields)

    def add_cdp(self, position, *, text=None, frame=None):
        """Project observed CDP text and a verified RAM frame descriptor."""
        return self._record(position, "cdp", lambda: self._with_frame(
            position, {"op": "observe", **_text_fields(text)}, frame, required_kind="cdp_png",
        ))

    def add_atspi(self, position, *, atspi_capture, text=None):
        """Project a polling-only AT-SPI snapshot without retaining tree content."""
        return self._record(position, "atspi", lambda: {
            "op": "observe", "atspi_capture": atspi_capture, **_text_fields(text),
        })

    def _check_position(self, position):
        if self._state != "active":
            raise TimelineError("recording demand is not active")
        _timestamp(position.mono_ns)
        if (position.capsule_id, position.incarnation) != (self._capsule_id, self._incarnation):
            raise TimelineError("event position disagrees with its recording binding")

    def _in_closed_gap(self, when):
        return any(start <= when < end for start, end in self._closed_gaps)

    def _with_frame(self, position, row, frame, *, required_kind=None):
        if frame is None:
            return row
        descriptor = deepcopy(frame)
        _validate("vp.frame/v1", descriptor)
        if not _RAM_REFERENCE.fullmatch(descriptor["ram_ref"]):
            raise TimelineError("disk-backed frame references are refused")
        if self._in_closed_gap(descriptor["captured_mono_ns"]):
            return None
        if required_kind is not None and descriptor["frame_kind"] != required_kind:
            raise TimelineError("the CDP feed requires a CDP raster")
        if "cdp_target_id" in descriptor["witness"]:
            _identifier(descriptor["witness"]["cdp_target_id"])
        if self._verify_ram_frame(position, deepcopy(descriptor)) is not True:
            raise TimelineError("live RAM frame ownership was not verified")
        row["frame"] = descriptor
        return row

    def _record(self, position, source, project):
        with self._lock:
            self._check_position(position)
            if self._open_gaps or self._in_closed_gap(position.mono_ns):
                return False
            fields = project()
            if fields is None:
                return False
            self._append(position, source, fields)
            return True

    def _append(self, position, source, fields):
        row = {"mono_ns": position.mono_ns, "capsule_id": self._capsule_id,
               "incarnation": self._incarnation, "source": source, **fields}
        _validate("rec.timeline/v1", {"schema": "rec.timeline/v1", "events": [row]})
        self._rows.append(row)
