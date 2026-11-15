# @module {"id":"wa.act.proto1_action_policy","layer":3,"owner":"ACT","deps":["wa.act.w8u_fence","wa.act.cdp_executor","wa.act.coordinates","wa.perceive.ax","wa.perceive.session_registry","wa.act.window_scheduler"],"book":"HB-WA-s2-act"}
"""Fail-closed PROTO1 action wire/parser, evidence binding, and route policy.

No payload confers actuation authority.  A real W8UFence owns every prepared
mutation and its exact-mint/quarantine lifecycle; this module never makes a
transport, shell, OS, focus or lease object.  Read-only admissions emit nothing.
The protocol does not accept owner receipts without their separately owned
validator.  A completion from the custody owner is not an owner receipt.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import re
import threading
import time
from types import MappingProxyType

WIRE_SCHEMA = "w8u.action/v1"
MAX_BODY_BYTES = 64 * 1024
ENVELOPE_FIELDS = (
    "schema", "run_id", "lease_id", "epoch", "generation", "action_id",
    "action_seq", "observed_frame_seq", "observed_frame_sha256",
    "deadline_ns", "verb", "args", "grant_sha256",
)
RESPONSE_FIELDS = (
    "schema", "action_id", "action_seq", "lease_id", "epoch", "status",
    "code", "emitted", "result_known", "applied_tick", "outcome_sha256",
)
ADAPTER_FIELDS = (
    "run_id", "target_handle", "epoch", "binding_sha256", "frame_refs",
    "state_sha256", "action_id", "trace_sha256", "outcome", "owner_receipt",
)
FRAME_FIELDS = ("frame_seq", "width", "height", "rgb24_sha256", "binding_sha256")
HANDLE_FIELDS = ("kind", "canonical_id", "generation")
VERBS = frozenset(("observe", "locate", "click", "type", "key",
                   "drag", "scroll", "wait", "assert"))
KEYS = frozenset(("Enter", "Escape", "Tab", "Backspace", "Delete", "Space",
                  "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
                  "Home", "End", "PageUp", "PageDown"))
MODIFIERS = ("Shift", "Control", "Alt", "Super")
CDP_MASK = {"Shift": 8, "Control": 2, "Alt": 1, "Super": 4}
ROUTED_OPS = {"click": "click", "type": "insert_text", "key": "press"}
_LOCAL_VERBS = frozenset(("observe", "locate", "wait", "assert"))
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class PolicyRefused(ValueError):
    """Stable, machine-testable refusal before or at an owned execution fence."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(code + ": " + detail)


def _refuse(code, why):
    raise PolicyRefused(code, why)


def _exact_int(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        _refuse("E_SCHEMA", label + " must be an exact integer")
    return value


def _nonempty(value, label):
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        _refuse("E_SCHEMA", label + " must be a canonical nonempty string")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as error:
        raise PolicyRefused("E_SCHEMA", label + " must be UTF-8") from error
    return value


def _digest(value, label):
    if type(value) is not str or not _DIGEST.fullmatch(value):
        _refuse("E_SCHEMA", label + " must be 64 lowercase hex digits")
    return value


def _closed(value, fields, label, *, ordered=False):
    if type(value) is not dict or set(value) != set(fields):
        _refuse("E_SCHEMA", label + " must contain exactly its declared keys")
    if ordered and tuple(value) != tuple(fields):
        _refuse("E_SCHEMA", label + " fields are out of canonical order")
    return value


def _json_value(value):
    if value is None or type(value) in (bool, int, str):
        if type(value) is str:
            value.encode("utf-8", "strict")
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _refuse("E_SCHEMA", "non-string JSON key")
            key.encode("utf-8", "strict")
            _json_value(item)
        return
    _refuse("E_SCHEMA", "unsupported JSON value, float or non-finite number")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _refuse("E_SCHEMA", "duplicate JSON key")
        result[key] = value
    return result


def _no_float(_value):
    _refuse("E_SCHEMA", "floats and nonfinite numbers are not permitted")


def parse_envelope(body: bytes) -> dict:
    """Parse one canonical UTF-8 w8u.action/v1 body, with no permissive casts."""
    if type(body) is not bytes or not body or len(body) > MAX_BODY_BYTES:
        _refuse("E_SCHEMA", "action body must be 1..65536 bytes")
    try:
        plain = body.decode("utf-8", "strict")
        obj = json.loads(plain, object_pairs_hook=_pairs,
                         parse_float=_no_float, parse_constant=_no_float)
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        if isinstance(error, PolicyRefused):
            raise
        raise PolicyRefused("E_SCHEMA", "invalid UTF-8 JSON or trailing data") from error
    _closed(obj, ENVELOPE_FIELDS, "envelope", ordered=True)
    if obj["schema"] != WIRE_SCHEMA:
        _refuse("E_SCHEMA", "unknown action extension/schema version")
    for field in ("run_id", "lease_id", "action_id"):
        _nonempty(obj[field], field)
    for field in ("epoch", "action_seq", "observed_frame_seq"):
        _exact_int(obj[field], field)
    _exact_int(obj["generation"], "generation", 1)
    _exact_int(obj["deadline_ns"], "deadline_ns", 1)
    _digest(obj["observed_frame_sha256"], "observed_frame_sha256")
    _digest(obj["grant_sha256"], "grant_sha256")
    if type(obj["verb"]) is not str or obj["verb"] not in VERBS:
        _refuse("E_VERB", "verb is outside the closed dispatch vocabulary")
    _validate_args(obj["verb"], obj["args"])
    # UTF-8 and no insignificant spaces, BOM, alternate escaped spellings, or
    # object key reordering: this is a canonical v1 wire body, not merely JSON.
    canonical = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if canonical.encode("utf-8", "strict") != body:
        _refuse("E_SCHEMA", "noncanonical action body")
    return obj


def _point(args, xname, yname):
    return _exact_int(args[xname], xname), _exact_int(args[yname], yname)


def _validate_args(verb, args):
    if type(args) is not dict:
        _refuse("E_SCHEMA", "args must be a closed object")
    if verb == "observe":
        _closed(args, (), "observe.args")
    elif verb == "locate":
        _closed(args, ("target",), "locate.args")
        _nonempty(args["target"], "target")
    elif verb == "click":
        if set(args) == {"target", "button", "irreversible"}:
            _nonempty(args["target"], "target")
        elif set(args) == {"x", "y", "button", "irreversible"}:
            _point(args, "x", "y")
        else:
            _refuse("E_SCHEMA", "click requires a unique node or exact pixel")
        _exact_int(args["button"], "button", 1)
        if args["button"] > 3 or type(args["irreversible"]) is not bool:
            _refuse("E_SCHEMA", "button must be 1..3 and irreversible must be bool")
    elif verb == "type":
        _closed(args, ("target", "text"), "type.args")
        _nonempty(args["target"], "target")
        val = args["text"]
        if type(val) is not str or "\x00" in val:
            _refuse("E_SCHEMA", "text must not contain NUL")
        try:
            length = len(val.encode("utf-8", "strict"))
        except UnicodeError as error:
            raise PolicyRefused("E_SCHEMA", "invalid UTF-8 text") from error
        if length > 4096:
            _refuse("E_SCHEMA", "text exceeds 4096 UTF-8 bytes")
    elif verb == "key":
        _closed(args, ("target", "operation", "key", "modifiers"), "key.args")
        _nonempty(args["target"], "target")
        if (type(args["operation"]) is not str or args["operation"] not in ("press", "down", "up")
                or type(args["key"]) is not str or args["key"] not in KEYS):
            _refuse("E_SCHEMA", "invalid key operation or key")
        mods = args["modifiers"]
        if type(mods) is not list or len(mods) > len(MODIFIERS) or any(type(m) is not str for m in mods):
            _refuse("E_SCHEMA", "modifiers must be an ordered list")
        if mods != [m for m in MODIFIERS if m in mods]:
            _refuse("E_SCHEMA", "duplicate or out-of-order modifiers")
    elif verb == "drag":
        _closed(args, ("from_x", "from_y", "to_x", "to_y", "button"), "drag.args")
        _point(args, "from_x", "from_y")
        _point(args, "to_x", "to_y")
        _exact_int(args["button"], "button", 1)
        if args["button"] > 3:
            _refuse("E_SCHEMA", "pointer button outside 1..3")
    elif verb == "scroll":
        _closed(args, ("x", "y", "delta_x", "delta_y"), "scroll.args")
        _point(args, "x", "y")
        for name in ("delta_x", "delta_y"):
            value = args[name]
            if type(value) is not int or not -120 <= value <= 120:
                _refuse("E_SCHEMA", name + " outside [-120,120]")
        if args["delta_x"] == args["delta_y"] == 0:
            _refuse("E_SCHEMA", "scroll requires a nonzero delta")
    elif verb == "wait":
        _closed(args, ("duration_ms",), "wait.args")
        if _exact_int(args["duration_ms"], "duration_ms") > 5000:
            _refuse("E_SCHEMA", "duration_ms exceeds 5000")
    elif verb == "assert":
        _closed(args, ("expected_state_sha256",), "assert.args")
        _digest(args["expected_state_sha256"], "expected_state_sha256")


def parse_adapter_input(value: dict) -> dict:
    """Separate adapter binding, never silently added to the closed seat wire."""
    _closed(value, ADAPTER_FIELDS, "adapter", ordered=True)
    for name in ("run_id", "action_id"):
        _nonempty(value[name], name)
    _exact_int(value["epoch"], "epoch")
    for name in ("binding_sha256", "state_sha256", "trace_sha256"):
        _digest(value[name], name)
    handle = _closed(value["target_handle"], HANDLE_FIELDS, "target_handle", ordered=True)
    _nonempty(handle["kind"], "handle.kind")
    _nonempty(handle["canonical_id"], "handle.canonical_id")
    _exact_int(handle["generation"], "handle.generation", 1)
    frames = value["frame_refs"]
    if type(frames) is not list or not frames or len(frames) > 128:
        _refuse("E_STALE_FRAME", "at least one frame identity is required")
    prior = -1
    for item in frames:
        _closed(item, FRAME_FIELDS, "frame identity", ordered=True)
        seq = _exact_int(item["frame_seq"], "frame_seq")
        if seq <= prior:
            _refuse("E_STALE_FRAME", "non-increasing frame identities")
        prior = seq
        for dim in ("width", "height"):
            _exact_int(item[dim], dim, 1)
        _digest(item["rgb24_sha256"], "rgb24_sha256")
        _digest(item["binding_sha256"], "binding_sha256")
        if item["binding_sha256"] != value["binding_sha256"]:
            _refuse("E_STALE_FRAME", "frame has foreign binding digest")
    if value["owner_receipt"] is not None or value["outcome"] is not None:
        _refuse("E_UNCERTAIN", "real owner receipt/outcome validator is AUSENTE")
    try:
        _json_value(value)
        return copy.deepcopy(value)
    except (UnicodeError, RecursionError) as error:
        raise PolicyRefused("E_SCHEMA", "adapter contains non-UTF-8 or cyclic data") from error


@dataclass(frozen=True)
class RouteEvidence:
    """Trusted in-process available transport ops, not a caller payload field.

    Only an actual merged CDP operation present in cdp_executor.OPS is admitted.
    api_ops are withheld until a real API mapping is proved; generic labels or
    arbitrary executable strings never become operation capabilities.
    """

    ax_ops: frozenset[str]
    coordinates_ops: frozenset[str] = frozenset()
    modifier_mask_supported: bool = False
    calibration: object | None = None
    recorded_ax: object | None = None


@dataclass(frozen=True)
class PixelCalibration:
    """Independently declared pixel mapping bound to a current issued frame."""

    frame_seq: int
    width: int
    height: int
    dpr_ppm: int
    binding_sha256: str


@dataclass(frozen=True)
class Decision:
    route: str
    verb: str
    operation: str | None
    params: object


@dataclass(frozen=True)
class Admission:
    envelope_bytes: bytes
    replay_sha256: str
    envelope: object
    decision: Decision
    prepared: object | None


def _state_digest(state):
    _json_value(state)
    return hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def make_response(env, status, code, *, emitted=False, result_known=False,
                  applied_tick=None, outcome_sha256=None):
    """Canonical response metadata; no synthetic owner receipt or applied tick."""
    if status not in ("refused", "admitted", "applied", "uncertain"):
        _refuse("E_SCHEMA", "unknown response status")
    if type(emitted) is not bool or type(result_known) is not bool:
        _refuse("E_SCHEMA", "response flags must be bool")
    if applied_tick is not None:
        _exact_int(applied_tick, "applied_tick")
    if outcome_sha256 is not None:
        _digest(outcome_sha256, "outcome_sha256")
    result = dict(zip(RESPONSE_FIELDS, (
        WIRE_SCHEMA, env["action_id"], env["action_seq"], env["lease_id"],
        env["epoch"], status, code, emitted, result_known,
        applied_tick, outcome_sha256,
    )))
    return result


def encode_response(result):
    _closed(result, RESPONSE_FIELDS, "response", ordered=True)
    _json_value(result)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8", "strict")


def client_rc(code):
    if code == "OK_ADMITTED":
        return 0
    if code == "E_SCHEMA":
        return 2
    if code == "E_VERB":
        return 4
    if code == "E_UNCERTAIN":
        return 6
    if code in ("E_LOCK", "E_EPOCH", "E_STALE_FRAME", "E_FOCUS",
                "E_BUDGET", "E_DEADLINE"):
        return 5
    _refuse("E_SCHEMA", "unknown owner client result code")


# @anchor:wa.act.proto1_action_policy.ActionPolicy
class ActionPolicy:
    """Validation/route decisions and once-only dispatch through real W8UFence.

    Constructor requires the actual W8U custody owner and a registry it owns;
    no user-provided booleans or shell strings can replace that authority.
    Only external callbacks providing transport completions can be test doubles.
    """

    # @anchor:wa.act.proto1_action_policy.ActionPolicy.__init__
    def __init__(self, *, fence, clock=None, script_interlock=None):
        from wender.act.w8u_fence import W8UFence
        if not isinstance(fence, W8UFence):
            _refuse("E_LOCK", "merged real W8UFence owner is mandatory")
        self.fence = fence
        self.script_interlock = script_interlock
        self.registry = fence.registry.sessions
        self.clock = time.monotonic_ns if clock is None else clock
        if not callable(self.clock):
            _refuse("E_SCHEMA", "monotonic clock is unavailable")
        self._lock = threading.RLock()
        self._ledger = {}
        self._stamps = {}  # independent immutable mint-time admission snapshots
        self._pending = set()
        self._sequences = {}
        self._dispatching = set()
        self._results = {}
        self._script_contexts = {}

    def snapshot(self):
        """Read-only sequence and replay identity for no-mutation refusal tests."""
        with self._lock:
            return (tuple(sorted(self._sequences.items())), tuple(sorted(self._ledger)))

    def _frame_gate(self, env, adapter, observation):
        from wender.perceive.session_registry import Handle, Observation, RegistryError
        handle = adapter["target_handle"]
        try:
            registered = Handle(handle["kind"], handle["canonical_id"], handle["generation"])
            record = self.registry.resolve(registered)
        except (RegistryError, KeyError, AttributeError) as error:
            raise PolicyRefused("E_LOCK", "unregistered, invalid or retired target") from error
        if record.closed is not False or record.handle != registered:
            _refuse("E_LOCK", "target is not the current registered incarnation")
        if env["generation"] != registered.generation:
            _refuse("E_LOCK", "wire generation does not match registered incarnation")
        if record.epoch != adapter["epoch"] or record.epoch != env["epoch"]:
            _refuse("E_EPOCH", "current epoch differs from request")
        if (env["run_id"] != adapter["run_id"] or env["action_id"] != adapter["action_id"]):
            _refuse("E_SCHEMA", "wire and adapter action identities differ")
        if type(observation) is not Observation or observation.handle != registered or observation.epoch != record.epoch:
            _refuse("E_STALE_FRAME", "real observation for target/epoch absent")
        expected = adapter["frame_refs"][-1]
        frame = observation.frame
        if (observation.frame_seq != env["observed_frame_seq"]
                or frame.frame_seq != expected["frame_seq"]
                or frame.width != expected["width"] or frame.height != expected["height"]
                or frame.digest != expected["rgb24_sha256"]
                or frame.digest != env["observed_frame_sha256"]):
            _refuse("E_STALE_FRAME", "frame identity, geometry or digest changed")
        try:
            self.registry.admit_observation(registered, frame,
                                            state=observation.state, dpr_ppm=observation.dpr_ppm)
        except RegistryError as error:
            raise PolicyRefused("E_STALE_FRAME", "superseded real frame") from error
        if _state_digest(observation.state) != adapter["state_sha256"]:
            _refuse("E_STALE_FRAME", "state digest does not match issued observation")
        return registered

    @staticmethod
    def _unique_node(observation, target):
        nodes = observation.state.get("nodes")
        if type(nodes) is not list:
            _refuse("E_UNCERTAIN", "no current structured nodes")
        matches = [node for node in nodes if type(node) is dict and node.get("id") == target]
        if len(matches) != 1:
            _refuse("E_UNCERTAIN", "target is missing or ambiguous")
        if matches[0].get("disabled") is True:
            _refuse("E_UNCERTAIN", "target is disabled")
        return matches[0]

    def _grounding(self, evidence, observation, node, *, irreversible):
        from wender.perceive import ax
        record = evidence.recorded_ax
        if type(record) is not dict or not record.get("fp_exact"):
            _refuse("E_UNCERTAIN", "previously recorded structural target is absent")
        result = ax.match(observation.state["nodes"], record)
        if (result.get("node") is not node and
            # Compare stable node key, not merely the recorded role/name.
                (not isinstance(result.get("node"), dict) or result["node"].get("id") != node.get("id"))):
            _refuse("E_UNCERTAIN", "recorded target was not re-resolved")
        if result.get("tie") or result.get("candidates") != 1 or result.get("established"):
            _refuse("E_UNCERTAIN", "ambiguous/unestablished AX target")
        if irreversible and result.get("level") not in ("L0_EXACT", "L1_STABLE"):
            _refuse("E_UNCERTAIN", "irreversible target does not have L0/L1 proof")
        return result

    def _calibrated(self, evidence, adapter, observation):
        cal = evidence.calibration
        if type(cal) is not PixelCalibration:
            _refuse("E_UNCERTAIN", "independent device-pixel calibration is AUSENTE")
        if (type(cal.frame_seq) is not int or cal.frame_seq != observation.frame_seq
                or type(cal.width) is not int or cal.width != observation.frame.width
                or type(cal.height) is not int or cal.height != observation.frame.height
                or type(cal.dpr_ppm) is not int or cal.dpr_ppm != observation.dpr_ppm
                or cal.binding_sha256 != adapter["binding_sha256"]):
            _refuse("E_UNCERTAIN", "calibration is stale or inconsistent")
        viewport = observation.state.get("viewport")
        if (type(viewport) is not dict or
                type(viewport.get("w")) is not int or type(viewport.get("h")) is not int
                or viewport["w"] < 1 or viewport["h"] < 1):
            _refuse("E_UNCERTAIN", "calibrated viewport is unavailable")
        # Require the declared frame's device dimensions to agree with CSS DPR.
        if (viewport["w"] * cal.dpr_ppm // 1000000 != cal.width or
                viewport["h"] * cal.dpr_ppm // 1000000 != cal.height):
            _refuse("E_UNCERTAIN", "calibration and viewport geometry conflict")

    def _route(self, env, adapter, observation, evidence, now):
        from wender.act import cdp_executor, coordinates
        if type(evidence) is not RouteEvidence or type(evidence.ax_ops) is not frozenset or type(evidence.coordinates_ops) is not frozenset:
            _refuse("E_VERB", "independent transport availability is AUSENTE")
        verb, args = env["verb"], env["args"]
        if verb == "wait":
            if args["duration_ms"] * 1000000 > env["deadline_ns"] - now:
                _refuse("E_DEADLINE", "wait would exceed the absolute deadline")
            return Decision("local", verb, None, MappingProxyType({"duration_ms": args["duration_ms"]}))
        if verb == "assert":
            if args["expected_state_sha256"] != adapter["state_sha256"]:
                _refuse("E_UNCERTAIN", "asserted state is not current")
            return Decision("local", verb, None, MappingProxyType({}))
        if verb == "observe":
            return Decision("local", verb, None, MappingProxyType({}))
        if verb == "locate":
            node = self._unique_node(observation, args["target"])
            return Decision("local", verb, None, MappingProxyType({"target": node["id"]}))
        if verb in ("drag", "scroll"):
            coords = (("x", "y"),) if verb == "scroll" else (("from_x", "from_y"), ("to_x", "to_y"))
            for xfield, yfield in coords:
                if not (0 <= args[xfield] < observation.frame.width and
                        0 <= args[yfield] < observation.frame.height):
                    _refuse("E_SCHEMA", "requested pointer pixel is out of bounds")
            # Requested shapes are validated; no actual merged CDP owner mapping.
            _refuse("E_VERB", verb + " transport mapping is unproven")
        if observation.handle.kind != "cdp_tab":
            _refuse("E_VERB", "no proven operation on this target kind")
        if verb == "click" and args["irreversible"] and "x" in args:
            _refuse("E_UNCERTAIN", "irreversible coordinate-only proposal")
        if verb == "click" and args["button"] != 1:
            _refuse("E_VERB", "CDP click mapping for non-left button unproven")
        if verb == "key" and (args["operation"] != "press"
                               or args["key"] not in cdp_executor.KEYS):
            _refuse("E_VERB", "CDP key/down/up mapping is unproven")
        if verb == "key" and args["modifiers"] and evidence.modifier_mask_supported is not True:
            _refuse("E_VERB", "CDP modifier mapping not positively available")
        target = args.get("target")
        if target is None:
            # A raw pixel never by itself proves a unique actionable target.
            if verb != "click":
                _refuse("E_SCHEMA", "target is mandatory")
            self._calibrated(evidence, adapter, observation)
            x, y = args["x"], args["y"]
            if not (0 <= x < observation.frame.width and 0 <= y < observation.frame.height):
                _refuse("E_SCHEMA", "pixel lies outside the exact current frame")
            nodes = observation.state.get("nodes")
            if type(nodes) is not list:
                _refuse("E_UNCERTAIN", "pixel has no grounded node")
            covering = []
            for node in nodes:
                if type(node) is dict:
                    box = node.get("box")
                    if (type(box) is list and len(box) == 4 and all(type(v) is int for v in box)
                            and box[2] > 0 and box[3] > 0):
                        if box[0] <= x < box[0] + box[2] and box[1] <= y < box[1] + box[3]:
                            covering.append(node)
            if len(covering) != 1:
                _refuse("E_UNCERTAIN", "raw pixel has no unique grounded node")
            node = covering[0]
            self._unique_node(observation, node["id"])
            self._grounding(evidence, observation, node, irreversible=False)
            # Do not silently relocate a specifically requested pixel to a box centre.
            if (node["box"][0] + node["box"][2] // 2,
                node["box"][1] + node["box"][3] // 2) != (x, y):
                _refuse("E_UNCERTAIN", "pixel is not the registered target centre")
        else:
            node = self._unique_node(observation, target)
        if verb == "type" and node.get("role") not in cdp_executor.EDITABLE_ROLES:
            _refuse("E_FOCUS", "text target is not editable")
        if verb == "click" and args["irreversible"]:
            self._grounding(evidence, observation, node, irreversible=True)
        mapped = ROUTED_OPS[verb]
        if mapped in evidence.ax_ops and mapped in cdp_executor.OPS:
            if node.get("backend_id") is None and mapped != "press":
                _refuse("E_UNCERTAIN", "structural route requires an AX backend id")
            params = {"op": mapped, "target": node["id"]}
            if verb == "type":
                params["text"] = args["text"]
            if verb == "key":
                params["key"] = args["key"]
                params["modifiers"] = sum(CDP_MASK[name] for name in args["modifiers"])
            if verb == "click":
                params["irreversible"] = args["irreversible"]
            return Decision("ax", verb, mapped, MappingProxyType(params))
        if verb == "click" and not args["irreversible"] and "click" in evidence.coordinates_ops:
            self._calibrated(evidence, adapter, observation)
            self._grounding(evidence, observation, node, irreversible=False)
            try:
                events = coordinates.plan_click(observation.state, node["id"])
            except coordinates.CoordRefused as error:
                raise PolicyRefused("E_UNCERTAIN", "coordinate planner refused grounding") from error
            for event in events:
                if (type(event.get("x")) is not int or type(event.get("y")) is not int
                        or not 0 <= event["x"] < observation.frame.width
                        or not 0 <= event["y"] < observation.frame.height):
                    _refuse("E_UNCERTAIN", "coordinate planner produced out-of-bounds event")
            return Decision("coordinates", verb, "click", MappingProxyType({
                "target": node["id"], "events": tuple(MappingProxyType(item) for item in events),
            }))
        _refuse("E_VERB", "no proven structural route or reversible calibrated fallback")

    def admit(self, body, adapter_input, *, observation, transport, frame_evidence):
        """Prepare a mutation only after strict identity, frame and route gates.

        No application/callback/transport channel is touched before all gates.
        A read-only admission does not consume budget, issue a lease or open IO.
        """
        env = parse_envelope(body)
        adapter = parse_adapter_input(adapter_input)
        snapshot = hashlib.sha256(body + b"\x00" + json.dumps(adapter, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        key = (env["run_id"], env["action_id"])
        # The original witnessed replay is idempotent even after the target
        # becomes stale, focus expires, or the send is quarantined.  Never
        # reacquire custody or demand a new action id merely to retrieve it.
        with self._lock:
            known = self._ledger.get(key)
            if known is not None:
                if self._stamps[key][0] != snapshot:
                    _refuse("E_SCHEMA", "changed action replay")
                return known
        handle = self._frame_gate(env, adapter, observation)
        now = self.clock()
        if type(now) is not int or now >= env["deadline_ns"]:
            _refuse("E_DEADLINE", "absolute monotonic deadline elapsed")
        decision = self._route(env, adapter, observation, transport, now)
        if decision.route != "local":
            # Focus read is from actual injected custody owner's authenticated
            # authority, never from adapter fields or an unverified bool.
            try:
                focus = self.fence.focus.evidence_for(handle, epoch=env["epoch"], now_ns=now)
            except Exception as error:
                raise PolicyRefused("E_FOCUS", "current authenticated focus absent") from error
            if env["verb"] == "type" and focus.editable is not True:
                _refuse("E_FOCUS", "text requires editable, authenticated focus")
        with self._lock:
            known = self._ledger.get(key)
            if known is not None:
                if self._stamps[key][0] != snapshot:
                    _refuse("E_SCHEMA", "changed action replay")
                return known
            if key in self._pending:
                _refuse("E_LOCK", "same action is being prepared")
            expected = self._sequences.get(env["run_id"], 0)
            if env["action_seq"] != expected:
                _refuse("E_SCHEMA", "noncontiguous action sequence")
            self._pending.add(key)
        prepared = None
        try:
            if decision.route != "local":
                from wender.perceive.session_registry import ActionRequest
                payload = {"verb": "text" if env["verb"] == "type" else "input",
                           "route": decision.route, "operation": decision.operation,
                           "params": _thaw_params(decision.params)}
                # The actual real W8UFence validates the independent frame pin,
                # budget, focus, nonce/lock and writer permit; no fake owner.
                prepared = self.fence.prepare(
                    ActionRequest(handle, env["action_id"], env["epoch"], payload,
                                  1, env["deadline_ns"]),
                    observation=observation, frame_evidence=frame_evidence,
                )
            admission = Admission(body, snapshot, MappingProxyType(copy.deepcopy(env)),
                                  decision, prepared)
            decision_stamp = _decision_stamp(decision)
            with self._lock:
                self._stamps[key] = (snapshot, body, decision_stamp, prepared)
                self._ledger[key] = admission
                self._sequences[env["run_id"]] = env["action_seq"] + 1
            return admission
        except PolicyRefused:
            raise
        except Exception as error:
            code = getattr(error, "code", "E_LOCK")
            if code not in ("E_SCHEMA", "E_VERB", "E_LOCK", "E_EPOCH", "E_STALE_FRAME",
                            "E_FOCUS", "E_BUDGET", "E_DEADLINE", "E_UNCERTAIN"):
                code = "E_LOCK"
            raise PolicyRefused(code, "real custody owner refused admission: " + type(error).__name__) from error
        finally:
            with self._lock:
                self._pending.discard(key)

    # @anchor:wa.act.proto1_action_policy.ActionPolicy.dispatch
    def dispatch(self, admission: Admission, callback, *, script_context=None):
        """One W8U-guarded send; external callback must return true Completion.

        Never retry after uncertainty.  No owner receipt is accepted or created.
        A second identical invocation returns the recorded metadata only.
        """
        if type(admission) is not Admission or not callable(callback):
            _refuse("E_SCHEMA", "minted admission and typed callback are required")
        if parse_envelope(admission.envelope_bytes) != admission.envelope:
            _refuse("E_SCHEMA", "admission envelope changed since its validation")
        env = admission.envelope
        key = (env["run_id"], env["action_id"])
        # Never acquire the shared SCRIPT owner while holding policy metadata:
        # bounded input admission may need this policy's metadata in callbacks.
        context_rejected = False
        try:
            if self.script_interlock is not None:
                self.script_interlock.check_context(admission, script_context)
            elif script_context is not None:
                context_rejected = True
        except Exception:
            context_rejected = True
        if context_rejected:
            _refuse("E_LOCK", "SCRIPT context association refused")
        with self._lock:
            mint = self._stamps.get(key)
            if self._ledger.get(key) is not admission or mint is None:
                _refuse("E_LOCK", "foreign or modified admission")
            if (admission.replay_sha256 != mint[0]
                    or admission.envelope_bytes != mint[1]
                    or _decision_stamp(admission.decision) != mint[2]
                    or admission.prepared is not mint[3]):
                _refuse("E_SCHEMA", "minted admission or route was modified")
            # Validate association before cached results or dispatch mutation.
            # Rejection outside the send transaction must not consume the mint.
            required = self._script_contexts.get(key)
            if required is not None and (script_context is None or id(script_context) != required):
                _refuse("E_LOCK", "SCRIPT context association refused")
            if script_context is not None:
                if admission.decision.route == "local":
                    _refuse("E_VERB", "SCRIPT effect requires a physical boundary")
                self._script_contexts[key] = id(script_context)
            if key in self._results:
                return copy.deepcopy(self._results[key])
            if key in self._dispatching:
                _refuse("E_UNCERTAIN", "dispatch already in progress; do not retry")
            self._dispatching.add(key)
        emitted = [False]
        script_error = None
        try:
            if admission.decision.route == "local":
                result = make_response(env, "admitted", "OK_ADMITTED")
            else:
                from wender.act.window_scheduler import Completion
                def typed_send(request):
                    def physical_send():
                        emitted[0] = True
                        return callback(request, admission.decision)
                    if script_context is not None:
                        return self.script_interlock.send_once(
                            admission, request, admission.decision, physical_send,
                            context=script_context)
                    return physical_send()

                receipt = self.fence.send(admission.prepared, typed_send)
                if type(receipt) is not Completion or receipt.acknowledged is not True:
                    _refuse("E_UNCERTAIN", "real custody owner did not acknowledge")
                # Actual transport send, but owner-receipt protocol is AUSENTE:
                # report admission, never invent an applied tick or digest.
                result = make_response(env, "admitted", "OK_ADMITTED", emitted=emitted[0])
            with self._lock:
                self._results[key] = result
            return copy.deepcopy(result)
        except Exception as error:
            code = getattr(error, "code", "E_UNCERTAIN")
            if code not in ("E_SCHEMA", "E_VERB", "E_LOCK", "E_EPOCH", "E_STALE_FRAME",
                            "E_FOCUS", "E_BUDGET", "E_DEADLINE", "E_UNCERTAIN"):
                code = "E_UNCERTAIN"
            result = make_response(env, "uncertain" if emitted[0] else "refused",
                                   "E_UNCERTAIN" if emitted[0] else code, emitted=emitted[0])
            with self._lock:
                self._results[key] = result
            if script_context is None:
                raise PolicyRefused(code, "real owner send refused or outcome uncertain") from error
            script_error = code
        finally:
            with self._lock:
                self._dispatching.discard(key)
        # Leave the exception handler before exposing a SCRIPT refusal; raw
        # transport exceptions can contain private payloads or callback reprs.
        if script_error is not None:
            raise PolicyRefused(script_error, "SCRIPT send refused or outcome uncertain")


def _thaw_params(value):
    if isinstance(value, MappingProxyType):
        return {k: _thaw_params(v) for k, v in value.items()}
    if type(value) is tuple:
        return [_thaw_params(v) for v in value]
    return copy.deepcopy(value)


def _decision_stamp(decision):
    if type(decision) is not Decision or type(decision.route) is not str or type(decision.verb) is not str:
        _refuse("E_SCHEMA", "invalid typed decision")
    data = (decision.route, decision.verb, decision.operation,
            _thaw_params(decision.params))
    _json_value(list(data))
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
