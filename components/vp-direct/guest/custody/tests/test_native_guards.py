"""Exercise the installed Core and daemon guards offline, inside safe_run.sh."""
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]


class NativeGuardTests(unittest.TestCase):
    def run_guard(self, argv, cwd, name):
        with TemporaryDirectory(dir=HERE) as scratch:
            env = dict(os.environ, CARGO_TARGET_DIR=scratch, CARGO_NET_OFFLINE="true",
                       CARGO_BUILD_JOBS="3", GOPROXY="off", GOSUMDB="off", GOTOOLCHAIN="local",
                       GOCACHE=str(Path(scratch) / "go-cache"), GOTMPDIR=scratch)
            result = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            self.assertEqual(result.returncode, 0, result.stdout[-4000:])
            self.assertIn(name, result.stdout, result.stdout[-4000:])

    def test_capacity_blocks_both_providers_and_graphical(self):
        name = "serve::guest_command::tests::capacity_blocks_both_providers_and_graphical"
        self.run_guard(["cargo", "test", "--offline", "--locked", "-p", "metaviewer-core", "--lib",
                        name, "--", "--exact"], REPO, f"test {name} ... ok")

    def test_vm_display_refuses_host_transport(self):
        name = "TestVMDisplayRefusesHostTransport"
        self.run_guard(["go", "test", "-p", "2", "-count=1", "-v", "-run", f"^{name}$",
                        "./internal/frames"], REPO / "daemon-adapter", f"--- PASS: {name} (")
