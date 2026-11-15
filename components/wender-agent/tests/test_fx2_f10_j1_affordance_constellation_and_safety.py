import json
import unittest
from unittest.mock import patch
import numpy as np

# Import reference spec functions/helpers (sibling spec) as required by prompt
from tests.test_jq1_a5_affordance_constellation_spec import (
    canvas, paint_rect, paint_glyph, glyph_scene, pool_calibration, Anchor,
    box_constellation, constellation_bytes, pair_bytes, extract_anchors,
    bounded_pairs, match_templates, hash64, ABSTAIN, UNKNOWN
)
from wender.state import schema

# Import affordance module directly; if not yet implemented in this batch component,
# ModuleNotFoundError will be raised and caught by @unittest.expectedFailure.
try:
    from wender.perceive import affordance
except ImportError:
    affordance = None


class TestFx2F10J1AffordanceConstellationAndSafety(unittest.TestCase):

    def setUp(self):
        # High calibration fit for testing selected controls
        self.strong_fit = pool_calibration([(score, 1000, 0) for score in range(3, 9)])

    def _require_affordance(self):
        if affordance is None:
            raise ModuleNotFoundError("No module named 'wender.perceive.affordance'")
        return affordance

    def test_production_affordance_import(self):
        """Production affordance module must be importable."""
        aff = self._require_affordance()
        self.assertIsNotNone(aff)

    def test_reference_detect_and_to_state_schema_and_admission(self):
        """Exercise production reference_detect and to_state through schema validation, canonical serialization,
        and irreversible admission refusal.
        """
        aff = self._require_affordance()

        # Build RAM NumPy scene with a labeled checkbox
        rgb = canvas(160, 100)
        paint_rect(rgb, (10, 20, 26, 36))
        for x in (34, 46, 58):
            paint_glyph(rgb, x, 23)

        # Candidate proposals using reference detector / spec helpers
        proposals = aff.propose(rgb) if hasattr(aff, "propose") else []
        templates = {"shape0": box_constellation(rgb, (10, 20, 26, 36))}

        # Production reference_detect call
        detect_out = aff.reference_detect(
            rgb, templates=templates, action_fit=self.strong_fit, role_fit=self.strong_fit
        )
        self.assertEqual(detect_out.get("schema"), "jq1-a5-affordance-reference/v1")
        self.assertTrue(len(detect_out.get("proposals", [])) > 0)

        # Check checkbox proposal selection
        checkbox_proposals = [p for p in detect_out["proposals"] if p["decision"] == "checkbox"]
        self.assertEqual(len(checkbox_proposals), 1)
        cb_prop = checkbox_proposals[0]

        # Verify decision fields
        self.assertIsNone(cb_prop.get("enabled"))
        self.assertEqual(cb_prop.get("provenance"), "vision")
        self.assertEqual(cb_prop.get("corroborated"), 0)
        self.assertEqual(cb_prop.get("irreversible_admitted"), 0)
        self.assertNotIn(cb_prop.get("level"), ("L0_EXACT", "L1_STABLE"))

        # Test irreversible_allowed admission control
        self.assertFalse(aff.irreversible_allowed(cb_prop))

        # Convert detector envelope to STATE using to_state
        state = aff.to_state(
            detect_out,
            url="file:///ram_scene",
            title="RAM Scene",
            viewport={"w": 160, "h": 100},
            prune={"name": "vision_prune_v1", "version": 1}
        )

        # Validate schema and canonical dumps
        errs = schema.validate_state(state)
        self.assertEqual(errs, [], f"Schema validation errors: {errs}")
        self.assertEqual(state["source"]["kind"], "vision")

        # Canonical serialization check (float-free)
        dumped = schema.dumps(state)
        self.assertIsInstance(dumped, str)

        # All vision STATE nodes must remain nonactionable
        for node in state["nodes"]:
            self.assertFalse(node["actionable"])
        self.assertEqual(state["actionable"], [])

    def test_pixel_identical_scenes_differing_hidden_enabled(self):
        """Compare pixel-identical scenes with differing hidden enabled labels under identical explicit calibration:
        decisions must match, enabled remains unknown (None) and all vision STATE nodes remain nonactionable.
        """
        aff = self._require_affordance()

        # Construct two pixel-identical RAM frames representing "enabled" and "disabled" controls
        frame_enabled, frame_disabled = canvas(), canvas()
        for f in (frame_enabled, frame_disabled):
            paint_rect(f, (10, 10, 70, 40))

        self.assertEqual(frame_enabled.tobytes(), frame_disabled.tobytes())

        res1 = aff.reference_detect(frame_enabled, action_fit=self.strong_fit, role_fit=self.strong_fit)
        res2 = aff.reference_detect(frame_disabled, action_fit=self.strong_fit, role_fit=self.strong_fit)

        # Canonical JSON outputs must be identical
        self.assertEqual(schema.dumps(res1), schema.dumps(res2))

        # Decisions must match and enabled remains None
        for p1, p2 in zip(res1["proposals"], res2["proposals"]):
            self.assertEqual(p1["decision"], p2["decision"])
            self.assertIsNone(p1.get("enabled"))
            self.assertIsNone(p2.get("enabled"))

        # Check to_state conversion for both
        st1 = aff.to_state(res1, url="file:///test", title="Test", viewport={"w": 160, "h": 100}, prune={"name": "p", "version": 1})
        st2 = aff.to_state(res2, url="file:///test", title="Test", viewport={"w": 160, "h": 100}, prune={"name": "p", "version": 1})

        self.assertEqual(schema.dumps(st1), schema.dumps(st2))
        for st in (st1, st2):
            self.assertTrue(all(not n["actionable"] for n in st["nodes"]))
            self.assertEqual(st["actionable"], [])

    def test_repeated_glyph_independent_group_counting(self):
        """Test repeated-glyph independent-group counting vs distinct glyphs."""
        repeated = extract_anchors(glyph_scene(repeated=True))
        self.assertEqual(len(repeated), 64)
        # All 64 repeated glyphs share 1 quantized descriptor group
        self.assertEqual(len({(a.kind, tuple(a.d48[:32]) + tuple(v // 16 for v in a.d48[32:])) for a in repeated}), 1)

        # Matching repeated grid against template of 5 yields only 1 vote
        rep_match = match_templates({"grid": repeated[:5]}, repeated)
        self.assertEqual(rep_match["best"]["votes"], 1)

        # 5 distinct glyph anchors yield 5 distinct groups and 5 votes when matched
        distinct = extract_anchors(glyph_scene())
        self.assertEqual(len(distinct), 5)
        distinct_match = match_templates({"distinct": distinct}, distinct)
        self.assertEqual(distinct_match["best"]["votes"], 5)
        self.assertGreater(distinct_match["best"]["votes"], rep_match["best"]["votes"])

    def test_forced_unequal_payload_pair_hash_collision(self):
        """Test forced unequal-payload pair-hash collisions verifying exact byte equality after hash lookup."""
        d0 = (0,) * 48
        d1 = (1,) + (0,) * 47

        # Create two node tuples with different payloads
        anchors_a = tuple(Anchor(5, i * 5, i * 9, (i,) + (0,) * 47) for i in range(5))
        anchors_b = tuple(Anchor(5, i * 5, i * 9, (i + 6,) + (0,) * 47) for i in range(5))

        # Force hash64 to return constant collision "0" * 16
        with patch("tests.test_jq1_a5_affordance_constellation_spec.hash64", return_value="0" * 16):
            # Despite hash collision, payload mismatch prevents false match
            match_res = match_templates({"tmpl": anchors_a}, anchors_b)
            self.assertIsNone(match_res["best"])

    def test_distinct_landmark_recovery_and_anchor_order_invariance(self):
        """Test distinct-landmark recovery (5 landmark nodes) and anchor-order / permutation invariance."""
        rgb = canvas()
        paint_rect(rgb, (20, 20, 90, 62))
        nodes = box_constellation(rgb, (20, 20, 90, 62))

        # 5 distinct landmark nodes recovered
        self.assertEqual(len(nodes), 5)
        self.assertEqual([(n.x, n.y, n.kind) for n in nodes],
                         [(20, 20, 1), (90, 20, 1), (55, 41, 2), (20, 62, 1), (90, 62, 1)])

        # Anchor order / permutation invariance
        base_payload = constellation_bytes(nodes)
        import itertools
        for perm in itertools.permutations(nodes):
            self.assertEqual(constellation_bytes(perm), base_payload)


if __name__ == "__main__":
    unittest.main()
