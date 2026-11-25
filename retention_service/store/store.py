"""Durable, tenant-scoped retention transactions for authenticated callers."""

from base64 import b64decode, b64encode
from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256 as hash_stream
from io import BytesIO
import os
from threading import RLock
from uuid import uuid4

from ..contracts.validation import ContractError
from ..contracts.wire import (
    MAX_ARTIFACT_BYTES, MAX_OFFSET, MAX_PAGE_BYTES, MAX_RECORD_BYTES,
    decode_record, decode_start, record_bytes, verify_admission,
)
from .bindings import artifact_index, require, start_binding, terminal_binding, verify_timeline
from .files import Conflict, Files, IntegrityError, RangeError, StorageFailure, Unavailable, digest
from .model import (
    ATTEMPTS, GIB, MAX_INTENT, MAX_METADATA, TENANTS, TOMBSTONES,
    Actor, Page, reference, timestamp, utc_now, when,
)
from .recovery import Recovery

class Store(Recovery):
    """One process owns the root. Every route enters the same transaction lock.

    authorize(actor, verb, attempt_id, admission) uses current authenticated
    grants. It is called before opening attempt data, then with verified admission
    for request-bound checks. authorize_source(actor, destination, source_id, side,
    source_admission) checks current source-use delegation and the designated
    inspector's grant, first with None and then with verified source admission.

    verify_witness(kind, actor, admission, record, evidence) checks trusted REA
    custody and authority proofs for the original bytes. The callback must verify
    historical proofs on reopen. Supply original proof octets in the persisted
    evidence document when the proof format is binary.
    validate_profile(admission, profile, terminal)
    verifies the approved local schema pin and bound fields. Callbacks must
    return literal True. Each is required at construction.
    """

    def __init__(self, root, validator, policy, *, authorize, authorize_source,
                 verify_witness, validate_profile, clock=utc_now, fault=None):
        self.validator = validator
        validator.validate("Id", policy.revision)
        if (not 0 < policy.object_bytes <= min(GIB, policy.attempt_bytes)
                or not policy.attempt_bytes <= min(4 * GIB, policy.tenant_bytes)
                or not policy.tenant_bytes <= 64 * GIB or not 0 < policy.snapshot_bytes <= 32 << 20
                or not timedelta(0) < policy.retention <= timedelta(days=30)
                or not timedelta(0) < policy.purge_window <= timedelta(hours=24)
                or not timedelta(0) < policy.snapshot_lifetime <= timedelta(minutes=15)):
            raise ValueError("invalid retention limits")
        if not all(callable(callback) for callback in
                   (authorize, authorize_source, verify_witness, validate_profile)):
            raise TypeError("retention requires explicit authorization and evidence verifiers")
        self.policy = policy
        self.authorize = authorize
        self.authorize_source = authorize_source
        self.verify_witness = verify_witness
        self.validate_profile = validate_profile
        self.clock = clock
        self.lock = RLock()
        self.fault = fault if fault is not None else lambda *_: None
        self.disk = Files(root, self.fault)
        self.receipts = {}
        self.snapshots = {}
        self.failed = False
        try:
            self.disk.mkdir(TENANTS)
            self.disk.mkdir(("policy",))
            if self.disk.exists(("policy", "restore-incomplete")):
                raise IntegrityError("incomplete restore cannot serve reads")
            self._reopen()
        except BaseException:
            self.disk.close()
            raise

    def ready(self, actor, verb):
        """Check current grant and capacity without creating tenant state."""
        self.validator.validate("Verb", verb)
        with self.transaction():
            self._grant(actor, verb)
            info = os.fstatvfs(self.disk.fd)
            if not info.f_bavail or not info.f_favail or info.f_flag & os.ST_RDONLY:
                raise StorageFailure("retention filesystem has no writable capacity")
            try:
                tenant = self._tenant(actor.tenant)
            except Unavailable:
                return True
            if self.disk.size(tenant) + self._snapshot_size(tenant) >= self.policy.tenant_bytes:
                raise StorageFailure("retention tenant has no writable capacity")
            return True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self.lock:
            self.disk.close()
            self.snapshots.clear()

    @contextmanager
    def transaction(self):
        with self.lock:
            if self.failed or self.disk.fd is None:
                raise StorageFailure("store needs to reopen and verify publications")
            try:
                yield
            except OSError as exc:
                self.failed = True
                raise StorageFailure("storage result is unresolved") from exc

    @contextmanager
    def stored(self):
        # An invalid persisted graph is an integrity error. Absent dependencies
        # are corruption once a higher-level publication refers to them.
        try:
            yield
        except (ValueError, TypeError, LookupError, FileNotFoundError) as exc:
            raise IntegrityError("durable evidence did not verify") from exc

    def _grant(self, actor, verb, attempt_id=None, admission=None):
        self.validator.validate("Id", actor.tenant)
        self.validator.validate("Id", actor.principal)
        if self.authorize(actor, verb, attempt_id, admission) is not True:
            raise Unavailable("unavailable")

    def _tenant(self, tenant, *, create=False):
        directory = TENANTS + (digest(tenant.encode("ascii")),)
        marker = directory + ("tenant.json",)
        if create and not self.disk.exists(marker):
            self.disk.mkdir(directory)
            self.disk.mkdir(directory + (ATTEMPTS,))
            self.disk.mkdir(directory + (TOMBSTONES,))
            self.disk.publish(marker, record_bytes({"tenant": tenant}), "tenant")
        if not self.disk.exists(marker):
            raise Unavailable("unavailable")
        with self.stored():
            if self.disk.json(marker, MAX_METADATA) != {"tenant": tenant}:
                raise IntegrityError("tenant directory binding mismatch")
        return directory

    def _attempt(self, actor, verb, attempt_id):
        self._grant(actor, verb, attempt_id)
        self.validator.validate("Attempt", attempt_id)
        tenant = self._tenant(actor.tenant)
        path = tenant + (ATTEMPTS, attempt_id)
        if self.disk.exists(tenant + (TOMBSTONES, attempt_id)) or not self.disk.exists(path):
            raise Unavailable("unavailable")
        return path

    def _admission(self, path):
        with self.stored():
            doc = self.disk.json(path + ("admission.json",), MAX_METADATA)
            verify_admission(doc, self.validator)
            require(doc["attempt_id"] == path[-1], "admission path binding mismatch")
        return doc

    def _ack(self, admission, start_raw):
        return dict(attempt_id=admission["attempt_id"], request_sha256=admission["request_sha256"],
                    start_sha256=digest(start_raw))

    def _proof(self, kind, actor, admission, record, evidence):
        require(self.verify_witness(kind, actor, admission, record, evidence) is True,
                f"{kind} evidence could not be verified")

    def _profile(self, admission, profile, terminal):
        require(self.validate_profile(admission, profile, terminal) is True,
                "approved profile could not be verified")

    def _save(self, path, name, doc, label=None):
        self.disk.publish(path + (name,), record_bytes(doc), label or name,
                          staging=path + ("staging",))

    def _start(self, actor, path, admission):
        if not self.disk.exists(path + ("start.json",)):
            raise Unavailable("unavailable")
        with self.stored():
            raw = self.disk.read(path + ("start.json",), MAX_RECORD_BYTES)
            start = decode_record(raw, "Start", self.validator)
            start_binding(admission, start)
            verify_timeline(start, lambda *_: require(False, "invalid start event reference"))
            proof = self.disk.json(path + ("start-proof.json",), MAX_METADATA)
            require(proof["start_sha256"] == digest(raw), "start proof digest mismatch")
            self._proof("start", Actor(actor.tenant, admission["executor_id"]), admission,
                        start, proof["evidence"])
        return raw, start, self._ack(admission, raw)

    def _claimed(self, path, admission, ack):
        claim = path + ("launch-claim.json",)
        if not self.disk.exists(claim):
            return False
        with self.stored():
            require(admission["operation"] != "compare"
                    and self.disk.json(claim, MAX_METADATA) == ack, "invalid durable launch claim")
        return True

    def admit(self, actor, admission):
        with self.transaction():
            self._grant(actor, "admit")
            verify_admission(admission, self.validator)
            attempt_id = admission["attempt_id"]
            self._grant(actor, "admit", attempt_id, admission)
            if admission["operation"] == "compare":
                self._sources(actor, admission)
            tenant = self._tenant(actor.tenant, create=True)
            path = tenant + (ATTEMPTS, attempt_id)
            if self.disk.exists(tenant + (TOMBSTONES, attempt_id)):
                raise Unavailable("unavailable")
            if self.disk.exists(path + ("admission.json",)):
                current = self._admission(path)
                if current != admission:
                    raise Conflict("admission binding changed")
                return current, False
            for directory in (path, path + ("staging",), path + ("uploads",), path + ("holds",)):
                self.disk.mkdir(directory)
            self._reserve(path, len(record_bytes(admission)))
            self._save(path, "admission.json", admission, "admission")
            self.fault("admission", "before_ack")
            return admission.copy(), True

    def _runner(self, actor, verb, attempt_id):
        path = self._attempt(actor, verb, attempt_id)
        if not self.disk.exists(path + ("admission.json",)):
            raise Unavailable("unavailable")
        admission = self._admission(path)
        self._grant(actor, verb, attempt_id, admission)
        if actor.principal != admission["executor_id"]:
            raise Unavailable("unavailable")
        return path, admission

    def start(self, actor, attempt_id, raw, *, content_length, sha256, evidence):
        with self.transaction():
            path, admission = self._runner(actor, "start", attempt_id)
            if self.disk.exists(path + ("start.json",)):
                prior, _, ack = self._start(actor, path, admission)
                with self.stored():
                    proof = self.disk.json(path + ("start-proof.json",), MAX_METADATA)
                if prior != raw or record_bytes(proof["evidence"]) != record_bytes(evidence):
                    raise Conflict("start bytes changed")
                decode_start(raw, content_length=content_length, digest=sha256, validator=self.validator)
                return ack, False
            start = decode_start(raw, content_length=content_length, digest=sha256, validator=self.validator)
            start_binding(admission, start)
            verify_timeline(start, lambda *_: require(False, "invalid start event reference"))
            self._proof("start", actor, admission, start, evidence)
            proof = dict(start_sha256=sha256, evidence=evidence)
            proof_bytes = record_bytes(proof)
            require(len(proof_bytes) <= MAX_METADATA, "start proof exceeds byte limit")
            self._reserve(path, len(raw) + len(proof_bytes))
            if self.disk.exists(path + ("start-proof.json",)):
                with self.stored():
                    previous = self.disk.json(path + ("start-proof.json",), MAX_METADATA)
                if previous != proof:
                    raise Conflict("start proof changed")
            else:
                self._save(path, "start-proof.json", proof, "start-proof")
            self.disk.publish(path + ("start.json",), raw, "start", staging=path + ("staging",))
            self.fault("start", "before_ack")
            return self._ack(admission, raw), True

    def claim(self, actor, attempt_id, ack, *, evidence):
        with self.transaction():
            path, admission = self._runner(actor, "claim", attempt_id)
            _, start, current = self._start(actor, path, admission)
            if (admission["operation"] == "compare" or self._claimed(path, admission, current)
                    or self.disk.exists(path + ("commit-intent.json",))):
                raise Conflict("launch claim is no longer available")
            self.validator.validate("StartAck", ack)
            require(current == ack, "claim does not bind the durable start")
            self._proof("claim", actor, admission, start, evidence)
            self._reserve(path, len(record_bytes(current)))
            self._save(path, "launch-claim.json", current, "claim")
            self.fault("claim", "before_ack")
            return current

    def _reserve(self, path, amount):
        tenant = path[:-2]
        if (self.disk.size(path) + amount > self.policy.attempt_bytes
                or self.disk.size(tenant) + self._snapshot_size(tenant) + amount > self.policy.tenant_bytes):
            raise StorageFailure("retention quota exhausted")

    def upload(self, actor, attempt_id, upload_id, stream, *, content_length, sha256):
        with self.transaction():
            path, admission = self._runner(actor, "upload", attempt_id)
            self._start(actor, path, admission)
            self.validator.validate("PathId", upload_id)
            self.validator.validate("Hash", sha256)
            require(type(content_length) is int and 0 <= content_length <= self.policy.object_bytes,
                    "upload exceeds object byte limit")
            dest = path + ("uploads", digest(upload_id.encode("ascii")))
            existing = self.disk.exists(dest)
            if not existing and self.disk.exists(path + ("commit-intent.json",)):
                raise Conflict("finish sealed the upload set")
            if existing:
                doc = self._upload(dest)
                with self.disk.open(dest + ("bytes",)) as original:
                    remaining = content_length
                    changed = False
                    received = hash_stream()
                    while remaining:
                        block = stream.read(min(1 << 20, remaining))
                        require(isinstance(block, bytes) and 0 < len(block) <= remaining,
                                "upload length does not match stream")
                        received.update(block)
                        changed |= block != original.read(len(block))
                        remaining -= len(block)
                    require(stream.read(1) == b"" and received.hexdigest() == sha256,
                            "upload length or digest mismatch")
                    changed |= original.read(1) != b""
                if changed or (doc["upload_id"], doc["bytes"], doc["sha256"]) != (
                        upload_id, content_length, sha256):
                    raise Conflict("upload bytes changed")
                return self._object_meta(doc), False
            self._reserve(path, content_length + MAX_METADATA // 1024)
            temp = path + ("staging", ".upload." + uuid4().hex)
            self.disk.mkdir(temp)
            try:
                self.disk.write(temp + ("bytes",), stream, length=content_length, sha=sha256,
                                label="upload")
            except ValueError as exc:
                self.disk.remove(temp)
                raise ContractError(str(exc)) from exc
            occupied = {self._upload(path + ("uploads", entry), verify_bytes=False)["object_ref"]
                        for entry in self.disk.names(path + ("uploads",))}
            handle = reference("obj")
            while handle in occupied:
                handle = reference("obj")
            doc = dict(upload_id=upload_id, object_ref=handle, bytes=content_length, sha256=sha256)
            self.disk.publish(temp + ("meta.json",), record_bytes(doc), "upload-meta")
            self.disk.rename(temp, dest, "upload")
            self.fault("upload", "before_ack")
            return self._object_meta(doc), True

    @staticmethod
    def _object_meta(doc):
        return {key: doc[key] for key in ("object_ref", "bytes", "sha256")}

    def _upload(self, path, *, verify_bytes=True):
        with self.stored():
            doc = self.disk.json(path + ("meta.json",), MAX_METADATA)
            require(doc.keys() == {"upload_id", "object_ref", "bytes", "sha256"},
                    "invalid durable upload binding")
            self.validator.validate("PathId", doc["upload_id"])
            self.validator.validate("ObjectMeta", self._object_meta(doc))
            require(digest(doc["upload_id"].encode("ascii")) == path[-1], "upload path binding mismatch")
            if verify_bytes:
                actual, _ = self.disk.metadata(path + ("bytes",), bound=self.policy.object_bytes)
                require(all(doc[key] == actual[key] for key in actual), "corrupt upload bytes")
        return doc

    def _uploads(self, path):
        result = {}
        for entry in self.disk.names(path + ("uploads",)):
            binding = self._upload(path + ("uploads", entry))
            if binding["object_ref"] in result:
                raise IntegrityError("duplicate durable object reference")
            result[binding["object_ref"]] = binding
        return result

    def _sources(self, actor, admission):
        result = {}
        tenant = self._tenant(actor.tenant)
        sources = {side: admission[f"{side}_attempt_id"] for side in ("reference", "candidate")}
        for side, source_id in sorted(sources.items(), key=lambda pair: (pair[1], pair[0])):
            if source_id == admission["attempt_id"]:
                raise ContractError("comparison cannot source its own attempt")
            if self.authorize_source(actor, admission, source_id, side, None) is not True:
                raise Unavailable("unavailable")
            path = tenant + (ATTEMPTS, source_id)
            if (self.disk.exists(tenant + (TOMBSTONES, source_id))
                    or not self.disk.exists(path + ("admission.json",))):
                raise Unavailable("unavailable")
            source_admission = self._admission(path)
            if (self.authorize_source(actor, admission, source_id, side, source_admission) is not True
                    or not self.disk.exists(path + ("end.json",))):
                raise Unavailable("unavailable")
            result[side] = self._committed(actor, path, source_admission)
        return result

    def _lineage(self, sources, terminal, objects):
        lineage = []
        for side, source in sorted(sources.items()):
            source_terminal = source["terminal"]
            source_sha = source["ack"]["terminal_sha256"]
            source_id = source_terminal["attempt_id"]
            anchor = dict(side=side, source_attempt_id=source_id,
                          source_terminal_sha256=source_sha, source_object_ref=None,
                          source_sha256=source_terminal["artifact"]["sha256"],
                          destination_object_ref=None)
            lineage.append(anchor)
            matched_sides = {}
            for field, keys in (
                    ("media", ("recording_sha256",)),
                    ("timeline", ("timeline_sha256", "clock_source", "origin",
                                  "canonicalization", "time_base"))):
                binding = terminal.get(field, {}).get(side)
                if binding is None:
                    continue
                matched_sides[field] = {
                    name for name, value in source_terminal.get(field, {}).items()
                    if all(binding.get(key) == value.get(key) for key in keys)
                }
                require(matched_sides[field], f"comparison {field} changed the admitted source")
            side_objects = {ref: entry for ref, entry in objects.items() if entry.get("side") == side}
            if "media" in matched_sides and not any(entry["role"] == "recording"
                                                    for entry in side_objects.values()):
                sha = terminal["media"][side]["recording_sha256"]
                source_ref = min((ref for ref, entry in source["objects"].items()
                                  if entry["role"] == "recording" and entry["sha256"] == sha
                                  and entry["side"] in matched_sides["media"]), default=None)
                require(source_ref is not None, "comparison media lacks retained source recording")
                lineage.append(dict(anchor, source_object_ref=source_ref, source_sha256=sha))
            for dest_ref, entry in sorted(side_objects.items()):
                role = entry["role"]
                field = {"recording": "media", "timeline_events": "timeline"}.get(role)
                source_ref = min((ref for ref, obj in source["objects"].items()
                                  if all(obj[key] == entry[key] for key in ("role", "bytes", "sha256"))
                                  and (field is None or obj["side"] in matched_sides[field])),
                                 default=None)
                if role == "recording":
                    require(source_ref is not None, "comparison recording lacks a matching source object")
                elif role == "timeline_events" and source_ref is None:
                    require(any("events" in source_terminal["timeline"][name]
                                for name in matched_sides["timeline"]),
                            "comparison timeline lacks matching source events")
                lineage.append(dict(anchor, source_object_ref=source_ref,
                                    source_sha256=entry["sha256"], destination_object_ref=dest_ref))
        return lineage

    def _verify_lineage(self, admission, lineage, terminal, objects):
        if admission["operation"] != "compare":
            require(lineage == [], "external launch has comparison lineage")
            return
        anchors = {}
        destinations = set()
        retained = set()
        media = terminal.get("media", {})
        copied = {obj["side"] for obj in objects.values() if obj["role"] == "recording"}
        for item in lineage:
            self.validator.validate("Lineage", item)
            side = item["side"]
            require(item["source_attempt_id"] == admission[f"{side}_attempt_id"],
                    "lineage swapped or substituted a source")
            destination = item["destination_object_ref"]
            source = item["source_object_ref"]
            if destination is not None:
                require(destination not in destinations and destination in objects
                        and objects[destination].get("side") == side
                        and objects[destination]["sha256"] == item["source_sha256"]
                        and (source is not None or objects[destination]["role"] != "recording"),
                        "lineage changed a destination object")
                destinations.add(destination)
            elif source is not None:
                require(side not in retained and side in media and side not in copied
                        and media[side]["recording_sha256"] == item["source_sha256"],
                        "lineage changed a retained recording")
                retained.add(side)
            else:
                require(side not in anchors, "lineage contains a duplicate source anchor")
                anchors[side] = item["source_terminal_sha256"]
        require(anchors.keys() == {"reference", "candidate"}
                and retained == media.keys() - copied
                and destinations == {ref for ref, obj in objects.items() if "side" in obj}
                and all(item["source_terminal_sha256"] == anchors[item["side"]] for item in lineage),
                "lineage is incomplete or disagrees on the source version")

    def _intent(self, path):
        with self.stored():
            doc = self.disk.json(path + ("commit-intent.json",), MAX_INTENT)
            require(doc.keys() == {"terminal_b64", "artifact", "uploads", "evidence",
                                   "lineage", "receipt_ref", "admission_sha256"},
                    "invalid commit intent fields")
            terminal = b64decode(doc["terminal_b64"], validate=True)
            require(len(terminal) <= MAX_RECORD_BYTES
                    and b64encode(terminal).decode("ascii") == doc["terminal_b64"],
                    "invalid commit intent terminal bytes")
            self.validator.validate("ReceiptRef", doc["receipt_ref"])
            self.validator.validate("ArtRef", doc["artifact"]["artifact_ref"])
            self.validator.validate("Size", doc["artifact"]["bytes"])
            self.validator.validate("Hash", doc["artifact"]["sha256"])
        return doc, terminal

    def _artifact(self, path, intent):
        with self.stored():
            base = path + ("artifact",)
            raw = self.disk.read(base + ("bytes",), MAX_ARTIFACT_BYTES)
            doc = self.disk.json(base + ("meta.json",), MAX_METADATA)
            require(doc == intent["artifact"] and doc["bytes"] == len(raw)
                    and doc["sha256"] == digest(raw), "artifact publication does not match intent")
        return raw

    def _finish_binding(self, actor, path, admission, start, ack, terminal_raw, artifact, evidence):
        terminal = decode_record(terminal_raw, "Terminal", self.validator)
        terminal_binding(admission, start, ack["start_sha256"], terminal,
                         claimed=self._claimed(path, admission, ack))
        self._proof("finish", Actor(actor.tenant, admission["executor_id"]), admission,
                    terminal, evidence)
        uploads = self._uploads(path)

        def read_object(ref, bound):
            doc = uploads[ref]
            upload = path + ("uploads", digest(doc["upload_id"].encode("ascii")))
            return self.disk.read(upload + ("bytes",), bound)

        objects, _ = artifact_index(self.validator, admission, start, terminal, artifact, uploads,
                                    read_object, self._profile)
        return terminal, uploads, objects

    def _commit_clock(self, path):
        with self.stored():
            doc = self.disk.json(path + ("commit-time.json",), MAX_METADATA)
            self.validator.validate("Id", doc["policy_revision"])
            self.validator.validate("Time", doc["committed_at"])
            self.validator.validate("Time", doc["expires_at"])
            retained = when(doc["expires_at"]) - when(doc["committed_at"])
            require(timedelta(0) < retained <= timedelta(days=30), "invalid durable commit expiry")
        return doc

    def _committed(self, actor, path, admission=None):
        with self.stored():
            if admission is None:
                admission = self._admission(path)
            try:
                _, start, start_ack = self._start(actor, path, admission)
            except Unavailable as exc:
                raise IntegrityError("published end has no verified start") from exc
            doc, raw = self._intent(path)
            require(doc["admission_sha256"] == admission["admission_sha256"],
                    "finish intent does not bind admission")
            end = self.disk.read(path + ("end.json",), MAX_RECORD_BYTES)
            require(end == raw, "published end does not match intent")
            artifact = self._artifact(path, doc)
            terminal, uploads, objects = self._finish_binding(
                actor, path, admission, start, start_ack, end, artifact, doc["evidence"])
            require(doc["uploads"] == uploads, "sealed upload set changed")
            self._verify_lineage(admission, doc["lineage"], terminal, objects)
            return dict(
                admission=admission, start=start, terminal=terminal, artifact=artifact,
                ack=dict(attempt_id=path[-1], start_sha256=start_ack["start_sha256"],
                         terminal_sha256=digest(end)),
                intent=doc, raw=end, uploads=uploads, objects=objects, **self._commit_clock(path),
            )

    def finish(self, actor, attempt_id, terminal_raw, artifact, *, evidence):
        with self.transaction():
            path, admission = self._runner(actor, "finish", attempt_id)
            _, start, ack = self._start(actor, path, admission)
            bounded = isinstance(artifact, bytes) and len(artifact) <= MAX_ARTIFACT_BYTES
            require(bounded and isinstance(terminal_raw, bytes)
                    and len(terminal_raw) <= MAX_RECORD_BYTES, "finish exceeds byte limits")
            require(len(record_bytes(evidence)) <= MAX_METADATA, "finish evidence exceeds byte limit")
            if self.disk.exists(path + ("end.json",)):
                prior = self._committed(actor, path, admission)
                if (prior["raw"] != terminal_raw or prior["artifact"] != artifact
                        or record_bytes(prior["intent"]["evidence"]) != record_bytes(evidence)):
                    raise Conflict("finish differs from the durable terminal")
                self._check_sources(actor, admission, prior["terminal"], prior["objects"],
                                    prior["intent"]["lineage"])
                return prior["ack"], False

            intent = path + ("commit-intent.json",)
            old = self.disk.exists(intent)
            if not old and self.disk.exists(path + ("artifact",)):
                raise IntegrityError("published artifact has no original commit intent")
            if old:
                doc, original = self._intent(path)
                expected = doc["artifact"]
                if (original != terminal_raw or expected["bytes"] != len(artifact)
                        or expected["sha256"] != digest(artifact)
                        or record_bytes(doc["evidence"]) != record_bytes(evidence)):
                    raise Conflict("finish differs from the interrupted intent")
                if self.disk.exists(path + ("artifact",)) and self._artifact(path, doc) != artifact:
                    raise Conflict("finish differs from the published artifact")
            terminal, uploads, objects = self._finish_binding(
                actor, path, admission, start, ack, terminal_raw, artifact, evidence)
            sources = self._sources(actor, admission) if admission["operation"] == "compare" else {}
            lineage = self._lineage(sources, terminal, objects)
            if old:
                with self.stored():
                    require(doc["uploads"] == uploads
                            and doc["admission_sha256"] == admission["admission_sha256"],
                            "interrupted intent no longer binds its dependencies")
                if lineage != doc["lineage"]:
                    raise Conflict("comparison source version changed")
            else:
                receipt = reference("rcp")
                while (actor.tenant, receipt) in self.receipts:
                    receipt = reference("rcp")
                doc = dict(terminal_b64=b64encode(terminal_raw).decode("ascii"),
                           artifact=dict(artifact_ref=reference("art"), bytes=len(artifact),
                                         sha256=digest(artifact)), uploads=uploads, evidence=evidence,
                           lineage=lineage, receipt_ref=receipt,
                           admission_sha256=admission["admission_sha256"])
            artifact_path = path + ("artifact",)
            needs_artifact = not self.disk.exists(artifact_path)
            intent_bytes = record_bytes(doc)
            require(len(intent_bytes) <= MAX_INTENT, "finish intent exceeds byte limit")
            self._reserve(path, len(terminal_raw) + (0 if old else len(intent_bytes))
                          + (len(artifact) if needs_artifact else 0) + 2048)
            if not old:
                self._save(path, "commit-intent.json", doc, "intent")
            if needs_artifact:
                temp = path + ("staging", ".artifact." + uuid4().hex)
                self.disk.mkdir(temp)
                self.disk.write(temp + ("bytes",), BytesIO(artifact), length=len(artifact),
                                sha=doc["artifact"]["sha256"], label="artifact")
                self.disk.publish(temp + ("meta.json",), record_bytes(doc["artifact"]), "artifact-meta")
                self.disk.rename(temp, artifact_path, "artifact")
            now = self.clock()
            clock = dict(committed_at=timestamp(now), expires_at=timestamp(now + self.policy.retention),
                         policy_revision=self.policy.revision)
            self.disk.publish(path + ("commit-time.json",), record_bytes(clock), "commit-time",
                              staging=path + ("staging",), replace=True)
            self.disk.publish(path + ("end.json",), terminal_raw, "end", staging=path + ("staging",))
            committed = self._committed(actor, path, admission)
            self._index_receipt(actor.tenant, doc["receipt_ref"], attempt_id)
            self.fault("end", "before_ack")
            return committed["ack"], True

    def _check_sources(self, actor, admission, terminal, objects, expected):
        if admission["operation"] == "compare":
            sources = self._sources(actor, admission)
            if self._lineage(sources, terminal, objects) != expected:
                raise Conflict("comparison source version changed")

    def _index_receipt(self, tenant, receipt, attempt_id):
        if self.receipts.setdefault((tenant, receipt), attempt_id) != attempt_id:
            raise IntegrityError("receipt has multiple committed bindings")

    def _inspection(self, actor, path, admission):
        if not self.disk.exists(path + ("start.json",)):
            raise Unavailable("unavailable")
        if self.disk.exists(path + ("end.json",)):
            graph = self._committed(actor, path, admission)
            return dict(state="committed", tenant=actor.tenant, admission=admission,
                        start=graph["start"], terminal=graph["terminal"], **graph["ack"],
                        receipt_ref=graph["intent"]["receipt_ref"],
                        artifact_ref=graph["intent"]["artifact"]["artifact_ref"])
        _, start, ack = self._start(actor, path, admission)
        claimed = self._claimed(path, admission, ack)
        return dict(state="unknown", attempt_id=path[-1], tenant=actor.tenant,
                    admission=admission, start=start, start_sha256=ack["start_sha256"],
                    terminal=None, retirement="unknown", launch_claimed=claimed)

    def _inspection_path(self, actor, attempt_id):
        path = self._attempt(actor, "inspect", attempt_id)
        if not self.disk.exists(path + ("start.json",)):
            raise Unavailable("unavailable")
        admission = self._admission(path)
        self._grant(actor, "inspect", attempt_id, admission)
        return path, admission

    def inspect(self, actor, attempt_id):
        with self.transaction():
            path, admission = self._inspection_path(actor, attempt_id)
            return self._inspection(actor, path, admission)

    def receipt(self, actor, receipt_ref):
        with self.transaction():
            self._grant(actor, "inspect")
            self.validator.validate("ReceiptRef", receipt_ref)
            attempt_id = self.receipts.get((actor.tenant, receipt_ref))
            if attempt_id is None:
                raise Unavailable("unavailable")
            path, admission = self._inspection_path(actor, attempt_id)
            result = self._inspection(actor, path, admission)
            if result.get("receipt_ref") != receipt_ref:
                raise IntegrityError("receipt lookup no longer binds committed graph")
            return result

    def artifact(self, actor, attempt_id, artifact_ref):
        with self.transaction():
            path, admission = self._inspection_path(actor, attempt_id)
            if not self.disk.exists(path + ("end.json",)):
                raise Unavailable("unavailable")
            graph = self._committed(actor, path, admission)
            if graph["intent"]["artifact"]["artifact_ref"] != artifact_ref:
                raise Unavailable("unavailable")
            return graph["artifact"]

    @staticmethod
    def _page_bounds(offset, limit):
        require(type(offset) is int and 0 <= offset <= MAX_OFFSET
                and type(limit) is int and 1 <= limit <= MAX_PAGE_BYTES,
                "invalid pagination limits")

    def object(self, actor, attempt_id, object_ref, *, offset=0, limit=MAX_PAGE_BYTES):
        with self.transaction():
            path, admission = self._inspection_path(actor, attempt_id)
            if not self.disk.exists(path + ("end.json",)):
                raise Unavailable("unavailable")
            graph = self._committed(actor, path, admission)
            if object_ref not in graph["objects"]:
                raise Unavailable("unavailable")
            self._page_bounds(offset, limit)
            doc = graph["uploads"][object_ref]
            target = path + ("uploads", digest(doc["upload_id"].encode("ascii")), "bytes")
            return self._file_page(target, object_ref, doc, self.policy.object_bytes, offset, limit)

    def _file_page(self, path, ref, doc, bound, offset, limit):
        if (doc["bytes"] and offset >= doc["bytes"]) or (not doc["bytes"] and offset):
            actual, _ = self.disk.metadata(path, bound=bound)
            if any(actual[key] != doc[key] for key in actual):
                raise IntegrityError("page source changed")
            raise RangeError("page offset is outside verified bytes")
        actual, data = self.disk.metadata(path, bound=bound, page=(offset, limit))
        if any(actual[key] != doc[key] for key in actual):
            raise IntegrityError("page source changed")
        return Page(ref, doc["bytes"], doc["sha256"], offset, data)
