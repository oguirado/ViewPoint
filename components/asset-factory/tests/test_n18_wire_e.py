"""Receipt-bound registry and local publish-plan integration."""

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from asset_factory import core
import test_d18_reg_store as reg_fixtures
from test_e2e_pipeline import SyntheticStages
from tools.blender.assembly import pilot
from tools.pipeline import engine
from tools.publish import publisher
from tools.registry import Registry


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        retained = os.environ.get("WIRE_E_EVIDENCE_DIR")
        if retained and self._testMethodName in {
            "test_incomplete_run_is_registered_and_round_trips_only_under_review",
            "test_registered_assembly_recipe_and_parts_close_over_pipeline_source",
        }:
            self.root = Path(retained) / self._testMethodName
            self.root.mkdir(parents=True)
        else:
            temp = tempfile.TemporaryDirectory(prefix="n18-wire-e-")
            self.addCleanup(temp.cleanup)
            self.root = Path(temp.name)
        self.registry = Registry(self.root / "registry", namespace="studio")
        self.registry.root.mkdir()
        self.snapshot = self.registry.initialize()
        self.pipeline_root = self.root / "pipeline"
        self.backend = SyntheticStages(self.root)
        self.backend.scope = "wire-e-fixture"
        self.backend.failure = {"appearance": 3}
        self.taxonomy, self.provenance = b"fixture taxonomy", b"fixture source"
        self.destination = self.root / "publish"
        self.config = {"schema": "af.publish-config/v1", "source_root": str(self.root),
                       "destination_root": ":local:" + str(self.destination)}
        self.session = publisher.NamespaceAuthority(
            {"fixture": self.config["destination_root"]}).session("fixture")
        locks = self.root / "locks"
        locks.mkdir(mode=0o700)
        self.enterContext(patch.object(publisher, "lock_user_root", return_value=locks))

    def run_fixture(self):
        run = engine.Pipeline(self.pipeline_root, self.backend).run()
        path = self.pipeline_root / "runs" / (core.sha256_json(run) + ".json")
        return run, path, core.sha256_file(path)

    def record_fixture(self, mesh, kind="asset"):
        raw, blobs = reg_fixtures.RegistryTests.fixture(self, mesh, kind=kind)
        return json.loads(raw), blobs

    def prepare(self, run, path, digest, **changes):
        from tools.pipeline import release

        export = next(r for r in run["stages"] if r["stage"] == "export")
        stage = engine.load(export["receipt"]["path"])
        mesh = Path(stage["subject"]).read_bytes()
        record, blobs = self.record_fixture(mesh)
        options = {"run_sha256": digest, "registry": self.registry,
                   "expected_snapshot": self.snapshot, "slug": "fixture", "version": "v1",
                   "asset_class": "props", "record": record, "blobs": blobs,
                   "publish_config": self.config,
                   "provenance": {"inputs": [], "model": {"id": "fixture", "revision": "wire-e-v1"},
                                  "licence": None, "commercial_use": None,
                                  "absence_reasons": [], "gates": []}}
        return release.build_release(path, **(options | changes))

    def assert_refuses(self, code, reason, run, path, digest, **changes):
        from tools.pipeline import release

        before = self.registry.current_snapshot()
        with self.assertRaises(release.Refusal) as caught:
            self.prepare(run, path, digest, **changes)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (code, reason))
        self.assertEqual(self.registry.current_snapshot(), before)
        self.assertFalse(self.destination.exists())

    def test_incomplete_run_is_registered_and_round_trips_only_under_review(self):
        run, path, digest = self.run_fixture()
        self.assertEqual((run["status"], run["verdict"], run["artifact"]),
                         ("INCOMPLETE", "CANNOT_SEE", None))
        result = self.prepare(run, path, digest)
        closure, plan = result["registry"], result["publish_plan"]
        fresh = Registry(self.root / "registry", namespace="studio")
        self.assertEqual(fresh.resolve(closure["locator"], snapshot=closure["snapshot"]), closure)
        self.assertIn("sha256:" + digest, closure["objects"])
        self.assertEqual(fresh.read_blob(closure["locator"], "sha256:" + digest,
                                        snapshot=closure["snapshot"]), path.read_bytes())
        self.assertEqual(self.prepare(run, path, digest, expected_snapshot=closure["snapshot"]), result)
        self.assertFalse(self.destination.exists())
        self.assertEqual(plan["assets"][0]["final_verdict"], "CANNOT_SEE")
        approval = {"schema": "af.publish-approval/v1", "approved": True,
                    "plan_sha256": core.sha256_json(plan), "destination_root": plan["destination_root"],
                    "allow_folder_creation": True, "allow_metadata_updates": True}
        sent = publisher.publish(plan, config=self.config, approval=approval, session=self.session)
        self.assertEqual((sent["rc"], sent["transport_verdict"], sent["asset_verdict"]),
                         (0, "PASS", "CANNOT_SEE"), sent)
        self.assertNotIn("verdict", sent)
        self.assertFalse((self.destination / "props/fixture/v1").exists())
        mesh = self.destination / "_review/props/fixture/v1/asset.glb"
        self.assertTrue(mesh.is_file())
        self.assertEqual("sha256:" + core.sha256_file(mesh), closure["record"]["geometry_digest"])
        import_root = self.root / "restore"
        imported_plan = publisher.plan_import(config=self.config, store=import_root, session=self.session)
        restored = publisher.restore(imported_plan, config=self.config, session=self.session)
        self.assertEqual((restored["rc"], restored["transport_verdict"], restored["asset_verdict"]),
                         (0, "PASS", "CANNOT_SEE"), restored)
        self.assertNotIn("verdict", restored)
        self.assertFalse((import_root / "props/fixture/v1").exists())
        self.assertEqual((import_root / "_review/props/fixture/v1/asset.glb").read_bytes(), mesh.read_bytes())
        for name, value in (("release", result), ("publish", sent), ("import", restored)):
            receipt_path = self.root / (name + "-evidence.json")
            core.atomic_write_json(receipt_path, value)
            if name != "release":
                self.assertEqual(publisher.read_receipt(receipt_path), value)

    def test_registered_assembly_recipe_and_parts_close_over_pipeline_source(self):
        blender = shutil.which("blender")
        self.assertIsNotNone(blender, "Blender executable AUSENTE")
        template = self.root / "template.md"
        template.write_bytes(b"Wire E portable assembly template fixture.\n")
        self.enterContext(patch.object(pilot, "TEMPLATE_DIGEST", pilot.pin(template.read_bytes())))
        recipe, approved, _ = pilot.author_parts(
            self.registry, root=self.root / "parts", blender=Path(blender), view_template=template,
            expected_snapshot=self.snapshot)
        assembly = pilot.assemble(
            self.registry, recipe, expected_recipe_digest=pilot.pin(recipe), approved_parts=approved,
            expected_snapshot=self.registry.current_snapshot(), root=self.root / "assembly",
            blender=Path(blender), view_template=template)
        self.snapshot = assembly["snapshot"]
        run, path, digest = self.run_fixture()
        self.assert_refuses(4, "assembly_run_subject_mismatch", run, path, digest,
                            assembly_locator=assembly["locator"])

        mesh_data = (self.root / "assembly/scene.glb").read_bytes()
        self.backend.source.write_bytes(mesh_data)
        execute = self.backend.execute

        def preserve_fixture_export(name, directory, subject):
            result = execute(name, directory, subject)
            if name == "export":
                (directory / result["subject"]).write_bytes(mesh_data)
            return result

        with patch.object(self.backend, "execute", side_effect=preserve_fixture_export):
            run, path, digest = self.run_fixture()
        result = self.prepare(run, path, digest, assembly_locator=assembly["locator"])
        closure = result["registry"]
        self.assertEqual(closure["record"]["lineage"]["parent_records"], [assembly["record_digest"]])
        self.assertEqual(set(closure["lineage_records"]), {assembly["record_digest"], *approved.values()})
        self.assertIn(pilot.pin(recipe), closure["objects"])
        self.assertEqual(self.registry.read_blob(closure["locator"], pilot.pin(recipe),
                                                 snapshot=closure["snapshot"]), recipe)
        self.assertTrue(result["publish_plan"]["assets"][0]["destination"].endswith("_review/props/fixture/v1/"))
        core.atomic_write_json(self.root / "assembly-release-evidence.json", result)

    def test_one_changed_run_byte_is_stale_even_if_json_still_parses(self):
        run, path, digest = self.run_fixture()
        path.write_bytes(path.read_bytes().replace(b"wire-e-fixture", b"wire-e-Fixture"))
        self.assert_refuses(4, "run_digest_mismatch", run, path, digest)

    def test_missing_stage_receipt_refuses(self):
        run, path, digest = self.run_fixture()
        Path(run["stages"][0]["receipt"]["path"]).unlink()
        self.assert_refuses(3, "stage_receipt_missing", run, path, digest)

    def test_changed_stage_receipt_refuses(self):
        run, path, digest = self.run_fixture()
        stage = Path(run["stages"][0]["receipt"]["path"])
        stage.write_bytes(stage.read_bytes() + b" ")
        self.assert_refuses(4, "stage_receipt_digest_mismatch", run, path, digest)

    def test_omitted_stage_row_refuses_even_with_a_new_run_pin(self):
        run, path, _ = self.run_fixture()
        run["stages"].pop()
        run["run_key"] = core.sha256_json([row["receipt"] for row in run["stages"]])
        core.atomic_write_json(path, run)
        self.assert_refuses(3, "stage_chain_incomplete", run, path, core.sha256_file(path))

    def test_resealed_stage_with_wrong_predecessor_refuses(self):
        run, path, _ = self.run_fixture()
        row = run["stages"][0]
        stage_path = Path(row["receipt"]["path"])
        stage = engine.load(stage_path)
        stage["previous"] = row["receipt"]
        core.atomic_write_json(stage_path, stage)
        row["receipt"] = engine.file_ref(stage_path)
        run["run_key"] = core.sha256_json([row["receipt"] for row in run["stages"]])
        core.atomic_write_json(path, run)
        self.assert_refuses(4, "stage_link_mismatch", run, path, core.sha256_file(path))

    def test_resealed_run_cannot_raise_the_stage_verdict(self):
        run, path, _ = self.run_fixture()
        run.update(rc=0, status="OK", verdict="PASS")
        core.atomic_write_json(path, run)
        self.assert_refuses(4, "aggregate_verdict_mismatch", run, path, core.sha256_file(path))

    def test_export_bytes_changed_after_run_refuse(self):
        run, path, digest = self.run_fixture()
        row = next(r for r in run["stages"] if r["stage"] == "export")
        mesh = Path(engine.load(row["receipt"]["path"])["subject"])
        mesh.write_bytes(mesh.read_bytes() + b"x")
        self.assert_refuses(4, "stage_witness_digest_mismatch", run, path, digest)

    def test_successful_fixture_cannot_authorize_release(self):
        self.backend.failure.clear()
        run, path, digest = self.run_fixture()
        self.assertEqual(run["verdict"], "PASS")
        self.assert_refuses(5, "production_scope_required", run, path, digest)

    def test_export_substitution_refuses(self):
        run, path, digest = self.run_fixture()
        other, blobs = self.record_fixture(b"different export")
        self.assert_refuses(4, "record_export_mismatch", run, path, digest, record=other, blobs=blobs)


class ReceiptCompatibilityTests(unittest.TestCase):
    def test_old_publish_and_import_receipts_read_without_promoting_transport(self):
        common = {"rc": 0, "verdict": "PASS", "reason_codes": [], "plan_sha256": "fixture-plan",
                  "files_verified": 1, "commands": []}
        publish = common | {"schema": "af.publish-receipt/v1", "destination_root": ":local:/fixture/",
                            "log_sha256": None, "catalog_sha256": None, "wall_ms": 0,
                            "asset_verdicts": [{"destination": "_review/props/fixture/v1/",
                                                "verdict": "CANNOT_SEE"}]}
        imported = common | {"schema": "af.import-receipt/v1", "assets_restored": []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            for value, asset_verdict in ((publish, "CANNOT_SEE"), (imported, None)):
                with self.subTest(schema=value["schema"]):
                    core.atomic_write_json(path, value)
                    raw = path.read_bytes()
                    read = publisher.read_receipt(path)
                    self.assertEqual((read["transport_verdict"], read["asset_verdict"]),
                                     ("PASS", asset_verdict))
                    self.assertNotIn("verdict", read)
                    self.assertEqual(path.read_bytes(), raw)
