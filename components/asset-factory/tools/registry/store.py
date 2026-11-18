"""Publish verified record closures through a single durable snapshot pointer."""

import hashlib
import json
import os
import re
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path

from asset_factory import core, math_acceptance

from . import _disk
from ._disk import RegistryError
from ._records import is_digest, verify_closure

_ID = r"[a-z][a-z0-9_-]{0,62}"
_IDENTIFIER = re.compile(_ID)
_LEGACY_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]*")
_LOCATOR = re.compile(rf"af://({_ID})/({_ID})(?:@(sha256:[0-9a-f]{{64}})|:({_ID}))")
_RELEASE = re.compile(r"v[0-9]+")
_LEGACY_SNAPSHOT = "af.registry-snapshot/v1"
_SNAPSHOT = "af.registry-snapshot/v2"
_HIGH_WATER = re.compile(rb"(sha256:[0-9a-f]{64}) (0|[1-9][0-9]{0,19})\n")
_UNSET = object()


def _digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _encoded(obj):
    return core.canonical_json(obj).encode("utf-8")


def _identifier(value):
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _tag(value):
    return _identifier(value) and value != "latest"


def _catalog_shape(obj, namespace):
    if not isinstance(obj, dict) or obj.keys() != {"schema", "namespace", "previous", "names"}:
        return False
    if obj["schema"] not in (_SNAPSHOT, _LEGACY_SNAPSHOT) or obj["namespace"] != namespace:
        return False
    if obj["previous"] is not None and not is_digest(obj["previous"]):
        return False
    names = obj["names"]
    if not isinstance(names, dict) or (bool(names) != (obj["previous"] is not None)):
        return False
    for slug, entry in names.items():
        # Accept legacy names when decoding an immutable catalog.
        if not _LEGACY_IDENTIFIER.fullmatch(slug) or not isinstance(entry, dict):
            return False
        if entry.keys() != {"records", "tags"}:
            return False
        members, tags = entry["records"], entry["tags"]
        if not isinstance(members, list) or not members or not all(map(is_digest, members)):
            return False
        if members != sorted(set(members)) or not isinstance(tags, dict):
            return False
        if any(tag == "latest" or not _LEGACY_IDENTIFIER.fullmatch(tag) or target not in members
               for tag, target in tags.items()):
            return False
    return True


class Registry:
    """Open a service-owned registry for a namespace authorized by the caller.

    For PASS assertions, `evidence_policy(record_digest)` returns BASE's approved
    scope map and `read_checked(role, digest)` supplies independently authorized
    witness bytes. Both are supplied by the trusted service, outside the record.
    """

    def __init__(self, root, *, namespace, evidence_policy=None, read_checked=None):
        self.root = Path(root)
        if not self.root.is_absolute() or ".." in self.root.parts or self.root == Path("/"):
            raise RegistryError(2, "invalid_store_root")
        if not _identifier(namespace):
            raise RegistryError(2, "invalid_namespace")
        if (evidence_policy is None) != (read_checked is None):
            raise RegistryError(2, "incomplete_evidence_policy")
        self.namespace = namespace
        self._evidence_policy = evidence_policy if evidence_policy is not None else lambda _pin: {}
        self._trusted_read = read_checked if read_checked is not None else lambda _role, _pin: None

    @contextmanager
    def _writer(self):
        try:
            with _disk.root_directory(self.root) as root, _disk.writer(root):
                yield root
        except OSError as exc:
            raise RegistryError(3, "store_io:" + str(exc.errno)) from exc

    def initialize(self):
        """Explicitly create the genesis snapshot in an existing directory."""
        with self._writer() as root:
            for name in ("blobs", "snapshots", "staging", "quarantine"):
                _disk.ensure_directory(root, name)
            with _disk.directory(root, "blobs") as blobs:
                _disk.ensure_directory(blobs, "sha256")
            with _disk.directory(root, "staging") as staging:
                _disk.recover_staging(staging)
                try:
                    self._current(root)
                except FileNotFoundError:
                    genesis = dict(schema=_SNAPSHOT, namespace=self.namespace, previous=None, names={})
                    digest = math_acceptance.canonical_digest(genesis)
                    with _disk.directory(root, "snapshots") as snapshots:
                        if set(os.listdir(snapshots)) - {digest[7:]}:
                            raise RegistryError(3, "missing_current")
                    mark, depth = self._high_water(root)
                    if mark is not None and (mark != digest or depth != 0):
                        raise RegistryError(4, "snapshot_rollback")
                    self._publish(root, staging, digest, genesis, 0)
                    return digest
                return self._writer_current(root, staging)[0]

    def _load_catalog(self, root, digest):
        try:
            with _disk.directory(root, "snapshots") as snapshots:
                with _disk.directory(snapshots, digest[7:]) as node:
                    _, raw = _disk.read_checked(node, "catalog.json", digest[7:], load=True)
        except FileNotFoundError as exc:
            raise RegistryError(3, "missing_catalog") from exc
        try:
            obj = json.loads(raw)
            if not _catalog_shape(obj, self.namespace) or _encoded(obj) != raw:
                raise RegistryError(4, "invalid_catalog")
        except (ValueError, RecursionError, core.ContractError) as exc:
            raise RegistryError(4, "invalid_catalog") from exc
        return obj

    def _current(self, root):
        with _disk.regular(root, "CURRENT") as file:
            try:
                raw = file.read(73).decode("ascii")
            except UnicodeError as exc:
                raise RegistryError(4, "invalid_current") from exc
        digest = raw.removesuffix("\n")
        if not is_digest(digest) or raw != digest + "\n":
            raise RegistryError(4, "invalid_current")
        return digest, self._load_catalog(root, digest)

    @staticmethod
    def _high_water(root):
        try:
            with _disk.regular(root, "HIGH_WATER") as file:
                match = _HIGH_WATER.fullmatch(file.read(94))
        except FileNotFoundError:
            return None, 0
        if match is None:
            raise RegistryError(4, "invalid_high_water")
        return match[1].decode("ascii"), int(match[2])

    def _checked_current(self, root):
        # Read the mark before CURRENT: publication advances them in reverse order.
        mark, depth = self._high_water(root)
        try:
            current, catalog = self._current(root)
        except FileNotFoundError as exc:
            if mark is not None:
                raise RegistryError(4, "snapshot_rollback") from exc
            raise
        if mark is None and catalog["schema"] == _SNAPSHOT:
            # A reader can start on unmarked v1 and see v2 after the writer's
            # first mark. Read a fresh ordered pair before declaring corruption.
            mark, depth = self._high_water(root)
            if mark is None:
                raise RegistryError(4, "missing_high_water")
            current, catalog = self._current(root)
        if current == mark:
            return current, catalog, depth, True

        # A legacy store has no mark. Verify its entire ancestry before pinning it.
        cursor, ancestor = current, catalog
        seen = set()
        while cursor != mark:
            if cursor in seen:
                raise RegistryError(4, "snapshot_cycle")
            seen.add(cursor)
            previous = ancestor["previous"]
            if previous is None:
                if mark is not None:
                    raise RegistryError(4, "snapshot_rollback")
                break
            depth += 1
            cursor = previous
            ancestor = self._load_catalog(root, cursor)
        return current, catalog, depth, False

    def _writer_current(self, root, staging):
        current, catalog, depth, marked = self._checked_current(root)
        if not marked:
            _disk.select_high_water(root, staging, current, depth)
        return current, catalog, depth

    def current_snapshot(self):
        try:
            with _disk.root_directory(self.root) as root:
                return self._checked_current(root)[0]
        except OSError as exc:
            raise RegistryError(3, "missing_current") from exc

    def _snapshot(self, root, expected):
        digest, catalog, _, _ = self._checked_current(root)
        seen = set()
        while expected is not None and digest != expected:
            if digest in seen:
                raise RegistryError(4, "snapshot_cycle")
            seen.add(digest)
            digest = catalog["previous"]
            if digest is None:
                raise RegistryError(4, "snapshot_unavailable")
            catalog = self._load_catalog(root, digest)
        return digest, catalog

    @staticmethod
    def _publish(root, staging, digest, catalog, depth):
        def conflict(_existing, _incoming):
            raise RegistryError(5, "snapshot_collision")

        with _disk.directory(root, "snapshots") as snapshots:
            _disk.ensure_directory(snapshots, digest[7:])
            with _disk.directory(snapshots, digest[7:]) as node:
                _disk.install(staging, node, "catalog.json", _encoded(catalog), conflict)
        if catalog["previous"] is None:
            # A reader may observe genesis only after its rollback mark is durable.
            _disk.select_high_water(root, staging, digest, depth)
            _disk.select_current(root, staging, digest)
            return
        _disk.select_current(root, staging, digest)
        try:
            _disk.select_high_water(root, staging, digest, depth)
        except (OSError, RegistryError) as exc:
            raise RegistryError(3, "commit_unconfirmed") from exc

    @staticmethod
    @contextmanager
    def _object_dirs(root):
        with ExitStack() as stack:
            blobs = stack.enter_context(_disk.directory(root, "blobs"))
            payloads = stack.enter_context(_disk.directory(blobs, "sha256"))
            quarantine = stack.enter_context(_disk.directory(root, "quarantine"))
            yield payloads, quarantine

    @staticmethod
    def _require_unquarantined(quarantine, pin):
        try:
            with _disk.directory(quarantine, pin[7:]):
                raise RegistryError(5, "quarantined_digest:" + pin)
        except FileNotFoundError:
            pass

    def _read_object(self, payloads, quarantine, pin, *, load, record=False, max_bytes=None):
        if not is_digest(pin):
            raise RegistryError(2, "invalid_digest")
        self._require_unquarantined(quarantine, pin)
        try:
            result = _disk.read_checked(payloads, pin[7:], pin[7:], load=load,
                                        record=record, max_bytes=max_bytes)
        except FileNotFoundError as exc:
            raise RegistryError(3, "missing_blob:" + pin) from exc
        self._require_unquarantined(quarantine, pin)
        return result

    def put(self, slug, raw, *, blobs, expected_snapshot, tag=None, expected_binding=_UNSET):
        """Verify bytes and atomically add a pinned name with an optional tag CAS."""
        if not _identifier(slug) or type(raw) is not bytes or not isinstance(blobs, Mapping):
            raise RegistryError(2, "invalid_record_input")
        _disk.check_size(len(raw), record=True, max_bytes=None)
        if not is_digest(expected_snapshot):
            raise RegistryError(2, "expected_snapshot_required")
        if tag is None:
            if expected_binding is not _UNSET:
                raise RegistryError(2, "tag_required")
        elif not _tag(tag) or (expected_binding is not None and not is_digest(expected_binding)):
            raise RegistryError(2, "invalid_tag_or_expected_binding")

        with self._writer() as root, _disk.directory(root, "staging") as staging:
            _disk.recover_staging(staging)
            current, prior, depth = self._writer_current(root, staging)
            if current != expected_snapshot:
                raise RegistryError(4, "stale_snapshot")
            entry = prior["names"].setdefault(slug, {"records": [], "tags": {}})
            digest = _digest(raw)
            if tag is not None:
                bound = entry["tags"].get(tag)
                if bound != expected_binding:
                    raise RegistryError(4, "stale_binding")
                if _RELEASE.fullmatch(tag) and bound not in (None, digest):
                    raise RegistryError(4, "immutable_release_tag")

            with self._object_dirs(root) as (payloads, quarantine):
                self._store_closure(payloads, quarantine, staging, digest, raw, blobs)

            if digest in entry["records"] and (tag is None or entry["tags"].get(tag) == digest):
                return current
            entry["records"] = sorted(set(entry["records"]) | {digest})
            if tag is not None:
                entry["tags"][tag] = digest
            # _writer_current durably marks the verified predecessor first, including
            # legacy stores without HIGH_WATER. Readers can recover a selected v2
            # descendant from that mark if publication stops before advancing it.
            prior["schema"] = _SNAPSHOT
            prior["previous"] = current
            new = math_acceptance.canonical_digest(prior)
            self._publish(root, staging, new, prior, depth + 1)
            return new

    def _store_closure(self, payloads, quarantine, staging, digest, raw, blobs):
        def conflict_for(pin, *, preserve=False):
            def conflict(existing, incoming):
                actual = "sha256:" + hashlib.file_digest(existing, "sha256").hexdigest()
                existing.seek(0)
                if actual != pin:
                    if preserve:
                        _disk.preserve_corrupt(staging, quarantine, pin[7:], existing)
                    return
                _disk.quarantine(staging, quarantine, pin[7:], existing, incoming)
                raise RegistryError(5, "digest_collision:" + pin)
            return conflict

        incoming = dict(blobs)
        for pin, data in incoming.items():
            if not is_digest(pin) or type(data) is not bytes:
                raise RegistryError(2, "invalid_blob_input")
            if _digest(data) != pin:
                raise RegistryError(4, "submitted_blob_digest_mismatch")
        if digest in incoming and incoming[digest] != raw:
            self._require_unquarantined(quarantine, digest)
            _disk.quarantine(staging, quarantine, digest[7:], raw, incoming[digest])
            raise RegistryError(5, "digest_collision:" + digest)
        incoming[digest] = raw
        compared = set()
        legacy_markers = set()

        def read(pin, *, load, record=False, max_bytes=None):
            if pin in incoming:
                data = incoming[pin]
                _disk.check_size(len(data), record=record, max_bytes=max_bytes)
                if pin not in compared:
                    if _disk.legacy_corruption(quarantine, pin[7:], data):
                        legacy_markers.add(pin)
                    _disk.compare(payloads, pin[7:], data, conflict_for(pin), record=record)
                    compared.add(pin)
                return len(data), data if load else None
            return self._read_object(payloads, quarantine, pin, load=load,
                                     record=record, max_bytes=max_bytes)

        _, related, reachable = verify_closure(digest, read, self._evidence_policy, self._trusted_read)
        if incoming.keys() - reachable.keys():
            raise RegistryError(2, "unreferenced_blob")
        for pin, data in sorted(incoming.items()):
            _disk.install(staging, payloads, pin[7:], data, conflict_for(pin, preserve=True),
                          record=pin == digest or pin in related)
            if pin in legacy_markers:
                _disk.archive_legacy_corruption(quarantine, pin[7:])

    @contextmanager
    def _lookup(self, locator, snapshot, max_objects, max_bytes):
        match = _LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
        if match is None or match[4] == "latest":
            raise RegistryError(2, "invalid_locator")
        namespace, slug, digest, tag = match.groups()
        if namespace != self.namespace:
            raise RegistryError(3, "name_unavailable")
        if snapshot is not None and not is_digest(snapshot):
            raise RegistryError(2, "invalid_snapshot")
        if max_objects is not None and (type(max_objects) is not int or max_objects < 1):
            raise RegistryError(2, "invalid_object_budget")
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
            raise RegistryError(2, "invalid_byte_budget")
        try:
            with _disk.root_directory(self.root) as root:
                selected, catalog = self._snapshot(root, snapshot)
                entry = catalog["names"].get(slug)
                if entry and tag is not None:
                    digest = entry["tags"].get(tag)
                if not entry or digest not in entry["records"]:
                    raise RegistryError(3, "name_unavailable")
                with self._object_dirs(root) as (payloads, quarantine):
                    def read(pin, *, load, record=False, max_bytes=None):
                        return self._read_object(payloads, quarantine, pin, load=load,
                                                 record=record, max_bytes=max_bytes)

                    record, related, objects = verify_closure(
                        digest, read, self._evidence_policy, self._trusted_read, max_objects, max_bytes,
                    )
                    yield dict(snapshot=selected, locator=f"af://{namespace}/{slug}@{digest}",
                               record_digest=digest, record=record, lineage_records=related,
                               objects=objects), read
        except OSError as exc:
            raise RegistryError(3, "store_io:" + str(exc.errno)) from exc

    def resolve(self, locator, *, snapshot=None, max_objects=None, max_bytes=None):
        """Return verified closure metadata; both budgets count each digest once."""
        with self._lookup(locator, snapshot, max_objects, max_bytes) as (resolution, _read):
            return resolution

    def read_blob(self, locator, digest, *, snapshot=None, max_objects=None, max_bytes=None):
        """Read exact bytes only from the closure authorized by the locator."""
        if not is_digest(digest):
            raise RegistryError(2, "invalid_digest")
        with self._lookup(locator, snapshot, max_objects, max_bytes) as (resolution, read):
            size = resolution["objects"].get(digest)
            if size is None:
                raise RegistryError(3, "object_unavailable")
            return read(digest, load=True, max_bytes=size)[1]
