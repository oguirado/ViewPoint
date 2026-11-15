"""Unit tests for eval/oracle_label.py (vqa.oracle.label/v1 validator)."""
import json
import unittest
import numpy as np

from eval import oracle_label as OL


def make_valid_record(ideal=OL.CHANGED, **overrides):
    """Build a valid vqa.oracle.label/v1 record for any class 0..6."""
    rec = {
        "schema": OL.SCHEMA,
        "run_id": "run_chromium_0001",
        "event_id": f"evt_{ideal}",
        "f0": 2,
        "f1": 5,
        "rects": [[16, 32, 64, 48]],
        "tiles": [0, 1, 4],
        "ideal": ideal,
        "mv": [-4, 8] if ideal == OL.MOVED else None,
        "period_ms": 500 if ideal == OL.FLICKER else None,
        "flags": ["POINTER", "STRADDLE"],
        "source": "scroll" if ideal == OL.MOVED else ("animation" if ideal == OL.FLICKER else "mutation"),
        "source_ref": "dom:#main-viewport",
        "confidence": "exact",
    }
    rec.update(overrides)
    return rec


class TestOracleLabelValidClasses(unittest.TestCase):
    """One valid record per class (0..6) in visioncore/change.py order."""

    def test_class_constants_order(self):
        self.assertEqual(
            (OL.UNCHANGED, OL.JITTER, OL.CHANGED, OL.MOVED, OL.FLICKER, OL.FROZEN, OL.DRIFT),
            (0, 1, 2, 3, 4, 5, 6),
        )
        self.assertEqual(
            (OL.C_UNCHANGED, OL.C_JITTER, OL.C_CHANGED, OL.C_MOVED, OL.C_FLICKER, OL.C_FROZEN, OL.C_DRIFT),
            (0, 1, 2, 3, 4, 5, 6),
        )
        self.assertEqual(
            OL.NAMES,
            ("UNCHANGED", "JITTER", "CHANGED", "MOVED", "FLICKER", "FROZEN", "DRIFT"),
        )

    def test_valid_class_0_unchanged(self):
        rec = make_valid_record(OL.UNCHANGED, flags=[], source="window")
        self.assertIs(OL.validate_record(rec), rec)

    def test_valid_class_1_jitter(self):
        rec = make_valid_record(OL.JITTER, flags=["UNOBSERVABLE"], source="animation")
        self.assertIs(OL.validate_record(rec), rec)

    def test_valid_class_2_changed(self):
        rec = make_valid_record(OL.CHANGED, flags=["POINTER", "UNCOVERED"], source="mutation")
        self.assertIs(OL.validate_record(rec), rec)

    def test_valid_class_3_moved(self):
        rec = make_valid_record(OL.MOVED, mv=[0, -16], flags=["UNCOVERED"], source="scroll")
        self.assertIs(OL.validate_record(rec), rec)

    def test_valid_class_4_flicker(self):
        rec = make_valid_record(OL.FLICKER, period_ms=1000, flags=["STRADDLE"], source="focus")
        self.assertIs(OL.validate_record(rec), rec)

    def test_valid_class_5_frozen(self):
        rec = make_valid_record(OL.FROZEN, tiles=None, flags=[], source="animation", confidence="derived")
        self.assertIs(OL.validate_record(rec), rec)

    def test_valid_class_6_drift(self):
        rec = make_valid_record(OL.DRIFT, f0=10, f1=10, flags=["UNOBSERVABLE"], source="layoutshift")
        del rec["tiles"]  # optional tiles omitted entirely
        self.assertIs(OL.validate_record(rec), rec)

    def test_in_memory_synthetic_frames_and_jsonl_stream(self):
        # Synthetic frames built strictly in memory (never written to disk, no GATE/TEST splits).
        f0_rgb = np.zeros((32, 32, 3), dtype=np.uint8)
        f1_rgb = f0_rgb.copy()
        f1_rgb[0:16, 0:16] = 200
        diff_y, diff_x = np.nonzero(np.any(f0_rgb != f1_rgb, axis=2))
        rect = [
            int(diff_x.min()),
            int(diff_y.min()),
            int(diff_x.max() - diff_x.min() + 1),
            int(diff_y.max() - diff_y.min() + 1),
        ]
        self.assertEqual(rect, [0, 0, 16, 16])

        records = [
            make_valid_record(cls, rects=[rect], confidence="exact" if cls % 2 == 0 else "derived")
            for cls in range(7)
        ]
        jsonl = "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n"
        parsed = list(OL.iter_records(jsonl))
        self.assertEqual(len(parsed), 7)

        gated = OL.gate_claim_set(parsed)
        self.assertEqual([r["ideal"] for r in gated], [0, 2, 4, 6])
        self.assertTrue(all(r["confidence"] == "exact" for r in gated))


class TestOracleLabelRejections(unittest.TestCase):
    """One rejection test per validation rule with stable error code checks."""

    def _assert_rejects(self, obj, expected_code):
        with self.assertRaises(ValueError) as ctx:
            OL.validate_record(obj)
        self.assertIn(expected_code, str(ctx.exception))

    def test_reject_non_dict_record(self):
        self._assert_rejects(["not", "a", "dict"], OL.ERR_RECORD)

    def test_reject_unknown_extra_field(self):
        self._assert_rejects(make_valid_record(extra_field=1), OL.ERR_EXTRA_FIELDS)

    def test_reject_bad_schema(self):
        self._assert_rejects(make_valid_record(schema="vqa.gui_synth.label/v1"), OL.ERR_SCHEMA)

    def test_reject_bad_run_id(self):
        self._assert_rejects(make_valid_record(run_id=""), OL.ERR_RUN_ID)
        self._assert_rejects(make_valid_record(run_id=123), OL.ERR_RUN_ID)

    def test_reject_bad_event_id(self):
        self._assert_rejects(make_valid_record(event_id="   "), OL.ERR_EVENT_ID)
        self._assert_rejects(make_valid_record(event_id=None), OL.ERR_EVENT_ID)

    def test_reject_frame_range_f0_above_f1_or_negative_or_bool(self):
        self._assert_rejects(make_valid_record(f0=6, f1=5), OL.ERR_FRAME_RANGE)
        self._assert_rejects(make_valid_record(f0=-1, f1=2), OL.ERR_FRAME_RANGE)
        self._assert_rejects(make_valid_record(f0=True, f1=2), OL.ERR_FRAME_RANGE)

    def test_reject_rects_invalid_geometry_or_type(self):
        self._assert_rejects(make_valid_record(rects=[]), OL.ERR_RECTS)
        self._assert_rejects(make_valid_record(rects=[[-1, 0, 10, 10]]), OL.ERR_RECTS)
        self._assert_rejects(make_valid_record(rects=[[0, 0, 0, 10]]), OL.ERR_RECTS)
        self._assert_rejects(make_valid_record(rects=[[0, 0, 10, 0]]), OL.ERR_RECTS)
        self._assert_rejects(make_valid_record(rects=[[0, 0, 10]]), OL.ERR_RECTS)
        self._assert_rejects(make_valid_record(rects=[[0, 0, True, 10]]), OL.ERR_RECTS)

    def test_reject_tiles_invalid(self):
        self._assert_rejects(make_valid_record(tiles=[-1]), OL.ERR_TILES)
        self._assert_rejects(make_valid_record(tiles=[2, 2]), OL.ERR_TILES)
        self._assert_rejects(make_valid_record(tiles=[False]), OL.ERR_TILES)
        self._assert_rejects(make_valid_record(tiles="0,1"), OL.ERR_TILES)

    def test_reject_ideal_out_of_range_or_bool(self):
        self._assert_rejects(make_valid_record(ideal=7), OL.ERR_IDEAL)
        self._assert_rejects(make_valid_record(ideal=-1), OL.ERR_IDEAL)
        self._assert_rejects(make_valid_record(ideal=True), OL.ERR_IDEAL)

    def test_reject_mv_missing_on_moved_or_present_on_other(self):
        self._assert_rejects(make_valid_record(OL.MOVED, mv=None), OL.ERR_MV)
        self._assert_rejects(make_valid_record(OL.MOVED, mv=[4]), OL.ERR_MV)
        self._assert_rejects(make_valid_record(OL.MOVED, mv=[True, 0]), OL.ERR_MV)
        self._assert_rejects(make_valid_record(OL.CHANGED, mv=[0, 0]), OL.ERR_MV)

    def test_reject_period_ms_missing_on_flicker_or_present_on_other(self):
        self._assert_rejects(make_valid_record(OL.FLICKER, period_ms=None), OL.ERR_PERIOD_MS)
        self._assert_rejects(make_valid_record(OL.FLICKER, period_ms=0), OL.ERR_PERIOD_MS)
        self._assert_rejects(make_valid_record(OL.FLICKER, period_ms=True), OL.ERR_PERIOD_MS)
        self._assert_rejects(make_valid_record(OL.CHANGED, period_ms=500), OL.ERR_PERIOD_MS)

    def test_reject_flags_unknown_or_duplicate(self):
        self._assert_rejects(make_valid_record(flags=["UNKNOWN_FLAG"]), OL.ERR_FLAGS)
        self._assert_rejects(make_valid_record(flags=["POINTER", "POINTER"]), OL.ERR_FLAGS)
        self._assert_rejects(make_valid_record(flags="POINTER"), OL.ERR_FLAGS)

    def test_reject_source_unknown(self):
        self._assert_rejects(make_valid_record(source="network"), OL.ERR_SOURCE)
        self._assert_rejects(make_valid_record(source=None), OL.ERR_SOURCE)

    def test_reject_source_ref_invalid(self):
        self._assert_rejects(make_valid_record(source_ref=""), OL.ERR_SOURCE_REF)
        self._assert_rejects(make_valid_record(source_ref=42), OL.ERR_SOURCE_REF)

    def test_reject_confidence_unknown(self):
        self._assert_rejects(make_valid_record(confidence="guessed"), OL.ERR_CONFIDENCE)
        self._assert_rejects(make_valid_record(confidence=None), OL.ERR_CONFIDENCE)

    def test_reject_jsonl_malformed_or_float(self):
        bad_float_line = json.dumps(make_valid_record()).replace('"f0": 2', '"f0": 2.5')
        with self.assertRaises(ValueError) as ctx:
            list(OL.iter_records(bad_float_line))
        self.assertIn(OL.ERR_JSON, str(ctx.exception))

        with self.assertRaises(ValueError) as ctx2:
            list(OL.iter_records("{not valid json}\n"))
        self.assertIn(OL.ERR_JSON, str(ctx2.exception))


if __name__ == "__main__":
    unittest.main()
