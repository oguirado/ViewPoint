"""Unit tests for wender.learn.segmenter: synthetic 30-frame RAM sequence and pipeline stages."""
from types import SimpleNamespace
import unittest

from wender.learn import demonstrations
from wender.learn.segmenter import _TileDiffEngine, run_pipeline, segment_frames
from wender.state.schema import dumps

WIDTH = 64
HEIGHT = 48


def make_frame(rects=(), bg=(16, 16, 16)):
    """Build an in-RAM 64x48 RGB24 frame with optional filled rectangles (x, y, w, h, rgb)."""
    buf = bytearray(bg * (WIDTH * HEIGHT))
    stride = WIDTH * 3
    for x0, y0, w, h, rgb in rects:
        row_bytes = bytes(rgb) * w
        for y in range(y0, y0 + h):
            start = y * stride + x0 * 3
            buf[start : start + w * 3] = row_bytes
    return SimpleNamespace(width=WIDTH, height=HEIGHT, data=bytes(buf))


def build_synthetic_30_frames():
    """Generate 30 frames (64x48 RGB24) in memory:
    - frames 0..2: still screen
    - frames 3..6 (pairs 2, 3, 4, 5): panel opening over 4 frames at x=8..40, y=8..40
    - frames 7..11 (pairs 6..10): stillness (5 quiet pairs)
    - frames 12..14 (pairs 11, 12, 13): second change over 3 frames at x=40..56, y=16..32
    - frames 15..20 (pairs 14..19): stillness (6 quiet pairs)
    - frame 21 (pair 20): single-frame flicker (4 tiles at x=0..16, y=0..16) followed by
      stillness through frame 29, dropped by min_len=2.
    """
    frames = []
    for _ in range(3):
        frames.append(make_frame())

    panel_rects = []
    for step in range(4):
        panel_rects = panel_rects + [(8 + step * 8, 8, 8, 32, (200, 120, 40))]
        frames.append(make_frame(panel_rects))

    for _ in range(5):
        frames.append(make_frame(panel_rects))

    for color in ((40, 180, 220), (80, 220, 100), (240, 200, 60)):
        frames.append(make_frame(panel_rects + [(40, 16, 16, 16, color)]))

    steady_after_second = panel_rects + [(40, 16, 16, 16, (240, 200, 60))]
    for _ in range(6):
        frames.append(make_frame(steady_after_second))

    flicker_state = steady_after_second + [(0, 0, 16, 16, (255, 0, 128))]
    for _ in range(9):
        frames.append(make_frame(flicker_state))

    assert len(frames) == 30
    return frames


def build_three_segment_frames():
    """Extend the synthetic in-RAM sequence so 3 valid segments survive default min_len=2."""
    frames = build_synthetic_30_frames()[:21]
    base = [
        (8, 8, 32, 32, (200, 120, 40)),
        (40, 16, 16, 16, (240, 200, 60)),
        (0, 0, 16, 16, (255, 0, 128)),
    ]
    frames.append(make_frame(base + [(0, 32, 16, 16, (100, 250, 100))]))
    frames.append(make_frame(base + [(0, 32, 16, 16, (20, 140, 255))]))
    for _ in range(7):
        frames.append(make_frame(base + [(0, 32, 16, 16, (20, 140, 255))]))
    assert len(frames) == 30
    return frames


class TestSegmenter(unittest.TestCase):
    def test_synthetic_30_frames_boundaries_bboxes_and_determinism(self):
        frames = build_synthetic_30_frames()
        self.assertEqual(len(frames), 30)
        for f in frames:
            self.assertEqual((f.width, f.height, len(f.data)), (64, 48, 64 * 48 * 3))
            self.assertIsInstance(f.data, bytes)

        engine = _TileDiffEngine(tile=8, threshold=10)
        segs1 = segment_frames(frames, engine, hi=4, lo=1, quiet=3, min_len=2)
        segs2 = segment_frames(frames, engine, hi=4, lo=1, quiet=3, min_len=2)
        self.assertEqual(segs1, segs2)
        self.assertEqual(dumps(segs1), dumps(segs2))

        expected = [
            {
                "start": 2,
                "end": 6,
                "peak_tiles": 4,
                "total_tiles": 16,
                "bbox": {"x": 8, "y": 8, "w": 32, "h": 32},
            },
            {
                "start": 11,
                "end": 14,
                "peak_tiles": 4,
                "total_tiles": 12,
                "bbox": {"x": 40, "y": 16, "w": 16, "h": 16},
            },
        ]
        self.assertEqual(segs1, expected)

        segs_with_flicker = segment_frames(frames, engine, hi=4, lo=1, quiet=3, min_len=1)
        self.assertEqual(len(segs_with_flicker), 3)
        self.assertEqual(
            segs_with_flicker[2],
            {
                "start": 20,
                "end": 21,
                "peak_tiles": 4,
                "total_tiles": 4,
                "bbox": {"x": 0, "y": 0, "w": 16, "h": 16},
            },
        )

    def test_hysteresis_bridging_and_custom_engine(self):
        class FakeEngine:
            def __init__(self, script):
                self.script = list(script)
                self.idx = 0

            def changed_regions(self, prev, cur):
                regs = self.script[self.idx]
                self.idx += 1
                return regs

        script = [
            [{"x": 0, "y": 0, "w": 16, "h": 24, "tiles": 5}],
            [{"x": 16, "y": 0, "w": 8, "h": 8, "tiles": 1}],
            [{"x": 8, "y": 16, "w": 16, "h": 8, "tiles": 2}],
            [],
            [],
            [],
        ]
        dummy_frames = [make_frame() for _ in range(7)]
        segs = segment_frames(dummy_frames, FakeEngine(script), hi=4, lo=1, quiet=3, min_len=2)
        self.assertEqual(
            segs,
            [
                {
                    "start": 0,
                    "end": 3,
                    "peak_tiles": 5,
                    "total_tiles": 8,
                    "bbox": {"x": 0, "y": 0, "w": 24, "h": 24},
                }
            ],
        )

    def test_run_pipeline_three_segments_reproduced_share_ppm_333333(self):
        frames = build_three_segment_frames()
        engine = _TileDiffEngine(tile=8, threshold=10)
        seen_slices = []

        def fake_labeler(segment, seg_frames):
            seen_slices.append((
                segment["start"],
                segment["end"],
                len(seg_frames),
                seg_frames[0] is frames[segment["start"]],
                seg_frames[-1] is frames[segment["end"]],
            ))
            return "intent-%d-%d" % (segment["start"], segment["end"])

        def fake_mapper(intent):
            if intent == "intent-20-22":
                raise NotImplementedError("mapper cannot map third segment")
            return [{"path": "api", "op": intent}]

        def fake_replayer(actions, segment):
            if segment["start"] == 2:
                return {"reproduced": True, "verified": True}
            return {"reproduced": True, "verified": False}

        report = run_pipeline(frames, engine, fake_labeler, fake_mapper, fake_replayer)
        self.assertEqual(report["reproduced_share_ppm"], 333333)
        self.assertEqual(len(report["segments"]), 3)
        self.assertEqual(
            seen_slices,
            [(2, 6, 5, True, True), (11, 14, 4, True, True), (20, 22, 3, True, True)],
        )

        s0, s1, s2 = report["segments"]
        self.assertEqual((s0["reproduced"], s0["verified"], s0["error"]), (True, True, None))
        self.assertEqual((s1["reproduced"], s1["verified"], s1["error"]), (True, False, None))
        self.assertEqual((s2["reproduced"], s2["verified"], s2["error"]), (False, False, "mapper"))
        self.assertTrue(dumps(report).startswith("{"))

    def test_run_pipeline_default_demonstration_stubs_give_zero(self):
        frames = build_synthetic_30_frames()
        engine = _TileDiffEngine(tile=8, threshold=10)
        report = run_pipeline(
            frames,
            engine,
            demonstrations.label_intent,
            demonstrations.map_to_actions,
            demonstrations.replay_and_verify,
        )
        self.assertEqual(report["reproduced_share_ppm"], 0)
        self.assertEqual(len(report["segments"]), 2)
        for seg in report["segments"]:
            self.assertFalse(seg["reproduced"])
            self.assertFalse(seg["verified"])
            self.assertEqual(seg["error"], "labeler")

        default_report = run_pipeline(frames, engine)
        self.assertEqual(default_report["reproduced_share_ppm"], 0)
        self.assertEqual(default_report, report)

    def test_stage_errors_for_labeler_mapper_and_replayer(self):
        frames = build_three_segment_frames()
        engine = _TileDiffEngine(tile=8, threshold=10)

        def stage_labeler(segment, seg_frames):
            if segment["start"] == 2:
                raise RuntimeError("labeler boom")
            return "ok-%d" % segment["start"]

        def stage_mapper(intent):
            if intent == "ok-11":
                return [{"path": "invalid_path"}]
            return [{"path": "ax", "target": intent}]

        def stage_replayer(actions, segment):
            raise NotImplementedError("replayer stub")

        report = run_pipeline(frames, engine, stage_labeler, stage_mapper, stage_replayer)
        self.assertEqual(report["reproduced_share_ppm"], 0)
        self.assertEqual(report["segments"][0]["error"], "labeler")
        self.assertEqual(report["segments"][1]["error"], "mapper")
        self.assertEqual(report["segments"][2]["error"], "replayer")


if __name__ == "__main__":
    unittest.main()
