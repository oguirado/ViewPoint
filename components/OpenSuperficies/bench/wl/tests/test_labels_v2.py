import io, json, os, re, sys, unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
WL = os.path.dirname(HERE)
sys.path.insert(0, WL)
import labels_v2 as L

MAN2 = os.path.join(WL, "prereg_wl_v2.json")
W, H = 1920, 1080


class T(unittest.TestCase):
    def setUp(self):
        self.m = L.load_manifest(MAN2)

    def rows(self, item):
        return L.labels_for(self.m, item, W, H)

    def test_flicker_period_median_integer(self):
        ts = [0, 500000, 1000000, 1500000, 2000000, 2530000, 3030000]  # intervals us: 500,500,500,500,530,500
        self.assertEqual(L.flicker_period_ms(ts), 500)
        self.assertEqual(type(L.flicker_period_ms(ts)), int)
        self.assertEqual(L.flicker_period_ms([0, 530000, 1060000, 1590000]), 530)
        self.assertEqual(L.flicker_period_ms([0, 400000, 1000000]), 400)  # x(k): k=max(1,ceil(500000*2/1e6))=1 -> lower median
        self.assertEqual(L.flicker_period_ms([0]), 0)
        r = self.rows("WL-2b")
        fl = [x for x in r if x["expected_kind"] == "FLICKER"]
        self.assertEqual(len(fl), 1)
        self.assertEqual((fl[0]["period_ms"], fl[0]["rect"]), (500, [50, 42, 2, 36]))
        self.assertTrue(any(x["expected_kind"] == "NOT_CHANGED" for x in r))

    def test_typing_labels_changed_rect_per_keystroke_and_caret_moved_dx(self):
        r = self.rows("WL-2")
        glyph = [x for x in r if x["target"] == "glyph"]
        caret = [x for x in r if x["target"] == "caret"]
        self.assertEqual((len(glyph), len(caret)), (20, 20))
        for i, g in enumerate(glyph):
            self.assertEqual(g["expected_kind"], "CHANGED")
            self.assertEqual(g["rect"], [50 + 12 * i, 42, 12, 36])
            self.assertEqual(g["t_us"], (1000 + 250 * i) * 1000)
        for i, c in enumerate(caret):
            self.assertEqual((c["expected_kind"], c["dx"]), ("MOVED", 12))
            self.assertEqual(c["rect"][0], 50 + 12 * (i + 1))
        m = json.loads(json.dumps(self.m))
        [it for it in m["items"] if it["id"] == "WL-2"][0]["caret_mode"] = "redraw"
        c2 = [x for x in L.labels_for(m, "WL-2", W, H) if x["target"] == "caret"]
        self.assertTrue(all(x["expected_kind"] == "CHANGED" and x["dx"] is None for x in c2))

    def test_scroll_labels_moved_dy_and_exposed_strip(self):
        r = self.rows("WL-1")
        moved = [x for x in r if x["expected_kind"] == "MOVED"]
        strip = [x for x in r if x["expected_kind"] == "CHANGED_REGION"]
        self.assertEqual([x["dy"] for x in moved], [40, 120, 360])
        self.assertEqual([x["rect"] for x in strip], [[0, H - 40, W, 40], [0, H - 120, W, 120], [0, H - 360, W, 360]])
        self.assertEqual(L.exposed_strip(-120, W, H), [0, 0, W, 120])  # upward: top strip
        self.assertEqual(L.exposed_strip(5000, W, H), [0, 0, W, H])  # d = min(h, |dy|)
        self.assertEqual(L.exposed_strip(-5000, W, H), [0, 0, W, H])
        self.assertEqual(L.exposed_strip(0, W, H), [0, H, W, 0])

    def test_pointer_labels_moved_small_rects(self):
        r = self.rows("WL-7")
        self.assertTrue(len(r) >= 7)
        for x in r:
            self.assertEqual(x["expected_kind"], "MOVED")
            self.assertEqual(x["rect"][2:], [16, 16])
            self.assertTrue(0 <= x["rect"][0] <= W - 16 and 0 <= x["rect"][1] <= H - 16)

    def test_frame_window_half_open_exactly_once(self):
        self.assertEqual((L.frame_of(16666), L.frame_of(16667), L.frame_of(0)), (0, 1, 0))
        for k in range(0, 600):
            b = L.frame_start_us(k)
            self.assertEqual(L.frame_of(b), k)
            self.assertEqual(L.frame_of(b - 1) if k else -1, k - 1 if k else -1)
        ev = [0, 1, 16666, 16667, 33333, 33334, 999999, 1000000, 5999999]
        hits = {}
        for t in ev:
            hits.setdefault(L.frame_of(t), []).append(t)
        self.assertEqual(sum(len(v) for v in hits.values()), len(ev))
        for rows in (self.rows("WL-2"), self.rows("WL-1")):
            for x in rows:
                self.assertEqual(x["frame"], L.frame_of(x["t_us"]))

    def test_labels_v2_no_rc1(self):
        src = open(os.path.join(WL, "labels_v2.py")).read()
        self.assertIsNone(re.search(r"sys\.exit\(\s*1\s*\)|return\s+1\b", src))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(L.main(["validate", MAN2]), 0)
        self.assertEqual(out.getvalue().strip(), L.prereg_sha256(self.m))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(L.main(["labels", MAN2, "WL-1", str(W), str(H)]), 0)
        self.assertEqual(len(out.getvalue().strip().splitlines()), 6)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(L.main([]), 2)
            self.assertEqual(L.main(["validate", "/nonexistent.json"]), 2)
            self.assertEqual(L.main(["labels", MAN2, "WL-9", "1", "1"]), 2)
            self.assertEqual(L.main(["labels", MAN2, "WL-1", "0", "5"]), 2)

    def check(self, mut):
        import json as j
        b = j.loads(j.dumps(self.m))
        mut(b)
        with self.assertRaises(L.WLError) as c:
            L.check_manifest(b, WL)
        self.assertEqual(c.exception.rc, 2)

    def test_string_in_integer_field_refused(self):
        self.check(lambda m: m["items"][1].update(line_h="bad"))
        self.check(lambda m: m["items"][1]["steps"][0].update(t_ms="2"))
        self.check(lambda m: m["items"][2]["steps"][0].update(dy="40"))
        self.check(lambda m: m["font"].update(advance_px="12"))
        self.check(lambda m: m.update(fps="60"))

    def test_missing_op_keys_and_text_len_refused(self):
        wl1 = lambda m: [i for i in m["items"] if i["id"] == "WL-1"][0]
        wl7 = lambda m: [i for i in m["items"] if i["id"] == "WL-7"][0]
        wl2 = lambda m: [i for i in m["items"] if i["id"] == "WL-2"][0]
        wl2b = lambda m: [i for i in m["items"] if i["id"] == "WL-2b"][0]
        self.check(lambda m: wl1(m)["steps"][0].pop("dy"))
        self.check(lambda m: wl7(m)["steps"][0].pop("path"))
        self.check(lambda m: wl7(m)["steps"][0].pop("dt_ms"))
        self.check(lambda m: wl2b(m)["steps"].clear())
        self.check(lambda m: wl2(m).update(text_len=19))
        self.check(lambda m: wl2(m)["steps"][0].update(text="Z"))

    def test_negative_dy_content_below_top_strip(self):
        import json as j
        m = j.loads(j.dumps(self.m))
        [i for i in m["items"] if i["id"] == "WL-1"][0]["steps"][0]["dy"] = -120
        r = L.labels_for(m, "WL-1", W, H)
        content = [x for x in r if x["target"] == "content"][0]
        strip = [x for x in r if x["target"] == "strip"][0]
        self.assertEqual(strip["rect"], [0, 0, W, 120])
        self.assertEqual(content["rect"], [0, 120, W, H - 120])
        self.assertEqual(content["rect"][1], strip["rect"][1] + strip["rect"][3])
        pos = [x for x in self.rows("WL-1") if x["target"] == "content"][0]
        self.assertEqual(pos["rect"][:2], [0, 0])

    # ADC2-W3-04: scroll dy=0, move without cursor_px, WL-2b without a 4-int caret_rect are refused at check time (rc 2)
    def test_scroll_dy_zero_refused(self):
        wl1 = lambda m: [i for i in m["items"] if i["id"] == "WL-1"][0]
        self.check(lambda m: wl1(m)["steps"][1].update(dy=0))

    def test_move_needs_cursor_px_and_wl2b_caret_rect(self):
        wl7 = lambda m: [i for i in m["items"] if i["id"] == "WL-7"][0]
        wl2b = lambda m: [i for i in m["items"] if i["id"] == "WL-2b"][0]
        self.check(lambda m: wl7(m).pop("cursor_px"))
        self.check(lambda m: wl7(m).update(cursor_px=0))
        self.check(lambda m: wl2b(m).pop("caret_rect"))
        self.check(lambda m: wl2b(m).update(caret_rect=[50, 42, 2]))
        self.check(lambda m: wl2b(m).update(caret_rect=[50, 42, 0, 36]))

    # CF-01: labels re-timed from the actuator AJSON act_steps (actuator_seq, t_us), never from the plan
    def act(self, item, late_us=0):
        rows, seq = [], 0
        it = [i for i in self.m["items"] if i["id"] == item][0]
        for k, s in enumerate(it["steps"]):
            pts = s["path"] if s["op"] == "move" else [None]
            for j, _ in enumerate(pts):
                seq += 1
                tp = (s["t_ms"] + j * s.get("dt_ms", 0)) * 1000
                rows.append({"schema": "os.cu.wl-act-step/v1", "item": item, "actuator_seq": seq, "step": k + 1, "point": j,
                             "op": s["op"], "t_plan_us": tp, "t_us": tp + late_us + 7 * seq, "rc": 0})
        return rows

    def test_labels_from_act_use_actuator_t_us(self):
        a = self.act("WL-1", late_us=40000)
        r = L.labels_from_act(self.m, "WL-1", W, H, a)
        self.assertEqual(len(r), 6)
        for x in r:
            src = [y for y in a if y["actuator_seq"] == x["actuator_seq"]][0]
            self.assertEqual(x["t_us"], src["t_us"])
            self.assertEqual(x["frame"], L.frame_of(src["t_us"]))
        self.assertEqual([x["t_us"] for x in r if x["target"] == "content"], [1040007, 3040014, 5040021])
        self.assertEqual([x["rect"] for x in r], [x["rect"] for x in self.rows("WL-1")])  # geometry unchanged
        a7 = self.act("WL-7")
        r7 = L.labels_from_act(self.m, "WL-7", W, H, a7)
        self.assertEqual(len(r7), 7)  # point 0 positions the pointer, it is not a labelled move
        self.assertEqual([x["actuator_seq"] for x in r7], list(range(2, 9)))
        self.assertEqual(r7[-1]["t_us"], 4500000 + 7 * 8)
        a2b = self.act("WL-2b", late_us=3000)
        r2b = L.labels_from_act(self.m, "WL-2b", W, H, a2b)
        self.assertEqual({x["until_us"] for x in r2b}, {6003007})
        a2 = self.act("WL-2")
        self.assertEqual(len(L.labels_from_act(self.m, "WL-2", W, H, a2)), 40)

    def test_labels_from_act_divergence_and_actuator_failure_rc5(self):
        def bad(item, mut, rc):
            a = self.act(item)
            mut(a)
            with self.assertRaises(L.WLError) as c:
                L.labels_from_act(self.m, item, W, H, a)
            self.assertEqual(c.exception.rc, rc)
        bad("WL-1", lambda a: a.pop(), 5)  # missing event
        bad("WL-1", lambda a: a[1].update(op="click"), 5)
        bad("WL-1", lambda a: a[2].update(step=2), 5)
        bad("WL-1", lambda a: a[1].update(rc=5), 5)  # actuator failed: no labels
        bad("WL-1", lambda a: a[2].update(t_us=a[1]["t_us"] - 1), 5)  # time went backwards
        bad("WL-1", lambda a: a[1].update(actuator_seq=1), 5)
        bad("WL-1", lambda a: a[0].update(item="WL-2"), 5)
        bad("WL-1", lambda a: a[0].update(t_us="1000"), 2)
        bad("WL-1", lambda a: a[0].update(extra=1), 2)
        bad("WL-1", lambda a: a[0].update(schema="vqa.act-step/v1"), 2)
        bad("WL-7", lambda a: a.append(dict(a[-1], actuator_seq=9, point=8)), 5)

    def test_labels_act_cli(self):
        import tempfile
        d = tempfile.mkdtemp(dir="/tmp")
        p = os.path.join(d, "act.jsonl")
        with open(p, "w") as fh:
            for r in self.act("WL-1", late_us=1000):
                fh.write(json.dumps(r) + "\n")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(L.main(["labels-act", MAN2, "WL-1", str(W), str(H), p]), 0)
        got = [json.loads(x) for x in out.getvalue().strip().splitlines()]
        self.assertEqual(got[0]["t_us"], 1001007)
        with open(p, "a") as fh:
            fh.write("{torn")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(L.main(["labels-act", MAN2, "WL-1", str(W), str(H), p]), 2)
            self.assertEqual(L.main(["labels-act", MAN2, "WL-1", str(W), str(H), "/nonexistent"]), 2)
        os.remove(p)
        os.rmdir(d)


if __name__ == "__main__":
    unittest.main()
