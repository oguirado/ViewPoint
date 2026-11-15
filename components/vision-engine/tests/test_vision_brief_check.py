"""Hand-written report/brief fixtures for the independent brief contract.

No engine imports, pixel data, generated files, or external packages are used.
"""

from copy import deepcopy
import json
import re
import unittest

from tools.vision_brief_check import JSON_SCHEMA, canonical_bytes, token_proxy, validate


GRID = "4x3"


def frame_row(frame, regions, cov_ppm=100, global_mv=None):
    """Write a report-shaped dictionary; do not derive brief expectations."""
    return {
        "schema": "vqa.core-frame/v1",
        "frame": frame,
        "grid": GRID,
        "n": [0, 0, 0, 0, 0, 0, 0],
        "cov_ppm": cov_ppm,
        "global_mv": global_mv,
        "regions": regions,
        "tiles_sha256": "0" * 64,
    }


def region_row(identifier, frame, n_tiles, bbox_px, cls="CHANGED",
               kind="EDGE", mv=None):
    return {
        "schema": "vqa.core-region/v1",
        "id": identifier,
        "frame": frame,
        "grid": GRID,
        "cls": cls,
        "n_tiles": n_tiles,
        "bbox_t": [0, 0, 0, 0],
        "bbox_px": bbox_px,
        "mv": [0, 0] if mv is None else mv,
        "r_max": 1,
        "r_sum": 1,
        "edge_ppm": 100,
        "kind": kind,
        "shade_only": False,
        "spans": [],
    }


def brief_region(identifier, n_tiles, bbox_px, first, last, cls="CHANGED",
                 kind="EDGE", mv=None, toggles=0):
    """Literal field shorthand, not a projection or merge implementation."""
    return {
        "id": identifier,
        "cls": cls,
        "kind": kind,
        "bbox_px": bbox_px,
        "mv": [0, 0] if mv is None else mv,
        "n_tiles": n_tiles,
        "first_frame": first,
        "last_frame": last,
        "toggles": toggles,
    }


class VisionBriefCheckTests(unittest.TestCase):
    def setUp(self):
        self.params = {
            "width": 100,
            "height": 80,
            "max_regions": 8,
            "max_crops": 4,
            "pad_px": 2,
            "source_frames": [
                frame_row(10, 2, 2500, [1, -1]),
                frame_row(20, 1, 1200, None),
                frame_row(30, 1, 900, [0, 0]),
            ],
            "source_regions": [
                region_row("a", 10, 8, [10, 10, 8, 6], "MOVED", "TEXTURE", [1, -1]),
                region_row("f1", 10, 3, [50, 20, 4, 4], "FLICKER", "SMOOTH"),
                region_row("b", 20, 5, [30, 40, 6, 8]),
                region_row("f3", 30, 4, [50, 20, 4, 4], "FLICKER", "SMOOTH"),
            ],
        }
        self.brief = {
            "schema": "vqa.vision-brief/v1",
            "frame_range": [10, 30],
            "grid": GRID,
            "frames": [
                {"frame": 10, "cov_ppm": 2500, "global_mv": [1, -1]},
                {"frame": 20, "cov_ppm": 1200, "global_mv": None},
                {"frame": 30, "cov_ppm": 900, "global_mv": [0, 0]},
            ],
            "regions": [
                brief_region("a", 8, [10, 10, 8, 6], 10, 10,
                             "MOVED", "TEXTURE", [1, -1]),
                brief_region("b", 5, [30, 40, 6, 8], 20, 20),
                brief_region("f3", 4, [50, 20, 4, 4], 10, 30,
                             "FLICKER", "SMOOTH", toggles=2),
            ],
            "crops": [[8, 8, 19, 17], [28, 38, 37, 49], [48, 18, 55, 25]],
            "truncated": 0,
        }

    def assert_valid(self, brief=None, params=None):
        result = validate(self.brief if brief is None else brief,
                          **(self.params if params is None else params))
        self.assertEqual([], result)

    def assert_invalid(self, brief=None, params=None):
        result = validate(self.brief if brief is None else brief,
                          **(self.params if params is None else params))
        self.assertIsInstance(result, list)
        self.assertTrue(result, "invalid input produced no reported problems")
        self.assertTrue(all(isinstance(problem, str) for problem in result))
        return result

    def one_frame_case(self, source_regions, expected_regions, crops,
                       width=100, height=80, pad_px=0):
        params = {
            "width": width, "height": height, "max_regions": 8,
            "max_crops": 8, "pad_px": pad_px,
            "source_frames": [frame_row(0, len(source_regions))],
            "source_regions": source_regions,
        }
        brief = {
            "schema": "vqa.vision-brief/v1", "frame_range": [0, 0],
            "grid": GRID,
            "frames": [{"frame": 0, "cov_ppm": 100, "global_mv": None}],
            "regions": expected_regions, "crops": crops, "truncated": 0,
        }
        return brief, params

    def test_valid_hand_written_brief_and_default_budget(self):
        self.assertLess(token_proxy(self.brief), 600)
        self.assert_valid()

    def test_canonical_serialization_and_size_proxy(self):
        value = {"z": 0, "a": [-1, None]}
        expected = b'{"a":[-1,null],"z":0}'
        self.assertEqual(expected, canonical_bytes(value))
        self.assertEqual((len(expected) + 3) // 4, token_proxy(value))
        self.assertEqual(1, token_proxy({}))
        self.assertEqual(2, token_proxy({"x": 0}))
        self.assertFalse(canonical_bytes(self.brief).endswith(b"\n"))

    def test_json_schema_is_a_serializable_fixed_key_contract(self):
        self.assertIsInstance(JSON_SCHEMA, dict)
        self.assertEqual(JSON_SCHEMA, json.loads(json.dumps(JSON_SCHEMA)))
        self.assertEqual("vqa.vision-brief/v1",
                         JSON_SCHEMA["properties"]["schema"]["const"])
        self.assertFalse(JSON_SCHEMA["additionalProperties"])
        self.assertEqual(set(self.brief), set(JSON_SCHEMA["required"]))

    def test_portable_schema_text_patterns_reject_trailing_newlines(self):
        patterns = [JSON_SCHEMA["properties"]["grid"]["pattern"],
                    JSON_SCHEMA["$defs"]["region"]["properties"]["id"]["pattern"]]
        for pattern in patterns:
            for value in ("grid", "two words", " id "):
                with self.subTest(pattern=pattern, valid=value):
                    self.assertIsNotNone(re.search(pattern, value))
            for value in ("", "grid\n", "grid\r", "grid\t", "gr\u00edd", "grid\x7f"):
                with self.subTest(pattern=pattern, invalid=value):
                    self.assertIsNone(re.search(pattern, value))

    def test_validation_does_not_mutate_any_input(self):
        before = deepcopy((self.brief, self.params))
        self.assert_valid()
        self.assertEqual(before, (self.brief, self.params))

    def test_source_order_is_irrelevant(self):
        params = deepcopy(self.params)
        params["source_frames"].reverse()
        params["source_regions"].reverse()
        self.assert_valid(params=params)

    def test_wrong_schema_version(self):
        brief = deepcopy(self.brief)
        brief["schema"] = "vqa.vision-brief/v2"
        self.assert_invalid(brief)

    def test_fixed_keys_at_each_object_level(self):
        for location in ("root", "frame", "region"):
            for action in ("extra", "missing"):
                with self.subTest(location=location, action=action):
                    brief = deepcopy(self.brief)
                    obj = (brief if location == "root" else
                           brief["frames"][0] if location == "frame" else
                           brief["regions"][0])
                    if action == "extra":
                        obj["unexpected"] = 1
                    else:
                        del obj[next(iter(obj))]
                    self.assert_invalid(brief)

    def test_invalid_container_types_do_not_raise(self):
        for value in ([], None, "brief", 1):
            with self.subTest(root=value):
                result = validate(value, **self.params)
                self.assertIsInstance(result, list)
                self.assertTrue(result)
        for key, bad in (("frames", {}), ("regions", None), ("crops", "[]"),
                         ("frame_range", [10]), ("frames", [None]),
                         ("regions", [3]), ("crops", [None])):
            with self.subTest(key=key, bad=bad):
                brief = deepcopy(self.brief)
                brief[key] = bad
                self.assert_invalid(brief)

    def test_every_output_integer_family_rejects_bool_and_float(self):
        paths = [
            ("frame_range", 0), ("frames", 0, "frame"),
            ("frames", 0, "cov_ppm"), ("frames", 0, "global_mv", 0),
            ("regions", 0, "n_tiles"), ("regions", 0, "bbox_px", 0),
            ("regions", 0, "mv", 0), ("regions", 0, "first_frame"),
            ("regions", 0, "last_frame"), ("regions", 0, "toggles"),
            ("crops", 0, 0), ("truncated",),
        ]
        for path in paths:
            for bad in (True, 1.0):
                with self.subTest(path=path, bad=bad):
                    brief = deepcopy(self.brief)
                    obj = brief
                    for part in path[:-1]:
                        obj = obj[part]
                    obj[path[-1]] = bad
                    self.assert_invalid(brief)

    def test_output_text_is_ascii_and_typed(self):
        for key, value in (("grid", "gr\u00edd"), ("grid", "grid\n"), ("grid", 7),
                           ("schema", "vqa.vision-br\u00edef/v1")):
            with self.subTest(key=key, value=value):
                brief = deepcopy(self.brief)
                brief[key] = value
                self.assert_invalid(brief)
        for value in ("caf\u00e9", "a\n", "a\t", "a\x7f", ""):
            with self.subTest(id=value):
                brief = deepcopy(self.brief)
                brief["regions"][0]["id"] = value
                self.assert_invalid(brief)
        brief = deepcopy(self.brief)
        brief[7] = "non-string key"
        self.assert_invalid(brief)

    def test_unknown_region_enums(self):
        for key in ("cls", "kind"):
            with self.subTest(key=key):
                brief = deepcopy(self.brief)
                brief["regions"][0][key] = "UNKNOWN"
                self.assert_invalid(brief)

    def test_invalid_output_numeric_ranges(self):
        for key, bad in (("n_tiles", 0), ("first_frame", -1),
                         ("last_frame", -1), ("toggles", -1)):
            with self.subTest(key=key):
                brief = deepcopy(self.brief)
                brief["regions"][0][key] = bad
                self.assert_invalid(brief)
        for bad in (-1, 1000001):
            brief = deepcopy(self.brief)
            brief["frames"][0]["cov_ppm"] = bad
            self.assert_invalid(brief)
        brief = deepcopy(self.brief)
        brief["truncated"] = -1
        self.assert_invalid(brief)

    def test_changed_fraction_and_motion_are_preserved(self):
        for key, bad in (("cov_ppm", 2501), ("global_mv", [1, 1])):
            with self.subTest(key=key):
                brief = deepcopy(self.brief)
                brief["frames"][0][key] = bad
                self.assert_invalid(brief)
        brief = deepcopy(self.brief)
        brief["frames"][1]["global_mv"] = [0, 0]
        self.assert_invalid(brief)
        brief = deepcopy(self.brief)
        brief["frames"][0]["global_mv"] = None
        self.assert_invalid(brief)

    def test_motion_vector_shape(self):
        for location in ("frames", "regions"):
            for value in ([], [1], [1, 2, 3], "0,0", [0, "1"]):
                with self.subTest(location=location, value=value):
                    brief = deepcopy(self.brief)
                    key = "global_mv" if location == "frames" else "mv"
                    brief[location][0][key] = value
                    self.assert_invalid(brief)

    def test_frame_range_order_and_full_frame_sequence(self):
        for value in ([30, 10], [10, 20], [0, 30]):
            brief = deepcopy(self.brief)
            brief["frame_range"] = value
            self.assert_invalid(brief)
        brief = deepcopy(self.brief)
        brief["frames"].reverse()
        self.assert_invalid(brief)
        brief = deepcopy(self.brief)
        brief["frames"].pop(1)
        self.assert_invalid(brief)
        brief = deepcopy(self.brief)
        brief["frames"].append(deepcopy(brief["frames"][-1]))
        self.assert_invalid(brief)

    def test_region_rank_order(self):
        brief = deepcopy(self.brief)
        brief["regions"][0], brief["regions"][1] = brief["regions"][1], brief["regions"][0]
        self.assert_invalid(brief)

    def test_equal_tile_count_ranks_by_id(self):
        params = deepcopy(self.params)
        params["source_regions"][2]["n_tiles"] = 8
        brief = deepcopy(self.brief)
        brief["regions"][1]["n_tiles"] = 8
        self.assert_valid(brief, params)
        brief["regions"][0], brief["regions"][1] = brief["regions"][1], brief["regions"][0]
        self.assert_invalid(brief, params)

    def test_region_fields_and_observation_bounds_match_sources(self):
        for key, value in (("n_tiles", 9), ("first_frame", 9),
                           ("last_frame", 20), ("toggles", 1),
                           ("mv", [2, -1]), ("id", "different")):
            with self.subTest(key=key):
                brief = deepcopy(self.brief)
                brief["regions"][0][key] = value
                self.assert_invalid(brief)

    def test_flicker_representative_is_largest_then_smallest_id(self):
        params = deepcopy(self.params)
        params["source_regions"][1]["n_tiles"] = 4
        brief = deepcopy(self.brief)
        brief["regions"][2]["id"] = "f1"
        self.assert_valid(brief, params)
        brief["regions"][2]["id"] = "f3"
        self.assert_invalid(brief, params)

    def test_flicker_presence_includes_both_window_sides(self):
        params = {
            "width": 10, "height": 10, "max_regions": 3,
            "max_crops": 3, "pad_px": 0,
            "source_frames": [frame_row(4, 0), frame_row(9, 1), frame_row(12, 0)],
            "source_regions": [region_row("f", 9, 2, [1, 1, 2, 2], "FLICKER")],
        }
        brief = {
            "schema": "vqa.vision-brief/v1", "frame_range": [4, 12], "grid": GRID,
            "frames": [
                {"frame": 4, "cov_ppm": 100, "global_mv": None},
                {"frame": 9, "cov_ppm": 100, "global_mv": None},
                {"frame": 12, "cov_ppm": 100, "global_mv": None},
            ],
            "regions": [brief_region("f", 2, [1, 1, 2, 2], 9, 9,
                                     "FLICKER", toggles=2)],
            "crops": [[1, 1, 2, 2]], "truncated": 0,
        }
        self.assert_valid(brief, params)
        brief["regions"][0]["toggles"] = 0
        self.assert_invalid(brief, params)

    def test_flicker_same_frame_rows_do_not_add_toggles_or_truncation(self):
        brief, params = self.one_frame_case(
            [region_row("f0", 0, 1, [1, 1, 2, 2], "FLICKER"),
             region_row("f1", 0, 2, [1, 1, 2, 2], "FLICKER")],
            [brief_region("f1", 2, [1, 1, 2, 2], 0, 0, "FLICKER")],
            [[1, 1, 2, 2]],
        )
        self.assert_valid(brief, params)
        brief["truncated"] = 1
        self.assert_invalid(brief, params)

    def test_flicker_signature_respects_kind_motion_and_exact_bbox(self):
        cases = [
            ("SMOOTH", [0, 0], [1, 1, 2, 2], [1, 1, 2, 2]),
            ("EDGE", [1, 0], [1, 1, 2, 2], [1, 1, 2, 2]),
            ("EDGE", [0, 0], [2, 1, 2, 2], [1, 1, 3, 2]),
        ]
        for kind, mv, box, crop in cases:
            with self.subTest(kind=kind, mv=mv, box=box):
                params = {
                    "width": 10, "height": 10, "max_regions": 3,
                    "max_crops": 3, "pad_px": 0,
                    "source_frames": [frame_row(1, 1), frame_row(3, 1)],
                    "source_regions": [
                        region_row("a", 1, 2, [1, 1, 2, 2], "FLICKER"),
                        region_row("b", 3, 1, box, "FLICKER", kind, mv),
                    ],
                }
                brief = {
                    "schema": "vqa.vision-brief/v1", "frame_range": [1, 3], "grid": GRID,
                    "frames": [
                        {"frame": 1, "cov_ppm": 100, "global_mv": None},
                        {"frame": 3, "cov_ppm": 100, "global_mv": None},
                    ],
                    "regions": [
                        brief_region("a", 2, [1, 1, 2, 2], 1, 1, "FLICKER", toggles=1),
                        brief_region("b", 1, box, 3, 3, "FLICKER", kind, mv, toggles=1),
                    ],
                    "crops": [crop], "truncated": 0,
                }
                self.assert_valid(brief, params)

    def test_parameter_caps_and_truncated_arithmetic(self):
        params = deepcopy(self.params)
        params["max_regions"] = 2
        params["max_crops"] = 1
        self.assert_invalid(params=params)
        brief = deepcopy(self.brief)
        brief["regions"].pop()
        brief["crops"] = [[8, 8, 19, 17]]
        brief["truncated"] = 2
        self.assert_valid(brief, params)
        brief["truncated"] = 1
        self.assert_invalid(brief, params)

    def test_zero_caps_are_valid_and_count_omissions(self):
        for key in ("max_regions", "max_crops"):
            with self.subTest(key=key):
                params = deepcopy(self.params)
                params[key] = 0
                brief = deepcopy(self.brief)
                if key == "max_regions":
                    brief["regions"] = []
                brief["crops"] = []
                brief["truncated"] = 3
                self.assert_valid(brief, params)

    def test_crops_are_closed_boxes_with_padding(self):
        for value in ([8, 8, 12, 10], [9, 8, 19, 17], [8, 8, 20, 18],
                      [19, 8, 8, 17], [8, 17, 19, 8], [8, 8, 19],
                      [-1, 8, 19, 17], [8, 8, 100, 17], [8, 8, 19, 80]):
            with self.subTest(value=value):
                brief = deepcopy(self.brief)
                brief["crops"][0] = value
                self.assert_invalid(brief)

    def test_region_xywh_boxes_are_positive_and_inside_frame(self):
        for value in ([10, 10, 0, 6], [-1, 10, 8, 6], [99, 10, 8, 6],
                      [10, 79, 8, 6], [10, 10, 8], [10, 10, 8, -1]):
            with self.subTest(value=value):
                brief = deepcopy(self.brief)
                brief["regions"][0]["bbox_px"] = value
                self.assert_invalid(brief)

    def test_clamping_at_all_frame_edges(self):
        brief, params = self.one_frame_case(
            [region_row("a", 0, 2, [0, 0, 2, 2]),
             region_row("b", 0, 1, [98, 78, 2, 2])],
            [brief_region("a", 2, [0, 0, 2, 2], 0, 0),
             brief_region("b", 1, [98, 78, 2, 2], 0, 0)],
            [[0, 0, 4, 4], [95, 75, 99, 79]], pad_px=3,
        )
        self.assert_valid(brief, params)
        brief["crops"][1][2] = 100
        self.assert_invalid(brief, params)

    def test_a_single_pixel_is_a_valid_closed_crop(self):
        brief, params = self.one_frame_case(
            [region_row("a", 0, 1, [4, 5, 1, 1])],
            [brief_region("a", 1, [4, 5, 1, 1], 0, 0)],
            [[4, 5, 4, 5]],
        )
        self.assert_valid(brief, params)

    def test_adjacent_crops_do_not_overlap_but_shared_pixels_do(self):
        adjacent, params = self.one_frame_case(
            [region_row("a", 0, 2, [1, 1, 2, 2]),
             region_row("b", 0, 1, [3, 1, 2, 2])],
            [brief_region("a", 2, [1, 1, 2, 2], 0, 0),
             brief_region("b", 1, [3, 1, 2, 2], 0, 0)],
            [[1, 1, 2, 2], [3, 1, 4, 2]],
        )
        self.assert_valid(adjacent, params)
        wrong = deepcopy(adjacent)
        wrong["crops"] = [[1, 1, 4, 2]]
        self.assert_invalid(wrong, params)

        shared, params = self.one_frame_case(
            [region_row("a", 0, 2, [1, 1, 3, 3]),
             region_row("b", 0, 1, [3, 1, 3, 3])],
            [brief_region("a", 2, [1, 1, 3, 3], 0, 0),
             brief_region("b", 1, [3, 1, 3, 3], 0, 0)],
            [[1, 1, 5, 3]],
        )
        self.assert_valid(shared, params)
        shared["crops"] = [[1, 1, 3, 3], [3, 1, 5, 3]]
        self.assert_invalid(shared, params)

    def test_crop_merging_reaches_new_rectangle_fixed_point(self):
        brief, params = self.one_frame_case(
            [region_row("a", 0, 3, [0, 0, 3, 7]),
             region_row("b", 0, 2, [1, 5, 6, 2]),
             region_row("c", 0, 1, [4, 0, 3, 3])],
            [brief_region("a", 3, [0, 0, 3, 7], 0, 0),
             brief_region("b", 2, [1, 5, 6, 2], 0, 0),
             brief_region("c", 1, [4, 0, 3, 3], 0, 0)],
            [[0, 0, 6, 6]], width=10, height=10,
        )
        self.assert_valid(brief, params)
        brief["crops"] = [[0, 0, 6, 6], [4, 0, 6, 2]]
        self.assert_invalid(brief, params)

    def test_crop_order_follows_region_rank(self):
        brief = deepcopy(self.brief)
        brief["crops"].reverse()
        self.assert_invalid(brief)

    def test_exact_budget_boundary(self):
        params = deepcopy(self.params)
        params["budget_tokens"] = token_proxy(self.brief)
        self.assert_valid(params=params)
        params["budget_tokens"] -= 1
        self.assert_invalid(params=params)

    def test_budget_drops_lowest_ranked_region_and_keeps_frozen_crops(self):
        brief = deepcopy(self.brief)
        brief["regions"].pop()
        brief["truncated"] = 1
        params = deepcopy(self.params)
        params["budget_tokens"] = token_proxy(brief)
        self.assertGreater(token_proxy(self.brief), params["budget_tokens"])
        self.assert_valid(brief, params)
        wrong = deepcopy(brief)
        wrong["regions"].pop(0)
        wrong["regions"].append(deepcopy(self.brief["regions"][-1]))
        self.assert_invalid(wrong, params)

    def test_budget_must_not_drop_crops_before_regions(self):
        brief = deepcopy(self.brief)
        brief["crops"].pop()
        brief["truncated"] = 1
        params = deepcopy(self.params)
        params["budget_tokens"] = token_proxy(brief)
        self.assert_invalid(brief, params)

    def test_budget_can_retain_crops_with_no_displayed_regions(self):
        brief = deepcopy(self.brief)
        brief["regions"] = []
        brief["truncated"] = 3
        params = deepcopy(self.params)
        params["budget_tokens"] = token_proxy(brief)
        self.assert_valid(brief, params)

    def test_budget_drops_crops_after_all_regions(self):
        brief = deepcopy(self.brief)
        brief["regions"] = []
        brief["crops"].pop()
        brief["truncated"] = 4
        params = deepcopy(self.params)
        params["budget_tokens"] = token_proxy(brief)
        self.assert_valid(brief, params)

    def test_unnecessary_drops_are_not_a_valid_budget_projection(self):
        brief = deepcopy(self.brief)
        brief["regions"].pop()
        brief["truncated"] = 1
        self.assert_invalid(brief)

    def test_impossible_protected_header_reports_budget_problem(self):
        brief = deepcopy(self.brief)
        brief["regions"] = []
        brief["crops"] = []
        brief["truncated"] = 6
        params = deepcopy(self.params)
        params["budget_tokens"] = token_proxy(brief) - 1
        problems = self.assert_invalid(brief, params)
        self.assertTrue(any("budget" in problem.lower() for problem in problems))

    def test_truncated_count_must_match_full_provenance(self):
        brief = deepcopy(self.brief)
        brief["truncated"] = 1
        self.assert_invalid(brief)
        params = deepcopy(self.params)
        params["source_regions"].pop()
        self.assert_invalid(params=params)

    def test_invalid_numeric_parameters(self):
        for key in ("width", "height", "max_regions", "max_crops", "pad_px", "budget_tokens"):
            bad_values = [True, 1.0, "1", -1]
            if key in ("width", "height", "budget_tokens"):
                bad_values.append(0)
            for bad in bad_values:
                with self.subTest(key=key, bad=bad):
                    params = deepcopy(self.params)
                    params[key] = bad
                    self.assert_invalid(params=params)

    def test_invalid_source_containers_and_empty_window(self):
        for key, bad in (("source_frames", []), ("source_frames", None),
                         ("source_frames", [None]), ("source_regions", {}),
                         ("source_regions", [None])):
            with self.subTest(key=key, bad=bad):
                params = deepcopy(self.params)
                params[key] = bad
                self.assert_invalid(params=params)

    def test_source_frame_consumed_fields(self):
        cases = [
            ("schema", "vqa.core-frame/v2"), ("frame", -1),
            ("frame", True), ("frame", 10.0), ("grid", "different"),
            ("cov_ppm", -1), ("cov_ppm", 1000001), ("cov_ppm", 1.0),
            ("regions", -1), ("regions", True), ("regions", 99),
            ("global_mv", [0]), ("global_mv", [0, False]),
        ]
        for key, bad in cases:
            with self.subTest(key=key, bad=bad):
                params = deepcopy(self.params)
                params["source_frames"][0][key] = bad
                self.assert_invalid(params=params)
        params = deepcopy(self.params)
        del params["source_frames"][0]["global_mv"]
        self.assert_invalid(params=params)

    def test_source_region_consumed_fields(self):
        cases = [
            ("schema", "vqa.core-region/v2"), ("frame", 99),
            ("frame", True), ("grid", "different"), ("id", ""),
            ("id", "caf\u00e9"), ("id", "a\n"), ("cls", "UNKNOWN"),
            ("kind", "UNKNOWN"), ("n_tiles", 0), ("n_tiles", True),
            ("n_tiles", 8.0), ("mv", [0]), ("mv", [False, 0]),
            ("bbox_px", [99, 10, 8, 6]), ("bbox_px", [10, 10, 0, 6]),
            ("bbox_px", [10, 10, 8.0, 6]), ("bbox_px", [10, 10, 8]),
        ]
        for key, bad in cases:
            with self.subTest(key=key, bad=bad):
                params = deepcopy(self.params)
                params["source_regions"][0][key] = bad
                self.assert_invalid(params=params)
        params = deepcopy(self.params)
        del params["source_regions"][0]["kind"]
        self.assert_invalid(params=params)

    def test_source_frames_and_region_ids_are_unique(self):
        params = deepcopy(self.params)
        params["source_frames"].append(deepcopy(params["source_frames"][0]))
        self.assert_invalid(params=params)
        params = deepcopy(self.params)
        params["source_regions"][2]["id"] = "a"
        self.assert_invalid(params=params)

    def test_source_grid_must_be_common_printable_ascii(self):
        for bad in ("", "gr\u00edd", "grid\n", "grid\x7f", 1):
            with self.subTest(bad=bad):
                params = deepcopy(self.params)
                brief = deepcopy(self.brief)
                for frame in params["source_frames"]:
                    frame["grid"] = bad
                for region in params["source_regions"]:
                    region["grid"] = bad
                brief["grid"] = bad
                self.assert_invalid(brief, params)


if __name__ == "__main__":
    unittest.main()
