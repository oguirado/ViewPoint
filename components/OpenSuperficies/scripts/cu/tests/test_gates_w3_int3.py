"""W3 INT3 tests: g_disk_size covers bin/ subdirs and top-level files (ADC2-W3-05), gates smoke dir populated, FX2 mutant registration (ADC2-W3-06, RJ3-01).
Run: python3 -B -m unittest scripts.cu.tests.test_gates_w3_int3 (or discover -s scripts/cu/tests) from the repo root."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CU = os.path.join(REPO, "scripts", "cu")


def size(d):
    return subprocess.run([sys.executable, "-B", os.path.join(CU, "g_disk_size.py"), "--out", d], capture_output=True, text=True)


def put(path, n):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * n)


def read(*p):
    with open(os.path.join(REPO, *p), encoding="utf-8") as fh:
        return fh.read()


class DiskSizeInt3(unittest.TestCase):
    def test_bin_subdir_counts_as_an_episode(self):  # ADC2-W3-05: bin/ is no longer exempt
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as d:
            put(os.path.join(d, "bin", "f"), 204800)
            r = size(d)
            self.assertEqual(r.returncode, 5, r.stdout)
            self.assertEqual(json.loads(r.stdout)["offending"][0]["ep"], "bin")

    def test_top_level_episode_files_capped(self):  # ADC2-W3-05
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as d:
            put(os.path.join(d, "rows.jsonl"), 100 * 1024)
            self.assertEqual(size(d).returncode, 0)
            put(os.path.join(d, "big.jsonl"), 204800)
            r = size(d)
            self.assertEqual(r.returncode, 5, r.stdout)
            self.assertEqual(json.loads(r.stdout)["offending"][0]["code"], "top_file_bytes")

    def test_top_level_symlink_not_followed(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as d, tempfile.NamedTemporaryFile() as big:
            big.write(b"x" * 204800)
            big.flush()
            os.symlink(big.name, os.path.join(d, "ln"))
            self.assertEqual(size(d).returncode, 0)


class SmokeExport(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("g_disk_frames_int3", os.path.join(CU, "g_disk_frames.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_export_smoke_copies_evidence_without_binaries(self):
        mod = self.load()
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as w, tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as dst:
            put(os.path.join(w, "bin", "srf-cu-snap"), 300 * 1024)
            put(os.path.join(w, "snap", "capture_receipt.json"), 100)
            put(os.path.join(w, "steps.jsonl"), 50)
            mod.export_smoke(w, dst)
            self.assertEqual(sorted(os.listdir(dst)), ["snap", "steps.jsonl"])
            self.assertEqual(size(dst).returncode, 0)

    def test_gates_populates_and_asserts_smoke_dir(self):
        txt = read("scripts", "cu", "gates.sh")
        self.assertIn("g_disk_frames.py --smoke-out build/gate_disk/smoke", txt)
        self.assertIn("disk-smoke-nonempty", txt)
        self.assertLess(txt.index("--smoke-out"), txt.index("g_disk_size.py --out build/gate_disk/smoke"))


class XshmClose(unittest.TestCase):
    def test_overlay_fixture_passes_on_the_real_tree(self):  # ADC2-W3-06: the control of os.i.xshmclose.I1 (the mutant makes the same script exit 5)
        r = subprocess.run(["bash", os.path.join(CU, "g_xshm_close.sh")], capture_output=True, text=True, timeout=240)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok", r.stdout)


class Fx2Mutants(unittest.TestCase):
    def by(self):
        return {x["id"]: x for x in json.loads(read("scripts", "cu", "mutants.json"))["mutants"]}

    def test_fx2_lane_mutants_registered(self):
        by = self.by()
        want = ["os.p.closeonce.P11", "os.p.relchk.P12"] + ["os.p.%s.P%d" % (n, i) for n, i in
                (("resacct", 13), ("start", 14), ("startexcl", 15), ("startt0", 16), ("hostfail", 17), ("hostnofollow", 18), ("hostwait", 19),
                 ("hosttimeout", 20), ("hostsha", 21), ("readydet", 22), ("wlwait", 23), ("wlmono", 24))]
        want += ["os.h.%s.H%d" % (n, i) for n, i in (("quota", 1), ("effq", 2), ("readyk", 3), ("antmo", 4), ("start", 5), ("actlab", 6),
                 ("readyreq", 7), ("nostart", 9), ("marker", 10), ("waitms", 11))]
        want += ["os.x.dy0.X1", "os.x.cursorpx.X2", "os.x.caret.X3", "os.x.acttime.X4", "os.x.release.X10", "os.x.stop.X12"]
        want += ["os.r.cpuhard.R1", "os.r.cpuchk.R2", "os.r.mode.R5", "os.r.linchmod.R8"]
        want += ["os.i.xshmclose.I1", "os.i.diskbin.I2", "os.i.disktop.I3", "os.i.execreq.I4"]
        for w in want:
            self.assertIn(w, by)
        self.assertGreaterEqual(len(by), 196 + 40)

    def test_rj3_01_double_close_probes_are_the_oracle(self):
        by = self.by()
        self.assertIn("TestKeptRunHookFailureIsGateNotPanic", by["os.p.closeonce.P11"]["oracle"])
        self.assertIn("TestFailedReleaseOfLastChunkRc5", by["os.p.relchk.P12"]["oracle"])
        self.assertIn("close(jobs)", by["os.p.closeonce.P11"]["replace"])

    def test_every_mutant_has_precheck_and_oracle(self):
        for k, x in self.by().items():
            self.assertTrue(x["precheck"] and x["oracle"] and x["find"] != x["replace"], k)

    def test_driver_exec_rule_mutant_points_at_the_test(self):
        self.assertIn("test_driver_v2_item_with_actions_needs_exec_rc2", self.by()["os.i.execreq.I4"]["oracle"])


if __name__ == "__main__":
    unittest.main()
