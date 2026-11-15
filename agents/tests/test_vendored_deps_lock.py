"""Negative and positive controls for vendored mode in deps_lock."""

import base64
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metaviewer_agents.perception import deps_lock
from metaviewer_agents.perception.deps_lock import (
    DependencyLockError,
    _vendored_tree_sha256,
    resolve_locked_component,
)


class VendoredDependencyLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mv-vendored-", dir="/dev/shm")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.commit = "a" * 40
        self.vendored = self.root / "components" / "vision-engine"
        self.vendored.mkdir(parents=True)
        (self.vendored / "pkg").mkdir()
        (self.vendored / "pkg" / "__init__.py").write_text("VAL = 1\n", encoding="utf-8")
        (self.vendored / "pkg" / "mod.py").write_text("X = 2\n", encoding="utf-8")
        (self.vendored / "pkg" / "mod.py").chmod(0o644)
        self.wender = self.root / "components" / "wender-agent"
        self.wender.mkdir(parents=True)
        (self.wender / "wender.py").write_text("W = 1\n", encoding="utf-8")
        self.lock = self.root / "deps.lock"
        self.write_lock()

    def write_lock(self, *, ve_sha=None, ve_path="components/vision-engine"):
        if ve_sha is None:
            ve_sha = _vendored_tree_sha256(self.vendored)
        payload = {
            "components": [
                {
                    "name": "vision-engine",
                    "commit": self.commit,
                    **({"vendored_path": ve_path} if ve_path is not None else {}),
                    **({"vendored_tree_sha256": ve_sha} if ve_sha is not None else {}),
                },
                {
                    "name": "wender-agent",
                    "commit": self.commit,
                    "vendored_path": "components/wender-agent",
                    "vendored_tree_sha256": _vendored_tree_sha256(self.wender),
                },
            ]
        }
        self.lock.write_text(json.dumps(payload), encoding="utf-8")

    def resolve_ve(self, env_overrides=None):
        env = dict(os.environ)
        env.pop("VQA_VE_ROOT", None)
        env.pop("MV_WENDER_ROOT", None)
        if env_overrides:
            env.update(env_overrides)
        with mock.patch.dict(os.environ, env, clear=True):
            return resolve_locked_component("vision-engine", lock_path=self.lock)

    def assert_ve_error(self, code, env_overrides=None, msg_contains=None):
        with self.assertRaises(DependencyLockError) as caught:
            self.resolve_ve(env_overrides)
        self.assertEqual(caught.exception.code, code)
        if msg_contains is not None:
            self.assertIn(msg_contains, caught.exception.message)
        return caught.exception

    def test_bytecode_and_pycache_rejected_and_all_other_files_hashed(self):
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())

        pycache = self.vendored / "pkg" / "__pycache__"
        pycache.mkdir()
        self.assert_ve_error("dirty_tree", msg_contains="__pycache__")
        (pycache / "mod.cpython-312.pyc").write_bytes(b"evil")
        self.assert_ve_error("dirty_tree", msg_contains="__pycache__")
        shutil.rmtree(pycache)
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())

        for suffix in (".pyc", ".pyo", ".PYC"):
            target = self.vendored / "pkg" / f"evil{suffix}"
            target.write_bytes(b"bytecode")
            try:
                self.assert_ve_error("dirty_tree", msg_contains="bytecode")
            finally:
                target.unlink()

        extra = self.vendored / "pkg" / "extra.txt"
        extra.write_text("unexpected", encoding="utf-8")
        self.assert_ve_error("dirty_tree")
        extra.unlink()
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())

    def test_dist_info_carveout_removed_and_vendored_always_content_hashed(self):
        # Empty dist-info / site-packages outside vendored_root must fail closed (unavailable).
        fake_site = self.root / "fake_env" / "lib" / "python3.12" / "site-packages"
        fake_info = fake_site / "vision_engine-0.2.0.dist-info"
        fake_info.mkdir(parents=True)
        (fake_site / "evil.py").write_text("EVIL = True\n", encoding="utf-8")
        self.assert_ve_error("unavailable", {"VQA_VE_ROOT": str(fake_site)})
        self.assert_ve_error("unavailable", {"VQA_VE_ROOT": str(fake_info)})

        # A vendored_root named *.dist-info or containing a valid wheel dist-info is STILL content-hashed.
        dist_vendored = self.root / "pkg.dist-info"
        dist_vendored.mkdir()
        (dist_vendored / "mod.py").write_text("OK = 1\n", encoding="utf-8")
        dist_sha = _vendored_tree_sha256(dist_vendored)
        self.write_lock(ve_sha=dist_sha, ve_path="pkg.dist-info")
        self.assertEqual(self.resolve_ve(), dist_vendored.resolve())
        (dist_vendored / "extra.py").write_text("BAD = 1\n", encoding="utf-8")
        self.assert_ve_error("dirty_tree")
        self.write_lock()

        ve_info = self.vendored / "vision_engine-0.2.0.dist-info"
        ve_info.mkdir()
        (ve_info / "direct_url.json").write_text(
            json.dumps({"vcs_info": {"vcs": "git", "commit_id": self.commit}}),
            encoding="utf-8",
        )
        mod_bytes = (self.vendored / "pkg" / "mod.py").read_bytes()
        mod_digest = base64.urlsafe_b64encode(hashlib.sha256(mod_bytes).digest()).rstrip(b"=").decode()
        with (ve_info / "RECORD").open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerows([
                ["pkg/mod.py", f"sha256={mod_digest}", str(len(mod_bytes))],
                ["vision_engine-0.2.0.dist-info/RECORD", "", ""],
            ])
        self.assert_ve_error("dirty_tree")
        shutil.rmtree(ve_info)

        # A real wheel root with valid RECORD and direct_url.json passes even with vendored_sha set.
        real_site = self.root / "real_env" / "lib" / "python3.12" / "site-packages"
        real_info = real_site / "vision_engine-0.2.0.dist-info"
        real_info.mkdir(parents=True)
        (real_site / "engine.py").write_bytes(b"OK = 1\n")
        (real_info / "direct_url.json").write_text(
            json.dumps({"vcs_info": {"vcs": "git", "commit_id": self.commit}}),
            encoding="utf-8",
        )
        rows = []
        for path in (real_site / "engine.py", real_info / "direct_url.json"):
            data = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            rows.append([path.relative_to(real_site).as_posix(), f"sha256={digest}", str(len(data))])
        rows.append([(real_info / "RECORD").relative_to(real_site).as_posix(), "", ""])
        with (real_info / "RECORD").open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerows(rows)
        self.assertEqual(
            self.resolve_ve({"VQA_VE_ROOT": str(real_site.parents[2])}),
            real_site.parents[2].resolve(),
        )
        (real_site / "evil.py").write_text("EVIL = 1\n", encoding="utf-8")
        self.assert_ve_error("dirty_tree", {"VQA_VE_ROOT": str(real_site.parents[2])})

    def test_symlinks_reject_absolute_and_outside_allow_relative_in_root(self):
        outside = self.root / "outside.py"
        outside.write_text("SECRET = 1\n", encoding="utf-8")
        outside_dir = self.root / "outside_dir"
        outside_dir.mkdir()

        # Relative in-root file symlink is allowed and hashed.
        link = self.vendored / "pkg" / "alias.py"
        link.symlink_to("mod.py")
        self.write_lock()
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())
        link.unlink()
        link.symlink_to("__init__.py")
        self.assert_ve_error("dirty_tree")
        link.unlink()
        self.write_lock()

        bad_targets = [
            ("abs_outside", str(outside)),
            ("abs_inside", str(self.vendored / "pkg" / "mod.py")),
            ("rel_escape", "../../outside.py"),
            ("dir_escape", "../../outside_dir"),
        ]
        for name, target in bad_targets:
            with self.subTest(name=name):
                bad = self.vendored / "pkg" / name
                bad.symlink_to(target)
                try:
                    with self.assertRaises(ValueError):
                        _vendored_tree_sha256(self.vendored)
                    self.assert_ve_error("dirty_tree", msg_contains="symlink")
                finally:
                    bad.unlink()

        # Symlink loop is also rejected cleanly.
        a = self.vendored / "pkg" / "loop_a"
        b = self.vendored / "pkg" / "loop_b"
        a.symlink_to("loop_b")
        b.symlink_to("loop_a")
        try:
            self.assert_ve_error("dirty_tree", msg_contains="symlink")
        finally:
            a.unlink()
            b.unlink()

    def test_git_stable_modes_empty_dirs_and_empty_env_refused(self):
        empty_dir = self.vendored / "pkg" / "empty_ns"
        empty_dir.mkdir()
        empty_dir.chmod(0o755)
        self.write_lock()
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())

        # Empty-string / whitespace env vars must fail closed, never fall back to vendored.
        for empty in ("", "   "):
            self.assert_ve_error("unavailable", {"VQA_VE_ROOT": empty})

        # Non-executable group/other mode bits (umask 0002 vs 0022) must preserve the hash.
        mod = self.vendored / "pkg" / "mod.py"
        mod.chmod(0o664)
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())

        # Changing executable bit (+x) must invalidate the vendored hash.
        mod.chmod(0o755)
        self.assert_ve_error("dirty_tree")
        self.write_lock()
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())
        mod.chmod(0o775)
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())
        mod.chmod(0o644)
        self.write_lock()
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())

        # Adding, chmod-ing, or removing empty directories must not affect the hash.
        empty_dir.chmod(0o700)
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())
        empty_dir.rmdir()
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())
        (self.vendored / "pkg" / "new_empty").mkdir()
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())

        # A root with only empty directories is still rejected as an empty vendored tree.
        only_empty = self.root / "only_empty"
        (only_empty / "nested").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "empty vendored tree"):
            _vendored_tree_sha256(only_empty)

    def test_dispatch_order_git_before_vendored_and_monorepo_before_generic_vendored(self):
        git = shutil.which("git")
        self.assertIsNotNone(git)
        git_env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.root),
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        def run_git(repo, *args):
            return subprocess.run(
                [git, "-C", str(repo), *args],
                env=git_env, check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()

        # 1. When vendored_root has .git, git check runs first (never vendored).
        run_git(self.vendored, "init", "-q")
        run_git(self.vendored, "add", "-A")
        run_git(self.vendored, "commit", "-qm", "fixture")
        git_commit = run_git(self.vendored, "rev-parse", "HEAD")

        # Put a WRONG vendored_tree_sha256 in lock; git checkout still passes!
        payload = json.loads(self.lock.read_text(encoding="utf-8"))
        payload["components"][0]["commit"] = git_commit
        payload["components"][0]["vendored_tree_sha256"] = "0" * 64
        self.lock.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.resolve_ve(), self.vendored.resolve())

        # Dirty git checkout (untracked .py) must fail even if vendored hash were updated!
        (self.vendored / "pkg" / "untracked.py").write_text("BAD = 1\n", encoding="utf-8")
        self.assert_ve_error("dirty_tree")
        (self.vendored / "pkg" / "untracked.py").unlink()
        # Wrong commit must fail commit_mismatch via git even if vendored_tree_sha256 matched.
        payload["components"][0]["commit"] = "b" * 40
        self.lock.write_text(json.dumps(payload), encoding="utf-8")
        self.assert_ve_error("commit_mismatch")

        # 2. Monorepo subtree runs before generic vendored_sha fallback when root != vendored_root.
        monorepo = self.root / "monorepo"
        monorepo.mkdir()
        run_git(monorepo, "init", "-q")
        (monorepo / "payload.py").write_text("VALUE = 1\n", encoding="utf-8")
        run_git(monorepo, "add", "payload.py")
        run_git(monorepo, "commit", "-qm", "pin")
        pin = run_git(monorepo, "rev-parse", "HEAD")
        subtree = monorepo / "components" / "vision-engine"
        subtree.mkdir(parents=True)
        (monorepo / "payload.py").rename(subtree / "payload.py")
        run_git(monorepo, "add", "-A")
        run_git(monorepo, "commit", "-qm", "import")

        mono_lock = monorepo / "deps.lock"
        mono_lock.write_text(json.dumps({
            "components": [
                {
                    "name": "vision-engine",
                    "commit": pin,
                    "vendored_path": "other/vision-engine",
                    "vendored_tree_sha256": "0" * 64,
                },
                {
                    "name": "wender-agent",
                    "commit": pin,
                },
            ]
        }), encoding="utf-8")
        with mock.patch.object(deps_lock, "_LOCK_PATH", mono_lock):
            with mock.patch.dict(os.environ, {"VQA_VE_ROOT": str(subtree)}):
                self.assertEqual(
                    resolve_locked_component("vision-engine", lock_path=mono_lock),
                    subtree.resolve(),
                )
                (subtree / "payload.py").write_text("VALUE = 2\n", encoding="utf-8")
                with self.assertRaises(DependencyLockError) as caught:
                    resolve_locked_component("vision-engine", lock_path=mono_lock)
                self.assertEqual(caught.exception.code, "dirty_tree")

    def test_real_repo_deps_lock_vendored_hashes_match(self):
        repo_root = Path(__file__).resolve().parents[2]
        lock_data = json.loads((repo_root / "deps.lock").read_text(encoding="utf-8"))
        env = dict(os.environ)
        env.pop("VQA_VE_ROOT", None)
        env.pop("MV_WENDER_ROOT", None)
        with mock.patch.dict(os.environ, env, clear=True):
            for name in ("vision-engine", "wender-agent"):
                resolved = resolve_locked_component(name)
                self.assertEqual(resolved, (repo_root / "components" / name).resolve())
        os_entry = next(c for c in lock_data["components"] if c["name"] == "opensuperficies")
        self.assertEqual(
            _vendored_tree_sha256(repo_root / os_entry["vendored_path"]),
            os_entry["vendored_tree_sha256"],
        )

    def test_fresh_git_clone_vendored_hash_matches(self):
        git = shutil.which("git")
        self.assertIsNotNone(git)
        git_env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.root),
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }

        # 1. Fixture repo with 644/755 files, nested dirs, relative file & dir symlinks,
        #    plus umask variations and an empty directory that git will not recreate.
        src = self.root / "src_repo"
        (src / "pkg").mkdir(parents=True)
        (src / "bin").mkdir()
        (src / "empty_untracked").mkdir()
        (src / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
        (src / "pkg" / "mod.py").chmod(0o664)
        (src / "bin" / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (src / "bin" / "run.sh").chmod(0o775)
        (src / "pkg" / "alias.py").symlink_to("mod.py")
        (src / "pkg_link").symlink_to("pkg")

        subprocess.run([git, "-C", str(src), "init", "-q"], env=git_env, check=True, timeout=5)
        subprocess.run([git, "-C", str(src), "add", "-A"], env=git_env, check=True, timeout=5)
        subprocess.run([git, "-C", str(src), "commit", "-qm", "fixture"], env=git_env, check=True, timeout=5)

        cloned = self.root / "fresh_clone"
        subprocess.run(
            [git, "clone", "-q", str(src), str(cloned)],
            env=git_env, check=True, timeout=10,
        )
        shutil.rmtree(src / ".git")
        shutil.rmtree(cloned / ".git")
        # Force 0644/0755 on cloned copy to simulate a different umask than src (0664/0775).
        (cloned / "pkg" / "mod.py").chmod(0o644)
        (cloned / "bin" / "run.sh").chmod(0o755)
        self.assertFalse((cloned / "empty_untracked").exists())
        self.assertEqual(_vendored_tree_sha256(cloned), _vendored_tree_sha256(src))

        # 2. Fresh clone of the real repository matches every vendored_tree_sha256 in deps.lock.
        repo_root = Path(__file__).resolve().parents[2]
        if (repo_root / ".git").exists():
            lock_data = json.loads((repo_root / "deps.lock").read_text(encoding="utf-8"))
            with tempfile.TemporaryDirectory(prefix="mv-fresh-clone-") as tmp_dir:
                repo_clone = Path(tmp_dir) / "repo"
                subprocess.run(
                    [git, "clone", "-q", str(repo_root), str(repo_clone)],
                    env=git_env, check=True, timeout=30,
                )
                for entry in lock_data["components"]:
                    vpath = entry.get("vendored_path")
                    expected = entry.get("vendored_tree_sha256")
                    if vpath and expected:
                        with self.subTest(component=entry["name"]):
                            self.assertEqual(_vendored_tree_sha256(repo_clone / vpath), expected)


if __name__ == "__main__":
    unittest.main()
