"""Protocol approvals and held-out exposures bound to an external trusted head."""

import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from fcntl import LOCK_EX, flock
from pathlib import Path
from typing import Protocol

from .common import CalibrationError, digest, encode, parse, require
from .pcm import MAX_CORRELATION_TERMS, MAX_DECODED_SAMPLES, compare_pcm, decode_fingerprint, encode_fingerprint


_HEADER = {"schema": "af.calibration-exposure-ledger/v2"}
_PIN = re.compile(r"sha256:[0-9a-f]{64}")


def _is_pin(value):
    return type(value) is str and _PIN.fullmatch(value) is not None


class ExposureAnchor(Protocol):
    """Implemented by the approver's trusted store, outside evaluator write access.

    Bind the canonical path to a stable ledger identity. Only a never-provisioned
    identity may return None. An unavailable anchor must raise OSError. Advance
    must durably compare and swap the head, returning False on a stale predecessor.
    """

    def read(self, path: Path) -> str | None: ...

    def advance(self, path: Path, previous: str | None, head: str) -> bool: ...


@dataclass
class _State:
    head: str
    approved: set = field(default_factory=set)
    exposed: dict = field(default_factory=dict)
    fingerprints: dict = field(default_factory=dict)
    completed: dict = field(default_factory=dict)


class ExposureLedger:
    """Use one ledger and the same independently pinned anchor across runs."""

    def __init__(self, path, *, anchor: ExposureAnchor):
        path = Path(path).absolute()
        self.path = path.parent.resolve() / path.name
        self._anchor = anchor

    @classmethod
    def initialize(cls, path, *, anchor: ExposureAnchor):
        """Provision a new identity without overwriting anchored history."""
        ledger = cls(path, anchor=anchor)
        require(ledger._read_head() is None, 4, "exposure_ledger_history_exists")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            with os.fdopen(os.open(ledger.path, flags, 0o600), "wb", buffering=0) as file:
                flock(file.fileno(), LOCK_EX)
                parent = os.open(ledger.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
                ledger._append(file, None, _HEADER)
        except OSError as exc:
            raise CalibrationError(3, "exposure_ledger_unavailable") from exc
        return ledger

    def _read_head(self):
        require(callable(getattr(self._anchor, "read", None))
                and callable(getattr(self._anchor, "advance", None)), 3, "exposure_anchor_unavailable")
        try:
            head = self._anchor.read(self.path)
        except OSError as exc:
            raise CalibrationError(3, "exposure_anchor_unavailable") from exc
        require(head is None or _is_pin(head), 3, "invalid_exposure_anchor")
        return head

    def _append(self, file, previous, entry):
        raw = encode({**entry, "previous": previous})
        require(file.write(raw) == len(raw), 3, "exposure_ledger_short_write")
        file.flush()
        os.fsync(file.fileno())
        head = digest(raw)
        try:
            advanced = self._anchor.advance(self.path, previous, head)
        except OSError as exc:
            raise CalibrationError(3, "exposure_anchor_unavailable") from exc
        require(advanced is True and self._read_head() == head, 4, "exposure_anchor_changed")

    @contextmanager
    def _locked(self):
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            with os.fdopen(os.open(self.path, flags), "r+b") as file:
                require(stat.S_ISREG(os.fstat(file.fileno()).st_mode), 3, "invalid_exposure_ledger")
                flock(file.fileno(), LOCK_EX)
                yield file
        except OSError as exc:
            raise CalibrationError(3, "exposure_ledger_unavailable") from exc

    def _state(self, file):
        expected = self._read_head()
        require(expected is not None, 3, "exposure_anchor_unavailable")
        file.seek(0)
        raw = file.readline()
        require(raw == encode({**_HEADER, "previous": None}), 3, "invalid_exposure_ledger")
        state = _State(digest(raw))
        for raw in file:
            try:
                event = parse(raw)
            except CalibrationError as exc:
                raise CalibrationError(3, "invalid_exposure_ledger") from exc
            require(event.pop("previous", None) == state.head, 3, "broken_exposure_chain")
            state.head = digest(raw)
            self._record(state, event)
        require(state.head == expected, 4, "exposure_ledger_head_changed")
        return state

    @staticmethod
    def _record(state, event):
        reason = "invalid_exposure_ledger"
        pin = event.get("protocol_digest")
        require(_is_pin(pin), 3, reason)
        if event.keys() == {"protocol_digest"}:
            require(pin not in state.approved, 3, reason)
            state.approved.add(pin)
            return
        if event.keys() == {"protocol_digest", "witness_digest"}:
            witness = event["witness_digest"]
            require(pin in state.fingerprints and pin not in state.completed and _is_pin(witness), 3, reason)
            state.completed[pin] = witness
            return
        held_out = event.get("held_out_digests")
        fingerprints = event.get("held_out_pcm")
        require(event.keys() == {"protocol_digest", "held_out_digests", "held_out_pcm"}
                and pin in state.approved and pin not in state.fingerprints
                and type(held_out) is list and held_out and all(map(_is_pin, held_out))
                and held_out == sorted(set(held_out)) and state.exposed.keys().isdisjoint(held_out)
                and type(fingerprints) is dict and fingerprints and fingerprints.keys() <= set(held_out), 3, reason)
        remaining = MAX_DECODED_SAMPLES
        decoded = []
        for fingerprint in fingerprints.values():
            pcm, remaining = decode_fingerprint(fingerprint, remaining)
            decoded.append(pcm)
        state.exposed.update(dict.fromkeys(held_out, pin))
        state.fingerprints[pin] = decoded

    def approve(self, raw):
        """Record an independent owner's approval of the canonical protocol bytes."""
        parse(raw)
        pin = digest(raw)
        with self._locked() as file:
            state = self._state(file)
            if pin not in state.approved:
                self._append(file, state.head, {"protocol_digest": pin})
        return pin

    def _check(self, protocol_pin, lineage, *, load_fingerprints, expose=None, held_out_audio=()):
        """Check approval and both splits, then optionally reserve the held-out evidence."""
        with self._locked() as file:
            state = self._state(file)
            prior = {state.exposed[key] for key in lineage if key in state.exposed}
            require(not prior - {protocol_pin}, 4, "held_out_protocol_changed")
            require(protocol_pin in state.approved, 4, "protocol_digest_changed")
            if expose is not None:
                require(protocol_pin not in state.fingerprints, 4, "evaluation_already_started")

            current = load_fingerprints()
            remaining = MAX_CORRELATION_TERMS
            for owner, exposed in state.fingerprints.items():
                if owner != protocol_pin:
                    for previous in exposed:
                        for candidate in current.values():
                            matched, remaining = compare_pcm(previous, candidate, remaining)
                            require(not matched, 4, "exposed_held_out_pcm_match")

            if expose is not None:
                held_out_pcm = {pin: encode_fingerprint(current[pin]) for pin in held_out_audio}
                self._append(file, state.head, {"protocol_digest": protocol_pin, "held_out_digests": sorted(expose),
                                                "held_out_pcm": held_out_pcm})
                self._state(file)
            return current

    def _seal(self, protocol_pin, witness_pin):
        with self._locked() as file:
            state = self._state(file)
            require(protocol_pin in state.fingerprints and protocol_pin not in state.completed,
                    4, "invalid_exposure_completion")
            self._append(file, state.head, {"protocol_digest": protocol_pin, "witness_digest": witness_pin})

    def _witness_pin(self, protocol_pin):
        with self._locked() as file:
            state = self._state(file)
            require(protocol_pin in state.completed, 3, "recorded_witness_unavailable")
            return state.completed[protocol_pin]
