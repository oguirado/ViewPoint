"""Tests for eval/p1_region_nodes.py: P1 Computer Use fusion mapping."""
import json
import unittest

import numpy as np

from eval import p1_region_nodes as P
from visioncore import layout, regions as R
from visioncore.change import TILE_DT

W, H = 1280, 720  # tile grid size (s16, 80x45)


def mk_tiles(cells, W=W, H=H):
    """Build synthetic tile array in RAM from dict of (tx, ty) -> properties."""
    items = sorted(((layout.morton(x, y), v) for (x, y), v in cells.items()))
    t = np.zeros(len(items), TILE_DT)
    for idx, (m, v) in enumerate(items):
        t[idx]["morton"] = m
        t[idx]["cls"] = v.get("cls", 2)  # 2 = CHANGED
        t[idx]["flags"] = v.get("flags", 0)
        t[idx]["resid"] = v.get("resid", 10)
        t[idx]["mv_dx"] = v.get("dx", 0)
        t[idx]["mv_dy"] = v.get("dy", 0)
        t[idx]["edge"] = v.get("edge", 0)
    return t


# @anchor:va.test_p1_region_nodes.TestP1RegionNodes
class TestP1RegionNodes(unittest.TestCase):
    def test_touching_and_one_pixel_apart(self):
        """Cover Rule R10: touching boxes sharing one pixel edge count as overlap,

        boxes one pixel apart do not.
        """
        # Single tile region at (tx=1, ty=1) with tile size s=16 -> bbox_px = [16, 16, 16, 16]
        # Closed region box: [16, 16, 31, 31]
        tiles = mk_tiles({(1, 1): {"cls": 2}})
        regs = R.regions_of(tiles, W, H, frame=0)
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0]["bbox_px"], [16, 16, 16, 16])

        nodes = [
            {"i": 0, "box": [31, 16, 50, 31], "act": False},  # touches at x=31 (1 pixel overlap)
            {"i": 1, "box": [32, 16, 50, 31], "act": False},  # 1 pixel apart at x=32 (no overlap)
        ]

        res = P.fuse(regs, nodes)
        mapped = res["regions"]
        self.assertEqual(len(mapped), 1)
        self.assertEqual(len(mapped[0]["nodes"]), 1)
        self.assertEqual(mapped[0]["nodes"][0]["i"], 0)
        self.assertEqual(mapped[0]["nodes"][0]["overlap_area"], 16)  # x: [31,31] (1px), y: [16,31] (16px) -> 1 * 16 = 16

    def test_exclusive_end_conversion(self):
        """Cover exclusive-end conversion of region bbox_px [x, y, w, h]."""
        # Hand-crafted region row with bbox_px = [100, 200, 50, 40]
        # Closed region box: [100, 200, 149, 239]
        reg = {
            "id": "r0:0001",
            "cls": "CHANGED",
            "bbox_px": [100, 200, 50, 40],
        }

        nodes = [
            {"i": 0, "box": [149, 239, 160, 250], "act": True},  # overlaps exactly at (149, 239)
            {"i": 1, "box": [150, 240, 160, 250], "act": True},  # outside by 1px
        ]

        res = P.fuse([reg], nodes)
        mapped = res["regions"]
        self.assertEqual(len(mapped), 1)
        self.assertEqual(len(mapped[0]["nodes"]), 1)
        self.assertEqual(mapped[0]["nodes"][0]["i"], 0)
        self.assertEqual(mapped[0]["nodes"][0]["overlap_area"], 1)

    def test_button_and_parent_div_ranking(self):
        """Cover: region covering a button and parent div ranks button first among actionable nodes,

        and flags the smallest actionable node.
        """
        # Region covering button area (closed box [10, 10, 20, 20], w=11, h=11, area=121)
        reg = {
            "id": "r0:0002",
            "cls": "CHANGED",
            "bbox_px": [10, 10, 11, 11],  # closed [10, 10, 20, 20]
        }

        # Parent div (large) and Button (small)
        # Parent div closed box [0, 0, 100, 100], area = 101*101 = 10201
        # Button closed box [10, 10, 20, 20], area = 11*11 = 121
        nodes = [
            {"i": 0, "box": [0, 0, 100, 100], "act": True},   # Parent div (actionable)
            {"i": 1, "box": [10, 10, 20, 20], "act": True},  # Button (actionable)
        ]

        res = P.fuse([reg], nodes)
        mapped = res["regions"]
        self.assertEqual(len(mapped), 1)
        top_nodes = mapped[0]["nodes"]
        # Overlap area for div is 121, overlap area for button is 121.
        # Secondary sort key (node_box_area asc) puts button (121) before div (10201).
        self.assertEqual(top_nodes[0]["i"], 1)
        self.assertEqual(top_nodes[1]["i"], 0)
        self.assertEqual(mapped[0]["smallest_actionable_node_i"], 1)
        self.assertTrue(top_nodes[0]["smallest_actionable"])
        self.assertFalse(top_nodes[1]["smallest_actionable"])

    def test_visual_only_and_dom_only(self):
        """Cover visual_only and dom_only list logic."""
        reg_visual = {
            "id": "r0:0010",
            "cls": "CHANGED",
            "bbox_px": [0, 0, 10, 10],  # closed [0, 0, 9, 9]
        }
        reg_dom = {
            "id": "r0:0011",
            "cls": "CHANGED",
            "bbox_px": [100, 100, 20, 20],  # closed [100, 100, 119, 119]
        }

        nodes = [
            {"i": 5, "box": [100, 100, 110, 110], "act": False},  # overlaps reg_dom
            {"i": 8, "box": [500, 500, 520, 520], "act": False},  # no region overlaps
        ]

        # dom_changed_nodes specifies node 5 and 8
        res = P.fuse([reg_visual, reg_dom], nodes, dom_changed_nodes=[5, 8])

        self.assertEqual(len(res["regions"]), 1)
        self.assertEqual(res["regions"][0]["id"], "r0:0011")

        # reg_visual has no overlapping node -> visual_only
        self.assertEqual(len(res["visual_only"]), 1)
        self.assertEqual(res["visual_only"][0]["id"], "r0:0010")

        # node 8 is in dom_changed_nodes but has no overlapping changed region -> dom_only
        self.assertEqual(res["dom_only"], [8])

    def test_ties_broken_by_i(self):
        """Cover tie breaking by node index i when overlap area and node box area are equal."""
        reg = {
            "id": "r0:0020",
            "cls": "CHANGED",
            "bbox_px": [0, 0, 100, 100],
        }

        # Two identical nodes with same box and act, differing only in i
        nodes = [
            {"i": 42, "box": [10, 10, 20, 20], "act": True},
            {"i": 7, "box": [10, 10, 20, 20], "act": True},
        ]

        res = P.fuse([reg], nodes)
        top_nodes = res["regions"][0]["nodes"]
        self.assertEqual([n["i"] for n in top_nodes], [7, 42])

    def test_large_coordinates_near_2_to_20(self):
        """Cover coordinates near 2^20 (1,048,576) ensuring no float conversion or integer overflow."""
        base = 1 << 20  # 1048576
        reg = {
            "id": "r0:0030",
            "cls": "CHANGED",
            "bbox_px": [base, base, 1000, 1000],  # closed [1048576, 1048576, 1049575, 1049575]
        }

        nodes = [
            {"i": 0, "box": [base, base, base + 999, base + 999], "act": True},  # exact match
            {"i": 1, "box": [base + 500, base + 500, base + 1499, base + 1499], "act": False},  # partial match
        ]

        res = P.fuse([reg], nodes)
        top_nodes = res["regions"][0]["nodes"]
        self.assertEqual(top_nodes[0]["i"], 0)
        self.assertEqual(top_nodes[0]["overlap_area"], 1000000)
        self.assertEqual(top_nodes[0]["node_box_area"], 1000000)

        # Confirm JSON serializability
        serialized = json.dumps(res, sort_keys=True)
        self.assertIsInstance(serialized, str)


    # @anchor:va.test_p1_region_nodes.TestP1RegionNodes.test_competing_overlaps_increase_frustration
    def test_competing_overlaps_increase_frustration(self):
        reg = {"id": "r0:0100", "cls": "CHANGED", "bbox_px": [10, 10, 20, 20]}
        base = {"i": 1, "box": [10, 10, 29, 29], "act": True}
        cases = [[base], [base, {"i": 2, "box": [10, 10, 29, 19]}],
                 [base, {"i": 2, "box": [10, 10, 29, 28]}, {"i": 3, "box": base["box"]}]]
        envs = [P.fuse_envelope([reg], nodes) for nodes in cases]
        self.assertEqual(envs[0]["frustration_promille"], 0)
        self.assertEqual(envs[0]["e2"]["energy"], 0)
        self.assertTrue(envs[0]["passed_gate"])
        for env in envs[1:]:
            self.assertGreaterEqual(env["frustration_promille"], 500)
            self.assertGreater(env["e2"]["energy"], 0)
            self.assertGreater(env["e2"]["competing_overlap_energy"], 0)
            self.assertGreater(env["region_bindings"][0]["frustration_promille"], 0)
            self.assertGreater(env["region_bindings"][0]["e2"], 0)
            self.assertFalse(env["passed_gate"])
            self.assertIs(type(env["e2"]["energy"]), int)
            # A zero-evidence mutant must fail the same predicate.
            with self.assertRaises(AssertionError):
                self.assertGreater(dict(env, frustration_promille=0)["frustration_promille"], 0)
            with self.assertRaises(AssertionError):
                self.assertGreater(dict(env["e2"], energy=0)["energy"], 0)
        self.assertGreater(envs[2]["frustration_promille"], envs[1]["frustration_promille"])

    # @anchor:va.test_p1_region_nodes.TestP1RegionNodes.test_boundary_off_by_one_closed_vs_half_open
    def test_boundary_off_by_one_closed_vs_half_open(self):
        regs = [{"id": str(i), "cls": "CHANGED", "bbox_px": [i*10, 0, 10, 10]} for i in range(2)]
        self.assertEqual(P._region_to_closed_box([0, 0, 10, 10]), (0, 0, 9, 9))
        self.assertEqual(P._region_to_closed_fb([10, 0, 10, 10]), (10, 0, 19, 9))
        self.assertFalse(P._boxes_overlap(0, 0, 9, 9, 10, 0, 19, 9))
        self.assertEqual(P._overlap_area(0, 0, 9, 9, 10, 0, 19, 9), 0)
        for mode in ("box", "bbox_px"):
            nodes = [{"i": i, mode: [i*10, 0, i*10+9, 9] if mode == "box" else [i*10, 0, 10, 10]} for i in range(2)]
            env = P.fuse_envelope(regs, nodes)
            self.assertTrue(env["passed_gate"])
            self.assertEqual([b["assigned_node_i"] for b in env["region_bindings"]], [0, 1])
            self.assertTrue(all(b["competitors"] == [] and b["frustration_promille"] == 0 for b in env["region_bindings"]))
        self.assertEqual(P._overlap_area(0, 0, 10, 10, 10, 0, 20, 10), 11)
        with self.assertRaises(AssertionError):
            self.assertEqual(P._overlap_area(0, 0, 10, 10, 10, 0, 20, 10), 0)
        self.assertEqual(P._overlap_area(0, 0, 10, 9, 10, 0, 19, 9), 10)

    # @anchor:va.test_p1_region_nodes.TestP1RegionNodes.test_hungarian_ambiguity_gate_and_dpr_ppm_projection
    def test_hungarian_ambiguity_gate_and_dpr_ppm_projection(self):
        import ast
        import inspect
        from unittest.mock import patch
        from vadapt import jq3_chromium_pixels as cpx
        mark = {"id": 1, "bbox_px": [0, 0, 30, 25]}
        accepted = P.assign_marks_nodes([mark], [{"i": 1, "bbox_px": [10, 0, 30, 25]}])
        self.assertEqual(accepted["pairs"][0]["w_promille"], 500)
        self.assertEqual(accepted["pairs"][0]["verdict"], "matched")
        rejected = P.assign_marks_nodes([dict(mark, bbox_px=[0, 0, 750, 1])], [{"i": 1, "bbox_px": [251, 0, 750, 1]}])
        self.assertEqual(rejected["pairs"][0]["w_promille"], 498)
        self.assertEqual(rejected["matched_pairs"], [])
        mark = dict(mark, bbox_px=[0, 0, 100, 10])
        for width, verdict in ((76, "ambiguous"), (74, "matched")):
            joined = P.assign_marks_nodes([mark], [{"i": 1, "bbox_px": [0, 0, 80, 10]}, {"i": 2, "bbox_px": [0, 0, width, 10]}])
            self.assertEqual(joined["pairs"][0]["verdict"], verdict)
            if verdict == "ambiguous":
                self.assertEqual(joined["pairs"][0]["reason"], "margin_m_lt_50")
                self.assertEqual(joined["ambiguous_sets"][0]["kind"], "AMBIGUOUS_SET")
        column = P.assign_marks_nodes([dict(mark, bbox_px=[0, 0, 80, 10]), {"id": 2, "bbox_px": [0, 0, 76, 10]}], [{"i": 1, "bbox_px": [0, 0, 100, 10]}])
        self.assertEqual(column["pairs"][0]["reason"], "margin_n_lt_50")
        marks = [{"id": i, "bbox_px": [i, 0, 1, 1]} for i in (1, 2)]
        nodes = [{"i": i, "bbox_px": [i, 0, 1, 1]} for i in (1, 2)]
        # Independent exact overlap fixture: area 35150 gives IoUs 900 and 850.
        with patch.object(P, "_area", return_value=35150), patch.object(P, "_overlap_area", side_effect=lambda *b: 33300 if b[0] == b[4] else 32300):
            swap = P.assign_marks_nodes(marks, nodes)
        self.assertEqual([p["swap_slack"] for p in swap["pairs"]], [100, 100])
        self.assertEqual([p["reason"] for p in swap["pairs"]], ["swap_slack_le_100"]*2)
        self.assertEqual(swap["matched_pairs"], [])
        marks = [dict(m, bbox_px=[0, 0, 20, 20]) for m in marks]
        nodes = [dict(n, bbox_px=[0, 0, 20, 20]) for n in nodes]
        tied = P.assign_marks_nodes(marks, nodes)
        self.assertEqual(tied["matched_pairs"], [])
        self.assertTrue(all(p["in_tight_cycle"] for p in tied["pairs"]))
        self.assertEqual(tied["ambiguous_sets"][0]["mark_ids"], [1, 2])
        self.assertEqual(tied["ambiguous_sets"][0]["node_ids"], [1, 2])
        for module in (P, cpx):
            self.assertEqual([module.project_css_coord_to_fb(c, 500_000) for c in (1, 3, 5)], [1, 2, 3])
            self.assertNotEqual([round(c * 0.5) for c in (1, 3, 5)], [1, 2, 3])
            huge = (1 << 53)+1
            self.assertEqual(module.project_css_coord_to_fb(huge), huge)
            self.assertNotEqual(int((huge*1_000_000+500_000)/1_000_000), huge)
            self.assertEqual(module.css_box_to_closed_fb([1, 0, 1, 10], 1_500_000), (2, 0, 2, 14))
            tree = ast.parse(inspect.getsource(module))
            self.assertFalse(any(isinstance(n, ast.Div) or isinstance(n, ast.Constant) and type(n.value) is float or isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("float", "round") for n in ast.walk(tree)))
        snapshot = cpx.PresentationTreeSnapshot(1, "env-1", 800, 600, 1_500_000, nodes=({"i": 0, "bbox_px": [1, 0, 1, 10]},))
        self.assertEqual(snapshot.scale_ppm, 1_500_000)
        self.assertEqual(snapshot.project_nodes_to_fb()[0]["box"], [2, 0, 2, 14])

    # @anchor:va.test_p1_region_nodes.TestP1RegionNodes.test_totality_projection_validation_and_determinism
    def test_totality_projection_validation_and_determinism(self):
        from vadapt import jq3_chromium_pixels as cpx
        regs = [{"id": str(i), "cls": "CHANGED", "bbox_px": [i*30, 0, 10, 10]} for i in range(3)]
        nodes = [{"i": i, "bbox_px": r["bbox_px"], "act": True} for i, r in enumerate(regs)]
        self.assertFalse(P.fuse_envelope([], [])["passed_gate"])
        positive = P.fuse_envelope(regs, nodes)
        self.assertTrue(positive["passed_gate"])
        self.assertEqual({k: positive[k] for k in ("regions", "visual_only", "dom_only")}, P.fuse(regs, nodes))
        self.assertEqual(positive, P.fuse(regs, nodes, include_envelope=True))
        for options in ({"transform_status": "absent"}, {"dx": None}, {"dy": None}):
            absent = P.fuse_envelope(regs, nodes, **options)
            self.assertEqual(absent["regions"], [])
            self.assertEqual(len(absent["orphan_regions"]), 3)
            self.assertTrue(all(r["reason"] == "transform_absent" for r in absent["orphan_regions"]))
            self.assertFalse(absent["passed_gate"])
            self.assertGreater(absent["e2"]["energy"], 0)
        dom = P.fuse_envelope(regs, nodes, dom_changed_nodes=[99])
        self.assertEqual(dom["e2"]["n_unmatched_dom_changed"], 1)
        self.assertFalse(dom["passed_gate"])
        for ms, ns in ((regs+regs[:1], nodes), (regs, nodes+nodes[:1])):
            with self.assertRaisesRegex(ValueError, "duplicate_join_ids"):
                P.assign_marks_nodes(ms, ns)
        self.assertEqual(json.dumps(P.assign_marks_nodes(regs, nodes), sort_keys=True), json.dumps(P.assign_marks_nodes(regs[::-1], nodes[::-1]), sort_keys=True))
        for module in (P, cpx):
            self.assertEqual(module.compose_scale_ppm(1_500_000, 2_000_000), 3_000_000)
            self.assertIsNone(module.css_box_to_closed_fb([0, 0, 0, 1]))
            self.assertIsNone(module.css_box_to_closed_fb([0, 0, 1, 1], 100_000))
            for bad in (True, 1.5, "2"):
                with self.assertRaises((TypeError, ValueError, cpx.ChromiumPixelError)):
                    module.project_css_coord_to_fb(bad)
                with self.assertRaises((TypeError, ValueError, cpx.ChromiumPixelError)):
                    module.css_box_to_closed_fb([0, 0, bad, 10])
            for scale in (99_999, 16_000_001, True):
                with self.assertRaises((TypeError, ValueError, cpx.ChromiumPixelError)):
                    module.compose_scale_ppm(scale)
        with self.assertRaises(TypeError):
            P.fuse([dict(regs[0], bbox_px=[True, 0, 10, 10])], nodes)
        for n in ({"i": 1, "box": [0, 0, 9, 9]}, {"i": 1, "x0": 0, "y0": 0, "x1": 9, "y1": 9}):
            self.assertEqual(P._parse_node(n, scale_ppm=2_000_000, dx=3, dy=4)[1], (3, 4, 22, 23))
        projected = cpx.project_tree_nodes_to_fb([{"i": 1, "box": [0, 0, 9, 9]}], dpr_ppm=2_000_000, dx=3, dy=4)
        self.assertEqual(projected[0]["box"], [3, 4, 22, 23])

    # @anchor:va.test_p1_region_nodes.TestP1RegionNodes.test_rectangular_solver_matches_exhaustive_oracle
    def test_rectangular_solver_matches_exhaustive_oracle(self):
        import itertools
        import random
        rng = random.Random(712)
        for rows, cols in ((1, 2), (2, 4), (3, 5), (4, 6)):
            for _ in range(12):
                weights = [[rng.randrange(0, 1001) for _ in range(cols)] for _ in range(rows)]
                assignment, u, v = P._hungarian(weights)
                optimum = max(sum(weights[i][j] for i, j in enumerate(js)) for js in itertools.permutations(range(cols), rows))
                self.assertEqual(sum(weights[i][j] for i, j in enumerate(assignment)), optimum)
                self.assertTrue(all(-weights[i][j] >= u[i]+v[j] for i in range(rows) for j in range(cols)))
                self.assertTrue(all(-weights[i][j] == u[i]+v[j] for i, j in enumerate(assignment)))

    # @anchor:va.test_p1_region_nodes.TestP1RegionNodes.test_dense_rows_and_tiny_competitors
    def test_dense_rows_and_tiny_competitors(self):
        regs = [{"id": i, "cls": "CHANGED", "bbox_px": [0, 28*i, 100, 20]} for i in range(24)]
        nodes = [{"i": i, "bbox_px": r["bbox_px"]} for i, r in enumerate(regs)]
        joined = P.assign_marks_nodes(regs, nodes)
        self.assertTrue(joined["passed_gate"])
        ids = [p["node_id"] for p in joined["matched_pairs"]]
        self.assertEqual(ids, list(range(24)))
        self.assertEqual(len(ids), len(set(ids)))
        # A straddling mark has no admissible edge; the gate must not guess a row.
        straddled = dict(regs[0], bbox_px=[0, 10, 100, 36])
        bad = P.assign_marks_nodes([straddled], nodes)
        self.assertEqual(bad["matched_pairs"], [])
        self.assertFalse(bad["passed_gate"])
        # A positive overlap that truncates to zero still contributes residual energy.
        tiny = P.fuse_envelope(regs[:1], nodes[:1]+[{"i": 99, "bbox_px": [99, 19, 100000, 100000]}])
        self.assertGreater(tiny["frustration_promille"], 0)
        self.assertGreater(tiny["e2"]["competing_overlap_energy"], 0)
        # The global penalty denominator includes all DOM changes, including matched ones.
        dom = P.fuse_envelope(regs[:1], nodes[:1], dom_changed_nodes=[0, 99])
        self.assertEqual(dom["frustration_promille"], 333)
        for bad_options in ({"scale_ppm": True}, {"dx": True}, {"dy": 1.5}):
            with self.assertRaises(TypeError):
                P.assign_marks_nodes([], [], **bad_options)

    # @anchor:va.test_p1_region_nodes.TestP1RegionNodes.test_orphan_routing_matches_join_evidence
    def test_orphan_routing_matches_join_evidence(self):
        reg = {"id": "r", "cls": "CHANGED", "bbox_px": [0, 0, 750, 1]}
        node = {"i": 1, "bbox_px": [251, 0, 750, 1]}

        def consistent(env):
            return (len(env["orphan_regions"]) == env["e2"]["n_orphan_regions"]
                    and sum(b["verdict"] == "ambiguous" for b in env["region_bindings"])
                    == env["e2"]["n_ambiguous"])

        rejected = P.fuse_envelope([reg], [node])
        self.assertTrue(consistent(rejected))
        self.assertEqual(rejected["region_bindings"], [])
        self.assertEqual(rejected["orphan_regions"][0]["reason"], "below_iou_gate")
        self.assertFalse(rejected["passed_gate"])
        mutant = dict(rejected, orphan_regions=[], region_bindings=[{"verdict": "ambiguous"}])
        self.assertFalse(consistent(mutant))
        accepted = P.fuse_envelope([reg], [dict(node, bbox_px=reg["bbox_px"])])
        self.assertTrue(consistent(accepted))
        self.assertTrue(accepted["passed_gate"])
        ambiguous = P.fuse_envelope([reg], [dict(node, bbox_px=reg["bbox_px"]),
                                          {"i": 2, "bbox_px": reg["bbox_px"]}])
        self.assertTrue(consistent(ambiguous))
        self.assertEqual(ambiguous["e2"]["n_ambiguous"], 1)

    # @anchor:va.test_p1_region_nodes.TestP1RegionNodes.test_canonical_tight_paths_against_exhaustive_oracle
    def test_canonical_tight_paths_against_exhaustive_oracle(self):
        import itertools
        import random
        rng = random.Random(513)
        fixtures = [[[1000, 1000, 0], [1000, 1000, 0]],
                    [[500, 900, 0], [500, 900, 0]],
                    [[600, 900, 0], [500, 800, 0]]]
        for rows, cols in ((1, 3), (2, 4), (3, 5), (4, 6)):
            fixtures.extend([[rng.choice((0, 500, 600, 1000)) for _ in range(cols)]
                             for _ in range(rows)] for _ in range(40))
        for weights in fixtures:
            choices = list(itertools.permutations(range(len(weights[0])), len(weights)))
            score = lambda js: sum(weights[i][j] for i, j in enumerate(js))
            optimum = max(map(score, choices))
            expected = min(js for js in choices if score(js) == optimum)
            assignment, u, v = P._hungarian(weights)
            canonical = P._canonical_assignment(weights, assignment, u, v)
            self.assertEqual(tuple(canonical), expected)
            self.assertTrue(all(-w >= u[i]+v[j] for i, row in enumerate(weights)
                                for j, w in enumerate(row)))
            self.assertTrue(all(-weights[i][j] == u[i]+v[j] for i, j in enumerate(canonical)))
        tied = fixtures[0]
        self.assertEqual(P._canonical_assignment(tied, [1, 0], [-1000]*2, [0]*3), [0, 1])
        # A reversed optimum is a must-FAIL fixture for the canonical predicate.
        self.assertFalse(tuple([1, 0]) == (0, 1))
        regs = [{"id": i, "bbox_px": [0, 0, 20, 20]} for i in range(24)]
        nodes = [{"i": i, "bbox_px": [0, 0, 20, 20]} for i in range(60)]
        from unittest.mock import patch
        with patch.object(P, "_hungarian", wraps=P._hungarian) as solver:
            result = P.assign_marks_nodes(regs, nodes)
        self.assertEqual(solver.call_count, 1)
        self.assertEqual([p["candidate_node_id"] for p in result["pairs"]], list(range(24)))
        self.assertTrue(all(p["in_tight_cycle"] for p in result["pairs"]))
        self.assertFalse(result["passed_gate"])


if __name__ == "__main__":
    unittest.main()
