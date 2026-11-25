"""Exercise MAIN against the pinned fcbootl-serve and a disposable runtime."""

import os
from pathlib import Path
import pwd
import selectors
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from fc_boot import BOOT_ID, Contract, FIXTURE_GRANT, Refusal, RuntimeFailure, canonical, decode, digest, hash_bytes
from fc_boot_owner import (OWNER, OWNER_CGROUP, OWNER_SLICE, OWNER_UID, SocketTools,
                           launch_owner, owned, ram_admission, run_attempt, run_config, run_pair, signing_key)
from test_fc_boot import GUEST, RuntimeFixture, ticks

GIB = 1 << 30
BOUND = Path(OWNER_CGROUP).parent
CGROUP_ROOT = Path("/sys/fs/cgroup") / str(BOUND).lstrip("/")
TIMEOUT = 10
TTL = 120_000_000_000
RAM = 512 << 20


def put(path, payload, mode=0o600):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def host_children(pid):
    return [int(n) for n in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]


def member(role, pid):
    return {"role": role, "pid": pid, "start_ticks": ticks(pid), "absence": "pid_absent"}


class StandIn:
    """Drive the subprocess fixture from outside its unshare PID namespace."""

    def __init__(self, scoped_leaf, runtime_dir, cut=None):
        self.scope, self.directory, self.cut = scoped_leaf, runtime_dir, cut
        self.process = None
        self.members = []
        self.sockets = []
        self.killed_at = None

    def start(self):
        self.process = subprocess.Popen(
            ["unshare", "-Urpf", "--mount-proc", "--kill-child", "--",
             sys.executable, "-B", __file__, "--stand-in"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.expect("waiting")

    def send(self, line):
        print(line, file=self.process.stdin, flush=True)

    def read(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(TIMEOUT):
                raise RuntimeFailure("stand-in response timeout")
        line = self.process.stdout.readline().strip()
        if not line:
            raise RuntimeFailure("stand-in response absent")
        return line

    def expect(self, value):
        if self.read() != value:
            raise RuntimeFailure("unexpected stand-in reply")

    def before_boot(self):
        if self.cut == "before_boot":
            self.start()
            self.send("kill-launcher")
            self.expect("fenced")
            self.killed_at = self.cut
            self.finish()
            raise RuntimeFailure("launcher killed before boot")

    def launch(self, profile, stage):
        assert profile["network_policy"] == {"mode": "none"}
        self.directory.mkdir(mode=0o700)
        self.identity = self.directory.stat()
        for name in ("api.sock", "vsock.sock"):
            path = self.directory / name
            endpoint = socket.socket(socket.AF_UNIX)
            endpoint.bind(str(path))
            self.sockets.append((endpoint, path))
        self.start()
        self.send("boot")
        self.expect("api_ready")
        (supervisor,) = host_children(self.process.pid)
        (guest,) = host_children(supervisor)
        self.members = [member("runtime", guest), member("supervisor", supervisor)]
        stage("vmm_exec")
        stage("api_ready")
        if self.cut == "after_api_ready":
            self.send("kill")
            self.expect("killed")
            self.killed_at = self.cut
            raise RuntimeFailure("stand-in killed after api_ready")
        self.send("hello")
        self.expect("ready")

    def sumsq(self, n):
        self.send("work " + str(n))
        self.expect("work_started")
        if self.cut == "mid_effect":
            self.send("kill")
            self.expect("killed")
            self.killed_at = self.cut
            raise RuntimeFailure("stand-in killed mid-effect")
        self.send("finish")
        return int(self.read())

    def finish(self):
        try:
            if self.process.wait(timeout=TIMEOUT) != 0:
                raise RuntimeFailure("stand-in supervisor did not exit cleanly")
        finally:
            self.process.stdin.close()
            self.process.stdout.close()

    def cleanup(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()  # Its own unshare --kill-child reaps the namespace.
            self.process.wait(timeout=TIMEOUT)
        for endpoint, _ in self.sockets:
            endpoint.close()

    def retire(self):
        if not self.members:
            assert self.process is not None and self.process.poll() == 0 and not self.directory.exists()
            proof = {"kind": "never_started", "launch_fenced": True, "spawn_count": 0,
                     "runtime_inventory_absent": True, "sockets_absent": True}
        else:
            if self.killed_at is None:
                self.send("stop")
                self.expect("stopping")
                if self.cut == "during_retirement":
                    self.send("kill")
                    self.expect("killed")
                    self.killed_at = self.cut
                else:
                    self.send("exit")
            guest_exit = int(self.read())
            self.finish()
            self.members[0]["exit"] = ({"kind": "signal", "signal": -guest_exit} if guest_exit < 0
                                       else {"kind": "exit", "code": guest_exit})
            self.members[1]["exit"] = {"kind": "exit", "code": 0}
            assert all(not Path(f"/proc/{item['pid']}").exists() for item in self.members)
            sockets = []
            for endpoint, path in self.sockets:
                endpoint.close()
                path.unlink()
                sockets.append({"path": str(path), "state": "removed"})
            self.directory.rmdir()
            proof = {"kind": "reaped", "launch_fenced": True, "members": self.members, "sockets": sockets,
                     "scope": {"path": str(self.scope), "device": str(self.identity.st_dev),
                               "inode": str(self.identity.st_ino), "invocation_id": "stand-in-scope",
                               "membership_complete": True, "gone": not self.scope.exists()},
                     "runtime_dir_gone": not self.directory.exists(), "backstop": "none"}
        return proof, {"scope_gone": not self.scope.exists(), "surviving_pids": [], "killed_at": self.killed_at}


def stand_in():
    assert os.getpid() == 1 and Path("/proc/1").stat() == Path("/proc/self").stat()
    runtime = RuntimeFixture(Path("."))
    print("waiting", flush=True)
    if input() == "kill-launcher":
        with runtime.start_child("input()") as launcher:
            runtime.kill(launcher, "before_boot")
        print("fenced", flush=True)
        return
    runtime.vmm = runtime.start_child(GUEST)
    runtime.expect("api_ready")
    print("api_ready", flush=True)
    for line in sys.stdin:
        command, _, argument = line.strip().partition(" ")
        if command == "kill":
            runtime.kill(runtime.vmm, "stand-in")
            print("killed", flush=True)
            break
        if command == "hello":
            runtime.send("hello nonce")
            runtime.expect("ready nonce")
            reply = "ready"
        elif command == "work":
            runtime.send("SUMSQ " + argument)
            reply = "work_started"
            runtime.expect(reply)
        elif command == "finish":
            runtime.send("finish")
            reply = "385"
            runtime.expect(reply)
        elif command == "stop":
            runtime.send("stop")
            reply = "stopping"
            runtime.expect(reply)
        else:
            assert command == "exit"
            runtime.send("exit")
            break
        print(reply, flush=True)
    print(runtime.vmm.wait(timeout=TIMEOUT), flush=True)


class SocketIntegration(unittest.TestCase):
    @classmethod
    def subids(cls):
        name = pwd.getpwuid(OWNER_UID).pw_name
        for line in Path("/etc/subuid").read_text().splitlines():
            owner, start, size = line.split(":")
            if owner in {name, str(OWNER_UID)} and int(size) >= 2:
                return int(start), int(start) + 1
        raise RuntimeError("two subordinate UIDs required")

    @classmethod
    def as_uid(cls, host_uid, *command):
        return ["unshare", "--map-user", "0", "--map-group", str(os.getgid()),
                "--map-users", f"1:{host_uid}:1", "--setuid", "1", "--", *command]

    @classmethod
    def arrange_dir(cls, path, host_uid):
        parent = path.parent
        os.chmod(parent, 0o770)
        try:
            subprocess.run(cls.as_uid(host_uid, "mkdir", "-m", "0750", str(path)), check=True)
        finally:
            os.chmod(parent, 0o750)

    @classmethod
    def remove_root(cls, path):
        # Namespace capabilities apply only to the MAIN and fixture UID mappings.
        script = "import shutil,sys; shutil.rmtree(sys.argv[1])"
        subprocess.run(["unshare", "--map-user", "0", "--map-group", str(os.getgid()),
                        "--map-users", f"1:{cls.tools_uid}:2", "--",
                        sys.executable, "-B", "-c", script, str(path)], check=True)

    @classmethod
    def as_tools_put(cls, path, payload):
        script = ("import os,sys; fd=os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); "
                  "os.write(fd, sys.stdin.buffer.read()); os.close(fd)")
        subprocess.run(cls.as_uid(cls.tools_uid, sys.executable, "-B", "-c", script, str(path)),
                       input=payload, check=True)
        return str(path)

    @classmethod
    def service_scope(cls, unit, *command):
        ram_admission(unit)
        return ["systemd-run", "--user", "--scope", f"--unit={unit}", f"--slice={OWNER_SLICE}",
                "-p", "MemoryHigh=6G", "-p", "MemoryMax=8G", "--", "taskset", "-c", "0-15",
                "env", "-u", "DISPLAY", "-u", "WAYLAND_DISPLAY", *command]

    @classmethod
    def setUpClass(cls):
        cls.enterClassContext(owned())
        cls.tools_uid, cls.observer_uid = cls.subids()
        cls.root = Path(tempfile.mkdtemp(prefix=".fc5-", dir=Path.home()))
        cls.addClassCleanup(cls.remove_root, cls.root)
        cls.root.chmod(0o750)
        tools, evidence, runtime, journal = (cls.root / name for name in ("t", "e", "r", "j"))
        cls.arrange_dir(tools, cls.tools_uid)
        cls.arrange_dir(evidence, cls.observer_uid)
        runtime.mkdir(mode=0o750)
        journal.mkdir(mode=0o700)
        cls.evidence, cls.runtime, cls.journal = evidence, runtime, journal
        cls.endpoint = str(tools / "api.sock")
        ledger = tools / "ledger"
        ram = ledger / "ram-admission"
        subprocess.run(cls.as_uid(cls.tools_uid, "mkdir", "-m", "0700", str(ledger), str(ram)), check=True)
        # stat via the owning namespace since the ledger root is private.
        result = subprocess.check_output(cls.as_uid(cls.tools_uid, "stat", "-c", "%d %i", str(ram)), text=True)
        dev, ino = result.split()
        cls.contract = Contract(os.environ["FC_BOOT_CONTRACT_ROOT"])
        cls.boot_id = BOOT_ID.read_text().strip()
        cls.host_id = "offline-fcbootl"
        cls.profile = cls.contract.schemas["capsule_profile"].schema["examples"][0].copy()
        guardian = {"mode": "offline-stand-in"}
        provenance = {"fixture": "offline-credential-free"}
        cls.profile.update(priority_class="agent_foreground", guardian_policy_sha256=digest(guardian),
                           credential_provenance={"id": "stand-in-credential", "sha256": digest(provenance)})
        cls.profile_sha = digest(cls.profile)
        tools_key, main_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
        tools_key_pin = ("offline-tools", "tools-key")
        main_key_pin = ("offline-main", "main-key")
        policy = {"version": "offline-test", "mode": "shadow", "sampler": "linux-proc-pressure-avg10/v1",
                  "max_age_ns": 1_000_000_000, "thresholds_bps": {"cpu_some_avg10_bps": 10000,
                    "memory_full_avg10_bps": 10000, "io_full_avg10_bps": 10000}}
        main_pin, signer = signing_key(*main_key_pin, main_key)
        cls.trust = {"host_id": cls.host_id, "tools_key": (*tools_key_pin, tools_key.public_key()),
                     "main_key": main_pin, "sign_witness": signer, "psi_policy": policy, "ttl_ns": (TTL,)}
        put(journal / "main.pem", main_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        put(journal / "tools.pub", tools_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
        put(journal / "policy.json", canonical(policy))
        paths = {"firecracker": str(ram), **dict.fromkeys(("docker", "vm", "host_lane", "legacy_arm"), "disabled")}
        cutover = {"schema": "vp.fc_admission_cutover/v1", "host_id": cls.host_id, "boot_id": cls.boot_id,
                   "ledger_root": str(ledger), "ram_device": dev, "ram_inode": ino,
                   "admission_paths": paths, "wholegate_bytes": 0}

        def key_pin(identity, path):
            return {"iss": identity[0], "kid": identity[1], "key_path": path}

        config = {
            "socket_path": cls.endpoint, "ledger_root": str(ledger), "host_id": cls.host_id,
            "cutover_path": cls.as_tools_put(tools / "cutover.json", canonical(cutover)),
            "cutover_sha256": digest(cutover), "grant_ttl_ns": TTL, "accepted_grant_ttl_ns": [TTL],
            "max_pair_window_ns": TTL, "max_pair_draw_budget": 4, "floor_bytes": 20 * GIB,
            "wholegate_bytes": 0, "admission_paths": paths, "psi_policy": policy,
            "profiles": {cls.profile_sha: {
                "guardian_path": cls.as_tools_put(tools / "guardian.json", canonical(guardian)),
                "guardian_sha256": digest(guardian), "outer_scope_cap_bytes": RAM, "overhead_bytes": 1024,
                "credential_provenance": cls.profile["credential_provenance"],
                "provenance_path": cls.as_tools_put(tools / "provenance.json", canonical(provenance)),
            }},
            "boot_owners": {OWNER: OWNER},
            "roles": [{"uid": 0, "cgroup": OWNER_CGROUP, "principal": OWNER,
                       "priorities": ["agent_foreground", "background"], "boot_owner": OWNER}],
            "signer": key_pin(tools_key_pin, cls.as_tools_put(tools / "tools.pem",
                tools_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))),
            "witness_keys": {OWNER: key_pin(main_key_pin, cls.as_tools_put(tools / "main.pub",
                main_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)))},
            "evidence_root": str(evidence),
            # Host root and the independently mapped observer are both unmapped
            # in the Tools user namespace. MAIN and Tools are mapped separately.
            "evidence_uid": int(Path("/proc/sys/kernel/overflowuid").read_text()),
            "cgroup_root": str(CGROUP_ROOT), "runtime_root": str(runtime),
        }
        config_file = cls.as_tools_put(tools / "config.json", canonical(config))
        pin = Path(os.environ["FC_BOOT_TOOLS_ROOT"])
        revision = subprocess.check_output(["git", "-C", str(pin), "rev-parse", "HEAD"], text=True).strip()
        if revision != "8990f272d202e5bb32f4e3dcd6c4248752908e38":
            raise RuntimeError("wrong frozen Tools checkout")
        unit = f"mv-fc5-tools-{cls.root.name[5:]}.scope"
        server = subprocess.Popen(cls.service_scope(unit, *cls.as_uid(cls.tools_uid, "env",
            "PYTHONDONTWRITEBYTECODE=1", f"MV_RAM_ADMISSION_DIR={ram}",
            str(pin / "tools/vp-tools"), "fcbootl-serve", "--config", config_file)))
        cls.addClassCleanup(cls.stop_server, server, unit)
        deadline = time.monotonic() + TIMEOUT
        while not Path(cls.endpoint).is_socket():
            if server.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("offline Tools service did not start")
            time.sleep(0.02)
        cls.client = SocketTools(cls.endpoint, cls.tools_uid, str(BOUND / unit))

    @staticmethod
    def stop_server(server, unit):
        if server.poll() is None:
            subprocess.run(["systemctl", "--user", "stop", unit], check=True)
        server.wait(timeout=TIMEOUT)

    def job(self, name):
        return {"admission": {"schema": "vp.admission_request/v1", "request_id": name, "host_id": self.host_id,
                              "boot_id": self.boot_id, "principal_id": OWNER, "backend": "firecracker",
                              "profile": self.profile, "profile_sha256": self.profile_sha,
                              "requested_ram_bytes": RAM},
                "create_request_id": "create-" + name,
                "stage_identity": {"run_id": "offline-" + name, "cell_id": "offline-cell",
                                   "block_id": "offline-block", "arm": "A"}}

    def adapter(self, job, grant):
        attempt = grant["attempt_id"]
        runtime = StandIn(CGROUP_ROOT / (attempt + ".scope"), self.runtime / attempt, getattr(self, "cut", None))
        self.addCleanup(runtime.cleanup)
        self.last_runtime = runtime

        def observe(claim, proof, observed):
            ref = "observer-" + attempt
            value = {"schema": "vp.fc_custody/v1", "claim": claim, "proof": proof, "observed_mono_ns": observed,
                     "scope_path": str(runtime.scope), "runtime_dir": str(runtime.directory)}
            leaf = self.evidence / (ref + ".json")
            script = """
import json
import os
from pathlib import Path
import sys

payload = sys.stdin.buffer.read()
evidence = json.loads(payload)
assert all(not os.path.lexists(evidence[field]) for field in ("scope_path", "runtime_dir"))
assert all(not Path(f"/proc/{member['pid']}").exists() for member in evidence["proof"].get("members", []))
path = Path(sys.argv[1])
with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444), "wb") as stream:
    stream.write(payload)
    stream.flush()
    os.fsync(stream.fileno())
fd = os.open(path.parent, os.O_DIRECTORY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
"""
            subprocess.run(self.as_uid(self.observer_uid, sys.executable, "-B", "-c", script, str(leaf)),
                           input=canonical(value), check=True)
            self.assertEqual(leaf.stat().st_uid, self.observer_uid)
            return {"id": ref, "sha256": digest(value)}
        return runtime, observe

    def attempt(self, job, pair=None):
        return run_attempt(self.client, self.contract, self.trust, job, self.journal, self.adapter, pair)

    def assert_settled(self, receipt, kind="reaped"):
        self.assertEqual(receipt["release_basis"], kind)
        self.assertEqual(receipt["disposition"], "released")
        self.assertEqual(receipt["ram_released_bytes"], RAM + 1024)
        attempt = self.journal / receipt["attempt_id"]
        witness = decode((attempt / "witness.json").read_bytes())
        grant = decode((attempt / "request.json").read_bytes())["grant"]
        self.assertEqual(receipt["prior_admission_receipt_ref"], grant["receipt_ref"])
        self.assertEqual(receipt["proof_ref"], {"id": witness["witness_id"], "sha256": digest(witness)})
        self.assertEqual(self.client.call("release", witness), receipt)
        claim = decode((attempt / "claim.json").read_bytes())
        self.assertEqual(claim["expires_mono_ns"], grant["expires_mono_ns"])
        return attempt

    def test_lost_reply_retry_and_receipt(self):
        job = self.job("retry")
        original = self.client._exchange
        sent, lost = {}, {}

        def drop_first_response(frame):
            operation = decode(frame)["op"]
            reply = original(frame)
            if operation in {"grant", "claim"}:
                sent.setdefault(operation, []).append(frame)
                if operation not in lost:
                    lost[operation] = reply
                    raise ConnectionError("first response discarded after Tools committed")
                if operation == "grant":
                    self.assertEqual(lost[operation], reply)
                else:
                    self.assertEqual(lost[operation]["outcome"], "claimed")
                    self.assertEqual({**lost[operation], "outcome": "already_claimed"}, reply)
            return reply

        with patch.object(self.client, "_exchange", side_effect=drop_first_response):
            response, result, receipt = self.attempt(job)
        self.assertEqual((response["outcome"], result), ("known", 385))
        for frames in sent.values():
            self.assertEqual(len(frames), 2)
            self.assertEqual(frames[0], frames[1])
        attempt = self.assert_settled(receipt)
        self.assertEqual(self.client.call("grant", job["admission"]), lost["grant"])
        claim = decode((attempt / "claim-intent.json").read_bytes())
        with self.assertRaisesRegex(Refusal, "^STALE_EPOCH$"):
            self.client.call("claim", claim)
        with self.assertRaisesRegex(Refusal, "^REQUEST_ID_CONFLICT$"):
            self.client.call("grant", {**job["admission"], "requested_ram_bytes": RAM + 1})

    def test_kill_matrix(self):
        for cut in ("before_boot", "after_api_ready", "mid_effect", "during_retirement"):
            with self.subTest(cut=cut):
                self.cut = cut
                response, _, receipt = self.attempt(self.job(cut))
                self.assertEqual(self.last_runtime.killed_at, cut)
                self.assertEqual(response["outcome"], "known" if cut == "during_retirement" else "unknown")
                attempt = self.assert_settled(receipt, "never_started" if cut == "before_boot" else "reaped")
                teardown = decode((attempt / "teardown.json").read_bytes())
                self.assertEqual(teardown["surviving_pids"], [])
                self.assertTrue(teardown["scope_gone"])

    def descriptor(self, name):
        now = time.monotonic_ns()
        return {"schema": "vp.paired_reservation/v1", "reservation_id": name, "cell_id": "offline-cell",
                "block_id": "offline-block", "host_id": self.host_id, "boot_id": self.boot_id, "design": "A/A",
                "profile_sha256": self.profile_sha, "opens_mono_ns": str(now), "closes_mono_ns": str(now + TTL),
                "draw_budget": 2, "max_concurrent": 1, "full_cap_bytes": RAM + 1024}

    def test_paired_limits_and_close(self):
        descriptor = self.descriptor("pair-limits")
        pair = descriptor["reservation_id"]
        self.client.paired_open(descriptor)
        first, second = self.job("draw-1"), self.job("draw-2")
        draw = lambda job: {"reservation_id": pair, "request": job["admission"]}
        self.client.call("paired-draw", draw(first))
        with self.assertRaisesRegex(Refusal, "^IN_FLIGHT_CAP_REACHED$"):
            self.client.call("paired-draw", draw(second))
        self.assert_settled(self.attempt(first, pair)[2])
        self.client.call("paired-draw", draw(second))
        with self.assertRaisesRegex(Refusal, "^EXHAUSTED$"):
            self.client.call("paired-draw", draw(self.job("draw-3")))
        self.assertEqual(self.client.paired_close(pair), "closing")
        self.assert_settled(self.attempt(second, pair)[2])
        self.assertEqual(self.client.paired_close(pair), "closed")
        with self.assertRaisesRegex(Refusal, "^RESERVATION_CLOSED$"):
            self.client.call("paired-draw", draw(self.job("draw-4")))

    def test_configured_owner_single_and_pair(self):
        source = b"from __main__ import offline_adapter as prepare\n"
        path = self.journal / "stand_in.py"
        put(path, source)
        config = {"mode": "TOOLS_SOCKET", "host_id": self.host_id,
                  "contract_root": os.environ["FC_BOOT_CONTRACT_ROOT"], "journal_root": str(self.journal),
                  "socket": {"path": self.endpoint, "uid": self.tools_uid, "cgroup": self.client.peer[1]},
                  "tools_signer": {"iss": "offline-tools", "kid": "tools-key",
                                   "key_path": str(self.journal / "tools.pub")},
                  "main_signer": {"iss": "offline-main", "kid": "main-key",
                                  "key_path": str(self.journal / "main.pem")},
                  "psi_policy_path": str(self.journal / "policy.json"),
                  "psi_policy_sha256": digest(self.trust["psi_policy"]), "accepted_grant_ttl_ns": [TTL],
                  "adapter": {"path": str(path), "sha256": hash_bytes(source)},
                  "single": self.job("owner-single"), "pair": {"reservation": self.descriptor("pair-owner"),
                                                              "jobs": [self.job("pair-a"), self.job("pair-b")]}}
        with self.assertRaisesRegex(ValueError, "must select TOOLS_SOCKET"):
            run_config({**config, "mode": FIXTURE_GRANT})
        with self.assertRaisesRegex(Refusal, "^INVALID_REQUEST$"):
            run_pair(self.client, self.contract, self.trust, {**config["pair"]["reservation"], "design": "ABBA"},
                     config["pair"]["jobs"], self.journal, self.adapter)
        with patch.dict(vars(sys.modules["__main__"]), {"offline_adapter": self.adapter}):
            result = run_config(config)
            with self.assertRaises(FileExistsError):
                run_config(config)
        self.assertEqual(result["pair"]["pair_state"], "closed")
        for response, number, receipt in (result["single"], *result["pair"]["attempts"]):
            self.assertEqual((response["outcome"], number), ("known", 385))
            self.assert_settled(receipt)

    def test_refusals(self):
        with self.assertRaisesRegex(Refusal, "^INVALID_REQUEST$"):
            self.client.call("claim", {})
        grant = self.client.call("grant", self.job("wrong-binding")["admission"])
        claim = {key: grant[key] for key in ("slot_id", "lease_id", "epoch", "attempt_id", "host_id", "boot_id")}
        with self.assertRaisesRegex(Refusal, "^UNAUTHORIZED$"):
            self.client.call("claim", {**claim, "boot_owner": "foreign", "create_request_sha256": "a" * 64})
        with self.assertRaisesRegex(Refusal, "^TOOLS_PEER_MISMATCH$"):
            SocketTools(self.endpoint, self.observer_uid, self.client.peer[1]).call("grant", {})
        with self.assertRaisesRegex(Refusal, "^TOOLS_PEER_MISMATCH$"):
            SocketTools(self.endpoint, self.tools_uid, "/wrong.scope").call("grant", {})
        self.assert_settled(self.attempt(self.job("wrong-binding"))[2])

    def test_wrong_uid_cgroup_and_duplicate_owner(self):
        frame = canonical({"op": "grant", "body": self.job("unprovisioned")["admission"]}) + b"\n"
        raw = [sys.executable, "-B", __file__, "--refused-peer", self.endpoint]
        subprocess.run(self.as_uid(self.observer_uid, *raw), input=frame, check=True, timeout=TIMEOUT)
        unit = f"mv-fc5-negative-{self.root.name[5:]}.scope"
        subprocess.run(self.service_scope(unit, *raw), input=frame, check=True, timeout=TIMEOUT)
        with self.assertRaisesRegex(PermissionError, "already exists"):
            launch_owner(["false"])
        with self.assertRaises(BlockingIOError):
            with owned():
                self.fail("second owner entered")


def refused_peer(path):
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(TIMEOUT)
        channel.connect(path)
        try:
            channel.sendall(sys.stdin.buffer.read())
            reply = channel.recv(1)
        except (BrokenPipeError, ConnectionResetError):
            return
        if reply:
            raise RuntimeError("unprovisioned peer received a Tools response")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--stand-in"]:
        stand_in()
    elif sys.argv[1:2] == ["--refused-peer"]:
        refused_peer(sys.argv[2])
    elif sys.argv[1:2] == ["--launch"]:
        raise SystemExit(launch_owner([sys.executable, "-B", str(Path(__file__).resolve()), "-v"]))
    else:
        unittest.main()
