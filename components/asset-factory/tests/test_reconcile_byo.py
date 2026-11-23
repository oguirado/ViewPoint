"""BYO admits static materials and preserves its stricter resource boundary."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from asset_factory import core
from tools.blender import glb_io
from tools.pipeline import engine, stages
from providers.trellis2 import adapter
from test_f2u1_mesh import glb


class ByoBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "static.glb"
        self.reference = self.root / "reference.png"
        self.reference.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-reference")
        self.doc, self.binary = glb_io.decode_glb(glb())
        self.doc["materials"] = [{"pbrMetallicRoughness": {
            "baseColorFactor": [0.5, 0.25, 0.125, 1], "metallicFactor": 0,
            "roughnessFactor": 1}}]
        self.doc["meshes"][0]["primitives"][0]["material"] = 0
        self.directory = self.root / "provider"
        self.directory.mkdir()

    def backend(self):
        self.source.write_bytes(glb_io.encode_glb(self.doc, self.binary))
        return stages.NativeStages(source=self.source, reference=self.reference,
            provider="byo", options={"timeout_s": 10})

    def test_material_bearing_static_byo_keeps_exact_bytes(self):
        result = self.backend().stage_provider(self.directory, None)
        self.assertEqual(result["rc"], 0)
        self.assertEqual((self.directory / "raw.glb").read_bytes(), self.source.read_bytes())
        self.assertEqual(engine.load(self.directory / "custody.json")["licence"],
                         "caller_owned_not_evaluated")

    def test_external_resource_is_refused_before_publication(self):
        self.doc["buffers"][0]["uri"] = "external.bin"
        with self.assertRaisesRegex(glb_io.GlbError, "external_resource"):
            self.backend().stage_provider(self.directory, None)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_dynamic_content_is_refused_before_publication(self):
        self.doc["animations"] = [{}]
        with self.assertRaisesRegex(glb_io.GlbError, "unsupported_dynamic_or_camera"):
            self.backend().stage_provider(self.directory, None)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_byo_validator_is_part_of_provider_cache_identity(self):
        backend = self.backend()
        before = backend._pin("provider")
        original = core.sha256_file
        def changed(path):
            return "e" * 64 if Path(path) == Path(glb_io.__file__) else original(path)
        with mock.patch.object(core, "sha256_file", side_effect=changed):
            self.assertNotEqual(backend._pin("provider"), before)

    def test_trellis_geometry_subset_still_refuses_materials(self):
        self.backend()
        with self.assertRaises(adapter.Refusal) as refusal:
            adapter.validate_glb(self.source.read_bytes())
        self.assertEqual(refusal.exception.rc, 2)
        self.assertEqual(refusal.exception.code, "glb_layout_unsupported")


if __name__ == "__main__":
    unittest.main(verbosity=2)
