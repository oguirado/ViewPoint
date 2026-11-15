"""End-to-end vcore chain on vadapt/synth: analyze -> (S5 gops) -> pack -> cite-check; rc contract (SPEC_INT R4)."""
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_pipeline import run_cli, tree_sha
from vadapt.synth import SynthAdapter


class TestChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp(prefix="e2e_"))
        SynthAdapter().write_run(cls.d, "flicker", 1)
        cls.man = str(cls.d / "frames.manifest.json")
        cls.v = cls.d / "vision"

    def test_1_analyze_has_gops(self):
        rc, o = run_cli(["analyze", self.man])
        self.assertEqual((rc, o["detail"]["stages"]["s5"]), (0, "ok"))
        for f in ("gops.bin", "gops.jsonl", "vision.json"):
            self.assertTrue((self.v / f).is_file(), f)
        gops = [json.loads(l) for l in (self.v / "gops.jsonl").read_text().splitlines()]
        self.assertTrue(any(t["period"] == 2 for g in gops for t in g["tracks"]))

    def test_2_pack_and_cite(self):
        run_cli(["analyze", self.man])
        rc, o = run_cli(["pack", str(self.v), "--manifest", self.man, "--out", str(self.d / "pk")])
        self.assertEqual((rc, o["code"]), (0, "OK"))
        gops = [json.loads(l) for l in (self.v / "gops.jsonl").read_text().splitlines()]
        k = gops[0]["tracks"][0]["k"]
        good = self.d / "good.json"
        good.write_text(json.dumps({"results": [{"check_id": "c1", "status": "FAIL", "reason_class": "static_content", "pointers": [k]}]}))
        rc, o = run_cli(["cite-check", str(self.v), "--pack", str(self.d / "pk/pack.json"), "--judge-output", str(good)])
        self.assertEqual((rc, o["code"]), (0, "OK"))
        # adversarial: the judge cites an id that does not exist -> rc 3, FAIL becomes CANNOT_SEE
        bad = self.d / "bad.json"
        bad.write_text(json.dumps({"results": [{"check_id": "c1", "status": "FAIL", "reason_class": "static_content", "pointers": ["f9999:t0000"]}]}))
        rc, o = run_cli(["cite-check", str(self.v), "--pack", str(self.d / "pk/pack.json"), "--judge-output", str(bad)])
        self.assertEqual(rc, 3)
        self.assertEqual(o["detail"]["results"][0]["status_out"], "CANNOT_SEE")

    def test_3_pack_budget_rc6_writes_nothing(self):
        run_cli(["analyze", self.man])
        out = self.d / "pk6"
        before = tree_sha(self.v)
        rc, o = run_cli(["pack", str(self.v), "--cap", "20", "--out", str(out)])
        self.assertEqual((rc, o["code"]), (6, "E_BUDGET"))
        self.assertFalse(out.exists() or Path(str(out) + ".tmp").exists())
        self.assertEqual(tree_sha(self.v), before)

    def test_4_pack_without_gops_rc2(self):
        (self.d / "emptyv").mkdir(exist_ok=True)
        run_cli(["analyze", self.man, "--out", str(self.d / "emptyv")])
        (self.d / "emptyv" / "gops.jsonl").write_text("")
        rc, o = run_cli(["pack", str(self.d / "emptyv")])
        self.assertEqual((rc, o["code"]), (2, "E_USAGE"))

    def test_5_cite_stale_params_rc4(self):
        run_cli(["analyze", self.man])
        run_cli(["pack", str(self.v), "--out", str(self.d / "pk5")])
        pk = json.loads((self.d / "pk5/pack.json").read_text())
        pk["params_sha256"] = "0" * 64
        (self.d / "stale.json").write_text(json.dumps(pk))
        j = self.d / "j5.json"
        j.write_text(json.dumps({"results": []}))
        rc, o = run_cli(["cite-check", str(self.v), "--pack", str(self.d / "stale.json"), "--judge-output", str(j)])
        self.assertEqual((rc, o["code"]), (4, "E_STALE"))

    def test_6_analysis_deterministic_incl_gops(self):
        shas = []
        for k in range(3):
            o = self.d / ("det%d" % k)
            run_cli(["analyze", self.man, "--out", str(o)])
            shas.append(tree_sha(o))
        self.assertEqual(shas[0], shas[1])
        self.assertEqual(shas[1], shas[2])


if __name__ == "__main__":
    unittest.main()
