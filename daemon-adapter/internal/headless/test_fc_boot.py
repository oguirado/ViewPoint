"""Offline contract and kill controls. Run signal cases under unshare -Urpf."""

import base64
import copy
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fc_boot import (BOOT_ID, WITNESS_BINDING, Contract, FixtureBoot,
                     Refusal, RuntimeFailure, canonical, decode, digest, selected,
                     signing_bytes, verify_signature)


class ToolsFixture:
    """Test doubles for the frozen wire and custody responses."""

    boot_owner = "fixture-main-boot"
    minimum_ram = 8 * (1 << 30)

    def __init__(self, contract):
        self.contract = contract
        self.keys = {role: Ed25519PrivateKey.generate() for role in ("tools", "main")}
        self.profile = copy.deepcopy(contract.schemas["capsule_profile"].schema["examples"][0])
        guardian = {"mode": "fixture", "scope_memory_max_bytes": self.minimum_ram}
        psi = {"mode": "shadow", "scenario": "fixture"}
        self.guardian_sha256 = digest(guardian)
        self.psi_sha256 = digest(psi)
        self.profile["guardian_policy_sha256"] = self.guardian_sha256
        self.expected = {"host_id": "fixture-host", "boot_id": BOOT_ID.read_text().strip(),
                         "principal_id": "fixture-principal", "attempt_id": "fixture-attempt",
                         "epoch": 1, "slot_id": "fixture-slot", "lease_id": "fixture-lease",
                         "admission_request_id": "fixture-admit"}
        self.admission_ref = {"id": "fixture-admission", "sha256": digest({
            "decision": "admitted", "request_id": self.expected["admission_request_id"],
        })}
        self.claimed = None
        self.receipt = None
        self.mismatch_claim = False
        self.mismatch_release = False
        self.claim_calls = 0

    def sign(self, record, role):
        record = {**record, "signer": {"iss": role, "kid": "fixture-" + role, "alg": "Ed25519"}}
        signature = self.keys[role].sign(signing_bytes(record))
        record["signer"]["sig"] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        return record

    def verify(self, record, role):
        verify_signature(record, role, "fixture-" + role, self.keys[role].public_key())

    def create(self, expired=False):
        now = time.monotonic_ns()
        self.grant = self.sign({
            "schema": "vp.admission_grant/v1", **self.expected, "backend": "firecracker",
            "ram_reservation_bytes": self.minimum_ram, "receipt_ref": self.admission_ref,
            "profile_sha256": digest(self.profile), "priority_class": self.profile["priority_class"],
            "issued_mono_ns": str(now - 2), "expires_mono_ns": str(now if expired else now + 30_000_000_000),
            "psi": {"mode": "shadow", "decision": "would_refuse", "policy_sha256": self.psi_sha256,
                    "sampled_mono_ns": str(now - 3), "metrics": {
                        "memory_full_avg10_bps": 1, "cpu_some_avg10_bps": 1, "io_full_avg10_bps": 0,
                    }},
        }, "tools")
        self.request = {"schema": "vp.boot_request/v1", "request_id": "fixture-create",
                        "op": "create", "profile": self.profile, "grant": self.grant}
        self.contract.validate("boot_request", self.request)
        return copy.deepcopy(self.request)

    def claim(self, call):
        self.claim_calls += 1
        expected = selected(self.grant, ("host_id", "boot_id", "slot_id", "lease_id", "epoch", "attempt_id"))
        expected.update(boot_owner=self.boot_owner, create_request_sha256=digest(self.request))
        if call != expected or not int(self.grant["issued_mono_ns"]) <= time.monotonic_ns() < int(
                self.grant["expires_mono_ns"]):
            return {"outcome": "invalid_grant", "code": "GRANT_INVALID"}
        self.claimed = dict(call)
        reply = {"outcome": "claimed", **call, "expires_mono_ns": self.grant["expires_mono_ns"]}
        if self.mismatch_claim:
            reply["attempt_id"] = "other-attempt"
        return reply

    def sign_witness(self, value):
        return self.sign(value, "main")

    def release(self, witness):
        self.verify(witness, "main")
        self.contract.validate("reap_witness", witness)
        assert selected(witness, WITNESS_BINDING) == selected(self.grant, WITNESS_BINDING)
        assert self.claimed is not None and self.receipt is None
        receipt = {
            "schema": "vp.slot_release_receipt/v1", **selected(witness, WITNESS_BINDING),
            "released_mono_ns": str(time.monotonic_ns()),
            "ram_released_bytes": self.grant["ram_reservation_bytes"], "disposition": "released",
            "release_basis": witness["proof"]["kind"],
            "prior_admission_receipt_ref": self.admission_ref,
            "proof_ref": {"id": witness["witness_id"], "sha256": digest(witness)},
            "receipt_id": "fixture-release",
        }
        if self.mismatch_release:
            receipt["attempt_id"] = "other-attempt"
        self.receipt = self.sign(receipt, "tools")
        return self.receipt


GUEST = """\
import sys
print('api_ready')
for row in sys.stdin:
    command, _, value = row.strip().partition(' ')
    if command == 'SUMSQ':
        print('work_started')
        assert input() == 'finish'
        print(sum(n * n for n in range(1, int(value) + 1)))
    elif command == 'stop':
        print('stopping')
        assert input() == 'exit'
        break
    else:
        assert command == 'hello'
        print('ready ' + value)
"""


def ticks(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


class RuntimeFixture:
    """Real disposable subprocess and sockets standing in for a VMM and its scope."""

    def __init__(self, root, cut=None):
        self.root = root
        self.cut = cut
        self.scope = root / "fixture.scope"
        self.sockets = []
        self.vmm = None
        self.started_work = False
        self.signal_at = None

    def start_child(self, program):
        return subprocess.Popen([sys.executable, "-I", "-u", "-c", program],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                text=True, start_new_session=True)

    def kill(self, process, at):
        # Namespace PID 1 runs unittest, with a procfs for that same namespace.
        assert os.getpid() == 1 and Path("/proc/1").stat() == Path("/proc/self").stat()
        assert process.pid > 1 and os.getpgid(process.pid) == process.pid
        with_pidfd = os.pidfd_open(process.pid)
        try:
            signal.pidfd_send_signal(with_pidfd, signal.SIGKILL)
        finally:
            os.close(with_pidfd)
        assert process.wait(timeout=5) == -signal.SIGKILL
        self.signal_at = at

    def before_boot(self):
        if self.cut == "before_boot":
            # Kill the launcher stand-in before it can return permission to
            # start the runtime. No runtime or private socket has been created.
            with self.start_child("input()") as launcher:
                pid = launcher.pid
                self.kill(launcher, self.cut)
                assert not Path(f"/proc/{pid}").exists()
            raise RuntimeFailure("launcher killed before boot")

    def send(self, value):
        self.vmm.stdin.write(value + "\n")
        self.vmm.stdin.flush()

    def expect(self, response):
        with selectors.DefaultSelector() as selector:
            selector.register(self.vmm.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=5):
                raise RuntimeFailure("fixture response timeout")
        value = self.vmm.stdout.readline().rstrip("\n")
        if value != response:
            raise RuntimeFailure("unexpected fixture response")

    def launch(self, profile, stage):
        assert profile["network_policy"] == {"mode": "none"} and self.vmm is None
        self.scope.mkdir(mode=0o700)
        self.identity = self.scope.stat()
        for name in ("api.sock", "vsock.sock"):
            path = self.scope / name
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(path))
            self.sockets.append((sock, path))
        self.vmm = self.start_child(GUEST)
        self.pid_ticks = ticks(self.vmm.pid)
        stage("vmm_exec")
        self.expect("api_ready")
        stage("api_ready")
        if self.cut == "after_api_ready":
            self.kill(self.vmm, self.cut)
            raise RuntimeFailure("runtime killed after api_ready")
        challenge = os.urandom(16).hex()
        self.send("hello " + challenge)
        self.expect("ready " + challenge)

    def sumsq(self, n):
        self.send("SUMSQ " + str(n))
        self.expect("work_started")
        self.started_work = True
        if self.cut == "mid_effect":
            self.kill(self.vmm, self.cut)
            raise RuntimeFailure("runtime killed with dispatched effect")
        self.send("finish")
        self.expect("385")
        return 385

    def retire(self):
        if self.vmm is None:
            assert not self.scope.exists() and not self.sockets
            return {"kind": "never_started", "launch_fenced": True, "spawn_count": 0,
                    "runtime_inventory_absent": True, "sockets_absent": True}, self.observations([])
        if self.vmm.poll() is None:
            self.send("stop")
            self.expect("stopping")
            if self.cut == "during_retirement":
                self.kill(self.vmm, self.cut)
            else:
                self.send("exit")
        code = self.vmm.wait(timeout=5)
        self.vmm.stdin.close()
        self.vmm.stdout.close()
        assert not Path(f"/proc/{self.vmm.pid}").exists()
        members = [{"role": "runtime", "pid": self.vmm.pid, "start_ticks": self.pid_ticks,
                    "exit": {"kind": "signal", "signal": -code} if code < 0 else {"kind": "exit", "code": code},
                    "absence": "pid_absent"}]
        sockets = []
        for sock, path in self.sockets:
            sock.close()
            path.unlink()
            sockets.append({"path": str(path), "state": "removed"})
        sockets.append({"path": str(self.scope / "vsock.sock_19001"), "state": "never_created"})
        self.scope.rmdir()
        return {"kind": "reaped", "launch_fenced": True, "members": members, "sockets": sockets,
                "scope": {"path": str(self.scope), "device": str(self.identity.st_dev),
                          "inode": str(self.identity.st_ino), "invocation_id": "fixture-invocation",
                          "membership_complete": True, "gone": not self.scope.exists()},
                "runtime_dir_gone": not self.scope.exists(), "backstop": "none"}, self.observations(members)

    def observations(self, members):
        return {"scope_kind": "fixture_directory", "scope_gone": not self.scope.exists(),
                "members": members, "surviving_pids": [], "killed_at": self.signal_at}


class BootTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = Contract(os.environ["FC_BOOT_CONTRACT_ROOT"])

    def setup_attempt(self, cut=None):
        temp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="fcboot-")))
        tools = ToolsFixture(self.contract)
        runtime = RuntimeFixture(temp, cut)
        root = temp / "evidence"
        observer = {
            "run_id": "fixture-run", "cell_id": "fixture-cell", "block_id": "fixture-block", "arm": "A",
        }
        keys = {role: ("fixture-" + role, key.public_key()) for role, key in tools.keys.items()}
        boot = FixtureBoot(self.contract, tools, runtime, root, observer, keys, tools.sign_witness)
        return boot, tools, runtime

    def check_settled(self, boot, tools, runtime, receipt):
        self.assertEqual(receipt, tools.receipt)
        self.assertIsNotNone(receipt)
        self.assertFalse(runtime.scope.exists())
        teardown = decode((boot.root / "teardown.json").read_bytes())
        self.assertTrue(teardown["scope_gone"])
        self.assertEqual(teardown["surviving_pids"], [])
        rows = [decode(row) for row in (boot.root / "stages.jsonl").read_bytes().splitlines()]
        self.assertEqual([row["sequence"] for row in rows], list(range(1, len(rows) + 1)))
        self.assertEqual([row["stage"] for row in rows[-3:]], ["reap", "release", "terminal"])
        self.assertEqual({row["boot_id"] for row in rows}, {BOOT_ID.read_text().strip()})
        observed = [int(row["monotonic_ns"]) for row in rows]
        self.assertEqual(sorted(observed), observed)

    def test_clean(self):
        boot, tools, runtime = self.setup_attempt()
        response, value, receipt = boot.run(canonical(tools.create()))
        self.assertEqual((response["state"], value), ("reaped", 385))
        self.check_settled(boot, tools, runtime, receipt)

    def test_refusals_zero_effect(self):
        for case, expected in (("forged", "GRANT_INVALID"), ("expired", "GRANT_EXPIRED"),
                               ("other_capsule", "GRANT_MISMATCH"), ("duplicate_key", "INVALID_REQUEST"),
                               ("mismatched_claim", "GRANT_UNAVAILABLE")):
            with self.subTest(case=case):
                boot, tools, runtime = self.setup_attempt()
                request = tools.create(expired=case == "expired")
                if case == "forged":
                    request["grant"]["ram_reservation_bytes"] += 1
                if case == "other_capsule":
                    request["profile"]["profile_id"] = "other-capsule"
                if case == "mismatched_claim":
                    tools.mismatch_claim = True
                raw = canonical(request)
                if case == "duplicate_key":
                    raw = b'{"op":"create",' + raw[1:]
                with self.assertRaisesRegex(Refusal, f"^{expected}$"):
                    boot.run(raw)
                self.assertEqual(tools.claim_calls, int(case == "mismatched_claim"))
                self.assertIsNone(runtime.vmm)
                self.assertFalse(runtime.scope.exists())
                self.assertFalse(runtime.started_work)
                self.assertIsNone(tools.receipt)
                self.assertFalse((boot.root / "launch-intent.json").exists())

    def test_signed_mismatched_release_is_unsettled(self):
        boot, tools, runtime = self.setup_attempt()
        tools.mismatch_release = True
        response, value, receipt = boot.run(canonical(tools.create()))
        self.assertEqual((response["outcome"], value, receipt), ("unknown", None, None))
        self.assertTrue((boot.root / "witness.json").is_file())
        self.assertFalse((boot.root / "release.json").exists())
        self.assertFalse(runtime.scope.exists())

    def test_q2_kill_points(self):
        for cut, proof in (("before_boot", "never_started"), ("after_api_ready", "reaped"),
                           ("mid_effect", "reaped"), ("during_retirement", "reaped")):
            with self.subTest(cut=cut):
                boot, tools, runtime = self.setup_attempt(cut)
                response, _, receipt = boot.run(canonical(tools.create()))
                self.check_settled(boot, tools, runtime, receipt)
                self.assertEqual(runtime.signal_at, cut)
                self.assertEqual(receipt["release_basis"], proof)
                self.assertEqual(runtime.started_work, cut in {"mid_effect", "during_retirement"})
                self.assertEqual(response["outcome"], "known" if cut == "during_retirement" else "unknown")
                print(f"{cut}: fixture scope gone, zero surviving PIDs, {proof} release verified", flush=True)


if __name__ == "__main__":
    unittest.main()
