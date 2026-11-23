import hashlib
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import locator
from locator import ArtifactError, ArtifactLocator


class LocatorTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.root = self.work / "root"
        self.root.mkdir()
        self.file = self.root / "mesh.glb"
        self.content = b"synthetic mesh"
        self.sha256 = hashlib.sha256(self.content).hexdigest()
        self.file.write_bytes(self.content)
        self.store = ArtifactLocator({"fixture": self.root})
        self.ref = {
            "schema": "vp.artifact_ref/v1",
            "locator": "af://fixture/mesh.glb",
            "sha256": self.sha256,
            "bytes": len(self.content),
            "media_type": "model/gltf-binary",
            "stage": "raw",
            "copy": "none",
        }
        self.request = {"op": "resolve", "locator": self.ref["locator"], "sha256": self.sha256}

    def assert_reference_without_copy(self, store):
        before = sorted(self.work.rglob("*"))
        original = self.file.stat()
        receipt = store.handle({"op": "put", "artifact_ref": self.ref})
        resolved = store.handle(self.request)
        self.assertEqual(sorted(self.work.rglob("*")), before)
        self.assertEqual(self.file.stat().st_ino, original.st_ino)
        self.assertEqual(self.file.read_bytes(), self.content)
        self.assertEqual(receipt, self.ref)
        self.assertEqual(resolved, self.ref)

    def assert_refuses_modified_file(self, store):
        with self.assertRaisesRegex(ArtifactError, "digest mismatch"):
            store.handle(self.request)

    def test_put_and_resolve_receipts_reference_original(self):
        self.assert_reference_without_copy(self.store)

    def test_resolve_rehashes_after_success(self):
        self.assert_reference_without_copy(self.store)
        self.file.write_bytes(b"x" * len(self.content))
        self.assert_refuses_modified_file(self.store)

    def test_refuses_wrong_digest_on_put_and_resolve(self):
        wrong = "0" * 64
        with self.assertRaisesRegex(ArtifactError, "digest mismatch"):
            self.store.put(dict(self.ref, sha256=wrong))
        self.store.put(self.ref)
        with self.assertRaisesRegex(ArtifactError, "digest mismatch"):
            self.store.handle(dict(self.request, sha256=wrong))

    def test_refuses_paths_outside_registered_roots(self):
        outside = self.work / "outside"
        outside.mkdir()
        file = outside / self.file.name
        file.write_bytes(self.content)
        for uri in (
            "af://unknown/mesh.glb",
            "af://fixture/../outside/mesh.glb",
            "af://fixture/%2e%2e%2foutside/mesh.glb",
            file.as_uri(),
            "file://other-host" + self.file.as_posix(),
        ):
            with self.subTest(uri=uri):
                with self.assertRaisesRegex(ArtifactError, "outside registered roots"):
                    self.store.put(dict(self.ref, locator=uri))
                with self.assertRaisesRegex(ArtifactError, "outside registered roots"):
                    self.store.handle(dict(self.request, locator=uri))

    def test_refuses_symlink_file_and_parent(self):
        outside = self.work / "outside"
        outside.mkdir()
        external_file = outside / self.file.name
        external_file.write_bytes(self.content)
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ArtifactError, "symbolic link"):
            self.store.put(dict(self.ref, locator="af://fixture/link/mesh.glb"))
        self.store.put(self.ref)
        self.file.unlink()
        self.file.symlink_to(external_file)
        with self.assertRaisesRegex(ArtifactError, "symbolic link"):
            self.store.handle(self.request)

    def test_refuses_file_replaced_while_hashing(self):
        self.store.put(self.ref)
        new_path = self.root / "replacement"
        new_path.write_bytes(self.content)
        read = os.read

        def replace_after_read(fd, size):
            result = read(fd, size)
            new_path.replace(self.file)
            return result

        with patch.object(locator.os, "read", side_effect=replace_after_read):
            with self.assertRaisesRegex(ArtifactError, "changed during verification"):
                self.store.handle(self.request)

    def test_file_uri_accepts_registered_root_and_encoded_filename(self):
        named = self.root / "mesh copy.glb"
        self.file.rename(named)
        result = self.store.put(dict(self.ref, locator=named.as_uri(), copy="file"))
        self.assertEqual(result, dict(self.ref, locator=named.as_uri()))
        self.assertEqual(self.store.resolve(named.as_uri(), self.sha256), result)

    def test_dry_run_and_failed_put_do_not_register(self):
        self.assertEqual(self.store.handle({"op": "put", "artifact_ref": self.ref}, dry_run=True), self.ref)
        with self.assertRaisesRegex(ArtifactError, "descriptor is not registered"):
            self.store.handle(self.request)
        with self.assertRaisesRegex(ArtifactError, "byte count mismatch"):
            self.store.put(dict(self.ref, bytes=len(self.content) + 1))
        with self.assertRaisesRegex(ArtifactError, "descriptor is not registered"):
            self.store.handle(self.request)

    def test_repeat_put_keeps_the_original_metadata(self):
        receipt = self.store.put(self.ref)
        receipt["stage"] = "caller-edited"
        self.assertEqual(self.store.put(self.ref), self.ref)
        with self.assertRaisesRegex(ArtifactError, "different descriptor"):
            self.store.put(dict(self.ref, stage="revised"))
        self.assertEqual(self.store.handle(self.request), self.ref)

    def test_refuses_non_regular_file(self):
        (self.root / "dir").mkdir()
        os.mkfifo(self.root / "pipe")
        for name in ("dir", "pipe"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ArtifactError, "regular file"):
                    self.store.put(dict(self.ref, locator="af://fixture/" + name))

    def test_frozen_adapter_rejects_extra_fields_and_missing_digest(self):
        with self.assertRaises(ValidationError):
            self.store.handle(dict(self.request, unexpected=True))
        with self.assertRaises(ValidationError):
            self.store.handle({"op": "resolve", "locator": self.ref["locator"]})
        with self.assertRaises(ValidationError):
            self.store.handle({"op": "put", "artifact_ref": dict(self.ref, unexpected=True)})

    def test_mutant_skip_hash_is_killed(self):
        self.store.put(self.ref)
        self.file.write_bytes(b"x" * len(self.content))
        with patch.object(locator, "_read_identity", return_value=(self.sha256, len(self.content))):
            with self.assertRaises(AssertionError):
                self.assert_refuses_modified_file(self.store)

    def test_mutant_copy_instead_of_reference_is_killed(self):
        class CopyingLocator(ArtifactLocator):
            def put(inner, artifact_ref, *, dry_run=False):
                result = super().put(artifact_ref, dry_run=dry_run)
                shutil.copyfile(self.file, self.root / "copy.glb")
                return result

        with self.assertRaises(AssertionError):
            self.assert_reference_without_copy(CopyingLocator({"fixture": self.root}))


if __name__ == "__main__":
    unittest.main()
