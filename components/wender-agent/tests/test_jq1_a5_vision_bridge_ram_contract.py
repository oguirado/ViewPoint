"""Regression tests for A5-D1 vision_bridge RAM-only contract."""
import unittest

from wender.perceive.vision_adapter import FakeVisionEngine, Frame, VisionEngineAdapter, fuse_regions
from wender.perceive import vision_bridge


class TestVisionBridgeRamContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Frame 1: 48x48 RGB pixels, initially all channels 16
        data1 = bytes([16]) * (48 * 48 * 3)
        cls.f1 = Frame(width=48, height=48, data=data1)

        # Frame 2: 48x48 RGB pixels with a 16x16 patch at x=16, y=32 with channels 160
        buf = bytearray(data1)
        for y in range(32, 48):
            for x in range(16, 32):
                idx = (y * 48 + x) * 3
                buf[idx] = 160
                buf[idx + 1] = 160
                buf[idx + 2] = 160
        data2 = bytes(buf)
        cls.f2 = Frame(width=48, height=48, data=data2)

        cls.engine = FakeVisionEngine(tile=16, threshold=8)

    def test_reference_screen_identity(self):
        identity = self.engine.screen_identity(self.f1)
        self.assertEqual(len(identity), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in identity))
        self.assertEqual(identity, "69141e963f90b801")

    def test_reference_changed_regions(self):
        identical_diff = self.engine.changed_regions(self.f1, self.f1)
        self.assertEqual(identical_diff, [])

        patch_diff = self.engine.changed_regions(self.f1, self.f2)
        self.assertEqual(
            patch_diff,
            [{"x": 16, "y": 32, "w": 16, "h": 16, "tiles": 1}],
        )

    def test_bridge_screen_identity(self):
        # A5-D1 contract: live bridge call for screen_identity
        identity = vision_bridge.screen_identity(self.f1)
        self.assertEqual(identity, self.engine.screen_identity(self.f1))

    def test_bridge_changed_regions_identical(self):
        # A5-D1 contract: live bridge call for changed_regions on identical frames
        regions = vision_bridge.changed_regions(self.f1, self.f1)
        self.assertEqual(regions, [])

    def test_bridge_changed_regions_patch(self):
        # A5-D1 contract: live bridge call for changed_regions on patched frame
        regions = vision_bridge.changed_regions(self.f1, self.f2)
        self.assertEqual(
            regions,
            [{"x": 16, "y": 32, "w": 16, "h": 16, "tiles": 1}],
        )

    def test_injected_adapter_receives_original_frames_and_residue_regions_fuse(self):
        class Adapter(VisionEngineAdapter):
            def __init__(self):
                self.calls = []

            def screen_identity(self, frame):
                self.calls.append(("identity", frame))
                return "0123456789abcdef"

            def changed_regions(self, prev, cur):
                self.calls.append(("regions", prev, cur))
                return [{"x": 16, "y": 0, "w": 1, "h": 5, "tiles": 1}]

        adapter = Adapter()
        before = (self.f1.width, self.f1.height, self.f1.data,
                  self.f2.width, self.f2.height, self.f2.data)
        self.assertEqual(vision_bridge.screen_identity(self.f1, adapter=adapter), "0123456789abcdef")
        regions = vision_bridge.changed_regions(self.f1, self.f2, adapter=adapter)
        self.assertIs(adapter.calls[0][1], self.f1)
        self.assertIs(adapter.calls[1][1], self.f1)
        self.assertIs(adapter.calls[1][2], self.f2)
        self.assertEqual(before, (self.f1.width, self.f1.height, self.f1.data,
                                  self.f2.width, self.f2.height, self.f2.data))
        state = {"nodes": [{"id": "n", "box": [12, 0, 1, 4], "actionable": False}]}
        self.assertEqual(fuse_regions(state, regions, 1250000, iou_num=1, iou_den=2), {
            "hits": [{"region": 0, "node": "n", "overlap": 4, "actionable": False}],
            "unmatched_regions": [],
        })
        with self.assertRaises(TypeError):
            vision_bridge.screen_identity(self.f1, adapter)
        with self.assertRaises(TypeError):
            vision_bridge.changed_regions(self.f1, self.f2, adapter)

    def test_invalid_frames_rejected_before_adapter_dispatch(self):
        class NeverCalled(VisionEngineAdapter):
            def screen_identity(self, frame):
                raise AssertionError("invalid input reached adapter")

            def changed_regions(self, prev, cur):
                raise AssertionError("invalid input reached adapter")

        adapter = NeverCalled()
        broken = Frame(1, 1, bytes(3))
        object.__setattr__(broken, "data", b"")
        for invalid in (None, self.f1.data, bytearray(self.f1.data), [], broken):
            with self.subTest(kind=type(invalid).__name__):
                with self.assertRaises(ValueError):
                    vision_bridge.screen_identity(invalid, adapter=adapter)
                with self.assertRaises(ValueError):
                    vision_bridge.changed_regions(invalid, self.f2, adapter=adapter)
                with self.assertRaises(ValueError):
                    vision_bridge.changed_regions(self.f1, invalid, adapter=adapter)
        with self.assertRaises(ValueError):
            vision_bridge.changed_regions(self.f1, Frame(1, 1, bytes(3)), adapter=adapter)

    def test_adapter_errors_propagate_unchanged(self):
        error = RuntimeError("adapter failed")

        class Failing(VisionEngineAdapter):
            def screen_identity(self, frame):
                raise error

            def changed_regions(self, prev, cur):
                raise error

        for call, args in ((vision_bridge.screen_identity, (self.f1,)),
                           (vision_bridge.changed_regions, (self.f1, self.f2))):
            with self.subTest(method=call.__name__), self.assertRaises(RuntimeError) as caught:
                call(*args, adapter=Failing())
            self.assertIs(caught.exception, error)


if __name__ == "__main__":
    unittest.main()
