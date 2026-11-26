"""Offline Tier 1 refusal, closure and kernel boundary controls."""

from contextlib import ExitStack, contextmanager, redirect_stdout
import copy
import fcntl
from io import StringIO
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import fc_boot_tier1 as tier1
from fc_boot import Contract, FixtureBoot, IsolationUnverified, UnsupportedProfile, canonical, decode, digest, hash_bytes
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

    def test_registered_tier2_is_refused_before_claim(self):
        self.entry["tier"] = 2
        request = self.tools.create()
        boot = self.boot("tier2")
        with self.assertRaises(UnsupportedProfile) as refused:
            boot.run(canonical(request))
        self.assertEqual(str(refused.exception), "PROFILE_UNSUPPORTED")
        self.assertEqual(set(refused.exception.action), {"reason", "retry_after", "next_command"})
        self.assertEqual(self.tools.claim_calls, 0)
        self.assertFalse((boot.root / "launch-intent.json").exists())

    def test_grant_expiring_during_verify_dispatch_never_reaches_sumsq(self):
        runtime = Mock()
        runtime.retire.return_value = (tier1.NEVER_STARTED, {"scope_gone": True, "surviving_pids": []})
        request = self.tools.create()
        boot = self.boot("expired-inspection", runtime)
        runtime.verify_dispatch.side_effect = lambda: setattr(
            boot, "now", lambda: int(request["grant"]["expires_mono_ns"]))
        runtime.sumsq.return_value = 385
        # The fixture's synthetic clock applies to dispatch, then returns to
        # the real monotonic clock for the signed release.
        runtime.retire.side_effect = lambda: (
            setattr(boot, "now", time.monotonic_ns) or runtime.retire.return_value)
        response, result, receipt = boot.run(canonical(request))
        runtime.verify_dispatch.assert_called_once_with()
        runtime.sumsq.assert_not_called()
        self.assertEqual((response["code"], result), ("OUTCOME_UNKNOWN", None))
        self.tools.verify(receipt, "tools")

    def test_grant_expiring_during_vsock_handshake_never_sends_sumsq(self):
        request = self.tools.create()
        runtime = NativeTier1(self.policy, request["grant"], self.root)
        exchange = Mock(return_value=f"SUMSQ {runtime.nonce} 10 385")

        @contextmanager
        def guest(_deadline):
            with patch("fc_boot_tier1.time.monotonic_ns", return_value=int(request["grant"]["expires_mono_ns"])):
                yield object()

        with patch.object(runtime, "guest", guest), patch.object(runtime.harness, "exchange", exchange):
            with self.assertRaises(IsolationUnverified):
                runtime.sumsq(10)
        exchange.assert_not_called()

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


class NativeTier1Q2(unittest.TestCase):
    """Run in an isolated PID namespace with disposable delegated cgroups."""

    boot = Tier1.boot

    def setUp(self):
        Tier1.setUp(self)
        self.assertNotEqual(os.readlink("/proc/self/ns/pid"), "pid:[4026531836]")
        cgroup = Path(os.environ["FC_BOOT_Q2_CGROUP"])
        self.assertIn(cgroup, (tier1.CGROUP / Path("/proc/self/cgroup").read_text().strip()[4:]).parents)
        for path in (self.root / "runtime", self.root / "outside"):
            path.mkdir(mode=0o700)
        caps = {"memory.high": str(384 << 20), "memory.max": str(512 << 20),
                "memory.swap.max": "0", "pids.max": "64", "cpu.max": "100000 100000",
                "cpuset.cpus": (cgroup / "cpuset.cpus.effective").read_text().strip()}
        self.policy.value.update(cgroup_root=str(cgroup), leaf_caps=caps)
        assets = Path(os.environ["FC_BOOT_TEST_ASSETS"])
        engine = read_pin(assets / "firecracker", CELL_IMAGE["runtime_sha256"])
        kernel = read_pin(assets / "vmlinux-6.1.188", CELL_IMAGE["kernel_sha256"])
        init = read_pin(assets / "init-aarch64", CELL_IMAGE["guest_init_sha256"])
        initrd = self.policy.harness.initramfs(init)
        self.assertEqual(hash_bytes(initrd), CELL_IMAGE["image_sha256"])
        self.policy.held = self.approved, Path("/usr/bin/bwrap").read_bytes(), {
            "firecracker": engine, "Image": kernel, "initrd": initrd,
        }
        self.request = self.tools.create()
        self.attempt = self.boot("native")
        self.runtime = NativeTier1(self.policy, self.request["grant"], self.attempt.root)
        self.attempt.runtime = self.runtime
        self.attempt.observe_custody = self.runtime.observe
        self.addCleanup(self.clean_native)
        self.enterContext(patch.object(self.runtime, "_before_boot", self.reserve))

    def reserve(self):
        # The test namespace owns a disposable delegation. Only the host
        # identity and unit-layout preflight is substituted.
        runtime = self.runtime
        runtime.groups = set(os.getgroups())
        runtime.baseline = int(tier1.status(Path("/proc/self/status"))["Seccomp_filters"][0])
        runtime.harness.check_kvm()
        runtime.cgroup_root_id = identity(runtime.root)
        runtime.runtime_root_id = identity(runtime.runtime_root)
        runtime.scope.mkdir()
        runtime.scope_id = identity(runtime.scope)
        tier1.set_caps(runtime.scope, self.policy.value["leaf_caps"])
        runtime.workspace.mkdir(mode=0o700)
        runtime.workspace_id = identity(runtime.workspace)

    def clean_native(self):
        runtime = self.runtime
        if runtime.process is not None and runtime.process.poll() is None:
            runtime.process.kill()
            runtime.process.wait(timeout=5)
        if runtime.scope.exists():
            (runtime.scope / "cgroup.kill").write_text("1\n")
            deadline = time.monotonic() + 5
            while runtime.members() and time.monotonic() < deadline:
                time.sleep(0.02)
            runtime.scope.rmdir()

    def assert_settled(self, basis, *, known=False):
        response, value, receipt = self.attempt.run(canonical(self.request))
        terminal = (self.attempt.root / "terminal.json").read_text()
        self.assertIsNotNone(receipt, (response, terminal))
        self.assertEqual(receipt["release_basis"], basis)
        if known:
            self.assertEqual((response["outcome"], value), ("known", 385), terminal)
        self.tools.verify(receipt, "tools")
        witness = decode((self.attempt.root / "witness.json").read_bytes())
        self.tools.verify(witness, "main")
        ref = witness["evidence_ref"]
        custody = decode((self.runtime.evidence / (ref["id"] + ".json")).read_bytes())
        self.assertEqual((custody["schema"], digest(custody), custody["proof"]),
                         ("vp.fc_custody/v1", ref["sha256"], witness["proof"]))
        self.assertFalse(self.runtime.scope.exists())
        self.assertFalse(self.runtime.workspace.exists())
        for member in witness["proof"].get("members", []):
            self.assertFalse(Path(f"/proc/{member['pid']}").exists())
        return response, witness["proof"]

    def test_partial_spawn_reaps_firecracker_without_startup_status(self):
        def missing_status(deadline):
            self.runtime.harness.wait_socket(self.runtime.api, self.runtime.process, deadline)
            raise IsolationUnverified("Injected loss of startup status")

        with patch.object(self.runtime, "read_status", side_effect=missing_status):
            response, proof = self.assert_settled("reaped")
        self.assertEqual(response["code"], "ISOLATION_UNVERIFIED")
        self.assertEqual({member["role"] for member in proof["members"]}, {"runtime", "supervisor"})
        self.assertEqual(proof["members"][0]["exit"], {"kind": "signal", "signal": signal.SIGKILL})

    def test_partial_spawn_without_runtime_drains_and_retains_custody(self):
        def missing_status(deadline):
            while self.runtime.process.pid not in self.runtime.members():
                time.sleep(min(0.02, self.runtime.harness.remaining(deadline)))
            raise IsolationUnverified("Bootstrap attached without launching a VMM")

        attach = tier1.ATTACH.replace("os.execve(", "import time\ntime.sleep(30)\nos.execve(")
        with patch.object(tier1, "ATTACH", attach), patch.object(
                self.runtime, "read_status", side_effect=missing_status):
            response, value, receipt = self.attempt.run(canonical(self.request))
        self.assertEqual((response["code"], value, receipt), ("OUTCOME_UNKNOWN", None, None))
        self.assertEqual(self.runtime.process.returncode, -signal.SIGKILL)
        self.assertFalse(self.runtime.scope.exists())
        self.assertFalse(self.runtime.workspace.exists())
        self.assertFalse((self.attempt.root / "witness.json").exists())
        recovery = decode((self.attempt.root / "recovery-required.json").read_bytes())
        self.assertIs(recovery["recovery_required"], True)

    def test_q2_before_boot(self):
        track = self.runtime.track

        def interrupt_bootstrap(pid, role, **options):
            member = track(pid, role, **options)
            if role == "supervisor":
                self.runtime.process.kill()
                raise IsolationUnverified("Killed the bootstrap before releasing the launch gate")
            return member

        with patch.object(self.runtime, "track", side_effect=interrupt_bootstrap):
            self.assert_settled("never_started")
        self.assertEqual(self.runtime.process.returncode, -signal.SIGKILL)

    def test_q2_after_api_ready(self):
        stage = self.attempt.stage
        reached_ready = []

        def interrupt(name, *args, **kwargs):
            stage(name, *args, **kwargs)
            if name == "api_ready":
                reached_ready.append(name)
                raise IsolationUnverified("Interrupted after API readiness")

        with patch.object(self.attempt, "stage", side_effect=interrupt):
            response, proof = self.assert_settled("reaped")
        self.assertEqual(reached_ready, ["api_ready"])
        self.assertTrue(self.runtime.api_seen)
        self.assertEqual(response["code"], "ISOLATION_UNVERIFIED")
        self.assertEqual(proof["members"][0]["exit"], {"kind": "signal", "signal": signal.SIGKILL})

    def stop_during_exchange(self, interrupt_effect=False):
        exchange = self.runtime.harness.exchange
        interrupted = set()

        def interrupt(channel, command, deadline):
            operation = command.split()[0]
            if operation == "STOP" or operation == "SUMSQ" and interrupt_effect:
                if operation == "SUMSQ":
                    channel.sendall((command + "\n").encode())
                interrupted.add(operation)
                raise IsolationUnverified("Interrupted the native guest exchange")
            return exchange(channel, command, deadline)

        with patch.object(self.runtime.harness, "exchange", side_effect=interrupt):
            response, proof = self.assert_settled("reaped", known=not interrupt_effect)
        self.assertEqual(interrupted, {"SUMSQ", "STOP"} if interrupt_effect else {"STOP"})
        self.assertEqual(response["outcome"], "unknown" if interrupt_effect else "known")
        self.assertEqual(proof["members"][0]["exit"], {"kind": "signal", "signal": signal.SIGKILL})

    def test_q2_mid_effect(self):
        self.stop_during_exchange(interrupt_effect=True)

    def test_q2_during_retirement(self):
        self.stop_during_exchange()

    def test_seccomp_inherited_filter_cannot_replace_firecracker_filter(self):
        original_args = tier1.sandbox_args
        launch = subprocess.Popen

        def readonly_stdin(*args, stdin, **kwargs):
            # Isolate the filter gate from unrelated writable-fd rejection.
            with open(os.devnull, "rb") as source:
                return launch(*args, stdin=source, **kwargs)

        # Install a harmless inherited filter in the test bootstrap, then
        # disable Firecracker's own filter. The prior count-only check accepts.
        inherited = """
import ctypes
class Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("action", ctypes.c_uint)]
class Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]
libc = ctypes.CDLL(None)
bpf = Filter(6, 0, 0, 0x7fff0000)
program = Program(1, ctypes.pointer(bpf))
assert libc.prctl(38, 1, 0, 0, 0) == 0
assert libc.prctl(22, 2, ctypes.byref(program), 0, 0) == 0
"""
        attach = tier1.ATTACH.replace("os.execve(", inherited + "\nos.execve(")
        with patch.object(tier1, "ATTACH", attach), patch.object(
                tier1, "sandbox_args", side_effect=lambda *args: [*original_args(*args), "--no-seccomp"]), \
                patch.object(tier1.subprocess, "Popen", side_effect=readonly_stdin):
            response, _ = self.assert_settled("reaped")
        self.assertEqual(response.get("code"), "ISOLATION_UNVERIFIED")
        terminal = decode((self.attempt.root / "terminal.json").read_bytes())
        self.assertEqual(terminal["reason"], "An unexpected filter preceded the pinned Firecracker executable")
        self.assertFalse(self.runtime.boot_requested)


if __name__ == "__main__":
    unittest.main()
