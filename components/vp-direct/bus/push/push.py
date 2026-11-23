"""Project broker records over an initialized TaskLight MCP stdio connection."""

import json
import re
from pathlib import Path
from threading import Lock

from jsonschema import ValidationError

from schemas.validation import canonical_bytes, validate_contract
from vendor.tasklight.model import schema as tasklight


MAX_FRAME_BYTES = 65536
FORBIDDEN_ROOMS = frozenset({"viewpoint"})
# TaskLight bridge/room/store.py at 410ff29.
CRED_RE = re.compile(r"(token|secret|credential|password|\.env|id_rsa|auth\.json)", re.I)
ROOM_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
AGENT_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
NOTICE_FIELDS = ("session_id", "goal_id", "reason", "raised_by")
_HUMAN_LEASE_SCHEMA = (Path(__file__).resolve().parents[2] / "schemas"
                       / "vp.human_lease_v1.schema.json")
HUMAN_REASONS = frozenset(
    json.loads(_HUMAN_LEASE_SCHEMA.read_text(encoding="utf-8"))["properties"]["reason"]["enum"]
)


class PushError(ValueError):
    """The projection, notification, or transport could not be verified."""


def _check_text(value):
    if isinstance(value, str) and CRED_RE.search(value):
        raise PushError("credential-like text refused")
    if isinstance(value, dict):
        for child in value.values():
            _check_text(child)
    if isinstance(value, (tuple, list)):
        for child in value:
            _check_text(child)


def _encode_frame(message):
    try:
        frame = canonical_bytes(message)
    except (TypeError, ValueError):
        raise PushError("invalid wall frame") from None
    if len(frame) > MAX_FRAME_BYTES:
        raise PushError("wall frame exceeds 65536 bytes")
    return frame


class ContextWallClient:
    """Own the initialized connection's binary streams exclusively while in use.

    The host provides stream lifetime and deadlines. A broken exchange retires
    the client because outstanding effects may have reached the wall.
    """

    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer
        self._lock = Lock()
        self._next_id = 1
        self._broken = False

    def _call_batch(self, calls):
        """Preflight every UTF-8 JSON-RPC frame, then send in projection order."""
        with self._lock:
            if self._broken:
                raise PushError("wall connection requires reconciliation")
            frames = []
            for request_id, (tool, args) in enumerate(calls, self._next_id):
                frame = _encode_frame({
                    "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                    "params": {"name": tool, "arguments": args},
                })
                frames.append((request_id, frame))
            self._next_id += len(frames)
            try:
                return [self._exchange(request_id, frame) for request_id, frame in frames]
            except (OSError, PushError):
                self._broken = True
                raise

    def _exchange(self, request_id, frame):
        if self._writer.write(frame) != len(frame):
            raise PushError("short wall write")
        self._writer.flush()
        raw = self._reader.readline(MAX_FRAME_BYTES + 1)
        if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
            raise PushError("oversized or incomplete wall response")
        try:
            reply = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise PushError("invalid wall response") from None
        if (not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0"
                or type(reply.get("id")) is not int or reply["id"] != request_id):
            raise PushError("uncorrelated wall response")
        if "error" in reply and "result" not in reply and isinstance(reply["error"], dict):
            return False, {}
        result = reply.get("result")
        if ("error" in reply or not isinstance(result, dict)
                or type(result.get("isError", False)) is not bool):
            raise PushError("invalid wall result")
        return not result.get("isError", False), result


def _projection_records(records):
    for record in records:
        if not isinstance(record, dict):
            raise PushError("invalid broker record")
        kind = record.get("rec")
        if kind == "HUMAN_NEEDED":
            continue
        tm = record.get("tm")
        if tm is not None:
            try:
                validate_contract("tm/v1", tm)
            except ValidationError:
                raise PushError("invalid tm/v1 record") from None
            _check_text(tm)
        if kind == "task":
            _check_text((record.get("title"), record.get("plan"), record.get("outcome")))
        elif kind == "enqueue":
            _check_text((record.get("lane"), record.get("kind")))
        elif kind in ("close", "txn"):
            _check_text((record.get("verdict"), record.get("reason")))
        yield record


def _read_handoffs(result, agent):
    parts = result.get("content")
    if not isinstance(parts, list) or not parts:
        raise PushError("invalid read_inbox result")
    inbox_events = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "text":
            raise PushError("invalid read_inbox result")
        try:
            payload = json.loads(part["text"])
        except (KeyError, TypeError, ValueError):
            raise PushError("invalid read_inbox result") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise PushError("invalid read_inbox result")
        inbox_events.extend(payload["events"])
    candidates = []
    for event in inbox_events:
        if not isinstance(event, dict):
            continue
        text = event.get("untrusted_text", event.get("text"))
        lamport = event.get("lamport")
        if (isinstance(text, str) and tasklight.HANDOFF_RE.fullmatch(text)
                and not CRED_RE.search(text) and (lamport is None or type(lamport) is int)):
            candidates.append(event)
    handoffs = [notice for notice in tasklight.parse_inbox(candidates)
                if notice["to_agent"] == agent]
    return len(inbox_events), handoffs


def push(records, room, client, *, inbox_agent=None):
    """Publish TaskLight calls and optionally read the addressed inbox."""
    if not isinstance(room, str) or not ROOM_RE.fullmatch(room) or room in FORBIDDEN_ROOMS:
        raise PushError("wall room refused")
    if inbox_agent is not None and (
            not isinstance(inbox_agent, str) or not AGENT_RE.fullmatch(inbox_agent)):
        raise PushError("wall inbox agent refused")

    projection = tasklight.calls(_projection_records(records), room)
    calls = [(row["tool"], row["arguments"]) for row in projection]
    if inbox_agent is not None:
        calls.append(("read_inbox", {"room": room, "agent": inbox_agent}))
    for _, args in calls:
        _check_text(args)

    responses = client._call_batch(calls)
    sent = len(projection)
    acknowledged = sum(ok for ok, _ in responses[:sent])
    summary = {"room": room, "calls": sent, "ok": acknowledged, "refused": sent - acknowledged}
    if inbox_agent is not None:
        ok, response = responses[-1]
        if not ok:
            raise PushError("read_inbox refused")
        count, handoffs = _read_handoffs(response, inbox_agent)
        summary.update(inbox_events=count, handoffs_read_back=handoffs)
    return summary


def notify_human(event, sink):
    """Pass only handoff metadata from a trusted broker event to an injected sink."""
    if (not isinstance(event, dict) or event.get("rec") != "HUMAN_NEEDED"
            or event.get("by") != "broker"):
        raise PushError("HUMAN_NEEDED broker event required")
    payload = {name: event.get(name) for name in NOTICE_FIELDS}
    if any(not isinstance(value, str) or not value or any(c.isspace() for c in value)
           for value in payload.values()) or payload["reason"] not in HUMAN_REASONS:
        raise PushError("invalid HUMAN_NEEDED metadata")
    _check_text(payload)
    return sink(payload)
