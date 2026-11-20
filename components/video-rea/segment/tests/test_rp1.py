# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
from PIL import Image, ImageDraw

from segment.__main__ import LocalSink
from segment.geometry import Detection, area, detect, overlap
from segment.ocr import OCR, OCRProcess, TESSDATA
from segment.provider import analyze, segment
from segment.temporal import compare, without_overlays
from test_inputs import MemoryInputs


class GeneralizationTests(unittest.TestCase):
    def test_small_control_and_fill_toggle_are_boundary_evidence(self):
        before = np.full((100, 150, 3), 220, np.uint8)
        after = before.copy()
        after[25:38, 22:41] = (70, 40, 100)
        row = Detection("menu", (22, 25, 19, 13), "foreground")
        self.assertTrue(compare(before, after, [], [row])["boundary"])
        first = Detection("button", (22, 25, 19, 13), "foreground", fill=(70, 40, 100))
        last = Detection("button", first.bbox, "foreground", fill=(90, 90, 90))
        toggled = after.copy()
        toggled[25:38, 22:41] = last.fill
        self.assertIn("fill_state", compare(after, toggled, [first], [last])["change_kinds"])

    def test_timeout_reaps_exact_child_and_preserves_failure_reason(self):
        worker = OCRProcess(TESSDATA)
        try:
            self.assertIsNone(worker.exchange(3))
            with self.assertRaises(TimeoutError):
                worker.exchange(0.01)
        finally:
            worker.close()
        self.assertIsNotNone(worker.child.returncode)
        self.assertTrue(worker.child.stdin.closed and worker.child.stdout.closed)

        image = np.array([[0, 255], [255, 0]], np.uint8)
        row = Detection("text", (0, 0, 2, 2), "foreground", (0, 0, 2, 2))
        engine = OCR(TESSDATA)
        child = engine.api
        with patch.object(child, "exchange", side_effect=TimeoutError):
            self.assertEqual(engine.read(np.repeat(image[..., None], 3, axis=2), row),
                             (None, "ocr_timeout"))
        self.assertEqual(engine.timeouts, 1)
        self.assertIsNotNone(child.child.returncode)
        self.assertIsNone(engine.api)

    def test_worker_thread_cap_is_internal(self):
        # Popen receives the capped environment even when the caller requests more threads.
        from segment import ocr
        real_popen = ocr.subprocess.Popen
        with patch.object(ocr.subprocess, "Popen", wraps=real_popen) as popen:
            worker = OCRProcess(TESSDATA)
            try:
                self.assertIsNone(worker.exchange(3))
            finally:
                worker.close()
        env = popen.call_args.kwargs["env"]
        self.assertEqual({env[k] for k in ("OMP_THREAD_LIMIT", "OMP_NUM_THREADS",
                                           "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")}, {"1"})

    def test_worker_rejects_unbounded_reply(self):
        worker = OCRProcess(TESSDATA)
        try:
            with patch.object(worker, "transfer", return_value=b"\xff\xff\xff\xff"):
                with self.assertRaisesRegex(ValueError, "response bounds"):
                    worker.exchange(1)
        finally:
            worker.close()


    def test_parent_fragments_follow_observed_scrim(self):
        before = np.full((160, 240, 3), (205, 184, 166), np.uint8)
        before[85:110, 12:210] = (87, 39, 99)
        after = (before.astype(np.uint16) * 63 // 100).astype(np.uint8)
        after[42:132, 78:165] = (225, 212, 205)
        after[46:128, 82:161] = (239, 228, 216)
        rows = detect(after, previous=before, prior=detect(before))
        dialog, = [d for d in rows if d.kind == "dialog"]
        clipped, = [d for d in rows if d.layer == "parent" and d.kind == "button"]
        self.assertEqual(len(clipped.visible), 2)
        self.assertFalse(any(area(overlap(region, dialog.bbox)) for region in clipped.visible))

    def test_wide_dark_field_with_light_ink(self):
        rgb = np.full((200, 240, 3), (45, 53, 62), np.uint8)
        rgb[80:103, 15:225] = (130, 122, 112)
        rgb[82:101, 17:223] = (26, 43, 48)
        rgb[87:96, 25:29] = (221, 230, 226)
        rgb[87:96, 33:39] = (221, 230, 226)
        self.assertIn("text_field", {d.kind for d in detect(rgb)})

    def test_modal_recovers_parent_from_better_backdrop(self):
        base = np.full((160, 240, 3), (205, 184, 166), np.uint8)
        base[88:108, 12:64] = (87, 39, 99)
        obscured = base.copy()
        obscured[66:120, 6:80] = (99, 69, 54)
        current = (base.astype(np.uint16)*63//100).astype(np.uint8)
        current[40:130, 100:205] = (237, 225, 207)
        rows = detect(current, obscured, detect(obscured), alternative=(base, detect(base)))
        self.assertTrue(any(row.layer == "parent" and row.bbox == (12, 88, 52, 20)
                            for row in rows))

    def test_large_light_field_without_scrim(self):
        rgb = np.full((160, 240, 3), (203, 190, 175), np.uint8)
        rgb[25:119, 20:180] = (152, 103, 87)
        rgb[27:117, 22:178] = (252, 248, 247)
        rows = detect(rgb)
        self.assertIn("text_field", {d.kind for d in rows})
        self.assertNotIn("dialog", {d.kind for d in rows})

    def test_local_text_and_caret_change(self):
        before = np.full((100, 150, 3), 220, np.uint8)
        after = before.copy()
        after[25:37, 22:23] = 10
        after[26:38, 28:34] = 10
        after[26:38, 39:45] = 10
        prior = [Detection("text", (28, 26, 17, 12), "foreground", (28, 26, 17, 12))]
        self.assertTrue(compare(before, after, [], prior)["boundary"])
        caret = before.copy()
        caret[25:37, 22:23] = 10
        self.assertFalse(compare(before, caret, [], [])["boundary"])

    def test_descriptor_return_suppresses_short_flicker(self):
        before = np.full((100, 150, 3), 220, np.uint8)
        after = before.copy()
        after[25:39, 22:40] = 10
        row = Detection("button", (22, 25, 18, 14), "foreground")
        self.assertTrue(compare(before, after, [], [row])["boundary"])
        self.assertFalse(compare(before, after, [], [row], transient_return=True)["boundary"])

    def test_provider_suppresses_exact_transient_return(self):
        original = Image.new("RGB", (64, 48), (231, 226, 218))
        flash = original.copy()
        ImageDraw.Draw(flash).rectangle((12, 12, 24, 20), fill=(63, 45, 88))
        with tempfile.TemporaryDirectory() as directory:
            inputs = MemoryInputs([original, flash, original])
            inputs.change("clock.json", lambda clock: clock.update(gaps=[]))
            document = analyze(inputs, directory)
            self.assertTrue(document["frames"][1]["change"]["transient_return"])
            self.assertTrue(document["frames"][2]["change"]["descriptor_return"])
            self.assertEqual(document["frames"][2]["change"]["exact_prior_occurrence"], 0)
            self.assertEqual(document["boundaries"], [])

            inputs = MemoryInputs([original, flash, flash])
            inputs.change("clock.json", lambda clock: clock.update(gaps=[]))
            document = analyze(inputs, directory)
            boundary, = document["boundaries"]
            self.assertEqual(boundary["after"]["occurrence"], 1)

    def test_hover_rim_reclassified_as_text_is_not_a_boundary(self):
        before = np.full((120, 180, 3), 220, np.uint8)
        before[30:56, 20:90] = 70
        after = before.copy()
        cv2.rectangle(after, (20, 30), (89, 55), (134, 208, 193), 3)
        box = (20, 30, 70, 26)
        old = Detection("menu_item", box, "foreground", (29, 39, 38, 12))
        stray = Detection("text", box, "foreground", box)
        self.assertFalse(compare(before, after, [old], [stray])["boundary"])

    def test_ring_does_not_mask_disjoint_local_glyph(self):
        residual = np.zeros((80, 80), np.uint8)
        cv2.circle(residual, (30, 30), 10, 255, 2)
        residual[24:32, 46:54] = 255
        evidence = without_overlays(residual)
        self.assertEqual(evidence[20, 30], 0)
        self.assertEqual(evidence[26, 48], 255)

    def test_provider_override_and_argument_guards(self):
        inputs, sink = MemoryInputs(), Mock()
        sink.write.return_value = "segmentation.json"
        with tempfile.TemporaryDirectory() as directory:
            segment({"op": "rea.segment", "tessdata_dir": directory}, inputs, sink)
        record = json.loads(sink.write.call_args.args[1])
        self.assertEqual(record["ocr"]["reason"], "tessdata_absent")
        self.assertIn("schema", record)
        for args in (None, {"op": "rea.segment", "unknown": True},
                     {"op": "rea.segment", "tessdata_dir": 7}):
            with self.assertRaises(ValueError):
                segment(args, Mock(), Mock())

    def test_local_sink_rejects_non_basename(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "sink"
            for invalid in ("../escape", "/absolute", ".", "..", ""):
                with self.assertRaises(ValueError):
                    LocalSink(destination).write(invalid, b"data")
            self.assertFalse(destination.exists())
