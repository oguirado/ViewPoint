import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from asset_factory.core import canonical_json
from asset_factory.math_acceptance import canonical_digest
import test_d18_base_evidence as base_fixtures
from tools.registry import Registry, RegistryError
from tools.registry import store


def encode(value):
    return canonical_json(value).encode("utf-8")


def pin(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class RegistryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "registry"
        self.root.mkdir()
        self.registry = Registry(self.root, namespace="studio")
        self.initial = self.registry.initialize()
        self.mesh, self.taxonomy, self.provenance = b"mesh bytes", b"taxonomy", b"source identity"
        self.asset, self.blobs = self.fixture(self.mesh)

    def fixture(self, mesh, kind="asset", parent=None):
        record = base_fixtures.EvidenceTests.make_record(None, kind, None)
        record["gates"] = []
        record["classification"]["taxonomy_digest"] = pin(self.taxonomy)
        record["geometry_digest"] = pin(mesh)
        record["rights"]["components"][0].update(
            provenance_digest=pin(self.provenance), subject_digest=pin(mesh),
        )
        record["files"][0].update(digest=pin(mesh), size_bytes=len(mesh))
        blobs = {pin(data): data for data in (mesh, self.taxonomy, self.provenance)}
        deps = set(blobs)
        if parent is not None:
            record["lineage"]["parent_records"] = [pin(parent)]
            record["lineage"]["parent_asset_digest"] = pin(parent)
            deps.add(pin(parent))
        record["dependencies"] = sorted(deps)
        return encode(record), blobs

    def put(self, record=None, blobs=None, **kwargs):
        return self.registry.put(
            "hero", self.asset if record is None else record,
            blobs=self.blobs if blobs is None else blobs,
            expected_snapshot=kwargs.pop("expected_snapshot", self.initial), **kwargs,
        )

    def address(self, raw=None):
        return "af://studio/hero@" + pin(self.asset if raw is None else raw)

    def blob_path(self, digest):
        return self.root / "blobs" / "sha256" / digest.removeprefix("sha256:")

    def assert_refuses(self, rc, reason, call, *args, **kwargs):
        with self.assertRaises(RegistryError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.rc, rc)
        self.assertIn(reason, str(caught.exception))

    def test_exact_part_and_asset_resolution_includes_lineage_and_bytes(self):
        asset_snapshot = self.put(tag="candidate", expected_binding=None)
        part, blobs = self.fixture(b"part mesh", "part", parent=self.asset)
        snapshot = self.registry.put("handle", part, blobs=blobs, expected_snapshot=asset_snapshot)
        resolution = self.registry.resolve("af://studio/handle@" + pin(part))
        self.assertEqual(resolution["snapshot"], snapshot)
        self.assertEqual(resolution["record"], json.loads(part))
        self.assertEqual(resolution["lineage_records"], {pin(self.asset): json.loads(self.asset)})
        self.assertEqual(resolution["record"]["gates"], [])
        self.assertEqual(resolution["objects"][pin(self.mesh)], len(self.mesh))
        self.assertEqual(
            self.registry.read_blob(resolution["locator"], pin(self.mesh), snapshot=snapshot), self.mesh,
        )
        self.assertEqual(self.registry.resolve("af://studio/hero:candidate")["locator"], self.address())

    def test_stale_snapshot_and_binding_refuse_without_changing_current(self):
        snapshot = self.put(tag="candidate", expected_binding=None)
        self.assert_refuses(4, "stale_snapshot", self.put)
        self.assert_refuses(4, "stale_binding", self.put, expected_snapshot=snapshot,
                            tag="candidate", expected_binding=None)
        self.assertEqual(self.registry.current_snapshot(), snapshot)

    def test_alias_cas_pins_old_snapshot_and_release_is_write_once(self):
        first = self.put(tag="candidate", expected_binding=None)
        second, blobs = self.fixture(b"other mesh")
        current = self.put(second, blobs, expected_snapshot=first, tag="candidate",
                           expected_binding=pin(self.asset))
        self.assertEqual(self.registry.resolve("af://studio/hero:candidate")["locator"], self.address(second))
        self.assertEqual(self.registry.resolve("af://studio/hero:candidate", snapshot=first)["locator"],
                         self.address())
        release = self.put(second, {}, expected_snapshot=current, tag="v0001", expected_binding=None)
        self.assert_refuses(4, "immutable_release_tag", self.put, blobs={}, expected_snapshot=release,
                            tag="v0001", expected_binding=pin(second))
        self.assertEqual(self.registry.current_snapshot(), release)

    def test_swapped_alias_in_catalog_refuses_even_when_both_members_exist(self):
        first = self.put(tag="candidate", expected_binding=None)
        other, blobs = self.fixture(b"other mesh")
        snapshot = self.put(other, blobs, expected_snapshot=first)
        catalog = self.root / "snapshots" / snapshot.removeprefix("sha256:") / "catalog.json"
        document = json.loads(catalog.read_bytes())
        document["names"]["hero"]["tags"]["candidate"] = pin(other)
        catalog.chmod(0o600)
        catalog.write_bytes(encode(document))
        self.assert_refuses(4, "digest_mismatch", self.registry.resolve, "af://studio/hero:candidate")

    def test_changed_blob_and_wrong_declared_size_refuse(self):
        record = json.loads(self.asset)
        record["files"][0]["size_bytes"] += 1
        self.assert_refuses(4, "file_size_mismatch", self.put, encode(record))
        snapshot = self.put()
        blob = self.blob_path(pin(self.mesh))
        blob.chmod(0o600)
        blob.write_bytes(b"X" * len(self.mesh))
        self.assert_refuses(4, "digest_mismatch", self.registry.resolve, self.address())
        self.assertEqual(self.registry.current_snapshot(), snapshot)

    def test_incomplete_and_omitted_dependencies_keep_prior_state(self):
        self.assert_refuses(3, "missing_blob", self.put, blobs={pin(self.mesh): self.mesh})
        record = json.loads(self.asset)
        record["dependencies"].remove(pin(self.taxonomy))
        self.assert_refuses(3, "undeclared_dependency", self.put, encode(record))
        self.assertEqual(self.registry.current_snapshot(), self.initial)
        self.assert_refuses(3, "name_unavailable", self.registry.resolve, self.address())

    def test_missing_parent_closure_refuses(self):
        first = self.put()
        part, blobs = self.fixture(b"part mesh", "part", parent=self.asset)
        self.blob_path(pin(self.mesh)).unlink()
        self.assert_refuses(3, "missing_blob", self.put, part, blobs, expected_snapshot=first)
        self.assertEqual(self.registry.current_snapshot(), first)

    def test_duplicate_keys_bad_schema_noncanonical_and_unsorted_rejected(self):
        duplicate = b'{"display_name":"replayed",' + self.asset[1:]
        bad_schema = json.loads(self.asset)
        bad_schema["schema"] = "af.asset-record/v9"
        bad_order = json.loads(self.asset)
        bad_order["dependencies"].reverse()
        for raw, reason in ((duplicate, "malformed_json"), (encode(bad_schema), "unknown_record_schema"),
                            (self.asset.rstrip(), "noncanonical_record"),
                            (encode(bad_order), "noncanonical_set")):
            with self.subTest(reason=reason):
                self.assert_refuses(2, reason, self.put, raw)
        self.assertEqual(self.registry.current_snapshot(), self.initial)

    def test_taxonomy_mismatch_and_logical_path_escape_refuse(self):
        record = json.loads(self.asset)
        record["taxonomy_path"] = "vehicle/handle"
        self.assert_refuses(2, "schema_mismatch:$.taxonomy_path", self.put, encode(record))
        record = json.loads(self.asset)
        record["files"][0]["logical_path"] = "asset/../sentinel"
        self.assert_refuses(2, "invalid_logical_path", self.put, encode(record))
        self.assertEqual(self.registry.current_snapshot(), self.initial)

    def test_forged_pass_requires_authorized_base_recomputation(self):
        evidence = base_fixtures.EvidenceTests()
        evidence.setUp()
        decision = evidence.assess()
        self.assertEqual(decision["rc"], 0, decision)
        gate = decision["gate"]
        record = json.loads(self.asset)
        record["gates"] = [gate]
        record["geometry_digest"] = gate["subject_digest"]
        self.assert_refuses(4, "unapproved_record_scope", self.put, encode(record))

    def test_authorized_baseline_recomputes_pass_and_needs_evidence_closure(self):
        evidence = base_fixtures.EvidenceTests()
        evidence.setUp()
        decision = evidence.assess()
        self.assertEqual(decision["rc"], 0, decision)
        gate = decision["gate"]
        record = evidence.make_record("asset", gate)
        record["files"][0]["size_bytes"] = len(b"subject")
        blobs = {digest: data for (_, digest), data in evidence.store.items()}
        blobs.update({pin(data): data for data in (
            b"subject", b"policy", b"domain", b"eval", b"tool", b"method", b"evidence",
        )})
        record["dependencies"] = sorted(blobs)
        scopes = {("mesh", gate["subject_digest"]): {gate["profile_digest"]}}
        registry = Registry(self.root, namespace="studio", evidence_policy=lambda _: scopes,
                            read_checked=lambda role, digest: evidence.store.get((role, digest)))
        missing = json.loads(encode(record))
        missing["dependencies"].remove(pin(b"domain"))
        self.assert_refuses(3, "undeclared_dependency", registry.put, "hero", encode(missing),
                            blobs={key: value for key, value in blobs.items() if key != pin(b"domain")},
                            expected_snapshot=self.initial)
        self.assertEqual(registry.current_snapshot(), self.initial)
        raw = encode(record)
        snapshot = registry.put("hero", raw, blobs=blobs, expected_snapshot=self.initial)
        self.assertEqual(registry.resolve(self.address(raw), snapshot=snapshot)["record"]["gates"], [gate])
        self.assert_refuses(6, "object_budget", registry.resolve, self.address(raw), max_objects=1)
        self.assert_refuses(4, "unapproved_record_scope", self.registry.resolve, self.address(raw))
        stale_reader = Registry(self.root, namespace="studio", evidence_policy=lambda _: scopes,
                                read_checked=lambda _role, _digest: b"stale evidence")
        self.assert_refuses(4, "evidence_bytes_mismatch", stale_reader.resolve, self.address(raw))

    def test_post_selection_sync_error_is_unconfirmed_and_stale_retry_refuses(self):
        original_replace, original_sync = store.os.replace, store.os.fsync
        selected = False

        def select(*args, **kwargs):
            nonlocal selected
            original_replace(*args, **kwargs)
            selected = True

        def sync(fd):
            if selected:
                raise OSError("injected post-commit sync error")
            original_sync(fd)

        with patch.object(store.os, "replace", side_effect=select):
            with patch.object(store.os, "fsync", side_effect=sync):
                self.assert_refuses(3, "commit_unconfirmed", self.put)
        self.assert_refuses(4, "stale_snapshot", self.put)
        self.assertEqual(self.registry.resolve(self.address())["record"], json.loads(self.asset))

    def test_symlinked_blob_and_descendant_directory_refuse(self):
        self.put()
        outside = self.root.parent / "sentinel"
        outside.write_bytes(self.mesh)
        blob = self.blob_path(pin(self.mesh))
        blob.unlink()
        blob.symlink_to(outside)
        self.assert_refuses(5, "unsafe_store_entry", self.registry.resolve, self.address())
        directory = blob.parent
        renamed = directory.with_name("saved")
        directory.rename(renamed)
        directory.symlink_to(renamed, target_is_directory=True)
        self.assert_refuses(5, "unsafe_store_entry", self.registry.resolve, self.address())
        self.assertEqual(outside.read_bytes(), self.mesh)

    def test_hardlink_refuses_before_reading_external_file(self):
        self.put()
        outside = self.root.parent / "sentinel"
        outside.write_bytes(self.mesh)
        blob = self.blob_path(pin(self.mesh))
        blob.unlink()
        blob.hardlink_to(outside)
        self.assert_refuses(5, "unsafe_store_entry", self.registry.resolve, self.address())

    def test_interruption_before_current_keeps_previous_and_fresh_instance_can_retry(self):
        prior = self.put(tag="candidate", expected_binding=None)
        other, blobs = self.fixture(b"other mesh")
        with patch.object(store.os, "replace", side_effect=OSError("interrupted")):
            self.assert_refuses(3, "replace_current", self.put, other, blobs, expected_snapshot=prior,
                                tag="candidate", expected_binding=pin(self.asset))
        self.registry = Registry(self.root, namespace="studio")
        self.assertEqual(self.registry.current_snapshot(), prior)
        self.assertEqual(self.registry.resolve("af://studio/hero:candidate")["locator"], self.address())
        self.assert_refuses(3, "name_unavailable", self.registry.resolve, self.address(other))
        orphan = next(
            path for path in (self.root / "snapshots").iterdir()
            if "sha256:" + path.name not in (prior, self.initial)
        )
        self.assert_refuses(4, "snapshot_unavailable", self.registry.resolve, self.address(other),
                            snapshot="sha256:" + orphan.name)
        committed = self.put(other, blobs, expected_snapshot=prior, tag="candidate",
                             expected_binding=pin(self.asset))
        self.assertEqual(self.registry.resolve("af://studio/hero:candidate")["snapshot"], committed)

    def test_interruption_after_immutable_link_is_recovered_by_next_writer(self):
        prior = self.put()
        other, blobs = self.fixture(b"new bytes")
        original_link = store.os.link

        def link_then_interrupt(*args, **kwargs):
            original_link(*args, **kwargs)
            raise KeyboardInterrupt

        with patch.object(store.os, "link", side_effect=link_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.put(other, blobs, expected_snapshot=prior)
        self.assertEqual(self.registry.current_snapshot(), prior)
        self.assertEqual(self.registry.resolve(self.address())["locator"], self.address())
        self.put(other, blobs, expected_snapshot=prior)
        self.assertEqual(self.registry.resolve(self.address(other))["record"], json.loads(other))

    def test_competing_writer_refuses_and_then_can_reopen(self):
        with self.registry._writer():
            self.assert_refuses(6, "writer_busy", self.put)
        self.put()

    def test_injected_digest_collision_quarantines_both_and_stays_blocked(self):
        first = self.put()
        collision = b"different bytes"
        with patch.object(store, "_digest", side_effect=lambda raw: pin(self.mesh) if raw == collision else pin(raw)):
            self.assert_refuses(5, "digest_collision", self.put,
                                blobs={pin(self.mesh): collision}, expected_snapshot=first)
        self.assertEqual(self.registry.current_snapshot(), first)
        self.assertEqual(self.blob_path(pin(self.mesh)).read_bytes(), self.mesh)
        quarantine = self.root / "quarantine" / pin(self.mesh).removeprefix("sha256:")
        self.assertEqual({p.read_bytes() for p in quarantine.iterdir()}, {self.mesh, collision})
        reopened = Registry(self.root, namespace="studio")
        self.assert_refuses(5, "quarantined_digest", reopened.resolve, self.address())
        self.assert_refuses(5, "quarantined_digest", self.put, expected_snapshot=first)

    def test_collision_between_submitted_record_and_blob_preserves_both(self):
        collision = b"alternate record bytes"
        with patch.object(store, "_digest", side_effect=lambda raw: pin(self.asset) if raw == collision else pin(raw)):
            self.assert_refuses(5, "digest_collision", self.put,
                                blobs={**self.blobs, pin(self.asset): collision})
        self.assertEqual(self.registry.current_snapshot(), self.initial)
        quarantine = self.root / "quarantine" / pin(self.asset).removeprefix("sha256:")
        self.assertEqual({p.read_bytes() for p in quarantine.iterdir()}, {self.asset, collision})

    def test_exact_names_snapshot_and_response_budget(self):
        self.put()
        for name in ("Fixture", "af://studio/hero", "af://studio/hero:latest", self.address()[:-1],
                     "af://studio/../hero@" + pin(self.asset)):
            with self.subTest(name=name):
                self.assert_refuses(2, "invalid_locator", self.registry.resolve, name)
        self.assert_refuses(3, "name_unavailable", self.registry.resolve,
                            self.address().replace("studio", "another"))
        self.assert_refuses(3, "name_unavailable", self.registry.resolve,
                            self.address().replace("hero", "other"))
        self.assert_refuses(6, "object_budget", self.registry.resolve, self.address(), max_objects=1)
        self.assert_refuses(3, "object_unavailable", self.registry.read_blob, self.address(), pin(b"unknown"))

    def test_base_digest_matches_registry_identity(self):
        self.assertEqual(canonical_digest(json.loads(self.asset)), pin(self.asset))
        snapshot = self.put()
        self.assertEqual(self.registry.resolve(self.address(), snapshot=snapshot)["record_digest"], pin(self.asset))

    def sized_record(self, size):
        record = json.loads(self.asset)
        record["annotations"] = {"padding": ""}
        record["annotations"]["padding"] = "x" * (size - len(encode(record)))
        return encode(record)

    def test_record_ceiling_precedes_ingress_hash(self):
        limit = 1 << 20
        over_limit = self.sized_record(limit + 1)
        with patch.object(store, "_digest", side_effect=AssertionError("record was hashed")):
            self.assert_refuses(6, "record_byte_limit", self.put, over_limit)
        self.assertEqual(self.registry.current_snapshot(), self.initial)
        at_limit = self.sized_record(limit)
        self.put(at_limit)
        self.assertEqual(self.registry.resolve(self.address(at_limit))["record"], json.loads(at_limit))

    def test_oversized_stored_record_is_refused_before_reading_even_as_lineage(self):
        prior = self.put()
        part, blobs = self.fixture(b"part mesh", "part", parent=self.asset)
        self.put(part, blobs, expected_snapshot=prior)
        record_path = self.blob_path(pin(self.asset))
        record_path.chmod(0o600)
        with record_path.open("r+b") as file:
            file.truncate((1 << 20) + 1)

        original_regular = store._disk.regular

        @contextmanager
        def forbid_record_read(parent, name):
            with original_regular(parent, name) as file:
                if name == record_path.name:
                    with patch.object(file, "read", side_effect=AssertionError("oversized record was read")):
                        yield file
                else:
                    yield file

        with patch.object(store._disk, "regular", side_effect=forbid_record_read):
            for raw in (self.asset, part):
                with self.subTest(record=pin(raw)):
                    self.assert_refuses(6, "record_byte_limit", self.registry.resolve, self.address(raw))

    def test_byte_budget_counts_each_closure_object_once_and_precedes_reads(self):
        first = self.put()
        part, part_blobs = self.fixture(b"part mesh", "part", parent=self.asset)
        self.put(part, part_blobs, expected_snapshot=first)
        objects = {**self.blobs, **part_blobs, pin(self.asset): self.asset, pin(part): part}
        exact = sum(map(len, objects.values()))
        self.assertEqual(self.registry.resolve(self.address(part), max_bytes=exact)["objects"],
                         {key: len(value) for key, value in objects.items()})
        self.assertEqual(self.registry.read_blob(self.address(part), pin(self.mesh), max_bytes=exact), self.mesh)
        self.assert_refuses(6, "byte_budget", self.registry.resolve, self.address(part), max_bytes=exact - 1)
        self.assert_refuses(6, "byte_budget", self.registry.read_blob, self.address(part), pin(self.mesh),
                            max_bytes=exact - 1)
        for malformed in (True, 0, -1):
            self.assert_refuses(2, "invalid_byte_budget", self.registry.resolve, self.address(part),
                                max_bytes=malformed)

        original_regular = store._disk.regular
        part_name = self.blob_path(pin(part)).name

        @contextmanager
        def forbid_root_read(parent, name):
            with original_regular(parent, name) as file:
                if name == part_name:
                    with patch.object(file, "read", side_effect=AssertionError("budgeted record was read")):
                        yield file
                else:
                    yield file

        with patch.object(store._disk, "regular", side_effect=forbid_root_read):
            self.assert_refuses(6, "byte_budget", self.registry.resolve, self.address(part),
                                max_bytes=len(part) - 1)

    def test_writer_refuses_rolled_back_current_across_instances(self):
        old = self.put(tag="candidate", expected_binding=None)
        other, blobs = self.fixture(b"other mesh")
        latest = self.put(other, blobs, expected_snapshot=old, tag="candidate", expected_binding=pin(self.asset))
        self.assertEqual(self.registry.resolve("af://studio/hero:candidate", snapshot=old)["locator"],
                         self.address())
        current = self.root / "CURRENT"
        current.chmod(0o600)
        current.write_text(old + "\n", encoding="ascii")
        self.registry = Registry(self.root, namespace="studio")
        self.assert_refuses(4, "snapshot_rollback", self.put, expected_snapshot=old)
        self.assert_refuses(4, "snapshot_rollback", self.registry.initialize)
        self.assert_refuses(4, "snapshot_rollback", self.registry.current_snapshot)
        self.assert_refuses(4, "snapshot_rollback", self.registry.resolve, "af://studio/hero:candidate")
        high_water = self.root / "HIGH_WATER"
        self.assertEqual(high_water.read_text(encoding="ascii"), latest + " 2\n")
        self.assertEqual(current.read_text(encoding="ascii"), old + "\n")
        current.write_text(latest + "\n", encoding="ascii")
        self.assertEqual(self.put(other, {}, expected_snapshot=latest), latest)

    def test_writer_recovers_selected_descendant_when_high_water_update_fails(self):
        old = self.put()
        newer, blobs = self.fixture(b"newer mesh")
        with patch.object(store._disk, "select_high_water", side_effect=OSError("interrupted"), create=True):
            self.assert_refuses(3, "commit_unconfirmed", self.put, newer, blobs, expected_snapshot=old)
        selected = self.registry.current_snapshot()
        self.assertNotEqual(selected, old)
        high_water = self.root / "HIGH_WATER"
        self.assertEqual(high_water.read_text(encoding="ascii"), old + " 1\n")
        reopened = Registry(self.root, namespace="studio")
        self.assertEqual(reopened.initialize(), selected)
        self.assertEqual(high_water.read_text(encoding="ascii"), selected + " 2\n")
        self.assert_refuses(4, "stale_snapshot", self.put, expected_snapshot=old)
        self.assertEqual(reopened.resolve(self.address(newer))["record"], json.loads(newer))

    def test_corrupt_blob_recovers_and_preserves_damaged_bytes(self):
        current = self.put()
        blob = self.blob_path(pin(self.mesh))
        blob.chmod(0o600)
        blob.write_bytes(b"")
        self.assert_refuses(4, "digest_mismatch", self.registry.resolve, self.address())
        self.assertEqual(self.put(expected_snapshot=current), current)
        self.registry = Registry(self.root, namespace="studio")
        self.assertEqual(self.registry.read_blob(self.address(), pin(self.mesh)), self.mesh)
        witnesses = list((self.root / "quarantine" / "corrupt" / blob.name).iterdir())
        self.assertEqual([path.read_bytes() for path in witnesses], [b""])
        self.assertEqual(self.put(expected_snapshot=current), current)
        self.assertEqual(list(witnesses[0].parent.iterdir()), witnesses)

    def test_legacy_corruption_marker_requires_complete_witnesses_to_clear(self):
        current = self.put()
        pin_hex = pin(self.mesh)[7:]
        blob = self.blob_path(pin(self.mesh))
        blob.chmod(0o600)
        blob.write_bytes(b"legacy damage")
        marker = self.root / "quarantine" / pin_hex
        marker.mkdir()
        (marker / ("existing-" + "a" * 32)).write_bytes(b"legacy damage")
        self.assert_refuses(5, "quarantined_digest", self.put, expected_snapshot=current)
        (marker / ("incoming-" + "b" * 32)).write_bytes(self.mesh)
        self.assertEqual(self.put(expected_snapshot=current), current)
        self.assertEqual(self.registry.read_blob(self.address(), pin(self.mesh)), self.mesh)
        archive = self.root / "quarantine" / "corrupt" / pin_hex
        self.assertFalse(marker.exists())
        witnesses = next(archive.glob("legacy-*"))
        self.assertEqual({path.read_bytes() for path in witnesses.iterdir()},
                         {b"legacy damage", self.mesh})

    def test_identifier_length_checked_at_put_open_and_lookup(self):
        allowed = "x" * 63
        oversized = allowed + "x"
        self.assert_refuses(2, "invalid_record_input", self.registry.put, oversized, self.asset,
                            blobs=self.blobs, expected_snapshot=self.initial)
        self.assert_refuses(2, "invalid_namespace", Registry, self.root, namespace=oversized)
        self.assert_refuses(2, "invalid_locator", self.registry.resolve,
                            f"af://studio/{oversized}@{pin(self.asset)}")
        self.assert_refuses(2, "invalid_locator", self.registry.resolve,
                            f"af://{oversized}/hero@{pin(self.asset)}")
        self.assert_refuses(2, "invalid_tag_or_expected_binding", self.put,
                            tag=oversized, expected_binding=None)
        other_root = self.root.parent / "other"
        other_root.mkdir()
        registry = Registry(other_root, namespace=allowed)
        initial = registry.initialize()
        current = registry.put(allowed, self.asset, blobs=self.blobs, expected_snapshot=initial)
        self.assertEqual(registry.resolve(f"af://{allowed}/{allowed}@{pin(self.asset)}")["record_digest"],
                         pin(self.asset))
        accepted_identifier = store._identifier
        with patch.object(
            store, "_identifier", side_effect=lambda name: name == oversized or accepted_identifier(name),
        ):
            legacy = registry.put(oversized, self.asset, blobs={}, expected_snapshot=current)
        self.assertEqual(registry.current_snapshot(), legacy)
        self.assertEqual(registry.resolve(f"af://{allowed}/{allowed}@{pin(self.asset)}")["record_digest"],
                         pin(self.asset))
        self.assert_refuses(2, "invalid_locator", registry.resolve,
                            f"af://{allowed}/{oversized}@{pin(self.asset)}")
