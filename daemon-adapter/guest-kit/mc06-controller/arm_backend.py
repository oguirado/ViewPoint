"""Own a single Firecracker child and authenticate its fixed-job guest replies."""
import hashlib
from pathlib import Path
import os
import platform
import re
import secrets
import subprocess
import tempfile
import time


class ArmBackend:
    def __init__(self, config, record):
        # H1-ENTRY executes this module before it installs contract_guard.
        from contract_guard import pinned_module

        self.harness = pinned_module(
            "mc04_arm_harness", "mc04-runtime/arm_harness.py",
            "d1e21a6de5d538ca3cf841e2c1e908cfbc3865f389548dac0f048877c367a74e",
        )
        self.config = config
        self.record = record
        self.directory = self.process = self.pidfd = None
        self.files = {}
        self.nonce = secrets.token_hex(32)
        self.started = False

    def _check_cgroup(self, child=None):
        return self.harness.check_cgroup(self.config["external_cgroup"], child)

    def _preflight(self):
        self.harness.require(platform.machine() == "aarch64", "AArch64 host required")
        self.harness.check_kvm()
        self.harness.available_memory()
        self._check_cgroup()

    def _spawn(self):
        return subprocess.Popen(
            [str(self.files["engine"]), "--api-sock", str(self.files["api"])],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=self.directory, env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            start_new_session=True, close_fds=True,
        )

    def _deadline(self, operation):
        return time.monotonic() + self.config["deadlines"][operation + "_seconds"]

    def _write_asset(self, name, data, mode):
        with self.files[name].open("xb") as file:
            file.write(data)
            os.fchmod(file.fileno(), mode)

    def _stage(self):
        base = self.harness.runtime_root(self.config["runtime_root"])
        # Resolve AF_UNIX sun_path capacity before any child exists.
        self.harness.require(len(os.fsencode(base / "fc-xxxxxxxx" / "vsock.sock")) <= 107,
                        "runtime path too long for AF_UNIX")
        self.directory = Path(tempfile.mkdtemp(prefix="fc-", dir=base))
        self.files = {
            "engine": self.directory / "firecracker",
            "kernel": self.directory / "Image",
            "init": self.directory / "initramfs.gz",
            "api": self.directory / "api.sock",
            "vsock": self.directory / "vsock.sock",
        }
        for name, pin in self.config["artifacts"].items():
            data = self.harness.pinned_asset(pin["path"], pin["sha256"], static=name == "init",
                                        kernel=name == "kernel")
            if name == "init":
                data = self.harness.initramfs(data)
                initrd_sha256 = hashlib.sha256(data).hexdigest()
            self._write_asset(name, data, 0o500 if name == "engine" else 0o400)
            del data
        return initrd_sha256

    def _hello(self, guest, deadline):
        hello = f"HELLO {self.nonce}"
        self.harness.require(self.harness.exchange(guest, hello, deadline) == hello, "guest HELLO proof")

    def create(self):
        self.harness.require(not self.started, "create already attempted")
        self.started = True
        deadline = self._deadline("startup")
        self._preflight()
        initrd_sha256 = self._stage()
        self.harness.remaining(deadline)
        self._check_cgroup()
        self.process = self._spawn()
        self.pidfd = os.pidfd_open(self.process.pid)
        cgroup = self._check_cgroup(self.process.pid)
        self.record("firecracker_process", {
            "pid": self.process.pid, "start_ticks": self.harness.pid_identity(self.process.pid),
            "cgroup": cgroup, "initrd_sha256": initrd_sha256,
            "pins": {name: pin["sha256"] for name, pin in self.config["artifacts"].items()},
        })
        self.harness.wait_socket(self.files["api"], self.process, deadline)
        # AArch64 Firecracker does not support SMT configuration.
        for endpoint, body in (
            ("/machine-config", self.config["resources"]),
            ("/boot-source", {
                "kernel_image_path": str(self.files["kernel"]),
                "initrd_path": str(self.files["init"]),
                "boot_args": f"panic=1 rdinit=/init mv_nonce={self.nonce}",
            }),
            ("/vsock", {"guest_cid": 3, "uds_path": str(self.files["vsock"])}),
            ("/actions", {"action_type": "InstanceStart"}),
        ):
            self.harness.request(self.files["api"], endpoint, body, deadline)
        with self.harness.connect_guest(self.files["vsock"], self.process, deadline) as guest:
            self._hello(guest, deadline)
            pong = self.harness.exchange(guest, f"PING {self.nonce}", deadline)
            self.harness.require(pong == f"PONG {self.nonce}", "guest PING proof")
            uname = self.harness.exchange(guest, f"UNAME {self.nonce}", deadline)
            match = re.fullmatch(rf"UNAME {self.nonce} Linux ([!-~]+) aarch64", uname)
            self.harness.require(match is not None, "AArch64 guest UNAME proof")
        self._check_cgroup(self.process.pid)
        self.record("guest_ready", {"sysname": "Linux", "release": match[1], "isa": "aarch64"})

    def run(self, n):
        self.harness.require(type(n) is int and 0 <= n <= 10000, "SUMSQ input bound")
        deadline = self._deadline("job")
        self._check_cgroup(self.process.pid)
        with self.harness.connect_guest(self.files["vsock"], self.process, deadline) as guest:
            self._hello(guest, deadline)
            result = self.harness.exchange(guest, f"SUMSQ {self.nonce} {n}", deadline)
        expected = n * (n + 1) * (2 * n + 1) // 6
        self.harness.require(result == f"SUMSQ {self.nonce} {n} {expected}", "guest SUMSQ proof")
        return expected

    def _release(self):
        self.harness.require(self.process is None or self.process.poll() is not None, "Firecracker is live")
        self._check_cgroup()
        for path in self.files.values():
            path.unlink(missing_ok=True)
        if self.directory is not None:
            self.directory.rmdir()  # Extra entries leave native closure unproved.
            self.directory = None
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None

    def stop(self):
        deadline = self._deadline("stop")
        if self.process is None:
            outcome = "no_child"
        elif self.process.poll() is not None:
            outcome = "already_exited"
        else:
            with self.harness.connect_guest(self.files["vsock"], self.process, deadline) as guest:
                self._hello(guest, deadline)
                response = self.harness.exchange(guest, f"STOP {self.nonce}", deadline)
                self.harness.require(response == f"STOPPING {self.nonce}", "guest STOP proof")
            status = self.process.wait(timeout=self.harness.remaining(deadline))
            self.harness.require(status == 0, "Firecracker exited unsuccessfully after STOP")
            outcome = "guest_stopped"
        self._release()
        return {"closed": True, "outcome": outcome,
                "exit_status": self.process.returncode if self.process else None}

    def emergency_close(self):
        # Evidence of owner shutdown does not release a public session.
        forced = False
        if self.process is not None:
            forced = self.harness.cleanup_process(self.process, self.pidfd, self.config["deadlines"]["stop_seconds"])
        self._release()
        return {"closed": True, "outcome": "forced_on_owner_exit" if forced else "nothing_live"}
