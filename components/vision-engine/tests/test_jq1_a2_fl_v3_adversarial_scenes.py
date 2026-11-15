"""FL v3 adversarial acceptance scenes for distinguishing motion/redraws from two-state blinking.

Tests Scene S1 (horizontal stripe translation), Scene S2 (canvas redraws), and Scene S3
(genuine 2-state blinking showing unsafe emission-time lag-two hash filter failure).
"""
import unittest
import numpy as np

from eval.v2x.fl_blinkprior import FlickerTracker
from visioncore.desc import describe_padded
from visioncore.grid import Grid


class TestFLV3AdversarialScenes(unittest.TestCase):
    def test_scene_s1_horizontal_translation(self):
        """Scene S1: 64x64 vertical period-2 stripe image translated left 1px per frame for 10 frames."""
        grid = Grid(64, 64)
        ft = FlickerTracker(n_tiles=1, max_half=64)

        # Base image is 64 x (64 + 10) vertical period-2 stripes
        # Column x gets value (x % 2) * 255 across all RGB channels
        full_width = 64 + 10
        base_stripes = np.zeros((64, full_width, 3), dtype=np.uint8)
        for x in range(full_width):
            base_stripes[:, x, :] = (x % 2) * 255

        frames = []
        hashes = []
        emissions = []

        # Translating left by f pixels for f in 0..9 exposes new right columns
        for f in range(10):
            frame = base_stripes[:, f : f + 64, :].copy()
            frames.append(frame)

            padded = grid.pad(frame)
            out, _ = describe_padded(padded, grid, lazy_xh=False)
            h = int(out["xh64"][0])
            hashes.append(h)

            st, _ = ft.update([h])
            emissions.append(st[0])

        # Require zero FlickerTracker FLICKER emissions
        flicker_count = sum(1 for e in emissions if e == FlickerTracker.FLICKER)
        self.assertEqual(flicker_count, 0, "Scene S1 emitted unexpected FLICKER")

        # Verify hashes actually vary
        self.assertGreater(len(set(hashes)), 1, "Scene S1 hashes did not vary")

    def test_scene_s2_canvas_redraws(self):
        """Scene S2: ten 16x16 canvas-style RGB redraws with integer noise and fixed seeds 1000..1009."""
        grid = Grid(16, 16)
        ft = FlickerTracker(n_tiles=1, max_half=64)

        frames = []
        hashes = []
        emissions = []

        for seed in range(1000, 1010):
            rng = np.random.default_rng(seed)
            # Canvas-style integer noise: channel g in [0, 220], g+12, g+24
            g = rng.integers(0, 221, size=(16, 16), dtype=np.uint8)
            frame = np.stack([g, g + 12, g + 24], axis=-1)
            frames.append(frame)

            padded = grid.pad(frame)
            out, _ = describe_padded(padded, grid, lazy_xh=False)
            h = int(out["xh64"][0])
            hashes.append(h)

            st, _ = ft.update([h])
            emissions.append(st[0])

        # Require zero FlickerTracker FLICKER emissions
        flicker_count = sum(1 for e in emissions if e == FlickerTracker.FLICKER)
        self.assertEqual(flicker_count, 0, "Scene S2 emitted unexpected FLICKER")

        # Verify hashes actually vary across all 10 redraws
        self.assertEqual(len(set(hashes)), 10, "Scene S2 hashes did not vary across redraws")

    def test_scene_s3_genuine_blinking_and_unsafe_lag_two_filter(self):
        """Scene S3: 32 frames (0..31) alternating held runs of 8 (A zeros, B with 2x8 bar of RGB 250)."""
        grid = Grid(16, 16)
        ft = FlickerTracker(n_tiles=1, max_half=64)

        frame_A = np.zeros((16, 16, 3), dtype=np.uint8)
        frame_B = np.zeros((16, 16, 3), dtype=np.uint8)
        # B contains a 2 by 8 bar of RGB 250
        frame_B[4:6, 4:12, :] = 250

        frames = []
        for i in range(32):
            if (i // 8) % 2 == 0:
                frames.append(frame_A)
            else:
                frames.append(frame_B)

        hashes = []
        emissions = []
        half_periods = []

        for f, frame in enumerate(frames):
            padded = grid.pad(frame)
            out, _ = describe_padded(padded, grid, lazy_xh=False)
            h = int(out["xh64"][0])
            hashes.append(h)

            st, hp = ft.update([h])
            emissions.append(st[0])
            half_periods.append(hp[0])

        # Require FLICKER at frame 24 with half-period 8
        self.assertEqual(emissions[24], FlickerTracker.FLICKER, "Frame 24 did not emit FLICKER")
        self.assertEqual(half_periods[24], 8, "Frame 24 half-period is not 8")

        # Require no held-frame emissions (emissions only occur at toggle frames)
        for f in range(32):
            if emissions[f] == FlickerTracker.FLICKER:
                self.assertEqual(f, 24, f"Unexpected FLICKER emission at frame {f}")

        # At frame 24 the current state is B
        # Assert equalities: hash[24] == hash[8], hash[24] != hash[22], hash[24] != hash[0]
        self.assertEqual(hashes[24], hashes[8], "Frame 24 hash does not equal frame 8 hash")
        self.assertNotEqual(hashes[24], hashes[22], "Frame 24 hash equals frame 22 hash unexpectedly")
        self.assertNotEqual(hashes[24], hashes[0], "Frame 24 hash equals frame 0 hash unexpectedly")

        # Show that a literal emission-time current-equals-lag-two hash filter rejects the genuine frame-24 emission
        lag2_filter_passes = (hashes[24] == hashes[24 - 2])  # hashes[24] == hashes[22]
        self.assertFalse(
            lag2_filter_passes,
            "Literal emission-time current-equals-lag-two filter should be False at frame 24",
        )


if __name__ == "__main__":
    unittest.main()
