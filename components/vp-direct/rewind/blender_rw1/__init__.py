"""Broker-owned Blender rewind and asset-stage checkpoints."""

from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import file_digest, sha256
import json
import os
import sqlite3
from pathlib import Path
import stat
from threading import Lock

from jsonschema import ValidationError
from schemas.validation import canonical_bytes, validate_contract
from vendor.rewind import schema as rw


from .catalog import Catalog


_PROVISION = Lock()


class RewindError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RuntimeBinding:
    """Fresh broker evidence, including the head of the external-effect journal."""

    authority_epoch: int
    compatibility_sha256: str
    effect_frontier_sha256: str


@dataclass(frozen=True)
class Checkpoint:
    record: dict
    artifact_refs: tuple[dict, ...]
    quality: dict
    effect_frontier_sha256: str


@dataclass(frozen=True)
class CheckpointResult:
    """In-process result. Only receipt is a vp.rewind_receipt/v1 payload."""

    receipt: dict
    checkpoint: Checkpoint


    def goal_evidence(self):
        """Correlate the verified scene with the acceptance evidence used by U4a."""
        point = self.checkpoint
        rw.validate_record(point.record)
        validate_contract("vp.rewind_receipt/v1", self.receipt)
        _require(self.receipt["backend"] == "blender_rw1" and
                 self.receipt["state_sha256"] == scene_state_sha256(point.record["digest"]) and
                 self.receipt["last_good"] == point.record["point_id"] and
                 self.receipt["confirmed_tick"] == point.record["tick"], rw.RESTORE_MISMATCH)
        evidence = dict(point_id=point.record["point_id"], artifact_refs=list(point.artifact_refs),
                        quality=point.quality, effect_frontier_sha256=point.effect_frontier_sha256,
                        blend_sha256=point.record["blend_sha256"])
        validate_acceptance(point)
        return deepcopy(evidence)


def validate_acceptance(point):
    for ref in point.artifact_refs:
        validate_contract("vp.artifact_ref/v1", ref)
    validate_contract("vp_factory_call.vqa3d.score.result/v1",
                      {k: point.quality[k] for k in ("scene_digest", "score", "predicates")})
    validate_contract("vp_factory_call.vqa3d.gate.result/v1", point.quality)
    _require(rw.HEX64_RE.fullmatch(point.effect_frontier_sha256), rw.RESTORE_MISMATCH)


def _require(condition, code):
    if not condition:
        raise RewindError(code)


def scene_state_sha256(digest):
    """Full digest of the rw1 scene witness, for the goal ledger and receipt."""
    rw.check_digest(digest)
    return sha256(canonical_bytes(digest)).hexdigest()


def _blend_identity(path):
    def version(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as blend:
            before = os.fstat(blend.fileno())
            _require(stat.S_ISREG(before.st_mode) and before.st_size > 0, rw.CKPT_CORRUPT)
            digest = file_digest(blend, "sha256").hexdigest()
            after = os.fstat(blend.fileno())
            _require(version(before) == version(after) == version(path.lstat()), rw.CKPT_CORRUPT)
    except OSError as exc:
        code = rw.CKPT_MISSING if isinstance(exc, FileNotFoundError) else rw.CKPT_CORRUPT
        raise RewindError(code) from exc
    return digest, before.st_size


class BlenderRewind:
    """One fresh checkpoint ring on an existing broker-owned rw1 runtime.

    The caller owns the lease and serializes this backend with other broker
    work. `ops` supplies the frozen mark/restore/digest job builders. `binding`
    reads the broker's current authority, compatibility and effect frontier.
    `resolve_artifact` must reverify content through the registered locator.
    A replacement broker recovers a settled ring against its journal pin. The broker remains the only writer of its event log.
    """

    def __init__(self, broker, ops, *, capsule_id, goal_id, admitted_inputs, binding,
                 compatibility_sha256, resolve_artifact):
        self._broker = broker
        self._ops = ops
        self._binding = binding
        self._resolve = resolve_artifact
        self._capsule_id = capsule_id
        self._goal_id = goal_id
        self._compatibility = compatibility_sha256
        self._inputs = deepcopy(tuple(admitted_inputs))
        for ref in self._inputs:
            validate_contract("vp.artifact_ref/v1", ref)
        self._epoch = rw.SCENE_EPOCH_0
        self._points = {}
        self._used_marks = set()
        self._current = None
        self._stopped = False
        self._failed_restore_recorded = False
        self._lock = Lock()
        self._directory = Path(broker.rt).resolve(strict=True) / rw.CKPT_DIRNAME
        with _PROVISION:
            self._open_catalog()
            broker._rw1_owner = self

    def _open_catalog(self):
        self._owner()
        root = self._directory.parent
        sidecar = root / "rw1-acceptance.sqlite3"
        fresh = not os.path.lexists(self._directory)
        try:
            if fresh:
                _require(not os.path.lexists(sidecar), "UNCERTAIN_OUTCOME")
                if os.path.lexists(self._broker.p_events):
                    with open(self._broker.p_events, "rb") as journal:
                        for line in journal:
                            _require(line.endswith(b"\n") and not rw.is_rewind_record(json.loads(line)),
                                     "UNCERTAIN_OUTCOME")
                else:
                    _require(self._broker.seq == 0, "UNCERTAIN_OUTCOME")
                self._directory.mkdir(mode=0o700)
            else:
                _require(self._directory.is_dir() and not self._directory.is_symlink()
                         and sidecar.is_file() and not sidecar.is_symlink(), "UNCERTAIN_OUTCOME")
            identity = dict(goal=self._goal_id, capsule=self._capsule_id, compatibility=self._compatibility,
                            admitted_inputs=self._inputs)
            self._catalog = Catalog(sidecar, identity, fresh=fresh)
            if not fresh:
                self._recover()
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            if isinstance(exc, RewindError):
                raise
            raise RewindError("UNCERTAIN_OUTCOME") from exc

    def _owner(self):
        _require(not self._broker._lock.closed, "NO_LEASE")
        _require(getattr(self._broker, "_rw1_owner", self) is self, "UNCERTAIN_OUTCOME")

    def _recover(self):
        manifest, pin = self._catalog.read()
        _require(canonical_bytes(manifest["identity"]) == canonical_bytes(self._catalog.identity),
                 rw.RESTORE_MISMATCH)
        with open(self._broker.p_events, "rb") as events:
            records = []
            for line in events:
                _require(line.endswith(b"\n"), "UNCERTAIN_OUTCOME")
                record = json.loads(line)
                if rw.is_rewind_record(record) or record.get("rec") == "control":
                    records.append(record)
        history = rw.replay(records)
        events = [r for r in records if r["rec"] == "ckpt" or r.get("op") == "rewind"]
        _require(events and events[-1].get("acceptance_sha256") == pin and
                 not history.violations and history.open_txn is None and
                 history.scene_epoch == manifest["epoch"], "UNCERTAIN_OUTCOME")
        points = manifest["points"]
        _require(0 < len(points) == len(history.points) <= rw.RING_SLOTS, rw.CKPT_CORRUPT)
        for index, data in enumerate(points):
            point = Checkpoint(data["record"], tuple(data["artifact_refs"]), data["quality"],
                               data["effect_frontier_sha256"])
            key = rw.point_id(index)
            record = history.points.get(key, {})
            _require(point.record["point_id"] == key and
                     all(record.get(k) == v for k, v in point.record.items()), rw.CKPT_CORRUPT)
            validate_acceptance(point)
            self._verify_artifacts(point.artifact_refs)
            _require(index != 0 or point.artifact_refs == self._inputs, rw.RESTORE_MISMATCH)
            _require(_blend_identity(self._directory / f"{key}.blend") ==
                     (record["blend_sha256"], record["bytes"]), rw.CKPT_CORRUPT)
            self._points[key] = point
        _require({p.name for p in self._directory.iterdir()} ==
                 {f"{key}.blend" for key in self._points}, "UNCERTAIN_OUTCOME")
        self._used_marks = set(self._points)
        self._epoch = history.scene_epoch
        self._current = self._points[manifest["current"]]
        binding = self._read_binding(manifest["authority_epoch"])
        _require(binding.effect_frontier_sha256 == self._current.effect_frontier_sha256, rw.RESTORE_MISMATCH)
        self._stopped = manifest["stopped"] or history.frozen

    def _publish(self, job, event, points, current, stopped, authority_epoch):
        raw = canonical_bytes(dict(identity=self._catalog.identity, epoch=self._epoch,
                                   authority_epoch=authority_epoch, points=[asdict(p) for p in points],
                                   current=current.record["point_id"], stopped=stopped))
        pin = sha256(raw).hexdigest()
        self._broker._append(dict(event, acceptance_sha256=pin))
        with open(self._broker.p_events, "rb") as events:
            os.fsync(events.fileno())
        self._catalog.finish(job, raw, pin)

    def points(self):
        with self._lock:
            self._owner()
            return tuple(self._points)

    def current(self):
        """Return the last published checkpoint, if its transition was verified."""
        with self._lock:
            self._owner()
            return deepcopy(self._current)

    def _read_binding(self, authority_epoch):
        binding = self._binding()
        _require(isinstance(binding, RuntimeBinding) and type(authority_epoch) is int and
                 type(binding.authority_epoch) is int and binding.authority_epoch == authority_epoch >= 0 and
                 isinstance(binding.compatibility_sha256, str) and
                 rw.HEX64_RE.fullmatch(binding.compatibility_sha256) and
                 binding.compatibility_sha256 == self._compatibility and
                 isinstance(binding.effect_frontier_sha256, str) and
                 rw.HEX64_RE.fullmatch(binding.effect_frontier_sha256), rw.RESTORE_MISMATCH)
        return binding

    def _verify_artifacts(self, refs):
        for ref in refs:
            try:
                actual = self._resolve(ref["locator"], ref["sha256"])
                validate_contract("vp.artifact_ref/v1", actual)
            except (OSError, LookupError, ValueError, ValidationError) as exc:
                raise RewindError(rw.RESTORE_MISMATCH) from exc
            # The locator's readback can have a different transfer mode.
            _require(all(actual[key] == ref[key] for key in ref if key != "copy"), rw.RESTORE_MISMATCH)

    def _control(self, job_id, script):
        reply = rw.dispatch_control(self._broker, job_id, script)
        if not reply["ok"]:
            code = rw.RESTORE_TIMEOUT if reply["code"] == "TURN_TIMEOUT" else "UNCERTAIN_OUTCOME"
            raise RewindError(code)
        result = reply["result"]
        _require(isinstance(result, dict), rw.RESTORE_MISMATCH)
        try:
            rw.check_digest(result.get("digest"))
        except rw.SchemaError as exc:
            raise RewindError(rw.RESTORE_MISMATCH) from exc
        return result

    def _receipt(self, op, record, confirmed, epoch, authority_epoch, budget, failed_branches):
        receipt = dict(
            schema="vp.rewind_receipt/v1", op=op, ok=True,
            code="RESTORED" if op == "rewind" else "OK", backend="blender_rw1",
            capsule_id=self._capsule_id, goal_id=self._goal_id, scene_epoch=epoch,
            authority_epoch=authority_epoch, last_good=confirmed["point_id"],
            confirmed_tick=confirmed["tick"], budget=deepcopy(budget),
            external_effect_frontier="UNREVERTED_HOST_NET_JOURNAL",
            compatibility_sha256=self._compatibility, state_sha256=scene_state_sha256(record["digest"]),
            digest_match=True, digest_delta=0, failed_branches=deepcopy(list(failed_branches)),
        )
        validate_contract("vp.rewind_receipt/v1", receipt)
        return receipt

    def mark(self, *, by, scene_epoch, authority_epoch, tick, frame, artifact_refs,
             score_checkpoint, budget, failed_branches=()):
        """Mark an admitted stage. The caller owns the rw1 transaction verdict."""
        _require(by in ("broker", "director"), "NO_LEASE")
        with self._lock:
            self._owner()
            _require(not self._stopped, "NO_LEASE")
            _require(len(self._points) < rw.RING_SLOTS, rw.CKPT_BUDGET)
            _require(type(scene_epoch) is int and scene_epoch == self._epoch, rw.RESTORE_MISMATCH)
            point = rw.point_id(len(self._points))
            previous = next(reversed(self._points.values()), None)
            _require(type(frame) is int and frame >= 0 and type(tick) is int and
                     (tick > previous.record["tick"] if previous else tick == 0), rw.CKPT_CORRUPT)
            refs = deepcopy(tuple(artifact_refs))
            for ref in refs:
                validate_contract("vp.artifact_ref/v1", ref)
            _require(point != "p0" or refs == self._inputs, rw.RESTORE_MISMATCH)
            binding = self._read_binding(authority_epoch)
            self._verify_artifacts(refs)
            path = self._directory / f"{point}.blend"
            _require(point not in self._used_marks and not os.path.lexists(path), "UNCERTAIN_OUTCOME")
            script = self._ops.mark_script(str(path))
            job = rw.control_job_id("mark", point)
            self._catalog.begin(job)
            self._used_marks.add(point)
            self._current = None
            result = self._control(job, script)
            digest, size = _blend_identity(path)
            _require(result.get("op") == "mark" and result.get("path") == str(path) and
                     type(result.get("bytes")) is int and result["bytes"] == size, rw.CKPT_CORRUPT)
            quality_result = score_checkpoint(digest)
            quality = dict(scene_digest=quality_result.scene_digest, score=quality_result.score,
                           predicates=[dict(render=p.render, frame_not_blank=p.frame_not_blank)
                                       for p in quality_result.predicates], accepted=quality_result.accepted)
            validate_contract("vp_factory_call.vqa3d.gate.result/v1", quality)
            self._verify_artifacts(refs)
            _require(_blend_identity(path) == (digest, size), rw.CKPT_CORRUPT)
            _require(self._read_binding(authority_epoch) == binding, rw.RESTORE_MISMATCH)
            record = rw.ckpt_rec(point, tick, self._epoch, digest, size, result["digest"], frame)
            receipt = self._receipt("checkpoint", record, record, self._epoch, authority_epoch,
                                    budget, failed_branches)
            checkpoint = Checkpoint(record, refs, quality, binding.effect_frontier_sha256)
            validate_acceptance(checkpoint)
            self._publish(job, record, [*self._points.values(), checkpoint], checkpoint, False, authority_epoch)
            self._points[point] = self._current = checkpoint
            return CheckpointResult(receipt, deepcopy(checkpoint))

    def restore(self, point, *, by, scene_epoch, authority_epoch, budget, failed_branches=()):
        """Load a point, then publish one result with scene and acceptance evidence."""
        _require(by in ("broker", "director"), "NO_LEASE")
        with self._lock:
            self._owner()
            latest = next(reversed(self._points), None)
            _require(not self._stopped or (by == "director" and point == latest), "NO_LEASE")
            _require(type(scene_epoch) is int and scene_epoch == self._epoch, rw.RESTORE_MISMATCH)
            _require(isinstance(point, str) and point in self._points, rw.CKPT_MISSING)
            checkpoint = self._points[point]
            record = checkpoint.record
            binding = self._read_binding(authority_epoch)
            _require(binding.effect_frontier_sha256 == checkpoint.effect_frontier_sha256, rw.RESTORE_MISMATCH)
            self._verify_artifacts(checkpoint.artifact_refs)
            path = self._directory / f"{point}.blend"
            identity = (record["blend_sha256"], record["bytes"])
            _require(_blend_identity(path) == identity, rw.CKPT_CORRUPT)
            _require(0 < self._broker.job_cap_s <= rw.RESTORE_CAP_S, rw.RESTORE_TIMEOUT)
            epoch = self._epoch + 1
            receipt = self._receipt("rewind", record, self._points[latest].record, epoch,
                                    authority_epoch, budget, failed_branches)
            job = rw.control_job_id("restore", point, f"e{epoch}")
            script = self._ops.restore_script(str(path), load_ui=True)
            _require(self._read_binding(authority_epoch) == binding, rw.RESTORE_MISMATCH)
            validate_acceptance(checkpoint)
            self._catalog.begin(job, recovery=self._failed_restore_recorded and by == "director")
            self._failed_restore_recorded = False
            was_stopped = self._stopped
            self._epoch, self._current, self._stopped = epoch, None, True
            try:
                restored = self._control(job, script)
                _require(restored.get("op") == "restore" and restored.get("path") == str(path) and
                         restored.get("load_ui") is True, rw.RESTORE_MISMATCH)
                probe = self._control(rw.control_job_id("digest", "restore", point, f"e{epoch}"),
                                      self._ops.digest_script())
                _require(probe.get("op") == "digest" and rw.digest_match(restored["digest"], record["digest"]) and
                         rw.digest_match(probe["digest"], record["digest"]), rw.RESTORE_MISMATCH)
                self._verify_artifacts(checkpoint.artifact_refs)
                _require(_blend_identity(path) == identity, rw.CKPT_CORRUPT)
                _require(self._read_binding(authority_epoch) == binding, rw.RESTORE_MISMATCH)
            except Exception as exc:
                code = getattr(exc, "code", "UNCERTAIN_OUTCOME")
                try:
                    self._broker._append(rw.rewind_rec(point, epoch, False, by=by, reason=code))
                    self._failed_restore_recorded = True
                finally:
                    self._broker.control("stop", "broker", code)
                raise
            try:
                self._publish(job, rw.rewind_rec(point, epoch, True, by=by),
                              self._points.values(), checkpoint, was_stopped, authority_epoch)
            except OSError:
                self._broker.control("stop", "broker", "UNCERTAIN_OUTCOME")
                raise
            self._current, self._stopped = checkpoint, was_stopped
            return CheckpointResult(receipt, deepcopy(checkpoint))
