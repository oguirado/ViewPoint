"""W6a INT wiring: the oracle gate lines, the merged oracle mutants (330, ids unique), ORACLE_SPEC_v1, docs and the AGENTS line."""
import json
import os
import re
import importlib.util
import stat
import subprocess
import tempfile
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CAMPAIGN_RUNNER = "/home/user/Música/TeamWork_Antigravity2/campaigns/os-cu-0/w6/a/slive/run_slive5.sh"


def rd(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


class Wiring(unittest.TestCase):
    def test_gates_sh_oracle_lines(self):
        s = rd("scripts/cu/gates.sh")
        self.assertRegex(s, r"(?m)^g oracle bash scripts/cu/oracle_gates\.sh$")
        self.assertRegex(s, r"(?m)^g oracle-shim python3 .*docker/cu/oracle/bin/chromium")
        bn = [l for l in s.splitlines() if l.startswith("g bash-n ")][0]
        for f in ("docker/cu/oracle/launch-oracle-cu.sh", "scripts/cu/oracle_gates.sh", "docker/cu/tests/test_oracle_launch.sh"):
            self.assertIn(f, bn)
        self.assertNotIn("oracle/bin/chromium", bn, "the shim is python: bash -n would fail")
        self.assertLess(s.index("g oracle bash"), s.index("g mutants "))

    def test_mutants_merged_and_unique(self):
        m = json.loads(rd("scripts/cu/mutants.json"))["mutants"]
        ids = [x["id"] for x in m]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 422)  # 364 + 17 FX3 L5 codec mutants (lc.x3.*, lab.x3.*, xc.x3.*) + 23 FX4 (lc.x4.*, lab.x4.*, xc.x4.*, 2 nl.od2.* RD6C-01) + 18 web prop.* (mutants_proposed)
        orc = json.loads(rd("scripts/cu/oracle_mutants.json"))["mutants"]
        self.assertEqual(len(orc), 39)
        self.assertTrue({x["id"] for x in orc} <= set(ids))
        by = {x["id"]: x for x in m}
        for x in orc:
            self.assertEqual(by[x["id"]], x)
        for x in m:
            self.assertEqual(set(x), {"id", "file", "find", "replace", "precheck", "oracle"})
            self.assertNotEqual(x["find"], x["replace"])

    def test_oracle_mutant_finds_match_once(self):
        for x in json.loads(rd("scripts/cu/mutants.json"))["mutants"]:
            if x["id"].startswith(("orc.", "xc.", "lab.", "nolisten.")) or x["file"].startswith(("docker/cu/oracle", "bench/oracle")):
                self.assertEqual(rd(x["file"]).count(x["find"]), 1, x["id"])

    def test_spec_and_docs(self):
        self.assertEqual(json.loads(rd("bench/oracle/ORACLE_SPEC_v1.json"))["schema"], "os.cu.oracle-spec/v1")
        d = rd("docs/CU_W6A.md")
        for k in ("ORACLE_SPEC_v1", "351", "derived", "run_slive5.sh", "OD-2"):
            self.assertIn(k, d)
        kl = rd("docs/CU_KNOWN_LIMITATIONS.md")
        for k in ("KL-38", "KL-39", "KL-40", "KL-41"):
            self.assertIn(k, kl)
        a = rd("AGENTS.md")
        self.assertRegex(a, r"(?m)^- The Chromium labelling oracle .*read-only and never live")

    def test_launcher_and_flags_unchanged(self):
        import hashlib
        h = lambda p: hashlib.sha256(open(os.path.join(REPO, p), "rb").read()).hexdigest()
        self.assertTrue(h("docker/cu/launch-chromium-cu.sh").startswith("854c560f"))
        self.assertTrue(h("docker/cu/profiles/chromium.flags").startswith("2e3b876e"))

    @unittest.skipUnless(os.path.isfile(CAMPAIGN_RUNNER), "conductor runner lives outside the repo")
    def test_runner_v5(self):
        s = open(CAMPAIGN_RUNNER, encoding="utf-8").read()
        self.assertEqual(subprocess.run(["bash", "-n", CAMPAIGN_RUNNER]).returncode, 0)
        self.assertIn("oracle0|o_wl1s|o_wl1c|o_wl1L|o_wl1h|o_wl4|o_wl3|o_wl7|o_wl2b", s)
        self.assertIn("RESOURCE_ACK", s)
        self.assertNotRegex(s, r"--remote-debugging-(?:port|address)")  # ADC-W6A-07: the runner argv check is argv_policy.py, no spelling list of its own
        self.assertIn("CPUSET must be 10-14", s)
        r = subprocess.run(["bash", CAMPAIGN_RUNNER], env=dict(os.environ, STEPS="bogus"), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertLess(s.index("ORACLE_SPEC_v1 not journalled"), s.index("docker run"))
        for k in ("harvest", "argv_check.py", "no_listener.py", "KEPT_SUM_MAX", "/opt/cu/oracle"):
            self.assertIn(k, s)

    @unittest.skipUnless(os.path.isfile(CAMPAIGN_RUNNER), "conductor runner lives outside the repo")
    def test_runner_int2_fixes(self):
        s = open(CAMPAIGN_RUNNER, encoding="utf-8").read()
        self.assertNotIn("proxy_still_running", s)  # JW6A-02
        self.assertGreater(s.index("oracle_${id}_proxy_no_rc5"), s.index("sampler_wait /tmp/oracle_rows.jsonl"))
        for k in ("import argv_policy", "argv_policy.observed_ok", "--cursor-px", "--caret-rect", "--period-ms", "lo_args.txt", "od2_stop", "OD-2 needed", "proc_net_"):
            self.assertIn(k, s, k)
        self.assertIn("exit 4", s)
        self.assertIn('prereg["steps"]'.replace('prereg', 'pre'), s)  # cursor x,y from the prereg plan
        self.assertNotRegex(s, r"(>>?|sed -i|tee)\s*\S*no_listener\.py")  # never extends the baseline itself (OD-2 is the operator's)

    def test_storage_gate_without_wheel(self):  # ADC-W6A-05: fresh clone, no gitignored wheel, own frames still refused
        import shutil, tempfile
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(os.path.join(t, "scripts", "cu")); os.makedirs(os.path.join(t, "d"))
            for f in ("g_disk_size.py", "oracle_gates.sh"):
                shutil.copy(os.path.join(REPO, "scripts", "cu", f), os.path.join(t, "scripts", "cu", f))
            open(os.path.join(t, "d", "a.json"), "w").write("{}")
            r = subprocess.run(["bash", "scripts/cu/oracle_gates.sh", "--storage", "d"], cwd=t, capture_output=True, text=True, timeout=60)
            self.assertEqual((r.returncode, "STORAGE_NOTE wheel absent" in r.stdout), (0, True), r.stdout + r.stderr)
            open(os.path.join(t, "d", "f.dat"), "wb").write(b"\x89PNG....")
            r = subprocess.run(["bash", "scripts/cu/oracle_gates.sh", "--storage", "d"], cwd=t, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 5)
            self.assertIn("frame_magic", r.stdout)



class OD2OwnerOk(unittest.TestCase):
    """RD6C-01 (FX4): each owner_ok conjunct alone (socket uid, dir uid) is guarded; os.lstat is patched in-process (no real chown)."""
    SOCK = "/tmp/chromium-ab12/SingletonSocket"

    def setUp(self):
        spec = importlib.util.spec_from_file_location("no_listener_rd6c", os.path.join(REPO, "docker", "cu", "oracle", "no_listener.py"))
        self.nl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.nl)
        self.td = tempfile.TemporaryDirectory()
        self.proc, self.tmp, self.fs = (os.path.join(self.td.name, n) for n in ("proc", "tmp", "fs"))
        os.makedirs(os.path.join(self.proc, "net"))
        os.makedirs(self.tmp)
        for n, body in (("tcp", ""), ("tcp6", ""), ("unix", "0000000000000000: 00000002 00000000 00010000 0001 01 4242 %s\n" % self.SOCK)):
            with open(os.path.join(self.proc, "net", n), "w") as f:
                f.write("header\n" + body)

    def tearDown(self):
        self.td.cleanup()

    def _lstat(self, sock_uid, dir_uid, dir_mode=0o700):
        sp = os.path.join(self.fs, self.SOCK.lstrip("/"))
        dp = os.path.dirname(sp)

        def fake(p, *a, **k):
            if p == sp:
                return types.SimpleNamespace(st_uid=sock_uid, st_mode=stat.S_IFSOCK | 0o755)
            if p == dp:
                return types.SimpleNamespace(st_uid=dir_uid, st_mode=stat.S_IFDIR | dir_mode)
            raise FileNotFoundError(p)
        return mock.patch.object(self.nl.os, "lstat", fake)

    def _case(self, sock_uid, dir_uid):
        with self._lstat(sock_uid, dir_uid):
            ok = self.nl.owner_ok(self.fs, self.SOCK, 10001)
            r = self.nl.scan(self.proc, self.tmp, True, self.fs, 10001)
        return ok, r

    def test_control_both_10001_ok(self):
        ok, r = self._case(10001, 10001)
        self.assertTrue(ok)
        self.assertEqual((r["baseline_unix"], r["baseline_drift"], r["violations"]), ([self.SOCK], [], []))

    def test_socket_uid_alone_drift(self):
        ok, r = self._case(10002, 10001)
        self.assertFalse(ok)
        self.assertEqual(r["baseline_drift"], [self.SOCK])
        self.assertIn("baseline owner/mode drift", r["violations"])

    def test_dir_uid_alone_drift(self):
        ok, r = self._case(10001, 10002)
        self.assertFalse(ok)
        self.assertEqual(r["baseline_drift"], [self.SOCK])
        self.assertIn("baseline owner/mode drift", r["violations"])


if __name__ == "__main__":
    unittest.main()
