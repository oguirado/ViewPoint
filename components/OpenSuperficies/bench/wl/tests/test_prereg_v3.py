import copy, hashlib, json, os, re, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WL = os.path.dirname(HERE)
sys.path.insert(0, WL)
import labels_v2 as L2
import labels_v3 as L
import recorder as R

MAN1 = os.path.join(WL, "prereg_wl.json")
MAN2 = os.path.join(WL, "prereg_wl_v2.json")
MAN3 = os.path.join(WL, "prereg_wl_v3.json")
V1_SHA = "505aac99904e99e15dbba13242dd8c2731bf803259bb39d80deb684bd934cd58"
V2_SHA = "b2f585b89aec55ffded950614a180c66e42b6ab0b2a2725a4c9f1f6333cc2a12"
V1_FILE = "4d9262f93441f99623dcb4820dcdac3bd056058e33475e7811f7ac966d1e0f0b"
V2_FILE = "e9ef6aad5c34fa76ce85aba3d6aad99cda45ca31e159377423cb3d6a4534e201"
W, H = 1920, 1080


def sha_file(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def scene(m, iid):
    with open(os.path.join(WL, L2._item(m, iid)["scene_file"])) as f:
        return f.read()


class T(unittest.TestCase):
    def setUp(self):
        self.m = L.load_manifest(MAN3)

    def test_v3_parent_v2_v1v2_unchanged(self):
        self.assertEqual(R.prereg_sha256(R.load_manifest(MAN1)), V1_SHA)
        self.assertEqual(L2.prereg_sha256(L2.load_manifest(MAN2)), V2_SHA)
        self.assertEqual((sha_file(MAN1), sha_file(MAN2)), (V1_FILE, V2_FILE))
        self.assertEqual((self.m["schema"], self.m["parent_prereg"]), ("os.cu.prereg-wl/v3", V2_SHA))
        self.assertEqual(L.prereg_sha256(self.m), L.PINNED_PREREG_V3_SHA256)
        self.assertNotIn(L.PINNED_PREREG_V3_SHA256, (V1_SHA, V2_SHA))
        self.assertRegex(L.PINNED_PREREG_V3_SHA256, r"^[0-9a-f]{64}$")
        b = copy.deepcopy(self.m)
        b["parent_prereg"] = V1_SHA
        with self.assertRaises(R.WLError):
            L.check_manifest(b, WL)

    def test_v3_order_closed_int(self):
        self.assertEqual(self.m["capture_order"], ["WL-1s", "WL-1c", "WL-1L", "WL-1h", "WL-4", "WL-3"])
        self.assertEqual((self.m["fps"], self.m["item_ms"], self.m["resolutions"], self.m["label_timebase"]),
                         (60, 6000, [[1920, 1080]], "actuator"))
        self.assertTrue(all(i["xvfb_cursor"] == 0 for i in self.m["items"]))
        self.assertNotIn("WL-7", [i["id"] for i in self.m["items"]])
        for mut in (lambda b: b.update(capture_order=list(reversed(b["capture_order"]))),
                    lambda b: b.update(extra=1),
                    lambda b: b.update(fps=60.0),
                    lambda b: b["items"][0].update(xvfb_cursor=1),
                    lambda b: b["items"][0]["steps"][0].update(dy=4.0),
                    lambda b: b["items"][0]["steps"][0].update(bogus=1),
                    lambda b: b.update(label_timebase="engine"),
                    lambda b: b.update(kept_max_mib=2401)):
            b = copy.deepcopy(self.m)
            mut(b)
            with self.assertRaises(R.WLError):
                L.check_manifest(b, WL)

    def test_v3_scene_sha_no_net(self):
        for it in self.m["items"]:
            p = os.path.join(WL, it["scene_file"])
            self.assertEqual(sha_file(p), it["scene_sha256"])
            src = scene(self.m, it["id"])
            self.assertIn("default-src 'none'", src)
            self.assertNotRegex(src, r"https?://|fetch\(|XMLHttpRequest|Math\.random|Date|performance\.now")
            self.assertRegex(os.path.basename(p), r"^[a-z0-9_]+\.html$")
        self.assertEqual(sha_file(os.path.join(WL, self.m["font"]["font_file"])), self.m["font"]["font_sha256"])
        self.assertEqual(sha_file(os.path.join(WL, "scenes/v3/fonts/DejaVuSansMono.ttf")),
                         sha_file(os.path.join(WL, "scenes/v2/fonts/DejaVuSansMono.ttf")))
        with tempfile.TemporaryDirectory() as td:
            shutil.copytree(os.path.join(WL, "scenes"), os.path.join(td, "scenes"))
            b = copy.deepcopy(self.m)
            it = L2._item(b, "WL-3")
            p = os.path.join(td, it["scene_file"])
            with open(p, "a") as f:
                f.write("<!-- Math.random -->")
            it["scene_sha256"] = sha_file(p)
            with self.assertRaises(R.WLError) as c:
                L.check_manifest(b, td)
            self.assertEqual(c.exception.rc, 5)
            b = copy.deepcopy(self.m)
            L2._item(b, "WL-3")["scene_sha256"] = "0" * 64
            with self.assertRaises(R.WLError) as c:
                L.check_manifest(b, WL)
            self.assertEqual(c.exception.rc, 5)

    def test_wl1s_dy_strip(self):
        rows = L.labels_for(self.m, "WL-1s", W, H)
        moved = [r for r in rows if r["expected_kind"] == "MOVED"]
        strip = [r for r in rows if r["expected_kind"] == "CHANGED_REGION"]
        dys = [4, 8, 16, 24] * 3
        self.assertEqual([r["dy"] for r in moved], dys)
        self.assertEqual([r["rect"] for r in strip], [[0, H - d, W, d] for d in dys])
        self.assertEqual([r["rect"] for r in moved], [[0, 0, W, H - d] for d in dys])
        ts = [r["t_us"] for r in strip]
        self.assertTrue(all(b - a >= 400000 for a, b in zip(ts, ts[1:])))
        self.assertIn("var SEQ=[" + ",".join(map(str, dys)) + "]", scene(self.m, "WL-1s"))
        self.assertIn("scroll-behavior:auto", scene(self.m, "WL-1s"))
        b = copy.deepcopy(self.m)
        L2._item(b, "WL-1s")["steps"][1]["t_ms"] = 1100
        with self.assertRaises(R.WLError):
            L.check_manifest(b, WL)

    def test_wl1c_smootherstep_2_8px(self):
        tab = L.dy_table(24)
        self.assertEqual(L2._item(self.m, "WL-1c")["dy_table"], tab)
        self.assertEqual(tab[:3], [2, 2, 3])
        self.assertEqual((min(tab), max(tab), sum(tab)), (2, 8, 122))
        self.assertEqual(tab, tab[::-1])
        for n in range(18, 31):
            t = L.dy_table(n)
            self.assertEqual(len(t), n)
            self.assertTrue(all(2 <= v <= 8 for v in t), n)
        self.assertEqual(L.dy_table(0), [])
        self.assertIn("var TAB=[" + ",".join(map(str, tab)) + "]", scene(self.m, "WL-1c"))
        rows = L.labels_for(self.m, "WL-1c", W, H)
        moved = [r for r in rows if r["expected_kind"] == "MOVED"]
        self.assertEqual(len(moved), 72)
        g0 = moved[:24]
        self.assertEqual([r["dy"] for r in g0], tab)
        self.assertEqual([r["frame"] for r in g0], list(range(60, 84)))
        self.assertEqual([r["anim_index"] for r in g0], list(range(24)))
        self.assertTrue(all(r["rect"] == [0, 0, W, H - r["dy"]] for r in moved))
        strips = [r for r in rows if r["expected_kind"] == "CHANGED_REGION"]
        self.assertEqual(strips[5]["rect"], [0, H - tab[5], W, tab[5]])
        b = copy.deepcopy(self.m)
        L2._item(b, "WL-1c")["dy_table"][3] = 9
        with self.assertRaises(R.WLError):
            L.check_manifest(b, WL)

    def test_wl1L_pagedown_dy(self):
        rows = L.labels_for(self.m, "WL-1L", W, H)
        mv = [(r["op"], r["dy"], r["rect"]) for r in rows if r["expected_kind"] == "MOVED"]
        self.assertEqual(mv, [("scroll", 200, [0, 0, W, 880])] * 3 + [("key", 960, [0, 0, W, 120])] * 3)
        st = [r["rect"] for r in rows if r["expected_kind"] == "CHANGED_REGION"]
        self.assertEqual(st, [[0, 880, W, 200]] * 3 + [[0, 120, W, 960]] * 3)
        s = scene(self.m, "WL-1L")
        self.assertIn("PAGE=960", s)
        self.assertIn('ev.key==="PageDown"', s)
        b = copy.deepcopy(self.m)
        it = L2._item(b, "WL-1L")
        it["page_dy"] = 1080
        for s_ in it["steps"]:
            if s_["op"] == "key":
                s_["dy"] = 1080
        with self.assertRaises(R.WLError):
            L.check_manifest(b, WL)

    def test_wl1h_dx_strip(self):
        rows = L.labels_for(self.m, "WL-1h", W, H)
        dxs = [40, 120] * 3
        self.assertEqual([r["dx"] for r in rows if r["expected_kind"] == "MOVED"], dxs)
        self.assertEqual([r["rect"] for r in rows if r["expected_kind"] == "MOVED"], [[0, 0, W - d, H] for d in dxs])
        self.assertEqual([r["rect"] for r in rows if r["expected_kind"] == "CHANGED_REGION"], [[W - d, 0, d, H] for d in dxs])
        self.assertIn("var SEQ=[40,120,40,120,40,120]", scene(self.m, "WL-1h"))
        b = copy.deepcopy(self.m)
        L2._item(b, "WL-1h")["steps"][0]["dx"] = 41
        with self.assertRaises(R.WLError):
            L.check_manifest(b, WL)

    def test_wl4_60_30_uncovered(self):
        it = L2._item(self.m, "WL-4")
        self.assertEqual([len(s["path"]) for s in it["steps"]], [60, 30])
        rows = L.labels_for(self.m, "WL-4", W, H)
        mv = [r for r in rows if r["expected_kind"] == "MOVED"]
        un = [r for r in rows if r["expected_kind"] == "CHANGED_REGION"]
        self.assertEqual(([r["dx"] for r in mv[:60]], [r["dx"] for r in mv[60:]]), ([3] * 60, [20] * 30))
        self.assertEqual(mv[0]["rect"], [103, 200, 640, 480])
        self.assertEqual(mv[59]["rect"], [280, 200, 640, 480])
        self.assertEqual(mv[89]["rect"], [880, 200, 640, 480])
        self.assertEqual(un[0]["rect"], [100, 200, 3, 480])
        self.assertEqual(un[60]["rect"], [280, 200, 20, 480])
        self.assertEqual(sum(r["rect"][2] for r in un), 780)
        self.assertEqual(mv[59]["t_us"], 2000000)  # drift-free 60 Hz grid: 60 moves span exactly 1 s
        ev = L.plan_events(it)
        rel1 = [e for e in ev if e[0] == 1 and e[2] == "drag_release"][0][3]
        press2 = [e for e in ev if e[0] == 2 and e[2] == "drag_press"][0][3]
        self.assertGreaterEqual(press2 - rel1, 500000)
        self.assertEqual(len(ev), 62 + 32)
        b = copy.deepcopy(self.m)
        L2._item(b, "WL-4")["steps"][1]["t_ms"] = 2100
        with self.assertRaises(R.WLError):
            L.check_manifest(b, WL)
        b = copy.deepcopy(self.m)
        L2._item(b, "WL-4")["div_rect0"] = [1400, 200, 640, 480]
        with self.assertRaises(R.WLError):
            L.check_manifest(b, WL)

    def test_wl3_region_seeded(self):
        it = L2._item(self.m, "WL-3")
        rows = L.labels_for(self.m, "WL-3", W, H)
        pane = [r for r in rows if r["expected_kind"] == "CHANGED_REGION"]
        rest = [r for r in rows if r["expected_kind"] == "NOT_CHANGED"]
        self.assertEqual([r["rect"] for r in pane], [[800, 420, 320, 240]])
        self.assertEqual(sum(r["rect"][2] * r["rect"][3] for r in rest) + 320 * 240, W * H)
        for r in rest:
            x, y, w, h = r["rect"]
            self.assertTrue(x + w <= 800 or x >= 1120 or y + h <= 420 or y >= 660)
        s = scene(self.m, "WL-3")
        self.assertIn("SEED=%d" % it["seed"], s)
        self.assertNotRegex(s, r"Math\.random|Date|performance\.now|function\(ts\)|function\(t\)")

        def imul(a, b):
            return (a * b) & 0xFFFFFFFF

        def hh(t, i):  # Python mirror of the scene's integer hash
            h = imul((it["seed"] ^ imul(t, 0x9E3779B1) ^ imul(i, 0x85EBCA6B)) & 0xFFFFFFFF, 0xC2B2AE35)
            h ^= h >> 15
            h = imul(h, 0x2C1B3C6D)
            h ^= h >> 13
            return h

        for t in range(50):
            diff = sum(hh(t, i) != hh(t + 1, i) for i in range(300))
            self.assertEqual(diff, 300, t)
        self.assertEqual([hh(0, i) for i in range(5)], [hh(0, i) for i in range(5)])
        self.assertIn("h^=h>>>15;h=Math.imul(h,0x2C1B3C6D);h^=h>>>13", s)
        b = copy.deepcopy(self.m)
        L2._item(b, "WL-3")["pane_rect"] = [800, 420, 321, 240]
        with self.assertRaises(R.WLError):
            L.check_manifest(b, WL)


if __name__ == "__main__":
    unittest.main()
