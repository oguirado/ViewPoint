"""Negative-path and unit tests for vmem spool ingestion in vmem/ingest.py."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from visioncore.errors import VqaError
from vmem import ingest


def make_spool_bytes(lines):
    return b"\n".join(json.dumps(l).encode("utf-8") for l in lines)


class TestVmemIngestSpool(unittest.TestCase):
    def test_bad_raises_vqa_error(self):
        """_bad raises VqaError with E_SCHEMA (rc 2) and reason/detail kwargs."""
        with self.assertRaises(VqaError) as cm:
            ingest._bad("test_reason", foo="bar")
        self.assertEqual(cm.exception.code, "E_SCHEMA")
        self.assertEqual(cm.exception.rc, 2)
        self.assertEqual(cm.exception.detail, {"reason": "test_reason", "foo": "bar"})

    def test_look_region_normalizes_carrier(self):
        """_look_region normalizes static-look carrier into a region dictionary."""
        scope = {"app": "demo", "scene": "hud", "res": "1280x720", "grid": "grid:1280x720:s16", "build_seq": 1}
        body = {
            "frame": 0,
            "render_index": 0,
            "role": "ui",
            "m0": 0,
            "m1": 0,
            "look": {"grid": "grid:1280x720:s16", "n": 2, "ah64": "0" * 16, "desc_blob": "0" * 64, "y_q4_mean": 100},
            "xh64": ["0" * 16, "1" * 16],
            "sig64": ["2" * 16, "3" * 16],
        }
        norm = ingest._look_region(body, scope)
        self.assertEqual(norm["cls"], "UNCHANGED")
        self.assertEqual(norm["id"], "r0:0000")
        self.assertEqual(norm["xh64"], "0" * 16)
        self.assertEqual(norm["sig64"], "2" * 16)
        self.assertEqual(norm["bbox_px"], [0, 0, 16, 16])

    def test_look_region_passes_through_expanded(self):
        """_look_region passes through body unchanged if 'id' is already present."""
        scope = {"app": "demo", "scene": "hud", "res": "1280x720", "grid": "grid:1280x720:s16", "build_seq": 1}
        body = {"id": "r0:0000", "frame": 0, "cls": "UNCHANGED"}
        res = ingest._look_region(body, scope)
        self.assertEqual(res, body)

    def test_look_region_missing_field(self):
        """_look_region rejects carrier missing required fields with E_SCHEMA (rc 2)."""
        scope = {"app": "demo", "scene": "hud", "res": "1280x720", "grid": "grid:1280x720:s16", "build_seq": 1}
        body = {
            "frame": 0,
            "role": "ui",
            "m0": 0,
            "m1": 0,
            "look": {"grid": "grid:1280x720:s16", "n": 1, "ah64": "0" * 16, "desc_blob": "0" * 64, "y_q4_mean": 100},
            "xh64": ["0" * 16],
            "sig64": ["2" * 16],
        }
        with self.assertRaises(VqaError) as cm:
            ingest._look_region(body, scope)
        self.assertEqual(cm.exception.code, "E_SCHEMA")
        self.assertEqual(cm.exception.rc, 2)
        self.assertEqual(cm.exception.detail["reason"], "look_missing_field")
        self.assertEqual(cm.exception.detail["missing"], ["render_index"])

    def test_look_region_invalid_hashes(self):
        """_look_region rejects carrier with mismatched hash count with E_SCHEMA (rc 2)."""
        scope = {"app": "demo", "scene": "hud", "res": "1280x720", "grid": "grid:1280x720:s16", "build_seq": 1}
        body = {
            "frame": 0,
            "render_index": 0,
            "role": "ui",
            "m0": 0,
            "m1": 0,
            "look": {"grid": "grid:1280x720:s16", "n": 2, "ah64": "0" * 16, "desc_blob": "0" * 64, "y_q4_mean": 100},
            "xh64": ["0" * 16],
            "sig64": ["2" * 16, "3" * 16],
        }
        with self.assertRaises(VqaError) as cm:
            ingest._look_region(body, scope)
        self.assertEqual(cm.exception.code, "E_SCHEMA")
        self.assertEqual(cm.exception.rc, 2)
        self.assertEqual(cm.exception.detail["reason"], "look_hashes")
        self.assertEqual(cm.exception.detail["field"], "xh64")

    def test_parse_spool_valid_minimal(self):
        """parse_spool accepts a minimal valid spool and returns run, eps, regs, vds."""
        rid = "r_000001_00000001"
        run_line = {
            "schema": "vmem.spool/v1",
            "seq": 0,
            "type": "run",
            "run_id": rid,
            "body": {
                "scope": {"app": "demo", "scene": "hud", "res": "1280x720", "grid": "grid:1280x720:s16", "build_seq": 1},
                "receipt_sha256": "1" * 64,
                "profile_sha256": "2" * 64,
                "align_source": "render_index",
            },
        }
        reg_line = {
            "schema": "vmem.spool/v1",
            "seq": 1,
            "type": "region",
            "run_id": rid,
            "body": {
                "id": "r3:0a40",
                "frame": 3,
                "cls": "CHANGED",
                "bbox_px": [0, 0, 16, 16],
                "xh64": "0" * 16,
                "sig64": "0" * 16,
                "render_index": 3,
                "look": {"n": 1},
                "desc_hex": "00" * 32,
            },
        }
        ver_line = {
            "schema": "vmem.spool/v1",
            "seq": 2,
            "type": "verdict",
            "run_id": rid,
            "body": {"verdict": "PASS", "check_id": "render.flicker", "pointers": ["r3:0a40"]},
        }
        ep_line = {
            "schema": "vmem.spool/v1",
            "seq": 3,
            "type": "episode",
            "run_id": rid,
            "body": {
                "schema": "vqa.episode/v1",
                "episode": "r_000001_00000001/g0",
                "run_id": rid,
                "gop": 0,
                "f0": 0,
                "f1": 19,
                "n_frames": 20,
                "cut": 0,
                "gop_blob_sha256": "a" * 64,
                "verdicts": [{"verdict": "PASS"}],
                "fg": False,
                "classes_ppm": {"CHANGED": 1},
                "delta_bytes": 10,
                "grid": "grid:1280x720:s16",
                "params_sha256": "3" * 64,
            },
        }
        raw = make_spool_bytes([run_line, reg_line, ver_line, ep_line])
        rb, eps, regs, vds = ingest.parse_spool(raw)
        self.assertEqual(rb, run_line["body"])
        self.assertEqual(eps, [ep_line["body"]])
        self.assertEqual(regs, [reg_line["body"]])
        self.assertEqual(vds, [ver_line["body"]])

    def test_parse_spool_static_look_carrier_normalised(self):
        """parse_spool normalizes 'look' type records using _look_region into UNCHANGED regions."""
        rid = "r_000001_00000001"
        run_line = {
            "schema": "vmem.spool/v1",
            "seq": 0,
            "type": "run",
            "run_id": rid,
            "body": {
                "scope": {"app": "demo", "scene": "hud", "res": "1280x720", "grid": "grid:1280x720:s16", "build_seq": 1},
                "receipt_sha256": "1" * 64,
                "profile_sha256": "2" * 64,
                "align_source": "render_index",
            },
        }
        look_line = {
            "schema": "vmem.spool/v1",
            "seq": 1,
            "type": "look",
            "run_id": rid,
            "body": {
                "frame": 0,
                "render_index": 0,
                "role": "ui",
                "m0": 0,
                "m1": 0,
                "look": {"grid": "grid:1280x720:s16", "n": 1, "ah64": "0" * 16, "desc_blob": "0" * 64, "y_q4_mean": 100},
                "xh64": ["0" * 16],
                "sig64": ["1" * 16],
                "desc_hex": "00" * 32,
            },
        }
        ver_line = {
            "schema": "vmem.spool/v1",
            "seq": 2,
            "type": "verdict",
            "run_id": rid,
            "body": {"verdict": "PASS", "check_id": "render.flicker", "pointers": ["r0:0000"]},
        }
        raw = make_spool_bytes([run_line, look_line, ver_line])
        rb, eps, regs, vds = ingest.parse_spool(raw)
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0]["cls"], "UNCHANGED")
        self.assertEqual(regs[0]["id"], "r0:0000")

    def test_parse_spool_empty_spool(self):
        """parse_spool rejects empty spool with E_SCHEMA (rc 2)."""
        with self.assertRaises(VqaError) as cm:
            ingest.parse_spool(b"\n\n")
        self.assertEqual(cm.exception.code, "E_SCHEMA")
        self.assertEqual(cm.exception.rc, 2)
        self.assertEqual(cm.exception.detail["reason"], "empty_spool")

    def test_parse_spool_missing_run_header(self):
        """parse_spool rejects spool missing a 'run' header as first record with E_SCHEMA (rc 2)."""
        rid = "r_000001_00000001"
        reg_line = {
            "schema": "vmem.spool/v1",
            "seq": 0,
            "type": "region",
            "run_id": rid,
            "body": {
                "id": "r3:0a40",
                "frame": 3,
                "cls": "CHANGED",
                "bbox_px": [0, 0, 16, 16],
                "xh64": "0" * 16,
                "sig64": "0" * 16,
                "render_index": 3,
                "look": {"n": 1},
                "desc_hex": "00" * 32,
            },
        }
        raw = make_spool_bytes([reg_line])
        with self.assertRaises(VqaError) as cm:
            ingest.parse_spool(raw)
        self.assertEqual(cm.exception.code, "E_SCHEMA")
        self.assertEqual(cm.exception.rc, 2)
        self.assertEqual(cm.exception.detail["reason"], "first_not_run")

    def test_parse_spool_duplicate_seq_rejected(self):
        """parse_spool rejects duplicate line sequence numbers with E_SCHEMA (rc 2)."""
        rid = "r_000001_00000001"
        run_line = {
            "schema": "vmem.spool/v1",
            "seq": 0,
            "type": "run",
            "run_id": rid,
            "body": {
                "scope": {"app": "demo", "scene": "hud", "res": "1280x720", "grid": "grid:1280x720:s16", "build_seq": 1},
                "receipt_sha256": "1" * 64,
                "profile_sha256": "2" * 64,
                "align_source": "render_index",
            },
        }
        reg_line = {
            "schema": "vmem.spool/v1",
            "seq": 0,  # duplicate seq 0
            "type": "region",
            "run_id": rid,
            "body": {
                "id": "r3:0a40",
                "frame": 3,
                "cls": "CHANGED",
                "bbox_px": [0, 0, 16, 16],
                "xh64": "0" * 16,
                "sig64": "0" * 16,
                "render_index": 3,
                "look": {"n": 1},
                "desc_hex": "00" * 32,
            },
        }
        raw = make_spool_bytes([run_line, reg_line])
        with self.assertRaises(VqaError) as cm:
            ingest.parse_spool(raw)
        self.assertEqual(cm.exception.code, "E_SCHEMA")
        self.assertEqual(cm.exception.rc, 2)
        self.assertEqual(cm.exception.detail["reason"], "spool_line")
        self.assertEqual(cm.exception.detail["seq"], 1)

    def test_parse_spool_mixed_run_ids_rejected(self):
        """parse_spool rejects records with mixed run_ids with E_SCHEMA (rc 2)."""
        run_line = {
            "schema": "vmem.spool/v1",
            "seq": 0,
            "type": "run",
            "run_id": "r_000001_00000001",
            "body": {
                "scope": {"app": "demo", "scene": "hud", "res": "1280x720", "grid": "grid:1280x720:s16", "build_seq": 1},
                "receipt_sha256": "1" * 64,
                "profile_sha256": "2" * 64,
                "align_source": "render_index",
            },
        }
        reg_line = {
            "schema": "vmem.spool/v1",
            "seq": 1,
            "type": "region",
            "run_id": "r_000002_00000002",  # mismatched run_id
            "body": {
                "id": "r3:0a40",
                "frame": 3,
                "cls": "CHANGED",
                "bbox_px": [0, 0, 16, 16],
                "xh64": "0" * 16,
                "sig64": "0" * 16,
                "render_index": 3,
                "look": {"n": 1},
                "desc_hex": "00" * 32,
            },
        }
        raw = make_spool_bytes([run_line, reg_line])
        with self.assertRaises(VqaError) as cm:
            ingest.parse_spool(raw)
        self.assertEqual(cm.exception.code, "E_SCHEMA")
        self.assertEqual(cm.exception.rc, 2)
        self.assertEqual(cm.exception.detail["reason"], "run_id_mixed")

    def test_parse_spool_unknown_record_kind(self):
        """parse_spool rejects unknown record kinds with E_SCHEMA (rc 2)."""
        rid = "r_000001_00000001"
        bad_line = {"schema": "vmem.spool/v1", "seq": 0, "type": "unknown_kind", "run_id": rid, "body": {}}
        raw = make_spool_bytes([bad_line])
        with self.assertRaises(VqaError) as cm:
            ingest.parse_spool(raw)
        self.assertEqual(cm.exception.code, "E_SCHEMA")
        self.assertEqual(cm.exception.rc, 2)
        self.assertEqual(cm.exception.detail["reason"], "spool_line")

    def test_parse_spool_floats_rejected(self):
        """parse_spool rejects float values with E_FLOAT (rc 2)."""
        rid = "r_000001_00000001"
        run_line = {
            "schema": "vmem.spool/v1",
            "seq": 0.0,
            "type": "run",
            "run_id": rid,
            "body": {
                "scope": {"app": "demo", "scene": "hud", "res": "1280x720", "grid": "grid:1280x720:s16", "build_seq": 1},
                "receipt_sha256": "1" * 64,
                "profile_sha256": "2" * 64,
                "align_source": "render_index",
            },
        }
        raw = make_spool_bytes([run_line])
        with self.assertRaises(VqaError) as cm:
            ingest.parse_spool(raw)
        self.assertEqual(cm.exception.code, "E_FLOAT")
        self.assertEqual(cm.exception.rc, 2)

    def test_parse_spool_regions_without_scope(self):
        """parse_spool rejects run header without scope with E_SCHEMA (rc 2)."""
        rid = "r_000001_00000001"
        run_line = {
            "schema": "vmem.spool/v1",
            "seq": 0,
            "type": "run",
            "run_id": rid,
            "body": {
                "receipt_sha256": "1" * 64,
                "profile_sha256": "2" * 64,
                "align_source": "render_index",
            },
        }
        reg_line = {
            "schema": "vmem.spool/v1",
            "seq": 1,
            "type": "region",
            "run_id": rid,
            "body": {
                "id": "r3:0a40",
                "frame": 3,
                "cls": "CHANGED",
                "bbox_px": [0, 0, 16, 16],
                "xh64": "0" * 16,
                "sig64": "0" * 16,
                "render_index": 3,
                "look": {"n": 1},
                "desc_hex": "00" * 32,
            },
        }
        raw = make_spool_bytes([run_line, reg_line])
        with self.assertRaises(VqaError) as cm:
            ingest.parse_spool(raw)
        self.assertEqual(cm.exception.code, "E_SCHEMA")
        self.assertEqual(cm.exception.rc, 2)
        self.assertEqual(cm.exception.detail["reason"], "run_body")

    def test_parse_spool_invalid_region_id(self):
        """parse_spool rejects region with invalid ID syntax with E_VC_ID_SYNTAX (rc 3)."""
        rid = "r_000001_00000001"
        run_line = {
            "schema": "vmem.spool/v1",
            "seq": 0,
            "type": "run",
            "run_id": rid,
            "body": {
                "scope": {"app": "demo", "scene": "hud", "res": "1280x720", "grid": "grid:1280x720:s16", "build_seq": 1},
                "receipt_sha256": "1" * 64,
                "profile_sha256": "2" * 64,
                "align_source": "render_index",
            },
        }
        reg_line = {
            "schema": "vmem.spool/v1",
            "seq": 1,
            "type": "region",
            "run_id": rid,
            "body": {
                "id": "bad_region_id_syntax",
                "frame": 3,
                "cls": "CHANGED",
                "bbox_px": [0, 0, 16, 16],
                "xh64": "0" * 16,
                "sig64": "0" * 16,
                "render_index": 3,
                "look": {"n": 1},
                "desc_hex": "00" * 32,
            },
        }
        raw = make_spool_bytes([run_line, reg_line])
        with self.assertRaises(VqaError) as cm:
            ingest.parse_spool(raw)
        self.assertEqual(cm.exception.rc, 3)

    def test_parse_spool_verdict_unknown_pointer(self):
        """parse_spool rejects verdict pointing to non-existent region ID with E_SCHEMA (rc 2)."""
        rid = "r_000001_00000001"
        run_line = {
            "schema": "vmem.spool/v1",
            "seq": 0,
            "type": "run",
            "run_id": rid,
            "body": {
                "scope": {"app": "demo", "scene": "hud", "res": "1280x720", "grid": "grid:1280x720:s16", "build_seq": 1},
                "receipt_sha256": "1" * 64,
                "profile_sha256": "2" * 64,
                "align_source": "render_index",
            },
        }
        reg_line = {
            "schema": "vmem.spool/v1",
            "seq": 1,
            "type": "region",
            "run_id": rid,
            "body": {
                "id": "r3:0a40",
                "frame": 3,
                "cls": "CHANGED",
                "bbox_px": [0, 0, 16, 16],
                "xh64": "0" * 16,
                "sig64": "0" * 16,
                "render_index": 3,
                "look": {"n": 1},
                "desc_hex": "00" * 32,
            },
        }
        ver_line = {
            "schema": "vmem.spool/v1",
            "seq": 2,
            "type": "verdict",
            "run_id": rid,
            "body": {"verdict": "PASS", "check_id": "render.flicker", "pointers": ["r99:0a40"]},
        }
        raw = make_spool_bytes([run_line, reg_line, ver_line])
        with self.assertRaises(VqaError) as cm:
            ingest.parse_spool(raw)
        self.assertEqual(cm.exception.code, "E_SCHEMA")
        self.assertEqual(cm.exception.rc, 2)
        self.assertEqual(cm.exception.detail["reason"], "verdict_body")

    def test_quarantine_moves_spool(self):
        """_quarantine moves spool file to store/quarantine/<run_id>/<filename>."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = os.path.join(tmpdir, "store")
            spool_file = os.path.join(tmpdir, "test.spool")
            with open(spool_file, "wb") as f:
                f.write(b"test spool content")

            q_dir = ingest._quarantine(root, spool_file, "run_123_456")
            expected_dir = os.path.join(root, "quarantine", "run_123_456")
            self.assertEqual(q_dir, expected_dir)
            self.assertFalse(os.path.exists(spool_file))
            self.assertTrue(os.path.exists(os.path.join(expected_dir, "test.spool")))

    def test_quarantine_sanitizes_run_id(self):
        """_quarantine sanitizes unsafe characters in run_id when creating quarantine path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = os.path.join(tmpdir, "store")
            spool_file = os.path.join(tmpdir, "test.spool")
            with open(spool_file, "wb") as f:
                f.write(b"test spool content")

            q_dir = ingest._quarantine(root, spool_file, "../../../invalid_run_id!!")
            expected_dir = os.path.join(root, "quarantine", "_________invalid_run_id__")
            self.assertEqual(q_dir, expected_dir)
            self.assertTrue(os.path.exists(os.path.join(expected_dir, "test.spool")))

    def test_next_returns_highest_id_counter(self):
        """_next scans units and returns the highest integer ID suffix for a prefix."""
        units = [
            {"id": "V-BAS-000001"},
            {"id": "V-BAS-000005"},
            {"id": "V-EVT-000002"},
        ]
        self.assertEqual(ingest._next(units, "BAS"), 5)
        self.assertEqual(ingest._next(units, "EVT"), 2)

    def test_next_returns_zero_when_prefix_absent(self):
        """_next returns 0 when no units match the specified prefix."""
        units = [{"id": "V-BAS-000001"}]
        self.assertEqual(ingest._next(units, "VER"), 0)
        self.assertEqual(ingest._next([], "BAS"), 0)

    def test_run_verb_compact(self):
        """run_verb executing 'compact' verb succeeds and returns generation dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = os.path.join(tmpdir, "store")
            ingest.Store(root)
            args = SimpleNamespace(spool=None, id=None, receipt=None)
            res = ingest.run_verb("compact", root, args)
            self.assertIn("gen", res)

    def test_run_verb_refuses_missing_arg(self):
        """run_verb raises E_USAGE (rc 2) when required verb argument is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = os.path.join(tmpdir, "store")
            args = SimpleNamespace(spool=None, id=None, receipt=None)
            with self.assertRaises(VqaError) as cm:
                ingest.run_verb("ingest", root, args)
            self.assertEqual(cm.exception.code, "E_USAGE")
            self.assertEqual(cm.exception.rc, 2)
            self.assertEqual(cm.exception.detail, {"reason": "missing_arg", "arg": "--spool"})

    def test_run_verb_unknown_verb_refused(self):
        """run_verb treats unknown verbs as pin_run, refusing missing ID or nonexistent unit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = os.path.join(tmpdir, "store")
            # Without ID arg: missing ID error
            args_no_id = SimpleNamespace(spool=None, id=None, receipt=None)
            with self.assertRaises(VqaError) as cm:
                ingest.run_verb("invalid_verb", root, args_no_id)
            self.assertEqual(cm.exception.code, "E_USAGE")
            self.assertEqual(cm.exception.rc, 2)
            self.assertEqual(cm.exception.detail, {"reason": "missing_arg", "arg": "ID"})

            # With ID and receipt args: falls through to pin_run and fails with E_ID (rc 3) unit_not_found
            rcpt = os.path.join(tmpdir, "receipt.txt")
            with open(rcpt, "wb") as f:
                f.write(b"receipt")
            args_with_id = SimpleNamespace(spool=None, id="V-BAS-000001", receipt=rcpt)
            with self.assertRaises(VqaError) as cm:
                ingest.run_verb("invalid_verb", root, args_with_id)
            self.assertEqual(cm.exception.code, "E_ID")
            self.assertEqual(cm.exception.rc, 3)
            self.assertEqual(cm.exception.detail["reason"], "unit_not_found")


if __name__ == "__main__":
    unittest.main()
