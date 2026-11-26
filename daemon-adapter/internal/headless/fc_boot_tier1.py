"""Rootless owner for the digest-pinned R7 fixed-job Firecracker cell."""

from contextlib import ExitStack, contextmanager
import fcntl
import grp
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import time

from fc_boot import (BOOT_ID, IsolationUnverified, RuntimeFailure, UnsupportedProfile, canonical,
                     decode, digest, hash_bytes, sync_directory, write_record)


CELL_IMAGE = {
    "name": "guest-command-arm64-v1",
    "runtime_sha256": "6f9e08df0a0072e3e7086c51ff434c034d705845e349175a01beb3f9cc025ce3",
    "kernel_sha256": "398889caadb3cb768911db845638edb7e46223ad36f76f8e05790a520588577d",
    "guest_init_sha256": "e72da722f5c4fe12b8c620d3482016dc04e869a72dff56cce4d04b378fe1f1e6",
    "image_sha256": "2c9605ec9ce7aa5be24ea1e3c97f329115db2a2bfa0c800764272983f1838980",
    "config_sha256": "c36113e9927ced5328dccbd35242f7623e7829b4f88fd1acce7a1b70c2c3b4b0",
}
LIMIT_FILES = ("memory.high", "memory.max", "memory.swap.max", "cpu.max", "pids.max", "cpuset.cpus")
CGROUP = Path("/sys/fs/cgroup")
PROC = Path("/proc")
KVM = Path("/dev/kvm")
APPS = {"firecracker": "/assets/firecracker", "Image": "/assets/Image", "initrd": "/assets/initrd"}
NEVER_STARTED = {"kind": "never_started", "launch_fenced": True, "spawn_count": 0,
                 "runtime_inventory_absent": True, "sockets_absent": True}
STARTUP, JOB, STOP = 15, 5, 10


def require(condition, message):
    if not condition:
        raise IsolationUnverified(message)


def read_pin(path, expected, limit=128 << 20):
    path = Path(path)
    require(path.is_absolute() and path.resolve(strict=True) == path, "Artifact path is not canonical")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid())
                and not info.st_mode & 0o022 and 0 < info.st_size <= limit, "Unsafe artifact pin")
        data = stream.read(limit + 1)
        require(len(data) == info.st_size and hash_bytes(data) == expected, "Artifact digest differs")
    return data


def arm_harness():
    source = Path(__file__).resolve().parents[2] / "guest-kit/mc04-runtime/arm_harness.py"
    spec = importlib.util.spec_from_file_location("fc_boot_arm_harness", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Tier1Policy:
    """Private owner policy. No job can supply paths or select its own tier."""

    def __init__(self, value):
        self.value = value
        self.harness = arm_harness()
        self.held = None

    def resolve(self, profile_sha256, profile):
        entry = self.value["profiles"].get(profile_sha256)
        if (digest(profile) != profile_sha256 or not isinstance(entry, dict)
                or type(entry.get("tier")) is not int or entry["tier"] != 1):
            raise UnsupportedProfile("The signed profile digest has no Tier 1 approval")
        if ("tier" in profile or profile.get("trust_class") != "operator_owned"
                or any(profile.get(field) is not False for field in ("untrusted_input", "web_input", "speculative"))):
            raise UnsupportedProfile("The profile requires a separately qualified launch tier")
        resources = profile["resources"]
        if (profile["class"], profile["backend"], profile["required_isolation"], profile["network_policy"],
                resources.get("guest_memory_mib"), resources["vcpu"], resources["memory_max_bytes"],
                resources["pids_max"], resources["swap_max_bytes"], resources["disk_bytes"]) != (
                "headless", "firecracker", "vm", {"mode": "none"}, 256, 1, 512 << 20, 64, 0, 0):
            raise UnsupportedProfile("The profile does not match the fixed R7 cell")
        if profile["image"] != CELL_IMAGE or entry.get("image") != CELL_IMAGE:
            raise UnsupportedProfile("The approved R7 artifact digests differ from the profile")
        if profile["guardian_policy_sha256"] != self.value["guardian"]["sha256"]:
            raise UnsupportedProfile("The profile does not pin the provisioned guardian")
        return entry

    def authorize(self, profile_sha256, profile):
        entry = self.resolve(profile_sha256, profile)
        if self.held is None:
            try:
                pins = entry["artifacts"]
                engine = read_pin(pins["firecracker"], CELL_IMAGE["runtime_sha256"])
                kernel = read_pin(pins["kernel"], CELL_IMAGE["kernel_sha256"])
                init = read_pin(pins["init"], CELL_IMAGE["guest_init_sha256"])
                initrd = self.harness.initramfs(init)
                require(hash_bytes(initrd) == CELL_IMAGE["image_sha256"], "Derived boot image digest differs")
                read_pin(pins["config"], CELL_IMAGE["config_sha256"], 1 << 20)
                guardian = self.value["guardian"]
                policy = decode(read_pin(guardian["path"], guardian["file_sha256"], 1 << 20))
                # The registered R7 profile pins the raw file. Earlier profiles
                # bind its canonical JSON digest.
                require(guardian["sha256"] in {guardian["file_sha256"], digest(policy)},
                        "Guardian policy digest differs")
                sandbox = self.value["sandbox"]
                executable = read_pin(sandbox["path"], sandbox["sha256"], 8 << 20)
            except (OSError, KeyError, ValueError) as error:
                raise UnsupportedProfile("The provisioned artifact bytes do not match their approved pins") from error
            self.held = profile_sha256, executable, {"firecracker": engine, "Image": kernel, "initrd": initrd}
        if self.held[0] != profile_sha256:
            raise UnsupportedProfile("The boot owner is pinned to one resolved cell profile")

    def prepare(self, job, grant, journal_root):
        profile = job["admission"]["profile"]
        if grant["profile_sha256"] != job["admission"]["profile_sha256"]:
            raise UnsupportedProfile("The Tools grant changed the selected profile")
        self.authorize(grant["profile_sha256"], profile)
        runtime = NativeTier1(self, grant, journal_root / grant["attempt_id"])
        return runtime, runtime.observe


def identity(path):
    info = path.lstat()
    return (info.st_dev, info.st_ino)


def directory(path, private=False):
    require(path.is_absolute() and path.resolve(strict=True) == path, "Noncanonical custody directory")
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and (not private or stat.S_IMODE(info.st_mode) == 0o700), "Unsafe custody directory")
    return info.st_dev, info.st_ino


def mount_table(path):
    mounts = {}
    for line in path.read_text().splitlines():
        metadata, filesystem = line.split(" - ", 1)
        fields = metadata.split()
        name = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        mounts[name] = (set(fields[5].split(",")), filesystem.split()[0])
    return mounts


def filesystem(path):
    matches = [(len(mount), info[1]) for mount, info in mount_table(PROC / "self/mountinfo").items()
               if Path(mount) == path or Path(mount) in path.parents]
    return max(matches)[1]


def verify_mounts(base, workspace_id, mount_ns):
    require((base / "ns/mnt").stat().st_ino == mount_ns
            and all((base / "ns" / name).stat().st_ino != (PROC / "self/ns" / name).stat().st_ino
                    for name in ("mnt", "net", "pid", "user")),
            "The VMM did not enter its required private namespaces")
    mounts = mount_table(base / "mountinfo")
    require(set(mounts) == {"/", "/workspace", "/dev/kvm", *APPS.values()}
            and mounts["/"][1] == "tmpfs"
            and all("ro" in mounts[path][0] for path in {"/", *APPS.values()})
            and mounts["/workspace"][1] == "tmpfs" and "rw" in mounts["/workspace"][0],
            "The VMM has an unexpected mount or writable artifact")
    require(identity(base / "root/workspace") == workspace_id
            and (base / "root/dev/kvm").stat().st_rdev == KVM.stat().st_rdev,
            "The VMM mounts do not bind the owned workspace and KVM device")
    verify_vmm_executable(base)


def verify_vmm_executable(base):
    # Bubblewrap unlinks the staged backing path after binding it.
    actual = (base / "exe").stat()
    expected = (base / "root" / APPS["firecracker"].lstrip("/")).stat()
    require((actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino), "Unexpected VMM executable")


def status(path):
    return {key: value.split() for key, _, value in (line.partition(":") for line in path.read_text().splitlines())}


def verify_credentials(values, uid, gid, groups):
    require(uid != 0 and values["Uid"] == [str(uid)] * 4 and values["Gid"] == [str(gid)] * 4
            and set(map(int, values["Groups"])) == groups
            and all(int(values[name][0], 16) == 0 for name in ("CapInh", "CapPrm", "CapEff", "CapAmb")),
            "Unexpected UID, GID, supplementary groups or capabilities")


def read_caps(path, expected, *, effective_cpus=None):
    for name in LIMIT_FILES:
        observed = (path / name).read_text().strip()
        require(observed == expected[name], f"Unexpected {name} at {path.name}")
    if effective_cpus is None:
        effective_cpus = expected["cpuset.cpus"]
    require(bool(effective_cpus)
            and (path / "cpuset.cpus.effective").read_text().strip() == effective_cpus
            and bool((path / "cpuset.mems.effective").read_text().strip()), "Effective cpuset differs")


def set_caps(path, caps):
    require(set(caps) == set(LIMIT_FILES)
            and all(isinstance(value, str) and value and "max" not in value.split() for value in caps.values()),
            "The leaf needs explicit finite caps")
    for name in LIMIT_FILES:
        (path / name).write_text(caps[name] + "\n")
    read_caps(path, caps)


def verify_ancestor_caps(leaf, caps):
    for ancestor in leaf.parents:
        if ancestor == CGROUP:
            break
        for name in ("memory.high", "memory.max", "memory.swap.max", "pids.max"):
            limit = (ancestor / name).read_text().strip()
            require(limit == "max" or int(limit) >= int(caps[name]), f"An ancestor reduces {name}")
        quota, period = (ancestor / "cpu.max").read_text().split()
        expected_quota, expected_period = map(int, caps["cpu.max"].split())
        require(quota == "max" or int(quota) * expected_period >= expected_quota * int(period),
                "An ancestor reduces cpu.max")


@contextmanager
def sealed(name, data):
    with os.fdopen(os.memfd_create(name, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING), "w+b") as file:
        file.write(data)
        file.flush()
        file.seek(0)
        os.fchmod(file.fileno(), 0o500)
        fcntl.fcntl(file, fcntl.F_ADD_SEALS, fcntl.F_SEAL_SEAL | fcntl.F_SEAL_WRITE
                    | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK)
        yield file.fileno()


def sandbox_args(workspace, assets, info, uid, gid, program=APPS["firecracker"]):
    # A minimal private root exposes one writable directory and the KVM device.
    args = ["--unshare-user", "--disable-userns", "--unshare-pid", "--as-pid-1",
            "--unshare-net", "--unshare-ipc", "--unshare-uts", "--cap-drop", "ALL",
            "--uid", str(uid), "--gid", str(gid), "--new-session", "--clearenv",
            "--setenv", "LANG", "C",
            "--dir", "/dev", "--dev-bind", str(KVM), "/dev/kvm",
            "--bind", str(workspace), "/workspace", "--chdir", "/workspace",
            "--json-status-fd", str(info)]
    for name, fd in assets.items():
        args += ["--perms", "0500" if name == "firecracker" else "0400",
                 "--ro-bind-data", str(fd), APPS[name]]
    return [*args, "--remount-ro", "/", "--", program]


# MAIN records the direct child before this bootstrap can move or exec.
ATTACH = '''
import os, sys
cgroup, executable, start = map(int, sys.argv[1:4])
if os.read(start, 1) != b"1":
    sys.exit(125)
os.close(start)
os.write(cgroup, str(os.getpid()).encode())
os.close(cgroup)
os.umask(0o077)
os.set_inheritable(executable, False)
os.execve(executable, ["bwrap", *sys.argv[4:]], {"LANG": "C", "PATH": "/usr/bin:/bin"})
'''


class NativeTier1:
    def __init__(self, policy, grant, journal):
        self.policy = policy
        value = policy.value
        self.harness = policy.harness
        self.grant = grant
        self.journal = journal
        self.root = Path(value["cgroup_root"])
        self.runtime_root = Path(value["runtime_root"])
        self.evidence = Path(value["evidence_root"])
        self.name = "fc-" + digest([grant["boot_id"], grant["attempt_id"], grant["lease_id"]])[:32]
        self.scope, self.workspace = self.root / self.name, self.runtime_root / self.name
        self.api, self.vsock = (self.workspace / name for name in ("api.sock", "vsock.sock"))
        self.scope_id = self.workspace_id = self.process = self.status_stream = None
        self.status_buffer = bytearray()
        self.supervisor = self.runtime = None
        self.pidfds = {}
        self.spawn_attempted = self.boot_requested = self.fenced = False
        self.bootstrap_released = False
        self.api_seen = self.vsock_requested = self.vsock_seen = False
        self.nonce = secrets.token_hex(32)
        self.uid, self.gid = os.getuid(), os.getgid()

    def before_boot(self):
        try:
            self._before_boot()
        except (OSError, KeyError, ValueError) as error:
            if isinstance(error, IsolationUnverified):
                raise
            raise IsolationUnverified("Tier 1 preflight could not be proved") from error

    def _before_boot(self):
        value = self.policy.value
        require(os.uname().machine == "aarch64", "The fixed cell requires ARM64")
        require(BOOT_ID.read_text().strip() == self.grant["boot_id"], "Host boot changed")
        owner_group = (PROC / "self/cgroup").read_text().strip().removeprefix("0::/")
        owner_scope = CGROUP / owner_group
        require("newton-ia.slice" in owner_scope.parts
                and re.fullmatch(r"mv-fcbootl-[0-9a-f]{16}\.scope", owner_scope.name)
                and (owner_scope / "memory.high").read_text().strip() == str(6 << 30)
                and (owner_scope / "memory.max").read_text().strip() == str(8 << 30)
                and os.sched_getaffinity(0) == set(range(16))
                and not {"DISPLAY", "WAYLAND_DISPLAY"} & set(os.environ),
                "The boot owner scope is unqualified")
        kvm_gid = grp.getgrnam("kvm").gr_gid
        primary = pwd.getpwuid(self.uid).pw_gid
        self.groups = set(os.getgroups())
        require(self.gid in {primary, kvm_gid} and self.groups <= {primary, kvm_gid},
                "Expected the KVM-only group set")
        parent_status = status(PROC / "self/status")
        verify_credentials(parent_status, self.uid, self.gid, self.groups)
        self.baseline = int(parent_status["Seccomp_filters"][0])
        # A device ACL can grant KVM access after all supplementary groups are dropped.
        self.harness.check_kvm()
        self.cgroup_root_id = directory(self.root)
        self.runtime_root_id = directory(self.runtime_root, private=True)
        require(filesystem(self.root) == "cgroup2" and filesystem(self.runtime_root) == "tmpfs",
                "Expected a real cgroup hierarchy and a RAM runtime root")
        home = CGROUP / f"user.slice/user-{self.uid}.slice/user@{self.uid}.service"
        guardian = Path(value["guardian"]["cgroup"])
        require(home in guardian.parents and (guardian == self.root or guardian in self.root.parents),
                "The registered cgroup root lacks qualified user guardian ancestry")
        directory(guardian)
        read_caps(guardian, value["guardian"]["caps"],
                  effective_cpus=value["guardian"].get("cpuset_cpus_effective"))
        require({"cpuset", "cpu", "memory", "pids"} <= set((self.root / "cgroup.subtree_control").read_text().split()),
                "The direct-leaf root lacks delegated controllers")
        require(max(len(os.fsencode(path)) for path in (self.api, self.vsock)) <= 107, "Unix socket path is too long")
        require(not os.path.lexists(self.workspace) and not os.path.lexists(self.scope), "Attempt leaf already exists")
        self.scope.mkdir(mode=0o700)
        self.scope_id = identity(self.scope)
        caps = value["leaf_caps"]
        require(caps["memory.max"] == str(512 << 20) and caps["pids.max"] == "64"
                and caps["memory.swap.max"] == "0", "The leaf caps differ from the matched cell")
        set_caps(self.scope, caps)
        verify_ancestor_caps(self.scope, caps)
        require(self.members() == set(), "New leaf contains processes")
        self.workspace.mkdir(mode=0o700)
        self.workspace_id = directory(self.workspace, private=True)
        write_record(self.journal / "native-reservation.json", {
            "invocation_id": self.name, "boot_id": self.grant["boot_id"], "scope": str(self.scope),
            "scope_identity": list(map(str, self.scope_id)), "runtime_dir": str(self.workspace),
            "runtime_identity": list(map(str, self.workspace_id)), "sockets": [str(self.api), str(self.vsock)],
        })

    def members(self):
        require(self.scope_id == identity(self.scope) and self.cgroup_root_id == identity(self.root),
                "The owned cgroup changed identity")
        require(not any(item.is_dir() for item in self.scope.iterdir()), "Unexpected cgroup descendants")
        return set(map(int, (self.scope / "cgroup.procs").read_text().split()))

    def track(self, pid, role, *, in_leaf=True):
        fd = os.pidfd_open(pid)
        try:
            ticks = self.harness.pid_identity(pid)
            if in_leaf:
                require(pid in self.members(), "The process is outside its owned leaf")
                require((PROC / str(pid) / "cgroup").read_text().strip() ==
                        "0::/" + str(self.scope.relative_to(CGROUP)), "The process cgroup differs")
        except BaseException:
            os.close(fd)
            raise
        self.pidfds[pid] = fd
        return {"pid": pid, "role": role, "start_ticks": str(ticks)}

    def read_status(self, deadline):
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "Sandbox status timed out")
            end = self.status_buffer.find(b"\n") + 1
            if end:
                line = bytes(self.status_buffer[:end])
                del self.status_buffer[:end]
                break
            capacity = 4096 - len(self.status_buffer)
            require(capacity > 0, "Sandbox status exceeds the frame limit")
            readable, _, _ = select.select([self.status_stream], [], [], remaining)
            require(bool(readable), "Sandbox status timed out")
            try:
                chunk = os.read(self.status_stream.fileno(), capacity)
            except BlockingIOError:
                continue
            require(bool(chunk), "Sandbox status is missing")
            self.status_buffer.extend(chunk)
        record = decode(line)
        require(isinstance(record, dict), "Sandbox status is not an object")
        return record

    def _spawn(self, deadline):
        require(not self.fenced and not self.spawn_attempted, "The launch fence is closed")
        require(self.members() == set(), "The cgroup is no longer empty")
        read_caps(self.scope, self.policy.value["leaf_caps"])
        require(identity(self.workspace) == self.workspace_id, "The RAM workspace changed identity")
        _, sandbox, held = self.policy.held
        with ExitStack() as stack:
            executable = stack.enter_context(sealed("sandbox", sandbox))
            sandbox_info = os.fstat(executable)
            self.sandbox_id = sandbox_info.st_dev, sandbox_info.st_ino
            files = {name: stack.enter_context(sealed(name, data)) for name, data in held.items()}
            reader, writer = os.pipe2(os.O_CLOEXEC)
            stack.callback(os.close, writer)
            self.status_stream = os.fdopen(reader, "rb", buffering=0)
            os.set_blocking(reader, False)
            attach = os.open(self.scope / "cgroup.procs", os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            stack.callback(os.close, attach)
            start_read, start_write = os.pipe2(os.O_CLOEXEC)
            stack.callback(os.close, start_read)
            stack.callback(os.close, start_write)
            console = stack.enter_context((self.workspace / "console").open("xb"))
            stdin = stack.enter_context(open(os.devnull, "rb"))
            self.console_id = os.fstat(console.fileno()).st_dev, os.fstat(console.fileno()).st_ino
            command = [sys.executable, "-I", "-B", "-c", ATTACH, str(attach), str(executable), str(start_read),
                       *sandbox_args(self.workspace, files, writer, self.uid, self.gid),
                       "--api-sock", "/workspace/api.sock"]
            self.spawn_attempted = True
            self.process = subprocess.Popen(command, stdin=stdin, stdout=console, stderr=console,
                                            close_fds=True, pass_fds=(attach, executable, start_read, writer, *files.values()),
                                            cwd=self.workspace, env={"LANG": "C"})
            self.supervisor = self.track(self.process.pid, "supervisor", in_leaf=False)
            write_record(self.journal / "native-spawn.json", self.supervisor)
            self.bootstrap_released = True
            os.write(start_write, b"1")
        info = self.read_status(deadline)
        require(type(info.get("child-pid")) is int and 1 < info["child-pid"] <= 0x7fffffff
                and info["child-pid"] != self.process.pid,
                "Sandbox did not report a distinct supervised VMM")
        self.runtime = self.track(info["child-pid"], "runtime")
        self.mount_ns = info["mnt-namespace"]
        write_record(self.journal / "native-roster.json", {"members": [self.supervisor, self.runtime]})

    def check_vmm(self, *, before_instance=False):
        pid = self.runtime["pid"]
        base = PROC / str(pid)
        require(self.process.poll() is None and self.members() == {pid, self.process.pid}
                and self.harness.pid_identity(pid) == int(self.runtime["start_ticks"]),
                "Incomplete live custody roster")
        read_caps(self.scope, self.policy.value["leaf_caps"])
        verify_ancestor_caps(self.scope, self.policy.value["leaf_caps"])
        verify_mounts(base, self.workspace_id, self.mount_ns)
        # The pinned Bubblewrap launch has no filter input or preload. Confirm
        # its retained process still has exactly the inherited host stack.
        supervisor = PROC / str(self.supervisor["pid"])
        sandbox = (supervisor / "exe").stat()
        require((sandbox.st_dev, sandbox.st_ino) == self.sandbox_id
                and self.harness.pid_identity(self.supervisor["pid"]) == int(self.supervisor["start_ticks"]),
                "The supervisor does not match the pinned sandbox")
        inherited = status(supervisor / "status")
        require(int(inherited["Seccomp_filters"][0]) == self.baseline
                and inherited["Seccomp"] == (["2"] if self.baseline else ["0"]),
                "An unexpected filter preceded the pinned Firecracker executable")
        verified_threads = {}
        thread_names = set()
        for task in (base / "task").iterdir():
            values = status(task / "status")
            verify_credentials(values, self.uid, self.gid, self.groups)
            filters = int(values["Seccomp_filters"][0])
            if before_instance and task.name == str(pid):
                # The VMM main thread installs its filter at InstanceStart.
                has_expected_filter = filters == self.baseline and values["Seccomp"] == inherited["Seccomp"]
            else:
                has_expected_filter = values["Seccomp"] == ["2"] and filters == self.baseline + 1
            require(has_expected_filter,
                    f"The pinned Firecracker default seccomp filter was not applied to {' '.join(values['Name'])}")
            require(values["NoNewPrivs"] == ["1"] and int(values["CapBnd"][0], 16) == 0,
                    "The VMM did not retain no-new-privileges and an empty capability bounding set")
            verified_threads[task.name] = filters
            thread_names.add(" ".join(values["Name"]))
        expected_names = {"firecracker", "fc_api"} if before_instance else {"firecracker", "fc_api", "fc_vcpu 0"}
        require(thread_names == expected_names and len(verified_threads) == len(expected_names),
                "The thread inventory does not match the pinned Firecracker cell")
        for fd in (base / "fd").iterdir():
            try:
                flags = int(status(base / "fdinfo" / fd.name)["flags"][0], 8)
                if flags & os.O_ACCMODE == os.O_RDONLY:
                    continue
                info = fd.stat()
                target = str(fd.readlink())
            except FileNotFoundError:
                continue
            allowed = ((info.st_dev, info.st_ino) == self.console_id
                       or stat.S_ISSOCK(info.st_mode)
                       or stat.S_ISCHR(info.st_mode) and info.st_rdev == KVM.stat().st_rdev
                       or target.startswith("anon_inode:") or target.startswith("anon_inode["))
            require(allowed, "An unexpected writable descriptor reached the VMM")
        return {"seccomp_filters": verified_threads, "host_filter_baseline": self.baseline,
                "sandbox_filter_baseline": int(inherited["Seccomp_filters"][0]),
                "mount_namespace": str(self.mount_ns), "scope": str(self.scope),
                "scope_identity": list(map(str, self.scope_id)), "members": [self.supervisor, self.runtime],
                "limits": self.policy.value["leaf_caps"], "uid": self.uid, "gid": self.gid,
                "groups": sorted(self.groups)}

    @contextmanager
    def peer(self, path, deadline):
        sock_info = path.lstat()
        require(identity(self.workspace) == self.workspace_id and stat.S_ISSOCK(sock_info.st_mode)
                and sock_info.st_uid == self.uid and not sock_info.st_mode & 0o077, "Unsafe private socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(self.harness.remaining(deadline))
            channel.connect(str(path))
            creds = channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            require(struct.unpack("3i", creds) == (self.runtime["pid"], self.uid, self.gid)
                    and self.harness.pid_identity(self.runtime["pid"]) == int(self.runtime["start_ticks"]),
                    "Socket peer does not match the retained VMM identity")
            yield channel

    def configure(self, endpoint, body, deadline):
        payload = json.dumps(body, separators=(",", ":")).encode()
        header = (f"PUT {endpoint} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                  f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode()
        with self.peer(self.api, deadline) as channel:
            channel.sendall(header + payload)
            response = bytearray()
            while not response.endswith(b"\r\n\r\n") and len(response) < 8192:
                channel.settimeout(self.harness.remaining(deadline))
                chunk = channel.recv(1)
                require(bool(chunk), "The Firecracker API closed mid-response")
                response += chunk
            require(response.endswith(b"\r\n\r\n") and re.fullmatch(rb"HTTP/1\.[01] 204(?: [^\r\n]*)?",
                    response.split(b"\r\n", 1)[0]) is not None, "The Firecracker API refused configuration")

    @contextmanager
    def guest(self, deadline):
        with self.peer(self.vsock, deadline) as channel:
            ack = self.harness.exchange(channel, f"CONNECT {self.harness.PORT}", deadline)
            require(re.fullmatch(r"OK [1-9][0-9]{0,9}", ack) and int(ack[3:]) <= 0xffffffff,
                    "The vsock connection was not acknowledged")
            hello = f"HELLO {self.nonce}"
            require(self.harness.exchange(channel, hello, deadline) == hello, "Guest identity proof failed")
            yield channel

    def launch(self, profile, stage):
        try:
            self._launch(profile, stage)
        except (KeyError, OSError, RuntimeError, ValueError) as error:
            if isinstance(error, IsolationUnverified):
                raise
            raise IsolationUnverified("The Tier 1 launch could not be verified") from error

    def _launch(self, profile, stage):
        deadline = time.monotonic() + STARTUP
        self._spawn(deadline)
        stage("vmm_exec")
        self.harness.wait_socket(self.api, self.process, deadline)
        self.api_seen = True
        self.check_vmm(before_instance=True)
        stage("api_ready")
        for endpoint, body in (
            ("/machine-config", {"mem_size_mib": 256, "vcpu_count": 1}),
            ("/boot-source", {"kernel_image_path": APPS["Image"], "initrd_path": APPS["initrd"],
                              "boot_args": f"panic=1 rdinit=/init mv_nonce={self.nonce}"}),
            ("/vsock", {"guest_cid": 3, "uds_path": "/workspace/vsock.sock"}),
        ):
            if endpoint == "/vsock":
                self.vsock_requested = True
            self.configure(endpoint, body, deadline)
        require(time.monotonic_ns() < int(self.grant["expires_mono_ns"]), "Grant expired before InstanceStart")
        self.boot_requested = True
        self.configure("/actions", {"action_type": "InstanceStart"}, deadline)
        while True:
            try:
                with self.guest(min(deadline, time.monotonic() + 0.5)) as channel:
                    self.vsock_seen = True
                    pong = self.harness.exchange(channel, f"PING {self.nonce}", deadline)
                    require(pong == f"PONG {self.nonce}", "Guest readiness mismatch")
                    uname = self.harness.exchange(channel, f"UNAME {self.nonce}", deadline)
                    require(re.fullmatch(rf"UNAME {self.nonce} Linux [!-~]+ aarch64", uname) is not None,
                            "Guest architecture identity differs")
                    break
            except (ConnectionError, FileNotFoundError, TimeoutError, RuntimeError):
                time.sleep(min(0.02, self.harness.remaining(deadline)))
        write_record(self.journal / "isolation.json", self.check_vmm())

    def verify_dispatch(self):
        try:
            self.check_vmm()
        except (OSError, KeyError, ValueError) as error:
            if isinstance(error, IsolationUnverified):
                raise
            raise IsolationUnverified("The pre-dispatch isolation proof failed") from error

    def sumsq(self, n):
        deadline = time.monotonic() + JOB
        with self.guest(deadline) as channel:
            require(time.monotonic_ns() < int(self.grant["expires_mono_ns"]),
                    "Grant expired before SUMSQ dispatch")
            result = self.harness.exchange(channel, f"SUMSQ {self.nonce} {n}", deadline)
        match = re.fullmatch(rf"SUMSQ {self.nonce} {n} ([0-9]+)", result)
        if match is None:
            raise RuntimeFailure("Guest SUMSQ proof failed")
        return int(match[1])

    def _freeze(self, frozen, deadline):
        self.members()
        (self.scope / "cgroup.freeze").write_text(f"{int(frozen)}\n")
        expected = f"frozen {int(frozen)}"
        while expected not in (self.scope / "cgroup.events").read_text().splitlines():
            time.sleep(min(0.02, self.harness.remaining(deadline)))

    def _stop(self):
        if not self.bootstrap_released:
            self._drain()
            return []
        try:
            return self._stop_observed()
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
            self._drain()
            # The frozen wire contract requires observed runtime and supervisor
            # exits. Absence alone cannot authorize release after a launch.
            return None

    def _drain(self):
        deadline = time.monotonic() + STOP
        with ExitStack() as stack:
            leaf = None
            try:
                if self.process is not None and self.process.poll() is None:
                    self.process.kill()
            finally:
                try:
                    if self.scope_id is not None:
                        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                        root = os.open(self.root, flags)
                        stack.callback(os.close, root)
                        root_info = os.fstat(root)
                        require((root_info.st_dev, root_info.st_ino) == self.cgroup_root_id,
                                "The cgroup root changed identity")
                        leaf = os.open(self.name, flags, dir_fd=root)
                        stack.callback(os.close, leaf)
                        leaf_info = os.fstat(leaf)
                        require((leaf_info.st_dev, leaf_info.st_ino) == self.scope_id,
                                "The owned cgroup changed identity")
                        self._kill_subtree(leaf)
                finally:
                    if self.process is not None:
                        self.process.wait(timeout=max(0, deadline - time.monotonic()))
            if leaf is not None:
                fd = os.open("cgroup.events", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=leaf)
                events = stack.enter_context(os.fdopen(fd))
                while True:
                    events.seek(0)
                    population = [line for line in events.read().splitlines() if line.startswith("populated ")]
                    require(population in (["populated 0"], ["populated 1"]), "Cgroup population is unproved")
                    if population == ["populated 0"]:
                        break
                    # The direct child has now exited. Drain any late attachment.
                    self.harness.remaining(deadline)
                    self._kill_subtree(leaf)
                    time.sleep(min(0.02, self.harness.remaining(deadline)))

    @staticmethod
    def _kill_subtree(leaf):
        # Use the retained identity even when an unexpected descendant made
        # the strict roster unusable. The drain provides no per-process proof.
        fd = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=leaf)
        try:
            require(os.write(fd, b"1\n") == 2, "Short cgroup kill request")
        finally:
            os.close(fd)

    def _stop_observed(self):
        deadline = time.monotonic() + STOP
        forced = False
        if self.process.poll() is None and self.boot_requested and self.runtime is not None:
            try:
                with self.guest(deadline) as channel:
                    reply = self.harness.exchange(channel, f"STOP {self.nonce}", deadline)
                    require(reply == f"STOPPING {self.nonce}", "Guest stop proof failed")
                self.process.wait(timeout=self.harness.remaining(deadline))
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                deadline = time.monotonic() + STOP
        if self.process.poll() is None:
            self._freeze(True, deadline)
            try:
                members = self.members()
                if self.runtime is None:
                    candidates = members - {self.supervisor["pid"]}
                    require(len(candidates) == 1, "Frozen roster has no unique runtime")
                    pid = candidates.pop()
                    verify_vmm_executable(PROC / str(pid))
                    self.runtime = self.track(pid, "runtime")
                roster = (self.supervisor, self.runtime)
                require(members == {member["pid"] for member in roster}, "Frozen roster differs")
                require(all(self.harness.pid_identity(member["pid"]) == int(member["start_ticks"])
                            for member in roster), "Frozen PID identity differs")
                signal.pidfd_send_signal(self.pidfds[self.runtime["pid"]], signal.SIGKILL)
                forced = True
            finally:
                self._freeze(False, deadline)
            self.process.wait(timeout=self.harness.remaining(deadline))
        require(self.runtime is not None, "The runtime exit could not be attributed")
        # The pinned supervisor propagates 128 + signal, paired with a signal
        # sent to the frozen runtime pidfd. No startup report is needed here.
        exit_code = (128 + signal.SIGKILL if forced
                     else self.read_status(time.monotonic() + STOP).get("exit-code"))
        require(type(exit_code) is int and exit_code == self.process.returncode and self.members() == set()
                and (exit_code == 128 + signal.SIGKILL if forced else 0 <= exit_code < 128),
                "Sandbox exit is unproved")
        members = []
        for member in (self.runtime, self.supervisor):
            proc = PROC / str(member["pid"])
            try:
                require(self.harness.pid_identity(member["pid"]) != int(member["start_ticks"]), "Member survived")
                absence = "start_ticks_changed"
            except FileNotFoundError:
                require(not proc.exists(), "Member absence is unproved")
                absence = "pid_absent"
            observed_exit = {"kind": "signal", "signal": signal.SIGKILL} if forced and member is self.runtime else {
                "kind": "exit", "code": exit_code}
            members.append({**member, "exit": observed_exit, "absence": absence})
        return members

    def retire(self):
        self.fenced = True
        try:
            try:
                write_record(self.journal / "native-fence.json", {"invocation_id": self.name, "launch_fenced": True})
            finally:
                result = self._retire()
            return result
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            raise RuntimeFailure("Native retirement is unproved") from error
        finally:
            if self.status_stream is not None:
                self.status_stream.close()
            for fd in self.pidfds.values():
                os.close(fd)
            self.pidfds.clear()

    def _retire(self):
        members = self._stop() if self.spawn_attempted else []
        sockets = []
        sockets_proved = True
        if self.workspace_id is not None:
            require(identity(self.workspace) == self.workspace_id
                    and identity(self.runtime_root) == self.runtime_root_id, "The RAM workspace changed")
            require({item.name for item in self.workspace.iterdir()} <= {"api.sock", "vsock.sock", "console"},
                    "Unexpected runtime inventory")
            for path, seen, creation_possible in (
                    (self.api, self.api_seen, self.bootstrap_released),
                    (self.vsock, self.vsock_seen, self.vsock_requested)):
                if os.path.lexists(path):
                    require(stat.S_ISSOCK(path.lstat().st_mode), "Runtime socket changed type")
                    path.unlink()
                    sockets.append({"path": str(path), "state": "removed"})
                elif seen:
                    sockets.append({"path": str(path), "state": "removed"})
                elif not creation_possible:
                    sockets.append({"path": str(path), "state": "never_created"})
                else:
                    sockets_proved = False
            (self.workspace / "console").unlink(missing_ok=True)
            self.workspace.rmdir()
        if self.scope_id is not None:
            require(self.members() == set(), "The leaf is populated")
            self.scope.rmdir()
        require(not os.path.lexists(self.scope) and not os.path.lexists(self.workspace), "Runtime paths survived")
        require(members is not None and sockets_proved, "The retired runtime inventory is unproved")
        observations = {"scope_gone": True, "surviving_pids": []}
        if not members:
            return NEVER_STARTED.copy(), observations
        scope = {"path": str(self.scope), "device": str(self.scope_id[0]), "inode": str(self.scope_id[1]),
                 "invocation_id": self.name, "membership_complete": True, "gone": True}
        return {"kind": "reaped", "launch_fenced": True, "scope": scope, "members": members,
                "sockets": sockets, "runtime_dir_gone": True, "backstop": "none"}, observations

    def observe(self, claim, proof, observed):
        require(BOOT_ID.read_text().strip() == self.grant["boot_id"], "The observation crossed a host boot")
        directory(self.evidence, private=True)
        require(not os.path.lexists(self.scope) and not os.path.lexists(self.workspace), "Closure paths survive")
        value = {"schema": "vp.fc_custody/v1", "claim": claim, "proof": proof, "observed_mono_ns": observed,
                 "scope_path": str(self.scope), "runtime_dir": str(self.workspace)}
        path = self.evidence / (self.name + ".json")
        record = canonical(value)
        # Publish immutable bytes atomically. Tools independently verifies them
        # against its registered roots, kernel membership and boot identity.
        temp = path.with_name(path.name + "." + secrets.token_hex(8))
        try:
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444), "wb") as out:
                out.write(record)
                out.flush()
                os.fsync(out.fileno())
            try:
                os.link(temp, path, follow_symlinks=False)
            except FileExistsError:
                require(read_pin(path, hash_bytes(record), 1 << 20) == record, "Conflicting custody publication")
        finally:
            temp.unlink(missing_ok=True)
        sync_directory(self.evidence)
        return {"id": self.name, "sha256": hash_bytes(record)}
