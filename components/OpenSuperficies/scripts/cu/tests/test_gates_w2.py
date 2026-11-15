"""W2 INT tests: mutant coverage of the W2 lanes, gates bundle, ER status vs pin, KL uid 10001.
Run: python3 -B -m unittest discover -s scripts/cu/tests -t . (from the repo root)."""
import json
import os
import re
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CU = os.path.join(REPO, "scripts", "cu")


def read(*p):
    return open(os.path.join(REPO, *p), encoding="utf-8").read()


class GatesW2(unittest.TestCase):
    def test_mutants_cover_w2_lanes(self):
        m = json.load(open(os.path.join(CU, "mutants.json")))["mutants"]
        ids = [x["id"] for x in m]
        self.assertGreaterEqual(len(ids), 100)
        self.assertEqual(len(ids), len(set(ids)))
        for want in ("os.pin.law2.R1", "os.pin.absent.R2", "os.uid.user.U1", "os.uid.tmpfs.U2", "os.uid.share.U3", "os.uid.chmod.U4",
                     "os.uid.merge.U5", "os.live.gap.D1", "os.live.release.D2", "os.live.det.D3", "os.live.seg.D4", "os.live.order.D5",
                     "os.ast.stored.K1", "os.ast.saveraw.K2", "os.qtile.attest.Q1"):
            self.assertIn(want, ids)
        self.assertGreaterEqual(sum(1 for x in m if x["file"].startswith("docker/")), 1)  # RJ2-04
        for x in m:
            self.assertTrue(x["precheck"] and x["oracle"] and x["find"] != x["replace"], x["id"])
        # the two mutants moved by lane K are re-anchored on the current checker text
        by = {x["id"]: x for x in m}
        self.assertIn("filepath_raw", by["os.ast.pathkey.27"]["find"])
        self.assertIn("isinstance(f, ast.Attribute) and f.attr", by["os.ast.pathcalls.30"]["find"])

    def test_gates_runs_driver_qtile_adw3(self):
        txt = read("scripts", "cu", "gates.sh")
        for want in ("bench/wl/tests", "vision/tests", "./cmd/srf-cu-wlcap/...", "./src/cubook/capture/...",
                     "test_entrypoint_preflight.sh", "capsule/astcheck/tests", "g unit python3 -B -m unittest discover -s scripts/cu/tests"):
            self.assertIn(want, txt)
        self.assertNotIn("test_repin_law2", txt)
        self.assertFalse(os.path.exists(os.path.join(REPO, "vision", "tests", "test_repin_law2.py")))
        self.assertTrue(os.path.exists(os.path.join(REPO, "vision", "tests", "test_repin_law3.py")))

    def test_suite_plan_runs_wlcap(self):
        out = subprocess.run(["bash", os.path.join(CU, "run_suite.sh"), "--plan"], capture_output=True, text=True).stdout
        line = [l for l in out.splitlines() if l.startswith("STEP go-cmd-cu")]
        self.assertEqual(len(line), 1)
        self.assertIn("./cmd/srf-cu-wlcap/...", line[0])

    def test_gates_wheel_check_passes_law4_peer(self):
        txt = read("scripts", "cu", "gates.sh")
        self.assertIn("ve-sealed-1-law4 --out build/gate_disk/peer", txt)
        self.assertIn('--check "${PEER_ARGS[@]}"', txt)
        self.assertIn("g wheel-witness bash -c", txt)  # RJ2-03: anything but equal:5e2395d5 fails
        self.assertIn('grep -q "\\"witness\\": \\"equal:5e2395d5"', txt)

    def test_gates_wheel_check_fails_without_peer(self):
        r = subprocess.run(["python3", "-B", os.path.join(REPO, "vision", "build_wheel.py"), "--check"], capture_output=True, text=True, cwd=REPO)
        w = json.loads(r.stdout.strip().splitlines()[-1])["witness"]
        self.assertFalse(w.startswith("equal:5e2395d5"), w)  # no peer wheel: not equal, so gates.sh must fail on it

    def test_wheel_check_witness_equal(self):
        tool = "/home/user/Documentos/vision-engine/tools/build_wheel.py"
        if not os.path.exists(tool):
            self.skipTest("vision-engine tag tool absent")
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build")) as d:
            b = subprocess.run(["python3", "-B", tool, "ve-sealed-1-law4", "--out", d], capture_output=True, text=True)
            self.assertEqual(b.returncode, 0, b.stderr)
            whl = b.stdout.strip().splitlines()[-1].split("  ", 1)[1]
            r = subprocess.run(["python3", "-B", os.path.join(REPO, "vision", "build_wheel.py"), "--check", "--peer", whl],
                               capture_output=True, text=True, cwd=REPO)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(json.loads(r.stdout.strip().splitlines()[-1])["witness"].startswith("equal:5e2395d5"), r.stdout)

    def test_engine_requests_er010_status_matches_pin(self):
        txt = read("docs", "ENGINE_REQUESTS.md")
        pin = read("vision", "pin.py")
        tag = re.search(r'^TAG = "([^"]+)"', pin, re.M).group(1)
        self.assertEqual(tag, "ve-sealed-1-law4")
        self.assertRegex(txt, r"## ER-010 [^\n]*\nstatus: DELIVERED@" + re.escape(tag))
        self.assertRegex(txt, r"## ER-011 [^\n]*\nstatus: DELIVERED@" + re.escape(tag))
        self.assertIn("u:<uid>:r--", txt)
        ids = re.findall(r"^## (ER-\d{3}) ", txt, re.M)
        self.assertEqual(ids, ["ER-%03d" % i for i in range(1, len(ids) + 1)])

    def test_kl_mentions_uid_10001(self):
        txt = read("docs", "CU_KNOWN_LIMITATIONS.md")
        rows = {l.split()[1]: l for l in txt.splitlines() if l.startswith("| KL-")}
        self.assertIn("10001", rows["KL-01"])
        self.assertIn("ER-011", rows["KL-10"])
        self.assertIn("qtile", rows["KL-14"])
        self.assertIn("ADW3-01", rows["KL-04"])

    def test_cu_w2_doc_present(self):
        t = read("docs", "CU_W2.md")
        for w in ("srf-cu-wlcap", "uid 10001", "handover-1s", "qtile", "TTL", "ve-sealed-1-law4"):
            self.assertIn(w, t)


if __name__ == "__main__":
    unittest.main()
