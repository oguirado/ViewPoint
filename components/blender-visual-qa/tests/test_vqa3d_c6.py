"""Cache source identity, admission, subprocess isolation and evidence controls."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vqa3d import kernel_cache as kc, profile
from vqa3d.adapters import blender as driver
from vqa3d.receipt import make_receipt
from vqa3d.canon import sha256_canonical

NAME = "cycles_kernel_sm_121_0123456789ABCDEF.cubin"


class CacheFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "sources"
        self.source.mkdir()
        self.header = self.source / "kernel.h"
        self.header.write_bytes(b"kernel_a")
        self.scene = self.root / "scenes" / "scene.blend"
        self.scene.parent.mkdir()
        self.scene.write_bytes(b"fixture")

    def bind(self, version="4.0.2", name="scratch"):
        cache = kc.KernelCache(self.root / "persistent" / "cycles", self.scene)
        cache.bind(version, self.source, self.root / name)
        return cache


class CacheTests(CacheFixture):
    def test_private_creation_and_source_folder_rejection(self):
        cache = self.bind()
        for directory in (cache.root, cache.root.parent, cache.directory, cache.kernels):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for directory in (self.scene.parent, self.scene.parent / "cache"):
            with self.assertRaisesRegex(ValueError, "inside_source"):
                kc.KernelCache(directory, self.scene)
        alias = self.root / "scene_alias"
        alias.symlink_to(self.scene.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "inside_source"):
            kc.KernelCache(alias / "cache", self.scene)

    def test_reject_permissions_ownership_symlinks_and_files(self):
        path = self.root / "bad"
        path.mkdir()
        for permissions in (0o777, 0o770):
            path.chmod(permissions)
            with self.assertRaisesRegex(ValueError, "unsafe_kernel_cache_directory"):
                kc.KernelCache(path, self.scene)
        path.chmod(0o700)
        with patch.object(kc.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, "unsafe_kernel_cache_directory"):
                kc.KernelCache(path, self.scene)
        alias = self.root / "alias"
        alias.symlink_to(path, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe_kernel_cache_directory"):
            kc.KernelCache(alias, self.scene)
        regular = self.root / "regular"
        regular.write_bytes(b"fixture")
        with self.assertRaisesRegex(ValueError, "unsafe_kernel_cache_directory"):
            kc.KernelCache(regular, self.scene)

    def assert_source_separation(self):
        first = self.bind(name="first")
        (first.kernels / NAME).write_bytes(b"old_cubin")
        (first.kernels / NAME).chmod(0o600)
        info = self.header.stat()
        self.header.write_bytes(b"kernel_b")
        os.utime(self.header, ns=(info.st_atime_ns, info.st_mtime_ns))
        second = self.bind(name="second")
        self.assertFalse((second.link / NAME).exists(), "stale cubin reused after same-size source edit")
        self.assertNotEqual(first.source_sha256, second.source_sha256)
        self.assertNotEqual(first.namespace, second.namespace)

    def test_source_content_change_cannot_reuse_cubin(self):
        self.assert_source_separation()

    def test_key_ignoring_mutant_is_caught_by_same_assertion(self):
        with patch.object(kc, "namespace_key", return_value="mutant_ignores_source"):
            with self.assertRaisesRegex(AssertionError, "stale cubin reused"):
                self.assert_source_separation()

    def test_version_namespaces_and_source_changes_during_session(self):
        first = self.bind(name="first")
        second = self.bind(version="4.0.3", name="second")
        self.assertNotEqual(first.namespace, second.namespace)
        self.header.rename(self.source / "other.h")
        with self.assertRaisesRegex(ValueError, "kernel_source_changed_during_session"):
            first.snapshot()

    def test_reject_empty_source_and_ambiguous_entries(self):
        self.header.unlink()
        with self.assertRaisesRegex(ValueError, "kernel_source_empty"):
            self.bind()
        self.header.symlink_to(self.scene)
        with self.assertRaisesRegex(ValueError, "kernel_source_symlink"):
            self.bind(name="second")

    def test_cache_entries_admitted_before_renderer_reads_them(self):
        cache = self.bind()
        cubin = cache.kernels / NAME
        cubin.write_bytes(b"kernel")
        cubin.chmod(0o666)
        with self.assertRaisesRegex(ValueError, "unsafe_kernel_cache_cubin"):
            cache.snapshot()
        cubin.unlink()
        cubin.symlink_to(self.header)
        with self.assertRaisesRegex(ValueError, "unsafe_kernel_cache_cubin"):
            cache.snapshot()
        cubin.unlink()
        (cache.kernels / "unexpected").mkdir()
        with self.assertRaisesRegex(ValueError, "unexpected_kernel_cache_entry"):
            cache.snapshot()

    def test_hit_miss_require_log_and_file_evidence(self):
        cache = self.bind()
        before = cache.snapshot()
        cubin = cache.kernels / NAME
        cubin.write_bytes(b"new_cubin")
        cubin.chmod(0o600)
        miss = cache.finish("gpu_probe", before,
            ["Testing for locally compiled kernel " + str(cache.link / NAME) + ".", "Compiling CUDA kernel ..."], True)
        self.assertEqual(miss["outcome"], "miss")
        self.assertEqual(miss["cubins"][0]["arch"], "sm_121")
        self.assertEqual(miss["cubins"][0]["sha256"], hashlib.sha256(b"new_cubin").hexdigest())
        before = cache.snapshot()
        unproven = cache.finish("gpu_probe", before, [], True)
        self.assertEqual(unproven["outcome"], "unverified")
        hit = cache.finish("gpu_probe", before,
            ["Testing for locally compiled kernel " + str(cache.link / NAME) + ".", "Using locally compiled kernel."], True)
        self.assertEqual(hit["outcome"], "hit")
        self.assertEqual(hit["cubins"][0]["before"], before[NAME])
        self.assertFalse(cache.finish("gpu_probe", before, [], False)["render_completed"])

    def test_default_and_environment_conflict(self):
        adapter = driver.BlenderAdapter(self.scene)
        self.assertIsNone(adapter._kernel_cache)
        self.assertIsNone(adapter.kernel_cache_receipt)
        with self.assertRaisesRegex(ValueError, "conflicts_with_XDG_CACHE_HOME"):
            driver.BlenderAdapter(self.scene, kernel_cache=self.root / "cache",
                blender_env={"XDG_CACHE_HOME": str(self.root / "other")})


FAKE_BLENDER = r"""#!/usr/bin/python3
import os,json,sys,hashlib
from pathlib import Path
req,res=sys.argv[sys.argv.index('--')+1:]
x=json.load(open(req))
while True:
    result={'ok':True,'runtime_version':'fake-4.0.2','state':{}}
    result['environment']={k:{'sha256':hashlib.sha256(os.environ[k].encode()).hexdigest()} for k in x['environment_keys'] if k in os.environ}
    result['kernel_source_path']=os.environ['CYCLES_KERNEL_PATH']
    mode=x['mode']
    if mode in ('gpu_probe','capture'):
        home=Path(os.environ['HOME']);xdg=Path(os.environ['XDG_CACHE_HOME'])
        assert home==xdg and not (home/'sentinel').exists()
        kernels=xdg/'cycles'/'kernels'
        if x.get('kernel_cache_declared'):
            assert kernels.is_dir() and not kernels.is_symlink()
            assert '--debug-cycles' in sys.argv
        else:
            assert not kernels.is_symlink()
            assert '--debug-cycles' not in sys.argv
            kernels.mkdir(parents=True,exist_ok=True)
        cubin=kernels/'cycles_kernel_sm_121_0123456789ABCDEF.cubin'
        print('Testing for locally compiled kernel '+str(cubin)+'.',file=sys.stderr,flush=True)
        if cubin.exists():
            print('Using locally compiled kernel.',file=sys.stderr,flush=True)
        else:
            print('Compiling CUDA kernel ...',flush=True)
            cubin.write_bytes(b'fixture-cubin')
    with open(res+'.tmp','w') as out:json.dump(result,out)
    os.replace(res+'.tmp',res)
    if not x.get('session'):break
    line=sys.stdin.readline()
    if not line:break
    x=json.loads(line)
"""


class ChildIntegration(CacheFixture):
    def test_real_subprocess_both_child_modes_default_and_declared(self):
        exe = self.root / "fake-blender"
        exe.write_text(FAKE_BLENDER)
        exe.chmod(0o700)
        for session in (False, True):
            for declared in (False, True):
                cache = self.root / ("cache-" + str(session)) if declared else None
                for run in (1, 2):
                    with self.subTest(session=session, declared=declared, run=run):
                        adapter = driver.BlenderAdapter(self.scene, profile_session=session,
                            blender_env={"CYCLES_KERNEL_PATH": str(self.source)}, kernel_cache=cache)
                        try:
                            with patch.object(driver, "BLENDER", exe):
                                adapter.scene_state()
                                # Cache custody is measured using the synthetic capture protocol.
                                adapter.capture("capture")
                                adapter.capture("capture-repeat")
                            if declared:
                                uses = adapter.kernel_cache_receipt["uses"]
                                self.assertEqual(uses[0]["outcome"], "miss" if run == 1 else "hit")
                                self.assertEqual(uses[1]["outcome"], "hit")
                                receipt = make_receipt(adapter, {"kind": "capability"}, None, None,
                                    [{"verdict": "PASS", "reason": "gpu_render_completed"}])
                                self.assertEqual(receipt["kernel_cache"], adapter.kernel_cache_receipt)
                                self.assertEqual(receipt["receipt_sha256"], sha256_canonical(
                                    {k:v for k,v in receipt.items() if k != "receipt_sha256"}))
                            else:
                                self.assertIsNone(adapter.kernel_cache_receipt)
                        finally:
                            adapter.close()

    def test_cli_cache_precedence_and_profile_controls(self):
        from unittest.mock import MagicMock
        from vqa3d import __main__ as cli
        policy = copy.deepcopy(profile.load("film-animation"))
        policy["render"]["kernel_cache"] = str(self.root / "profile-cache")
        fake = MagicMock()
        fake.probe.return_value = {"available": True, "status": "implemented"}
        with patch.object(cli, "emit"), patch.object(cli, "create_adapter", return_value=fake) as create:
            self.assertEqual(cli.main(["probe", "--adapter", "blender", "--kernel-cache", str(self.root / "cli-cache")]), 0)
            self.assertEqual(create.call_args.kwargs["kernel_cache"], self.root / "cli-cache")
            with patch.object(profile, "load", return_value=policy), patch.object(profile, "run", return_value=([], {"verdict_rc": 0}, {})):
                self.assertEqual(cli.main(["profile", "check", "--profile", "film-animation", "--kernel-cache", str(self.root / "cli-cache")]), 0)
                self.assertEqual(create.call_args.kwargs["kernel_cache"], self.root / "cli-cache")
                self.assertEqual(cli.main(["profile", "check", "--profile", "film-animation"]), 0)
                self.assertEqual(create.call_args.kwargs["kernel_cache"], str(self.root / "profile-cache"))

    def test_profile_path_validation_and_adapter_support(self):
        policy = copy.deepcopy(profile.load("film-animation"))
        policy["render"]["kernel_cache"] = str(self.root / "cache")
        profile.validate(policy)
        self.assertEqual(profile.adapter_kwargs(policy, driver.BlenderAdapter)["kernel_cache"], str(self.root / "cache"))
        with self.assertRaisesRegex(profile.CannotSee, "kernel_cache_unsupported"):
            profile.adapter_kwargs(policy, lambda width=96, height=72, capture_engine="cycles", capture_samples=1: None)
        policy["render"]["kernel_cache"] = 1
        with self.assertRaises(ValueError):
            profile.validate(policy)


if __name__ == "__main__":
    unittest.main()
