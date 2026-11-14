import contextlib
import io
import os
import subprocess
import sys
import unittest
import unittest.mock

from vision.tests.helpers import FAKE_FILES, REPO, SCRATCH, make_wheel

TINY_PYPROJECT = (
    '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
    '[project]\nname = "tinyvis"\nversion = "0.0.1"\n[tool.setuptools]\npackages = ["tinyvis"]\n'
)


class BuildWheelTests(unittest.TestCase):
    def test_build_wheel_reproducible_sha(self):
        from vision import build_wheel
        tree = SCRATCH / "tiny_tree"
        (tree / "tinyvis").mkdir(parents=True, exist_ok=True)
        (tree / "pyproject.toml").write_text(TINY_PYPROJECT, encoding="utf-8")
        (tree / "tinyvis" / "__init__.py").write_text("X = 1\n", encoding="utf-8")
        a = build_wheel.build_from_tree(tree, SCRATCH / "tiny_a", 1792266319)
        b = build_wheel.build_from_tree(tree, SCRATCH / "tiny_b", 1792266319)
        self.assertEqual(build_wheel.sha256_file(a), build_wheel.sha256_file(b))

    def test_build_wheel_member_equivalence(self):
        from vision import build_wheel
        a = make_wheel(SCRATCH / "eq_a.whl", FAKE_FILES)
        files = dict(FAKE_FILES)
        files["vision_engine-0.0.0.dist-info/RECORD"] = "different record\n"
        b = make_wheel(SCRATCH / "eq_b.whl", files)
        ok, diffs = build_wheel.members_equivalent(a, b)
        self.assertTrue(ok, diffs)
        files["vadapt/ramws.py"] = "MARK = 'tampered'\n"
        c = make_wheel(SCRATCH / "eq_c.whl", files)
        ok, diffs = build_wheel.members_equivalent(a, c)
        self.assertFalse(ok)
        self.assertEqual(diffs, ["vadapt/ramws.py"])

    def test_build_wheel_peer_moved_not_fatal(self):
        from vision import build_wheel
        a = make_wheel(SCRATCH / "pm_a.whl", FAKE_FILES)
        files = dict(FAKE_FILES)
        files["vadapt/ramws.py"] = "MARK = 'rebuilt peer'\n"
        peer = make_wheel(SCRATCH / "pm_peer.whl", files)  # foreign sha (!= pinned) and different members
        w = build_wheel.witness_for(a, peer)
        self.assertTrue(w.startswith("peer_moved:"), w)
        self.assertEqual(w, "peer_moved:" + build_wheel.sha256_file(peer)[:8])

    def test_build_wheel_member_mismatch_pinned_peer_rc5(self):
        from vision import build_wheel
        a = make_wheel(SCRATCH / "mm_a.whl", FAKE_FILES)
        files = dict(FAKE_FILES)
        files["vadapt/ramws.py"] = "MARK = 'tampered'\n"
        peer = make_wheel(SCRATCH / "mm_peer.whl", files)
        with self.assertRaises(build_wheel.BuildError):
            build_wheel.witness_for(a, peer, peer_sha=build_wheel.sha256_file(peer))
        same = make_wheel(SCRATCH / "mm_same.whl", FAKE_FILES)
        self.assertTrue(build_wheel.witness_for(a, same, peer_sha=build_wheel.sha256_file(same)).startswith("equal:"))
        self.assertEqual(build_wheel.witness_for(a, SCRATCH / "nope.whl"), "absent")

    def test_build_wheel_cli_peer_moved_rc0_and_check_readonly(self):
        peer = make_wheel(SCRATCH / "cli_peer.whl", FAKE_FILES)  # foreign sha, members differ from the real wheel
        ven = REPO / "vision" / "VENDORED.json"
        before = (ven.read_bytes(), ven.stat().st_mtime_ns)
        # HOME matters: the pinned sha was built with the user-site setuptools; a bare env fails closed (rc 5), see REPORT_FL4
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "HOME": os.environ.get("HOME", "")}
        for argv in (["--check", "--peer", str(peer)], ["--check", "--peer", "/nonexistent/peer.whl"]):
            r = subprocess.run(["taskset", "-c", "0,1", sys.executable, "-B", str(REPO / "vision" / "build_wheel.py")] + argv,
                               cwd=str(REPO), env=env, capture_output=True, text=True, timeout=250)
            self.assertEqual(r.returncode, 0, (argv, r.stdout, r.stderr))
        self.assertEqual((ven.read_bytes(), ven.stat().st_mtime_ns), before, "--check rewrote VENDORED.json")

    def test_check_missing_shipped_wheel_rc5(self):
        """ADC-W2-06(3): --check must not return rc 0 when build/wheels/<wheel> is absent."""
        from vision import build_wheel
        import shutil
        import tempfile
        out = SCRATCH / "check_missing_out"
        shutil.rmtree(out, ignore_errors=True)
        buf = io.StringIO()
        with unittest.mock.patch.object(build_wheel, "OUT", out), contextlib.redirect_stdout(buf):
            rc = build_wheel.main(["--check", "--peer", "/nonexistent/peer.whl"])
        self.assertEqual(rc, 5, buf.getvalue())
        self.assertIn("E_PIN", buf.getvalue())

    def test_check_explicit_peer_records_equal(self):
        """JW2-05: --check --peer <explicit law4 wheel> reports witness equal:<sha8>."""
        from vision import build_wheel
        shipped = REPO / "build" / "wheels" / build_wheel.WHEEL_NAME
        if not shipped.is_file():
            self.skipTest("wheel not built")
        import json
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "HOME": os.environ.get("HOME", "")}
        r = subprocess.run(["taskset", "-c", "0,1", sys.executable, "-B", str(REPO / "vision" / "build_wheel.py"),
                            "--check", "--peer", str(shipped)],
                           cwd=str(REPO), env=env, capture_output=True, text=True, timeout=250)
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        self.assertEqual(json.loads(r.stdout)["witness"], "equal:" + build_wheel.EXPECTED_WHEEL_SHA256[:8])

    def test_build_wheel_uses_tag_not_worktree(self):
        from unittest import mock
        from vision import build_wheel
        repo = SCRATCH / "tagrepo"
        import shutil
        shutil.rmtree(repo, ignore_errors=True)
        repo.mkdir(parents=True)
        g = lambda *a: subprocess.run(["git", "-C", str(repo)] + list(a), check=True, capture_output=True, text=True,
                                      env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))
        g("init", "-q")
        (repo / "f.txt").write_text("TAGGED\n", encoding="utf-8")
        g("add", "f.txt")
        g("commit", "-q", "-m", "c")
        g("tag", "-a", "-m", "t", "faketag")
        commit = g("rev-parse", "faketag^{commit}").stdout.strip()
        (repo / "f.txt").write_text("WORKTREE\n", encoding="utf-8")
        with mock.patch.object(build_wheel, "TAG", "faketag"), mock.patch.object(build_wheel, "EXPECTED_COMMIT", commit):
            build_wheel.extract_tag(str(repo), SCRATCH / "tagout")
        self.assertEqual((SCRATCH / "tagout" / "f.txt").read_text(encoding="utf-8"), "TAGGED\n")
        with mock.patch.object(build_wheel, "TAG", "faketag"), mock.patch.object(build_wheel, "EXPECTED_COMMIT", "0" * 40):
            with self.assertRaises(build_wheel.BuildError):
                build_wheel.extract_tag(str(repo), SCRATCH / "tagout2")

    def test_build_wheel_mismatch_rc5_no_repin(self):
        from unittest import mock
        from vision import build_wheel
        cfg = REPO / "config" / "vision.json"
        ven = REPO / "vision" / "VENDORED.json"
        before = (cfg.read_bytes(), ven.read_bytes())
        bad = make_wheel(SCRATCH / "bad_built.whl", FAKE_FILES)
        with mock.patch.object(build_wheel, "extract_tag", return_value=1), \
                mock.patch.object(build_wheel, "build_from_tree", return_value=bad):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = build_wheel.main(["--check"])
        self.assertEqual(rc, 5)
        self.assertEqual((cfg.read_bytes(), ven.read_bytes()), before)
        self.assertEqual(build_wheel.EXPECTED_WHEEL_SHA256, "5e2395d56709274f21123bb5ef714adf52f0cf58f78d4f4938da736c52a7a4a5")

    def test_build_wheel_env_matches_tag_tool(self):
        """No chmod after extract (law2 tool uses plain extractall); pip runs offline with cwd = source tree."""
        from unittest import mock
        from vision import build_wheel
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(kw)
            seen["cmd"] = cmd
            d = SCRATCH / "env_out"
            d.mkdir(parents=True, exist_ok=True)
            (d / "x-0-py3-none-any.whl").write_bytes(b"x")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        tree = SCRATCH / "env_tree"
        tree.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(build_wheel.subprocess, "run", fake_run):
            build_wheel.build_from_tree(tree, SCRATCH / "env_out", 5)
        self.assertEqual(seen["env"].get("PIP_NO_INDEX"), "1")
        self.assertEqual(str(seen.get("cwd")), str(tree))
        self.assertNotIn("os.chmod", open(REPO / "vision" / "build_wheel.py", encoding="utf-8").read())

    def test_build_wheel_no_rc1(self):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        for argv in (["--bogus"], ["--check", "--ve", "/nonexistent/ve"], ["--ve", "/nonexistent/ve"]):
            r = subprocess.run([sys.executable, "-B", str(REPO / "vision" / "build_wheel.py")] + argv, cwd=str(REPO), env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertIn(r.returncode, (2, 3, 4, 5, 6, 7), (argv, r.returncode, r.stderr))
            self.assertNotEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
