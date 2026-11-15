"""W3 FX1 INT2 tests: g_disk_size per-episode cap on every out dir (ADC-W3-10), FX1 mutant coverage, gates/suite wiring.
Run: python3 -B -m unittest discover -s scripts/cu/tests (from the repo root)."""
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


class DiskSizeEpisodes(unittest.TestCase):
    def test_every_out_dir_episode_capped_at_100k(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as d:
            put(os.path.join(d, "WL-1", "rows.jsonl"), 100 * 1024)
            self.assertEqual(size(d).returncode, 0)
            put(os.path.join(d, "WL-2", "rows.jsonl"), 200 * 1024)
            r = size(d)
            self.assertEqual(r.returncode, 5, r.stdout)
            self.assertEqual(json.loads(r.stdout)["offending"][0]["ep"], "WL-2")

    def test_nested_files_count_and_kept_frames_exempt(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as d:
            put(os.path.join(d, "ep", "a", "x.bin"), 60 * 1024)
            put(os.path.join(d, "ep", "b", "y.bin"), 60 * 1024)
            self.assertEqual(size(d).returncode, 5)
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as d:
            put(os.path.join(d, "kept_frames", "r1", "frames.rgb24"), 300 * 1024)
            json.dump({"ttl_until": 4102444800}, open(os.path.join(d, "kept_frames", "r1", "KEEP.json"), "w"))
            self.assertEqual(size(d).returncode, 0)

    def test_bin_dir_is_an_episode_since_int3(self):  # ADC2-W3-05 replaces the W3 bin/ exemption; g_disk_frames removes work/bin instead
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as d:
            put(os.path.join(d, "bin", "srf-cu-snap"), 300 * 1024)
            self.assertEqual(size(d).returncode, 5)


class Fx1Wiring(unittest.TestCase):
    def mutants(self):
        with open(os.path.join(CU, "mutants.json"), encoding="utf-8") as fh:
            return {x["id"]: x for x in json.load(fh)["mutants"]}

    def test_fx1_mutants_registered_and_reanchored(self):
        by = self.mutants()
        for want in ("os.p.keepq.P8", "os.p.status.P9", "os.p.close.P10", "os.e.root.E7", "os.e.frames.E8", "os.c.trap.F1", "os.c.extra.F2",
                     "os.c.cdp.F3", "os.c.mktemp.F4", "os.c.exec.F5", "os.c.snapumask.F6", "os.w.quota.W5", "os.w.strint.W6",
                     "os.w.negdy.W7", "os.w.duprun.W8", "os.w.resmanifest.W9", "os.w.umaskdrv.W10", "os.w.umaskrec.W11"):
            self.assertIn(want, by)
        for x in by.values():  # JW3-04: no oracle may be killed by a missing test name; every precheck compiles the mutant
            self.assertTrue(x["precheck"] and x["oracle"] and x["find"] != x["replace"], x["id"])
        self.assertIn("TestAnalyzerRejectsChunkIndexMismatch", by["os.e.release.E1"]["oracle"])
        self.assertIn("TestXWDSourceCachesHeaderAfterFrame0", by["os.e.carrier.E2"]["oracle"])
        self.assertIn("errtrace", by["os.c.umask.C3"]["find"])
        self.assertIn("TestKeptRunPacerNotBlockedBySlowHook", by["os.p.keepq.P8"]["oracle"])
        for dead in ("Offload", "TestAnalyzerRunsBeforeRelease", "TestAnalyzerOffCarrierPacerNotBlocked"):
            self.assertFalse([k for k, x in by.items() if dead in x["oracle"] or dead in x["find"]], dead)

    def test_gates_wires_fx1_suites(self):
        with open(os.path.join(CU, "gates.sh"), encoding="utf-8") as fh:
            txt = fh.read()
        for want in ("test_w3_fx1_launcher.sh", "./cmd/srf-cu-snap/...", "build/gate_disk/smoke"):
            self.assertIn(want, txt)


if __name__ == "__main__":
    unittest.main()
