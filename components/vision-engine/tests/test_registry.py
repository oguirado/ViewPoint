"""T-R1..T-R5 registry tests (SPEC_INT §4)."""
import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from visioncore import canon, ids, layout, params
from visioncore.errors import CODES, VqaError

ROOT = Path(__file__).resolve().parent.parent
RES = ((1280, 720), (1920, 1080), (3840, 2160), (1024, 1024))


def code_of(fn, *a, **k):
    try:
        fn(*a, **k)
    except VqaError as e:
        return e
    return None


class TR1(unittest.TestCase):
    def test_grid_table(self):
        self.assertEqual(layout.grid_params(1280, 720), (16, 80, 45))
        self.assertEqual(layout.grid_params(1920, 1080), (32, 60, 34))
        self.assertEqual(layout.grid_params(3840, 2160), (64, 60, 34))
        self.assertEqual(layout.grid_params(1024, 1024), (16, 64, 64))
        self.assertEqual(layout.grid_params(16, 4320), (32, 1, 135))
        self.assertEqual(layout.grid_ident(1280, 720), "grid:1280x720:s16")

    def test_morton_roundtrip(self):
        for tx in range(0, 256, 5):
            for ty in range(0, 256, 7):
                self.assertEqual(layout.unmorton(layout.morton(tx, ty)), (tx, ty))

    def test_roundtrip_all_kinds(self):
        for W, H in RES:
            s, gw, gh = layout.grid_params(W, H)
            for tx, ty in ((0, 0), (gw - 1, gh - 1), (gw // 2, gh // 3)):
                m = layout.morton(tx, ty)
                for fn, kind in ((ids.field_id, "field"), (ids.region_id, "region")):
                    sid = fn(84, m, W, H)
                    k, f = ids.parse_any(sid, W, H)
                    self.assertEqual((k, f), (kind, {"frame": 84, "morton": m}))
                    self.assertEqual(fn(f["frame"], f["morton"], W, H), sid)
        for sid, kind in (("f57:all", "global"), ("p42:L2:0068", "node"), ("k38:019c", "track"), ("g2", "gop"),
                          ("r_000104_77abe901", "run_id"), ("V-EVT-000019", "unit"), ("T0_Q4", "threshold"),
                          ("r_000104_77abe901/f84:t0a40", "cross_cite"), ("grid:1280x720:s16", "grid"),
                          ("T:operator_zero:combat_hud:1920x1080:ui:0a40-0a7f", "track_key")):
            self.assertEqual(ids.parse_any(sid)[0], kind, sid)
        self.assertEqual(ids.node_id(42, 2, 0x68), "p42:L2:0068")
        self.assertEqual(ids.run_id(104, "77abe901"), "r_000104_77abe901")
        self.assertEqual(ids.episode_id("r_000104_77abe901", 2), "r_000104_77abe901/g2")
        self.assertEqual(ids.field_ids_of_rect((0, 0, 16, 16), 1280, 720, 7), ["f7:t0000"])

    def test_neg_syntax(self):
        for bad in ("f84:t0a4", "f084:t0a40", "f84:t0A40", "f84:t0a400", "f-1:t0000", "r42:1a0", "g02"):
            e = code_of(ids.parse_any, bad)
            self.assertIsNotNone(e, bad)
            self.assertEqual((e.code, e.rc), ("E_VC_ID_SYNTAX", 3), bad)

    def test_neg_range(self):
        for call in (lambda: ids.field_id(1, 65536), lambda: ids.field_id(2 ** 32, 0),
                     lambda: ids.field_id(1, layout.morton(80, 0), 1280, 720),
                     lambda: ids.parse_any("f1:t%04x" % layout.morton(0, 45), 1280, 720)):
            self.assertEqual(code_of(call).code, "E_VC_ID_RANGE")

    def test_neg_grid(self):
        for W, H in ((1, 70000), (0, 10), (8193, 8193)):
            e = code_of(layout.grid_params, W, H)
            self.assertEqual((e.code, e.rc), ("E_GRID", 2))

    def test_region_run_disjoint(self):
        self.assertEqual(ids.parse_any("r_000104_77abe901")[0], "run_id")
        self.assertEqual(ids.parse_any("r42:01a3")[0], "region")


class TR2(unittest.TestCase):
    def test_cls_enum(self):
        self.assertEqual(layout.CLS[0], "UNCHANGED")
        self.assertEqual(layout.cls_id("DRIFT"), 6)
        self.assertFalse(layout.is_dirty(1))
        self.assertTrue(layout.is_dirty(2))
        self.assertEqual(code_of(layout.cls_name, 7).code, "E_SCHEMA")
        self.assertEqual(code_of(layout.cls_id, "UNCH").rc, 2)

    def test_tiles_bin_cls7(self):
        import struct
        ok = struct.pack(layout.TILE_FMT, 0, 6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        bad = struct.pack(layout.TILE_FMT, 0, 7, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        self.assertEqual(len(ok), 32)
        self.assertIsNone(layout.check_tile_records(ok))
        e = code_of(layout.check_tile_records, ok + bad)
        self.assertEqual((e.code, e.rc), ("E_SCHEMA", 2))

    def test_tile_header(self):
        h = layout.pack_tile_header(16, 1280, 720, 80, 45, 3600, 0, 0, "ab" * 32, "1.1.0")
        self.assertEqual(len(h), 64)
        self.assertEqual(layout.parse_tile_header(h)["N"], 3600)
        self.assertEqual(code_of(layout.parse_tile_header, b"X" * 64).code, "E_VC_FORMAT")

    def test_offsets(self):
        import struct
        off = 0
        for fmt, o in zip(re.findall(r"[HBbQ]", layout.TILE_FMT), layout.TILE_OFFSETS):
            self.assertEqual(o, off)
            off += struct.calcsize("<" + fmt)


class TR3(unittest.TestCase):
    def schemas(self):
        return sorted((ROOT / "visioncore" / "schemas").glob("*.schema.json"))

    def test_ids_match(self):
        files = self.schemas()
        self.assertGreaterEqual(len(files), 21)
        for p in files:
            d = json.loads(p.read_text())
            m = re.fullmatch(r"(vqa|vmem)\.([a-z][a-z-]*)/v([0-9]+)", d["$id"])
            self.assertIsNotNone(m, d["$id"])
            self.assertEqual(p.name, "%s.v%s.schema.json" % (d["$id"].split("/")[0], m[3]))
            self.assertEqual(d["properties"]["schema"]["const"], d["$id"])
            self.assertEqual(canon.load_schema(d["$id"]), d)

    def test_dotted_rejected(self):
        for bad in ("vqa.core.region.v1", "vqa.pack.v2", "vmem.unit.v1"):
            self.assertFalse(re.fullmatch(r"(vqa|vmem)\.([a-z][a-z-]*)/v([0-9]+)", bad))
            self.assertEqual(code_of(canon.load_schema, bad).code, "E_SCHEMA")

    def test_registry_complete(self):
        want = """vqa.frame/v1 vqa.region-hint/v1 vqa.tile/v1 vqa.core-frame/v1 vqa.core-link/v1 vqa.core-region/v1
        vqa.profile/v1 vqa.vision/v1 vqa.gop/v1 vqa.pack/v2 vqa.judge-input/v2 vqa.judge-output/v2 vqa.cite-verdict/v1
        vqa.episode/v1 vmem.unit/v1 vmem.catalogue/v1 vmem.answer/v1 vmem.manifest/v1 vqa.px-mutant/v1
        vqa.mutant-manifest/v3 vqa.eval-row/v1""".split()
        for sid in want:
            self.assertEqual(canon.load_schema(sid)["$id"], sid)

    def test_canonical(self):
        self.assertEqual(canon.dumps({"b": 1, "a": [1, "é"]}), '{"a":[1,"é"],"b":1}\n')
        self.assertEqual(code_of(canon.dumps, {"a": 1.5}).code, "E_FLOAT")
        self.assertEqual(code_of(canon.loads, '{"a":1.5}').code, "E_FLOAT")
        self.assertTrue(canon.is_canonical('{"a":1}\n'))
        self.assertFalse(canon.is_canonical('{"a": 1}\n'))


class TR4(unittest.TestCase):
    def test_codes(self):
        self.assertNotIn(1, CODES.values())
        for name, rc in (("E_USAGE", 2), ("E_SCHEMA", 2), ("E_GRID", 2), ("E_CITE", 3), ("NOT_IN_VISUAL_MEMORY", 3),
                         ("E_STALE", 4), ("E_DRIFT", 4), ("E_GATE", 5), ("E_MUTANT_SURVIVED", 5), ("E_BUDGET", 6)):
            e = VqaError(name)
            self.assertEqual(e.rc, rc)
            o = canon.loads(canon.dumps(e.to_obj()))
            self.assertEqual((o["code"], o["rc"]), (name, rc))

    def test_forbidden(self):
        with self.assertRaises(ValueError):
            VqaError("E_X", rc=1)
        with self.assertRaises(ValueError):
            VqaError("E_UNREGISTERED")
        with self.assertRaises(ValueError):
            VqaError("OK")

    def test_refusal_leaves_output(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "o.json"
            out.write_bytes(b"guard")
            sha = hashlib.sha256(out.read_bytes()).hexdigest()
            e = code_of(canon.write_atomic, Path(d) / "missing" / "o.json", "x")
            self.assertEqual(e.code, "E_IO")
            self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), sha)
            canon.write_atomic(out, "new")
            self.assertEqual(out.read_bytes(), b"new")
            self.assertFalse(Path(str(out) + ".tmp").exists())


class TR5(unittest.TestCase):
    def test_ids_and_ints(self):
        for k, v in params.PARAMS.items():
            self.assertRegex(k, params.THRESHOLD_ID_RE.pattern)
            self.assertIs(type(v), int)
            lo, hi = params.BOUNDS[k]
            self.assertTrue(lo <= v <= hi, k)
        self.assertEqual(params.PARAMS["T0_Q4"], 32)
        self.assertEqual(params.PARAMS["HYST_BAND_PERMILLE"], 100)

    def test_thresholds_tsv_bijection(self):
        p = ROOT / "catalogue" / "thresholds.tsv"
        if not p.is_file():
            self.skipTest("catalogue/thresholds.tsv not present yet (lane S3)")
        rows = [l.split("\t")[0].split()[0] for l in p.read_text().splitlines() if l.strip() and not l.startswith("#")]
        rows = [r for r in rows if r != "threshold_id"]
        self.assertEqual(sorted(rows), sorted(params.PARAMS))

    def test_profile(self):
        base = params.params_sha256(params.effective(None))
        self.assertEqual(base, params.params_sha256())
        t = params.effective({"t0_q4": 40})
        self.assertEqual(t["T0_Q4"], 40)
        self.assertNotEqual(params.params_sha256(t), base)
        self.assertEqual(params.params_sha256(params.effective({"T0_Q4": 32})), base)

    def test_profile_neg(self):
        e = code_of(params.effective, {"H_DEAD": 3})
        self.assertEqual((e.code, e.rc), ("E_SCHEMA", 2))
        self.assertEqual(code_of(params.effective, {"NOPE": 1}).code, "E_VC_PARAM_UNKNOWN")
        self.assertEqual(code_of(params.effective, {"T0_Q4": 99999}).code, "E_VC_PARAM_RANGE")
        self.assertEqual(code_of(params.effective, {"T0_Q4": True}).code, "E_VC_PARAM_RANGE")

    def test_sha_iff_row_changes(self):
        base = params.params_sha256()
        for k in ("GOP_K", "QUOTE_MIN_CHARS"):
            t = dict(params.PARAMS)
            t[k] += 1
            self.assertNotEqual(params.params_sha256(t), base)


if __name__ == "__main__":
    unittest.main()
