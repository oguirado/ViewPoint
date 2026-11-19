# SPDX-License-Identifier: AGPL-3.0-only
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw

from segment.__main__ import LocalSink
from segment.inputs import MANIFEST, DirectoryInputs, Recording, locator
from segment.ocr import OCR
from segment.provider import analyze, segment
from source.records import canonical, digest


class MemoryInputs:
    def __init__(self, images=None):
        self.files = {}
        self.refs = {}
        self.data = {}
        base = {"numerator": 1, "denominator": 100}
        source = digest(b"movie")
        clock = {"video_sha256": source, "time_base": base, "width": 64, "height": 48,
                 "start_pts": -2, "vision_origin_pts": -2, "frame_count": 3,
                 "gaps": [{"kind": "owner_gap", "pts": 1}], "occurrences": []}
        manifest = {"schema": "vqa.frame/v1", "storage": "png_dir", "count": 3,
                    "channels": 3, "pixel_format": "rgb24", "width": 64, "height": 48,
                    "scope": {"app": "video_rea", "scene": "recorded"},
                    "provenance": {"video_sha256": source, "time_base": base, "origin_pts": -2}, "frames": []}
        frames = []
        seen_rgb = {}
        for idx, tick in enumerate((-2, 1, 4)):
            image = Image.new("RGB", (64, 48), (230, 238, 243))
            if images is not None:
                image = images[idx]
            elif idx == 2:
                ImageDraw.Draw(image).rectangle((12, 12, 50, 31), fill=(21, 87, 119))
            stream = BytesIO()
            image.save(stream, format="PNG")
            data, rgb_sha = stream.getvalue(), digest(image.tobytes())
            png_sha = digest(data)
            path = f"artifacts/{png_sha}.png"
            self.files[path] = data
            provenance = {"video_sha256": source, "pts_ms": tick * 10, "frame_index": idx,
                          "frame_sha256": png_sha, "label": "observed"}
            clock["occurrences"].append({"frame_index": idx, "pts": tick, "artifact_ref": locator(png_sha),
                                         "rgb_sha256": rgb_sha, "duplicate_of": seen_rgb.get(rgb_sha)})
            seen_rgb.setdefault(rgb_sha, idx)
            manifest["frames"].append({"i": idx, "t_us": (tick+2)*10000, "file": path,
                                       "sha256": rgb_sha, "file_sha256": png_sha})
            frames.append({"schema": "rea.frame/v1", "frame_format": "png", "width": 64, "height": 48,
                           "provenance": provenance})
        self.put(MANIFEST, manifest)
        self.put("frames.json", frames)
        self.put("clock.json", clock)
        first = frames[0]["provenance"]
        self.put("keyframes.json", {"schema": "rea.keyframe_set/v1", "derived_from": [digest(canonical(frames))],
                                   "evidence": [first], "keyframes": [{"provenance": first}]})

    def put(self, name, value):
        self.data[name] = value
        self.files[name] = canonical(value)
        self.refs[name] = locator(digest(self.files[name]))
        self.files["refs.json"] = canonical(self.refs)

    def change(self, name, modify):
        value = deepcopy(self.data[name])
        modify(value)
        self.put(name, value)

    def read(self, name):
        return self.files[name]


class InputTests(unittest.TestCase):
    def setUp(self):
        self.inputs = MemoryInputs()

    def test_presentation_and_hash_domains(self):
        rows = [w for _, w in Recording(self.inputs.read)]
        self.assertEqual(rows[0]["pts"], {"numerator": -1, "denominator": 50})
        self.assertEqual(rows[1]["duplicate_of"], 0)
        self.assertEqual(rows[1]["png_sha256"], rows[0]["png_sha256"])
        self.assertEqual(rows[1]["rgb_sha256"], rows[0]["rgb_sha256"])
        self.assertEqual(rows[1]["normalized_sha256"], rows[0]["normalized_sha256"])
        self.assertEqual(len({rows[0][k] for k in ("png_sha256", "rgb_sha256", "normalized_sha256")}), 3)
        self.assertTrue(rows[0]["keyframe"])
        self.assertFalse(rows[1]["keyframe"])

    def test_admission_rejects_inconsistent_rows(self):
        cases = (
            (MANIFEST, lambda r: r.update(storage="rgb24_raw"), "png_dir"),
            ("clock.json", lambda r: r.update(width=63), "recording identity"),
            ("clock.json", lambda r: r["time_base"].update(denominator=0), "time base"),
            ("clock.json", lambda r: r.update(vision_origin_pts=0), "presentation clock"),
            ("clock.json", lambda r: r["occurrences"].pop(), "frame join"),
            ("clock.json", lambda r: r.update(start_pts=-3), "starting PTS"),
            ("clock.json", lambda r: r["occurrences"][1].update(pts=2), "occurrence or PTS"),
            ("clock.json", lambda r: r["occurrences"][1].update(duplicate_of=None), "duplicate lineage"),
            ("keyframes.json", lambda r: r["derived_from"].clear(), "keyframe lineage"),
            ("keyframes.json", lambda r: r["evidence"].clear(), "keyframe evidence"),
            (MANIFEST, lambda r: r["frames"][0].update(file="../outside.png"), "PNG locator"),
        )
        for name, change, error in cases:
            with self.subTest(name=name, error=error):
                inputs = MemoryInputs()
                inputs.change(name, change)
                with self.assertRaisesRegex(ValueError, error):
                    list(Recording(inputs.read))

    def test_record_and_keyframe_occurrence_rejected(self):
        def bad_keyframe(meta):
            meta["evidence"][0].update(frame_index=1)
            meta["keyframes"][0]["provenance"].update(frame_index=1)

        with self.subTest("keyframe"):
            self.inputs.change("keyframes.json", bad_keyframe)
            with self.assertRaisesRegex(ValueError, "keyframe occurrence"):
                Recording(self.inputs.read)
        with self.subTest("frame record"):
            inputs = MemoryInputs()
            inputs.change("frames.json", lambda r: r[1].update(frame_format="jpeg"))
            inputs.change("keyframes.json", lambda r: r.update(derived_from=[digest(inputs.files["frames.json"])]))
            with self.assertRaisesRegex(ValueError, "record mismatch"):
                list(Recording(inputs.read))

    def test_unpinned_and_unbounded_bytes_rejected(self):
        self.inputs.files["clock.json"] += b" "
        with self.assertRaisesRegex(ValueError, "sidecar pin"):
            Recording(self.inputs.read)
        with self.assertRaisesRegex(ValueError, "invalid admitted input bytes"):
            Recording(lambda _: b"")

    def test_disk_reader_rejects_unadmitted_names_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / MANIFEST
            target = root / "unadmitted.json"
            target.write_bytes(b"{}")
            file.symlink_to(target)
            reader = DirectoryInputs(file)
            with self.assertRaises(OSError):
                reader.read(MANIFEST)
            with self.assertRaisesRegex(ValueError, "W1 input name"):
                reader.read("../outside")
            with self.assertRaisesRegex(ValueError, "expected frames"):
                DirectoryInputs(root / "missing")
            file.unlink()
            file.mkdir()
            with self.assertRaisesRegex(ValueError, "regular file"):
                reader.read(MANIFEST)

    def test_png_file_integrity_before_any_publication(self):
        path = self.inputs.data[MANIFEST]["frames"][-1]["file"]
        self.inputs.files[path] += b"corrupt"
        sink = Mock()
        with self.assertRaisesRegex(ValueError, "PNG file pin"):
            segment({"op": "rea.segment"}, self.inputs, sink)
        sink.write.assert_not_called()

    def test_png_decoded_format_and_rgb_pin(self):
        for label, mutate in (
            ("PNG format or extent", Image.new("RGBA", (64, 48), (21, 87, 119, 255))),
            ("raw RGB pin", Image.new("RGB", (64, 48), (10, 20, 30))),
        ):
            inputs = MemoryInputs()
            data = BytesIO()
            mutate.save(data, format="PNG")
            hashed = digest(data.getvalue())
            path = f"artifacts/{hashed}.png"
            inputs.files[path] = data.getvalue()
            inputs.change(MANIFEST, lambda m: m["frames"][2].update(file_sha256=hashed, file=path))
            inputs.change("clock.json", lambda c: c["occurrences"][2].update(artifact_ref=locator(hashed)))
            inputs.change("frames.json", lambda fs: fs[2]["provenance"].update(frame_sha256=hashed))
            inputs.change("keyframes.json", lambda k: k.update(derived_from=[digest(inputs.files["frames.json"])]))
            with self.subTest(label), self.assertRaisesRegex(ValueError, label):
                list(Recording(inputs.read))

    def test_selector_and_dry_run_have_zero_reads_and_writes(self):
        read, sink = Mock(), Mock()
        for options in ({"op": "rea.sample"}, {"op": "rea.segment", "dry_run": True}):
            with self.assertRaises(ValueError):
                segment(options, read, sink)
        read.read.assert_not_called()
        sink.write.assert_not_called()

    def test_local_sink_refuses_existing_and_cleans_failed_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "out"
            sink = LocalSink(dest)
            self.assertEqual(sink.write("segmentation.json", b"result"), "segmentation.json")
            with self.assertRaises(FileExistsError):
                sink.write("segmentation.json", b"replacement")
            self.assertEqual((dest / "segmentation.json").read_bytes(), b"result")
            failed = Path(directory) / "failed"
            with patch("pathlib.Path.replace", side_effect=OSError("test failure")):
                with self.assertRaisesRegex(OSError, "test failure"):
                    LocalSink(failed).write("segmentation.json", b"unpublished")
            self.assertFalse(failed.exists())

    def test_provider_receipt_and_owner_gap(self):
        sink = Mock()
        sink.write.return_value = "records/segment.json"
        with patch("segment.provider.open_ocr", return_value=_missing_ocr()):
            result = segment({"op": "rea.segment"}, self.inputs, sink)
        name, data = sink.write.call_args.args
        document = json.loads(data)
        self.assertEqual(name, "segmentation.json")
        self.assertEqual(result["receipt"], {"path": "records/segment.json", "sha256": digest(data)})
        self.assertEqual(result["artifact_refs"][0]["locator"], locator(digest(data)))
        self.assertTrue(document["frames"][2]["change"]["boundary"])
        self.assertTrue(document["frames"][2]["change"]["owner_gap"])
        self.assertFalse(document["boundaries"])

    def test_missing_and_corrupt_english_keep_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = analyze(self.inputs, directory)
            self.assertEqual(baseline["ocr"]["reason"], "tessdata_absent")
            rows = baseline["frames"][2]["elements"]
            self.assertTrue(rows)
            self.assertTrue(all(r["record"]["bbox"] and r["record"]["text"] is None
                                and r["ocr_reason"] == "tessdata_absent" for r in rows))
            self.assertEqual(baseline["frames"][2]["screen"]["degraded_reasons"], ["tessdata_absent"])
            Path(directory, "eng.traineddata").write_bytes(b"wrong pinned model")
            engine = OCR(Path(directory))
            self.assertEqual(engine.reason, "tessdata_pin_mismatch")
            self.assertIsNone(engine.api)


def _missing_ocr():
    from contextlib import nullcontext

    engine = Mock(reason="tessdata_absent", engine=None, calls=0, hits=0, timeouts=0)
    engine.read.return_value = (None, "tessdata_absent")
    return nullcontext(engine)
