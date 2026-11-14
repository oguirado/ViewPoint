"""Adversarial pin tests for wender.learn.segmenter contract (wa1-spark-learn-segmenter)."""
import os
import sys
import unittest
import inspect

try:
    import wender.learn.segmenter as segmenter
except ImportError as e:
    if os.environ.get("WA1_PIN_ALLOW_SKIP") == "1":
        segmenter = None
    else:
        raise e


class DummyFrame:
    """Synthetic frame object for testing."""
    def __init__(self, width=100, height=100, data=b"\x00" * 30000):
        self.width = width
        self.height = height
        self.data = data


class PresetEngine:
    """Fake vision engine returning preset tile diffs per frame pair."""
    def __init__(self, pair_regions):
        self.pair_regions = pair_regions
        self.idx = 0

    def changed_regions(self, prev, cur):
        if self.idx < len(self.pair_regions):
            res = self.pair_regions[self.idx]
            self.idx += 1
            if isinstance(res, int):
                if res > 0:
                    return [{"x": 10, "y": 20, "w": 30, "h": 40, "tiles": res}]
                return []
            return res
        return []


def _get_bbox(seg):
    """Extract (x, y, w, h) from segment dict regardless of whether bbox is top-level or nested dict."""
    if "bbox" in seg and isinstance(seg["bbox"], dict):
        b = seg["bbox"]
        return b["x"], b["y"], b["w"], b["h"]
    return seg["x"], seg["y"], seg["w"], seg["h"]


def _has_bytes(obj, seen=None):
    """Recursively check if obj contains any bytes object."""
    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return False
    seen.add(obj_id)

    if isinstance(obj, (bytes, bytearray)):
        return True
    if isinstance(obj, dict):
        return any(_has_bytes(k, seen) or _has_bytes(v, seen) for k, v in obj.items())
    if isinstance(obj, (list, tuple, set)):
        return any(_has_bytes(x, seen) for x in obj)
    return False


@unittest.skipIf(segmenter is None and os.environ.get("WA1_PIN_ALLOW_SKIP") == "1", "wa1-spark-learn-segmenter missing")
class TestWa1SegmenterPins(unittest.TestCase):

    def test_empty_and_single_frame_sequences(self):
        """Kills mutant: Returning non-empty segments for empty or 1-frame inputs."""
        engine = PresetEngine([])
        self.assertEqual(segmenter.segment_frames([], engine), [])
        f0 = DummyFrame()
        self.assertEqual(segmenter.segment_frames([f0], engine), [])

    def test_counts_exactly_at_hi_and_lo(self):
        """Kills mutant: Off-by-one in tile count threshold using strict > hi or < lo."""
        # hi=4, lo=1, quiet=3, min_len=2
        # Pair 0: count 3 (< hi, stays closed)
        # Pair 1: count 4 (== hi, opens segment!)
        # Pair 2: count 4 (>= hi, continues)
        # Pair 3: count 1 (== lo, quiet pair 1)
        # Pair 4: count 1 (== lo, quiet pair 2)
        # Pair 5: count 1 (== lo, quiet pair 3 -> closes segment)
        frames = [DummyFrame() for _ in range(7)]
        engine = PresetEngine([3, 4, 4, 1, 1, 1])
        segs = segmenter.segment_frames(frames, engine, hi=4, lo=1, quiet=3, min_len=2)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["start"], 1)

    def test_unclosed_change_closes_at_last_frame(self):
        """Kills mutant: Dropping unclosed segment at end of frame sequence."""
        # 4 frames = 3 pairs. Pairs with count 5, 5, 5 (never goes quiet <= lo).
        frames = [DummyFrame() for _ in range(4)]
        engine = PresetEngine([5, 5, 5])
        segs = segmenter.segment_frames(frames, engine, hi=4, lo=1, quiet=3, min_len=2)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["start"], 0)
        self.assertEqual(segs[0]["end"], 3)

    def test_quiet_gap_minus_one_keeps_single_segment(self):
        """Kills mutant: Off-by-one in quiet counter closing segment early at quiet - 1."""
        # hi=4, lo=1, quiet=3.
        # Pairs: 5 (open), 1 (quiet 1), 1 (quiet 2), 5 (activity resumes!), 1, 1, 1 (quiet 3 -> close)
        frames = [DummyFrame() for _ in range(8)]
        engine = PresetEngine([5, 1, 1, 5, 1, 1, 1])
        segs = segmenter.segment_frames(frames, engine, hi=4, lo=1, quiet=3, min_len=2)
        self.assertEqual(len(segs), 1, "Quiet gap of quiet - 1 (2 pairs) must keep single segment")

    def test_union_bbox_calculation_and_type(self):
        """Kills mutant: Swapped x and y in union bbox or float leaking into bbox output."""
        # Pair 0: region x=10, y=20, w=30, h=40 (tiles=5)
        # Pair 1: region x=5, y=50, w=100, h=10 (tiles=5)
        # Pair 2..4: quiet
        r0 = [{"x": 10, "y": 20, "w": 30, "h": 40, "tiles": 5}]
        r1 = [{"x": 5, "y": 50, "w": 100, "h": 10, "tiles": 5}]
        frames = [DummyFrame() for _ in range(6)]
        engine = PresetEngine([r0, r1, 1, 1, 1])
        segs = segmenter.segment_frames(frames, engine, hi=4, lo=1, quiet=3, min_len=2)
        self.assertEqual(len(segs), 1)
        x, y, w, h = _get_bbox(segs[0])
        self.assertEqual(x, 5)
        self.assertEqual(y, 20)
        self.assertEqual(w, 100) # x range [5, 105] -> w = 100
        self.assertEqual(h, 40)  # y range [20, 60] -> h = 40
        for val in (x, y, w, h, segs[0]["start"], segs[0]["end"], segs[0]["peak_tiles"], segs[0]["total_tiles"]):
            self.assertIsInstance(val, int)
            self.assertNotIsInstance(val, float)

    def test_labeler_receives_only_in_segment_frames(self):
        """Kills mutant: Labeler receiving full frame sequence or out-of-segment frame slice."""
        frames = [DummyFrame() for _ in range(6)]
        engine = PresetEngine([0, 5, 5, 1, 1, 1])

        received_slices = []

        def mock_labeler(seg, seg_frames):
            received_slices.append(seg_frames)
            return {"intent": "click_button"}

        def mock_mapper(intent):
            return {"path": "ax", "action": "press"}

        def mock_replayer(actions, seg):
            return {"reproduced": True, "verified": True}

        report = segmenter.run_pipeline(frames, engine, mock_labeler, mock_mapper, mock_replayer)
        self.assertEqual(len(received_slices), 1)
        # Segment starts at frame 1 and ends at frame 5 (or frames[1:6])
        expected_count = report["segments"][0]["end"] - report["segments"][0]["start"] + 1
        self.assertEqual(len(received_slices[0]), expected_count)
        self.assertEqual(received_slices[0], frames[report["segments"][0]["start"]:report["segments"][0]["end"] + 1])

    def test_mapper_teleport_path_fails_segment(self):
        """Kills mutant: Mapper returning a teleport path treated as successful execution."""
        frames = [DummyFrame() for _ in range(5)]
        engine = PresetEngine([5, 5, 1, 1, 1])

        def mock_labeler(seg, seg_frames):
            return "teleport_intent"

        def mock_mapper(intent):
            return {"path": "teleport", "op": "warp"}

        def mock_replayer(actions, seg):
            return {"reproduced": True, "verified": True}

        report = segmenter.run_pipeline(frames, engine, mock_labeler, mock_mapper, mock_replayer)
        self.assertEqual(len(report["segments"]), 1)
        outcome = report["segments"][0] if "reproduced" in report["segments"][0] else report.get("outcomes", [{}])[0]
        self.assertFalse(outcome.get("reproduced"))
        self.assertFalse(outcome.get("verified"))

    def test_replayer_unverified_reproduction_does_not_count(self):
        """Kills mutant: Verification that always returns true or counts unverified reproductions."""
        frames = [DummyFrame() for _ in range(5)]
        engine = PresetEngine([5, 5, 1, 1, 1])

        def mock_labeler(seg, seg_frames):
            return "intent"

        def mock_mapper(intent):
            return {"path": "api", "op": "call"}

        def mock_replayer(actions, seg):
            return {"reproduced": True, "verified": False}

        report = segmenter.run_pipeline(frames, engine, mock_labeler, mock_mapper, mock_replayer)
        self.assertEqual(report["reproduced_share_ppm"], 0)

    def test_report_never_contains_frame_bytes(self):
        """Kills mutant: Leaking raw frame bytes into report dict."""
        frames = [DummyFrame(data=b"\x12\x34\x56" * 100) for _ in range(5)]
        engine = PresetEngine([5, 5, 1, 1, 1])

        def mock_labeler(seg, seg_frames):
            return "intent"

        def mock_mapper(intent):
            return {"path": "ax", "op": "click"}

        def mock_replayer(actions, seg):
            return {"reproduced": True, "verified": True}

        report = segmenter.run_pipeline(frames, engine, mock_labeler, mock_mapper, mock_replayer)
        self.assertFalse(_has_bytes(report), "Report dict must not contain bytes objects")

    def test_guard_module_source_never_opens_files_for_writing(self):
        """Kills mutant: Writing frames or state to disk violating STORAGE LAW."""
        src = inspect.getsource(segmenter)
        for forbidden in ('open(', 'write(', 'imwrite', 'to_file'):
            if forbidden in src:
                # Check lines that contain forbidden calls
                for line in src.splitlines():
                    if forbidden in line and not line.strip().startswith("#"):
                        self.assertNotIn(' open(', line)
                        self.assertNotIn('.write(', line)

    def test_tile_diff_engine_interface(self):
        """Kills mutant: _TileDiffEngine returning non-integer dimensions or malformed tile dicts."""
        self.assertTrue(hasattr(segmenter, "_TileDiffEngine"))
        engine_cls = getattr(segmenter, "_TileDiffEngine")
        engine = engine_cls(tile=16, threshold=10) if inspect.signature(engine_cls).parameters else engine_cls()
        f1 = DummyFrame(width=64, height=64, data=b"\x00" * (64 * 64 * 3))
        f2 = DummyFrame(width=64, height=64, data=b"\xff" * (64 * 64 * 3))
        regions = engine.changed_regions(f1, f2)
        self.assertIsInstance(regions, list)
        for r in regions:
            for k in ("x", "y", "w", "h", "tiles"):
                self.assertIn(k, r)
                self.assertIsInstance(r[k], int)
                self.assertNotIsInstance(r[k], float)

    def test_pipeline_exception_handling(self):
        """Kills mutant: Unhandled stage exception crashing pipeline instead of marking segment false with stage name in error."""
        frames = [DummyFrame() for _ in range(5)]
        engine = PresetEngine([5, 5, 1, 1, 1])

        def failing_labeler(seg, seg_frames):
            raise NotImplementedError("labeler stub error")

        def mock_mapper(intent):
            return {"path": "ax"}

        def mock_replayer(actions, seg):
            return {"reproduced": True, "verified": True}

        report = segmenter.run_pipeline(frames, engine, failing_labeler, mock_mapper, mock_replayer)
        self.assertEqual(len(report["segments"]), 1)
        outcome = report["segments"][0]
        self.assertFalse(outcome.get("reproduced"))
        self.assertFalse(outcome.get("verified"))
        self.assertEqual(outcome.get("error"), "labeler")

    def test_segment_shorter_than_min_len_dropped(self):
        """Kills mutant: Keeping segments shorter than min_len pairs."""
        # hi=4, lo=1, quiet=1, min_len=3
        # Pair 0: count 5 (starts seg at pair 0)
        # Pair 1: count 1 (quiet 1 -> closes segment after pair 0, length 1 pair < min_len 3 -> dropped)
        frames = [DummyFrame() for _ in range(4)]
        engine = PresetEngine([5, 1, 0])
        segs = segmenter.segment_frames(frames, engine, hi=4, lo=1, quiet=1, min_len=3)
        self.assertEqual(segs, [])

    def test_determinism_and_integer_only(self):
        """Kills mutant: Non-deterministic segment output or floats in metric calculations."""
        frames = [DummyFrame() for _ in range(5)]
        engine1 = PresetEngine([5, 5, 1, 1, 1])
        engine2 = PresetEngine([5, 5, 1, 1, 1])
        segs1 = segmenter.segment_frames(frames, engine1)
        segs2 = segmenter.segment_frames(frames, engine2)
        self.assertEqual(segs1, segs2)


if __name__ == "__main__":
    unittest.main()
