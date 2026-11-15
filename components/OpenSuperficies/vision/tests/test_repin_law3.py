import json
import os
import subprocess
import unittest

from vision.tests.helpers import FAKE_FILES, REPO, SCRATCH, make_wheel, run_py, sha256_file

VE = "/home/user/Documentos/vision-engine"
LAW3_TAG = "ve-sealed-1-law3"
LAW3_COMMIT = "666d0198b732f089de8bfda626fb187fdca431cc"
LAW3_TAG_OBJECT = "eb88aef2644f9ee7ae27c78e7fb4363eee0aaa0c"
LAW3_SHA = "da54f3048be02b25c3a0373f5549492898f6fe717957baa26141dd5ef345bbee"
LAW4_TAG = "ve-sealed-1-law4"
LAW4_COMMIT = "2612b3acd67c49234adfaa77ab2b3803d5a81cb5"
LAW4_SHA = "5e2395d56709274f21123bb5ef714adf52f0cf58f78d4f4938da736c52a7a4a5"
LAW2_COMMIT = "15714b62225caec064235ff341335a378d462d8e"
LAW2_SHA = "ebcf3854d14ab77e1ae7b6d65fd88ed07e417cef920e3c2b7f6b4149e7416e89"
LAW1_COMMIT = "65a8469266c7fceef599689fbf899691cd1c88b9"
LAW1_SHA = "41e798d6208681f029c9ad5136a47b4e56dcb22f4884279df5d60b6b8a12a22b"


def _records(name, tag, commit, sha):
    cfg = SCRATCH / ("l3_%s_cfg.json" % name)
    ven = SCRATCH / ("l3_%s_ven.json" % name)
    cfg.write_text(json.dumps({"mode": "shadow", "pin": {"tag": tag, "commit": commit, "wheel_sha256": sha}}), encoding="utf-8")
    ven.write_text(json.dumps({"tag": tag, "commit": commit, "wheel_sha256": sha}), encoding="utf-8")
    return cfg, ven


def _run(wheel, sha, cfg, ven, post=""):
    code = ("from vision import pin\npin._activate_for_test(%r, %r, %r, %r)\nimport vadapt.ramws as r\nprint('OK')\n%s\n"
            % (str(wheel), sha, str(cfg), str(ven), post))
    return run_py(code)


def _git_ve(*a):
    return subprocess.run(["git", "-C", VE] + list(a), capture_output=True, text=True, timeout=60)


class Law3PinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wheel = make_wheel(SCRATCH / "law3" / "vision_engine-0.0.0-py3-none-any.whl", FAKE_FILES)
        cls.sha = sha256_file(cls.wheel)

    def test_pin_refuses_law3_wheel_after_law4_repin(self):
        cfg, ven = _records("ok", LAW3_TAG, LAW3_COMMIT, LAW3_SHA)
        r = _run(self.wheel, self.sha, cfg, ven)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("E_PIN", r.stdout)

    def test_pin_refuses_law2_wheel_sha(self):
        cfg, ven = _records("law2", "ve-sealed-1-law2", LAW2_COMMIT, LAW2_SHA)
        r = _run(self.wheel, self.sha, cfg, ven)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("E_PIN", r.stdout)
        from vision import pin
        self.assertNotEqual(pin.EXPECTED_WHEEL_SHA256, LAW2_SHA)

    def test_pin_refuses_law1_wheel_sha(self):
        cfg, ven = _records("law1", "ve-sealed-1-law1", LAW1_COMMIT, LAW1_SHA)
        r = _run(self.wheel, self.sha, cfg, ven)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("E_PIN", r.stdout)

    def test_pin_refuses_tag_object_as_commit(self):
        cfg, ven = _records("tagobj", LAW3_TAG, LAW3_TAG_OBJECT, LAW3_SHA)
        r = _run(self.wheel, self.sha, cfg, ven)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("E_PIN", r.stdout)

    def test_repin_refuses_absent_tag_rc5(self):
        import contextlib
        import io
        from vision import build_wheel
        empty = SCRATCH / "l3_notag_repo"
        empty.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(empty), "init", "-q"], check=True, capture_output=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = build_wheel.main(["--check", "--ve", str(empty)])
        self.assertEqual(rc, 5)
        self.assertIn("E_PIN", buf.getvalue())

    def test_config_vendored_pin_triple_agree_law4(self):
        from vision import build_wheel, pin
        cfg = json.loads((REPO / "config" / "vision.json").read_text(encoding="utf-8"))["pin"]
        ven = json.loads((REPO / "vision" / "VENDORED.json").read_text(encoding="utf-8"))
        want = {"tag": LAW4_TAG, "commit": LAW4_COMMIT, "wheel_sha256": LAW4_SHA}
        self.assertEqual(cfg, want)
        self.assertEqual({k: ven[k] for k in want}, want)
        self.assertEqual((pin.TAG, pin.EXPECTED_COMMIT, pin.EXPECTED_WHEEL_SHA256), tuple(want.values()))
        self.assertEqual((build_wheel.TAG, build_wheel.EXPECTED_COMMIT, build_wheel.EXPECTED_WHEEL_SHA256), tuple(want.values()))
        self.assertEqual(build_wheel.PEER_WHEEL_SHA256, LAW4_SHA)

    def test_build_wheel_uses_tag_not_worktree_law3(self):
        """Reads the tag object, never the VE worktree (VE sits on branch rt): archive of the tag carries ramws share kw."""
        from vision import build_wheel
        self.assertEqual(build_wheel.TAG, LAW4_TAG)
        src = (REPO / "vision" / "build_wheel.py").read_text(encoding="utf-8")
        self.assertIn('"archive", TAG', src)
        self.assertNotIn("VE + \"/vadapt", src)

    def test_law3_ramws_has_share_api(self):
        if not os.path.isdir(VE):
            self.skipTest("vision-engine checkout absent")
        r = _git_ve("show", LAW3_TAG + ":vadapt/ramws.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        for tok in ("share_with_uid", "share_group", "E_RAM_SHARE"):
            self.assertIn(tok, r.stdout)

    def test_sealed_core_diff_empty_law2_law3(self):
        if not os.path.isdir(VE):
            self.skipTest("vision-engine checkout absent")
        r = _git_ve("diff", "--stat", "ve-sealed-1-law2", LAW3_TAG, "--", "visioncore", "vmem")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        names = _git_ve("diff", "--name-only", "ve-sealed-1-law2", LAW3_TAG).stdout.split()
        for n in names:
            self.assertTrue(n.startswith(("vadapt/", "tools/", "tests/")) or n == "CHANGELOG.md", n)

    def test_shipped_wheel_signature_has_share_kw(self):
        from vision import pin
        if not pin.DEFAULT_WHEEL.is_file():
            self.skipTest("wheel not built")
        r = run_py("from vision import pin\npin.activate()\nimport inspect, vadapt.ramws as r\n"
                   "print(sorted(inspect.signature(r.open_run).parameters))\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("share_with_uid", r.stdout)
        self.assertIn("share_group", r.stdout)


if __name__ == "__main__":
    unittest.main()
