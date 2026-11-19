# SPDX-License-Identifier: AGPL-3.0-only
from hashlib import sha256
from contextlib import nullcontext
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from source import ingest_timeline, ingest_timeline_files, media, timeline
from source.__main__ import main
from source.records import canonical

DIRECT = Path(__file__).resolve().parents[3] / "vp-direct"
sys.path.insert(0, str(DIRECT))
from data.frames import FileFrameSequence


class TimelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.out = self.root / "admitted"
        binding = json.loads((DIRECT / "schemas/tests/examples/vp.surface_binding_v1.valid.json").read_bytes())
        self.owner = binding["owner"]
        self.seq = FileFrameSequence(binding, directory=temp.name, slots=4, max_frame_bytes=4096)
        self.addCleanup(self.seq.close, self.owner)
        self.events, self.paths = [], []
        self.seq.append(self.owner, b"earlier slot")
        for i, (stamp, color) in enumerate([(1000000, "red"), (1000000, "red"), (99000000, "blue")]):
            buf = BytesIO()
            Image.new("RGB", (32, 24), color).save(buf, format="PNG")
            raw = buf.getvalue()
            info = self.seq.append(self.owner, raw)
            path = self.root / f"owner-{i}.png"
            path.write_bytes(raw)
            self.paths.append(path)
            self.events.append({"mono_ns": stamp, "capsule_id": "capsule", "incarnation": "instance",
                                "source": "cdp", "op": "observe", "frame": {
                                    "width": 32, "height": 24, "frame_kind": "cdp_png",
                                    "captured_mono_ns": stamp, "frame_sha256": info.sha256,
                                    "ram_ref": "ram:slot", "witness": {
                                        "cdp_target_id": "target", "ax_sha256": "a" * 64}}})
        self.events[2:2] = [self.gap(2000000, "start"), self.gap(90000000, "end")]

    def gap(self, t, edge, reason="privacy"):
        return {"mono_ns": t, "capsule_id": "capsule", "incarnation": "instance",
                "source": "channel", "op": f"{reason}_gap_{edge}", "gap": True}

    def encode(self):
        data = canonical({"schema": "rec.timeline/v1", "events": self.events})
        return data, sha256(data).hexdigest()

    def import_sequence(self, **kwargs):
        data, digest = self.encode()
        ingest_timeline(data, self.out, expected_sha256=digest, owner=self.owner, first_index=1,
                        sequence=self.seq, **kwargs)

    def test_owner_sequence_and_cli_export_match(self):
        self.import_sequence()
        clock = json.loads((self.out / "clock.json").read_bytes())
        self.assertEqual([g["kind"] for g in clock["gaps"]].count("owner_gap"), 2)
        self.assertIn("repeated_pts", [g["kind"] for g in clock["gaps"]])
        self.assertEqual([r["pts"] for r in clock["occurrences"]], [1000000, 1000000, 99000000])
        self.assertEqual(self.seq.status(self.owner).outstanding_borrows, 0)
        data, digest = self.encode()
        path = self.root / "timeline.json"
        path.write_bytes(data)
        self.assertEqual(main([str(path), "--out", str(self.out), "--resume", "--timeline-sha256",
                               digest, "--frame-files", *map(str, self.paths)]), 0)

    def test_pinned_owner_bytes_checked_and_borrow_released(self):
        self.events[-1]["frame"]["frame_sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "owner frame hash"):
            self.import_sequence()
        self.assertEqual(self.seq.status(self.owner).outstanding_borrows, 0)
        self.assertFalse(self.out.exists())

    def test_held_capture_fails_before_borrow(self):
        self.events[-1]["frame"]["captured_mono_ns"] = 3000000
        data, digest = self.encode()
        with self.assertRaisesRegex(ValueError, "hold"):
            ingest_timeline(data, self.out, expected_sha256=digest, owner=self.owner,
                            sequence=None)
        self.assertFalse(self.out.exists())

    def test_overlapping_and_abutting_holds(self):
        self.events[2:4] = [
            self.gap(2000000, "start"),
            self.gap(3000000, "start", "sleep"),
            self.gap(4000000, "end"),
            self.gap(5000000, "end", "sleep"),
            self.gap(5000000, "start", "human"),
            self.gap(90000000, "end", "human"),
        ]
        frame = self.events[-1]["frame"]
        for captured in (2000000, 3500000, 4500000, 5000000, 89999999):
            with self.subTest(captured=captured):
                frame["captured_mono_ns"] = captured
                with self.assertRaisesRegex(ValueError, "frame intersects owner hold"):
                    self.import_sequence()
                self.assertFalse(self.out.exists())
        frame["captured_mono_ns"] = 90000000
        self.import_sequence()
        self.assertEqual(self.seq.status(self.owner).outstanding_borrows, 0)

    def test_unresolved_hold_fails_before_borrow(self):
        self.events = self.events[:3]
        data, digest = self.encode()
        with self.assertRaisesRegex(ValueError, "hold"):
            ingest_timeline(data, self.out, expected_sha256=digest, owner=self.owner, sequence=None)

    def test_incorrect_seal_fails_before_borrow(self):
        data, _ = self.encode()
        with self.assertRaisesRegex(ValueError, "attested digest"):
            ingest_timeline(data, self.out, expected_sha256="0" * 64, owner=self.owner,
                            sequence=None)

    def test_owner_frame_count_is_bounded(self):
        with patch.object(media, "MAX_FRAMES", 2):
            with self.assertRaisesRegex(ValueError, "^FRAME_COUNT_LIMIT:"):
                self.import_sequence()
        self.assertFalse(self.out.exists())

    def test_owner_byte_limits_are_stable(self):
        data, digest = self.encode()
        timeline_path = self.root / "sealed.json"
        timeline_path.write_bytes(data)
        with patch.object(timeline, "MAX_TIMELINE_BYTES", len(data) - 1):
            with self.assertRaisesRegex(ValueError, "^INPUT_BYTES_LIMIT:"):
                self.import_sequence()
            with self.assertRaisesRegex(ValueError, "^INPUT_BYTES_LIMIT: sealed artifact"):
                ingest_timeline_files(timeline_path, self.out, expected_sha256=digest, files=self.paths)
        track, descriptors, _ = timeline.prepare(data, digest)
        with patch.object(timeline, "MAX_OWNER_PNG_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "^FRAME_BYTES_LIMIT: owner slot"):
                self.import_sequence()
            with self.assertRaisesRegex(ValueError, "^FRAME_BYTES_LIMIT: owner PNG"):
                timeline.decode_sequence(track, descriptors, lambda i: nullcontext(self.paths[i].read_bytes()),
                                         lambda i, raw: self.fail("oversized PNG was admitted"))
            with self.assertRaisesRegex(ValueError, "^FRAME_BYTES_LIMIT: sealed artifact"):
                ingest_timeline_files(timeline_path, self.out, expected_sha256=digest, files=self.paths)
        self.assertFalse(self.out.exists())

    def test_capsule_identity_and_epoch(self):
        self.events[-1]["incarnation"] = "another"
        with self.assertRaisesRegex(ValueError, "incarnation"):
            self.import_sequence()
