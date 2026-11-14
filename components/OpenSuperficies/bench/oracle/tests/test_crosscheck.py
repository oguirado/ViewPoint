"""Lane X tests: oracle-vs-prereg cross-check on synthetic fixtures (no Chromium)."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ORA = os.path.dirname(HERE)
sys.path.insert(0, ORA)
import crosscheck as XC  # noqa: E402
import label_codec as LC  # noqa: E402

FX = os.path.join(HERE, "fixtures")
ITEMS = {"WL-1s": 24, "WL-1c": 144, "WL-1L": 12, "WL-1h": 12, "WL-4": 180, "WL-3": 5, "WL-7": 7}


def fx(item):
    return os.path.join(FX, "xc_%s.jsonl" % item.lower().replace("-", ""))


def run(item, rows=None):
    rows = XC.load_oracle(fx(item)) if rows is None else rows
    return XC.compare(item, rows, XC.prereg_rows(item))


def sup(rep, cls):
    return [r for r in rep["support"] if r["class"] == cls][0]


class T(unittest.TestCase):
    def test_prereg_counts(self):
        for item, n in ITEMS.items():
            self.assertEqual(len(XC.prereg_rows(item)), n, item)

    def test_agree_all_v3_items(self):
        for item in ("WL-1s", "WL-1c", "WL-1L", "WL-1h", "WL-4", "WL-3"):
            rep = run(item)
            self.assertEqual((rep["prereg_only"], rep["oracle_only"]), (0, 0), item)
            self.assertEqual(rep["agree"], ITEMS[item], item)

    def test_alias_tolerance_exactly_one_frame(self):
        rows = XC.load_oracle(fx("WL-1s"))
        late = copy.deepcopy(rows)
        for r in late:
            r["f0"] += 1
            r["f1"] += 1
        self.assertGreater(run("WL-1s", late)["prereg_only"], 0)  # +1 on top of the fixture jitter may exceed 1
        far = copy.deepcopy(rows)
        for r in far:
            r["f0"] += 2
            r["f1"] += 2
        rep = run("WL-1s", far)
        self.assertEqual(rep["agree"], 0)
        self.assertEqual(rep["prereg_only"], 24)

    def test_mv_sign_flip_disagrees(self):
        rows = XC.load_oracle(fx("WL-1s"))
        for r in rows:
            if r["ideal"] == 3:
                r["mv"] = [-r["mv"][0], -r["mv"][1]]
        rep = run("WL-1s", rows)
        self.assertEqual(rep["prereg_only"], 12)
        self.assertEqual(rep["agree"], 12)

    def test_changed_requires_null_mv(self):
        rows = XC.load_oracle(fx("WL-1s"))
        for r in rows:
            if r["ideal"] == 2:
                r["mv"] = [0, 4]
        self.assertEqual(run("WL-1s", rows)["prereg_only"], 12)

    def test_rect_off_by_one_px_disagrees(self):
        rows = XC.load_oracle(fx("WL-1h"))
        rows[0]["rects"][0][2] -= 1
        rep = run("WL-1h", rows)
        self.assertEqual(rep["prereg_only"], 1)
        self.assertEqual(rep["oracle_only"], 1)

    def test_derived_never_agrees(self):
        rows = XC.load_oracle(fx("WL-1s"))
        for r in rows:
            r["confidence"] = "derived"
        rep = run("WL-1s", rows)
        self.assertEqual(rep["agree"], 0)
        self.assertEqual(rep["prereg_only"], 24)
        self.assertEqual(rep["oracle_only"], 0)
        self.assertEqual(sup(rep, 3)["oracle_derived"], 12)
        self.assertEqual(sup(rep, 3)["oracle_exact"], 0)

    def test_one_to_one_and_oracle_only(self):
        rows = XC.load_oracle(fx("WL-1L"))
        extra = copy.deepcopy(rows[0])
        rep = run("WL-1L", rows + [extra])
        self.assertEqual(rep["agree"], 12)
        self.assertEqual(rep["oracle_only"], 1)

    def test_empty_inputs_fail(self):
        with self.assertRaises(XC.XCError) as c:
            run("WL-1s", [])
        self.assertEqual(c.exception.rc, 2)  # library compare() never passes vacuously
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "e.jsonl")
            open(p, "w").close()
            with self.assertRaises(XC.XCError):
                XC.load_oracle(p)
            r = subprocess.run([sys.executable, "-B", os.path.join(ORA, "crosscheck.py"), "--item", "WL-9", "--oracle", p],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)  # unknown item stays a usage error even with 0 rows

    def test_zero_oracle_labels_is_untested_rc0(self):
        # ADC-W6A-01: WL-2b static focus / WL-3 canvas may yield 0 labels: explicit untested result, rc 0
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "e.jsonl")
            open(p, "w").close()
            for item, npre in (("WL-2b", 2), ("WL-3", 5)):
                r = subprocess.run([sys.executable, "-B", os.path.join(ORA, "crosscheck.py"), "--item", item, "--oracle", p],
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)
                rep = json.loads(r.stdout)
                self.assertEqual((rep["result"], rep["oracle_rows"], rep["agree"], rep["prereg"]), ("untested", 0, 0, npre))
                self.assertTrue(all(x["oracle_exact"] == 0 and x["oracle_derived"] == 0 for x in rep["support"]))
            r = subprocess.run([sys.executable, "-B", os.path.join(ORA, "crosscheck.py"), "--item", "WL-2b", "--oracle", os.path.join(d, "missing")],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)  # a missing file is still an io error

    def test_wl2b_known_item(self):
        self.assertEqual(len(XC.prereg_rows("WL-2b")), 2)
        rep = run("WL-2b")
        self.assertEqual(sup(rep, 4)["oracle_derived"], 1)
        self.assertEqual(sup(rep, 4)["oracle_exact"], 0)
        self.assertEqual(sup(rep, 4)["status"], "tested")
        self.assertEqual(rep["agree"], 1)  # the NOT_CHANGED surround; derived FLICKER never agrees
        self.assertEqual(rep["prereg_only"], 1)
        r = subprocess.run([sys.executable, "-B", os.path.join(ORA, "crosscheck.py"), "--item", "WL-2b", "--oracle", fx("WL-2b")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 5, r.stderr)

    def test_non_integer_rejected(self):
        rows = XC.load_oracle(fx("WL-1s"))
        rows[0]["rects"][0][0] = 1.5
        with self.assertRaises(XC.XCError):
            run("WL-1s", rows)

    def test_wl4_uncovered_discrepancy(self):
        rep = run("WL-4")
        d = [x for x in rep["discrepancies"] if x["kind"] == "uncovered_changed_flag"]
        self.assertEqual(d[0]["count"], 90)
        self.assertEqual(sup(rep, 2)["agree"], 90)
        self.assertEqual(sup(rep, 3)["agree"], 90)
        rows = XC.load_oracle(fx("WL-4"))
        for r in rows:
            if "UNCOVERED" in r["flags"]:
                r["mv"] = [3, 0]  # prereg labels_v3 dx=3 leaks into the oracle: must not agree (AW6-09)
        self.assertEqual(run("WL-4", rows)["prereg_only"], 90)

    def test_wl3_straddle_edges(self):
        rep = run("WL-3")
        d = [x for x in rep["discrepancies"] if x["kind"] == "straddle_edge"]
        self.assertEqual(sorted(d[0]["edges"]), [420, 660])
        self.assertEqual(sup(rep, 0)["prereg"], 4)
        self.assertEqual(sup(rep, 0)["agree"], 4)
        rows = XC.load_oracle(fx("WL-3"))
        rows[0]["rects"] = [[0, 0, 1920, 1080]]  # oracle claims a change over the NOT_CHANGED surround
        rep = run("WL-3", rows)
        self.assertGreater(rep["prereg_only"], 0)

    def test_wl7_subtile_pointer(self):
        rep = run("WL-7")
        self.assertEqual(rep["agree"], 0)
        self.assertEqual(rep["prereg_only"], 7)
        self.assertEqual(rep["oracle_only"], 0)
        d = [x for x in rep["discrepancies"] if x["kind"] == "subtile_pointer"]
        self.assertEqual(d[0]["count"], 7)

    def test_support_table_untested(self):
        rep = run("WL-1s")
        self.assertEqual([r["class"] for r in rep["support"]], [0, 1, 2, 3, 4, 5, 6])
        for c in (1, 4, 5, 6):
            self.assertEqual(sup(rep, c)["status"], "untested")
        self.assertEqual(sup(rep, 3)["status"], "tested")
        self.assertEqual(sup(rep, 3)["agree"], 12)

    def test_cli_rc(self):
        cli = [sys.executable, "-B", os.path.join(ORA, "crosscheck.py")]
        r = subprocess.run(cli + ["--item", "WL-1s", "--oracle", fx("WL-1s")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["agree"], 24)
        r = subprocess.run(cli + ["--item", "WL-7", "--oracle", fx("WL-7")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 5)
        r = subprocess.run(cli + ["--item", "WL-9", "--oracle", fx("WL-1s")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)


class Fx3Compact(unittest.TestCase):
    """W6a FX3 L5: crosscheck reads the compact labels file (label_codec decode) with results identical to the v1 file."""
    L5 = os.path.join(HERE, "fixtures", "l5")

    def _fx(self, item):
        """xc fixtures carry tiles null (crosscheck never reads tiles); the codec needs v1 tiles, so they are derived with the EXT rule."""
        sys.path.insert(0, os.path.dirname(ORA))
        from bench.oracle import labels_oracle as LO
        return [dict(l, tiles=sorted({t for q in l["rects"] for t in LO.rect_tiles(1920, 1080, q)})) for l in XC.load_oracle(fx(item))]

    def _both(self, labels, extra):
        cli = [sys.executable, "-B", os.path.join(ORA, "crosscheck.py")]
        with tempfile.TemporaryDirectory() as td:
            pv, pc, p2 = os.path.join(td, "v1.jsonl"), os.path.join(td, "c.jsonl"), os.path.join(td, "c2.jsonl")
            open(pv, "wb").write(LC.canonical_v1(labels))
            open(pc, "wb").write(LC.encode(labels))
            open(p2, "wb").write(LC.encode_v2(labels))  # FX4: codec v2 too
            self.assertEqual(XC.load_oracle(pc), XC.load_oracle(pv))
            self.assertEqual(XC.load_oracle(p2), XC.load_oracle(pv))
            rv = subprocess.run(cli + ["--oracle", pv] + extra, capture_output=True, text=True, timeout=60)
            rc = subprocess.run(cli + ["--oracle", pc] + extra, capture_output=True, text=True, timeout=60)
            r2 = subprocess.run(cli + ["--oracle", p2] + extra, capture_output=True, text=True, timeout=60)
            self.assertEqual((rc.returncode, rc.stdout), (rv.returncode, rv.stdout))
            self.assertEqual((r2.returncode, r2.stdout), (rv.returncode, rv.stdout))
            return r2

    def test_fixtures_compact_equal(self):
        for item in ITEMS:
            r = self._both(self._fx(item), ["--item", item])
            self.assertEqual(r.returncode, 5 if item == "WL-7" else 0, item)
            self.assertTrue(r.stdout.startswith("{"), item)

    def test_live_wl1s_compact_equal(self):
        sys.path.insert(0, os.path.dirname(ORA))
        from bench.oracle import labels_oracle as LO
        d = json.load(open(os.path.join(self.L5, "wl1s_live_samples.json")))
        labs = LO.derive(d["samples"], d["ops"], 1920, 1080, "slive-w6-WL-1s")
        r = self._both(labs, ["--item", "WL-1s", "--act", os.path.join(self.L5, "wl1s_live_act.jsonl")])
        self.assertIn(r.returncode, (0, 5))
        self.assertEqual(json.loads(r.stdout)["item"], "WL-1s")

    def test_compact_empty_is_untested(self):
        with tempfile.TemporaryDirectory() as td:
            pc = os.path.join(td, "c.jsonl")
            open(pc, "wb").write(LC.encode([]))
            self.assertEqual(XC.load_oracle(pc, allow_empty=True), [])
            with self.assertRaises(XC.XCError):
                XC.load_oracle(pc)

    def test_compact_corrupt_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            pc = os.path.join(td, "c.jsonl")
            c = LC.encode(self._fx("WL-1s")).decode().splitlines()
            open(pc, "w").write("\n".join(c[:-1]) + "\n")  # n mismatch
            with self.assertRaises(XC.XCError) as cm:
                XC.load_oracle(pc)
            self.assertEqual(cm.exception.rc, 2)
            r = subprocess.run([sys.executable, "-B", os.path.join(ORA, "crosscheck.py"), "--item", "WL-1s", "--oracle", pc], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)



class Fx4V2(unittest.TestCase):
    """W6a FX4 L5: crosscheck reads codec v2 files with stdout and rc identical to the v1 and v1-compact files."""
    L5B = os.path.join(HERE, "fixtures", "l5b")

    _fx = Fx3Compact._fx
    _both = Fx3Compact._both

    def test_live_wl4_v2_equal(self):
        labs = LC.decode(open(os.path.join(self.L5B, "wl4_live_labels_v2.jsonl"), "rb").read())
        r = self._both(labs, ["--item", "WL-4", "--act", os.path.join(self.L5B, "wl4_live_act.jsonl")])
        self.assertIn(r.returncode, (0, 5))
        self.assertEqual(json.loads(r.stdout)["item"], "WL-4")

    def test_compact_empty_is_untested(self):
        with tempfile.TemporaryDirectory() as td:
            pc = os.path.join(td, "c.jsonl")
            open(pc, "wb").write(LC.encode_v2([]))
            self.assertEqual(XC.load_oracle(pc, allow_empty=True), [])
            with self.assertRaises(XC.XCError):
                XC.load_oracle(pc)

    def test_compact_corrupt_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            pc = os.path.join(td, "c.jsonl")
            h, b = LC.encode_v2(self._fx("WL-1s")).decode().splitlines()
            hd = json.loads(h)
            hd["inner_sha256"] = "0" * 64
            open(pc, "w").write(json.dumps(hd, sort_keys=True, separators=(",", ":")) + "\n" + b + "\n")
            with self.assertRaises(XC.XCError) as cm:
                XC.load_oracle(pc)
            self.assertEqual(cm.exception.rc, 2)
            r = subprocess.run([sys.executable, "-B", os.path.join(ORA, "crosscheck.py"), "--item", "WL-1s", "--oracle", pc], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
