import copy
import json
import unittest

from visioncore import canon
from visioncore.errors import VqaError
from vmem import compact as C, store as S, units as U

SC = {"app": "operator_zero", "scene": "combat_hud", "res": "1920x1080", "grid": "grid:1920x1080:s32", "build_seq": 1}
TK = U.track_key("operator_zero", "combat_hud", 1920, 1080, "ui", 0x0a40, 0x0a7f)


def anchor(run="r_000001_00000001", crop=None, desc=None):
    return {
        "run_id": run,
        "cite": run + "/f84:t0a40",
        "frame": 84,
        "render_index": 84,
        "bbox_px": [0, 0, 32, 32],
        "mv_px": [0, 0],
        "xh64": "0" * 16,
        "sig64": "f" * 16,
        "crop": crop,
        "desc_blob": desc,
    }


def mk_bg(n=1, kind="baseline", run="r_000001_00000001", crop=None, desc=None):
    sc = dict(SC, build_seq=int(run.split("_")[1]))
    return U.make_unit(n, kind, sc, TK, anchors=[anchor(run, crop, desc)])


def mk_fg(n=1, kind="verdict", pin="FAIL", run="r_000001_00000001", crop=None, desc=None):
    sc = dict(SC, build_seq=int(run.split("_")[1]))
    return U.make_unit(n, kind, sc, TK, pin=pin, verdict="FAIL" if pin == "FAIL" else None,
                       check_id="check.test", anchors=[anchor(run, crop, desc)])


class TestVmemCompactHelpers(unittest.TestCase):
    def test_g_fg_monotone_positive(self):
        fg1 = mk_fg(1, pin="FAIL")
        fg2 = mk_fg(2, pin="GOLDEN")
        bg = mk_bg(3)
        parent = [fg1, bg]
        child = [fg1, fg2, bg]
        self.assertTrue(C.g_fg_monotone(parent, child))

    def test_g_fg_monotone_negative_disappearing_fg(self):
        fg1 = mk_fg(1, pin="FAIL")
        fg2 = mk_fg(2, pin="GOLDEN")
        parent = [fg1, fg2]
        child = [fg2]  # fg1 missing
        with self.assertRaises(VqaError) as c:
            C.g_fg_monotone(parent, child)
        self.assertEqual(c.exception.rc, 5)
        self.assertEqual(c.exception.code, "E_GATE")
        self.assertEqual(c.exception.detail["reason"], "g_fg_monotone")
        self.assertEqual(c.exception.detail["missing"], [fg1["id"]])

    def test_fg_bytes_with_and_without_blob_sizes(self):
        b1, b2 = "a" * 64, "b" * 64
        fg1 = mk_fg(1, crop=b1, desc=b2)
        fg2 = mk_fg(2, crop=b1)  # shares b1 with fg1
        bg = mk_bg(3, crop="c" * 64)

        units = [fg1, fg2, bg]

        # Without blob_sizes
        expected_units_bytes = len(U.dump_unit(fg1).encode("utf-8")) + len(U.dump_unit(fg2).encode("utf-8"))
        self.assertEqual(C.fg_bytes(units, blob_sizes=None), expected_units_bytes)

        # With blob_sizes (b1 deduplicated across fg1 and fg2; bg's blob ignored)
        sizes = {b1: 1000, b2: 2000, "c" * 64: 5000}
        expected_total = expected_units_bytes + 1000 + 2000
        self.assertEqual(C.fg_bytes(units, blob_sizes=sizes), expected_total)

    def test_fg_bytes_negative_bg_only_returns_zero(self):
        bg = mk_bg(1, crop="a" * 64)
        sizes = {"a" * 64: 1000}
        self.assertEqual(C.fg_bytes([bg], blob_sizes=sizes), 0)
        self.assertEqual(C.fg_bytes([], blob_sizes=sizes), 0)

    def test_run_order_deterministic(self):
        u1 = mk_bg(1, run="r_000003_00000003")
        u2 = mk_bg(2, run="r_000001_00000001")
        u3 = mk_bg(3, run="r_000002_00000002")
        order = C._run_order([u1, u2, u3])
        self.assertEqual(order, ["r_000001_00000001", "r_000002_00000002", "r_000003_00000003"])

    def test_run_order_negative_units_without_runs(self):
        u_no_anchors = U.make_unit(1, "baseline", SC, TK, anchors=[])
        self.assertEqual(C._run_order([u_no_anchors]), [])
        self.assertEqual(C._run_order([]), [])

    def test_strip_blobs_preserves_input_and_strips_refs(self):
        crop, desc, gop = "1" * 64, "2" * 64, "3" * 64
        lk_desc = "4" * 64
        anc = anchor("r_000001_00000001", crop, desc)
        anc["gop_blob"] = gop
        look = {"grid": SC["grid"], "n": 1, "ah64": "0" * 16, "desc_blob": lk_desc, "y_q4_mean": 10}

        orig = {
            "schema": "vmem.unit/v1",
            "id": "V-EPI-000001",
            "key": U.unit_key("operator_zero", "combat_hud", TK, "episode"),
            "kind": "episode",
            "layer": "BG",
            "pin": None,
            "status": "active",
            "born_gen": 1,
            "last_gen": 1,
            "scope": SC,
            "tk": TK,
            "subject": "subj",
            "predicate": "baseline",
            "value": "",
            "value_num": 0,
            "value_den": 1,
            "class": "UNCHANGED",
            "look": look,
            "anchors": [anc],
            "links": [],
            "verdict": None,
            "check_id": None,
            "receipt_sha256": "0" * 64,
            "summary": "",
        }
        orig_copy = copy.deepcopy(orig)

        # drop_look = False
        stripped_keep_look = C._strip_blobs(orig, drop_look=False)
        self.assertEqual(orig, orig_copy)  # Input unmutated
        self.assertIsNone(stripped_keep_look["anchors"][0]["crop"])
        self.assertEqual(stripped_keep_look["anchors"][0]["desc_blob"], desc)
        self.assertEqual(stripped_keep_look["anchors"][0]["gop_blob"], gop)
        self.assertEqual(stripped_keep_look["look"]["desc_blob"], lk_desc)

        # drop_look = True
        stripped_drop_look = C._strip_blobs(orig, drop_look=True)
        self.assertEqual(orig, orig_copy)  # Input unmutated
        self.assertIsNone(stripped_drop_look["anchors"][0]["crop"])
        self.assertIsNone(stripped_drop_look["anchors"][0]["desc_blob"])
        self.assertIsNone(stripped_drop_look["anchors"][0]["gop_blob"])
        self.assertIsNone(stripped_drop_look["look"]["desc_blob"])

    def test_strip_blobs_negative_no_look_or_anchors(self):
        u = U.make_unit(1, "baseline", SC, TK, anchors=[])
        u_copy = copy.deepcopy(u)
        out = C._strip_blobs(u, drop_look=True)
        self.assertEqual(u, u_copy)
        self.assertEqual(out["anchors"], [])
        self.assertIsNone(out["look"])

    def test_gc_plan_keeps_window_blobs(self):
        a, b, c = "a" * 64, "b" * 64, "c" * 64
        u_gen1 = mk_bg(1, crop=a)
        u_gen2 = mk_bg(2, crop=b)
        all_blobs = [a, b, c]
        window = [[u_gen1], [u_gen2]]
        plan = C.gc_plan(all_blobs, window, durable=True)
        self.assertEqual(plan, [c])

    def test_gc_plan_negative_not_durable_and_window_too_large(self):
        a, b = "a" * 64, "b" * 64
        u = mk_bg(1, crop=a)
        # Non-durable case -> returns empty list
        self.assertEqual(C.gc_plan([a, b], [[u]], durable=False), [])

        # Window too large (> VM_GEN_KEEP = 4) -> raises VqaError E_USAGE / rc 2
        oversized_window = [[u]] * 5
        with self.assertRaises(VqaError) as c:
            C.gc_plan([a, b], oversized_window, durable=True)
        self.assertEqual(c.exception.rc, 2)
        self.assertEqual(c.exception.code, "E_USAGE")
        self.assertEqual(c.exception.detail["reason"], "window_too_large")
        self.assertEqual(c.exception.detail["n"], 5)


class TestVmemStoreHelpers(unittest.TestCase):
    def test_parse_units_strict_and_lenient(self):
        u1 = mk_bg(1)
        u2 = mk_fg(2)
        raw_good = U.dump_unit(u1).encode("utf-8") + U.dump_unit(u2).encode("utf-8")

        # Both strict and lenient parse valid units
        self.assertEqual(S._parse_units(raw_good, strict=True), [u1, u2])
        self.assertEqual(S._parse_units(raw_good, strict=False), [u1, u2])

        bad_json = b"not json\n"
        raw_mixed = raw_good + bad_json

        # Strict mode raises VqaError on bad json line
        with self.assertRaises(VqaError) as c:
            S._parse_units(raw_mixed, strict=True)
        self.assertEqual(c.exception.rc, 2)
        self.assertEqual(c.exception.code, "E_SCHEMA")

        # Lenient mode ignores bad non-json lines and parses valid units
        self.assertEqual(S._parse_units(raw_mixed, strict=False), [u1, u2])

    def test_parse_units_negative_empty_and_whitespace(self):
        raw_empty = b"\n  \n\t\n"
        self.assertEqual(S._parse_units(raw_empty, strict=True), [])
        self.assertEqual(S._parse_units(raw_empty, strict=False), [])

    def test_check_fg_edits_allows_valid_supersession_and_last_gen_bump(self):
        old_fg = mk_fg(1, pin="GOLDEN")
        new_fg_same = copy.deepcopy(old_fg)

        # Unchanged FG unit allowed
        S._check_fg_edits([old_fg], [new_fg_same])

        # Bump last_gen allowed
        new_fg_bumped = dict(old_fg, last_gen=old_fg["last_gen"] + 1)
        S._check_fg_edits([old_fg], [new_fg_bumped])

        # Active -> superseded with valid supersedes link allowed
        old_fg_active = mk_fg(1, pin="GOLDEN")
        old_superseded, link = U.supersede(old_fg_active, "V-VER-000002")
        new_fg_successor = U.make_unit(2, "verdict", SC, TK, pin="GOLDEN", verdict="PASS",
                                       links=[link], check_id="check.test", anchors=[anchor()])
        S._check_fg_edits([old_fg_active], [old_superseded, new_fg_successor])

    def test_check_fg_edits_negative_field_mutation_and_decreased_last_gen(self):
        old_fg = mk_fg(1, pin="GOLDEN")

        # Negative 1: Mutating summary on published FG evidence -> raises E_GATE / rc 5
        mutated_summary = dict(old_fg, summary="tampered summary")
        with self.assertRaises(VqaError) as c1:
            S._check_fg_edits([old_fg], [mutated_summary])
        self.assertEqual(c1.exception.rc, 5)
        self.assertEqual(c1.exception.code, "E_GATE")
        self.assertEqual(c1.exception.detail["reason"], "g_fg_monotone")
        self.assertEqual(c1.exception.detail["edited"], old_fg["id"])

        # Negative 2: Decreasing last_gen on child FG unit -> raises E_GATE / rc 5
        decreased_gen = dict(old_fg, born_gen=2, last_gen=1)  # invalid last_gen < parent's last_gen (1)
        old_fg_gen2 = dict(old_fg, born_gen=2, last_gen=2)
        with self.assertRaises(VqaError) as c2:
            S._check_fg_edits([old_fg_gen2], [decreased_gen])
        self.assertEqual(c2.exception.rc, 5)
        self.assertEqual(c2.exception.code, "E_GATE")
        self.assertEqual(c2.exception.detail["reason"], "g_fg_monotone")

        # Negative 3: Unallowed status transition (active -> retired) -> raises E_GATE / rc 5
        retired_fg = dict(old_fg, status="retired")
        with self.assertRaises(VqaError) as c3:
            S._check_fg_edits([old_fg], [retired_fg])
        self.assertEqual(c3.exception.rc, 5)
        self.assertEqual(c3.exception.code, "E_GATE")
        self.assertEqual(c3.exception.detail["reason"], "g_fg_monotone")


if __name__ == "__main__":
    unittest.main()
