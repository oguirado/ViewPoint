"""Real-checkout integration tests; synthetic RGB frames never leave RAM."""
import hashlib
import os
from pathlib import Path
import pickle
import subprocess
import sys
import unittest
from unittest.mock import patch

from wender.perceive.vision_adapter import Frame, VisionEngineAdapter, fuse_regions
from wender.perceive.vision_engine_real import RealVisionEngine, _WORKER
from wender.state import schema


ROOT = Path(os.environ.get("VISION_ENGINE_ROOT", str(Path(__file__).resolve().parents[2] / "vision-engine")))


@unittest.skipUnless(ROOT.exists(), "VISION_ENGINE_ROOT does not exist: %s" % ROOT)
class TestRealVisionEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import numpy as np
        cls.np = np

    def setUp(self):
        self.engine = RealVisionEngine()
        self.first = self.np.full((96, 128, 3), 32, dtype=self.np.uint8)
        self.first[32:64, 16:48] = 200
        self.moved = self.np.full_like(self.first, 32)
        self.moved[32:64, 64:96] = 200

    def test_same_frame_identity_and_engine_comparison(self):
        self.assertIsInstance(self.engine, VisionEngineAdapter)
        fingerprint = self.engine.screen_fingerprint(self.first)
        self.assertIsInstance(fingerprint, bytes)
        identity = self.engine.screen_identity(self.first)
        self.assertRegex(identity, r"^[0-9a-f]{16}$")
        self.assertEqual(identity, hashlib.sha256(fingerprint).hexdigest()[:16])
        self.assertEqual(identity, self.engine.screen_identity(self.first.copy()))
        self.assertTrue(self.engine.same_screen(self.first, self.first.copy()))
        distance = self.engine.fingerprint_distance(self.first, self.first)
        self.assertIs(type(distance), int)
        self.assertEqual(distance, 0)
        self.assertEqual(self.engine.changed_regions(self.first, self.first), [])

    def test_moved_button_regions_fuse_with_actionable_state(self):
        regions = self.engine.changed_regions(self.first, self.moved)
        self.assertTrue(regions)
        self.assertEqual(regions, sorted(regions, key=lambda r: (r["y"], r["x"], r["h"], r["w"], r["tiles"])))
        for region in regions:
            self.assertEqual(set(region), {"x", "y", "w", "h", "tiles"})
            self.assertTrue(all(type(value) is int for value in region.values()))
            self.assertGreater(region["tiles"], 0)
        button, unrelated = "0000000000000001", "0000000000000002"
        nodes = [{"id": node_id, "role": "button", "name": name,
                  "depth": 0, "ordinal": ordinal, "attrs": ()}
                 for ordinal, (node_id, name) in enumerate(((button, "Save"), (unrelated, "Other")))]
        state = schema.build_state(
            nodes, kind="cdp", url="https://example.test", title="Synthetic",
            ax_sha256="a" * 64, ax_stable_sha256="b" * 64,
            prune={"name": "prune_ax_v1", "version": 1}, raw_count=2,
            boxes={button: [64, 32, 32, 32], unrelated: [0, 80, 16, 16]},
            viewport={"w": 128, "h": 96, "scroll_x": 0, "scroll_y": 0,
                      "doc_w": 128, "doc_h": 96, "dpr_ppm": 1000000},
            actionable_roles={"button"},
        )
        fused = fuse_regions(state, regions)
        self.assertEqual({hit["node"] for hit in fused["hits"]}, {button})
        self.assertTrue(all(hit["actionable"] for hit in fused["hits"]))
        schema.dumps(fused)  # Canonical serialization rejects floating-point values.

    def test_frame_contract_noncontiguous_arrays_and_input_immutability(self):
        before = self.first.copy()
        frame = Frame(128, 96, self.first.tobytes())
        self.assertEqual(self.engine.screen_identity(frame), self.engine.screen_identity(self.first))
        view = self.first[:, ::-1]
        self.assertEqual(self.engine.screen_identity(view), self.engine.screen_identity(view.copy()))
        self.engine.changed_regions(self.first, self.moved)
        self.np.testing.assert_array_equal(self.first, before)
        self.assertTrue(self.first.flags.writeable)
        for bad in (b"rgb", self.first.astype(self.np.float64), self.first[:, :, 0], self.first[:0]):
            with self.subTest(type=type(bad).__name__), self.assertRaises(ValueError):
                self.engine.screen_identity(bad)
        with self.assertRaises(ValueError):
            self.engine.changed_regions(self.first, self.moved[:32])

    def test_engine_imports_are_isolated_and_frames_use_pipes(self):
        # Pre-existing modules from another checkout must not be replaced or used.
        sentinel = object()
        with patch.dict(sys.modules, {"visioncore": sentinel, "sf_tiledesc": sentinel}):
            previous = {k: v for k, v in sys.modules.items()
                        if k == "visioncore" or k.startswith("visioncore.") or k == "sf_tiledesc"}
            path = sys.path[:]
            run = subprocess.run
            with patch("wender.perceive.vision_engine_real.subprocess.run", wraps=run) as spy:
                self.engine.screen_identity(self.first)
                self.engine.changed_regions(self.first, self.moved)
            for call in spy.call_args_list:
                self.assertIn("-B", call.args[0])
                buffers = pickle.loads(call.kwargs["input"])
                self.assertEqual(buffers[0], (128, 96, self.first.tobytes()))
                self.assertGreater(call.kwargs["timeout"], 0)
            current = {k: v for k, v in sys.modules.items()
                       if k == "visioncore" or k.startswith("visioncore.") or k == "sf_tiledesc"}
            self.assertEqual(current, previous)
            self.assertEqual(sys.path, path)

    def test_storage_law_guard_rejects_write_negative(self):
        # Probe the actual worker guard without writing any pixel data anywhere.
        guard = _WORKER.split("root, operation = sys.argv[1:3]", 1)[0]
        result = subprocess.run([sys.executable, "-B", "-c", guard + '\nopen(os.devnull, "wb")'],
                                capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"STORAGE LAW", result.stderr)

    def test_b11_brightness_result_matches_unmodified_engine(self):
        # B11 is an upstream limitation, not a bridge correction. This fixture
        # records actual checkout behavior even when an upstream revision fixes it.
        shifted = self.first + self.np.uint8(1)
        script = r'''
import importlib.util, pickle, sys
sys.path.insert(0, sys.argv[1])
import numpy as np
spec = importlib.util.spec_from_file_location("sf", sys.argv[1] + "/eval/v2x/sf_tiledesc.py")
sf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sf)
arrays = [np.frombuffer(data, np.uint8).reshape(96,128,3) for data in pickle.loads(sys.stdin.buffer.read())]
fingerprints = [sf.screen_fingerprint(a) for a in arrays]
sys.stdout.buffer.write(pickle.dumps((sf.same_screen(*fingerprints), sf.fingerprint_distance(*fingerprints))))
'''
        reference = subprocess.run([sys.executable, "-B", "-c", script, str(self.engine.root)],
                                   input=pickle.dumps([self.first.tobytes(), shifted.tobytes()]),
                                   capture_output=True, timeout=60, check=True)
        same, distance = pickle.loads(reference.stdout)
        self.assertEqual(self.engine.same_screen(self.first, shifted), same)
        self.assertEqual(self.engine.fingerprint_distance(self.first, shifted), distance)
        self.assertNotEqual(self.engine.screen_identity(self.first), self.engine.screen_identity(shifted))

    def test_missing_checkout_fails_without_fake_fallback(self):
        with patch.dict(os.environ, {"VISION_ENGINE_ROOT": str(self.engine.root / "__missing_checkout__")}):
            with self.assertRaisesRegex(FileNotFoundError, "VISION_ENGINE_ROOT"):
                RealVisionEngine()


if __name__ == "__main__":
    unittest.main()
