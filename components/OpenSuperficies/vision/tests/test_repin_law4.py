import json
import os
import subprocess
import unittest

from vision.tests.helpers import FAKE_FILES, REPO, SCRATCH, make_wheel, run_py, sha256_file

VE = "/home/user/Documentos/vision-engine"
LAW4_TAG = "ve-sealed-1-law4"
LAW4_COMMIT = "2612b3acd67c49234adfaa77ab2b3803d5a81cb5"
LAW4_TAG_OBJECT = "0ad89daa64250df471ad7326377db47aaf230f1f"
LAW4_SHA = "5e2395d56709274f21123bb5ef714adf52f0cf58f78d4f4938da736c52a7a4a5"
LAW3_TAG = "ve-sealed-1-law3"
LAW3_COMMIT = "666d0198b732f089de8bfda626fb187fdca431cc"
LAW3_SHA = "da54f3048be02b25c3a0373f5549492898f6fe717957baa26141dd5ef345bbee"
LAW2_COMMIT = "15714b62225caec064235ff341335a378d462d8e"
LAW2_SHA = "ebcf3854d14ab77e1ae7b6d65fd88ed07e417cef920e3c2b7f6b4149e7416e89"


def _records(name, tag, commit, sha):
    cfg = SCRATCH / ("l4_%s_cfg.json" % name)
    ven = SCRATCH / ("l4_%s_ven.json" % name)
    cfg.write_text(json.dumps({"mode": "shadow", "pin": {"tag": tag, "commit": commit, "wheel_sha256": sha}}), encoding="utf-8")
    ven.write_text(json.dumps({"tag": tag, "commit": commit, "wheel_sha256": sha}), encoding="utf-8")
    return cfg, ven


def _run(wheel, sha, cfg, ven):
    code = ("from vision import pin\npin._activate_for_test(%r, %r, %r, %r)\nimport vadapt.ramws as r\nprint('OK')\n"
            % (str(wheel), sha, str(cfg), str(ven)))
    return run_py(code)


def _git_ve(*a):
    return subprocess.run(["git", "-C", VE] + list(a), capture_output=True, text=True, timeout=60)


class Law4PinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wheel = make_wheel(SCRATCH / "law4" / "vision_engine-0.0.0-py3-none-any.whl", FAKE_FILES)
        cls.sha = sha256_file(cls.wheel)

    def test_pin_accepts_law4_wheel(self):
        cfg, ven = _records("ok", LAW4_TAG, LAW4_COMMIT, LAW4_SHA)
        r = _run(self.wheel, self.sha, cfg, ven)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK", r.stdout)

    def test_pin_refuses_law3_sha(self):
        cfg, ven = _records("law3", LAW3_TAG, LAW3_COMMIT, LAW3_SHA)
        r = _run(self.wheel, self.sha, cfg, ven)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("E_PIN", r.stdout)
        from vision import pin
        self.assertNotEqual(pin.EXPECTED_WHEEL_SHA256, LAW3_SHA)
        self.assertNotEqual(pin.EXPECTED_COMMIT, LAW3_COMMIT)

    def test_pin_refuses_law2_sha(self):
        cfg, ven = _records("law2", "ve-sealed-1-law2", LAW2_COMMIT, LAW2_SHA)
        r = _run(self.wheel, self.sha, cfg, ven)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("E_PIN", r.stdout)

    def test_pin_refuses_tag_object_as_commit(self):
        cfg, ven = _records("tagobj", LAW4_TAG, LAW4_TAG_OBJECT, LAW4_SHA)
        r = _run(self.wheel, self.sha, cfg, ven)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("E_PIN", r.stdout)

    def test_config_vendored_pin_triple_agree(self):
        from vision import build_wheel, pin
        cfg = json.loads((REPO / "config" / "vision.json").read_text(encoding="utf-8"))["pin"]
        ven = json.loads((REPO / "vision" / "VENDORED.json").read_text(encoding="utf-8"))
        want = {"tag": LAW4_TAG, "commit": LAW4_COMMIT, "wheel_sha256": LAW4_SHA}
        self.assertEqual(cfg, want)
        self.assertEqual({k: ven[k] for k in want}, want)
        self.assertEqual((pin.TAG, pin.EXPECTED_COMMIT, pin.EXPECTED_WHEEL_SHA256), tuple(want.values()))
        self.assertEqual((build_wheel.TAG, build_wheel.EXPECTED_COMMIT, build_wheel.EXPECTED_WHEEL_SHA256), tuple(want.values()))
        self.assertEqual(build_wheel.PEER_WHEEL_SHA256, LAW4_SHA)

    def test_build_wheel_uses_tag_not_worktree_law4(self):
        from vision import build_wheel
        self.assertEqual(build_wheel.TAG, LAW4_TAG)
        src = (REPO / "vision" / "build_wheel.py").read_text(encoding="utf-8")
        self.assertIn('"archive", TAG', src)
        self.assertNotIn("VE + \"/vadapt", src)

    def test_law4_tag_peels_to_commit_not_object(self):
        if not os.path.isdir(VE):
            self.skipTest("vision-engine checkout absent")
        self.assertEqual(_git_ve("rev-parse", LAW4_TAG + "^{commit}").stdout.strip(), LAW4_COMMIT)
        self.assertEqual(_git_ve("rev-parse", LAW4_TAG).stdout.strip(), LAW4_TAG_OBJECT)

    def test_law4_ramws_grants_read_acl(self):
        if not os.path.isdir(VE):
            self.skipTest("vision-engine checkout absent")
        r = _git_ve("show", LAW4_TAG + ":vadapt/ramws.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        for tok in ("share_with_uid", "files_read", "E_RAM_SHARE", ".lock", "owner.json"):
            self.assertIn(tok, r.stdout)

    def test_law3_law4_diff_only_ramws_tests_changelog(self):
        if not os.path.isdir(VE):
            self.skipTest("vision-engine checkout absent")
        r = _git_ve("diff", "--stat", LAW3_TAG, LAW4_TAG, "--", "visioncore", "vmem")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        names = sorted(_git_ve("diff", "--name-only", LAW3_TAG, LAW4_TAG).stdout.split())
        self.assertEqual(names, ["CHANGELOG.md", "tests/test_ramws_share.py", "vadapt/ramws.py"])

    def test_real_wheel_share_acl_e2e(self):
        """ADC-W2-06(2): real law4 wheel on tmpfs: getfacl u:10001:r-- on .lock/owner.json, receipt files_read, open line merged."""
        from vision import pin
        if not pin.DEFAULT_WHEEL.is_file():
            self.skipTest("wheel not built")
        root = "/dev/shm/vqa-ramws-test/fv_e2e"
        if not os.path.isdir("/dev/shm/vqa-ramws-test") or not os.access("/dev/shm/vqa-ramws-test", os.W_OK):
            self.skipTest("no writable test ramws root")
        import shutil
        shutil.rmtree(root, ignore_errors=True)
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "OS_CU_TEST_ROOT": "1"}
        proc = subprocess.Popen(["taskset", "-c", "0,1", "python3", "-B", str(REPO / "vision" / "ramws_shim.py"), "session",
                                 "--run-id", "fvacl", "--root", root, "--share-uid", "10001"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=str(REPO), env=env)
        try:
            hello = json.loads(proc.stdout.readline())
            self.assertEqual(hello["code"], "OK", hello)
            self.assertEqual(hello["share"]["uid"], 10001)
            self.assertEqual(hello["share"]["files_read"], [".lock", "owner.json"])
            proc.stdin.write('{"cmd":"receipt"}\n')
            proc.stdin.flush()
            rec = json.loads(proc.stdout.readline())
            self.assertEqual(rec["receipt"]["share"]["files_read"], [".lock", "owner.json"])
            for name in (".lock", "owner.json"):
                f = os.path.join(hello["root"], name)
                g = subprocess.run(["getfacl", "-cp", f], capture_output=True, text=True, timeout=20)
                self.assertIn("user:10001:r--", g.stdout, (f, g.stdout, g.stderr))
        finally:
            try:
                proc.communicate('{"cmd":"close"}\n', timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            shutil.rmtree(root, ignore_errors=True)

    def test_shipped_wheel_is_law4(self):
        from vision import pin
        if not pin.DEFAULT_WHEEL.is_file():
            self.skipTest("wheel not built")
        self.assertEqual(sha256_file(pin.DEFAULT_WHEEL), LAW4_SHA)
        r = run_py("from vision import pin\npin.activate()\nimport vadapt.ramws as r, inspect\n"
                   "print(sorted(inspect.signature(r.open_run).parameters))\nprint('files_read' in inspect.getsource(r))\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("share_with_uid", r.stdout)
        self.assertIn("True", r.stdout)


if __name__ == "__main__":
    unittest.main()
