import hashlib, json, os, re, struct, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WL = os.path.dirname(HERE)
sys.path.insert(0, WL)
import labels_v2 as L
import recorder as R

MAN2 = os.path.join(WL, "prereg_wl_v2.json")
MAN1 = os.path.join(WL, "prereg_wl.json")
V1_SHA = "505aac99904e99e15dbba13242dd8c2731bf803259bb39d80deb684bd934cd58"


def sha_file(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


class T(unittest.TestCase):
    def setUp(self):
        self.m = L.load_manifest(MAN2)

    def test_prereg_v2_order_wl7_last(self):
        self.assertEqual(self.m["capture_order"], ["WL-2b", "WL-2", "WL-1", "WL-7"])
        wl7 = [i for i in self.m["items"] if i["id"] == "WL-7"][0]
        self.assertEqual(wl7["xvfb_cursor"], 1)
        self.assertTrue(all(i["xvfb_cursor"] == 0 for i in self.m["items"] if i["id"] != "WL-7"))
        self.assertEqual((self.m["fps"], self.m["segment_min_ms"], self.m["resolutions"]), (60, 5000, [[1920, 1080]]))
        b = json.loads(json.dumps(self.m))
        b["capture_order"] = ["WL-7", "WL-2b", "WL-2", "WL-1"]
        with self.assertRaises(R.WLError):
            L.check_manifest(b, os.path.dirname(MAN2))

    def test_prereg_v2_parent_v1_frozen(self):
        self.assertEqual(self.m["parent_prereg"], V1_SHA)
        self.assertEqual(R.prereg_sha256(R.load_manifest(MAN1)), V1_SHA)
        self.assertEqual(sha_file(MAN1), "4d9262f93441f99623dcb4820dcdac3bd056058e33475e7811f7ac966d1e0f0b")
        self.assertRegex(L.prereg_sha256(self.m), r"^[0-9a-f]{64}$")
        self.assertNotEqual(L.prereg_sha256(self.m), V1_SHA)

    def test_prereg_v2_closed_schema_integer_only(self):
        self.assertEqual(self.m["schema"], "os.cu.prereg-wl/v2")
        base = os.path.dirname(MAN2)
        for mut in (lambda m: m.update(extra=1), lambda m: m["items"][0].update(extra=1),
                    lambda m: m["items"][0]["steps"][0].update(extra=1),
                    lambda m: m.update(fps=60.0), lambda m: m["items"][0].update(period_ms=500.5),
                    lambda m: m["items"][0].update(period_ms=True), lambda m: m.update(schema="os.cu.prereg-wl/v1")):
            b = json.loads(json.dumps(self.m))
            mut(b)
            with self.assertRaises(R.WLError) as c:
                L.check_manifest(b, base)
            self.assertEqual(c.exception.rc, 2)
        b = json.loads(json.dumps(self.m))
        L.check_manifest(b, base)  # the unmutated manifest passes
        wl2b = [i for i in self.m["items"] if i["id"] == "WL-2b"][0]
        self.assertEqual(wl2b["period_ms"], 500)  # AW3-04: pre-registered, never 0

    def test_scene_sha_and_font_sha_pinned(self):
        font = self.m["font"]
        self.assertEqual(sha_file(os.path.join(WL, font["font_file"])), font["font_sha256"])
        for it in self.m["items"]:
            self.assertEqual(sha_file(os.path.join(WL, it["scene_file"])), it["scene_sha256"], it["id"])
            self.assertEqual(it["font_sha256"], font["font_sha256"])
        b = json.loads(json.dumps(self.m))
        b["items"][0]["scene_sha256"] = "0" * 64
        with self.assertRaises(R.WLError) as c:
            L.check_manifest(b, os.path.dirname(MAN2))
        self.assertEqual(c.exception.rc, 5)
        for sc in (False, True):  # font bytes also pinned
            b = json.loads(json.dumps(self.m))
            b["font"]["font_sha256"] = "1" * 64
            b["items"][0]["font_sha256"] = "1" * 64
            with self.assertRaises(R.WLError) as c:
                L.check_manifest(b, os.path.dirname(MAN2))
            self.assertEqual(c.exception.rc, 5)

    def test_scenes_no_network_refs(self):
        d = os.path.join(WL, "scenes", "v2")
        files = [f for f in os.listdir(d) if f.endswith(".html")]
        self.assertEqual(sorted(files), ["wl1_scroll.html", "wl2_type.html", "wl2b_caret.html", "wl7_pointer.html"])
        for f in files:
            s = open(os.path.join(d, f), encoding="utf-8").read()
            self.assertIn("default-src 'none'", s, f)
            self.assertIsNone(re.search(r"(https?:)?//[A-Za-z0-9]", s), f)
            self.assertIsNone(re.search(r"(src|href)=[\"'](?!fonts/)", s), f)
            self.assertIn("scroll-behavior:auto" if f == "wl1_scroll.html" else "@font-face", s)
        conf = open(os.path.join(d, "fonts", "fonts.conf")).read()
        self.assertEqual(re.findall(r"<dir>([^<]*)</dir>", conf), ["/opt/cu/scenes/v2/fonts"])
        self.assertTrue(os.path.getsize(os.path.join(d, "fonts", "LICENSE")) > 0)

    def test_wl2_text_len_20(self):
        it = [i for i in self.m["items"] if i["id"] == "WL-2"][0]
        typed = [s for s in it["steps"] if s["op"] == "type"]
        text = "".join(s["text"] for s in typed)
        self.assertEqual(text, self.m["text_synth"])
        self.assertEqual((len(text), it["text_len"], len(typed)), (20, 20, 20))
        self.assertTrue(all(32 <= ord(c) < 127 for c in text))
        ts = [s["t_ms"] for s in typed]
        self.assertTrue(all(b - a >= 250 for a, b in zip(ts, ts[1:])))

    def test_font_advance_matches_hmtx(self):
        data = open(os.path.join(WL, self.m["font"]["font_file"]), "rb").read()
        n = struct.unpack(">H", data[4:6])[0]
        tabs = {data[12 + 16 * i:16 + 16 * i].decode(): struct.unpack(">II", data[20 + 16 * i:28 + 16 * i]) for i in range(n)}
        upem = struct.unpack(">H", data[tabs["head"][0] + 18:tabs["head"][0] + 20])[0]
        adv0 = struct.unpack(">H", data[tabs["hmtx"][0]:tabs["hmtx"][0] + 2])[0]
        f = self.m["font"]
        self.assertEqual((upem, adv0), (f["units_per_em"], f["advance_units"]))
        self.assertEqual(f["advance_px"], f["advance_units"] * f["font_px"] // f["units_per_em"])  # 1233*20/2048 = 12.04 -> 12


if __name__ == "__main__":
    unittest.main()
