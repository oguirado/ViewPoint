"""Route index v2: freshness, byte determinism, abstention, coordinates and the frozen bench."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "route"))
import build as B  # noqa: E402
import query as Q  # noqa: E402


def run(*argv):
    res = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "route", "query.py"), *argv],
                         capture_output=True, text=True, timeout=120, cwd=ROOT)
    return res.returncode, res.stdout


class RouteV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ix = Q.Index(Q.load())

    def test_committed_index_is_fresh(self):
        self.assertEqual(B.check(), [], "run python3 -B route/build.py")

    def test_build_is_byte_deterministic(self):
        self.assertEqual(B.build(), B.build())

    def test_stale_source_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "route", "index"))
            with open(os.path.join(tmp, "a.py"), "w") as fh:
                fh.write("def alpha():\n    return 1\n")
            out = B.build(tmp)
            for name in B.OUTPUTS:
                with open(os.path.join(tmp, "route", "index", name), "w") as fh:
                    fh.write(out[name])
            self.assertEqual(B.check(tmp), [])
            with open(os.path.join(tmp, "a.py"), "a") as fh:
                fh.write("def beta():\n    return 2\n")
            self.assertIn("symbols.jsonl", B.check(tmp))

    def test_every_symbol_row_points_at_its_definition(self):
        cache = {}
        for r in self.ix.rows:
            if r["type"] != "sym":
                continue
            lines = cache.setdefault(r["path"], open(os.path.join(ROOT, r["path"]), encoding="utf-8").read().splitlines())
            name = r["id"].rsplit(".", 1)[-1]
            self.assertRegex(lines[r["line"] - 1], r"^\s*(async\s+)?(def|class)\s+" + name + r"\b", r["id"])

    def test_absent_environments_abstain_with_rc3(self):
        for q in ("stream an Unreal Engine sublevel", "read the SolidWorks feature tree", "open a Houdini HDA"):
            rc, out = run("any", q)
            self.assertEqual(rc, 3, q)
            self.assertEqual(json.loads(out)["abstain"], "terms_absent_from_corpus")

    def test_simulation_adapter_is_routed_once_it_exists(self):
        rc, out = run("any", "MuJoCo simulation adapter", "--k", "5")
        self.assertEqual(rc, 0, out)
        self.assertTrue(any(r["id"].startswith("vqa3d.adapters.mujoco_sim") for r in json.loads(out)["rows"]))

    def test_known_question_answers_with_coordinates(self):
        rc, out = run("any", "which function detects magenta pixels in a frame", "--k", "5")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertIn("bv.oracles.pixel.frame_magenta", [r["id"] for r in data["rows"]])
        self.assertLessEqual(len(out.encode("utf-8")), Q.MAX_B + 1)

    def test_env_axis_marks_blender_side_code(self):
        bpy_rows = [r for r in self.ix.rows if r["type"] == "sym" and r["path"].startswith("bpy_side/")]
        self.assertTrue(bpy_rows)
        self.assertTrue(all(r["env"] == "bpy" for r in bpy_rows))
        self.assertEqual({r["env"] for r in self.ix.rows if r["type"] == "sym"}, {"bpy", "blender", "engine", "generic"})

    def test_frozen_bench_holds_its_floors(self):
        rc, out = run("bench")
        self.assertEqual(rc, 0, out)
        bench = json.loads(out)["bench"]
        for split in ("TEST", "HOLD"):
            self.assertEqual(bench[split]["neg_answered"], [], split)


if __name__ == "__main__":
    unittest.main()
