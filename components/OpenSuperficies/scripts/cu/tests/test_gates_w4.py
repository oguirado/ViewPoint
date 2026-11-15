"""W4 INT tests: mutant registration (>= 280, every find unique, no INVALID), gates.sh runs the W4 suites, prereg v3 sha in docs, KL closed claims match code,
RD3-03 build-side scoping, runner patch markers.
Run: python3 -B -m unittest scripts.cu.tests.test_gates_w4 (or discover -s scripts/cu/tests) from the repo root."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CU = os.path.join(REPO, "scripts", "cu")
RUNNER = "/home/user/Música/TeamWork_Antigravity2/campaigns/os-cu-0/w4/slive/run_slive4.sh"
V3_SHA = "15be6e44b486a310a429ac42c902f7f9ba0d7ae5ea98a7471e4e7c3fb4e8cb20"


def read(*p):
    with open(os.path.join(REPO, *p), encoding="utf-8") as fh:
        return fh.read()


def mutants():
    return json.loads(read("scripts", "cu", "mutants.json"))["mutants"]


class W4(unittest.TestCase):
    @staticmethod
    def put(p):
        with open(p, "wb") as f:
            f.write(b"x")

    def test_mutants_cover_w4_lanes(self):
        ms = mutants()
        ids = [m["id"] for m in ms]
        self.assertGreaterEqual(len(ms), 300)
        self.assertEqual(len(ids), len(set(ids)))
        for pfx, n in (("os.v.", 9), ("os.b.", 7), ("os.m.", 19), ("os.r7.", 3)):
            self.assertGreaterEqual(sum(1 for i in ids if i.startswith(pfx)), n, pfx)
        for must in ("os.m.nonblock.M1", "os.m.nofollow.M2", "os.m.isreg.M3", "os.m.cap.M5", "os.m.nonce.M6", "os.m.stale.M7", "os.m.startnofollow.M10",
                     "os.m.inscreen.M13", "os.m.rd303.M19", "os.b.keep.B1", "os.b.start.B3", "os.v.dytable.V1", "os.r7.idem.R1"):
            self.assertIn(must, ids)
        # INVALID guard: every find still matches exactly once in the tracked file (the runner reports INVALID otherwise)
        for m in ms:
            self.assertEqual(read(m["file"]).count(m["find"]), 1, m["id"])
            self.assertNotEqual(m["find"], m["replace"], m["id"])
        by = {m["id"]: m for m in ms}
        for i in ("os.fd.resident_end", "os.p.status.P9", "os.p.hostfail.P17", "os.p.readydet.P22", "os.h.start.H5", "os.h.actlab.H6", "os.h.readyreq.H7"):
            self.assertIn(i, by)  # the 7 ids re-anchored in W4 stay registered

    def test_gates_runs_w4_suites(self):
        g = read("scripts", "cu", "gates.sh")
        for frag in ("bench.wl.tests.test_prereg_v3", "bench.wl.tests.test_labels_v3", "bench.wl.tests.test_driver_markers", "scripts.cu.g_disk_frames_m_test",
                     "labels_v3.py validate bench/wl/prereg_wl_v3.json", "docker/cu/tests/test_diag.sh", "docker/cu/tests/test_launch_chromium_cu.sh",
                     "./src/cubook/exec/...", "./cmd/srf-cu-exec/...", "docker/cu/diag/harvest.sh", "g_disk_frames.py", "g_disk_size.py", "run_mutants.py",
                     "bench.wl.tests.test_driver_fx1", "TestWlcapNonceFailRc5", "TestRunEmptyNonceRefused", "TestV3RectOffScreenRefused", "TestV3DragRectOffScreenRefused",
                     "TestV3RectCheckBoundsStart"):
            self.assertIn(frag, g, frag)
        # go test -run on a name that does not exist exits 0 ("no tests to run"): every named FX1 test must be defined in a _test.go
        for name, path in (("TestRunEmptyNonceRefused", ("src", "cubook", "capture", "livedriver_fx1_test.go")), ("TestWlcapNonceFailRc5", ("cmd", "srf-cu-wlcap", "main_fx1_test.go")),
                           ("TestV3RectOffScreenRefused", ("src", "cubook", "exec", "fx1_test.go")), ("TestV3DragRectOffScreenRefused", ("src", "cubook", "exec", "fx1_test.go")),
                           ("TestV3RectCheckBoundsStart", ("src", "cubook", "exec", "fx1_test.go"))):
            self.assertIn("func %s(" % name, read(*path), name)
        self.assertLess(g.index("sweep python3"), g.index("g unit"))  # janitor first

    def test_prereg_v3_sha_in_docs(self):
        spec = importlib.util.spec_from_file_location("labels_v3", os.path.join(REPO, "bench", "wl", "labels_v3.py"))
        sys.path.insert(0, os.path.join(REPO, "bench", "wl"))
        try:
            lib = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(lib)
        finally:
            sys.path.pop(0)
        self.assertEqual(lib.PINNED_PREREG_V3_SHA256, V3_SHA)
        self.assertEqual(lib.prereg_sha256(json.loads(read("bench", "wl", "prereg_wl_v3.json"))), V3_SHA)
        for doc in ("CU_W4.md", "CU_KNOWN_LIMITATIONS.md"):
            self.assertIn(V3_SHA, read("docs", doc), doc)
        self.assertIn("b2f585b89aec55ffded950614a180c66e42b6ab0b2a2725a4c9f1f6333cc2a12", read("docs", "CU_W4.md"))  # v2 stays frozen

    def test_kl_closed_match_code(self):
        kl = read("docs", "CU_KNOWN_LIMITATIONS.md")
        row = [l for l in kl.split("\n") if l.startswith("| KL-32 |")]
        self.assertEqual(len(row), 1)
        for term in ("ADC3-W3-01", "ADC3-W3-02", "ADC3-W3-03", "RD3-03"):
            self.assertIn(term, row[0])
        code = {"ModeBench": ("src", "cubook", "capture", "livedriver.go"), "func openMarker": ("src", "cubook", "capture", "analyzer.go"),
                "def _open_marker": ("bench", "wl", "driver.py"), "func readStart": ("cmd", "srf-cu-exec", "wl.go"), "func InScreen": ("src", "cubook", "exec", "bounds.go"),
                "ignored_foreign": ("scripts", "cu", "g_disk_frames.py"), "def check_build": ("scripts", "cu", "g_disk_frames.py")}
        for term, path in code.items():
            self.assertIn(term, read(*path), term)
        for kid in ("KL-30", "KL-31"):  # live values stay [H], never claimed closed
            r = [l for l in kl.split("\n") if l.startswith("| %s |" % kid)][0]
            self.assertTrue("[H]" in r or "OPEN" in r, kid)
        self.assertNotIn("| KL-30 | CLOSED", kl)
        eng = read("docs", "ENGINE_REQUESTS.md")
        self.assertNotIn("W4", eng.split("\n")[0])  # no W4 engine request was drafted by a writer bus

    def test_docs_record_fx1(self):
        w4, kl = read("docs", "CU_W4.md"), read("docs", "CU_KNOWN_LIMITATIONS.md")
        for t in ("JW4-01", "JW4-02", "JW4-03", "JW4-04", "JW4-05", "JW4-06", "ADC-W4-01", "ADC-W4-02", "ADC-W4-03", "FX1"):
            self.assertIn(t, w4, t)
        self.assertIn("scripts/cu/g_disk_frames.py", w4.split("JW4-03", 1)[1].split("\n", 1)[0])  # the INT edit of lane M's file is recorded
        row = [l for l in kl.split("\n") if l.startswith("| KL-33 |")]
        self.assertEqual(len(row), 1)
        for t in ("JW4-02", "JW4-05", "JW4-06", "ADC-W4-01", "ADC-W4-02", "ADC-W4-03"):
            self.assertIn(t, row[0], t)
        code = {"existing=": ("docker", "cu", "diag", "harvest.sh"), "NewRunNonce = func": ("src", "cubook", "capture", "livedriver.go"),
                "InRect(r.R": ("src", "cubook", "exec", "wlplan.go"), "os.fchmod": ("bench", "wl", "driver.py")}
        for term, path in code.items():
            self.assertIn(term, read(*path), term)  # a closed claim must have its code witness
        self.assertNotIn("| KL-33 | OPEN", kl)

    def test_gdf_check_build_skips_foreign_rooms(self):
        spec = importlib.util.spec_from_file_location("gdf", os.path.join(CU, "g_disk_frames.py"))
        sys.path.insert(0, REPO)
        try:
            gdf = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(gdf)
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as b:
            for sub in ("room_L4/tests", "gate_disk/123"):
                os.makedirs(os.path.join(b, sub))
            self.put(os.path.join(b, "room_L4", "tests", "f.png"))
            self.assertEqual(gdf.check_build(b, 0), ([], 1))  # foreign room PNG: counted, never fails
            self.put(os.path.join(b, "gate_disk", "123", "own.png"))
            bad, ign = gdf.check_build(b, 0)
            self.assertEqual((len(bad), ign), (1, 1))  # own gate_disk frame still fails
            self.assertTrue(bad[0].endswith("own.png"))

    def test_runner_v4_patch_markers(self):
        if not os.path.isfile(RUNNER):
            self.skipTest("conductor runner not present")
        with open(RUNNER, encoding="utf-8") as fh:
            s = fh.read()
        r = subprocess.run(["bash", "-n", RUNNER], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for frag in ('SBMODE="${SBMODE_OVERRIDE:-nosandbox}"', "--sandbox-mode '$2'", "--quota-bytes \"$KEPT_Q\"", "docker/cu/diag/harvest.sh", "wl7diag_$arm",
                     "--mode bench --det", "--analyze off", "prereg_wl_v3.json", "15be6e44b486a310a429ac42c902f7f9ba0d7ae5ea98a7471e4e7c3fb4e8cb20",
                     "scenes/v3", "KEPT_SUM_MAX=$((10 * 1073741824))", "cpuset", "exec-umask.sh", "ttl_writer.pid", "setfacl -b",
                     "SYNC_VQA.md", "PREREG_WL_V3.json", 'case "$CPUSET" in 10-14)', "head -c 8192", "CU_HARVEST_SUB="):
            self.assertIn(frag, s, frag)
        self.assertNotIn("--mode live", s)
        # JW4-04: CPUSET is 10-14 only. Only code lines are checked: run_slive4.sh:111 has a comment "(RunArgs hardcodes 0,1)" that is not an accepted value
        for ln in s.split("\n"):
            if not ln.lstrip().startswith("#") and "CPUSET" in ln:
                self.assertNotIn("0,1)", ln.split(" # ")[0].split("RunArgs")[0], ln)
        self.assertNotRegex(s, r"10-14\|0,1\)")
        self.assertNotIn("head -c 32768", s)  # JW4-02: diag tails are 8 KiB
        for ln in s.split("\n"):  # harvest before every rm: a bare rm line must be rmcap's own, start_capsule's guarded one or the wl7diag one after harvest
            if "docker rm -f" in ln and not ln.lstrip().startswith("#"):
                self.assertTrue("rmcap()" in ln or "harvest" in ln or ln.strip().startswith("docker rm -f"), ln)
        self.assertEqual(subprocess.run(["bash", RUNNER], capture_output=True, text=True, env=dict(os.environ, STEPS="wl1s,wl1c", RESOURCE_ACK="yes")).returncode, 2)


if __name__ == "__main__":
    unittest.main()
