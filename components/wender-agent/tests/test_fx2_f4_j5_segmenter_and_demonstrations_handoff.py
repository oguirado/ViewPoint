"""Integration tests for segmenter, demonstrations, skills admission, and Store persistence handoff."""
import copy
import json
import tempfile
from types import SimpleNamespace
import unittest

from wender.learn import demonstrations, segmenter
from wender.remember import skills
from wender.remember.store import Store


class PresetEngine:
    """Preset vision engine returning tile diffs per frame pair."""

    def __init__(self, pair_regions):
        self.pair_regions = list(pair_regions)
        self.idx = 0

    def changed_regions(self, prev, cur):
        if self.idx < len(self.pair_regions):
            res = self.pair_regions[self.idx]
            self.idx += 1
            if isinstance(res, int):
                if res > 0:
                    return [{"x": 0, "y": 0, "w": 8, "h": 8, "tiles": res}]
                return []
            return res
        return []


def build_ram_frame(width=16, height=16, fill_byte=0):
    """Create in-RAM RGB24 frame."""
    return SimpleNamespace(width=width, height=height, data=bytes([fill_byte] * (width * height * 3)))


def make_skill_dict(skill_id, intent_name, actions, verifier="test_verifier", source="test_source", version=1):
    """Construct a valid skill dictionary compliant with skills.admit."""
    steps = actions if isinstance(actions, list) else [actions]
    return {
        "id": skill_id,
        "intent": intent_name,
        "params": {"param1": "val1"},
        "steps": steps,
        "checkpoints": [{"after_step": len(steps) - 1, "expected_post": {"status": "ok"}}],
        "provenance": {"source": source},
        "evidence": {
            "reproduced": True,
            "verified_by": verifier,
            "version": version,
        },
    }


class TestFx2F4J5SegmenterAndDemonstrationsHandoff(unittest.TestCase):
    def test_pipeline_composition_and_store_persistence_lifecycle(self):
        """Compose run_pipeline over distinct RAM segments, admit skills, persist to Store, and verify after reopen."""
        # 12 frames -> 11 pairs
        # Segment 0: pairs 0..1 (counts 5, 5), quiet 2..4 (counts 1, 1, 1) -> start 0, end 2
        # Segment 1: pairs 5..6 (counts 5, 5), quiet 7..9 (counts 1, 1, 1) -> start 5, end 7
        frames = [build_ram_frame(16, 16, i) for i in range(12)]
        counts = [5, 5, 1, 1, 1, 5, 5, 1, 1, 1, 0]
        engine = PresetEngine(counts)

        def labeler(seg, seg_frames):
            return "intent_seg_%d" % seg["start"]

        def mapper(intent):
            return [{"path": "api", "op": "exec_" + intent}]

        def replayer(actions, seg):
            if seg["start"] == 0:
                return {"reproduced": True, "verified": True}
            return {"reproduced": True, "verified": False}

        report = segmenter.run_pipeline(frames, engine, labeler=labeler, mapper=mapper, replayer=replayer)

        # 1 verified out of 2 total segments -> reproduced_share_ppm = 500000
        self.assertEqual(report["reproduced_share_ppm"], 500000)
        self.assertEqual(len(report["segments"]), 2)

        seg0, seg1 = report["segments"]
        self.assertTrue(seg0["verified"])
        self.assertFalse(seg1["verified"])

        # Translate verified outcome seg0 into an admitted skill
        skill_data0 = make_skill_dict(
            skill_id="skill_seg_0",
            intent_name=seg0["intent"],
            actions=seg0["actions"],
            verifier="replayer_verifier_v1",
            source="pipeline_segment_0",
            version=1,
        )

        canonical_json = skills.admit(skill_data0)
        self.assertTrue(canonical_json.startswith("{"))

        with tempfile.TemporaryDirectory() as tmpdir:
            store1 = Store(tmpdir)
            store1.append_skill(skill_data0)

            loaded1 = store1.load_skills()
            self.assertEqual(len(loaded1), 1)
            self.assertEqual(loaded1[0]["id"], "skill_seg_0")
            self.assertEqual(loaded1[0]["intent"], "intent_seg_0")
            self.assertEqual(loaded1[0]["provenance"]["source"], "pipeline_segment_0")
            self.assertEqual(loaded1[0]["evidence"]["verified_by"], "replayer_verifier_v1")

            # Reopen store in fresh instance
            store2 = Store(tmpdir)
            loaded2 = store2.load_skills()
            self.assertEqual(len(loaded2), 1)
            self.assertEqual(loaded2[0]["id"], "skill_seg_0")
            self.assertEqual(loaded2[0]["intent"], "intent_seg_0")
            self.assertEqual(loaded2[0]["provenance"]["source"], "pipeline_segment_0")
            self.assertEqual(loaded2[0]["evidence"]["verified_by"], "replayer_verifier_v1")

    def test_mixed_outcomes_and_ppm_denominators(self):
        """Mix verified, unverified, error (mapper exception, labeler exception), and malformed replayer outputs."""
        # 5 segments needed
        frames = [build_ram_frame(16, 16, 0) for _ in range(25)]
        # Pair counts to generate 5 segments
        counts = [
            5, 5, 1, 1, 1,  # Seg 0 (start 0)
            5, 5, 1, 1, 1,  # Seg 1 (start 5)
            5, 5, 1, 1, 1,  # Seg 2 (start 10)
            5, 5, 1, 1, 1,  # Seg 3 (start 15)
            5, 5, 1, 1, 1,  # Seg 4 (start 20)
        ]
        engine = PresetEngine(counts)

        def labeler(seg, seg_frames):
            if seg["start"] == 15:
                raise ValueError("labeler error on seg 3")
            return "intent_%d" % seg["start"]

        def mapper(intent):
            if intent == "intent_10":
                raise ValueError("mapper error on seg 2")
            return [{"path": "ax", "action": "press_%s" % intent}]

        def replayer(actions, seg):
            if seg["start"] == 0:
                # Seg 0: verified
                return {"reproduced": True, "verified": True}
            elif seg["start"] == 5:
                # Seg 1: reproduced but unverified
                return {"reproduced": True, "verified": False}
            elif seg["start"] == 20:
                # Seg 4: malformed replayer return (not strict boolean)
                return {"reproduced": "yes", "verified": True}
            return {"reproduced": False, "verified": False}

        report = segmenter.run_pipeline(frames, engine, labeler=labeler, mapper=mapper, replayer=replayer)

        # 5 total segments: 1 verified, 1 unverified, 1 mapper error, 1 labeler error, 1 replayer error
        self.assertEqual(len(report["segments"]), 5)
        # Exactly 1 of 5 verified -> 200,000 ppm
        self.assertEqual(report["reproduced_share_ppm"], 200000)

        s0, s1, s2, s3, s4 = report["segments"]
        self.assertEqual((s0["reproduced"], s0["verified"], s0["error"]), (True, True, None))
        self.assertEqual((s1["reproduced"], s1["verified"], s1["error"]), (True, False, None))
        self.assertEqual((s2["reproduced"], s2["verified"], s2["error"]), (False, False, "mapper"))
        self.assertEqual((s3["reproduced"], s3["verified"], s3["error"]), (False, False, "labeler"))
        self.assertEqual((s4["reproduced"], s4["verified"], s4["error"]), (False, False, "replayer"))

        # Test single segment verified -> 1,000,000 ppm
        ppm_single = demonstrations.reproduced_share_ppm([{"reproduced": True, "verified": True}])
        self.assertEqual(ppm_single, 1000000)

        # Test 3 segments with 2 verified -> 666,666 ppm
        ppm_thirds = demonstrations.reproduced_share_ppm([
            {"reproduced": True, "verified": True},
            {"reproduced": True, "verified": True},
            {"reproduced": False, "verified": False},
        ])
        self.assertEqual(ppm_thirds, 666666)

    def test_mutation_isolation_later_metadata_edits_do_not_alter_earlier(self):
        """Mutating later skill dicts or caller metadata does not alter previously admitted or persisted skills."""
        skill1 = make_skill_dict(
            skill_id="skill_alpha",
            intent_name="initial_intent",
            actions=[{"path": "coordinates", "x": 10, "y": 20}],
            verifier="verifier_v1",
            source="source_v1",
            version=1,
        )

        admitted1 = json.loads(skills.admit(skill1))

        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(tmpdir)
            store.append_skill(skill1)

            # Mutate skill1 object locally
            skill1["intent"] = "MUTATED_INTENT"
            skill1["steps"][0]["x"] = 999
            skill1["provenance"]["source"] = "MUTATED_SOURCE"
            skill1["evidence"]["verified_by"] = "MUTATED_VERIFIER"

            # Check admitted1 snapshot remained intact
            self.assertEqual(admitted1["intent"], "initial_intent")
            self.assertEqual(admitted1["steps"][0]["x"], 10)
            self.assertEqual(admitted1["provenance"]["source"], "source_v1")
            self.assertEqual(admitted1["evidence"]["verified_by"], "verifier_v1")

            # Check store content remained intact
            persisted = store.load_skills()[0]
            self.assertEqual(persisted["intent"], "initial_intent")
            self.assertEqual(persisted["steps"][0]["x"], 10)
            self.assertEqual(persisted["provenance"]["source"], "source_v1")
            self.assertEqual(persisted["evidence"]["verified_by"], "verifier_v1")

    def test_demonstrations_stubs_raise_not_implemented_error(self):
        """Verify demonstration inference and replay stubs raise exact NotImplementedError boundaries."""
        with self.assertRaises(NotImplementedError) as cm:
            demonstrations.segment("video_data")
        self.assertEqual(str(cm.exception), "v0 stub: vision-engine segmentation")

        with self.assertRaises(NotImplementedError) as cm:
            demonstrations.label_intent(["frame0", "frame1"])
        self.assertEqual(str(cm.exception), "v0 stub: multimodal intent labelling")

        with self.assertRaises(NotImplementedError) as cm:
            demonstrations.map_to_actions("intent_text")
        self.assertEqual(str(cm.exception), "v0 stub: reasoner -> structured actions")

        with self.assertRaises(NotImplementedError) as cm:
            demonstrations.replay_and_verify([{"path": "api"}], {})
        self.assertEqual(str(cm.exception), "v0 stub: headless replay + verification")

    def test_defect_d_a8_10_automatic_segment_outcome_handoff(self):
        """Defect D-A8-10: skills.admit fails when passing raw segment outcome directly without caller translation."""
        frames = [build_ram_frame(16, 16, i) for i in range(6)]
        engine = PresetEngine([5, 5, 1, 1, 1])

        def labeler(seg_frames):
            return "auto_intent"

        def mapper(intent):
            return [{"path": "coordinates", "x": 5, "y": 5}]

        def replayer(actions, seg):
            return {"reproduced": True, "verified": True}

        report = segmenter.run_pipeline(frames, engine, labeler=labeler, mapper=mapper, replayer=replayer)
        verified_segment = report["segments"][0]

        # Expecting skills.admit(verified_segment) to succeed automatically without manual schema construction
        admitted_json = skills.admit(verified_segment)
        parsed = json.loads(admitted_json)
        self.assertEqual(parsed["evidence"]["reproduced"], True)


if __name__ == "__main__":
    unittest.main()
