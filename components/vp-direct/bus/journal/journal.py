"""ViewPoint's Lineage journal, backed by an exclusively locked JSONL file."""

from contextlib import contextmanager
import fcntl
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

from jsonschema import ValidationError

from schemas.validation import SCHEMA_DIR, canonical_bytes, validate_contract


EVENT = "lineage.event/v1"
RUN = "lineage.run/v1"
HUMAN = "vp.human_lease/v1"
FORGE = "vp.forge_receipt/v1"
TRANSITION = "vp.transition_receipt/v1"
RUN_CLOSE = "run.close"
GENESIS = "0" * 64
FILENAME = "lineage.jsonl"
_FROZEN = {
    EVENT: SCHEMA_DIR / "lineage.event_v1.schema.json",
    RUN: SCHEMA_DIR / "lineage.run_v1.schema.json",
}
_METADATA = {"schema", "id", "lamport", "ts", "ledger", "prev_sha256"}
_REQUEST = {"agent", "op_id", "kind", "payload", "evidence"}
_HASH = re.compile(r"[a-f0-9]{64}")


class JournalError(ValueError):
    """A request or stored history that the journal cannot accept."""


def _digest(data):
    return sha256(data).hexdigest()


def _check_json(value):
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise JournalError("JSON object keys must be strings")
            _check_json(item)
    elif type(value) is list:
        for item in value:
            _check_json(item)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise JournalError("expected strict JSON")


def _request(agent, op_id, kind, payload, evidence):
    if type(agent) is not str or not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", agent):
        raise JournalError("invalid agent")
    if type(op_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", op_id):
        raise JournalError("invalid op_id")
    if type(kind) is not str or not kind or type(payload) is not dict:
        raise JournalError("invalid event kind or payload")
    if type(evidence) is not list:
        raise JournalError("evidence must be a list")
    for item in evidence:
        if (type(item) is not dict or set(item) != {"path", "sha256"}
                or type(item["path"]) is not str or type(item["sha256"]) is not str
                or not _HASH.fullmatch(item["sha256"])):
            raise JournalError("invalid evidence descriptor")
        path = item["path"]
        if ("\\" in path or "\0" in path or re.match(r"[A-Za-z]:", path)
                or any(part in ("", ".", "..") for part in path.split("/"))):
            raise JournalError("evidence path must be normalized and relative")
    return dict(agent=agent, op_id=op_id, kind=kind, payload=payload, evidence=evidence)


def _json_bytes(value):
    # Lineage's embedded and request digests use canonical JSON without framing.
    return canonical_bytes(value).removesuffix(b"\n")


def _sign(body):
    return dict(body, sha256=_digest(_json_bytes(body)))


def _require_frozen(schema):
    if not _FROZEN[schema].is_file():
        raise JournalError(f"SCHEMA_GAP: {schema} is absent from the frozen bundle")


def _validate_event(event):
    try:
        validate_contract(EVENT, event)
    except KeyError as error:
        raise JournalError(f"SCHEMA_GAP: {EVENT} is missing from the contract registry") from error


def _lineage_reference(event):
    return {
        "lamport": event["lamport"],
        "prev_hash": event["prev_sha256"],
        "event_hash": _digest(canonical_bytes(event)),
    }


def _read(file):
    records = []
    by_request = {}
    ids = set()
    head = GENESIS
    for position, line in enumerate(file, start=1):
        if not line.endswith(b"\n"):
            raise JournalError(f"JOURNAL_TORN at event {position}")
        try:
            event = json.loads(line)
            if (type(event) is not dict
                    or event.keys() != _METADATA | _REQUEST | {"sha256", "request_sha256"}
                    or event["schema"] != EVENT or event["ledger"] != "main"):
                raise JournalError("invalid Lineage event")
            if type(event["lamport"]) is not int or event["lamport"] != position:
                raise JournalError("noncontiguous lamport")
            if type(event["ts"]) is not int or event["ts"] < 0:
                raise JournalError("invalid timestamp")
            event_id = event["id"]
            if (type(event_id) is not str or not re.fullmatch(r"e-[0-9a-f]{16}", event_id)
                    or event_id in ids):
                raise JournalError("invalid or duplicate event id")
            request = _request(**{key: event[key] for key in _REQUEST})
            if (type(event["request_sha256"]) is not str
                    or not _HASH.fullmatch(event["request_sha256"])):
                raise JournalError("invalid request digest")
            if event["prev_sha256"] != head:
                raise JournalError("previous line SHA-256 mismatch")
            body = {key: value for key, value in event.items() if key != "sha256"}
            if canonical_bytes(_sign(body)) != line:
                raise JournalError("invalid embedded SHA-256 or noncanonical JSON")
            if event["kind"] == RUN_CLOSE:
                _require_frozen(RUN)
            _validate_event(event)
            if event["kind"] == TRANSITION and event["request_sha256"] != _digest(_json_bytes(request)):
                raise JournalError("transition request SHA-256 mismatch")
            key = (event["agent"], event["op_id"])
            if key in by_request:
                raise JournalError("duplicate agent/op_id")
        except (ValueError, ValidationError, RecursionError) as error:
            raise JournalError(f"event {position}: {error}") from error
        records.append(event)
        by_request[key] = event
        ids.add(event_id)
        head = _digest(line)  # The link to this event covers the complete line, including LF.
    return records, by_request, ids, head


class Journal:
    """Use an existing, caller-owned runtime directory for the dedicated journal."""

    def __init__(self, directory):
        self.directory = Path(directory)

    @contextmanager
    def _open(self, *, write=False):
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            flags = os.O_RDWR | os.O_APPEND | os.O_CREAT if write else os.O_RDONLY
            try:
                fd = os.open(FILENAME, flags | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=directory)
            except FileNotFoundError:
                if write:
                    raise
                yield None, directory
                return
            with os.fdopen(fd, "r+b" if write else "rb", buffering=0) as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise JournalError("Lineage requires a private regular journal file")
                fcntl.flock(file.fileno(), fcntl.LOCK_EX if write else fcntl.LOCK_SH)
                yield file, directory
        finally:
            os.close(directory)

    @staticmethod
    def _read(file):
        # Buffer only reads. An exception while writing must never cause a
        # buffered retry at close, since the first write may have taken effect.
        reader = io.BufferedReader(file)
        try:
            return _read(reader)
        finally:
            reader.detach()

    def verify(self):
        """Read the complete verified history, including after a process restart."""
        _require_frozen(EVENT)
        with self._open() as (file, _):
            return [] if file is None else self._read(file)[0]

    def append(self, *, agent, op_id, kind, payload, evidence=None):
        """Return a durable event, or the lineage reference for a receipt payload.

        Forge payloads omit lineage. Forge and transition writers receive
        lamport, prev_hash and event_hash after the line is durable. An
        identical replay returns the original result.
        """
        request = _request(agent, op_id, kind, payload, [] if evidence is None else evidence)
        try:
            _check_json(request)
            raw = _json_bytes(request)
        except (ValueError, RecursionError) as error:
            raise JournalError("request is not strict JSON") from error
        request = json.loads(raw)
        request_hash = _digest(raw)
        if kind not in (HUMAN, FORGE, TRANSITION, RUN_CLOSE):
            raise JournalError("unregistered event kind")
        if kind == FORGE and "lineage" in request["payload"]:
            raise JournalError("forge lineage.event_hash must be computed after append")
        if kind == RUN_CLOSE:
            _require_frozen(RUN)
        _require_frozen(EVENT)

        with self._open(write=True) as (file, directory):
            records, by_request, ids, head = self._read(file)
            prior = by_request.get((agent, op_id))
            if prior is not None:
                if prior["request_sha256"] != request_hash:
                    raise JournalError("REQUEST_ID_CONFLICT")
                # A prior writer may have failed before confirming durability.
                os.fsync(file.fileno())
                os.fsync(directory)
                event = prior
            else:
                event = self._write(file, directory, request, request_hash, len(records) + 1, ids, head)
            return _lineage_reference(event) if kind in (FORGE, TRANSITION) else event

    @staticmethod
    def _write(file, directory, request, request_hash, lamport, ids, head):
        event_id = "e-" + secrets.token_hex(8)
        while event_id in ids:
            event_id = "e-" + secrets.token_hex(8)
        body = dict(request, schema=EVENT, id=event_id, lamport=lamport,
                    ts=time.time_ns() // 1_000_000, ledger="main", prev_sha256=head,
                    request_sha256=request_hash)
        event = _sign(body)
        _validate_event(event)
        line = canonical_bytes(event)
        if file.write(line) != len(line):
            raise OSError("short Lineage write")
        os.fsync(file.fileno())
        os.fsync(directory)
        return json.loads(line)
