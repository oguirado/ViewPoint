"""Exercise the local publisher using synthetic records and isolated stores."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from asset_factory import core, proc
from tools.publish import publisher as p
from tools.publish.static_gate import scan_surface


class PublishTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="d18-pub-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        runtime = self.base / "runtime"
        runtime.mkdir(mode=0o700)
        self.enterContext(patch.object(p, "lock_user_root", return_value=runtime))
        self.source = self.base / "source"
        self.destination = self.base / "destination"
        self.store = self.base / "restore"
        self.source.mkdir()
        self.config = {"schema": "af.publish-config/v1",
                       "destination_root": ":local:" + str(self.destination),
                       "source_root": str(self.source), "busy_attempts": 5}
        self.session = p.NamespaceAuthority({"fixture": self.config["destination_root"]}).session("fixture")
        self.mesh = self.source / "mesh.glb"
        self.mesh.write_bytes(b"isolated mesh fixture")
        self.gate = self.source / "gate.json"
        self.gate.write_bytes(p.encoded({"schema": "af.bridge-verdict/v1", "rc": 0,
                                        "verdict": "PASS", "source_sha256": core.sha256_file(self.mesh),
                                        "score": 7}))

    def plan(self, version, *, extra=None, extra_path="physics/source.json", declaration=None):
        files = [{"source": str(self.mesh), "path": "asset.glb"},
                 {"source": str(self.gate), "path": "receipts/gate.json"}]
        if extra is not None:
            files.append({"source": str(extra), "path": extra_path})
        source = {"schema": "af.publish-source/v1", "assets": [{
            "asset_class": "props", "asset_id": "crate", "version": version,
            "final_verdict": "PASS", "files": files,
            "provenance": {
                "inputs": [{"role": "reference", "sha256": "a" * 64}],
                "model": {"id": "fixture", "revision": "fixture-v1"},
                "licence": {"code": "MIT", "weights": None, "kind": "tool", "deps": [],
                            "commercial": True, "gated": False, "territory_excluded": []},
                "commercial_use": False, "absence_reasons": [],
                "gates": [{"name": "shape", "verdict": "PASS", "numbers": {"score": 7},
                           "receipt": "receipts/gate.json", "verdict_key": "verdict"}],
            },
        }]}
        if declaration is not None:
            provenance = source["assets"][0]["provenance"]
            for field in ("licence", "commercial_use", "jurisdiction"):
                provenance.pop(field, None)
            provenance.update(declaration)
        return p.build_plan(source, self.config)

    @staticmethod
    def approval(plan):
        return {"schema": "af.publish-approval/v1", "approved": True,
                "plan_sha256": core.sha256_json(plan), "destination_root": plan["destination_root"],
                "allow_folder_creation": True, "allow_metadata_updates": True}

    def publish(self, plan, **kwargs):
        return p.publish(plan, config=self.config,
                         approval=kwargs.get("approval", self.approval(plan)),
                         session=kwargs.get("session", self.session))

    def import_plan(self, versions=None):
        return p.plan_import(config=self.config, store=self.store, asset_paths=versions, session=self.session)

    def restore(self, plan):
        return p.restore(plan, config=self.config, session=self.session)

    def round_trip(self, version):
        plan = self.plan(version)
        result = self.publish(plan)
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(result["commands"], [])
        self.assertEqual(result["files_verified"], plan["file_count"])
        return plan, result

    def event_bytes(self):
        return {path.name: path.read_bytes()
                for path in (self.destination / "_estado/events").glob("*.json")}

    def test_publication_and_restore_pass_through_optional_model_declarations(self):
        for version, declaration in (
            ("v1", {}),
            ("v2", {"licence": {"weights": "LicenseRef-NonCommercial", "commercial": False},
                    "commercial_use": True, "jurisdiction": "user-declared"}),
        ):
            with self.subTest(version=version):
                plan = self.plan(version, declaration=declaration)
                result = self.publish(plan)
                self.assertEqual(result["rc"], 0, result)
                self.assertEqual(self.restore(self.import_plan())["rc"], 0)
                provenance = p.decode((self.store / "props" / "crate" / version /
                                       "provenance.json").read_bytes())
                self.assertEqual({field: provenance[field] for field in
                                  ("licence", "commercial_use", "jurisdiction") if field in provenance},
                                 declaration)

    def test_round_trip_and_existing_restore_rehash(self):
        plan, result = self.round_trip("v1")
        events = self.event_bytes()
        self.assertEqual(len(events), 1)
        for name, raw in events.items():
            self.assertEqual(name, hashlib.sha256(raw).hexdigest() + ".json")
            event = p.decode(raw)
            asset = event["assets"][0]
            for item in asset["files"]:
                data = (self.destination / "props/crate/v1" / item["path"]).read_bytes()
                self.assertEqual(item["local_sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(item["remote_evidence"],
                                 {"size": len(data), "md5": hashlib.md5(data, usedforsecurity=False).hexdigest()})
        catalogue = self.destination / "_index/catalog.json"
        self.assertEqual(core.sha256_file(catalogue), result["catalog_sha256"])

        restore_plan = self.import_plan()
        self.assertFalse(self.store.exists())
        restored = self.restore(restore_plan)
        self.assertEqual(restored["rc"], 0, restored)
        self.assertEqual(restored["files_verified"], plan["file_count"])
        for item in plan["assets"][0]["files"]:
            self.assertEqual(core.sha256_file(self.store / "props/crate/v1" / item["path"]), item["sha256"])

        replay = self.restore(restore_plan)
        self.assertEqual(replay["rc"], 0, replay)
        self.assertEqual(replay["assets_restored"], [])
        self.assertEqual(replay["files_verified"], plan["file_count"])
        self.assertEqual(self.event_bytes(), events)
        existing = self.store / "props/crate/v1/asset.glb"
        existing.write_bytes(b"locally altered fixture")
        refusal = self.restore(restore_plan)
        self.assertEqual(refusal["reason_codes"], ["local_asset_conflict"])
        self.assertEqual(refusal["files_verified"], 0)
        self.assertEqual(existing.read_bytes(), b"locally altered fixture")

    def test_descendant_and_root_symlinks_refuse_and_preserve_sentinel(self):
        plan = self.plan("v1")
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside witness")
        self.destination.mkdir()
        (self.destination / "props").symlink_to(outside, target_is_directory=True)
        result = self.publish(plan)
        self.assertEqual(result["rc"], 5, result)
        self.assertEqual(result["reason_codes"], ["local_destination_unsafe"])
        self.assertEqual(result["files_verified"], 0)
        self.assertEqual(self.event_bytes(), {})
        self.assertEqual(list(outside.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"outside witness")
        (self.destination / "props").unlink()
        (self.destination / "props").mkdir()
        nested = self.destination / "props/crate"
        nested.symlink_to(outside, target_is_directory=True)
        result = self.publish(plan)
        self.assertEqual(result["reason_codes"], ["local_destination_unsafe"], result)
        self.assertEqual(list(outside.iterdir()), [sentinel])
        nested.unlink()
        (self.destination / "props").rmdir()
        self.destination.rmdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(p.PublishError, "invalid_local_destination"):
            self.publish(plan)
        self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_existing_store_unchanged_after_staging_identity_changes(self):
        self.round_trip("v1")
        initial = self.restore(self.import_plan())
        self.assertEqual(initial["rc"], 0, initial)
        prior = self.store / "props/crate/v1/asset.glb"
        prior_digest = core.sha256_file(prior)
        self.round_trip("v2")
        plan = self.import_plan()
        verify = p.verify_tree
        changed = False

        def observe(storage, fd, asset):
            nonlocal changed
            count = verify(storage, fd, asset)
            if asset["version"] == "v2" and not changed:
                scratch, = self.store.glob(".af-import-*")
                scratch.rename(self.store / "detached-fixture")
                scratch.mkdir()
                changed = True
            return count

        with patch.object(p, "verify_tree", side_effect=observe):
            result = self.restore(plan)
        self.assertTrue(changed)
        self.assertEqual(result["reason_codes"], ["import_stage_changed"], result)
        self.assertEqual(result["files_verified"], 0)
        self.assertEqual(result["assets_restored"], [])
        self.assertFalse((self.store / "props/crate/v2").exists())
        self.assertEqual(core.sha256_file(prior), prior_digest)

    def test_late_stage_refusal_rolls_back_moved_inode(self):
        self.round_trip("v1")
        initial = self.restore(self.import_plan())
        self.assertEqual(initial["rc"], 0, initial)
        prior = self.store / "props/crate/v1/asset.glb"
        prior_digest = core.sha256_file(prior)
        self.round_trip("v2")
        plan = self.import_plan()
        assert_stage = p.assert_stage
        final = self.store / "props/crate/v2"

        def check_stage(root_fd, name, fd):
            if final.exists():
                raise p.PublishError("import_stage_changed", 5)
            return assert_stage(root_fd, name, fd)

        with patch.object(p, "assert_stage", side_effect=check_stage):
            result = self.restore(plan)
        self.assertEqual(result["reason_codes"], ["import_stage_changed"], result)
        self.assertEqual(result["files_verified"], 0)
        self.assertEqual(result["assets_restored"], [])
        self.assertFalse(final.exists())
        self.assertEqual(core.sha256_file(prior), prior_digest)
        retry = self.restore(plan)
        self.assertEqual(retry["rc"], 0, retry)

    def test_staging_file_and_tree_changes_refuse(self):
        self.round_trip("v1")
        plan = self.import_plan()
        observe = p.verify_tree
        for change in ("file", "extra", "empty-directory", "link"):
            with self.subTest(change=change):
                changed = False
                outside = self.base / "untouched"
                outside.write_bytes(b"untouched")
                def verify_then_change(storage, fd, asset):
                    nonlocal changed
                    count = observe(storage, fd, asset)
                    if not changed:
                        scratch, = self.store.glob(".af-import-*")
                        tree = scratch / "0"
                        if change == "file":
                            (tree / "asset.glb").write_bytes(b"changed")
                        elif change == "extra":
                            (tree / "extra.txt").write_bytes(b"extra")
                        elif change == "empty-directory":
                            (tree / "extra").mkdir()
                        else:
                            (tree / "asset.glb").unlink()
                            (tree / "asset.glb").symlink_to(outside)
                        changed = True
                    return count
                with patch.object(p, "verify_tree", side_effect=verify_then_change):
                    result = self.restore(plan)
                self.assertTrue(changed)
                self.assertNotEqual(result["rc"], 0, result)
                self.assertEqual(result["files_verified"], 0)
                self.assertFalse((self.store / "props/crate/v1").exists())
                self.assertEqual(outside.read_bytes(), b"untouched")

    def test_credential_filenames_never_open_during_export_or_import(self):
        safe_extra = self.source / "fixture.json"
        safe_extra.write_bytes(b"{}")
        names = (".netrc", ".npmrc", ".NetRC", "_netrc", "netrc", ".netrc.bak",
                 ".netrc~", ".npmrc.json", ".git-credentials", ".pypirc", ".docker",
                 ".dockercfg", "fixture.p12", "fixture.pfx.backup~")
        for name in names:
            with self.subTest(name=name):
                # Empty placeholders are the only credential-named files here.
                synthetic = self.source / name
                synthetic.touch()
                try:
                    with self.assertRaisesRegex(p.PublishError, "credential_source_forbidden"):
                        self.plan("v1", extra=synthetic)
                    with self.assertRaisesRegex(p.PublishError, "credential_source_forbidden"):
                        p.relative("physics/" + name)
                    plan = self.plan("v1", extra=safe_extra)
                    for item in plan["assets"][0]["files"]:
                        if item["path"] == "physics/source.json":
                            item["source"] = str(synthetic)
                    result = self.publish(plan)
                    self.assertEqual(result["reason_codes"], ["credential_source_forbidden"], result)
                    self.assertFalse(self.destination.exists())
                finally:
                    synthetic.unlink()

        self.round_trip("v1")
        plan = self.import_plan()
        for name in names:
            with self.subTest(name=name):
                synthetic = self.destination / "props/crate/v1" / name
                synthetic.touch()
                real_open = p.LocalStore._file
                def reject_open(storage, parent, leaf):
                    self.assertNotEqual(leaf, name, "credential basename opened")
                    return real_open(storage, parent, leaf)
                with patch.object(p.LocalStore, "_file", reject_open):
                    result = self.restore(plan)
                self.assertEqual(result["reason_codes"], ["credential_path_forbidden"], result)
                self.assertEqual(result["files_verified"], 0)
                self.assertFalse(self.store.exists())
                synthetic.unlink()

    def test_source_suffix_bound_to_artifact_role(self):
        cases = (("asset-name.glb", "meshes/lod.glb"), ("image.png", "textures/base.png"),
                 ("linear.exr", "textures/detail.exr"), ("record.json", "records/part.json"),
                 ("scene.blend", "asset.blend"), ("data.json", "physics/source.json"),
                 ("render.mp4", "renders/turntable.mp4"), ("model.step", "cad/part.step"))
        wrong = self.source / "unclassified.txt"
        wrong.touch()
        for index, (basename, destination) in enumerate(cases):
            with self.subTest(destination=destination):
                source = self.source / basename
                source.write_bytes(b"fixture")
                version = "v" + str(index)
                plan = self.plan(version, extra=source, extra_path=destination)
                with self.assertRaisesRegex(p.PublishError, "source_artifact_type_mismatch"):
                    self.plan(version, extra=wrong, extra_path=destination)
                for item in plan["assets"][0]["files"]:
                    if item["path"] == destination:
                        item["source"] = str(wrong)
                with patch.object(p, "source_snapshot", side_effect=AssertionError("must refuse before opening sources")):
                    refused = self.publish(plan)
                self.assertEqual(refused["reason_codes"], ["source_artifact_type_mismatch"])
                if destination in ("textures/detail.exr", "asset.blend"):
                    accepted = self.publish(self.plan(version, extra=source, extra_path=destination))
                    self.assertEqual(accepted["rc"], 0, accepted)
                    restored = self.restore(self.import_plan())
                    self.assertEqual(restored["rc"], 0, restored)
                    self.assertEqual((self.store / "props/crate" / version / destination).read_bytes(),
                                     source.read_bytes())

    def test_swapped_mutable_view_parent_refuses(self):
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"untouched")
        real_replace = os.replace
        for version, folder, leaf in (("v1", "_index", "catalog.json"), ("v2", "_estado", "log.jsonl")):
            with self.subTest(folder=folder):
                original = self.destination / folder
                detached = self.base / ("detached-" + folder)
                def replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
                    if dst == leaf and dst_dir_fd is not None:
                        original.rename(detached)
                        original.symlink_to(outside, target_is_directory=True)
                    return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
                plan = self.plan(version)
                with patch.object(os, "replace", replace):
                    result = self.publish(plan)
                self.assertTrue(original.is_symlink())
                original.unlink()
                detached.rename(original)
                self.assertEqual(result["reason_codes"], ["local_destination_unsafe"], result)
                self.assertEqual(sentinel.read_bytes(), b"untouched")
                self.assertEqual(list(outside.iterdir()), [sentinel])
                retry = self.publish(plan)
                self.assertEqual(retry["rc"], 0, retry)

    def test_bind_mounted_descendants_refuse_before_transfer(self):
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"untouched")
        target = self.destination / "props/crate"
        target.mkdir(parents=True)
        self.assertEqual(outside.stat().st_dev, target.stat().st_dev)
        subprocess.run(["mount", "--bind", str(outside), str(target)], check=True)
        try:
            result = self.publish(self.plan("v1"))
        finally:
            subprocess.run(["umount", str(target)], check=True)
        self.assertEqual(result["reason_codes"], ["local_destination_unsafe"], result)
        self.assertEqual(sentinel.read_bytes(), b"untouched")
        self.assertEqual(list(outside.iterdir()), [sentinel])

        target_file = self.source / "foreign.json"
        target_file.touch()
        subprocess.run(["mount", "--bind", str(sentinel), str(target_file)], check=True)
        try:
            with self.assertRaisesRegex(p.PublishError, "source_unsafe"):
                self.plan("v2", extra=target_file)
        finally:
            subprocess.run(["umount", str(target_file)], check=True)
        self.round_trip("v2")

    def test_concurrent_publication_keeps_prior_events_and_merges_catalogue(self):
        self.round_trip("v0")
        prior = self.event_bytes()
        barrier = Barrier(2)
        def run(plan):
            barrier.wait(timeout=5)
            return self.publish(plan)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(run, (self.plan("v1"), self.plan("v2"))))
        self.assertTrue(all(result["rc"] == 0 for result in results), results)
        observed = self.event_bytes()
        self.assertEqual(len(observed), 3)
        self.assertEqual({name: observed[name] for name in prior}, prior)
        (self.destination / "_index/catalog.json").write_bytes(b"stale disposable cache")
        self.assertEqual([asset["version"] for asset in self.import_plan()["assets"]], ["v0", "v1", "v2"])

    def test_interrupted_retry_and_approval_drift(self):
        self.round_trip("v0")
        plan = self.plan("v1")
        prior = self.event_bytes()
        copy = p.LocalPublication.copy
        def interrupt(client, source, destination=None, *, mutable=False):
            if mutable:
                raise p.PublishError("fixture_interrupted", 6)
            return copy(client, source, destination, mutable=mutable)
        with patch.object(p.LocalPublication, "copy", interrupt):
            partial = self.publish(plan)
        self.assertEqual(partial["reason_codes"], ["fixture_interrupted"], partial)
        observed = self.event_bytes()
        self.assertEqual(len(observed), len(prior) + 1)
        self.assertEqual({name: observed[name] for name in prior}, prior)
        mismatched = self.publish(self.plan("v2"), approval=self.approval(plan))
        self.assertEqual(mismatched["reason_codes"], ["approval_plan_mismatch"])
        retried = self.publish(plan)
        self.assertEqual(retried["rc"], 0, retried)
        self.assertEqual(self.event_bytes(), observed)
        self.assertEqual([asset["version"] for asset in self.import_plan()["assets"]], ["v0", "v1"])

    def test_namespace_and_import_catalogue_drift(self):
        plan = self.plan("v1")
        other = p.NamespaceAuthority({"other": ":local:" + str(self.base / "other")}).session("other")
        result = self.publish(plan, session=other)
        self.assertEqual(result["reason_codes"], ["namespace_not_authorised"], result)
        self.assertFalse(self.destination.exists())
        self.round_trip("v1")
        import_plan = self.import_plan()
        self.round_trip("v2")
        result = self.restore(import_plan)
        self.assertEqual(result["reason_codes"], ["import_catalogue_stale"], result)
        self.assertFalse(self.store.exists())

    def test_static_surface_and_live_argv_only_helper(self):
        self.assertEqual(scan_surface(Path(p.__file__).parent), [])
        child = proc.popen(["/usr/bin/true"], start_new_session=True)
        self.assertEqual(child.wait(timeout=5), 0)

    def test_live_helper_refuses_shell_and_string_commands(self):
        variants = {
            "shell_kw": lambda: proc.popen(["true"], start_new_session=True, shell=True),
            "string_cmd": lambda: proc.popen("true", start_new_session=True),
            "kwargs_shell": lambda: proc.popen(["true"], start_new_session=True, **{"shell": True}),
        }
        for variant, call in variants.items():
            with self.subTest(variant=variant):
                with self.assertRaisesRegex(proc.SignalRefusal, "group_spawn_unproven"):
                    job = call()
                    job.wait(timeout=5)
