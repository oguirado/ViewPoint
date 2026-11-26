"""Offline Tier 1 refusal, closure and kernel boundary controls."""

from contextlib import ExitStack, redirect_stdout
import copy
import fcntl
from io import StringIO
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from fc_boot import Contract, FixtureBoot, UnsupportedProfile, canonical, decode, digest, hash_bytes
from fc_boot_owner import authorize_job, main
from fc_boot_tier1 import (CELL_IMAGE, LIMIT_FILES, NativeTier1, Tier1Policy, identity, read_caps,
                           read_pin, sandbox_args, sealed, verify_mounts)
from test_fc_boot import ToolsFixture


class Tier1(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="fc1-", dir="/dev/shm")))
        self.contract = Contract(os.environ["FC_BOOT_CONTRACT_ROOT"])
        self.tools = ToolsFixture(self.contract)
        self.profile = self.tools.profile
        self.profile.update(image=CELL_IMAGE.copy(), trust_class="operator_owned", untrusted_input=False,
                            web_input=False, speculative=False)
        self.approved = digest(self.profile)
        self.entry = {"tier": 1, "image": CELL_IMAGE.copy(), "artifacts": {}}
        self.policy = Tier1Policy({"profiles": {self.approved: self.entry},
                                   "guardian": {"sha256": self.tools.guardian_sha256},
                                   "cgroup_root": str(self.root / "groups"),
                                   "runtime_root": str(self.root / "runtime"), "evidence_root": str(self.root / "outside")})

    def boot(self, name, runtime=None):
        keys = {role: ("fixture-" + role, key.public_key()) for role, key in self.tools.keys.items()}
        observer = {"run_id": "tier1-test", "cell_id": "tier1-cell", "block_id": "offline", "arm": "A"}
        boot = FixtureBoot(self.contract, self.tools, runtime, self.root / name, observer, keys,
                           self.tools.sign_witness)
        boot.authorize_profile = self.policy.resolve
        return boot

    def test_only_signed_registered_digest_passes(self):
        self.assertIs(self.policy.resolve(self.approved, self.profile), self.entry)
        self.boot("nominal").verify(canonical(self.tools.create()))
        self.profile["profile_id"] = "unregistered"
        request = self.tools.create()
        boot = self.boot("refused")
        with self.assertRaisesRegex(UnsupportedProfile, "^PROFILE_UNSUPPORTED$"):
            boot.run(canonical(request))
        self.assertEqual(self.tools.claim_calls, 0)
        self.assertFalse((boot.root / "launch-intent.json").exists())

    def test_image_swap_is_refused_despite_matching_grant_and_registry(self):
        self.profile["image"]["image_sha256"] = "a" * 64
        self.policy.value["profiles"][digest(self.profile)] = copy.deepcopy(self.entry)
        self.policy.value["profiles"][digest(self.profile)]["image"] = self.profile["image"].copy()
        with self.assertRaisesRegex(UnsupportedProfile, "^PROFILE_UNSUPPORTED$"):
            self.boot("swapped-image").run(canonical(self.tools.create()))
        self.assertEqual(self.tools.claim_calls, 0)
        held = self.root / "held"
        held.write_bytes(b"approved bytes")
        held.chmod(0o600)
        expected = hash_bytes(held.read_bytes())
        self.assertEqual(read_pin(held, expected), b"approved bytes")
        held.write_bytes(b"substituted bytes")
        with self.assertRaisesRegex(ValueError, "^ISOLATION_UNVERIFIED$"):
            read_pin(held, expected)

    def test_untrusted_profile_and_tier_override_are_refused(self):
        self.profile["trust_class"] = "untrusted"
        self.policy.value["profiles"][digest(self.profile)] = self.entry
        request = self.tools.create()
        for case in (request, {**request, "tier": 1}):
            with self.assertRaisesRegex(UnsupportedProfile, "^PROFILE_UNSUPPORTED$") as refused:
                self.boot("refused-" + str("tier" in case)).run(canonical(case))
            self.assertEqual(set(refused.exception.action), {"reason", "retry_after", "next_command"})
        with self.assertRaises(UnsupportedProfile):
            authorize_job(self.contract, {"tier": 1, "admission": {"profile": self.profile}}, self.policy)
        self.assertEqual(self.tools.claim_calls, 0)

    def test_missing_cap_refuses_before_spawn_and_proves_zero_spawn(self):
        cgroup = self.root / "cap-readback"
        cgroup.mkdir()
        caps = dict.fromkeys(LIMIT_FILES, "1")
        for name in (*LIMIT_FILES, "cpuset.cpus.effective", "cpuset.mems.effective"):
            (cgroup / name).write_text("1\n")
        read_caps(cgroup, caps)
        (cgroup / "cpu.max").unlink()
        request = self.tools.create()
        boot = self.boot("missing-cap")
        runtime = NativeTier1(self.policy, request["grant"], boot.root)
        boot.runtime = runtime
        with patch.object(runtime, "_before_boot", side_effect=lambda: read_caps(cgroup, caps)):
            response, result, receipt = boot.run(canonical(request))
        self.assertEqual(response["code"], "ISOLATION_UNVERIFIED")
        self.assertIsNone(result)
        self.assertFalse(runtime.spawn_attempted)
        self.assertEqual(self.tools.claim_calls, 1)
        self.assertEqual(receipt["release_basis"], "never_started")
        self.tools.verify(receipt, "tools")

    def test_host_writes_are_confined_by_the_actual_mount_boundary(self):
        workspace = self.root / "workspace"
        workspace.mkdir(mode=0o700)
        outside = self.root / "outside"
        outside.write_text("unchanged")
        (workspace / "alias").symlink_to(outside)
        script = '''
            printf inside > /workspace/inside
            test ! -e "$1"
            if (printf wrong > "$1") 2>/workspace/path-error; then exit 1; fi
            if (printf wrong > /workspace/alias) 2>/workspace/link-error; then exit 2; fi
            if (printf wrong >&"$2") 2>/workspace/fd-error; then exit 3; fi
            if /assets/busybox mount -o remount,rw / 2>/workspace/mount-error; then exit 4; fi
            if (printf wrong > /assets/busybox) 2>/workspace/pin-error; then exit 5; fi
        '''
        with outside.open("ab") as source, tempfile.TemporaryFile() as evidence:
            forbidden = fcntl.fcntl(source.fileno(), fcntl.F_DUPFD_CLOEXEC, 64)
            try:
                with sealed("busybox", Path("/usr/bin/busybox").read_bytes()) as asset:
                    with patch.dict("fc_boot_tier1.APPS", {"firecracker": "/assets/busybox"}):
                        args = sandbox_args(workspace, {"firecracker": asset}, evidence.fileno(),
                                            os.getuid(), os.getgid(), "/assets/busybox")
                    subprocess.run(["/usr/bin/bwrap", *args, "sh", "-ec", script, "test",
                                    str(outside), str(forbidden)], check=True, timeout=8,
                                   close_fds=True, pass_fds=(asset, evidence.fileno()))
            finally:
                os.close(forbidden)
            evidence.seek(0)
            report = [decode(row) for row in evidence.read().splitlines()]
        self.assertEqual(report[-1], {"exit-code": 0})
        self.assertEqual((workspace / "inside").read_text(), "inside")
        self.assertEqual(outside.read_text(), "unchanged")

    def test_cli_returns_machine_readable_override_refusal(self):
        with redirect_stdout(StringIO()) as output:
            rc = main(["fcbootl", "--config", "/unused", "--socket", "/unused", "--tier", "1"])
        self.assertEqual(rc, 1)
        reply = decode(output.getvalue().encode())
        self.assertEqual(reply["code"], "PROFILE_UNSUPPORTED")
        self.assertTrue(reply["next_command"])
        self.assertIn("retry_after", reply)

    def test_kernel_readback_identifies_the_held_executable_and_private_mounts(self):
        workspace = self.root / "readback"
        workspace.mkdir(mode=0o700)
        with ExitStack() as stack:
            read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
            report = stack.enter_context(os.fdopen(read_fd, "rb"))
            status_writer = stack.enter_context(os.fdopen(write_fd, "wb"))
            assets = {name: stack.enter_context(sealed(name, payload))
                      for name, payload in (("firecracker", Path("/usr/bin/busybox").read_bytes()),
                                            ("Image", b"kernel fixture"), ("initrd", b"initrd fixture"))}
            stack.enter_context(patch.dict("fc_boot_tier1.APPS", {"firecracker": "/assets/busybox"}))
            args = sandbox_args(workspace, assets, write_fd, os.getuid(), os.getgid(), "/assets/busybox")
            script = 'n=0; while [ "$n" -lt 150 ] && [ ! -f /workspace/finished ]; do ' \
                     '/assets/busybox sleep 0.02; n=$((n+1)); done'
            with subprocess.Popen(["/usr/bin/bwrap", *args, "sh", "-ec", script],
                                  pass_fds=(write_fd, *assets.values())) as child:
                status_writer.close()
                first = decode(report.readline())
                proc = Path("/proc") / str(first["child-pid"])
                try:
                    deadline = time.monotonic() + 2
                    while "/assets/busybox" not in str((proc / "exe").readlink()):
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                    verify_mounts(proc, identity(workspace), first["mnt-namespace"])
                    with self.assertRaisesRegex(ValueError, "^ISOLATION_UNVERIFIED$"):
                        verify_mounts(proc, identity(self.root), first["mnt-namespace"])
                finally:
                    (workspace / "finished").touch()
                self.assertEqual(child.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
