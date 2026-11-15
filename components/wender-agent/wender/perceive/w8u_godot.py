# @module {"id":"wa.perceive.w8u_godot","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.godot","wa.perceive.godot_window_target","wa.perceive.session_registry","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Authenticated, injected W8U capsule-pair perception for Godot.

This module consumes trusted capabilities, not a self-authenticating payload.
It neither defines the OpenSuperficies wire format nor opens a frame source.
See docs/spec/w8u_wa_godot.md for the frozen local capability contract and the
production wire declarations that remain AUSENTE in this integration wave.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, fields, is_dataclass
import hashlib
import re
import threading
import unicodedata

from . import godot
from .godot_window_target import GodotPrerequisiteError
from .session_registry import FrameRef, Handle, Observation
from ..state import schema


FRAME_METADATA_KEYS = frozenset({
    "seq", "tick", "tick_valid", "capture_mono_ns", "width", "height",
    "stride", "pixel_format", "sha256", "binding_sha256",
})
OUTCOME_KEYS = frozenset({
    "seq", "requested_tick", "applied_tick", "order_seq_id", "status",
    "reason", "state_sha256",
})
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REASON = re.compile(r"[A-Z][A-Z0-9_]*\Z")


class W8UGodotError(godot.GodotError):
    """An explicit refusal; no publication is implied by a protocol success."""

    def __init__(self, code: str, detail: str, rc: int = 5):
        self.code, self.detail, self.rc = code, detail, rc
        super().__init__(f"{code}: {detail}")


class W8UGodotUnavailable(GodotPrerequisiteError):
    """A required production/injected capability has not been supplied."""

    code = None
    rc = 4

    def __init__(self, detail: str):
        super().__init__(f"AUSENTE: {detail}")


@dataclass(frozen=True)
class CapsuleBinding:
    run_id: str
    lease_id: str
    session_id: str
    stream_id: str
    target_handle: Handle
    epoch: int


@dataclass(frozen=True)
class VerifiedCapsulePair:
    """Trusted verifier output, never constructed from payload flags here."""

    binding: CapsuleBinding
    base: dict
    sidecar: dict
    frame_metadata: dict
    left_rgb: bytes
    right_rgb: bytes
    left_grab_id: str
    right_grab_id: str
    xwd_subset: object


@dataclass(frozen=True)
class CommittedPresentation:
    binding: CapsuleBinding
    presentation_id: str
    grab_id: str
    frame_seq: int
    frame_sha256: str
    binding_sha256: str
    basis_sha256: str
    action_id: str
    action_seq: int
    requested_tick: int
    applied_tick: int
    outcome_sha256: str
    committed: bool


@dataclass(frozen=True)
class GodotPerceptionResult:
    observation: Observation
    frame_metadata: dict
    basis_sha256: str
    outcome: dict | None
    outcome_sha256: str | None
    presentation_id: str | None
    tick_bound: bool


def _bad(detail):
    raise W8UGodotError("E_SCHEMA", detail, 2)


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        _bad(f"{name} must be an exact integer >= {minimum}")
    return value


def _utf8(value, name):
    if type(value) is not str:
        _bad(f"{name} must be a UTF-8 string")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _bad(f"{name} must be valid UTF-8")
    return value


def _token(value, name):
    _utf8(value, name)
    if (not value or value != value.strip()
            or any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in value)):
        _bad(f"{name} must be a nonempty canonical string")
    return value


def _digest(value, name):
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        _bad(f"{name} must be lowercase SHA-256 hex")
    return value


def _json_value(value, name):
    """Reject coercions and invalid Unicode before touching legacy validators."""
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        _utf8(value, name)
        return
    if type(value) is list:
        for item in value:
            _json_value(item, name)
        return
    if type(value) is dict:
        for key, item in value.items():
            _utf8(key, name + ".key")
            _json_value(item, name + "." + key)
        return
    _bad(f"{name} contains a non-JSON value or float")


def _json_bytes(value, name):
    try:
        _json_value(value, name)
        return schema.dumps(value).encode("utf-8", "strict")
    except (RecursionError, TypeError, ValueError, UnicodeError) as exc:
        if isinstance(exc, W8UGodotError):
            raise
        _bad(f"{name} cannot be serialized as canonical integer JSON")


def _binding_key(binding):
    if type(binding) is not CapsuleBinding:
        _bad("binding must be CapsuleBinding")
    names = ("run_id", "lease_id", "session_id", "stream_id")
    values = tuple(_token(getattr(binding, name), name) for name in names)
    if binding.run_id in (".", "..") or "/" in binding.run_id or "\\" in binding.run_id:
        _bad("run_id must be a single canonical path component")
    handle = binding.target_handle
    required = {"kind", "canonical_id", "generation"}
    if type(handle) is dict:
        parts = handle
    elif is_dataclass(handle) and not isinstance(handle, type):
        parts = {f.name: getattr(handle, f.name) for f in fields(handle)}
    else:
        _bad("target_handle must have the exact handle fields")
    if set(parts) != required:
        _bad("target_handle must have the exact handle fields")
    kind = _token(parts["kind"], "target_handle.kind")
    canonical_id = _token(parts["canonical_id"], "target_handle.canonical_id")
    generation = _integer(parts["generation"], "target_handle.generation", 1)
    if kind != "godot_window":
        raise W8UGodotError("E_STALE_FRAME", "target is not a godot_window")
    epoch = _integer(binding.epoch, "epoch")
    return values + (kind, canonical_id, generation, epoch)


def _same_binding(expected, actual):
    actual_key = _binding_key(actual)
    if expected[:4] != actual_key[:4]:
        raise W8UGodotError("E_LOCK", "authenticated run/lease/session/stream mismatch")
    if expected[4:7] != actual_key[4:7]:
        raise W8UGodotError("E_STALE_FRAME", "target identity or generation mismatch")
    if expected[7] != actual_key[7]:
        raise W8UGodotError("E_EPOCH", "geometry epoch mismatch")


def _closed_dict(value, keys, name):
    if type(value) is not dict or set(value) != set(keys):
        _bad(f"{name} has unknown or missing keys")


def _snapshot(value, name):
    try:
        return copy.deepcopy(value)
    except (TypeError, ValueError, RecursionError):
        _bad(f"{name} cannot be copied as a closed evidence value")


def _state_basis(base, sidecar, registries):
    _json_bytes(base, "STATE")
    _json_bytes(sidecar, "sidecar")
    _closed_dict(base, schema.TOP_KEYS, "STATE")
    _closed_dict(base["source"], ("kind", "url", "title"), "STATE.source")
    _closed_dict(base["prune"], ("name", "version"), "STATE.prune")
    _closed_dict(base["counts"], ("raw", "nodes", "actionable"), "STATE.counts")
    for key in ("raw", "nodes", "actionable"):
        _integer(base["counts"][key], "STATE.counts." + key)
    _integer(base["prune"]["version"], "STATE.prune.version", 1)
    for key in ("ax_sha256", "ax_stable_sha256"):
        _digest(base[key], "STATE." + key)
    if type(base["nodes"]) is not list or type(base["actionable"]) is not list:
        _bad("STATE nodes and actionable must be arrays")
    if not all(type(nid) is str for nid in base["actionable"]):
        _bad("STATE actionable IDs must be strings")
    viewport = base["viewport"]
    if viewport is not None:
        if type(viewport) is not dict:
            _bad("STATE viewport must be an object or null")
        if any(type(v) is not int for v in viewport.values()):
            _bad("STATE viewport values must be exact integers")
    ticks = set()
    for node in base["nodes"]:
        _closed_dict(node, schema.NODE_KEYS, "STATE node")
        if type(node["id"]) is not str or not re.fullmatch(r"[0-9a-f]{16}", node["id"]):
            _bad("STATE node id must be lowercase hex16")
        if node["parent"] is not None and type(node["parent"]) is not str:
            _bad("STATE node parent must be a string or null")
        _integer(node["depth"], "STATE node depth")
        _integer(node["ordinal"], "STATE node ordinal")
        if type(node["actionable"]) is not bool:
            _bad("STATE node actionable must be bool")
        if type(node["states"]) is not dict or not all(type(v) is str for v in node["states"].values()):
            _bad("STATE node states must map strings to strings")
        box = node["box"]
        if box is not None and not (
            type(box) is list and len(box) == 4 and all(type(v) is int for v in box)
            and box[2] >= 0 and box[3] >= 0
        ):
            _bad("STATE node box must be exact integer pixels")
        tick = node["states"].get("tick")
        if tick is not None:
            if not tick or not tick.isascii() or not tick.isdecimal():
                _bad("STATE tick must be an ASCII nonnegative integer string")
            try:
                ticks.add(int(tick))
            except ValueError:
                _bad("STATE tick integer is invalid")
    if len(ticks) > 1:
        raise W8UGodotError("E_STALE_FRAME", "conflicting STATE ticks")
    try:
        errors = schema.validate_state(base)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        _bad("STATE validator rejected malformed structure: " + type(exc).__name__)
    if type(errors) is not list or errors != []:
        _bad("STATE validation did not return an empty error list")

    _closed_dict(sidecar, godot.SIDECAR_TOP_KEYS, "sidecar")
    _digest(sidecar["base_sha256"], "sidecar.base_sha256")
    if type(sidecar["nodes"]) is not dict:
        _bad("sidecar nodes must be an object")
    for node_id, entry in sidecar["nodes"].items():
        if type(entry) is not dict:
            _bad("sidecar node must be an object")
        path = entry.get("control_path")
        # Grammar and root are checked BEFORE literal hashing. No normalization.
        if godot.is_valid_control_path(path) is not True:
            _bad("invalid literal control_path")
        if "/" in path and path.split("/", 1)[0] != entry.get("screen_id"):
            _bad("slash control_path must be rooted at screen_id")
        if godot.node_id_for_control_path(path) != node_id:
            _bad("control_path literal hash does not match node identity")
    basis = hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest()
    if sidecar["base_sha256"] != basis:
        raise W8UGodotError("E_STALE_FRAME", "base/sidecar canonical basis mismatch")
    try:
        errors = godot.validate_uitree(base, sidecar, registries)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        _bad("Godot validator rejected malformed structure: " + type(exc).__name__)
    if type(errors) is not list or errors != []:
        _bad("Godot pair validation did not return an empty error list")
    tick = next(iter(ticks), None)
    if tick is not None and any(
        "as_of_tick" in entry and entry["as_of_tick"] > tick
        for entry in sidecar["nodes"].values()
    ):
        raise W8UGodotError("E_STALE_FRAME", "sidecar summary is ahead of STATE tick")
    return basis, tick


def _json_line(raw, name):
    if type(raw) is not bytes or not raw.endswith(b"\n"):
        _bad(f"{name} must be canonical UTF-8 JSON-L bytes including LF")
    try:
        record = godot.parse_canonical_json_line(raw[:-1])
    except (godot.GodotError, UnicodeError, RecursionError):
        _bad(f"{name} is not strict integer UTF-8 JSON-L")
    if _json_bytes(record, name) + b"\n" != raw:
        _bad(f"{name} is not canonical JSON-L")
    return record


def _outcome_record(raw):
    record = _json_line(raw, "outcome")
    _closed_dict(record, OUTCOME_KEYS, "outcome")
    for key in ("seq", "order_seq_id"):
        _integer(record[key], "outcome." + key, 1)
    for key in ("requested_tick", "applied_tick"):
        _integer(record[key], "outcome." + key)
    if type(record["status"]) is not str or record["status"] not in ("accepted", "rejected", "proposed"):
        _bad("outcome status must be accepted, rejected or proposed")
    reason = record["reason"]
    if reason is not None and (type(reason) is not str or _REASON.fullmatch(reason) is None):
        _bad("outcome reason must be an uppercase code or null")
    _digest(record["state_sha256"], "outcome.state_sha256")
    return record


class W8UGodotPerception:
    """One binding, one pinned verifier, monotonic publication, no I/O source.

    Authority is injected by the caller through the capability object. The
    capability must authenticate pair/outcome/presentation evidence and decode
    its revision's common XWD subset to canonical RGB. A test double supplies
    independent TARGET vectors; that is not production contract acceptance.
    """

    def __init__(self, binding, *, contract, contract_revision, registries,
                 command_validator, publish=None):
        self._key = _binding_key(binding)
        self._binding = copy.deepcopy(binding)
        if contract is None:
            raise W8UGodotUnavailable("revision-pinned w8u-os-contract capability")
        if type(contract_revision) is not str or _REVISION.fullmatch(contract_revision) is None:
            raise W8UGodotUnavailable("w8u-os-contract immutable revision pin")
        self._contract, self._revision = contract, contract_revision
        for method in ("current_binding", "verify_pair", "verify_outcome", "verify_presentation"):
            if not callable(getattr(contract, method, None)):
                raise W8UGodotUnavailable("w8u-os-contract." + method)
        if getattr(contract, "xwd_subset", None) is None:
            raise W8UGodotUnavailable("w8u-os-contract common XWD subset")
        if getattr(contract, "pixel_format", None) is None:
            raise W8UGodotUnavailable("w8u-os-contract canonical pixel format")
        self._subset = _json_bytes(contract.xwd_subset, "contract XWD subset")
        self._format = _token(contract.pixel_format, "contract pixel_format")
        if type(registries) is not dict:
            _bad("Godot registries must be an injected dict")
        self._registries = copy.deepcopy(registries)
        if not callable(command_validator):
            raise W8UGodotUnavailable("closed producer command validator")
        if publish is not None and not callable(publish):
            _bad("publish must be callable or null")
        self._command_validator, self._publish = command_validator, publish
        self._last_seq, self._last_tick = 0, None
        self._quarantined = False
        self._mutex = threading.Lock()
        self._check_contract()

    @property
    def quarantined(self):
        return self._quarantined

    @property
    def published_frame_seq(self):
        return self._last_seq

    def _check_contract(self):
        if (getattr(self._contract, "contract_name", None) != "w8u-os-contract"
                or getattr(self._contract, "revision", None) != self._revision):
            raise W8UGodotError("E_LOCK", "w8u-os-contract revision/name mismatch")
        if (getattr(self._contract, "pixel_format", None) != self._format
                or _json_bytes(getattr(self._contract, "xwd_subset", None), "contract XWD subset") != self._subset):
            raise W8UGodotError("E_LOCK", "pinned contract declarations changed")

    def _call(self, method, *args):
        callback = getattr(self._contract, method, None)
        if not callable(callback):
            raise W8UGodotUnavailable("w8u-os-contract." + method)
        try:
            return callback(*args)
        except (W8UGodotError, W8UGodotUnavailable):
            raise
        except NotImplementedError:
            raise W8UGodotUnavailable("w8u-os-contract." + method) from None
        except Exception:
            # Authentication details may contain transport data; never echo them.
            raise W8UGodotError("E_LOCK", "authentication capability refused " + method) from None

    def _current(self):
        self._check_contract()
        _same_binding(self._key, self._call("current_binding"))

    def _frame(self, pair):
        metadata = pair.frame_metadata
        _closed_dict(metadata, FRAME_METADATA_KEYS, "frame metadata")
        _json_bytes(metadata, "frame metadata")
        for key in ("seq", "width", "height", "stride"):
            _integer(metadata[key], "frame." + key, 1)
        _integer(metadata["capture_mono_ns"], "frame.capture_mono_ns")
        if type(metadata["tick_valid"]) is not bool:
            _bad("frame.tick_valid must be bool")
        if metadata["tick_valid"]:
            _integer(metadata["tick"], "frame.tick")
        elif metadata["tick"] is not None:
            _bad("invalid frame tick must be null")
        if metadata["stride"] != metadata["width"] * 3:
            _bad("canonical RGB stride must equal width * 3")
        if type(metadata["pixel_format"]) is not str or metadata["pixel_format"] != self._format:
            _bad("frame pixel format differs from pinned canonical RGB format")
        _digest(metadata["sha256"], "frame.sha256")
        _digest(metadata["binding_sha256"], "frame.binding_sha256")
        if metadata["seq"] <= self._last_seq:
            raise W8UGodotError("E_STALE_FRAME", "frame sequence was already published or is stale")
        _token(pair.left_grab_id, "left grab ID")
        _token(pair.right_grab_id, "right grab ID")
        if pair.left_grab_id != pair.right_grab_id:
            raise W8UGodotError("E_STALE_FRAME", "canonical pair is not from the same grab")
        if _json_bytes(pair.xwd_subset, "pair XWD subset") != self._subset:
            raise W8UGodotError("E_STALE_FRAME", "pair common XWD subset differs from pin")
        if type(pair.left_rgb) is not bytes or type(pair.right_rgb) is not bytes:
            _bad("verified canonical RGB pair must be immutable RAM bytes")
        if pair.left_rgb != pair.right_rgb:
            raise W8UGodotError("E_STALE_FRAME", "same-grab canonical pair bytes differ")
        if len(pair.left_rgb) != metadata["stride"] * metadata["height"]:
            _bad("canonical RGB byte count disagrees with frame geometry")
        if hashlib.sha256(pair.left_rgb).hexdigest() != metadata["sha256"]:
            raise W8UGodotError("E_STALE_FRAME", "canonical frame digest mismatch")
        # NumPy is used only to enter the existing immutable FrameRef API.
        import numpy as np
        pixels = np.frombuffer(pair.left_rgb, dtype=np.uint8).reshape(
            metadata["height"], metadata["width"], 3)
        handle = Handle(*self._key[4:7])
        return FrameRef(handle, self._key[7], metadata["seq"], metadata["width"],
                        metadata["height"], pixels, metadata["sha256"])

    def _correlate(self, pair, basis, tick, command, action_id, outcome, presentation):
        if any(value is None for value in (command, action_id, outcome, presentation)):
            raise W8UGodotError("E_UNCERTAIN", "tick-bound frame lacks authenticated outcome/presentation", 6)
        _token(action_id, "action_id")
        request = _json_line(command, "command")
        try:
            godot.validate_inbox_record(request)
        except (godot.GodotError, ValueError, TypeError, KeyError):
            _bad("command must have the exact closed T2 fields and integer values")
        try:
            errors = self._command_validator(copy.deepcopy(request))
        except Exception:
            raise W8UGodotError("E_VERB", "closed producer command validator refused") from None
        if type(errors) is not list or errors != []:
            raise W8UGodotError("E_VERB", "closed producer command schema refused")
        record = _outcome_record(outcome)
        authenticated = self._call("verify_outcome", outcome, copy.deepcopy(self._binding))
        if type(authenticated) is not bytes or authenticated != outcome:
            raise W8UGodotError("E_LOCK", "outcome authentication did not bind exact bytes")
        if record["status"] != "accepted":
            raise W8UGodotError("E_UNCERTAIN", "outcome does not establish accepted T2 effect", 6)
        if (record["seq"] != request["seq"]
                or record["requested_tick"] != request["tick"]
                or record["applied_tick"] < record["requested_tick"]
                or record["state_sha256"] != basis
                or tick is None or record["applied_tick"] != tick
                or pair.frame_metadata["tick"] != tick):
            raise W8UGodotError("E_STALE_FRAME", "command/outcome/STATE/frame tick or basis mismatch")
        if self._last_tick is not None and tick < self._last_tick:
            raise W8UGodotError("E_STALE_FRAME", "committed tick rollback")
        proof = self._call("verify_presentation", presentation, copy.deepcopy(self._binding))
        if proof is None:
            raise W8UGodotError("E_UNCERTAIN", "committed presentation AUSENTE", 6)
        if type(proof) is not CommittedPresentation:
            _bad("presentation verifier must return CommittedPresentation")
        proof = _snapshot(proof, "presentation")
        _same_binding(self._key, proof.binding)
        if proof.committed is not True:
            raise W8UGodotError("E_UNCERTAIN", "presentation is not committed", 6)
        for name in ("presentation_id", "grab_id", "action_id"):
            _token(getattr(proof, name), "presentation." + name)
        for name in ("frame_seq", "action_seq"):
            _integer(getattr(proof, name), "presentation." + name, 1)
        for name in ("requested_tick", "applied_tick"):
            _integer(getattr(proof, name), "presentation." + name)
        for name in ("frame_sha256", "binding_sha256", "basis_sha256", "outcome_sha256"):
            _digest(getattr(proof, name), "presentation." + name)
        outcome_sha256 = hashlib.sha256(outcome).hexdigest()
        expected = (
            pair.left_grab_id, pair.frame_metadata["seq"], pair.frame_metadata["sha256"],
            pair.frame_metadata["binding_sha256"], basis, action_id, request["seq"],
            request["tick"], record["applied_tick"], outcome_sha256,
        )
        actual = (
            proof.grab_id, proof.frame_seq, proof.frame_sha256, proof.binding_sha256,
            proof.basis_sha256, proof.action_id, proof.action_seq, proof.requested_tick,
            proof.applied_tick, proof.outcome_sha256,
        )
        if actual != expected:
            raise W8UGodotError("E_STALE_FRAME", "committed presentation does not correlate with exact evidence")
        return record, outcome_sha256, proof.presentation_id

    def perceive(self, capsule, *, command=None, action_id=None,
                 outcome=None, presentation=None):
        if not self._mutex.acquire(blocking=False):
            raise W8UGodotError("E_LOCK", "perception/publication already in progress")
        try:
            if self._quarantined:
                raise W8UGodotError("E_UNCERTAIN", "publication acknowledgement lost; adapter quarantined", 6)
            self._current()
            pair = self._call("verify_pair", capsule, copy.deepcopy(self._binding))
            if type(pair) is not VerifiedCapsulePair:
                _bad("pair verifier must return VerifiedCapsulePair")
            pair = _snapshot(pair, "pair")
            _same_binding(self._key, pair.binding)
            basis, tick = _state_basis(pair.base, pair.sidecar, self._registries)
            if self._last_tick is not None and tick is not None and tick < self._last_tick:
                raise W8UGodotError("E_STALE_FRAME", "STATE predates the last committed tick")
            frame = self._frame(pair)
            tick_bound = pair.frame_metadata["tick_valid"]
            if tick_bound:
                record, outcome_sha256, presentation_id = self._correlate(
                    pair, basis, tick, command, action_id, outcome, presentation)
            else:
                if any(value is not None for value in (command, action_id, outcome, presentation)):
                    _bad("unticked frame cannot carry an inferred T2/presentation join")
                record = outcome_sha256 = presentation_id = None
            viewport = pair.base["viewport"] or {}
            dpr = _integer(viewport.get("dpr_ppm", 1000000), "dpr_ppm", 1)
            observation = Observation(frame.handle, frame.epoch, frame.frame_seq,
                                      dpr, copy.deepcopy(pair.base), frame)
            result = GodotPerceptionResult(
                observation, copy.deepcopy(pair.frame_metadata), basis,
                copy.deepcopy(record), outcome_sha256, presentation_id, tick_bound)
            # Recheck live authority after all injected validation, before publication.
            self._current()
            self._last_seq = frame.frame_seq
            if tick_bound:
                self._last_tick = tick
            if self._publish is not None:
                try:
                    self._publish(result)
                except Exception:
                    self._quarantined = True
                    raise W8UGodotError("E_UNCERTAIN", "publication acknowledgement lost; adapter quarantined", 6) from None
                except BaseException:
                    # Cancellation can follow an emitted publication too. Keep
                    # cancellation semantics while preventing a new-ID retry.
                    self._quarantined = True
                    raise
            return result
        finally:
            self._mutex.release()


__all__ = (
    "CapsuleBinding", "VerifiedCapsulePair", "CommittedPresentation",
    "GodotPerceptionResult", "W8UGodotPerception", "W8UGodotError",
    "W8UGodotUnavailable", "FRAME_METADATA_KEYS", "OUTCOME_KEYS",
)
