"""W10R: immutable, receipt-bound analysis reuse and clean retry regression."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from blender_visual_qa.truth import run_bench as rb


class ReanalysisTests(unittest.TestCase):
    def setUp(self):
        scratch = rb.REPO / "build/room_R"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / "B5"
        (self.run / "render").mkdir(parents=True)
        self.table = rb.vparams.effective(None)
        self.render()

    def render(self, value=0):
        for i in range(1, 4):
            rgb = np.full((16, 32, 3), value, np.uint8)
            rgb[:, :16] = value + (200 if i % 2 else 20)
            (self.run / "render" / ("frame_%04d.png" % i)).write_bytes(rb.decode.encode_png(rgb))

    def snapshot(self, path):
        return {p.relative_to(path).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in path.rglob("*") if p.is_file()}

    def test_three_identical_calls_reuse_one_output(self):
        with mock.patch.object(rb.pipeline, "analyze", wraps=rb.pipeline.analyze) as analyze:
            first = rb.vision_analyze(self.run, self.table)
            original = self.snapshot(self.run / "vision")
            second = rb.vision_analyze(self.run, self.table)
            third = rb.vision_analyze(self.run, self.table)
        self.assertEqual(analyze.call_count, 1)
        self.assertTrue(any(first[0].regions))
        self.assertFalse((self.run / "vision_reanalysis").exists())
        self.assertEqual(self.snapshot(self.run / "vision"), original)
        for result in (second, third):
            self.assertEqual(result[0]._blobs(), first[0]._blobs())
            self.assertEqual(result[0].global_mv, first[0].global_mv)
            self.assertEqual(result[0].meta, first[0].meta)
            self.assertEqual([g.blob for g in result[0].gops_raw], [g.blob for g in first[0].gops_raw])
            self.assertEqual(result[1], first[1])

    def test_fitted_table_has_one_bounded_output_and_reuses_it(self):
        rb.vision_analyze(self.run, self.table)
        initial = self.snapshot(self.run / "vision")
        table = dict(self.table, T0_Q4=self.table["T0_Q4"] + 1)
        with mock.patch.object(rb.pipeline, "analyze", wraps=rb.pipeline.analyze) as analyze:
            runs = [rb.vision_analyze(self.run, table) for _ in range(3)]
        self.assertEqual(analyze.call_count, 1)
        root = self.run / "vision_reanalysis"
        self.assertEqual([p.name for p in root.iterdir()], [rb.vparams.params_sha256(table)])
        self.assertEqual(self.snapshot(self.run / "vision"), initial)
        self.assertEqual(runs[0][0]._blobs(), runs[2][0]._blobs())

    def test_tampered_output_fails_closed_without_writing(self):
        for filename in ("tiles.bin", "vision.json", "analysis.receipt.json"):
            with self.subTest(filename=filename):
                run = self.run / filename.replace(".", "_")
                (run / "render").mkdir(parents=True)
                for png in (self.run / "render").glob("*.png"):
                    (run / "render" / png.name).write_bytes(png.read_bytes())
                rb.vision_analyze(run, self.table)
                target = run / "vision" / filename
                target.write_bytes(b"tampered")
                before = self.snapshot(run / "vision")
                with mock.patch.object(rb.pipeline, "analyze", side_effect=AssertionError("must not recompute")):
                    with self.assertRaises(rb.BenchError) as raised:
                        rb.vision_analyze(run, self.table)
                self.assertEqual(raised.exception.code, "E_VISION_REUSE")
                self.assertEqual(self.snapshot(run / "vision"), before)

    def test_changed_capture_is_not_reused(self):
        rb.vision_analyze(self.run, self.table)
        before = self.snapshot(self.run / "vision")
        self.render(9)
        with self.assertRaises(rb.BenchError) as raised:
            rb.vision_analyze(self.run, self.table)
        self.assertEqual(raised.exception.code, "E_VISION_REUSE")
        self.assertEqual(self.snapshot(self.run / "vision"), before)

    def test_deleted_or_mismatched_receipt_is_refused(self):
        rb.vision_analyze(self.run, self.table)
        path = self.run / "vision/analysis.receipt.json"
        original = path.read_text()
        for field, value in (("manifest_sha256", "0" * 64), ("params_sha256", "0" * 64),
                             ("files", []), ("cpu_ms_per_frame", None)):
            with self.subTest(field=field):
                receipt = json.loads(original)
                receipt[field] = value
                path.write_text(json.dumps(receipt))
                with self.assertRaises(rb.BenchError) as raised:
                    rb.vision_analyze(self.run, self.table)
                self.assertEqual(raised.exception.code, "E_VISION_REUSE")
        path.unlink()
        with self.assertRaises(rb.BenchError) as raised:
            rb.vision_analyze(self.run, self.table)
        self.assertEqual(raised.exception.code, "E_VISION_REUSE")
        self.assertFalse((self.run / "vision_reanalysis").exists())

    def test_missing_receipt_at_reanalysis_target_is_refused(self):
        rb.vision_analyze(self.run, self.table)
        table = dict(self.table, T0_Q4=self.table["T0_Q4"] + 1)
        target = self.run / "vision_reanalysis" / rb.vparams.params_sha256(table)
        target.mkdir(parents=True)
        with self.assertRaises(rb.BenchError) as raised:
            rb.vision_analyze(self.run, table)
        self.assertEqual(raised.exception.code, "E_VISION_REUSE")

    def test_clean_capture_retries_do_not_persist_redundant_analysis(self):
        renders = []

        def render(bench, run, w, h, nframes, spp, which):
            renders.append((spp, which))
            clean = Path(run) / ("clean_%d" % which)
            (clean / "render").mkdir(parents=True, exist_ok=True)
            for i in range(1, nframes + 1):
                rgb = np.full((h, w, 3), which if spp < 4 else 0, np.uint8)
                (clean / "render" / ("frame_%04d.png" % i)).write_bytes(rb.decode.encode_png(rgb))
            return clean

        original_residual = rb.change.residual

        def diff(a, b, hd, *args):
            if args:  # the engine's classification calls carry a fourth argument
                return original_residual(a, b, hd, *args)
            # Force two retry rounds: the third capture is the first accepted pair.
            return np.full(len(a), 100000 if len(renders) < 6 else 0, np.int64)

        with mock.patch.object(rb, "render_clean", side_effect=render), \
                mock.patch.object(rb.change, "residual", side_effect=diff):
            resid, spp = rb.clean_resid(self.run, "B5", self.table, 32, 16, 3, spp0=1)
        self.assertEqual(spp, 4)
        self.assertEqual(resid.shape, (3, 2))
        self.assertEqual(renders, [(1, 0), (1, 1), (2, 0), (2, 1), (4, 0), (4, 1)])
        for which in (0, 1):
            clean = self.run / ("clean_%d" % which)
            self.assertFalse((clean / "vision").exists())
            self.assertFalse((clean / "vision_reanalysis").exists())


if __name__ == "__main__":
    unittest.main()
