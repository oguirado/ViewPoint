"""ORACLE_SPEC_v1.json: every sha is recomputed from the tracked files (prereg-first, R6). Not journalled here: the conductor journals it."""
import hashlib
import json
import os
import re
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SPEC = os.path.join(REPO, "bench", "oracle", "ORACLE_SPEC_v1.json")
HEX = re.compile(r"^[0-9a-f]{64}$")
STEPS = {"o_wl1s", "o_wl1c", "o_wl1L", "o_wl1h", "o_wl4", "o_wl3", "o_wl7", "o_wl2b"}


def sha(rel):
    with open(os.path.join(REPO, rel), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def spec():
    with open(SPEC, encoding="utf-8") as f:
        return json.load(f)


class OracleSpec(unittest.TestCase):
    def test_shape(self):
        s = spec()
        self.assertEqual(s["schema"], "os.cu.oracle-spec/v1")
        for k in ("label_schema", "allowlist", "scenes", "prereg", "grid_rule", "exact_rule", "derived_rule", "items", "never_emitted"):
            self.assertIn(k, s)
        self.assertEqual(s["never_emitted"], ["UNCHANGED", "JITTER", "FROZEN", "DRIFT"])
        self.assertEqual(s["timebase"]["alias_frames"], 1)

    def test_schema_and_allowlist_sha(self):
        s = spec()
        for k in ("label_schema", "allowlist"):
            self.assertRegex(s[k]["sha256"], HEX)
            self.assertEqual(s[k]["sha256"], sha(s[k]["path"]), k)

    def test_mv_convention_one_sign(self):  # JW6A-06: scorer-visible, one convention for every MOVED source
        mc = spec()["mv_convention"]
        self.assertEqual(set(mc), {"scroll", "window", "layoutshift"})
        for k in ("window", "layoutshift"):
            self.assertEqual(mc[k], "pixel translation")
        self.assertIn("pixel translation", mc["scroll"])
        self.assertIn("[-dScrollX, -dScrollY]", mc["scroll"])

    def test_argv_refusal_sha(self):  # ADC-W6A-07: the argv table the runner and the shim use is frozen too
        a = spec()["argv_refusal"]
        self.assertEqual(a["sha256"], sha(a["path"]))
        self.assertTrue(os.path.isfile(os.path.join(REPO, a["policy"])))

    def test_scene_shas(self):
        s = spec()
        self.assertEqual(len(s["scenes"]), 8)
        for sc in s["scenes"]:
            self.assertRegex(sc["sha256"], HEX)
            self.assertEqual(sc["sha256"], sha(sc["path"]), sc["path"])

    def test_prereg_shas_file_and_canonical(self):
        s = spec()
        for v, mod in (("v3", "labels_v3.py"), ("v2", "labels_v2.py")):
            p = s["prereg"][v]
            self.assertEqual(p["file_sha256"], sha(p["path"]), v)
            r = subprocess.run([sys.executable, "-B", os.path.join(REPO, "bench", "wl", mod), "validate", os.path.join(REPO, p["path"])],
                               capture_output=True, text=True, timeout=60, cwd=REPO)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip().splitlines()[-1], p["canonical_sha256"], v)

    def test_items_cover_scenes_and_steps(self):
        s = spec()
        paths = {sc["path"] for sc in s["scenes"]}
        self.assertEqual({i["step"] for i in s["items"]}, STEPS)
        for i in s["items"]:
            self.assertIn(i["scene"], paths)
            self.assertTrue(os.path.isfile(os.path.join(REPO, i["prereg"])))
        self.assertIn("WL-2b", {i["item"] for i in s["items"]})
        self.assertIn("0 live exact support", s["item_notes"]["WL-2b"])

    def test_items_exist_in_prereg(self):
        s = spec()
        for i in s["items"]:
            with open(os.path.join(REPO, i["prereg"]), encoding="utf-8") as f:
                ids = {x["id"] for x in json.load(f)["items"]}
            self.assertIn(i["item"], ids)

    def test_grid_rule_matches_code(self):
        sys.path.insert(0, REPO)
        from bench.oracle import labels_oracle as lo
        for wh, e in spec()["grid_rule"]["examples"].items():
            W, H = map(int, wh.split("x"))
            self.assertEqual(lo.grid_params(W, H), (e["s"], e["gw"], e["gh"]))

    def test_tamper_detected(self):
        s = spec()
        bad = dict(s["allowlist"], sha256="0" * 64)
        self.assertNotEqual(bad["sha256"], sha(bad["path"]))


if __name__ == "__main__":
    unittest.main()
