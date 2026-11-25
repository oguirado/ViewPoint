"""Snapshot inventories, deletion fencing and isolated restoration."""

from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from time import monotonic
import re
from uuid import uuid4

from ..contracts.validation import strict_json
from ..contracts.wire import MAX_RECORD_BYTES, record_bytes
from .bindings import require
from .files import Conflict, IntegrityError, RangeError, StorageFailure, Unavailable, digest

from .model import (
    ATTEMPTS, MAX_INTENT, MAX_METADATA, TENANTS, TOMBSTONES, Actor, Page, timestamp, when,
)

HEX_ID = re.compile(r"[a-f0-9]{32}\Z")
MAX_SNAPSHOTS = 16
MAX_SNAPSHOT_RAM = 64 << 20
RECOVERY_KINDS = {
    "admission.json": "admission", "start.json": "start", "launch-claim.json": "launch_claim",
    "commit-intent.json": "commit_intent", "end.json": "terminal",
}
SIDE_FILES = ("start-proof.json", "commit-time.json")


@dataclass
class Snapshot:
    owner: object
    expiry: datetime
    deadline: float
    manifest: bytes
    root: dict
    entries: dict
    cursors: dict
    scopes: set
    frozen: dict
    admissions: dict
    charged_bytes: int


def _item_token(snapshot, attempt_id, name):
    return "obj_" + digest(record_bytes([snapshot, attempt_id, name]))[:32]


class Recovery:
    def _tenant_actor(self, tenant_path):
        with self.stored():
            doc = self.disk.json(tenant_path + ("tenant.json",), MAX_METADATA)
            name = doc["tenant"]
            self.validator.validate("Id", name)
            require(digest(name.encode("ascii")) == tenant_path[-1], "tenant marker mismatch")
        return Actor(name, name)

    def _fence(self, actor, tenant, *, check_grant=False):
        records = []
        for name in self.disk.names(tenant + (TOMBSTONES,)):
            if not HEX_ID.fullmatch(name):
                # Unpublished temporary tombstones survive as private residue.
                continue
            if check_grant:
                self._grant(actor, "recover", name)
            path = tenant + (TOMBSTONES, name)
            with self.stored():
                doc = self.disk.json(path, MAX_METADATA)
                self.validator.validate("Tombstone", doc)
                require(doc["attempt_id"] == name and doc["tenant"] == actor.tenant,
                        "tombstone tenancy mismatch")
                require(when(doc["eligible_at"]) <= when(doc["tombstoned_at"])
                        and when(doc["eligible_at"]) < when(doc["purge_due_at"]),
                        "invalid tombstone clock")
            records.append(doc)
        records.sort(key=lambda item: item["deletion_seq"])
        if any(doc["deletion_seq"] != seq for seq, doc in enumerate(records, 1)):
            raise IntegrityError("tenant deletion sequence is incomplete")
        high_water = tenant + ("deletion-high-water.json",)
        pinned = 0
        if self.disk.exists(high_water):
            with self.stored():
                pinned = self.disk.json(high_water, MAX_METADATA)["deletion_high_water"]
                self.validator.validate("Size", pinned)
        if len(records) < pinned:
            raise IntegrityError("tenant deleted a tombstone below its high-water mark")
        return records

    def _pin_fence(self, tenant, count):
        path = tenant + ("deletion-high-water.json",)
        doc = {"deletion_high_water": count}
        if not count:
            return
        if self.disk.exists(path) and self.disk.json(path, MAX_METADATA) == doc:
            return
        self.disk.publish(path, record_bytes(doc), "fence", replace=True)

    def _purge(self, tenant, tombstone):
        attempt_id = tombstone["attempt_id"]
        self.disk.remove(tenant + (ATTEMPTS, attempt_id))
        for key, bound in tuple(self.receipts.items()):
            if key[0] == tombstone["tenant"] and bound == attempt_id:
                del self.receipts[key]

    def _reopen(self):
        for name in self.disk.names(TENANTS):
            tenant = TENANTS + (name,)
            if not self.disk.exists(tenant + ("tenant.json",)):
                continue
            actor = self._tenant_actor(tenant)
            self._sync_tree(tenant)
            fence = self._fence(actor, tenant)
            self._pin_fence(tenant, len(fence))
            for doc in fence:
                self._purge(tenant, doc)
            for attempt in self.disk.names(tenant + (ATTEMPTS,)):
                if not HEX_ID.fullmatch(attempt):
                    continue
                path = tenant + (ATTEMPTS, attempt)
                if not self.disk.exists(path + ("end.json",)):
                    self._disposition(actor, path)
                    continue
                if not self.disk.exists(path + ("start.json",)):
                    continue
                try:
                    graph = self._committed(actor, path)
                except IntegrityError:
                    # Retain a known receipt binding so an authorized request
                    # reports integrity failure for a corrupt published graph.
                    try:
                        intent, _ = self._intent(path)
                    except IntegrityError:
                        continue
                else:
                    intent = graph["intent"]
                self._index_receipt(actor.tenant, intent["receipt_ref"], attempt)

    def _sync_tree(self, path):
        # File sync precedes storage rename. Re-sync directory entries which
        # may have survived a crash before the writer could acknowledge them.
        for name in self.disk.names(path):
            child = path + (name,)
            if self.disk.isdir(child):
                self._sync_tree(child)
        self.disk.sync(path)

    def _holds(self, path):
        state = path + ("holds", "state.json")
        if not self.disk.exists(state):
            return {"holds": [], "last_released_at": None}
        with self.stored():
            doc = self.disk.json(state, MAX_METADATA)
            require(doc.keys() == {"holds", "last_released_at"} and isinstance(doc["holds"], list)
                    and sorted(set(doc["holds"])) == doc["holds"], "invalid hold state")
            for hold in doc["holds"]:
                self.validator.validate("PathId", hold)
            if doc["last_released_at"] is not None:
                self.validator.validate("Time", doc["last_released_at"])
        return doc

    def hold(self, actor, attempt_id, hold_id, *, enable):
        with self.transaction():
            path = self._attempt(actor, "hold", attempt_id)
            admission = self._admission(path)
            self._grant(actor, "hold", attempt_id, admission)
            self.validator.validate("PathId", hold_id)
            require(type(enable) is bool, "hold action must be boolean")
            current = self._holds(path)
            holds = set(current["holds"])
            if (hold_id in holds) == enable:
                return False
            if enable:
                holds.add(hold_id)
            else:
                holds.remove(hold_id)
                if not holds:
                    current["last_released_at"] = timestamp(self.clock())
            current["holds"] = sorted(holds)
            raw = record_bytes(current)
            require(len(raw) <= MAX_METADATA, "hold state exceeds byte limit")
            self._reserve(path, len(raw))
            self.disk.publish(path + ("holds", "state.json"), raw, "hold",
                              staging=path + ("staging",), replace=True)
            return True

    def sweep(self):
        """Run the retention scheduler's serialized pass over local managed bytes."""
        with self.transaction():
            count = 0
            for name in self.disk.names(TENANTS):
                tenant = TENANTS + (name,)
                if not self.disk.exists(tenant + ("tenant.json",)):
                    continue
                actor = self._tenant_actor(tenant)
                fence = self._fence(actor, tenant)
                self._pin_fence(tenant, len(fence))
                for doc in fence:
                    self._purge(tenant, doc)
                high = len(fence)
                for attempt_id in self.disk.names(tenant + (ATTEMPTS,)):
                    path = tenant + (ATTEMPTS, attempt_id)
                    if not HEX_ID.fullmatch(attempt_id) or not self.disk.exists(path + ("end.json",)):
                        continue
                    try:
                        graph = self._committed(actor, path)
                    except IntegrityError:
                        continue
                    pins = self._holds(path)
                    if pins["holds"]:
                        continue
                    eligible = when(graph["expires_at"])
                    if pins["last_released_at"] is not None:
                        eligible = max(eligible, when(pins["last_released_at"]))
                    now = self.clock()
                    if now < eligible:
                        continue
                    high += 1
                    doc = dict(schema="retention.tombstone/v1", tenant=actor.tenant,
                               attempt_id=attempt_id, deletion_seq=high,
                               policy_revision=graph["policy_revision"],
                               eligible_at=timestamp(eligible), tombstoned_at=timestamp(now),
                               purge_due_at=timestamp(eligible + self.policy.purge_window))
                    self.validator.validate("Tombstone", doc)
                    self.disk.publish(tenant + (TOMBSTONES, attempt_id), record_bytes(doc), "tombstone")
                    self._pin_fence(tenant, high)
                    self.fault("tombstone", "before_purge")
                    self._invalidate_snapshots(actor.tenant)
                    self._purge(tenant, doc)
                    self.fault("tombstone", "before_ack")
                    count += 1
            return count

    def _disposition(self, actor, path):
        if not self.disk.exists(path + ("start.json",)):
            if (self._has_members(path + ("staging",))
                    or self.disk.exists(path + ("artifact",))
                    or self.disk.exists(path + ("end.json",))
                    or self.disk.exists(path + ("start-proof.json",))
                    or self.disk.exists(path + ("commit-intent.json",))
                    or self.disk.exists(path + ("launch-claim.json",))
                    or self._has_members(path + ("uploads",))):
                return "orphaned", None
            return "unknown", None
        try:
            admission = self._admission(path)
            _, _, ack = self._start(actor, path, admission)
            claimed = self._claimed(path, admission, ack)
            if self.disk.exists(path + ("end.json",)):
                return "committed", self._committed(actor, path, admission)
            if self.disk.exists(path + ("commit-intent.json",)):
                self._intent(path)
                return "terminal_commit_interrupted", None
            if self.disk.exists(path + ("artifact",)):
                return "terminal_commit_interrupted", None
            return ("unknown" if claimed else "started_only"), None
        except IntegrityError:
            return "unknown", None

    def _has_members(self, path):
        return self.disk.exists(path) and bool(self.disk.names(path))

    def _clear_expired(self):
        now = self.clock()
        tick = monotonic()
        for snapshot, entry in tuple(self.snapshots.items()):
            if now >= entry.expiry or tick >= entry.deadline:
                del self.snapshots[snapshot]

    def _snapshot_size(self, tenant):
        self._clear_expired()
        return sum(item.charged_bytes for item in self.snapshots.values()
                   if digest(item.owner.tenant.encode("ascii")) == tenant[-1])

    def _invalidate_snapshots(self, tenant):
        for key, snapshot in tuple(self.snapshots.items()):
            if snapshot.owner.tenant == tenant:
                del self.snapshots[key]

    def _inventory(self, actor, tenant, snapshot_id, tombstones):
        entries, attempts, frozen, admissions = {}, [], {}, {}
        estimated = 0

        def add(path, attempt, kind, disposition, *, tag=None, freeze=False):
            nonlocal estimated
            # Count the serialized item before opening its bytes; the running
            # bound also caps the in-memory lookup table across small entries.
            estimated += 384
            if estimated > self.policy.snapshot_bytes:
                raise StorageFailure("snapshot inventory quota exhausted")
            ref = _item_token(snapshot_id, attempt, tag or list(path))
            if ref in entries:
                raise IntegrityError("duplicate snapshot item token")
            if freeze:
                raw = self.disk.read(path, MAX_METADATA)
                estimated += len(raw)
                if estimated > self.policy.snapshot_bytes:
                    raise StorageFailure("snapshot inventory quota exhausted")
                frozen[ref] = raw
                meta = {"bytes": len(raw), "sha256": digest(raw)}
            else:
                meta, _ = self.disk.metadata(path, bound=max(self.policy.object_bytes, MAX_INTENT))
            item = dict(item=ref, attempt_id=attempt, kind=kind, disposition=disposition, **meta)
            entries[ref] = (item, path)
            return item

        def stage(path, attempt, disposition):
            for name in self.disk.names(path):
                child = path + (name,)
                if self.disk.isdir(child):
                    stage(child, attempt, disposition)
                else:
                    add(child, attempt, "staging", disposition)

        for attempt in self.disk.names(tenant + (ATTEMPTS,)):
            path = tenant + (ATTEMPTS, attempt)
            if not HEX_ID.fullmatch(attempt) or not self.disk.exists(path + ("admission.json",)):
                continue
            self._grant(actor, "recover", attempt)
            try:
                admission = self._admission(path)
            except IntegrityError:
                continue
            self._grant(actor, "recover", attempt, admission)
            admissions[attempt] = admission
            estimated += len(record_bytes(admission))
            disposition, graph = self._disposition(actor, path)
            uploads, artifact, lineage, receipt = [], None, [], None
            committed_at, expires_at = None, None
            revision = self.policy.revision
            for name, kind in RECOVERY_KINDS.items():
                child = path + (name,)
                if self.disk.exists(child):
                    add(child, attempt, kind, disposition)
            for name in SIDE_FILES:
                if self.disk.exists(path + (name,)):
                    add(path + (name,), attempt, "staging", disposition, tag=name, freeze=True)
            hold_path = path + ("holds", "state.json")
            if self.disk.exists(hold_path):
                add(hold_path, attempt, "hold", disposition, freeze=True)
            for key in self.disk.names(path + ("uploads",)):
                upload = path + ("uploads", key)
                try:
                    doc = self._upload(upload)
                except IntegrityError:
                    stage(upload, attempt, disposition)
                    continue
                item = add(upload + ("bytes",), attempt, "object", disposition)
                uploads.append(dict(doc, item=item["item"]))
            if self.disk.exists(path + ("artifact",)):
                base = path + ("artifact",)
                try:
                    with self.stored():
                        meta = self.disk.json(base + ("meta.json",), MAX_METADATA)
                        self.validator.validate("ArtRef", meta["artifact_ref"])
                        raw = self.disk.read(base + ("bytes",), 65_536)
                        require(meta["bytes"] == len(raw) and meta["sha256"] == digest(raw),
                                "unverified inventory artifact")
                except IntegrityError:
                    stage(base, attempt, disposition)
                else:
                    item = add(base + ("bytes",), attempt, "artifact", disposition)
                    artifact = dict(meta, item=item["item"])
            if graph is not None:
                lineage = graph["intent"]["lineage"]
                receipt = graph["intent"]["receipt_ref"]
                committed_at, expires_at = graph["committed_at"], graph["expires_at"]
                revision = graph["policy_revision"]
            stage(path + ("staging",), attempt, disposition)
            attempts.append(dict(attempt_id=attempt, disposition=disposition, uploads=uploads,
                                 artifact=artifact, receipt_ref=receipt, holds=self._holds(path)["holds"],
                                 source_lineage=lineage, policy_revision=revision,
                                 committed_at=committed_at, expires_at=expires_at))
        for doc in tombstones:
            attempt = doc["attempt_id"]
            add(tenant + (TOMBSTONES, attempt), attempt, "tombstone", "tombstoned")
        return attempts, entries, frozen, admissions

    def _snapshot(self, actor):
        tenant = self._tenant(actor.tenant)
        self._clear_expired()
        if len(self.snapshots) >= MAX_SNAPSHOTS:
            raise StorageFailure("too many active recovery snapshots")
        fence = self._fence(actor, tenant, check_grant=True)
        snapshot_id = uuid4().hex
        now = self.clock()
        expiry = now + self.policy.snapshot_lifetime
        deadline = monotonic() + self.policy.snapshot_lifetime.total_seconds()
        attempts, entries, frozen, admissions = self._inventory(actor, tenant, snapshot_id, fence)
        manifest = dict(schema="retention.recovery_manifest/v1", tenant=actor.tenant,
                        snapshot=snapshot_id, created_at=timestamp(now), snapshot_expires_at=timestamp(expiry),
                        policy_revision=self.policy.revision, deletion_high_water=len(fence),
                        attempts=attempts, items=[item for item, _ in entries.values()])
        self.validator.validate("Manifest", manifest)
        raw = record_bytes(manifest)
        charged = len(raw) + sum(map(len, frozen.values()))
        charged += sum(len(record_bytes(admission)) for admission in admissions.values())
        snapshot_usage = sum(item.charged_bytes for item in self.snapshots.values())
        if (charged > self.policy.snapshot_bytes or snapshot_usage + charged > MAX_SNAPSHOT_RAM
                or self.disk.size(tenant) + self._snapshot_size(tenant) + charged > self.policy.tenant_bytes):
            raise StorageFailure("snapshot quota exhausted")
        token = _item_token(snapshot_id, None, "manifest")
        root = dict(item=token, attempt_id=None, kind="manifest", bytes=len(raw),
                    sha256=digest(raw), disposition="committed")
        scopes = {item["attempt_id"] for item, _ in entries.values()}
        if self.clock() >= expiry or monotonic() >= deadline:
            raise StorageFailure("snapshot expired while reading the inventory")
        snap = Snapshot(actor, expiry, deadline, raw, root, entries, {}, scopes, frozen,
                        admissions, charged)
        self.snapshots[snapshot_id] = snap
        return snapshot_id, snap

    def _snapshot_grants(self, snapshot):
        for attempt in sorted(snapshot.scopes):
            self._grant(snapshot.owner, "recover", attempt, snapshot.admissions.get(attempt))

    def recovery_page(self, actor, *, limit, cursor=None):
        with self.transaction():
            self._grant(actor, "recover")
            query = {"limit": limit}
            if cursor is not None:
                query["cursor"] = cursor
            self.validator.validate("RecoveryQuery", query)
            self._clear_expired()
            if cursor is None:
                snapshot_id, snapshot = self._snapshot(actor)
                position = 0
            else:
                snapshot_id = cursor.split(".", 1)[0]
                snapshot = self.snapshots.get(snapshot_id)
                if snapshot is None or snapshot.owner != actor or cursor not in snapshot.cursors:
                    raise Unavailable("unavailable")
                self._snapshot_grants(snapshot)
                position = snapshot.cursors[cursor]
            end = position + limit
            entries = islice(snapshot.entries.values(), max(0, position - 1), max(0, end - 1))
            items = [item.copy() for item, _ in entries]
            if position == 0:
                items.insert(0, snapshot.root.copy())
            next_cursor = None
            if end < len(snapshot.entries) + 1:
                next_cursor = f"{snapshot_id}.{uuid4().hex}"
                # One continuation per position even with repeated requests.
                for token, index in snapshot.cursors.items():
                    if index == end:
                        next_cursor = token
                        break
                snapshot.cursors[next_cursor] = end
            return dict(snapshot=snapshot_id, manifest_item=snapshot.root["item"],
                        manifest_sha256=snapshot.root["sha256"], items=items, next=next_cursor)

    def recovery_item(self, actor, token, *, offset=0, limit=1 << 20):
        with self.transaction():
            self._grant(actor, "recover")
            self.validator.validate("ObjRef", token)
            self._clear_expired()
            for snapshot in self.snapshots.values():
                if snapshot.owner != actor:
                    continue
                if token == snapshot.root["item"]:
                    self._snapshot_grants(snapshot)
                    self._page_bounds(offset, limit)
                    if offset >= len(snapshot.manifest):
                        raise RangeError("page offset is outside verified bytes")
                    return Page(token, len(snapshot.manifest), snapshot.root["sha256"], offset,
                                snapshot.manifest[offset:offset + limit])
                if token in snapshot.entries:
                    item, path = snapshot.entries[token]
                    attempt = item["attempt_id"]
                    self._grant(actor, "recover", attempt, snapshot.admissions.get(attempt))
                    self._page_bounds(offset, limit)
                    if token in snapshot.frozen:
                        raw = snapshot.frozen[token]
                        if offset >= len(raw):
                            raise RangeError("page offset is outside verified bytes")
                        return Page(token, len(raw), item["sha256"], offset, raw[offset:offset + limit])
                    if not self.disk.exists(path):
                        self._invalidate_snapshots(actor.tenant)
                        raise Unavailable("unavailable")
                    try:
                        return self._file_page(path, token, item,
                                               max(self.policy.object_bytes, MAX_INTENT), offset, limit)
                    except IntegrityError as exc:
                        # A mutable source (for example hold state) changed.
                        # Require a new complete snapshot after revocation.
                        self._invalidate_snapshots(actor.tenant)
                        raise Unavailable("unavailable") from exc
            raise Unavailable("unavailable")

    def restore(self, actor, manifest_raw, pages, fetch, *, manifest_sha256, authority):
        """Restore a verified inventory into this isolated, empty store root.

        pages is the complete ordered RecoveryPage stream. fetch takes the item
        token, offset and limit and returns a verified Page from the export.
        manifest_sha256 is the root digest from the authenticated inventory.
        authority is the current store for this tenant, consulted under its
        transaction lock at cutover. Both local roots must be accessible.
        """

        if authority is self:
            raise ValueError("restore authority must be a different store")
        with self.transaction():
            self._grant(actor, "recover")
            if self.disk.names(TENANTS):
                raise Conflict("restore requires an isolated empty root")
            require(isinstance(manifest_raw, bytes) and len(manifest_raw) <= self.policy.snapshot_bytes,
                    "restore manifest exceeds quota")
            require(digest(manifest_raw) == manifest_sha256,
                    "restore manifest does not match the authenticated root digest")
            manifest = strict_json(manifest_raw)
            self.validator.validate("Manifest", manifest)
            require(manifest["tenant"] == actor.tenant and record_bytes(manifest) == manifest_raw,
                    "restore manifest has wrong tenant or encoding")
            by_attempt, fence = self._restore_inventory(actor, manifest, manifest_raw, pages)
            marker = ("policy", "restore-incomplete")
            self.disk.publish(marker, b"restore in progress\n", "restore")
            try:
                target = self._tenant(actor.tenant, create=True)
                for attempt in manifest["attempts"]:
                    attempt_id = attempt["attempt_id"]
                    self._grant(actor, "recover", attempt_id)
                    self._restore_attempt(actor, target, attempt, by_attempt[attempt_id], fetch,
                                          manifest["snapshot"])
                witnessed = self._restore_tombstones(actor, target, fence, fetch)
                with authority.transaction():
                    authority._grant(actor, "recover")
                    source = authority._tenant(actor.tenant)
                    current = authority._fence(actor, source, check_grant=True)
                    require(len(current) >= len(witnessed) and current[:len(witnessed)] == witnessed,
                            "restore deletion fence is stale or disagrees with authority")
                    for doc in current[len(witnessed):]:
                        path = target + (TOMBSTONES, doc["attempt_id"])
                        self.disk.publish(path, record_bytes(doc), "tombstone")
                    self._pin_fence(target, len(current))
                    for doc in current:
                        self._purge(target, doc)
                    self._reopen()
                    self.disk.remove(marker)
            except BaseException:
                self.failed = True
                raise

    def _restore_inventory(self, actor, manifest, raw, pages):
        root = dict(item=_item_token(manifest["snapshot"], None, "manifest"),
                    attempt_id=None, kind="manifest", bytes=len(raw), sha256=digest(raw),
                    disposition="committed")
        expected = [root, *manifest["items"]]
        tokens = {item["item"] for item in expected}
        require(len(tokens) == len(expected), "restore inventory has duplicate item tokens")
        seen = 0
        cursors = set()
        final = False
        for page in pages:
            self.validator.validate("RecoveryPage", page)
            require(not final and page["snapshot"] == manifest["snapshot"]
                    and page["manifest_item"] == root["item"]
                    and page["manifest_sha256"] == root["sha256"]
                    and bool(page["items"]), "restore page has wrong snapshot or manifest binding")
            end = seen + len(page["items"])
            require(page["items"] == expected[seen:end], "restore inventory coverage mismatch")
            seen = end
            cursor = page["next"]
            final = cursor is None
            if not final:
                require(cursor not in cursors, "restore page repeats a continuation")
                cursors.add(cursor)
        require(final and seen == len(expected), "restore inventory is incomplete")
        attempts = {doc["attempt_id"]: doc for doc in manifest["attempts"]}
        require(len(attempts) == len(manifest["attempts"]), "restore has duplicate attempt mappings")
        by_attempt = {key: {} for key in attempts}
        fence = []
        tombstoned = set()
        for item in manifest["items"]:
            self._grant(actor, "recover", item["attempt_id"])
            attempt = item["attempt_id"]
            if item["kind"] == "tombstone":
                require(attempt not in attempts and attempt not in tombstoned,
                        "restore reuses a deleted attempt ID")
                tombstoned.add(attempt)
                fence.append(item)
            else:
                require(item["kind"] not in {"manifest", "reconciliation"} and attempt in attempts
                        and item["disposition"] == attempts[attempt]["disposition"]
                        and item["disposition"] != "tombstoned",
                        "restore has an unbound item")
                by_attempt[attempt][item["item"]] = item
        require(len(fence) == manifest["deletion_high_water"], "restore deletion sequence is incomplete")
        return by_attempt, fence

    def _restore_tombstones(self, actor, target, fence, fetch):

        records = []
        for item in fence:
            require(item["bytes"] <= MAX_METADATA, "oversized tombstone")
            raw = ExportStream(item, fetch).read(item["bytes"])
            require(len(raw) == item["bytes"] and digest(raw) == item["sha256"],
                    "tombstone inventory bytes changed")
            doc = strict_json(raw)
            self.validator.validate("Tombstone", doc)
            require(doc["tenant"] == actor.tenant and doc["attempt_id"] == item["attempt_id"]
                    and record_bytes(doc) == raw, "tombstone does not bind its inventory item")
            path = target + (TOMBSTONES, doc["attempt_id"])
            self.disk.publish(path, raw, "tombstone")
            records.append(doc)
        records.sort(key=lambda doc: doc["deletion_seq"])
        require(all(doc["deletion_seq"] == index for index, doc in enumerate(records, 1)),
                "tombstone deletion sequence has gaps")
        return records

    def _restore_attempt(self, actor, tenant, binding, items, fetch, snapshot):
        attempt_id = binding["attempt_id"]
        path = tenant + (ATTEMPTS, attempt_id)
        for directory in (path, path + ("staging",), path + ("uploads",), path + ("holds",)):
            self.disk.mkdir(directory)

        def copy(item, dest, bound):
            require(item["bytes"] <= bound, "restored item exceeds its byte limit")
            self._reserve(path, item["bytes"])
            stream = ExportStream(item, fetch)
            if item["bytes"] == 0:
                stream.check_empty()
            self.disk.write(dest, stream, length=item["bytes"], sha=item["sha256"], label="restore")

        admissions = [item for item in items.values() if item["kind"] == "admission"]
        require(len(admissions) == 1, "restore requires one admission per attempt")
        copy(admissions[0], path + ("admission.json",), MAX_METADATA)
        self._grant(actor, "recover", attempt_id, self._admission(path))

        objects = {}
        upload_ids = set()
        for upload in binding["uploads"]:
            token = upload["item"]
            require(token in items and token not in objects and upload["upload_id"] not in upload_ids
                    and items[token]["kind"] == "object"
                    and all(items[token][key] == upload[key] for key in ("bytes", "sha256")),
                    "restore has an invalid upload binding")
            upload_ids.add(upload["upload_id"])
            objects[token] = upload
        files = {kind: name for name, kind in RECOVERY_KINDS.items()}
        seen = set()
        for token, item in items.items():
            kind = item["kind"]
            if kind == "admission":
                continue
            if kind in files or kind in {"hold", "artifact"}:
                require(kind not in seen, "restore has duplicate single-file bindings")
                seen.add(kind)
            if kind in files:
                bound = {"start": MAX_RECORD_BYTES, "terminal": MAX_RECORD_BYTES,
                         "commit_intent": MAX_INTENT}.get(kind, MAX_METADATA)
                copy(item, path + (files[kind],), bound)
            elif kind == "hold":
                copy(item, path + ("holds", "state.json"), MAX_METADATA)
            elif kind == "artifact":
                artifact = binding["artifact"]
                require(artifact is not None and artifact["item"] == token
                        and all(artifact[key] == item[key] for key in ("bytes", "sha256")),
                        "restore has an invalid artifact binding")
                base = path + ("artifact",)
                self.disk.mkdir(base)
                copy(item, base + ("bytes",), 65_536)
                self._restore_meta(path, base, artifact)
            elif kind == "object":
                require(token in objects, "restore has an unbound object")
                upload = objects.pop(token)
                base = path + ("uploads", digest(upload["upload_id"].encode("ascii")))
                self.disk.mkdir(base)
                copy(item, base + ("bytes",), self.policy.object_bytes)
                self._restore_meta(path, base, upload)
            else:
                require(kind == "staging", "unsupported restore item")
                for name in SIDE_FILES:
                    if token == _item_token(snapshot, attempt_id, name):
                        copy(item, path + (name,), MAX_METADATA)
                        break
                else:
                    copy(item, path + ("staging", token), max(self.policy.object_bytes, MAX_INTENT))
        require(not objects
                and ("artifact" in seen) == (binding["artifact"] is not None)
                and binding["holds"] == self._holds(path)["holds"],
                "restore does not cover its attempt bindings")
        actual, graph = self._disposition(actor, path)
        require(actual == binding["disposition"], "restore changed the attempt disposition")
        if graph is not None:
            require(all(graph[key] == binding[key] for key in
                        ("policy_revision", "committed_at", "expires_at"))
                    and graph["intent"]["receipt_ref"] == binding["receipt_ref"]
                    and graph["intent"]["lineage"] == binding["source_lineage"],
                    "restore changed the committed manifest or lineage")

    def _restore_meta(self, path, destination, binding):
        raw = record_bytes({key: value for key, value in binding.items() if key != "item"})
        self._reserve(path, len(raw))
        self.disk.publish(destination + ("meta.json",), raw, "restore-meta")


class ExportStream:
    """Sequential, bounded reader over one exported recovery item."""

    def __init__(self, item, fetch):
        self.item = item
        self.fetch = fetch
        self.offset = 0

    def _page(self, size):
        item = self.item
        page = self.fetch(item["item"], self.offset, size)
        require((page.ref, page.bytes, page.sha256, page.offset) ==
                (item["item"], item["bytes"], item["sha256"], self.offset)
                and isinstance(page.data, bytes)
                and len(page.data) == min(size, item["bytes"] - self.offset),
                "export page changed its item or offset")
        return page.data

    def check_empty(self):
        self._page(1)

    def read(self, size):
        if self.offset >= self.item["bytes"]:
            return b""
        data = self._page(min(size, 1 << 20))
        self.offset += len(data)
        return data
