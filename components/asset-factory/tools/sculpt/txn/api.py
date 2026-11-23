"""Snapshot -> apply -> measure -> commit/revert, without an undo stack.

Callbacks are local trusted code, must honour their absolute monotonic deadline,
and may mutate only the owned target. Use an outer process timeout as well: Python
cannot preempt a blocked synchronous callback. A checkpoint must cover all state
the apply callback can change. A failed cleanup fences this surface in-process;
discard the scratch scene/process rather than bypassing the fence.

measure(target, deadline_ns) returns {rc, state_sha256, metrics, reason_codes}.
metrics maps names to integers; state_sha256 binds the measured input bytes.
decide(before_metrics, after_metrics) returns {rc, keep: bool, reason_codes}.
Only rc=0/keep=True can commit; empty metrics and no-op strokes cannot pass.
commit=False trials are always restored, including successful candidates.
Receipt PASS proves this transaction contract only, never asset appearance.
"""

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import math
import struct
import threading
import time
from typing import Protocol

from asset_factory import core


@lru_cache(maxsize=2)
def _finite_vertices(vertices):
    # Immutable bytes are the cache key, compared in full. Retain only the two
    # current geometry buffers, never a history of trial meshes. Re-reading an
    # unchanged buffer does not need another Python-level scan of every float.
    if any(not math.isfinite(v[0]) for v in struct.iter_unpack("<f", vertices)):
        raise core.ContractError("non_finite_vertex")


@dataclass(frozen=True)
class MeshState:
    """Exact little-endian IEEE binary32 XYZ and topology/context bytes.

    Coordinates are never quantised for the rollback oracle. JSON exposes only
    digests and integer counts. Equality checks the bytes, not only their hashes.
    """

    vertices: bytes
    topology: bytes

    def __post_init__(self):
        if (type(self.vertices) is not bytes or not self.vertices
                or len(self.vertices) % 12 or type(self.topology) is not bytes
                or not self.topology):
            raise core.ContractError("mesh_state_invalid")
        _finite_vertices(self.vertices)

    def evidence(self):
        return {
            "vertex_sha256": hashlib.sha256(self.vertices).hexdigest(),
            "topology_sha256": hashlib.sha256(self.topology).hexdigest(),
            "state_sha256": hashlib.sha256(
                struct.pack("<QQ", len(self.vertices), len(self.topology))
                + self.vertices + self.topology).hexdigest(),
            "vertex_count": len(self.vertices) // 12,
        }


@dataclass(frozen=True)
class Checkpoint:
    state: MeshState
    token: object


class Target(Protocol):
    identity: str

    def read(self) -> MeshState: ...
    def snapshot(self) -> Checkpoint: ...
    def restore(self, checkpoint: Checkpoint) -> None: ...
    def discard(self, checkpoint: Checkpoint) -> None: ...


class _Stop(Exception):
    def __init__(self, rc, reason):
        self.rc, self.reason = rc, reason


class _Gate:
    def __init__(self):
        self.lock = threading.Lock()
        self.fenced = False


_GATES = {}
_REGISTRY_LOCK = threading.Lock()


def _check_deadline(deadline):
    if time.monotonic_ns() >= deadline:
        raise _Stop(6, "deadline_exceeded")


def _checked_record(value, *, decision=False):
    core.canonical_json(value)
    field = "keep" if decision else "metrics"
    keys = {"rc", field, "reason_codes"} | (set() if decision else {"state_sha256"})
    if (type(value) is not dict or set(value) != keys
            or type(value["rc"]) is not int or value["rc"] not in core.RC
            or type(value["reason_codes"]) is not list
            or any(type(s) is not str or not s for s in value["reason_codes"])):
        raise core.ContractError("callback_contract")
    if decision:
        if type(value[field]) is not bool or value[field] != (value["rc"] == 0):
            raise core.ContractError("callback_contract")
    elif (type(value[field]) is not dict
          or any(type(k) is not str or type(v) is not int for k, v in value[field].items())):
        raise core.ContractError("callback_contract")
    elif value["rc"] == 0 and not value[field]:
        raise _Stop(3, "measurement_empty")
    if value["rc"] != 0 and not value["reason_codes"]:
        raise core.ContractError("callback_contract")
    # Detach the caller's mutable dictionaries before they can change evidence.
    import json
    return json.loads(core.canonical_json(value))


class Transaction:
    """One in-process actor per stable target identity, including across handles."""

    def __init__(self, target: Target):
        if type(target.identity) is not str or not target.identity:
            raise core.ContractError("target_identity_invalid")
        self.target = target
        with _REGISTRY_LOCK:
            self._gate = _GATES.setdefault(target.identity, _Gate())
        # Retained on failed cleanup for diagnosis; never silently released.
        self.pending_checkpoint = None

    def run(self, candidate_id, apply, measure, decide, *, commit,
            timeout_s, cleanup_timeout_s):
        started = time.monotonic_ns()
        receipt = {"schema": "af.sculpt-stroke/v1", "candidate_id": candidate_id,
                   "rc": 2, "status": "ERROR", "verdict": "ERROR",
                   "operation_rc": 2, "cleanup_rc": 0, "action": "REFUSE",
                   "restored": None, "fenced": self._gate.fenced,
                   "before": None, "proposed": None, "final": None,
                   "measurements": {"before": None, "after": None},
                   "reason_codes": [], "wall_ns": 0}
        if (type(candidate_id) is not str or not candidate_id or type(commit) is not bool
                or type(timeout_s) is not int or timeout_s <= 0
                or type(cleanup_timeout_s) is not int or cleanup_timeout_s <= 0):
            raise core.ContractError("transaction_invalid")
        if not self._gate.lock.acquire(blocking=False):
            return self._finish(receipt, 5, ["actor_busy"], started)
        checkpoint = before = proposed = None
        operation_rc, cleanup_rc, reasons = 0, 0, []
        keep = False
        try:
            if self._gate.fenced:
                return self._finish(receipt, 3, ["surface_fenced"], started)
            deadline = started + timeout_s * 1_000_000_000
            try:
                _check_deadline(deadline)
                before = self.target.read()
                receipt["before"] = before.evidence()
                candidate_checkpoint = self.target.snapshot()
                self.pending_checkpoint = candidate_checkpoint
                if (not isinstance(candidate_checkpoint, Checkpoint)
                        or candidate_checkpoint.state != before):
                    self._gate.fenced = True
                    raise _Stop(4, "snapshot_stale")
                checkpoint = candidate_checkpoint
                if self.target.read() != before:
                    raise _Stop(4, "snapshot_mutated_state")
                baseline = self._observe(measure, before, deadline)
                receipt["measurements"]["before"] = baseline
                if baseline["rc"]:
                    raise _Stop(baseline["rc"], baseline["reason_codes"][0])
                _check_deadline(deadline)
                apply(self.target, deadline)
                _check_deadline(deadline)
                proposed = self.target.read()
                receipt["proposed"] = proposed.evidence()
                if proposed.topology != before.topology:
                    raise _Stop(5, "topology_or_context_changed")
                after = self._observe(measure, proposed, deadline)
                receipt["measurements"]["after"] = after
                if after["rc"]:
                    raise _Stop(after["rc"], after["reason_codes"][0])
                decision = _checked_record(decide(dict(baseline["metrics"]), dict(after["metrics"])),
                                           decision=True)
                _check_deadline(deadline)
                if self.target.read() != proposed:
                    raise _Stop(4, "decision_mutated_state")
                if decision["rc"]:
                    reasons.extend(decision["reason_codes"])
                    raise _Stop(decision["rc"], decision["reason_codes"][0])
                if proposed.vertices == before.vertices:
                    raise _Stop(5, "no_op_stroke")
                keep = commit
            except _Stop as exc:
                operation_rc = exc.rc
                reasons.append(exc.reason)
            except core.ContractError as exc:
                operation_rc = 2
                reasons.append(exc.code)
            except BaseException as exc:
                # Includes cancellation/interrupt after partial application.
                operation_rc = 2 if isinstance(exc, Exception) else 3
                reasons.append("callback_error" if operation_rc == 2 else "interrupted")
            finally:
                if checkpoint is not None:
                    receipt["action"] = "COMMIT" if keep and operation_rc == 0 else "REVERT"
                    cleanup_deadline = time.monotonic_ns() + cleanup_timeout_s * 1_000_000_000
                    try:
                        if receipt["action"] == "COMMIT" and self.target.read() != proposed:
                            operation_rc = 4
                            reasons.append("commit_drift")
                            receipt["action"] = "REVERT"
                        if receipt["action"] == "REVERT":
                            self.target.restore(checkpoint)
                            final = self.target.read()
                            receipt["final"] = final.evidence()
                            receipt["restored"] = final == before
                            if final != before:
                                raise _Stop(5, "rollback_residue")
                        else:
                            final = self.target.read()
                            receipt["final"] = final.evidence()
                            if final != proposed:
                                raise _Stop(4, "commit_drift")
                        _check_deadline(cleanup_deadline)
                        self.target.discard(checkpoint)
                        _check_deadline(cleanup_deadline)
                        self.pending_checkpoint = None
                    except _Stop as exc:
                        cleanup_rc = exc.rc
                        reasons.append(exc.reason)
                    except BaseException:
                        cleanup_rc = 3
                        reasons.append("rollback_unproven")
                    if cleanup_rc:
                        self._gate.fenced = True
                elif before is not None:
                    try:
                        final = self.target.read()
                        receipt["final"] = final.evidence()
                        if final != before:
                            cleanup_rc = 3
                            self._gate.fenced = True
                            reasons.append("snapshot_unproven")
                    except BaseException:
                        cleanup_rc = 3
                        self._gate.fenced = True
                        reasons.append("snapshot_unproven")
            receipt["operation_rc"], receipt["cleanup_rc"] = operation_rc, cleanup_rc
            rc = (operation_rc if cleanup_rc == 0 else cleanup_rc if operation_rc == 0
                  else core.worst_rc([operation_rc, cleanup_rc]))
            return self._finish(receipt, rc, reasons, started)
        finally:
            self._gate.lock.release()

    def _observe(self, measure, state, deadline):
        _check_deadline(deadline)
        if self.target.read() != state:
            raise _Stop(4, "measurement_stale")
        record = _checked_record(measure(self.target, deadline))
        _check_deadline(deadline)
        if self.target.read() != state:
            raise _Stop(4, "measurement_mutated_state")
        if record["state_sha256"] != state.evidence()["state_sha256"]:
            raise _Stop(4, "measurement_stale")
        return record

    def _finish(self, receipt, rc, reasons, started):
        receipt.update(rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc],
                       reason_codes=sorted(set(reasons)), fenced=self._gate.fenced,
                       wall_ns=time.monotonic_ns() - started)
        if receipt["action"] == "REFUSE":
            receipt["operation_rc"] = rc
        core.canonical_json(receipt)
        return receipt
