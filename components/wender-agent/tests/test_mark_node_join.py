"""W4 predicates with planted negative fixtures and positive controls; RAM data only."""
import ast
import dataclasses
import itertools
import importlib.util
import os
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from wender.perceive import transform as tf
from wender.perceive import mark_node_join as join
from wender.state import schema


def _mark(mid=1, box=(0, 0, 100, 100), **extra):
    return dict(id=mid, **dict(zip(("x", "y", "w", "h"), box)), **extra)


def _node(nid="a", box=(0, 0, 100, 100), **extra):
    return dict(id=nid, box=list(box), actionable=True, role="button",
                states={"visible": True, "enabled": True}, **extra)


def _scene(dx=8, dy=84, jitter=(0, 0, 0, 0), scale=tf.DEFAULT_PPM):
    ns = [_node(str(i), (i * 300, 100, 100, 28)) for i in range(len(jitter))]
    ms = [_mark(i + 1, tf.closed_to_xywh(tf.css_box_to_closed_fb(n["box"], scale, dx + jitter[i], dy)))
          for i, n in enumerate(ns)]
    return ms, ns


def _label_data(nid="a"):
    return (dict(driver="keyboard", intent=dict(kind="focus-key", key="Return",
                 target=dict(role="button", anchor="/inbox"))),
            [dict(anchor="/inbox", role="button", actionable=True, state_node_id=nid)])


def _violations(source):
    tree = ast.parse(source)
    return [n for n in ast.walk(tree) if
            isinstance(n, ast.Constant) and isinstance(n.value, float) or
            isinstance(n, ast.Div) or
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("float", "round") or
            isinstance(n, (ast.Import, ast.ImportFrom)) and
            (getattr(n, "module", None) == "math" or any(a.name == "math" for a in n.names))]


# @anchor:wa.perceive.mark_node_join.tests.TestTransform
class TestTransform(unittest.TestCase):
    # @anchor:wa.perceive.mark_node_join.tests.TestTransform.test_css_to_fb_half_up_vs_bankers_round_and_exclusive_end
    def test_css_to_fb_half_up_vs_bankers_round_and_exclusive_end(self):
        self.assertEqual([tf.project_coord(i, 500_000) for i in (-3, -1, 1, 3)], [-1, 0, 1, 2])
        self.assertNotEqual(tf.project_coord(1, 500_000), round(1 * 0.5))
        # Endpoint projection: width is one; height is TWO at this scale.
        self.assertEqual(tf.css_box_to_closed_fb([1, 0, 1, 1], 1_500_000), (2, 0, 2, 1))
        self.assertNotEqual(tf.project_coord(1, 1_500_000) + tf.project_coord(1, 1_500_000) - 1, 2)
        for x in range(-10, 11):
            self.assertEqual(tf.css_box_to_closed_fb([x, x, 1, 1]), (x, x, x, x))
        for box in ([0, 0, 0, 1], [0, 0, 1, 0], [True, 0, 1, 1], [0, 0, 1.0, 1], None):
            self.assertIsNone(tf.xywh_to_closed(box))
            self.assertIsNone(tf.css_box_to_closed_fb(box))
        self.assertIsNone(tf.css_box_to_closed_fb([0, 0, 1, 1], 100_000))

    # @anchor:wa.perceive.mark_node_join.tests.TestTransform.test_exact_type_scale_and_coordinate_bounds
    def test_exact_type_scale_and_coordinate_bounds(self):
        for bad in (True, 1.0, "1", None):
            for fn in (tf.exact_int, tf.project_coord, tf.compose_scale_ppm):
                with self.assertRaises(ValueError):
                    fn(bad)
        self.assertEqual(tf.compose_scale_ppm(1_500_000, 2_000_000), 3_000_000)
        for args in ((100_000, 100_000), (16_000_000, 16_000_000), (0, 1_000_000)):
            with self.assertRaises(ValueError):
                tf.compose_scale_ppm(*args)
        with self.assertRaises(ValueError):
            tf.project_coord(tf.MAX_COORD + 1)
        self.assertEqual(tf.project_coord(tf.MAX_COORD), tf.MAX_COORD)

    # @anchor:wa.perceive.mark_node_join.tests.TestTransform.test_box_forms_closed_area_and_property_oracle
    def test_box_forms_closed_area_and_property_oracle(self):
        @dataclasses.dataclass
        class Box:
            x: int = 2
            y: int = 3
            w: int = 4
            h: int = 5
        for b in ([2, 3, 4, 5], (2, 3, 4, 5), dict(x=2, y=3, w=4, h=5),
                  dict(bbox=dict(x=2, y=3, w=4, h=5)), Box()):
            self.assertEqual(tf.xywh_to_closed(b), (2, 3, 5, 7))
        rng = random.Random(284)
        for _ in range(64):
            a, b = [[rng.randrange(-4, 5), rng.randrange(-4, 5), rng.randrange(1, 5), rng.randrange(1, 5)] for _ in range(2)]
            sets = [{(x, y) for x in range(c[0], c[0] + c[2]) for y in range(c[1], c[1] + c[3])} for c in (a, b)]
            self.assertEqual(tf.closed_overlap_and_union(tf.xywh_to_closed(a), tf.xywh_to_closed(b)),
                             (len(sets[0] & sets[1]), len(sets[0] | sets[1]), len(sets[0]), len(sets[1])))
            self.assertEqual(tf.closed_to_xywh(tf.xywh_to_closed(a)), a)
        self.assertIsNone(tf.closed_to_xywh([0, 0, -1, 0]))
        self.assertEqual(tf.closed_overlap_and_union(None, (0, 0, 0, 0)), (0, 0, 0, 0))

    # @anchor:wa.perceive.mark_node_join.tests.TestTransform.test_fixture_transform_support_below_3_or_residual_above_3px_coerced_to_identity_zero
    def test_fixture_transform_support_below_3_or_residual_above_3px_coerced_to_identity_zero(self):
        for jitter in ((0, 0), (-5, 0, 5)):
            ms, ns = _scene(4, 48, jitter)
            tx = tf.estimate_transform(ms, ns)
            self.assertEqual(tx["status"], "absent")
            self.assertIsNone(tx["dx"])
            self.assertIsNone(tx["dy"])
            pairs = join.assign_marks_nodes(ms, ns, tx)["pairs"]
            self.assertTrue(all(p["reason"] == "transform_absent" and p["node_id"] is None for p in pairs))
            self.assertTrue(all(not r["matched"] for r in join.join_marks_nodes(ms, ns, tx)["by_mark"]))
        self.assertEqual(tx["residual_px_num"], 25)
        with self.assertRaises(ValueError):
            tf.CoordTransform(status="absent", dx=0, dy=0)
        ms, ns = _scene(4, 48, (-1, 0, 1))
        tx = tf.estimate_transform(ms, ns)
        self.assertEqual((tx["dx"], tx["dy"], tx["support"], tx["residual_px_num"]), (4, 48, 3, 1))
        self.assertTrue(all(p["verdict"] == "matched" for p in join.assign_marks_nodes(ms, ns, tx)["pairs"]))

    # @anchor:wa.perceive.mark_node_join.tests.TestTransform.test_estimation_scale_bounds_cache_and_permutation
    def test_estimation_scale_bounds_cache_and_permutation(self):
        ms, ns = _scene(scale=1_500_000)
        cache = {}
        tx = tf.estimate_transform(ms, ns, dpr_ppm=1_500_000, cache=cache)
        self.assertEqual((tx["dx"], tx["dy"], tx["support"], tx["residual_px_num"]), (8, 84, 4, 0))
        self.assertEqual(tf.estimate_transform(ms[::-1], ns[::-1], scale_ppm=1_500_000), tx)
        hit = tf.estimate_transform([], [], scale_ppm=1_500_000, cache=cache)
        hit["dx"] = 99
        self.assertEqual(tf.estimate_transform([], [], scale_ppm=1_500_000, cache=cache), tx)
        self.assertEqual(tf.estimate_transform([], [], scale_ppm=1_500_000, epoch=1, cache=cache)["status"], "absent")
        for kwargs in (dict(dpr_ppm=0), dict(dx_bounds=(0, 1, 0)), dict(epoch=True)):
            with self.assertRaises(ValueError):
                tf.estimate_transform(ms, ns, **kwargs)
        # Refinement outside caller bounds cannot be certified.
        ms, ns = _scene(dx=12, dy=48)
        self.assertEqual(tf.estimate_transform(ms, ns)["reason"], "offset_out_of_bounds")
        self.assertEqual(tf.estimate_transform(ms, ns, dx_bounds=(-10, 12, 2))["status"], "certified")

    # @anchor:wa.perceive.mark_node_join.tests.TestTransform.test_transform_dataclass_and_null_propagation
    def test_transform_dataclass_and_null_propagation(self):
        valid = dict(status="certified", dx=0, dy=0, support=3, residual_px_num=0)
        tx = tf.CoordTransform(**valid)
        self.assertEqual(tf.translated_box([2, 3, 4, 5], tx), [2, 3, 4, 5])
        for change in (dict(support=2), dict(dx=True), dict(residual_px_num=10), dict(status="guess")):
            with self.assertRaises(ValueError):
                tf.CoordTransform(**(valid | change))
        for bad in (None, {}, dict(dx=None, dy=0), dict(dx=True, dy=0), dict(status="absent", dx=0, dy=0)):
            self.assertIsNone(tf.translated_box([2, 3, 4, 5], bad))
        self.assertEqual(tf.translated_box([2, 3, 4, 5], dict(dx=0, dy=0)), [2, 3, 4, 5])

    # @anchor:wa.perceive.mark_node_join.tests.TestTransform.test_hungarian_global_optimum_against_exhaustive_oracle
    def test_hungarian_global_optimum_against_exhaustive_oracle(self):
        rng = random.Random(984)
        self.assertEqual(tf._hungarian([[560, 520], [900, 0]])[0], [(0, 1), (1, 0)])
        self.assertEqual(tf._hungarian([])[0], [])
        for rows, cols in ((2, 3), (3, 2), (3, 3), (3, 0)):
            for _ in range(12):
                weights = [[rng.randrange(1001) for _ in range(cols)] for _ in range(rows)]
                padded = [w + [0] * rows for w in weights]
                oracle = max(sum(padded[i][j] for i, j in enumerate(perm))
                             for perm in itertools.permutations(range(cols + rows), rows))
                pairs = tf._hungarian(weights)[0]
                self.assertEqual(sum(weights[i][j] for i, j in pairs), oracle)
                self.assertEqual(len({j for i, j in pairs}), len(pairs))


# @anchor:wa.perceive.mark_node_join.tests.TestJoins
class TestJoins(unittest.TestCase):
    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_fixture_chrome_offset_dy84_greedy_mismatch_vs_hungarian_optimal_assignment
    def test_fixture_chrome_offset_dy84_greedy_mismatch_vs_hungarian_optimal_assignment(self):
        ms, ns = _scene()
        tx = tf.estimate_transform(ms, ns)
        self.assertEqual((tx["dx"], tx["dy"], tx["status"]), (8, 84, "certified"))
        result = join.assign_marks_nodes(ms, ns, tx)
        self.assertEqual([r["w_promille"] for r in result["pairs"]], [1000] * 4)
        self.assertTrue(all(r["verdict"] == "matched" for r in result["pairs"]))
        # An adversarial matrix defeats greedy; nonmutual optimal candidates
        # must still be rejected by the binding gate, rather than guessed.
        matrix = [[560, 520], [900, 0]]
        self.assertGreater(sum(matrix[i][j] for i, j in tf._hungarian(matrix)[0]), 900)
        ms, ns = [_mark(1), _mark(2)], [_node("a"), _node("b")]
        edges = {(i, j): (w, 1000, w, w >= 500) for i, row in enumerate(matrix) for j, w in enumerate(row)}
        with patch.object(join, "_evidence", return_value=edges):
            out = join.assign_marks_nodes(ms, ns)
        self.assertEqual([p["candidate_node_id"] for p in out["pairs"]], ["b", "a"])
        self.assertIsNone(out["pairs"][0]["node_id"])

    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_fixture_asymmetric_best_overlap_accepted_without_mutual_reverse_match
    def test_fixture_asymmetric_best_overlap_accepted_without_mutual_reverse_match(self):
        ms = [_mark(1, (0, 0, 10, 10))]
        target = _node("target", (0, 0, 8, 10))
        ns = [target, _node("other", (0, 0, 10, 10))]
        leg, elements = _label_data("target")
        joins = join.join_marks_nodes(ms, ns)
        self.assertEqual(join.label_from_osc2(leg, elements, ns, joins), (None, "conflicting_mark_node"))
        mutual = join.join_marks_nodes(ms, ns, require_mutual=True)
        row = next(r for r in mutual["by_node"] if r["node_id"] == "target")
        self.assertFalse(row["matched"])
        self.assertIsNone(row["mark_id"])
        out = join.join_marks_nodes(ms, [target])
        label, reason = join.label_from_osc2(leg, elements, [target], out)
        self.assertIsNone(reason)
        self.assertEqual(label, dict(kind="mark", mark_id=1, node_id="target", anchor="/inbox",
                                    iou_promille=800, iou_num=80, iou_den=100))

    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_fixture_sub_threshold_iou_049_promoted_to_matched_join_pair
    def test_fixture_sub_threshold_iou_049_promoted_to_matched_join_pair(self):
        ms, ns = [_mark(1, (0, 0, 100, 10))], [_node("a", (0, 0, 49, 10))]
        out = join.join_marks_nodes(ms, ns)
        for row, key in ((out["by_mark"][0], "node_id"), (out["by_node"][0], "mark_id")):
            self.assertFalse(row["matched"])
            self.assertIsNone(row[key])
            self.assertEqual(row["iou_promille"], 490)
        self.assertEqual(out["pairs"][0]["reason"], "iou_below_threshold")
        leg, elements = _label_data()
        self.assertEqual(join.label_from_osc2(leg, elements, ns, out)[1], "target_mark_iou_below_threshold")
        # 499 intersection pixels cannot be made by shifting two 30x25
        # rectangles; use an explicit primitive boundary fixture instead.
        with patch.object(tf, "closed_overlap_and_union", return_value=(499, 1001, 750, 750)):
            self.assertEqual(join.box_overlap_int([0, 0, 30, 25], [10, 0, 30, 25]), (499, 1001, 498, False))
        pos = join.assign_marks_nodes([_mark(1, (0, 0, 30, 25))], [_node("a", (10, 0, 30, 25))])["pairs"][0]
        self.assertEqual((pos["w_promille"], pos["verdict"]), (500, "matched"))

    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_fixture_duplicate_mark_ids_or_node_ids_silently_accepted_in_join
    def test_fixture_duplicate_mark_ids_or_node_ids_silently_accepted_in_join(self):
        m, n = _mark(), _node()
        for fn in (join.join_marks_nodes, join.assign_marks_nodes):
            for ms, ns in (([m, m | {"x": 50}], [n]), ([m], [n, n | {"box": [50, 0, 100, 100]}])):
                with self.assertRaisesRegex(ValueError, "duplicate_join_ids"):
                    fn(ms, ns)
            for mid, nid in ((True, "a"), (0, "a"), (1, ""), (1, None)):
                with self.assertRaisesRegex(ValueError, "invalid_join_id"):
                    fn([m | {"id": mid}], [n | {"id": nid}])
            out = fn([m, _mark(2, (200, 0, 100, 100))], [n, _node("b", (200, 0, 100, 100))])
            self.assertEqual([r["node_id"] for r in out["pairs"]], ["a", "b"])

    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_3part_ambiguity_gate_margins_swap_slack_and_symmetric_2x2_cycle
    def test_3part_ambiguity_gate_margins_swap_slack_and_symmetric_2x2_cycle(self):
        out = join.assign_marks_nodes([_mark(1), _mark(2)], [_node("a"), _node("b")])
        self.assertEqual(len(out["ambiguous_sets"]), 1)
        for r in out["pairs"]:
            self.assertEqual((r["verdict"], r["e2"], r["swap_slack"]), ("ambiguous", 0, 0))
            self.assertTrue(r["in_tight_cycle"])
            self.assertIsNone(r["node_id"])
        for width, reason in ((76, "margin_below_50"), (74, "swap_slack_le_100"), (60, None)):
            row = join.assign_marks_nodes([_mark()], [_node("a", (0, 0, 80, 100)),
                                                     _node("b", (0, 0, width, 100))])["pairs"][0]
            self.assertEqual(row["reason"], reason)
            self.assertEqual(row["competitors"], [dict(id="b", w_promille=width * 10)])
        for cross, expected in ((750, "ambiguous"), (600, "matched")):
            edges = {(i, j): (w, 1000, w, True) for i in range(2) for j in range(2)
                     for w in [800 if i == j else cross]}
            with patch.object(join, "_evidence", return_value=edges):
                rows = join.assign_marks_nodes([_mark(1), _mark(2)], [_node("a"), _node("b")])["pairs"]
            self.assertTrue(all(r["verdict"] == expected for r in rows))

    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_long_tight_cycle_and_disjoint_ambiguity_groups
    def test_long_tight_cycle_and_disjoint_ambiguity_groups(self):
        weights = [[1000, 1000, 0], [0, 1000, 1000], [1000, 0, 1000]]
        pairs, u, v = tf._hungarian(weights)
        self.assertEqual(join._cycle_members(pairs, weights, u, v), {0, 1, 2})
        weights = [[1000, 0], [0, 1000]]
        pairs, u, v = tf._hungarian(weights)
        self.assertEqual(join._cycle_members(pairs, weights, u, v), set())
        ms = [_mark(i + 1, (i // 2 * 300, 0, 100, 100)) for i in range(4)]
        ns = [_node(str(i), (i // 2 * 300, 0, 100, 100)) for i in range(4)]
        self.assertEqual(len(join.assign_marks_nodes(ms, ns)["ambiguous_sets"]), 2)

    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_dense_28px_rows_bijection_and_straddling_mark
    def test_dense_28px_rows_bijection_and_straddling_mark(self):
        ns = [_node(str(i), (0, 100 + i * 28, 100, 28)) for i in range(10)]
        ms = [_mark(i + 1, n["box"]) for i, n in enumerate(ns)]
        rows = join.assign_marks_nodes(ms, ns)["pairs"]
        self.assertEqual(len({r["node_id"] for r in rows}), 10)
        self.assertTrue(all(r["verdict"] == "matched" and r["margin_m"] == 1000 for r in rows))
        self.assertNotEqual(join.assign_marks_nodes([_mark(1, (0, 114, 100, 28))], ns)["pairs"][0]["verdict"], "matched")

    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_eligibility_role_rim_and_duck_objects
    def test_eligibility_role_rim_and_duck_objects(self):
        m = _mark(kind="button", score_promille=987)
        n = _node()
        for change in (dict(actionable=False), dict(states={"visible": False}),
                       dict(states={"enabled": "false"}), dict(role="textbox"), dict(box=None)):
            self.assertEqual(join.assign_marks_nodes([m], [n | change])["pairs"][0]["verdict"], "unmatched")
        from types import SimpleNamespace
        row = join.assign_marks_nodes([SimpleNamespace(**m)], [SimpleNamespace(**n)])["pairs"][0]
        self.assertEqual((row["verdict"], row["rim_promille"]), ("matched", 987))
        for score, expected in ((0.9, 0), (1, 1000), (True, 0)):
            self.assertEqual(join._rim_promille(_mark(score=score)), expected)
        with self.assertRaises(ValueError):
            join.assign_marks_nodes([_mark(rim_promille=1001)], [n])
        self.assertEqual(join.assign_marks_nodes([], [n])["unmatched_nodes"], ["a"])
        self.assertEqual(join.assign_marks_nodes([_mark()], [])["unmatched_marks"], [1])

    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_rational_best_not_promille_tie_and_determinism
    def test_rational_best_not_promille_tie_and_determinism(self):
        m = _mark(1, (0, 0, 10000, 1))
        ns = [_node("a", (0, 0, 8000, 1)), _node("b", (0, 0, 8001, 1))]
        self.assertEqual(join.join_marks_nodes([m], ns)["by_mark"][0]["node_id"], "b")
        ms, ns = _scene()
        tx = tf.estimate_transform(ms, ns)
        for fn in (join.join_marks_nodes, join.assign_marks_nodes):
            self.assertEqual(schema.dumps(fn(ms, ns, tx)), schema.dumps(fn(ms[::-1], ns[::-1], tx)))
        ms, ns = [_mark(1), _mark(2)], [_node("a"), _node("b")]
        self.assertEqual(schema.dumps(join.join_marks_nodes(ms, ns)), schema.dumps(join.join_marks_nodes(ms[::-1], ns[::-1])))

    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_zero_float_ast_and_canonical_json_determinism
    def test_zero_float_ast_and_canonical_json_determinism(self):
        for bad in ("x = 1 / 2", "y = 0.5", "x = float(1)", "x = round(1)", "import math"):
            self.assertTrue(_violations(bad))
        for module in (tf, join):
            self.assertEqual(_violations(Path(module.__file__).read_text()), [])
        ms, ns = [_mark(1), _mark(2)], [_node("a"), _node("b")]
        for fn in (join.assign_marks_nodes, join.join_marks_nodes):
            self.assertEqual(schema.dumps(fn(ms, ns)), schema.dumps(fn(ms[::-1], ns[::-1])))

    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_osc2_reference_parity
    @unittest.skipUnless(os.environ.get("W4_OSC2_REFERENCE"), "external OSC2 reference not supplied")
    def test_osc2_reference_parity(self):
        spec = importlib.util.spec_from_file_location("w4_osc2_reference", os.environ["W4_OSC2_REFERENCE"])
        bench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bench)
        ms, ns = _scene()
        expected, actual = bench.estimate_transform(ms, ns), tf.estimate_transform(ms, ns)
        for key in ("dx", "dy", "support", "source"):
            self.assertEqual(actual[key], expected[key])
        self.assertEqual(actual["residual_px_num"], expected["residual"] ** 2)
        for width in (49, 50, 80, 100):
            ms, ns = [_mark()], [_node("a", (0, 0, width, 100))]
            expected, actual = bench.join_marks_nodes(ms, ns), join.join_marks_nodes(ms, ns)
            for side in ("by_mark", "by_node"):
                self.assertEqual(actual[side][0]["matched"], expected[side][0]["matched"])
                self.assertEqual(actual[side][0]["iou_promille"], int(1000 * expected[side][0]["iou"]))
            if width < 50:
                self.assertIsNone(actual["by_mark"][0]["node_id"])
            else:
                self.assertEqual(actual["by_mark"][0]["node_id"], "a")

    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_summary_integer_interpolation_empty_and_invalid
    def test_summary_integer_interpolation_empty_and_invalid(self):
        self.assertEqual(join.iou_summary([]), dict(n=0, minimum=None, maximum=None, p50=None, p95=None,
                                                   zero=0, below_threshold=0, matched=0))
        self.assertEqual(join.iou_summary([0, 499, 500, 1000]), dict(n=4, minimum=0, maximum=1000,
                         p50=500, p95=925, zero=1, below_threshold=2, matched=2))
        for bad in ([True], [1.0], [-1], [1001]):
            with self.assertRaises(ValueError):
                join.iou_summary(bad)

    # @anchor:wa.perceive.mark_node_join.tests.TestJoins.test_label_failure_codes_and_nodes_control
    def test_label_failure_codes_and_nodes_control(self):
        leg, elements = _label_data()
        ns = [_node()]
        joins = join.join_marks_nodes([_mark()], ns)
        self.assertIsNone(join.label_from_osc2(leg, elements, ns, joins)[1])
        self.assertEqual(join.label_from_osc2(leg, elements, ns, mode="nodes")[0],
                         dict(kind="node", node_id="a", anchor="/inbox"))
        cases = [(None, elements, ns, "terminal_frame"),
                 (leg | {"intent": {"kind": "key"}}, elements, ns, "key_only"),
                 (leg | {"driver": "pointer"}, elements, ns, "lane_only"),
                 (leg | {"intent": {"kind": "click"}}, elements, ns, "unsupported_intent"),
                 (leg | {"intent": leg["intent"] | {"key": None}}, elements, ns, "focus_only_not_click"),
                 (leg | {"intent": leg["intent"] | {"key": "Space"}}, elements, ns, "unproven_activation_equivalence"),
                 (leg, [], ns, "anchor_absent"), (leg, elements * 2, ns, "ambiguous_anchor"),
                 (leg, [elements[0] | {"actionable": False}], ns, "target_element_not_actionable_or_role_mismatch"),
                 (leg, elements, [], "node_absent_or_ambiguous"),
                 (leg, elements, [ns[0] | {"actionable": False}], "target_node_not_enabled_actionable_visible"),
                 (leg, elements, [ns[0] | {"role": "link"}], "target_node_role_mismatch")]
        for ll, ee, nn, code in cases:
            self.assertEqual(join.label_from_osc2(ll, ee, nn, joins), (None, code))
        self.assertEqual(join.label_from_osc2(leg, elements, ns, {})[1], "target_mark_iou_below_threshold")
        broken = dict(joins, by_mark=[])
        self.assertEqual(join.label_from_osc2(leg, elements, ns, broken)[1], "conflicting_mark_node")


if __name__ == "__main__":
    unittest.main()
