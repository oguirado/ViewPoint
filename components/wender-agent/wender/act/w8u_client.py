# @module {"id":"wa.act.w8u_client","layer":1,"owner":"ACT","deps":[],"book":"HB-WA-s2-act"}
"""W8U finite, injected, one-step action client (no process or frame transport).

A trusted caller supplies seat/frame evidence, a DispatchContext writer fence, a
step-channel factory and an independent outcome verifier. No program, display,
argv, environment or command path can be selected from action data.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

ACTION_SCHEMA = "w8u.action/v1"
MAX_BODY_BYTES = 64 * 1024
MAX_TEXT_BYTES = 4096
_RESULT_FIELDS = frozenset(("schema", "action_id", "action_seq", "lease_id", "epoch",
                            "status", "code", "emitted", "result_known", "applied_tick",
                            "outcome_sha256"))
_ACTION_FIELDS = frozenset(("schema", "run_id", "lease_id", "epoch", "generation",
                            "action_id", "action_seq", "observed_frame_seq",
                            "observed_frame_sha256", "deadline_ns", "verb", "args",
                            "grant_sha256"))
_CODES = frozenset(("OK_ADMITTED", "E_SCHEMA", "E_VERB", "E_LOCK", "E_EPOCH",
                    "E_STALE_FRAME", "E_FOCUS", "E_BUDGET", "E_DEADLINE", "E_UNCERTAIN"))
_KEYS = frozenset(("Enter", "Escape", "Tab", "Backspace", "Delete", "Space",
                   "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End",
                   "PageUp", "PageDown"))
_MODIFIERS = ("Shift", "Control", "Alt", "Super")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class StepError(ValueError):
    """Typed refusal; `code` is a protocol error, never an actuator outcome."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code


def _reject(code: str, detail: str):
    raise StepError(code, detail)


def _integer(value, name, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        _reject("E_SCHEMA", f"invalid {name}")
    return value


def _name(value, name):
    if type(value) is not str or not _NAME.fullmatch(value):
        _reject("E_SCHEMA", f"invalid {name}")
    return value


def _sha(value, name):
    if type(value) is not str or not _SHA.fullmatch(value):
        _reject("E_SCHEMA", f"invalid {name}")
    return value


def _keys(value, allowed, name):
    if type(value) is not dict or frozenset(value) != allowed:
        _reject("E_SCHEMA", f"{name} requires exactly {sorted(allowed)}")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject("E_SCHEMA", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _bad_float(_):
    _reject("E_SCHEMA", "floating point, NaN and infinity are forbidden")


def _json(value, expected_keys, name):
    """Strict UTF-8, single JSON value, no duplicate keys, closed key sets."""
    if type(value) is bytes:
        if len(value) > MAX_BODY_BYTES:
            _reject("E_SCHEMA", f"{name} exceeds byte cap")
        try:
            decoded = value.decode("utf-8", errors="strict")
            obj = json.loads(decoded, object_pairs_hook=_pairs,
                             parse_float=_bad_float, parse_constant=_bad_float)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StepError("E_SCHEMA", f"invalid {name} JSON/UTF-8") from exc
    elif type(value) is dict:
        obj = value
    else:
        _reject("E_SCHEMA", f"{name} must be bytes or a dict")
    _keys(obj, expected_keys, name)
    try:
        canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError) as exc:
        raise StepError("E_SCHEMA", f"invalid {name} value") from exc
    if len(canonical) > MAX_BODY_BYTES:
        _reject("E_SCHEMA", f"{name} canonical body exceeds byte cap")
    return obj, canonical


@dataclass(frozen=True)
class SeatEvidence:
    """Authenticated out-of-band input; all identities and pixels are pinned."""

    run_id: str
    lease_id: str
    epoch: int
    generation: int
    frame_seq: int
    frame_sha256: str
    grant_sha256: str
    width: int
    height: int
    editable_focus: bool

    def __post_init__(self):
        _name(self.run_id, "run_id")
        _name(self.lease_id, "lease_id")
        _integer(self.epoch, "epoch")
        _integer(self.generation, "generation", 1)
        _integer(self.frame_seq, "frame_seq")
        _sha(self.frame_sha256, "frame_sha256")
        _sha(self.grant_sha256, "grant_sha256")
        _integer(self.width, "width", 1)
        _integer(self.height, "height", 1)
        if type(self.editable_focus) is not bool:
            _reject("E_SCHEMA", "editable_focus must be a trusted boolean")


@dataclass(frozen=True)
class StepResult:
    action_id: str
    action_seq: int
    status: str
    code: str
    emitted: bool
    result_known: bool
    applied_tick: int | None
    outcome_sha256: str | None

    @property
    def applied_known(self):
        return self.status == "applied" and self.result_known


class W8UStepClient:
    """Explicitly selected helper, never a fallback from actor.execute.

    channel_factory.open() returns a step-only channel with send(bytes),
    read(max_bytes)->bytes (b'' means exact EOF), wait()->int and close().
    dispatch.send(callback) must be the genuine, already-authorized host fence.
    verify_applied(response, action, seat) must independently attest an effect.
    Neither frame stream nor executor is constructed by this module.
    """

    def __init__(self, *, channel_factory, seat: SeatEvidence, clock,
                 result_schema: str, verify_applied):
        if not isinstance(seat, SeatEvidence) or not callable(clock):
            _reject("E_SCHEMA", "seat and clock are required")
        if not callable(getattr(channel_factory, "open", None)):
            _reject("E_SCHEMA", "injected channel factory required")
        if type(result_schema) is not str or not result_schema.strip():
            _reject("E_SCHEMA", "revision-pinned result schema required")
        if not callable(verify_applied):
            _reject("E_SCHEMA", "independent outcome verifier required")
        self.channel_factory = channel_factory
        self.seat = seat
        self.clock = clock
        self.result_schema = result_schema
        self.verify_applied = verify_applied
        self._actions = {}  # action_id -> (canonical bytes, StepResult | None)
        self._sequences = {}  # action_seq -> action_id; never try a new ID
        self._held_keys = set()
        self._held_buttons = set()
        self.quarantined = False

    def _validate(self, action, now):
        s = self.seat
        if action["schema"] != ACTION_SCHEMA:
            _reject("E_SCHEMA", "wrong action schema")
        for key, expected, code in (("run_id", s.run_id, "E_SCHEMA"),
                                    ("lease_id", s.lease_id, "E_LOCK"),
                                    ("epoch", s.epoch, "E_EPOCH"),
                                    ("generation", s.generation, "E_LOCK"),
                                    ("observed_frame_seq", s.frame_seq, "E_STALE_FRAME"),
                                    ("observed_frame_sha256", s.frame_sha256, "E_STALE_FRAME"),
                                    ("grant_sha256", s.grant_sha256, "E_LOCK")):
            value = action[key]
            if type(value) is not type(expected) or value != expected:
                _reject(code, f"untrusted {key}")
        _name(action["action_id"], "action_id")
        _integer(action["action_seq"], "action_seq")
        deadline = _integer(action["deadline_ns"], "deadline_ns")
        if deadline <= now:
            _reject("E_DEADLINE", "expired before dispatch")
        args = action["args"]
        verb = action["verb"]
        if verb == "pointer":
            if type(args) is not dict or args.get("operation") not in ("move", "click", "down", "up", "scroll"):
                _reject("E_VERB", "unknown pointer operation")
            op = args["operation"]
            expected = {"operation", "x", "y"}
            if op in ("click", "down", "up"):
                expected.add("button")
            if op == "scroll":
                expected.update(("dx", "dy"))
            _keys(args, frozenset(expected), "pointer args")
            _integer(args["x"], "x", 0, s.width - 1)
            _integer(args["y"], "y", 0, s.height - 1)
            if "button" in expected:
                _integer(args["button"], "button", 1, 3)
                if op == "up" and args["button"] not in self._held_buttons:
                    _reject("E_LOCK", "unheld pointer release")
                if op == "down" and args["button"] in self._held_buttons:
                    _reject("E_LOCK", "duplicate pointer down")
            if op == "scroll":
                dx = _integer(args["dx"], "dx", -120, 120)
                dy = _integer(args["dy"], "dy", -120, 120)
                if dx == dy == 0:
                    _reject("E_SCHEMA", "zero scroll")
        elif verb == "key":
            _keys(args, frozenset(("operation", "key", "modifiers")), "key args")
            op, key, modifiers = args["operation"], args["key"], args["modifiers"]
            if op not in ("press", "down", "up") or type(key) is not str or key not in _KEYS:
                _reject("E_VERB", "unsupported key or operation")
            if (type(modifiers) is not list or any(type(m) is not str or m not in _MODIFIERS for m in modifiers)
                    or modifiers != [m for m in _MODIFIERS if m in modifiers]):
                _reject("E_SCHEMA", "modifiers must be finite, unique and ordered")
            if op == "up" and key not in self._held_keys:
                _reject("E_LOCK", "unheld key release")
            if op == "down" and key in self._held_keys:
                _reject("E_LOCK", "duplicate key down")
        elif verb == "text":
            _keys(args, frozenset(("mode", "text")), "text args")
            if args["mode"] != "type" or type(args["text"]) is not str:
                _reject("E_VERB", "text requires type mode")
            if not s.editable_focus:
                _reject("E_FOCUS", "editable focus not evidenced")
            if "\x00" in args["text"]:
                _reject("E_SCHEMA", "text contains NUL")
            try:
                if len(args["text"].encode("utf-8")) > MAX_TEXT_BYTES:
                    _reject("E_SCHEMA", "text byte cap exceeded")
            except UnicodeError as exc:
                raise StepError("E_SCHEMA", "text is invalid UTF-8") from exc
        elif verb == "wait":
            _keys(args, frozenset(("duration_ms",)), "wait args")
            duration = _integer(args["duration_ms"], "duration_ms", 0, 5000)
            if now + duration * 1_000_000 > deadline:
                _reject("E_DEADLINE", "wait crosses action deadline")
        else:
            _reject("E_VERB", "unsupported action verb")

    def _validate_dispatch(self, dispatch, action, now):
        # The transport cannot authenticate itself: these are additional
        # structural guards over the host's DispatchContext.send() fence.
        try:
            t = dispatch.ticket
            actor = dispatch.actor_lease
            focus = dispatch.focus_lease
            h = t.handle
            valid = (t.action_id == action["action_id"]
                     and type(h.generation) is int and h.generation == self.seat.generation
                     and actor.handle == h and actor.generation == h.generation
                     and type(actor.expires_ns) is int and actor.expires_ns > now
                     and focus.lease == actor
                     and type(focus.epoch) is int and focus.epoch == self.seat.epoch
                     and type(dispatch.deadline_ns) is int
                     and dispatch.deadline_ns >= action["deadline_ns"]
                     and callable(dispatch.send))
        except (AttributeError, TypeError):
            valid = False
        if not valid:
            _reject("E_LOCK", "host dispatch identity/epoch/deadline mismatch")

    def _invoke(self, data):
        channel = self.channel_factory.open()
        try:
            channel.send(data)
            chunks = []
            total = 0
            while True:
                chunk = channel.read(4096)
                if type(chunk) is not bytes:
                    _reject("E_SCHEMA", "step reader returned non-bytes")
                if not chunk:  # actual EOF, never a newline or short-read proxy
                    break
                total += len(chunk)
                if total > MAX_BODY_BYTES:
                    _reject("E_SCHEMA", "step response exceeds byte cap")
                chunks.append(chunk)
            rc = channel.wait()
            if type(rc) is not int or rc not in (0, 2, 4, 5, 6):
                _reject("E_SCHEMA", "invalid step exit code")
            return rc, b"".join(chunks)
        finally:
            channel.close()

    def _reply(self, rc, wire, action):
        reply, _ = _json(wire, _RESULT_FIELDS, "step response")
        if reply["schema"] != self.result_schema:
            _reject("E_SCHEMA", "unfrozen or wrong response schema")
        for key in ("action_id", "action_seq", "lease_id", "epoch"):
            if type(reply[key]) is not type(action[key]) or reply[key] != action[key]:
                _reject("E_SCHEMA", f"uncorrelated response {key}")
        status, code = reply["status"], reply["code"]
        if type(status) is not str or status not in ("refused", "admitted", "applied", "uncertain"):
            _reject("E_SCHEMA", "invalid response status")
        if type(code) is not str or code not in _CODES:
            _reject("E_SCHEMA", "invalid response code")
        if type(reply["emitted"]) is not bool or type(reply["result_known"]) is not bool:
            _reject("E_SCHEMA", "invalid effect flags")
        emitted, known = reply["emitted"], reply["result_known"]
        tick, digest = reply["applied_tick"], reply["outcome_sha256"]
        if status == "applied":
            if not emitted or not known or code != "OK_ADMITTED":
                _reject("E_SCHEMA", "applied requires effect and known result")
            _integer(tick, "applied_tick")
            _sha(digest, "outcome_sha256")
        else:
            if tick is not None or digest is not None:
                _reject("E_SCHEMA", "non-applied reply asserts effect digest/tick")
            if status == "refused" and (emitted or not known or code == "OK_ADMITTED"):
                _reject("E_SCHEMA", "refusal contradicts effect flags")
            if status == "admitted" and (known or code != "OK_ADMITTED"):
                _reject("E_SCHEMA", "admission is not an effect")
            if status == "uncertain" and (known or code != "E_UNCERTAIN"):
                _reject("E_SCHEMA", "uncertain status must stay unknown")
        if (rc == 2 and (status != "refused" or code not in ("E_SCHEMA", "E_VERB"))
                or rc == 4 and status != "refused"
                or rc == 5 and status != "refused"
                or rc == 6 and status != "uncertain"
                or rc != 0 and status in ("admitted", "applied")):
            _reject("E_SCHEMA", "step exit code and status disagree")
        return StepResult(action["action_id"], action["action_seq"], status, code,
                          emitted, known, tick, digest), reply

    def _quarantine(self, dispatch):
        self.quarantined = True
        try:
            fence = dispatch.writer_fence
            resource = fence.resource_for(dispatch.ticket.handle)
            if resource is not None:
                fence.quarantine(resource)
        except AttributeError:
            pass  # no fake host fence method is inferred or substituted

    def execute(self, raw_action, *, dispatch):
        action, body = _json(raw_action, _ACTION_FIELDS, "step action")
        _name(action["action_id"], "action_id")
        _integer(action["action_seq"], "action_seq")
        ident, seq = action["action_id"], action["action_seq"]
        if ident in self._actions:
            prior, result = self._actions[ident]
            if prior != body:
                _reject("E_SCHEMA", "changed replay payload")
            if result is None or self.quarantined:
                _reject("E_UNCERTAIN", "possible prior effect is quarantined")
            return result
        if self.quarantined:
            _reject("E_UNCERTAIN", "unreconciled possible effect")
        if seq in self._sequences:
            _reject("E_SCHEMA", "action sequence bound to another ID")
        now = _integer(self.clock(), "clock")
        self._validate(action, now)
        self._validate_dispatch(dispatch, action, now)
        self._actions[ident] = (body, None)
        self._sequences[seq] = ident
        called = False

        def send_one():
            nonlocal called
            called = True  # before crossing an unobservable physical write
            return self._invoke(body)

        try:
            rc, wire = dispatch.send(send_one)
            result, reply = self._reply(rc, wire, action)
            if result.status == "applied":
                try:
                    verified = self.verify_applied(reply, action, self.seat)
                except Exception as exc:
                    raise StepError("E_UNCERTAIN", "independent outcome verifier failed") from exc
                if verified is not True:
                    _reject("E_UNCERTAIN", "applied claim lacks trusted outcome")
            elif result.status == "uncertain" or (result.status == "admitted" and result.emitted):
                _reject("E_UNCERTAIN", "emission lacks a trusted terminal effect")
            self._actions[ident] = (body, result)
            if result.applied_known:
                args, verb = action["args"], action["verb"]
                if verb == "key" and args["operation"] == "down":
                    self._held_keys.add(args["key"])
                elif verb == "key" and args["operation"] == "up":
                    self._held_keys.remove(args["key"])
                elif verb == "pointer" and args["operation"] == "down":
                    self._held_buttons.add(args["button"])
                elif verb == "pointer" and args["operation"] == "up":
                    self._held_buttons.remove(args["button"])
            return result
        except Exception as exc:
            if called:
                self._quarantine(dispatch)
                if isinstance(exc, StepError):
                    raise
                raise StepError("E_UNCERTAIN", "possible emission with lost acknowledgement") from exc
            del self._actions[ident]
            del self._sequences[seq]
            if isinstance(exc, StepError):
                raise
            raise StepError("E_LOCK", "host writer fence refused dispatch") from exc
