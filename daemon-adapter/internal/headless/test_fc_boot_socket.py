"""Exercise MAIN against the pinned fcbootl-serve and a disposable runtime."""

import os
from pathlib import Path
import selectors
import secrets
import site
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key

from fc_boot import (BOOT_ID, Contract, FIXTURE_GRANT, Refusal, RuntimeFailure, canonical, decode, digest,
                     hash_bytes, sync_directory)
from fc_boot_owner import (OWNER, OutcomeUnknown, ReconciliationRequired, SocketTools, default_socket_path,
                           main, owned, run_attempt, run_config, run_pair, signing_key)
from test_fc_boot import GUEST, RuntimeFixture, ticks

GIB = 1 << 30
CGROUP_ROOT = Path("/sys/fs/cgroup")
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
    def setUpClass(cls):
        user_site = site.getusersitepackages()
        cls.root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix=".fc6-", dir=Path.home())))
        cls.journal = cls.root / "journal"
        cls.journal.mkdir(mode=0o700)
        cls.enterClassContext(patch.dict(os.environ, {
            "HOME": str(cls.root), "XDG_RUNTIME_DIR": str(cls.root / "r"),
            "XDG_STATE_HOME": str(cls.root / "state"),
        }))
        cls.endpoint = default_socket_path()
        cls.runtime = cls.endpoint.parent / "fcbootl-runtime"
        ledger = cls.root / "state/viewpoint-tools/fcbootl"
        cls.evidence = ledger / "evidence"
        cls.contract = Contract(os.environ["FC_BOOT_CONTRACT_ROOT"])
        cls.boot_id = BOOT_ID.read_text().strip()
        cls.profile = cls.contract.schemas["capsule_profile"].schema["examples"][0].copy()
        guardian = {"mode": "offline-stand-in"}
        provenance = {"fixture": "offline-credential-free"}
        cls.profile.update(priority_class="agent_foreground", guardian_policy_sha256=digest(guardian),
                           credential_provenance={"id": "stand-in-credential", "sha256": digest(provenance)})
        cls.profile_sha = digest(cls.profile)
        pin = Path(os.environ["FC_BOOT_TOOLS_ROOT"])
        revision = subprocess.check_output(["git", "-C", str(pin), "rev-parse", "HEAD"], text=True).strip()
        if revision != "f301a72feecd0c828b9f1a8dc5d007f64fe1c812":
            raise RuntimeError("wrong frozen Tools checkout")
        cls.cli = pin / "tools/vp-tools"
        cls.client = SocketTools(cls.journal, cls.contract)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        if user_site in sys.path:
            env["PYTHONPATH"] = os.pathsep.join(filter(None, (env.get("PYTHONPATH"), user_site)))
        env.pop("MV_RAM_ADMISSION_DIR", None)
        default = cls.start_server(env)
        try:
            signer_path = ledger / "fcbootl-signer.pem"
            cls.witness_path = ledger / "fcbootl-witness.pem"
            cls.assert_private_key(signer_path)
            cls.assert_private_key(cls.witness_path)
            tools_pem = signer_path.read_bytes()
            witness_pem = cls.witness_path.read_bytes()
            tools_key, witness_key = (load_pem_private_key(data, password=None) for data in (tools_pem, witness_pem))
            tools_pub = tools_key.public_key()
            tools_kid, witness_kid = (hash_bytes(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
                                      for key in (tools_key, witness_key))
            cls.host_id = "local-" + tools_kid
            try:
                cls.client.call("grant", cls.job("zero-config")["admission"])
            except Refusal as error:
                if str(error) != "GRANT_UNAVAILABLE":
                    raise
            else:
                raise AssertionError("unprovisioned admission issued a grant")
        finally:
            cls.stop_server(default)
        ram = ledger / "ram-admission"
        ram_info = ram.stat()
        paths = {"firecracker": str(ram), **dict.fromkeys(("docker", "vm", "host_lane", "legacy_arm"), "disabled")}
        cutover = {"schema": "vp.fc_admission_cutover/v1", "host_id": cls.host_id, "boot_id": cls.boot_id,
                   "ledger_root": str(ledger), "ram_device": str(ram_info.st_dev), "ram_inode": str(ram_info.st_ino),
                   "admission_paths": paths, "wholegate_bytes": 0}
        for name, value in (("guardian", guardian), ("provenance", provenance), ("cutover", cutover)):
            put(cls.root / (name + ".json"), canonical(value))
        policy = {"version": "offline-test", "mode": "shadow", "sampler": "linux-proc-pressure-avg10/v1",
                  "max_age_ns": 1_000_000_000, "thresholds_bps": {"cpu_some_avg10_bps": 10000,
                    "memory_full_avg10_bps": 10000, "io_full_avg10_bps": 10000}}
        main_pin, signer = signing_key(OWNER, witness_kid, witness_key)
        cls.trust = {"host_id": cls.host_id, "tools_key": ("viewpoint-tools", tools_kid, tools_pub),
                     "main_key": main_pin, "sign_witness": signer, "psi_policy": policy, "ttl_ns": (TTL,)}
        put(cls.journal / "tools.pub", tools_pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
        put(cls.journal / "policy.json", canonical(policy))
        config = {
            "cutover_path": str(cls.root / "cutover.json"), "cutover_sha256": digest(cutover),
            "grant_ttl_ns": TTL, "max_pair_window_ns": TTL, "floor_bytes": 20 * GIB, "psi_policy": policy,
            "profiles": {cls.profile_sha: {
                "guardian_path": str(cls.root / "guardian.json"), "guardian_sha256": digest(guardian),
                "outer_scope_cap_bytes": RAM, "overhead_bytes": 1024,
                "credential_provenance": cls.profile["credential_provenance"],
                "provenance_path": str(cls.root / "provenance.json"),
            }},
        }
        config_file = cls.root / "service.json"
        put(config_file, canonical(config))
        server = cls.start_server(dict(env, MV_RAM_ADMISSION_DIR=str(ram)), "--config", str(config_file))
        cls.addClassCleanup(cls.stop_server, server)
        if signer_path.read_bytes() != tools_pem or cls.witness_path.read_bytes() != witness_pem:
            raise RuntimeError("Tools regenerated the zero-config keys")

    @staticmethod
    def assert_private_key(path):
        if path.stat().st_mode & 0o777 != 0o600:
            raise RuntimeError("Tools key permissions differ from the handoff")

    @classmethod
    def start_server(cls, env, *args):
        server = subprocess.Popen([str(cls.cli), "fcbootl-serve", *args], env=env)
        deadline = time.monotonic() + TIMEOUT
        while not cls.endpoint.is_socket():
            if server.poll() is not None or time.monotonic() >= deadline:
                cls.stop_server(server)
                raise RuntimeError("offline Tools service did not start")
            time.sleep(0.02)
        return server

    @staticmethod
    def stop_server(server):
        if server.poll() is None:
            server.terminate()
        server.wait(timeout=TIMEOUT)

    @classmethod
    def job(cls, name):
        return {"admission": {"schema": "vp.admission_request/v1", "request_id": name, "host_id": cls.host_id,
                              "boot_id": cls.boot_id, "principal_id": OWNER, "backend": "firecracker",
                              "profile": cls.profile, "profile_sha256": cls.profile_sha,
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
            self.assertTrue(all(not os.path.lexists(value[field]) for field in ("scope_path", "runtime_dir")))
            self.assertTrue(all(not Path(f"/proc/{member['pid']}").exists() for member in proof.get("members", [])))
            put(leaf, canonical(value), mode=0o444)
            sync_directory(leaf.parent)
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
        self.assertTrue(grant["receipt_ref"]["id"].startswith("same_user:"))
        self.assertTrue(receipt["receipt_id"].startswith("same_user:"))
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

        def drop_first_response(frame, request):
            operation = request["op"]
            self.assertRegex(request["transaction_id"], r"^[0-9a-f]{32}$")
            self.assertEqual(self.client._load(), request)
            reply = original(frame, request)
            if operation in {"grant", "claim", "release"}:
                sent.setdefault(operation, []).append(frame)
                if operation not in lost:
                    lost[operation] = reply
                    if operation == "grant":
                        raise OutcomeUnknown(request["transaction_id"])
                    if operation == "claim":
                        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                            root = Path(tmp)
                            rival = SocketTools(root, self.contract)
                            with self.assertRaisesRegex(Refusal, "^GRANT_UNAVAILABLE$"):
                                run_attempt(rival, self.contract, self.trust, job, root,
                                            lambda *_: self.fail("an earlier claim authorized a second launch"))
                    return OutcomeUnknown(request["transaction_id"]).reply
                self.assertEqual(lost[operation], reply)
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
                  "tools_signer": {"iss": "viewpoint-tools", "kid": self.trust["tools_key"][1],
                                   "key_path": str(self.journal / "tools.pub")},
                  "main_signer": {"iss": OWNER, "kid": self.trust["main_key"][1],
                                  "key_path": str(self.witness_path)},
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
        self.assert_settled(self.attempt(self.job("wrong-binding"))[2])

    def test_peer_and_owner_lock(self):
        foreign = self.root / "foreign.sock"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(foreign))
            server.listen()
            client = SocketTools(self.journal, self.contract, foreign)
            with patch("fc_boot_owner.os.getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(PermissionError, "^TOOLS_PEER_MISMATCH$"):
                    client.call("grant", {})
            with server.accept()[0] as accepted:
                self.assertEqual(accepted.recv(1), b"")
        self.assertFalse(self.client.pending.exists())
        with owned(self.journal), self.assertRaises(BlockingIOError):
            with owned(self.journal):
                self.fail("second owner entered")

    def test_default_socket_falls_back_to_the_state_directory(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}):
            self.assertEqual(default_socket_path(), self.root / "state/viewpoint-tools/fcbootl/fcbootl.sock")
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "", "XDG_STATE_HOME": ""}):
            self.assertEqual(default_socket_path(), self.root / ".local/state/viewpoint-tools/fcbootl/fcbootl.sock")

    def test_unknown_survives_restart_and_superseded_claim_fences_journal(self):
        job = self.job("restart")
        sent = []
        original = self.client._exchange

        def lose_reply(frame, request):
            sent.append(frame)
            original(frame, request)
            return OutcomeUnknown(request["transaction_id"]).reply

        with patch.object(self.client, "_exchange", side_effect=lose_reply), patch(
                "fc_boot_owner.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(OutcomeUnknown) as interrupted:
                self.client.call("grant", job["admission"])
        resumed = SocketTools(self.journal, self.contract)
        self.assertEqual(resumed._load()["transaction_id"], interrupted.exception.reply["transaction_id"])
        with self.assertRaises(OutcomeUnknown):
            resumed.call("claim", {})
        with patch.object(resumed, "_exchange", wraps=resumed._exchange) as exchange:
            grant = resumed.replay_pending()
        self.assertEqual(exchange.call_args.args[0], sent[0])
        self.assertEqual(grant["admission_request_id"], job["admission"]["request_id"])
        self.assertFalse(resumed.pending.exists())

        frames = []

        def capture_claim(frame, request):
            if request["op"] == "claim":
                frames.append((frame, request))
            return original(frame, request)

        with patch.object(self.client, "_exchange", side_effect=capture_claim):
            receipt = self.attempt(job)[2]
        self.assert_settled(receipt)
        _, claim = frames[0]
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            recovery = SocketTools(Path(tmp), self.contract)
            recovery._persist(claim)
            with self.assertRaisesRegex(ReconciliationRequired, "^REPLAY_SUPERSEDED$"):
                recovery.replay_pending()
            self.assertEqual(recovery._load(), claim)
            with self.assertRaises(OutcomeUnknown):
                recovery.call("grant", job["admission"])

    def test_client_exit_codes(self):
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            root = Path(tmp)
            config = root / "main.json"
            base = {"mode": "TOOLS_SOCKET", "contract_root": os.environ["FC_BOOT_CONTRACT_ROOT"],
                    "journal_root": str(root)}
            put(config, canonical(base))
            argv = ["--inside-owner", "--config", str(config), "--replay-pending"]
            client = SocketTools(root, self.contract)
            body = self.job("cli")["admission"]
            with patch("builtins.print"):
                self.assertEqual(main(argv), 3)
                client._persist({"op": "claim", "body": {}, "transaction_id": secrets.token_hex(16)})
                self.assertEqual(main(argv), 1)
                client._persist({"op": "grant", "body": body, "transaction_id": secrets.token_hex(16)})
                with patch.object(SocketTools, "_exchange", side_effect=KeyboardInterrupt):
                    self.assertEqual(main(argv), 4)
                self.assertEqual(main(argv), 0)
                self.assertFalse(client.pending.exists())


if __name__ == "__main__":
    if sys.argv[1:2] == ["--stand-in"]:
        stand_in()
    else:
        unittest.main()
