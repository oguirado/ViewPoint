"""Unit tests for segmenter replayer strictness and skill admission handoff (D-A8-10)."""
import json
from types import SimpleNamespace
import unittest

from wender.learn import demonstrations, segmenter
from wender.remember import skills


class FakeEngine:
    """Fake vision engine returning preset tile diffs over pair sequence."""

    def __init__(self, counts):
        self.counts = list(counts)
        self.idx = 0

    def changed_regions(self, prev, cur):
        if self.idx < len(self.counts):
            count = self.counts[self.idx]
            self.idx += 1
            if count > 0:
                return [{"x": 0, "y": 0, "w": 8, "h": 8, "tiles": count}]
        return []


def build_six_frames():
    """Build six in-RAM RGB24 frames with 16x16 geometry."""
    return [
        SimpleNamespace(width=16, height=16, data=bytes(16 * 16 * 3))
        for _ in range(6)
    ]


class TestSegmenterReplayerAndSkillHandoff(unittest.TestCase):
    def test_pipeline_shape_strict_boolean_replayer_success(self):
        """Pipeline produces verified segment with single-arg labeler, coordinates mapper, strict replayer."""
        frames = build_six_frames()
        engine = FakeEngine([5, 5, 1, 1, 1])

        def single_arg_labeler(seg_frames):
            self.assertEqual(len(seg_frames), 3)
            return "intent_1"

        def mapper(intent):
            self.assertEqual(intent, "intent_1")
            return {"path": "coordinates", "x": 10, "y": 10}

        def replayer(actions, seg):
            self.assertEqual(actions, {"path": "coordinates", "x": 10, "y": 10})
            return {"reproduced": True, "verified": True}

        report = segmenter.run_pipeline(
            frames,
            engine,
            labeler=single_arg_labeler,
            mapper=mapper,
            replayer=replayer,
        )

        self.assertEqual(report["reproduced_share_ppm"], 1000000)
        self.assertEqual(len(report["segments"]), 1)
        seg = report["segments"][0]
        self.assertEqual(seg["intent"], "intent_1")
        self.assertEqual(seg["actions"], {"path": "coordinates", "x": 10, "y": 10})
        self.assertIs(seg["reproduced"], True)
        self.assertIs(seg["verified"], True)
        self.assertIsNone(seg["error"])

    def test_non_strict_replayer_integer_one_reproduced_error(self):
        """Fresh engine: replayer returning integer 1 for reproduced produces error='replayer'."""
        frames = build_six_frames()
        engine = FakeEngine([5, 5, 1, 1, 1])

        def single_arg_labeler(seg_frames):
            return "intent_1"

        def mapper(intent):
            return {"path": "coordinates", "x": 10, "y": 10}

        def replayer(actions, seg):
            return {"reproduced": 1, "verified": True}

        report = segmenter.run_pipeline(
            frames,
            engine,
            labeler=single_arg_labeler,
            mapper=mapper,
            replayer=replayer,
        )

        self.assertEqual(report["reproduced_share_ppm"], 0)
        self.assertEqual(len(report["segments"]), 1)
        seg = report["segments"][0]
        self.assertIs(seg["reproduced"], False)
        self.assertIs(seg["verified"], False)
        self.assertEqual(seg["error"], "replayer")

    def test_non_strict_replayer_string_yes_verified_error(self):
        """Fresh engine: replayer returning string 'yes' for verified produces error='replayer'."""
        frames = build_six_frames()
        engine = FakeEngine([5, 5, 1, 1, 1])

        def single_arg_labeler(seg_frames):
            return "intent_1"

        def mapper(intent):
            return {"path": "coordinates", "x": 10, "y": 10}

        def replayer(actions, seg):
            return {"reproduced": True, "verified": "yes"}

        report = segmenter.run_pipeline(
            frames,
            engine,
            labeler=single_arg_labeler,
            mapper=mapper,
            replayer=replayer,
        )

        self.assertEqual(report["reproduced_share_ppm"], 0)
        self.assertEqual(len(report["segments"]), 1)
        seg = report["segments"][0]
        self.assertIs(seg["reproduced"], False)
        self.assertIs(seg["verified"], False)
        self.assertEqual(seg["error"], "replayer")

    def test_non_strict_replayer_returns_none_error(self):
        """Fresh engine: replayer returning None produces error='replayer'."""
        frames = build_six_frames()
        engine = FakeEngine([5, 5, 1, 1, 1])

        def single_arg_labeler(seg_frames):
            return "intent_1"

        def mapper(intent):
            return {"path": "coordinates", "x": 10, "y": 10}

        def replayer(actions, seg):
            return None

        report = segmenter.run_pipeline(
            frames,
            engine,
            labeler=single_arg_labeler,
            mapper=mapper,
            replayer=replayer,
        )

        self.assertEqual(report["reproduced_share_ppm"], 0)
        self.assertEqual(len(report["segments"]), 1)
        seg = report["segments"][0]
        self.assertIs(seg["reproduced"], False)
        self.assertIs(seg["verified"], False)
        self.assertEqual(seg["error"], "replayer")

    def test_skills_admit_automatic_handoff_from_segment_outcome(self):
        # Promoted D-A8-10: the executed pipeline provides the admission fields.
        frames = build_six_frames()
        engine = FakeEngine([5, 5, 1, 1, 1])

        report = segmenter.run_pipeline(
            frames,
            engine,
            labeler=lambda seg_frames: "intent_1",
            mapper=lambda intent: {"path": "coordinates", "x": 10, "y": 10},
            replayer=lambda actions, seg: {"reproduced": True, "verified": True},
        )

        verified_segment = report["segments"][0]
        self.assertTrue(verified_segment["verified"])

        # Direct admission handoff of real verified segment outcome without manual evidence translation
        admitted_json = skills.admit(verified_segment)
        parsed = json.loads(admitted_json)
        self.assertIs(parsed["evidence"]["reproduced"], True)
        self.assertTrue(parsed["evidence"]["verified_by"])

    def test_verified_handoff_preserves_actual_lineage_and_copy_ownership(self):
        actions = {"path": "ax", "target": {"name": "Original"}}
        receipt = {"reproduced": True, "verified": True,
                   "evidence": {"verified_by": "fixture-verifier/v2", "runs": 3,
                                "details": {"receipt": "r1"}}}

        def replayer(action_copy, segment_copy):
            action_copy["target"]["name"] = "Mutated by replayer"
            segment_copy["bbox"]["x"] = 999
            return receipt

        outcome = segmenter.run_pipeline(
            build_six_frames(), FakeEngine([5, 5, 1, 1, 1]),
            labeler=lambda frames: "intent", mapper=lambda intent: actions,
            replayer=replayer,
        )["segments"][0]
        actions["target"]["name"] = "Mutated by mapper owner"
        receipt["evidence"]["details"]["receipt"] = "Mutated by receipt owner"
        self.assertEqual(outcome["actions"]["target"]["name"], "Original")
        self.assertEqual(outcome["steps"][0]["target"]["name"], "Original")
        self.assertEqual(outcome["bbox"]["x"], 0)
        self.assertEqual(outcome["evidence"]["verified_by"], "fixture-verifier/v2")
        self.assertEqual(outcome["evidence"]["details"]["receipt"], "r1")
        self.assertEqual(outcome["evidence"]["runs"], 3)
        self.assertEqual(outcome["evidence"]["version"], 1)
        self.assertEqual(outcome["checkpoints"][0]["verified_by"], "fixture-verifier/v2")
        self.assertIsInstance(skills.admit(outcome), str)

    def test_missing_explicit_lineage_names_the_executed_callable(self):
        def local_verifier(actions, segment):
            return {"reproduced": True, "verified": True}

        outcome = segmenter.run_pipeline(
            build_six_frames(), FakeEngine([5, 5, 1, 1, 1]),
            labeler=lambda frames: "intent", mapper=lambda intent: {"path": "ax"},
            replayer=local_verifier,
        )["segments"][0]
        self.assertEqual(outcome["evidence"]["verified_by"],
                         local_verifier.__module__ + "." + local_verifier.__qualname__)

    def test_unverified_or_contradictory_receipt_cannot_admit(self):
        receipts = [
            {"reproduced": True, "verified": False},
            {"reproduced": False, "verified": True},
            {"reproduced": True, "verified": True, "verified_by": ""},
            {"reproduced": True, "verified": True, "verified_by": "one",
             "evidence": {"verified_by": "two"}},
            {"reproduced": True, "verified": True, "evidence": {"reproduced": False}},
            {"reproduced": True, "verified": True, "evidence": {"verified": False}},
            {"reproduced": True, "verified": True, "evidence": {"verified": 1}},
            {"reproduced": True, "verified": True,
             "checkpoints": [{"after_step": 0, "verified": False}]},
        ]
        for receipt in receipts:
            with self.subTest(receipt=receipt):
                report = segmenter.run_pipeline(
                    build_six_frames(), FakeEngine([5, 5, 1, 1, 1]),
                    labeler=lambda frames: "intent", mapper=lambda intent: {"path": "ax"},
                    replayer=lambda actions, segment: receipt,
                )
                self.assertEqual(report["reproduced_share_ppm"], 0)
                self.assertNotIn("evidence", report["segments"][0])
                with self.assertRaises(skills.SkillRejected):
                    skills.admit(report["segments"][0])

    def test_binary_metadata_is_not_retained_in_pipeline_report(self):
        report = segmenter.run_pipeline(
            build_six_frames(), FakeEngine([5, 5, 1, 1, 1]),
            labeler=lambda frames: {"unexpected": frames[0].data},
            mapper=lambda intent: {"path": "ax"},
            replayer=lambda actions, segment: {"reproduced": True, "verified": True},
        )
        outcome = report["segments"][0]
        self.assertEqual(outcome["error"], "labeler")
        self.assertIsNone(outcome["intent"])
        self.assertEqual(report["reproduced_share_ppm"], 0)
        json.dumps(report)


if __name__ == "__main__":
    unittest.main()
