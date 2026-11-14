"""G M3 1 / G M3 5: real sealed-core, offline, RAM-only multi-target parity tests.

No backend, X server, Godot, network, worker process, or on-disk frame fixture.
"""
import hashlib
import unittest
from dataclasses import dataclass, fields, FrozenInstanceError
from pathlib import Path
from unittest import mock

import numpy as np

from visioncore import frame as core_frame, pipeline
from vadapt import multi_describe as md


def _frames(seed, size=(48, 64), count=3):
    h, w = size
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    return (base.copy(), base.copy(), np.bitwise_xor(base, np.uint8(129)))[:count]


def _window(name, seed, *, kind="cdp_tab", generation=0, epoch=0,
            size=(48, 64), seqs=(10, 11, 12), dpr_ppm=1_000_000):
    return md.WindowFrames(md.Handle(kind, name, generation), epoch,
                           _frames(seed, size, len(seqs)), tuple(seqs), dpr_ppm)


def _serial(frames, seqs, params=None):
    """Oracle is the actual sealed core, not a test-local implementation."""
    h, w, _ = frames[0].shape
    owned = [np.array(arr, copy=True, order="C") for arr in frames]
    source = core_frame.MemoryFrameSource([
        core_frame.Frame(i=i, t_us=i, render_index=seq, rgb=rgb)
        for i, (seq, rgb) in enumerate(zip(seqs, owned))
    ], {"width": w, "height": h, "count": len(frames),
        "channels": 3, "pixel_format": "rgb24", "storage": "rgb24_raw"})
    run = pipeline.analyze(source, params=params, out_dir=None, workers=1, cpus=[])
    return (tuple(t.tobytes() for t in run.tiles),
            tuple(tuple(r.copy() for r in frame_regions) for frame_regions in run.regions))


class ReverseCompletionRunner:
    def __init__(self):
        self.calls = 0
        self.snapshot_writable = []
        self.submitted = ()

    def map(self, fn, items):
        self.calls += 1
        self.submitted = tuple(items)
        self.snapshot_writable = [arr.flags.writeable
                                  for job in self.submitted for entry in job.entries
                                  for arr in entry.frames]
        return reversed([fn(item) for item in self.submitted])


class TestMultiDescribe(unittest.TestCase):
    def assert_serial(self, source, actual, params=None):
        self.assertIsNone(actual.error)
        self.assertIs(actual.handle, source.handle)
        self.assertEqual(actual.epoch, source.epoch)
        self.assertEqual(actual.frame_seqs, source.frame_seqs)
        expected_tiles, expected_regions = _serial(source.frames, source.frame_seqs, params)
        self.assertEqual(actual.descriptors, expected_tiles)
        self.assertEqual(actual.regions, expected_regions)
        self.assertEqual(len(actual.descriptors), len(source.frame_seqs))
        self.assertTrue(all(isinstance(t, bytes) and t for t in actual.descriptors))

    def test_public_canonical_frozen_field_order(self):
        self.assertEqual([f.name for f in fields(md.Handle)],
                         ["kind", "canonical_id", "generation"])
        self.assertEqual([f.name for f in fields(md.WindowFrames)],
                         ["handle", "epoch", "frames", "frame_seqs", "dpr_ppm"])
        self.assertEqual([f.name for f in fields(md.WindowDescription)],
                         ["handle", "epoch", "frame_seqs", "descriptors", "regions", "error"])
        with self.assertRaises(FrozenInstanceError):
            md.Handle("cdp_tab", "test", 0).generation = 1

    def test_m3_1_reversed_completions_keep_identity_and_serial_bytes(self):
        items = [_window("A", 1), _window("B", 2),
                 _window("C", 3, kind="godot_window", size=(64, 80), dpr_ppm=1_250_000)]
        runner = ReverseCompletionRunner()
        results = md.describe_windows(items, workers=3, runner=runner, out_dir=None)
        self.assertEqual(len(results), len(items))
        self.assertEqual(runner.calls, 1)
        self.assertEqual(runner.snapshot_writable, [False] * 9)
        self.assertEqual(len(runner.submitted), 3)
        for src, got in zip(items, results):
            self.assert_serial(src, got)
        # Detect a mutant that simply enumerates reverse completion order.
        self.assertNotEqual(results[0].descriptors, results[1].descriptors)

    def test_m3_5_same_dimensions_never_share_unrelated_predecessor(self):
        a = _window("A", 11, kind="cdp_tab")
        b = _window("B", 21, kind="xvfb_screen")
        results = md.describe_windows((a, b), workers=2)
        self.assert_serial(a, results[0])
        self.assert_serial(b, results[1])
        # This control proves the discriminant: concatenation by shape changes
        # B's first frame history and would not match its actual serial history.
        # The sealed core requires strictly increasing frame_nums for the
        # *combined* oracle. Rebase only A into the slots preceding B: B keeps
        # its original numbers, so its descriptor cannot differ just because
        # its own frame identity was renumbered.
        prefix_nums = tuple(range(b.frame_seqs[0] - len(a.frames), b.frame_seqs[0]))
        combined_nums = prefix_nums + b.frame_seqs
        self.assertTrue(all(prev < cur for prev, cur in zip(combined_nums, combined_nums[1:])))
        self.assertEqual(combined_nums[len(a.frames):], b.frame_seqs)
        contaminated, _ = _serial(a.frames + b.frames, combined_nums)
        self.assertNotEqual(contaminated[len(a.frames)], results[1].descriptors[0])

    def test_m3_5_generation_and_geometry_epoch_isolation(self):
        a = _window("tab-one", 5, generation=1, epoch=0)
        b = _window("tab-one", 7, generation=2, epoch=0)
        c = _window("tab-one", 9, generation=2, epoch=1)
        for workers in (1, 2, 3, 8):
            outputs = md.describe_windows((a, b, c), workers=workers)
            for original, output in zip((a, b, c), outputs):
                self.assert_serial(original, output)

    def test_m3_5_same_stream_slices_share_only_own_predecessor(self):
        a = _window("split", 31, seqs=(10, 11, 12))
        # Two chronological slices supplied out of submission order.
        first = md.WindowFrames(a.handle, a.epoch, a.frames[:2], a.frame_seqs[:2], a.dpr_ppm)
        last = md.WindowFrames(a.handle, a.epoch, a.frames[2:], a.frame_seqs[2:], a.dpr_ppm)
        foreign = _window("foreign", 8)
        output = md.describe_windows((last, foreign, first), workers=2,
                                     runner=ReverseCompletionRunner())
        tiles, regions = _serial(a.frames, a.frame_seqs)
        self.assertEqual(output[0].descriptors, tiles[2:])
        self.assertEqual(output[0].regions, regions[2:])
        self.assertEqual(output[2].descriptors, tiles[:2])
        self.assertEqual(output[2].regions, regions[:2])
        self.assert_serial(foreign, output[1])

    def test_m3_1_invalid_inputs_are_isolated_before_runner_admission(self):
        good = _window("good", 2)
        bad_dtype = _window("dtype", 3)
        bad_dtype = md.WindowFrames(bad_dtype.handle, bad_dtype.epoch,
                                    (bad_dtype.frames[0].astype(np.float32),), (10,), 1_000_000)
        bad_dpr = _window("dpr", 4, dpr_ppm=0)
        non_contiguous = _window("stride", 5)
        non_contiguous = md.WindowFrames(non_contiguous.handle, non_contiguous.epoch,
                                       (non_contiguous.frames[0][:, ::2, :],), (10,), 1_000_000)
        runner = ReverseCompletionRunner()
        out = md.describe_windows((bad_dtype, good, bad_dpr, non_contiguous),
                                  workers=4, runner=runner)
        self.assertEqual(len(runner.submitted), 1)
        self.assertEqual(runner.calls, 1)
        self.assert_serial(good, out[1])
        self.assertTrue(out[0].error.startswith("E_M3_FRAME_SCHEMA"))
        self.assertTrue(out[2].error.startswith("E_M3_SCHEMA"))
        self.assertTrue(out[3].error.startswith("E_M3_FRAME_SCHEMA"))
        for i in (0, 2, 3):
            self.assertEqual(out[i].descriptors, ())
            self.assertEqual(out[i].regions, ())
            self.assertIs(out[i].handle, (bad_dtype, good, bad_dpr, non_contiguous)[i].handle)

    def test_frame_ref_structural_compatibility_and_guards(self):
        @dataclass(frozen=True)
        class ForeignHandle:
            kind: str
            canonical_id: str
            generation: int

        @dataclass(frozen=True)
        class ForeignFrameRef:
            handle: object
            epoch: int
            frame_seq: int
            width: int
            height: int
            rgb: object
            digest: str

        x = _window("frame-ref", 51, seqs=(70,))
        rgb = x.frames[0]
        foreign = ForeignHandle("cdp_tab", "frame-ref", 0)
        ref = ForeignFrameRef(foreign, 0, 70, rgb.shape[1], rgb.shape[0], rgb,
                              hashlib.sha256(rgb.tobytes()).hexdigest())
        item = md.WindowFrames(x.handle, 0, (ref,), (70,), x.dpr_ppm)
        self.assert_serial(x, md.describe_windows((item,))[0])
        wrong = ForeignFrameRef(foreign, 1, 70, rgb.shape[1], rgb.shape[0], rgb,
                                ref.digest)
        out = md.describe_windows((md.WindowFrames(x.handle, 0, (wrong,), (70,), x.dpr_ppm),))
        self.assertTrue(out[0].error.startswith("E_M3_FRAME_IDENTITY"))
        damaged = ForeignFrameRef(foreign, 0, 70, rgb.shape[1], rgb.shape[0], rgb,
                                  "0" * 64)
        out = md.describe_windows((md.WindowFrames(x.handle, 0, (damaged,), (70,), x.dpr_ppm),))
        self.assertTrue(out[0].error.startswith("E_M3_DIGEST"))

    def test_m3_1_own_readonly_snapshot_before_injected_runner_executes(self):
        original = _window("snapshot", 42)
        before = tuple(f.copy() for f in original.frames)

        class MutatingRunner:
            def map(self, fn, jobs):
                # Mutate the caller's *mutable* ring buffers after admission.
                for f in original.frames:
                    f[:] = 0
                return [fn(job) for job in jobs]

        out = md.describe_windows((original,), workers=2, runner=MutatingRunner())
        oracle = _serial(before, original.frame_seqs)
        self.assertEqual(out[0].descriptors, oracle[0])
        self.assertEqual(out[0].regions, oracle[1])
        self.assertTrue(all((f == 0).all() for f in original.frames))

    def test_runner_death_retains_completed_without_dispatch_retries(self):
        items = (_window("A", 1), _window("B", 2), _window("C", 3))

        class PartiallyDeadRunner:
            count = 0
            def map(self, fn, batches):
                for batch in batches:
                    self.count += 1
                    yield fn(batch)
                    raise RuntimeError("simulated dead runner")

        runner = PartiallyDeadRunner()
        out = md.describe_windows(items, workers=3, runner=runner)
        self.assertEqual(runner.count, 1)
        self.assert_serial(items[0], out[0])
        self.assertTrue(all(r.error.startswith("E_M3_RUNNER_DIED") for r in out[1:]))
        self.assertEqual(out[1].descriptors, ())

    def test_core_failure_isolated_by_target(self):
        small, large = _window("small", 17), _window("large", 19, size=(64, 80))
        real_analyze = pipeline.analyze
        def fail_one(source, **kwargs):
            if source.meta["width"] == 80:
                raise RuntimeError("fake single target failure")
            return real_analyze(source, **kwargs)

        with mock.patch.object(md.pipeline, "analyze", side_effect=fail_one):
            out = md.describe_windows((small, large), workers=2)
        self.assert_serial(small, out[0])
        self.assertTrue(out[1].error.startswith("E_M3_CORE_FAILED"))
        self.assertEqual(out[1].descriptors, ())

    def test_geometry_conflict_sequence_overlap_and_bad_workers_refused(self):
        a = _window("geometry", 6)
        b = _window("geometry", 8, size=(64, 80))
        out = md.describe_windows((a, b))
        self.assertTrue(all(r.error.startswith("E_M3_GEOMETRY_EPOCH") for r in out))
        c = md.WindowFrames(a.handle, a.epoch, a.frames[:2], (10, 11), a.dpr_ppm)
        d = md.WindowFrames(a.handle, a.epoch, a.frames[1:], (11, 12), a.dpr_ppm)
        out = md.describe_windows((c, d))
        self.assertTrue(all(r.error.startswith("E_M3_SEQUENCE_OVERLAP") for r in out))
        for workers in (0, -1, True, 1.5, "2"):
            with self.subTest(workers=workers), self.assertRaises(md.WindowInputError):
                md.describe_windows((a,), workers=workers)
        with self.assertRaises(md.WindowInputError):
            md.describe_windows((a,), out_dir="forbidden")

    def test_zero_disk_activity_when_out_dir_none(self):
        a = _window("ram-only", 100)
        with mock.patch("builtins.open", side_effect=AssertionError("disk I/O")), \
             mock.patch.object(Path, "write_bytes", side_effect=AssertionError("disk I/O")), \
             mock.patch.object(Path, "mkdir", side_effect=AssertionError("disk I/O")):
            self.assert_serial(a, md.describe_windows((a,), out_dir=None)[0])

    def test_worker_count_parity_and_repeatable_bytes(self):
        items = (_window("one", 5), _window("two", 6, kind="x11_window"),
                 _window("three", 7, kind="capsule", size=(64, 80)))
        serial = md.describe_windows(items, workers=1)
        for w in (2, 3, 8, 64):
            got = md.describe_windows(items, workers=w)
            self.assertEqual([(x.handle, x.epoch, x.frame_seqs, x.descriptors, x.regions, x.error)
                              for x in serial],
                             [(x.handle, x.epoch, x.frame_seqs, x.descriptors, x.regions, x.error)
                              for x in got])
        self.assertEqual(md.describe_windows([], workers=4), [])


if __name__ == "__main__":
    unittest.main()
