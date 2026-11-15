import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

from bench.oracle import label_codec as LC
from bench.oracle import labels_oracle as LO

HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.join(HERE, "fixtures")
W, H = 1920, 1080


def fx(n):
    with open(os.path.join(FX, n)) as f:
        return json.load(f)


def row(seq, t, nodes=None, sx=0, sy=0, snap="aa", **k):
    d = {"seq": seq, "t_us": t, "snap": snap, "nodes": nodes, "sx": sx, "sy": sy, "focus": None, "shifts": [], "frac": 0, "canv": []}
    d.update(k)
    return d


def D(samples, ops, **o):
    return LO.derive(samples, ops, W, H, "run1", o or None)


class Grid(unittest.TestCase):
    def test_grid_params(self):
        self.assertEqual(LO.grid_params(1920, 1080), (32, 60, 34))
        self.assertEqual(LO.grid_params(3840, 2160), (64, 60, 34))
        self.assertEqual(LO.grid_params(1280, 800), (16, 80, 50))  # engine rule (params.py CELL_MAX 4096); the CLASS_CONTRACT example s32 is inconsistent with its own rule
        self.assertEqual(LO.grid_params(320, 180), (16, 20, 12))

    def test_morton_rank_dense(self):
        t = LO.order_table(1920, 1080)
        self.assertEqual(sorted(t.values()), list(range(60 * 34)))
        self.assertEqual(t[(0, 0)], 0)
        self.assertEqual(t[(1, 0)], 1)
        self.assertEqual(t[(0, 1)], 2)
        self.assertLess(max(t.values()), 2040 + 1)

    def test_ext_rule(self):
        t = LO.order_table(1920, 1080)
        got = LO.rect_tiles(1920, 1080, [800, 420, 320, 240])
        want = sorted(t[(tx, ty)] for tx in range(25, 35) for ty in range(13, 21))
        self.assertEqual(got, want)  # x 800..1120 -> tx 25..34; y 420..660 -> ty 13..20 (edge tiles straddle)
        self.assertEqual(LO.rect_tiles(1920, 1080, [0, 0, 32, 32]), [0])
        self.assertEqual(LO.rect_tiles(1920, 1080, [0, 0, 33, 32]), sorted([0, 1]))

    def test_straddle_rule(self):
        self.assertTrue(LO.straddle([800, 420, 320, 240], W, H))
        self.assertFalse(LO.straddle([800, 416, 320, 224], W, H))
        self.assertFalse(LO.straddle([0, 0, 1920, 1080], W, H))  # frame borders never straddle


class Scroll(unittest.TestCase):
    def test_wl1s_moved_exact(self):
        d = fx("oracle_samples_wl1s.json")
        out = D(d["samples"], d["ops"])
        mv = [l for l in out if l["ideal"] == 3]
        ch = [l for l in out if l["ideal"] == 2]
        self.assertEqual(len(mv), 1)
        self.assertEqual(mv[0]["mv"], [0, -4])
        self.assertEqual(mv[0]["rects"], [[0, 0, 1920, 1076]])
        self.assertEqual(mv[0]["confidence"], "exact")
        self.assertEqual(mv[0]["source"], "scroll")
        self.assertEqual(ch[0]["rects"], [[0, 1076, 1920, 4]])
        self.assertEqual(ch[0]["mv"], None)
        self.assertEqual(mv[0]["f0"], 1010000 * 60 // 1000000)
        self.assertEqual(mv[0]["f1"], mv[0]["f0"] + 1)

    def test_negative_scroll_sign(self):
        s = [row(1, 1000000, sy=64), row(2, 1033333, sy=40)]
        out = D(s, [{"op": "scroll", "t_us": 1010000}])
        mv = [l for l in out if l["ideal"] == 3][0]
        self.assertEqual(mv["mv"], [0, 24])
        self.assertEqual(mv["rects"], [[0, 24, 1920, 1056]])
        self.assertEqual([l for l in out if l["ideal"] == 2][0]["rects"], [[0, 0, 1920, 24]])

    def test_horizontal(self):
        s = [row(1, 1000000), row(2, 1033333, sx=120)]
        out = D(s, [{"op": "hscroll", "t_us": 1010000}])
        mv = [l for l in out if l["ideal"] == 3][0]
        self.assertEqual(mv["mv"], [-120, 0])
        self.assertEqual(mv["rects"], [[0, 0, 1800, 1080]])
        self.assertEqual([l for l in out if l["ideal"] == 2][0]["rects"], [[1800, 0, 120, 1080]])

    def test_two_ops_bracketed_is_derived(self):
        s = [row(1, 1000000), row(2, 1100000, sy=12)]
        out = D(s, [{"op": "scroll", "t_us": 1010000}, {"op": "scroll", "t_us": 1050000}])
        self.assertTrue(out and all(l["confidence"] == "derived" for l in out))

    def test_no_op_bracketed_is_derived(self):
        s = [row(1, 1000000), row(2, 1033333, sy=4)]
        out = D(s, [])
        self.assertTrue(out and all(l["confidence"] == "derived" for l in out))

    def test_smooth_exact_only_with_seq_gap_zero(self):
        tab = [2, 2, 3, 3, 4, 5, 6, 6, 7, 7, 8, 8, 8, 8, 7, 7, 6, 6, 5, 4, 3, 3, 2, 2]
        s, y, t = [row(1, 1000000)], 0, 1000000
        for k, dy in enumerate(tab):
            y += dy
            t += 16666
            s.append(row(k + 2, t, sy=y))
        ops = [{"op": "scroll", "t_us": 1000100}]
        out = D(s, ops, smooth=True)
        mv = [l for l in out if l["ideal"] == 3]
        self.assertEqual(len(mv), 24)
        self.assertEqual(sum(l["mv"][1] for l in mv), -122)
        self.assertEqual(LO.smooth_gesture_sums(mv), [122])
        # a hole in seq makes the following step derived
        s2 = [dict(x) for x in s]
        for x in s2[10:]:
            x["seq"] += 1
        out2 = D(s2, ops, smooth=True)
        mv2 = [l for l in out2 if l["ideal"] == 3]
        self.assertEqual(len([l for l in mv2 if l["confidence"] == "derived"]), 1)
        self.assertTrue(all(l["confidence"] == "exact" for l in mv2[:9]))


class Elements(unittest.TestCase):
    def test_wl4_drag(self):
        d = fx("oracle_samples_wl4.json")
        out = D(d["samples"], d["ops"])
        mv = [l for l in out if l["ideal"] == 3]
        unc = [l for l in out if "UNCOVERED" in l["flags"]]
        self.assertEqual(mv[0]["mv"], [3, 0])
        self.assertEqual(mv[0]["rects"], [[103, 200, 640, 480]])
        self.assertEqual(mv[0]["source"], "window")
        self.assertEqual(mv[0]["confidence"], "exact")
        self.assertEqual(unc[0]["ideal"], 2)
        self.assertEqual(unc[0]["mv"], None)
        self.assertEqual(unc[0]["rects"], [[100, 200, 3, 480]])
        self.assertEqual(unc[0]["confidence"], "exact")

    def test_wl4_drag_left(self):
        n1 = [[4, 100, 200, 640, 480, 1, "%016x" % 2]]
        n2 = [[4, 97, 200, 640, 480, 1, "%016x" % 2]]
        out = D([row(1, 1000000, n1, snap="a"), row(2, 1016666, n2, snap="b")], [{"op": "drag_move", "t_us": 1010000}])
        unc = [l for l in out if "UNCOVERED" in l["flags"]][0]
        self.assertEqual(unc["rects"], [[737, 200, 3, 480]])

    def test_wl3_canvas_straddle(self):
        n1 = [[5, 800, 420, 320, 240, 1, "%016x" % 1]]
        n2 = [[5, 800, 420, 320, 240, 1, "%016x" % 9]]
        out = D([row(1, 1000000, n1, snap="a", canv=[5]), row(2, 1016666, n2, snap="b", canv=[5])], [])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["ideal"], 2)
        self.assertIn("STRADDLE", out[0]["flags"])
        self.assertEqual(out[0]["confidence"], "derived")  # canvas content is not in the DOM
        self.assertEqual(out[0]["rects"], [[800, 420, 320, 240]])

    def test_dom_edit_exact_and_aligned_no_straddle(self):
        n1 = [[5, 800, 416, 320, 224, 1, "%016x" % 1]]
        n2 = [[5, 800, 416, 320, 224, 1, "%016x" % 9]]
        out = D([row(1, 1000000, n1, snap="a"), row(2, 1016666, n2, snap="b")], [])
        self.assertEqual(out[0]["confidence"], "exact")
        self.assertNotIn("STRADDLE", out[0]["flags"])
        self.assertEqual(out[0]["source"], "mutation")

    def test_unobservable(self):
        n1 = [[5, 32, 32, 64, 64, 0, "%016x" % 1]]
        n2 = [[5, 32, 32, 64, 64, 0, "%016x" % 9]]
        out = D([row(1, 1000000, n1, snap="a"), row(2, 1016666, n2, snap="b")], [])
        self.assertEqual(out[0]["flags"], ["UNOBSERVABLE"])
        self.assertEqual(out[0]["ideal"], 2)

    def test_layout_shift_and_input_exclusion(self):
        sh = {"n": 5, "p": [0, 100, 50, 50], "c": [0, 130, 50, 50], "ri": 0}
        out = D([row(1, 1000000), row(2, 1016666, shifts=[sh])], [])
        self.assertEqual(out[0]["ideal"], 3)
        self.assertEqual(out[0]["mv"], [0, 30])
        self.assertEqual(out[0]["source"], "layoutshift")
        self.assertEqual(out[0]["rects"], [[0, 130, 50, 50]])
        sh2 = dict(sh, ri=1)
        self.assertEqual(D([row(1, 1000000), row(2, 1016666, shifts=[sh2])], []), [])

    def test_caret_flicker_derived(self):
        s = [row(1, 1000000), row(2, 1016666, focus="7")]
        out = D(s, [], caret_rect=[40, 100, 2, 40])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["ideal"], 4)
        self.assertEqual(out[0]["confidence"], "derived")
        self.assertIsNone(out[0]["period_ms"])
        self.assertEqual(out[0]["source"], "focus")
        self.assertEqual(out[0]["source_ref"], "7")

    def test_frac_bounds_derived(self):
        d = fx("oracle_samples_wl1s.json")
        s = d["samples"]
        s[1]["frac"] = 1
        out = D(s, d["ops"])
        self.assertTrue(out and all(l["confidence"] == "derived" for l in out))


class Fx1(unittest.TestCase):
    """FX1: JW6A-01/06, ADC-W6A-02/03."""

    def test_smooth_exact_needs_one_frame_span(self):
        s = [row(1, 1000000), row(2, 1050000, sy=10), row(3, 1100000, sy=22)]
        out = D(s, [{"op": "scroll", "t_us": 1000100}], smooth=True)
        mv = [l for l in out if l["ideal"] == 3]
        self.assertEqual(len(mv), 2)
        self.assertTrue(all(l["confidence"] == "derived" for l in out), [l["confidence"] for l in out])

    def test_mv_is_pixel_translation_everywhere(self):
        # scroll down 4: content moves up 4 px (CLASS_CONTRACT: previous content translated by (dx, dy))
        out = D([row(1, 1000000), row(2, 1016666, sy=4)], [{"op": "scroll", "t_us": 1008000}])
        self.assertEqual([l["mv"] for l in out if l["ideal"] == 3], [[0, -4]])
        # element moved right/down 7,3: translation (7, 3)
        a = [[9, 100, 100, 50, 50, 1, "aa"]]
        b = [[9, 107, 103, 50, 50, 1, "aa"]]
        out = D([row(1, 1000000, nodes=a), row(2, 1016666, nodes=b)], [])
        self.assertEqual([l["mv"] for l in out if l["ideal"] == 3], [[7, 3]])
        # layout shift: translation
        sh = {"n": 5, "p": [10, 10, 30, 30], "c": [10, 20, 30, 30], "ri": 0}
        out = D([row(1, 1000000), row(2, 1016666, shifts=[sh])], [])
        self.assertEqual([l["mv"] for l in out if l["ideal"] == 3], [[0, 10]])

    def test_gesture_sums_stay_positive_scroll_distance(self):
        labs = [{"ideal": 3, "source": "scroll", "f0": 1, "f1": 2, "mv": [0, -5]}, {"ideal": 3, "source": "scroll", "f0": 2, "f1": 3, "mv": [0, -7]}]
        self.assertEqual(LO.smooth_gesture_sums(labs), [12])

    def test_negative_y_node_is_clipped_not_a_crash(self):
        a = [[9, 0, -22, 1920, 22, 1, "aa"]]
        b = [[9, 0, -22, 1920, 22, 1, "bb"]]
        out = D([row(1, 1000000, nodes=a), row(2, 1016666, nodes=b)], [])
        self.assertEqual(out, [])  # fully outside the viewport: nothing paintable to label
        a = [[9, 0, -10, 100, 30, 1, "aa"]]
        b = [[9, 0, -10, 100, 30, 1, "bb"]]
        out = D([row(1, 1000000, nodes=a), row(2, 1016666, nodes=b)], [])
        self.assertEqual([l["rects"] for l in out], [[[0, 0, 100, 20]]])

    def test_rect_beyond_right_bottom_is_clipped(self):
        a = [[9, 1900, 1070, 100, 100, 1, "aa"]]
        b = [[9, 1900, 1070, 100, 100, 1, "bb"]]
        out = D([row(1, 1000000, nodes=a), row(2, 1016666, nodes=b)], [])
        self.assertEqual([l["rects"] for l in out], [[[1900, 1070, 20, 10]]])

    def test_autofocus_caret_flicker_once(self):
        s = [row(i + 1, 1000000 + i * 16666, focus="f1") for i in range(5)]
        out = D(s, [], caret_rect=[50, 42, 2, 36], period_ms=500)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["ideal"], out[0]["period_ms"], out[0]["confidence"], out[0]["rects"]), (4, 500, "derived", [[50, 42, 2, 36]]))
        self.assertGreater(out[0]["f1"], out[0]["f0"])
        self.assertEqual(D(s, []), [])  # no caret rect declared: nothing guessed

    def test_cli_cursor_px_and_caret_flags(self):
        with tempfile.TemporaryDirectory() as td:
            sp, op = os.path.join(td, "s.json"), os.path.join(td, "l.jsonl")
            json.dump({"samples": [row(1, 1000000, focus="f1"), row(2, 1016666, focus="f1")],
                       "ops": [{"op": "move", "t_us": 1005000, "x": 80, "y": 200, "point": 0}, {"op": "move", "t_us": 1010000, "x": 100, "y": 200, "point": 1}]},
                      open(sp, "w"))
            base = ["--samples", sp, "--w", "1920", "--h", "1080", "--run-id", "r", "--out", op]
            self.assertEqual(LO.main(base + ["--cursor-px", "16", "--caret-rect", "50,42,2,36", "--period-ms", "500"]), 0)
            labs = LC.decode(open(op, "rb").read())  # FX3 L5: the CLI writes the compact form
            self.assertEqual(sorted(l["source"] for l in labs), ["focus", "pointer"])
            ptr = [l for l in labs if l["source"] == "pointer"][0]
            self.assertEqual(ptr["rects"], [[100, 200, 16, 16]])
            self.assertIn("POINTER", ptr["flags"])
            json.dump({"samples": [row(1, 1000000)], "ops": [{"op": "move", "t_us": 1010000}]}, open(sp, "w"))
            self.assertEqual(LO.main(base + ["--cursor-px", "16"]), 2)  # move without x/y: explicit, never silent


class Fx2(unittest.TestCase):
    """W6a FX2: ADC2-W6A-02 (WL-7 pointer moves MOVED ideal 3, as labels_v2/xc_wl7) and ADC2-W6A-03 (malformed input rc 2, never 1)."""

    def test_wl7_moves_are_moved_ideal3(self):
        path = [[200, 200], [400, 200], [600, 300], [800, 300], [1000, 500], [1200, 500], [1400, 700], [1600, 700]]
        ops = [{"op": "move", "t_us": (1000 + j * 500) * 1000 + 50, "x": x, "y": y, "point": j} for j, (x, y) in enumerate(path)]
        out = D([row(1, 900000), row(2, 5000000)], ops, cursor_px=16)
        xc = [json.loads(l) for l in open(os.path.join(FX, "xc_wl7.jsonl")) if l.strip()]
        self.assertEqual([l["ideal"] for l in out], [3] * 7)
        self.assertEqual([l["rects"] for l in out], [l["rects"] for l in xc])
        self.assertTrue(all(abs(a["f0"] - b["f0"]) <= 1 for a, b in zip(out, xc)))  # xc_wl7 carries measured (jittered) act times
        self.assertEqual([l["mv"] for l in out], [[200, 0], [200, 100], [200, 0], [200, 200], [200, 0], [200, 200], [200, 0]])
        self.assertTrue(all("POINTER" in l["flags"] and l["source"] == "pointer" for l in out))  # + STRADDLE: a 16 px box never aligns to s 32

    def _cli(self, samples, extra):
        with tempfile.TemporaryDirectory() as td:
            sp, op = os.path.join(td, "s.json"), os.path.join(td, "l.jsonl")
            with open(sp, "w") as f:
                json.dump({"samples": samples, "ops": []}, f)
            return LO.main(["--samples", sp, "--w", "1920", "--h", "1080", "--run-id", "r", "--out", op] + extra)

    def test_caret_rect_not_four_ints_rc2(self):
        s = [row(1, 1000000, focus="f1"), row(2, 1016666, focus="f1")]
        for bad in ("50,42", "50,42,2", "50,42,2,36,1"):
            self.assertEqual(self._cli(s, ["--caret-rect", bad]), 2, bad)
        self.assertEqual(self._cli(s, ["--caret-rect", "50,42,2,36"]), 0)

    def test_short_node_row_rc2(self):
        for bad in ([[9, 1, 2]], [[9, 1, 2, 3, 4, 1]], [[9, "x", 2, 3, 4, 1, "aa"]], [9]):
            self.assertEqual(self._cli([row(1, 1000000, nodes=bad), row(2, 1016666, nodes=bad)], []), 2, bad)
        with self.assertRaises(ValueError):  # a schema error, not an IndexError caught by the backstop
            D([row(1, 1000000, nodes=[[9, 1, 2, 3, 4, 1]]), row(2, 1016666, nodes=[[9, 1, 2, 3, 4, 1]])], [])
        good = [[9, 1, 2, 3, 4, 1, "aa"]]
        self.assertEqual(self._cli([row(1, 1000000, nodes=good), row(2, 1016666, nodes=good)], []), 0)


class Contract(unittest.TestCase):
    def test_schema_closed_keys(self):
        sch = json.load(open(os.path.join(os.path.dirname(HERE), "label_schema.json")))
        self.assertEqual(sch["additionalProperties"], False)
        self.assertEqual(sorted(sch["required"]), sorted(sch["properties"]))
        self.assertEqual(sorted(sch["properties"]), sorted(LO.KEYS))
        self.assertEqual(sch["properties"]["schema"]["const"], "vqa.oracle.label/v1")
        d = fx("oracle_samples_wl1s.json")
        for l in D(d["samples"], d["ops"]):
            self.assertEqual(sorted(l), sorted(LO.KEYS))
            LO.validate_label(l)
        bad = dict(l, extra=1)
        with self.assertRaises(ValueError):
            LO.validate_label(bad)

    def test_integer_only(self):
        d = fx("oracle_samples_wl1s.json")
        l = D(d["samples"], d["ops"])[0]
        for mut in ({"f0": 1.0}, {"rects": [[0.0, 0, 1, 1]]}, {"mv": [0.5, 1]}):
            with self.assertRaises(ValueError):
                LO.validate_label(dict(l, **mut))

    def test_forbidden_classes_never(self):
        d = fx("oracle_samples_wl1s.json")
        l = D(d["samples"], d["ops"])[0]
        for ideal in (0, 1, 5, 6):
            with self.assertRaises(ValueError):
                LO.validate_label(dict(l, ideal=ideal))
        with self.assertRaises(ValueError):
            LO.validate_label(dict(l, confidence="guess"))
        with self.assertRaises(ValueError):
            LO.validate_label(dict(l, source="bogus"))

    def test_file_cap(self):
        d = fx("oracle_samples_wl1s.json")
        labs = D(d["samples"], d["ops"])
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "o.jsonl")
            n = LO.write_jsonl(p, labs)
            self.assertEqual(os.path.getsize(p), n)
            self.assertGreater(n, 0)
            self.assertEqual([json.loads(x)["event_id"] for x in open(p)], list(range(len(labs))))
            with self.assertRaises(ValueError):
                LO.write_jsonl(p, labs * 400)
            self.assertEqual(os.path.getsize(p), n)  # refused write leaves the old file

    def test_deterministic(self):
        d = fx("oracle_samples_wl4.json")
        a = json.dumps(D(d["samples"], d["ops"]), sort_keys=True)
        b = json.dumps(D(d["samples"], d["ops"]), sort_keys=True)
        self.assertEqual(a, b)
        self.assertNotEqual(a, "[]")

    def test_empty_samples_no_labels(self):
        self.assertEqual(D([], []), [])
        self.assertEqual(D([row(1, 1)], []), [])


class Fx3(unittest.TestCase):
    """W6a FX3 L5: the CLI writes the compact form (label_codec); the 102400 B cap is unchanged and applies to the bytes written."""
    L5 = os.path.join(FX, "l5")

    def test_live_wl1s_cli_fits_cap(self):
        e = json.load(open(os.path.join(self.L5, "wl1s_live_expect.json")))
        with tempfile.TemporaryDirectory() as td:
            op = os.path.join(td, "labels_compact.jsonl")
            rc = LO.main(["--samples", os.path.join(self.L5, "wl1s_live_samples.json"), "--w", "1920", "--h", "1080", "--run-id", e["run_id"], "--out", op])
            self.assertEqual(rc, 0)  # rc 5 (E_CAP, 117690 B) before FX3
            data = open(op, "rb").read()
            self.assertLessEqual(len(data), 24 * 1024)
            labs = LC.decode(data)
            self.assertEqual(len(labs), e["n_labels"])
            self.assertEqual(hashlib.sha256(LC.canonical_v1(labs)).hexdigest(), e["v1_sha256"])  # same information as the v1 file
            self.assertEqual(data, LC.encode_v2(labs))  # FX4: the written form is codec v2

    def test_compact_cap_on_written_bytes(self):
        d = fx("oracle_samples_wl1s.json")
        labs = D(d["samples"], d["ops"])
        n_c = len(LC.encode_v2(labs))
        self.assertLess(n_c, len(LC.canonical_v1(labs)))
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "c.jsonl")
            self.assertEqual(LO.write_compact(p, labs, cap=n_c), n_c)  # exactly at the cap: written
            self.assertEqual(os.path.getsize(p), n_c)
            with self.assertRaises(ValueError):
                LO.write_compact(p, labs[:-1] + [dict(labs[-1], source_ref=labs[-1]["source_ref"] + "x")], cap=n_c)  # one byte over the cap: refused, old file kept
            self.assertEqual(open(p, "rb").read(), LC.encode_v2(labs))
            self.assertEqual(LO.CAP, 100 * 1024)

    def test_cli_cap_rc5(self):
        with tempfile.TemporaryDirectory() as td:
            sp, op = os.path.join(td, "s.json"), os.path.join(td, "l.jsonl")
            s, x, sy = [row(1, 1000000)], 12345, 0  # FX4: v2 deflates regular scrolls far below the cap, so the deltas are pseudo-random (LCG)
            for k in range(120):
                x = (x * 1103515245 + 12345) % 2147483648
                sy += 1 + (x >> 16) % 997
                s.append(row(k + 2, 1000000 + 16666 * (k + 1), sy=sy))
            json.dump({"samples": s, "ops": []}, open(sp, "w"))
            wc = LO.write_compact
            # the real 102400 B boundary is test_compact_cap_on_written_bytes; here the CLI E_CAP -> rc 5 mapping at a 2000 B cap (oracle gate time budget)
            with mock.patch.object(LO, "write_compact", lambda p, labs: wc(p, labs, cap=2000)):
                self.assertEqual(LO.main(["--samples", sp, "--w", "1920", "--h", "1080", "--run-id", "r", "--out", op]), 5)
            self.assertGreater(len(LC.encode_v2(LO.derive(s, [], 1920, 1080, "r"))), 2000)
            self.assertFalse(os.path.exists(op))



class Fx4(unittest.TestCase):
    """W6a FX4 L5: labels_oracle writes codec v2 (deflate text); the 102400 B cap is unchanged and applies to the v2 bytes written."""
    L5B = os.path.join(FX, "l5b")
    WL4 = "/home/user/Música/TeamWork_Antigravity2/campaigns/os-cu-0/w6/evidence/X4L5/wl4_inputs/samples.json"

    def test_write_compact_is_v2(self):
        d = fx("oracle_samples_wl4.json")
        labs = D(d["samples"], d["ops"])
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "labels_compact.jsonl")
            n = LO.write_compact(p, labs)
            data = open(p, "rb").read()
            self.assertEqual((n, data), (len(LC.encode_v2(labs)), LC.encode_v2(labs)))
            self.assertTrue(data.startswith(b'{"codec":"vqa.oracle.label-compact/v2",'))
            self.assertEqual(data.count(b"\n"), 2)
            self.assertEqual(LC.decode(data), labs)

    def test_live_wl4_cli_v2(self):
        if not os.path.exists(self.WL4):
            self.skipTest("live WL-4 samples (C evidence, 64 KB) absent")
        e = json.load(open(os.path.join(self.L5B, "wl4_live_expect.json")))
        with tempfile.TemporaryDirectory() as td:
            op = os.path.join(td, "labels_compact.jsonl")
            rc = LO.main(["--samples", self.WL4, "--w", "1920", "--h", "1080", "--run-id", e["run_id"], "--out", op])
            self.assertEqual(rc, 0)
            self.assertEqual(open(op, "rb").read(), open(os.path.join(self.L5B, "wl4_live_labels_v2.jsonl"), "rb").read())


if __name__ == "__main__":
    unittest.main()
