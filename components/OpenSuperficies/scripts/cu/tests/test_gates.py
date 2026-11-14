"""Tests of the INT gates. Run: python3 -B -m unittest discover -s scripts/cu/tests (from the repo root)."""
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CU = os.path.join(REPO, "scripts", "cu")
RAMROOT = "/dev/shm/vqa-ramws/opensuperficies"
ENV = dict(os.environ, PATH=os.environ.get("PATH", "") + ":/usr/local/go/bin", PYTHONDONTWRITEBYTECODE="1")


def run(args, **kw):
    kw.setdefault("timeout", 280)
    return subprocess.run(args, cwd=REPO, env=ENV, capture_output=True, text=True, **kw)


def gate_frames(*extra):
    return run(["taskset", "-c", "10-14", sys.executable, "-B", os.path.join(CU, "g_disk_frames.py")] + list(extra))


def sweep():
    run([sys.executable, "-B", os.path.join(REPO, "vision", "ramws_shim.py"), "sweep"])


class DiskFrames(unittest.TestCase):
    def tearDown(self):
        sweep()

    def test_disk_frames_clean_passes(self):
        r = gate_frames()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["code"], "OK")

    def test_disk_frames_catches_png_in_build(self):
        r = gate_frames("--plant", "frame.png")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("frame.png", r.stdout)

    def test_disk_frames_catches_raw_in_build(self):
        for name in ("frames.raw", "x.xwd", "Xvfb_screen0", "frames.rgb24", "x.rgb24"):
            r = gate_frames("--plant", name)
            self.assertEqual(r.returncode, 5, name + r.stdout + r.stderr)
            self.assertIn(name, r.stdout)

    def test_disk_frames_catches_leftover_ramws(self):
        r = gate_frames("--plant-ramws")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("E_RAMWS_LEFTOVER", r.stdout)

    def test_disk_frames_usage_rc2(self):
        self.assertEqual(gate_frames("--bogus").returncode, 2)


class DiskSize(unittest.TestCase):
    def size(self, d):
        return run([sys.executable, "-B", os.path.join(CU, "g_disk_size.py"), "--out", d])

    def test_disk_size_fail_evidence_over_100k(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as d:
            ep = os.path.join(d, "fail_evidence", "ep1")
            os.makedirs(ep)
            with open(os.path.join(ep, "a.json"), "wb") as f:
                f.write(b"x" * 1000)
            self.assertEqual(self.size(d).returncode, 0)
            with open(os.path.join(ep, "big.bin"), "wb") as f:
                f.write(b"x" * 101 * 1024)
            r = self.size(d)
            self.assertEqual(r.returncode, 5, r.stdout)
            self.assertIn("fail_evidence_bytes", r.stdout)

    def test_disk_size_fail_evidence_over_six_files(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as d:
            ep = os.path.join(d, "fail_evidence", "ep1")
            os.makedirs(ep)
            for i in range(7):
                open(os.path.join(ep, "f%d" % i), "w").write("x")
            self.assertEqual(self.size(d).returncode, 5)

    def test_disk_size_kept_frames_needs_live_keep(self):
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as d:
            k = os.path.join(d, "kept_frames", "r1")
            os.makedirs(k)
            self.assertEqual(self.size(d).returncode, 5)
            json.dump({"ttl_until": 1}, open(os.path.join(k, "KEEP.json"), "w"))
            self.assertEqual(self.size(d).returncode, 5)
            json.dump({"ttl_until": 4102444800}, open(os.path.join(k, "KEEP.json"), "w"))
            self.assertEqual(self.size(d).returncode, 0)


class Suite(unittest.TestCase):
    def plan(self):
        r = run(["bash", os.path.join(CU, "run_suite.sh"), "--plan"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.splitlines()

    def test_suite_runner_never_rc1(self):
        s = os.path.join(CU, "run_suite.sh")
        self.assertEqual(run(["bash", s, "--bogus"]).returncode, 2)
        self.assertEqual(run(["bash", s, "--selftest-fail"]).returncode, 5)
        self.assertEqual(run(["bash", s, "--plan"]).returncode, 0)

    def test_janitor_runs_first(self):
        steps = [l for l in self.plan() if l.startswith("STEP ")]
        self.assertTrue(steps and steps[0].startswith("STEP sweep"), steps)
        self.assertIn("ramws_shim.py sweep", steps[0])

    def test_suite_exclusions(self):
        out = "\n".join(self.plan())
        self.assertRegex(out, r"SKIP src/surfacebook/pbx")
        self.assertRegex(out, r"SKIP src/surfacebook/grabador")
        self.assertRegex(out, r"STEP go-manager .*-p 1")
        self.assertIn("-timeout 30s", out)
        self.assertIn("vision/tests", out)
        self.assertIn("scripts/cu/tests", out)

    def test_suite_skips_live_display(self):
        # RD-01: the legacy live-display XTest falls back to host :1 and moves the operator pointer
        out = "\n".join(self.plan())
        self.assertRegex(out, r"SKIP src/displaybook/desktop TestXTestWithLiveDisplay")
        self.assertRegex(out, r"STEP go-src .*-skip TestXTestWithLiveDisplay")

    def test_suite_skips_flaky_manager_uid(self):
        # RD1-01: legacy manager uds broken-pipe race makes the suite non-deterministic (KL-07)
        out = "\n".join(self.plan())
        self.assertRegex(out, r"SKIP src/surfacebook/manager TestServidorAdmiteSoloAlUIDConfigurado")
        self.assertRegex(out, r"STEP go-manager .*-skip TestServidorAdmiteSoloAlUIDConfigurado")


class Docs(unittest.TestCase):
    def _er(self):
        return open(os.path.join(REPO, "docs", "ENGINE_REQUESTS.md"), encoding="utf-8").read()

    def test_engine_requests_has_er001_to_er009(self):
        ids = re.findall(r"^## (ER-\d{3}) ", self._er(), re.M)
        self.assertGreaterEqual(len(ids), 9)
        self.assertEqual(ids, ["ER-%03d" % i for i in range(1, len(ids) + 1)])  # contiguous, append-only

    def test_engine_requests_er002_delivered_law2(self):
        """ER-002/ER-006 were delivered at law2 and stay so; the pin in use moved to law4 (W2)."""
        txt = self._er()
        self.assertIn("Pin in use: tag `ve-sealed-1-law4`", txt)
        self.assertIn("2612b3acd67c49234adfaa77ab2b3803d5a81cb5", txt)
        self.assertIn("5e2395d56709274f21123bb5ef714adf52f0cf58f78d4f4938da736c52a7a4a5", txt)
        for er in ("ER-002", "ER-006"):
            self.assertRegex(txt, r"## %s [^\n]*\nstatus: DELIVERED@ve-sealed-1-law2" % er)
        self.assertRegex(txt, r"## ER-003 [^\n]*\nstatus: OPEN \(sub-item[^\n]*DELIVERED@ve-sealed-1-law2")

    def test_engine_requests_has_er007_to_er009(self):
        txt = self._er()
        for er in ("ER-007", "ER-008", "ER-009"):
            self.assertRegex(txt, r"## %s [^\n]*\nstatus: OPEN" % er)
        self.assertIn("ve-rt-ring-1", txt)

    def test_kl_mentions_law4_pin(self):
        txt = open(os.path.join(REPO, "docs", "CU_KNOWN_LIMITATIONS.md"), encoding="utf-8").read()
        row = [l for l in txt.splitlines() if l.startswith("| KL-06 ")]
        self.assertEqual(len(row), 1)
        self.assertIn("ve-sealed-1-law4", row[0])
        self.assertIn("CLOSED", row[0])
        for kl in ("KL-10", "KL-11"):
            self.assertTrue([l for l in txt.splitlines() if l.startswith("| %s " % kl)], kl)

    def test_mutants_include_fx3(self):
        m = json.load(open(os.path.join(CU, "mutants.json")))["mutants"]
        ids = [x["id"] for x in m]
        self.assertGreaterEqual(len(ids), 24)
        for want in ("os.mcp.fx3.RDm1", "os.mcp.fx3.RDm2"):
            self.assertIn(want, ids)

    def test_mutants_cover_w1_lanes(self):
        m = json.load(open(os.path.join(CU, "mutants.json")))["mutants"]
        ids = [x["id"] for x in m]
        self.assertGreaterEqual(len(ids), 32)
        self.assertEqual(len(ids), len(set(ids)))
        for pre in ("os.pin.", "os.prod.", "os.ast.", "os.engine.", "os.bench.", "os.ingest.", "os.wl."):
            self.assertTrue([i for i in ids if i.startswith(pre)], pre)
        for x in m:
            self.assertTrue(x["precheck"] and x["oracle"] and x["find"] != x["replace"], x["id"])

    def test_gates_runs_ast_engine_bench(self):
        txt = open(os.path.join(CU, "gates.sh"), encoding="utf-8").read()
        for want in ("capsule/astcheck/tests", "vision.tests.test_engine_path", "bench/cu/harness.py --dry-run",
                     "bench/wl/recorder.py validate"):
            self.assertIn(want, txt)
        gl = [l.split()[1] for l in txt.splitlines() if l.startswith("g ")]
        self.assertEqual(gl[:3], ["wheel", "wheel-check", "sweep"])
        self.assertIn('taskset -c "${CU_CPUSET:-10-14}"', txt)  # INT3: os-cu-0 owns cpus 10-14

    def test_gitignore_has_build(self):
        lines = open(os.path.join(REPO, ".gitignore")).read().splitlines()
        self.assertIn("build/", lines)

    def test_mutants_json_ten(self):
        m = json.load(open(os.path.join(CU, "mutants.json")))
        ids = [x["id"] for x in m["mutants"]]
        self.assertGreaterEqual(len(ids), 10)
        for want in ("os.disk.png.01", "os.ramws.janitor.02", "os.det.path.03", "os.exec.release.04",
                     "os.exec.rcfromactuator.05", "os.pin.livepath.06", "os.mcp.hostport.07",
                     "os.disk.raw.08", "os.mcp.unverified.09", "os.exec.matchteeth.10"):
            self.assertIn(want, ids)

    def test_kl04_mentions_bypass(self):
        txt = open(os.path.join(REPO, "docs", "CU_KNOWN_LIMITATIONS.md"), encoding="utf-8").read()
        row = [l for l in txt.splitlines() if l.startswith("| KL-04 ")]
        self.assertEqual(len(row), 1, row)
        for w in ("bypass", "not a sandbox", "unwired", "AST"):
            self.assertIn(w, row[0])

    def test_mutants_include_fx2(self):
        m = json.load(open(os.path.join(CU, "mutants.json")))["mutants"]
        ids = [x["id"] for x in m]
        self.assertGreaterEqual(len(ids), 22)
        self.assertEqual(len(ids), len(set(ids)))
        for want in ["os.exec.xtest.M19b", "os.exec.xtest.M20b"] + ["os.mcp.x3.MX%d" % i for i in range(1, 11)]:
            self.assertIn(want, ids)
        for x in m:
            self.assertTrue(x["precheck"] and x["oracle"] and x["find"] != x["replace"], x["id"])

    def test_mutants_cover_fx1_fx2(self):
        m = json.load(open(os.path.join(CU, "mutants.json")))["mutants"]
        ids = [x["id"] for x in m]
        self.assertGreater(len(ids), 36)
        self.assertGreaterEqual(len(ids), 86)
        self.assertEqual(len(ids), len(set(ids)))
        for want in ["os.ast.getattr.23", "os.wl.L5", "os.mcp.astwire.F5b", "os.mcp.prodgui.F11"] + \
                ["os.ast.%s.%d" % (n, i) for n, i in (("alias", 24), ("callroot", 25), ("subtarget", 26), ("pathkey", 27), ("min2json", 28))] + \
                ["os.cu.tracked.%d" % i for i in range(1, 7)] + ["os.wl.dims.%d" % i for i in (1, 2, 3)]:
            self.assertIn(want, ids)
        for pre in ("os.snap.", "os.capture.", "os.ingest.", "os.bench.", "os.wl.", "os.mcp.", "os.cu."):
            self.assertTrue([i for i in ids if i.startswith(pre)], pre)
        for x in m:
            self.assertTrue(x["precheck"] and x["oracle"] and x["find"] != x["replace"], x["id"])

    def test_kl_checker_is_lint_inside_capsule(self):
        txt = open(os.path.join(REPO, "docs", "CU_KNOWN_LIMITATIONS.md"), encoding="utf-8").read()
        row = [l for l in txt.splitlines() if l.startswith("| KL-04 ")]
        self.assertEqual(len(row), 1, row)
        for w in ("lint", "capsule boundary", "wired in production", "closed as tests"):
            self.assertIn(w, row[0])

    def test_gates_builds_wheel_first(self):
        txt = open(os.path.join(CU, "gates.sh"), encoding="utf-8").read()
        gl = [l for l in txt.splitlines() if l.startswith("g ")]
        self.assertTrue(gl, txt)
        self.assertRegex(gl[0], r"^g wheel python3 -B vision/build_wheel\.py$")
        self.assertRegex(gl[1], r"^g wheel-check python3 -B vision/build_wheel\.py --check( \"\$\{PEER_ARGS\[@\]\}\")?$")
        doc = open(os.path.join(REPO, "docs", "CU_W0.md"), encoding="utf-8").read()
        self.assertIn("fresh checkout", doc)

    def test_cu_doc_present(self):
        t = open(os.path.join(REPO, "docs", "CU_W0.md"), encoding="utf-8").read()
        for w in ("srf-cu-snap", "srf-cu-exec", "srf-cu-mcp", "gates.sh", "run_suite.sh"):
            self.assertIn(w, t)


if __name__ == "__main__":
    unittest.main()
