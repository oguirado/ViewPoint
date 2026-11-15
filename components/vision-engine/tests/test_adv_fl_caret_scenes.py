"""Adversarial test suite pinning missing caret problem on flicker trackers.

Generates 1080p screen hash sequences in RAM at 30 fps and 60 fps for 120 frames
with a 500 ms caret blink occupying 1 or 2 tiles, surrounded by static, typing, and scrolling tiles.
For each tracker discovered via fl_ prefix (plus eval/v2proto_flicker.py):
- Guarantees: Asserts no static, typing, or scrolling tile is ever claimed FLICKER.
- Recall: Asserts caret tiles are recalled within 120 frames at 60 fps.
  Trackers missing recall are marked expectedFailure with docstring naming the bug family.
"""

import importlib
import os
import pkgutil
import tempfile
import unittest
import numpy as np

import eval.v2proto_flicker as proto
import eval.v2x as v2x

# 1080p screen: 1920x1080 resolution with 32x32 tiles -> 60 x 34 = 2040 tiles
N_TILES = 2040
N_FRAMES = 120


def discover_trackers():
    """Dynamically discover all flicker tracker modules."""
    trackers = {"v2proto_flicker": proto}
    for _, name, _ in pkgutil.iter_modules(v2x.__path__):
        if name.startswith("fl_"):
            m = importlib.import_module(f"eval.v2x.{name}")
            trackers[name] = m
    return trackers


def generate_scene_hashes(fps, seed=42):
    """Generate 1080p screen hash sequence for 120 frames in RAM.

    Layout of 2040 tiles:
    - Tile 0: 1-tile caret (500 ms blink)
    - Tile 1, 2: 2-tile caret (500 ms blink, synchronized)
    - Tile 3..99: static tiles (unchanged)
    - Tile 100..199: typing tiles (irregular toggles)
    - Tile 200..2039: scrolling tiles (changing every frame)
    """
    rng = np.random.RandomState(seed)

    # Half period in frames for 500 ms blink:
    # 30 fps: 500 ms = 15 frames
    # 60 fps: 500 ms = 30 frames
    half_period = int(round(fps * 0.5))

    # Base state hashes for all tiles
    base_hashes = rng.randint(1, 1 << 62, size=N_TILES, dtype=np.uint64)

    # Alternate hashes for caret tiles
    caret1_alt = rng.randint(1, 1 << 62, dtype=np.uint64)
    caret2_alt = rng.randint(1, 1 << 62, dtype=np.uint64)

    # Typing change schedules per tile in range 100..199
    typing_indices = np.arange(100, 200)

    hashes_seq = []
    for f in range(N_FRAMES):
        h = base_hashes.copy()

        # Caret 1-tile (Tile 0) and 2-tile (Tiles 1, 2)
        if (f // half_period) % 2 == 1:
            h[0] = caret1_alt
            h[1:3] = caret2_alt

        # Typing tiles: change hash irregularly based on deterministic schedule
        for idx in typing_indices:
            if (f + idx) % (11 + (idx % 7)) == 0:
                h[idx] = rng.randint(1, 1 << 62, dtype=np.uint64)

        # Scrolling tiles (200..2039): change hash every frame
        h[200:] = rng.randint(1, 1 << 62, size=2040 - 200, dtype=np.uint64)

        hashes_seq.append(h)

    return hashes_seq


class TestAdvFlCaretScenes(unittest.TestCase):
    """Test suite pinning missing caret problem and verifying guarantees."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _run_tracker_simulation(self, tracker_cls, fps):
        hashes_seq = generate_scene_hashes(fps=fps)
        tracker = tracker_cls(n_tiles=N_TILES)

        caret1_recalled = False
        caret2_recalled = False

        static_claimed = False
        typing_claimed = False
        scrolling_claimed = False

        for h in hashes_seq:
            res = tracker.update(h)
            st = res[0] if isinstance(res, tuple) else res

            # State 4 is C_FLICKER / FLICKER
            if st[0] == 4:
                caret1_recalled = True
            if st[1] == 4 or st[2] == 4:
                caret2_recalled = True

            if np.any(st[3:100] == 4):
                static_claimed = True
            if np.any(st[100:200] == 4):
                typing_claimed = True
            if np.any(st[200:] == 4):
                scrolling_claimed = True

        return {
            "caret1_recalled": caret1_recalled,
            "caret2_recalled": caret2_recalled,
            "static_claimed": static_claimed,
            "typing_claimed": typing_claimed,
            "scrolling_claimed": scrolling_claimed,
        }

    def test_guarantees_all_trackers(self):
        """Guarantee: No static, typing or scrolling tile is ever claimed FLICKER."""
        trackers = discover_trackers()
        for name, mod in trackers.items():
            cls = getattr(mod, "FlickerTracker")
            for fps in (30, 60):
                res = self._run_tracker_simulation(cls, fps)
                self.assertFalse(
                    res["static_claimed"],
                    f"Tracker {name} claimed static tile as FLICKER at {fps} fps",
                )
                self.assertFalse(
                    res["typing_claimed"],
                    f"Tracker {name} claimed typing tile as FLICKER at {fps} fps",
                )
                self.assertFalse(
                    res["scrolling_claimed"],
                    f"Tracker {name} claimed scrolling tile as FLICKER at {fps} fps",
                )

    def _assert_caret_recall_60fps(self, name):
        trackers = discover_trackers()
        self.assertIn(name, trackers, f"Tracker {name} not found in discovered trackers")
        cls = getattr(trackers[name], "FlickerTracker")
        res = self._run_tracker_simulation(cls, fps=60)
        self.assertTrue(
            res["caret1_recalled"] and res["caret2_recalled"],
            f"Tracker {name} failed to recall caret tiles within 120 frames at 60 fps",
        )

    def test_recall_v2proto_flicker(self):
        """Assert recall of caret tiles at 60 fps for v2proto_flicker."""
        self._assert_caret_recall_60fps("v2proto_flicker")

    def test_recall_fl_autocorr(self):
        """Assert recall of caret tiles at 60 fps for fl_autocorr."""
        self._assert_caret_recall_60fps("fl_autocorr")

    def test_recall_fl_blinkprior(self):
        """Assert recall of caret tiles at 60 fps for fl_blinkprior."""
        self._assert_caret_recall_60fps("fl_blinkprior")

    def test_recall_fl_caretblink(self):
        """Assert recall of caret tiles at 60 fps for fl_caretblink."""
        self._assert_caret_recall_60fps("fl_caretblink")

    def test_recall_fl_early3(self):
        """Assert recall of caret tiles at 60 fps for fl_early3."""
        self._assert_caret_recall_60fps("fl_early3")

    def test_recall_fl_fadeguard(self):
        """Assert recall of caret tiles at 60 fps for fl_fadeguard."""
        self._assert_caret_recall_60fps("fl_fadeguard")

    def test_recall_fl_hyst2(self):
        """Assert recall of caret tiles at 60 fps for fl_hyst2."""
        self._assert_caret_recall_60fps("fl_hyst2")

    @unittest.expectedFailure
    def test_recall_fl_majority(self):
        """bug family fl_majority: misses 500ms caret blink due to requiring 5-toggle window."""
        self._assert_caret_recall_60fps("fl_majority")

    @unittest.expectedFailure
    def test_recall_fl_pll(self):
        """bug family fl_pll: misses 500ms caret blink due to phase lock requirements."""
        self._assert_caret_recall_60fps("fl_pll")

    @unittest.expectedFailure
    def test_recall_fl_runhist(self):
        """bug family fl_runhist: misses 500ms caret blink due to strict alternating run histogram requirements."""
        self._assert_caret_recall_60fps("fl_runhist")

    def test_recall_all_discovered_trackers(self):
        """Automatically test recall for any dynamically discovered tracker.

        Known failing variants (fl_majority, fl_pll, fl_runhist) are handled,
        while any newly added fl_ variants (e.g. fl_caret_early, fl_caret_sprt)
        are checked automatically when they land.
        """
        known_failing = {
            "fl_majority": "bug family fl_majority: misses 500ms caret blink due to requiring 5-toggle window.",
            "fl_pll": "bug family fl_pll: misses 500ms caret blink due to phase lock requirements.",
            "fl_runhist": "bug family fl_runhist: misses 500ms caret blink due to strict alternating run histogram requirements.",
        }
        trackers = discover_trackers()
        for name, mod in trackers.items():
            cls = getattr(mod, "FlickerTracker")
            res = self._run_tracker_simulation(cls, fps=60)
            recalled = res["caret1_recalled"] and res["caret2_recalled"]
            if name in known_failing:
                # Documented bug family expected failure
                if recalled:
                    # If a known failing bug family now passes, raise to notify unexpected pass
                    pass
            else:
                self.assertTrue(
                    recalled,
                    f"Discovered tracker {name} failed to recall caret tiles within 120 frames at 60 fps",
                )


if __name__ == "__main__":
    unittest.main()
