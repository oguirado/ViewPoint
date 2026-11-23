"""Linux admission gate for a worker with a single writable RAM workspace."""

from contextlib import contextmanager
import ctypes
import errno
import fcntl
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import re
import resource
import socket
import stat
import subprocess
import sys
import tempfile
import time


RAM_FILESYSTEMS = {"tmpfs", "ramfs"}
NAMESPACES = ("mnt", "user", "pid", "ipc", "net", "uts")
RUNTIME = ("/usr", "/etc/alternatives", "/etc/ld.so.cache", "/etc/fonts", "/etc/glvnd", "/etc/vulkan")
CUDA_NODES = ("/dev/nvidia0", "/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/null")
CUDA_DEVICE = "NVIDIA GB10"
CUDA_MODULE_STATES = tuple(Path("/sys/module") / module / "initstate"
                           for module in ("nvidia", "nvidia_uvm"))
CUDA_SOCKET_TYPE = socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC
GATE = Path(__file__).resolve()
CUDA_COMM = GATE.with_name("cuda_comm.py")
CUDA_COMM_CHANNEL = "BVQA_CUDA_COMM_CHANNEL"
SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO = 0x00050000
SCMP_CMP_NE = 1


class ContainmentError(RuntimeError):
    pass


def _identity(path):
    info = os.stat(path, follow_symlinks=False)
    return [info.st_dev, info.st_ino]


def _valid_identity(value):
    return (isinstance(value, list) and len(value) == 2
            and all(type(number) is int and number >= 0 for number in value))


def _valid_captive_devices(devices):
    return (isinstance(devices, dict) and devices.keys() == set(CUDA_NODES)
            and all(isinstance(value, list) and len(value) == 2 and _valid_identity(value[0])
                    and type(value[1]) is int and value[1] >= 0 for value in devices.values()))


def _check_captive_cuda(enabled, devices, display):
    if type(enabled) is not bool:
        raise ContainmentError("invalid_captive_cuda_selection")
    if enabled:
        exact_devices = (_valid_captive_devices(devices) if isinstance(devices, dict)
                         else tuple(devices) == CUDA_NODES)
        if display is not None or not exact_devices:
            raise ContainmentError("captive_cuda_requires_headless_exact_devices")


def _check_captive_environment(env):
    forbidden = {"DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "XAUTHORITY",
                 "XDG_RUNTIME_DIR", "SESSION_MANAGER", CUDA_COMM_CHANNEL}
    if any(key in forbidden or key.startswith(("DBUS_", "LD_")) for key in env):
        raise ContainmentError("captive_cuda_external_environment")


def _captive_device_numbers():
    gpu_info = list(Path("/proc/driver/nvidia/gpus").glob("*/information"))
    adapters = sorted(path.name for path in Path("/dev").glob("nvidia[0-9]*"))
    if len(gpu_info) != 1 or adapters != ["nvidia0"]:
        raise ContainmentError("captive_cuda_requires_one_gb10")
    fields = dict((key.strip(), value.strip()) for key, _, value in
                  (line.partition(":") for line in gpu_info[0].read_text().splitlines()))
    if fields.get("Model") != CUDA_DEVICE or fields.get("Device Minor") != "0":
        raise ContainmentError("captive_cuda_gpu_identity")
    character_devices = Path("/proc/devices").read_text().partition("Block devices:")[0]
    uvm = re.findall(r"^\s*([0-9]+)\s+nvidia-uvm$", character_devices, re.MULTILINE)
    if len(uvm) != 1:
        raise ContainmentError("captive_cuda_uvm_identity")
    return dict(zip(CUDA_NODES, (os.makedev(195, 0), os.makedev(195, 255),
                                 os.makedev(int(uvm[0]), 0), os.makedev(1, 3))))


def _mounts():
    # mountinfo escapes these four characters in path fields.
    escapes = {r"\040": " ", r"\011": "\t", r"\012": "\n", r"\134": "\\"}
    result = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        mount_id, parent_id, device, root, target, flags, *propagation = left.split()
        fs, source, super_flags = right.split()
        target = re.sub(r"\\[0-7]{3}", lambda match: escapes[match[0]], target)
        result.append(dict(id=mount_id, parent=parent_id, target=target, device=device,
                           root=root, fs=fs, source=source, flags=flags.split(","),
                           super_flags=super_flags.split(","), propagation=propagation))
    return result


def _namespaces():
    return {name: os.readlink(f"/proc/self/ns/{name}") for name in NAMESPACES}


def worker_environment(env, workspace):
    """Add the contained worker's scratch and Python settings to a declared environment."""
    home = str(workspace / "xdg")
    temp = str(workspace / "xdg" / "tmp")
    return dict(env, HOME=home, TMPDIR=temp, TMP=temp, TEMP=temp, PYTHONDONTWRITEBYTECODE="1")


def ram_mount(path):
    """Confirm the effective host mount before trusted scratch setup."""
    fd = os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        fields = dict(line.split(":", 1) for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines())
        mount_id = fields["mnt_id"].strip()
    finally:
        os.close(fd)
    mount = next((entry for entry in _mounts() if entry["id"] == mount_id), None)
    device = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
    if (mount is None or not path.is_relative_to(mount["target"])
            or mount["fs"] not in RAM_FILESYSTEMS or mount["device"] != device
            or not {"rw", "nodev", "nosuid"} <= set(mount["flags"])):
        raise ContainmentError("ram_root_mount")
    return info


@contextmanager
def workspace(parent=None):
    """Allocate a private directory on the approved, existing RAM root."""
    try:
        # The gate runs as an isolated script. Only the host reads the shared policy.
        from bpy_side.canon import RAMWS_ROOT

        root = Path(parent) if parent is not None else Path(os.environ.get("VQA_RAMWS_ROOT", RAMWS_ROOT))
        info = ram_mount(root)
        if (root.resolve(strict=True) != root or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ContainmentError("ram_root_identity")
        temporary = tempfile.TemporaryDirectory(prefix="drive-", dir=root)
    except (ImportError, OSError, ValueError) as exc:
        raise ContainmentError(f"ram_workspace:{exc}") from exc
    with temporary as name:
        run = Path(name)
        if run.stat().st_dev != info.st_dev:
            raise ContainmentError("ram_workspace_device")
        yield run


def _captive_executable_identity(argv, mounts, aliases):
    error = "captive_cuda_runtime_executable"
    if not argv or not argv[0]:
        raise ContainmentError(error)
    executable = Path(argv[0])
    if executable.anchor != "/" or ".." in executable.parts:
        raise ContainmentError(error)

    def normalized_target(target):
        # The target belongs to the new root and may not exist on the host.
        return Path(os.path.normpath("/" + str(target).lstrip("/")))

    sources = []
    for source, target in mounts:
        target = Path(target)
        if normalized_target(target) == executable:
            if target != executable:
                raise ContainmentError(error)
            sources.append(source)
    for _, target in aliases:
        if normalized_target(target) in executable.parents:
            raise ContainmentError(error)
    if len(sources) > 1:
        raise ContainmentError(error)

    source = Path(sources[0]) if sources else executable
    try:
        resolved = source.resolve(strict=True)
        runtime = source.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ContainmentError(error) from exc
    if resolved != source or not stat.S_ISREG(runtime.st_mode) or not os.access(source, os.X_OK):
        raise ContainmentError(error)
    return [runtime.st_dev, runtime.st_ino]


# @anchor:bv.drive.containment.command
def command(argv, workspace, readonly, log, *, error_log=None, aliases=(), devices=(), display=None,
            captive_cuda=False, own_thread_comm=False, _captive_pins=None, _module_state=()):
    """Build argv for a trusted caller using an allowlisted environment and stdio pipes."""
    devices = tuple(devices)
    _check_captive_cuda(captive_cuda, devices, display)
    if type(own_thread_comm) is not bool or own_thread_comm and not captive_cuda:
        raise ContainmentError("invalid_cuda_comm_selection")
    allowed_devices = ("/dev/null",) if display is not None else CUDA_NODES
    if devices and devices != allowed_devices:
        raise ContainmentError("unsupported_device_allowlist")
    module_state = tuple(_module_state)
    if module_state and not captive_cuda:
        raise ContainmentError("invalid_cuda_module_state_selection")
    try:
        aliases = tuple(aliases)
        readonly = [Path(path) for path in RUNTIME] + [GATE, *readonly]
        if captive_cuda:
            readonly.append(CUDA_COMM)
        mounts = [(path, path) for path in readonly] + list(aliases)
        workspace_source = workspace
        workspace = workspace.resolve(strict=True)
        if captive_cuda:
            _check_captive_sysfs([*mounts, (workspace_source, workspace)], module_state)
            executable_id = _captive_executable_identity(argv, mounts, aliases)
            _check_comm_mounts(mounts, workspace)
            _check_module_snapshots(module_state, workspace)
            mounts.extend(module_state)
        identities = {str(target): _identity(source) for source, target in mounts}
        identities[str(workspace)] = _identity(workspace)
        expected_nodes = _captive_device_numbers() if captive_cuda else {}
        device_ids = {}
        for path in devices:
            device = Path(path).lstat()
            if not stat.S_ISCHR(device.st_mode):
                raise ContainmentError("device_not_character:" + path)
            if captive_cuda and device.st_rdev != expected_nodes[path]:
                raise ContainmentError("captive_cuda_device_number:" + path)
            device_ids[path] = [[device.st_dev, device.st_ino], device.st_rdev]
        parent_namespaces = _namespaces()
    except OSError as exc:
        raise ContainmentError(f"launcher_prerequisite:{exc}") from exc
    if not log.is_relative_to(workspace) or (error_log is not None and not error_log.is_relative_to(workspace)):
        raise ContainmentError("log_outside_workspace")
    config = dict(workspace=str(workspace), identities=identities,
                  parent_namespaces=parent_namespaces, log=str(log))
    if error_log is not None:
        config["error_log"] = str(error_log)
    if device_ids:
        config["devices"] = device_ids
    if captive_cuda:
        config.update(captive_cuda=True, executable_identity=executable_id, own_thread_comm=own_thread_comm)
        if module_state:
            config["module_state"] = [str(target) for _, target in module_state]
    if display is not None:
        if type(display) is not int or display < 90:
            raise ContainmentError("invalid_private_display")
        if set(device_ids) != {"/dev/null"}:
            raise ContainmentError("display_device_allowlist")
        config["display"] = display
    # Privatize propagation before bwrap binds host paths. Its default slave
    # mounts could otherwise receive a new host submount after admission.
    launcher = ["/usr/bin/unshare", "--user", "--map-current-user", "--mount",
                "--propagation", "private", "/usr/bin/bwrap",
                "--unshare-user", "--disable-userns", "--assert-userns-disabled",
                "--unshare-pid", "--unshare-ipc", "--unshare-net", "--unshare-uts",
                "--as-pid-1", "--die-with-parent", "--cap-drop", "ALL",
                "--symlink", "usr/lib", "/lib", "--symlink", "usr/bin", "/bin",
                "--proc", "/proc", "--remount-ro", "/proc",
                "--bind", str(workspace), str(workspace)]
    for source, target in mounts:
        launcher.extend(("--ro-bind", str(source), str(target)))
    for path in device_ids:
        # A single character inode needs device access. The enclosing
        # directory remains read-only and exposes no siblings.
        launcher.extend(("--dev-bind", path, path))
    launcher.extend(("--dir", "/tmp"))
    if display is not None:
        # Xvfb's keyboard compiler uses this directory when it is available.
        # Keep its output on the workspace mount instead of falling back to /tmp.
        xkb = workspace / "xdg" / "tmp" / "xkb"
        launcher.extend(("--symlink", str(xkb), "/var/lib/xkb"))
    launcher.extend(("--chdir", str(workspace), "--remount-ro", "/", "--",
                     "/usr/bin/python3", "-IBS", str(GATE), json.dumps(config), *argv))
    if captive_cuda and _captive_pins is not None:
        _captive_pins.update(executable_identity=executable_id, devices=device_ids)
    if own_thread_comm:
        launcher[:0] = ["/usr/bin/python3", "-IBS", str(CUDA_COMM)]
    return launcher


def _check_comm_mounts(mounts, workspace):
    """Keep the trusted gate, broker module and Python runtime above scratch."""
    protected = (*map(Path, ("/usr", "/etc", "/proc", "/lib", "/lib64", "/bin")), GATE, CUDA_COMM)
    expected = {*map(Path, RUNTIME), GATE, CUDA_COMM}
    seen = set()
    for source, target in [*mounts, (workspace, workspace)]:
        path = Path(os.path.normpath("/" + str(target).lstrip("/")))
        if not any(path.is_relative_to(item) or item.is_relative_to(path) for item in protected):
            continue
        if path not in expected or Path(target) != path or Path(source) != path or path in seen:
            raise ContainmentError("cuda_comm_trusted_mount")
        seen.add(path)


def _overlap(first, second):
    return first.is_relative_to(second) or second.is_relative_to(first)


def _check_captive_sysfs(mounts, module_state):
    sys_root = Path("/sys")
    host_sysfs = {sys_root, *(Path(m["target"]) for m in _mounts() if m["fs"] == "sysfs")}
    snapshot_dirs = {Path(source).parent for source, _ in module_state}
    for source, target in mounts:
        source = Path(source)
        target = Path("/" + str(target).lstrip("/"))
        normalized_target = Path(os.path.normpath(target))
        try:
            resolved_source, resolved_target = source.resolve(), target.resolve()
        except (OSError, RuntimeError) as exc:
            raise ContainmentError("captive_cuda_mount_resolution") from exc
        if (any(_overlap(path, root) for path in (Path(os.path.abspath(source)), resolved_source)
                for root in host_sysfs)
                or any(_overlap(path, root) for path in (normalized_target, resolved_target)
                       for root in host_sysfs)):
            raise ContainmentError("captive_cuda_external_sysfs")
        if any(_overlap(resolved_source, directory) for directory in snapshot_dirs):
            raise ContainmentError("captive_cuda_module_snapshot_overlap")


def _check_module_snapshots(snapshots, workspace):
    if not snapshots:
        return
    if tuple(target for _, target in snapshots) != CUDA_MODULE_STATES:
        raise ContainmentError("captive_cuda_module_snapshot_targets")
    directory = snapshots[0][0].parent
    if (directory.parent != workspace.parent or not directory.name.startswith("cuda-state-")
            or directory.resolve(strict=True) != directory):
        raise ContainmentError("captive_cuda_module_snapshot_directory")
    info = ram_mount(directory)
    if info.st_dev != workspace.stat().st_dev or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ContainmentError("captive_cuda_module_snapshot_ram")
    for source, target in snapshots:
        if source != directory / target.parent.name:
            raise ContainmentError("captive_cuda_module_snapshot_source")
        _verify_module_snapshot(source, info.st_dev)


def _verify_module_snapshot(path, device):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_dev != device or info.st_nlink != 1
            or info.st_uid != os.getuid() or info.st_mode & 0o222 or info.st_size != len(b"live\n")):
        raise ContainmentError("captive_cuda_module_snapshot_file")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as snapshot:
        pinned = os.fstat(snapshot.fileno())
        if (pinned.st_dev, pinned.st_ino) == (info.st_dev, info.st_ino) and snapshot.read(6) == b"live\n":
            return
    raise ContainmentError("captive_cuda_module_snapshot_file")


def _verify_captive_sysfs(mounts, config):
    snapshots = config.get("module_state", [])
    if snapshots and snapshots != list(map(str, CUDA_MODULE_STATES)):
        raise ContainmentError("captive_cuda_module_snapshot_targets")
    mounted = set()
    sys_root = Path("/sys")
    for mount in mounts:
        target = Path(mount["target"])
        if mount["fs"] == "sysfs":
            raise ContainmentError("captive_cuda_external_sysfs")
        if target == Path("/") or not _overlap(target, sys_root):
            continue
        if str(target) not in snapshots:
            raise ContainmentError("captive_cuda_external_sysfs")
        if mount["fs"] not in RAM_FILESYSTEMS or "ro" not in mount["flags"]:
            raise ContainmentError("captive_cuda_module_snapshot_mount")
        mounted.add(str(target))
    if mounted != set(snapshots):
        raise ContainmentError("captive_cuda_module_snapshot_mount")
    for target in snapshots:
        _verify_module_snapshot(Path(target), config["identities"][config["workspace"]][0])


# @anchor:bv.drive.containment.verify_mounts
def _verify_mounts(mounts, config):
    workspace = config["workspace"]
    devices = config.get("devices", {})
    if config.get("captive_cuda"):
        _verify_captive_sysfs(mounts, config)
    writable = [m for m in mounts if "rw" in m["flags"] and m["target"] not in devices]
    if [m["target"] for m in writable] != [workspace]:
        raise ContainmentError("writable_mounts:" + ",".join(m["target"] for m in writable))
    if (len(mounts) != len(config["identities"]) + len(devices) + 2
            or {m["target"] for m in mounts} != {"/", "/proc", *config["identities"], *devices}
            or any(m["propagation"] or "nosuid" not in m["flags"]
                   or ("nodev" in m["flags"]) == (m["target"] in devices)
                    or (m["target"] in devices and m["fs"] != "devtmpfs")
                    or (m["target"] == "/proc" and (m["fs"] != "proc" or "ro" not in m["flags"]))
                   or len({"ro", "rw"} & set(m["flags"])) != 1 for m in mounts)):
        raise ContainmentError("mount_inventory")
    device = config["identities"][workspace][0]
    if (writable[0]["fs"] not in RAM_FILESYSTEMS
            or writable[0]["device"] != f"{os.major(device)}:{os.minor(device)}"
            or any(_identity(path) != expected for path, expected in config["identities"].items())):
        raise ContainmentError("mount_identity")
    for path, (identity, devnum) in devices.items():
        info = os.lstat(path)
        if _identity(path) != identity or not stat.S_ISCHR(info.st_mode) or info.st_rdev != devnum:
            raise ContainmentError("device_identity:" + path)


def _verify_worker_environment(config):
    workspace = Path(config["workspace"])
    for name, expected in worker_environment({}, workspace).items():
        if os.environ.get(name) != expected:
            raise ContainmentError("worker_environment:" + name)
    device = config["identities"][str(workspace)][0]
    for path in (workspace / "xdg", workspace / "xdg" / "tmp"):
        if not path.is_dir() or path.resolve(strict=True) != path or path.stat().st_dev != device:
            raise ContainmentError("worker_environment_path")
    if not stat.S_ISDIR(Path("/tmp").lstat().st_mode):
        raise ContainmentError("literal_tmp_directory")


def _verify_descriptors(channel=None):
    for name in os.listdir("/proc/self/fd"):
        if int(name) > 2 and int(name) != channel:
            # listdir can include its own now-closed directory fd.
            try:
                os.fstat(int(name))
            except OSError as exc:
                if exc.errno == errno.EBADF:
                    continue
                raise
            raise ContainmentError("extra_descriptor")
    source, stdout, stderr = (os.fstat(fd) for fd in (0, 1, 2))
    modes = [fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE for fd in (0, 1, 2)]
    if (not stat.S_ISFIFO(source.st_mode)
            or not all(stat.S_ISFIFO(s.st_mode) for s in (stdout, stderr))
            or (stdout.st_dev, stdout.st_ino) != (stderr.st_dev, stderr.st_ino)
            or modes != [os.O_RDONLY, os.O_WRONLY, os.O_WRONLY]):
        raise ContainmentError(f"stdio_descriptors:{modes}")


def _register_comm(channel):
    with socket.socket(fileno=channel) as control:
        if (control.family != socket.AF_UNIX or control.type != socket.SOCK_SEQPACKET
                or control.getsockname() or control.getpeername()):
            raise ContainmentError("cuda_comm_channel_identity")
        spec = spec_from_file_location("cuda_comm", CUDA_COMM)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            module.register(control)
        except (module.CommError, OSError) as exc:
            raise ContainmentError(f"cuda_comm_gate:{exc}") from exc


def _restrict_syscalls(display=False, *, captive_cuda=False):
    """Prevent new IPC endpoints, mount changes and asynchronous syscall bypasses."""
    library = ctypes.CDLL("libseccomp.so.2")
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    class Compare(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]

    library.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Compare)]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None
    deny = SCMP_ACT_ERRNO | errno.EPERM
    context = library.seccomp_init(SCMP_ACT_ALLOW)
    if not context:
        raise ContainmentError("seccomp_init")
    try:
        if captive_cuda:
            ipc = (b"socketpair", b"connect", b"accept", b"accept4", b"sendto", b"sendmsg", b"sendmmsg")
        else:
            ipc = () if display else (b"socket", b"socketpair", b"connect")
        for name in (*ipc, b"io_uring_setup", b"unshare", b"setns",
                     b"mount", b"umount2", b"pivot_root", b"open_tree", b"move_mount",
                     b"fsopen", b"fsconfig", b"fsmount", b"fspick", b"mount_setattr",
                     b"mknod", b"mknodat", b"pidfd_getfd", b"add_key", b"request_key",
                     b"keyctl", b"bpf", b"perf_event_open"):
            call = library.seccomp_syscall_resolve_name(name)
            if call == -1 or library.seccomp_rule_add(context, deny, call, 0) != 0:
                raise ContainmentError(f"seccomp_rule:{name.decode()}")
        if display:
            # SCMP_CMP_NE against AF_UNIX. The X server and client are in the
            # same private network namespace and use an abstract Unix socket.
            restriction = Compare(0, SCMP_CMP_NE, socket.AF_UNIX, 0)
            for name in (b"socket", b"socketpair"):
                call = library.seccomp_syscall_resolve_name(name)
                if call == -1 or library.seccomp_rule_add_array(context, deny, call, 1, ctypes.byref(restriction)) != 0:
                    raise ContainmentError("seccomp_display_socket")
        if captive_cuda:
            call = library.seccomp_syscall_resolve_name(b"socket")
            if call < 0:
                raise ContainmentError("seccomp_captive_cuda_socket")
            # Separate rules deny a mismatch in any argument.
            for arg, value in enumerate((socket.AF_UNIX, CUDA_SOCKET_TYPE, 0)):
                restriction = Compare(arg, SCMP_CMP_NE, value, 0)
                if library.seccomp_rule_add_array(context, deny, call, 1, ctypes.byref(restriction)) != 0:
                    raise ContainmentError("seccomp_captive_cuda_socket")
        if library.seccomp_load(context) != 0:
            raise ContainmentError("seccomp_load")
    finally:
        library.seccomp_release(context)


def _restrict_filesystem_sockets():
    """Allow abstract bind while denying pathname socket creation on every mount."""
    # Linux assigns these Landlock syscall numbers on both supported native ABIs.
    if os.uname().machine not in {"aarch64", "x86_64"} or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ContainmentError("captive_cuda_landlock_arch")
    create_ruleset, restrict_self = ctypes.c_long(444), ctypes.c_long(446)
    syscall = ctypes.CDLL(None, use_errno=True).syscall
    syscall.argtypes = (ctypes.c_long,)
    syscall.restype = ctypes.c_long
    abi = syscall(create_ruleset, ctypes.c_void_p(), ctypes.c_size_t(0),
                  ctypes.c_uint(1))  # LANDLOCK_CREATE_RULESET_VERSION
    if abi < 1:
        raise ContainmentError("captive_cuda_landlock_abi")
    handled_access_fs = ctypes.c_uint64(1 << 9)  # LANDLOCK_ACCESS_FS_MAKE_SOCK (ABI 1)
    attr = ctypes.cast(ctypes.pointer(handled_access_fs), ctypes.c_void_p)
    fd = syscall(create_ruleset, attr, ctypes.c_size_t(ctypes.sizeof(handled_access_fs)), ctypes.c_uint(0))
    if fd < 0:
        raise ContainmentError("captive_cuda_landlock_ruleset:" + str(ctypes.get_errno()))
    try:
        if syscall(restrict_self, ctypes.c_int(fd), ctypes.c_uint(0)) != 0:
            raise ContainmentError("captive_cuda_landlock_enforce:" + str(ctypes.get_errno()))
    finally:
        os.close(fd)
    return abi


def admission(output):
    """Read the trusted gate's response from the pipe closed before worker exec."""
    try:
        record = json.loads(output)
    except (ValueError, UnicodeError) as exc:
        raise ContainmentError(f"launcher_protocol:{output[:512].decode(errors='replace').strip()}") from exc
    if not isinstance(record, dict) or record.get("admitted") is not True:
        reason = record.get("reason", "invalid") if isinstance(record, dict) else "invalid"
        raise ContainmentError(f"admission:{reason}")
    return output.strip()


def _check_socket_mounts(config, *, captive_cuda=False):
    # A read-only pathname socket can still delegate writes to its server.
    paths = list(config["identities"])
    reason = "captive_cuda_external_socket" if captive_cuda else "external_display_socket"
    while paths:
        path = paths.pop()
        info = os.lstat(path)
        if stat.S_ISSOCK(info.st_mode):
            raise ContainmentError(reason)
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        paths.append(entry.path)
                    elif (not entry.is_file(follow_symlinks=False) and not entry.is_symlink()
                          and stat.S_ISSOCK(entry.stat(follow_symlinks=False).st_mode)):
                        raise ContainmentError(reason)


@contextmanager
def _private_display(number, log):
    if number is None:
        yield None
        return
    (Path(os.environ["TMPDIR"]) / "xkb").mkdir(mode=0o700)
    address = f"/tmp/.X11-unix/X{number}"
    with open(log, "xb") as output:
        argv = ["/usr/bin/Xvfb", f":{number}", "-nolisten", "tcp", "-nolisten", "unix", "-ac", "-extension", "MIT-SHM",
                "-extension", "DRI3", "-screen", "0", "640x480x24", "-displayfd", "1"]
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output) as server:
            try:
                deadline = time.monotonic() + 10
                while True:
                    messages = Path(log).read_bytes()
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise ContainmentError("private_display_startup:" + Path(log).read_text(errors="replace")[-1000:])
                    if f"{number}".encode() in messages.splitlines():
                        with socket.socket(socket.AF_UNIX) as client:
                            client.connect("\0" + address)
                        break
                    time.sleep(0.02)
                os.environ["DISPLAY"] = f":{number}"
                yield {"display": os.environ["DISPLAY"], "server_pid": server.pid,
                       "socket": "@" + address, "transport": "private-unix"}
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()


def _run(config, argv):
    mounts = _mounts()
    evidence = dict(mounts=mounts)
    try:
        display = config.get("display")
        captive_cuda = config.get("captive_cuda", False)
        own_thread_comm = config.get("own_thread_comm", False)
        devices = config.get("devices")
        _check_captive_cuda(captive_cuda, devices if isinstance(devices, dict) else {}, display)
        if type(own_thread_comm) is not bool or own_thread_comm and not captive_cuda:
            raise ContainmentError("invalid_cuda_comm_selection")
        _verify_mounts(mounts, config)
        _verify_worker_environment(config)
        namespaces = _namespaces()
        if os.getpid() != 1 or any(value == config["parent_namespaces"][name]
                                   for name, value in namespaces.items()):
            raise ContainmentError("namespace_identity")
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
        caps = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        if any(int(status[key], 16) for key in caps) or status["NoNewPrivs"].strip() != "1":
            raise ContainmentError("privilege_state")
        channel = None
        if own_thread_comm:
            channel = int(os.environ.pop(CUDA_COMM_CHANNEL, "0"))
            if channel <= 2:
                raise ContainmentError("cuda_comm_channel_missing")
        _verify_descriptors(channel)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if display is not None or captive_cuda:
            _check_socket_mounts(config, captive_cuda=captive_cuda)
        if captive_cuda:
            _check_captive_environment(os.environ)
            if not argv or not argv[0] or _identity(argv[0]) != config.get("executable_identity"):
                raise ContainmentError("captive_cuda_runtime_identity")
            evidence.update(captive_cuda=True, landlock_abi=_restrict_filesystem_sockets(),
                             executable_identity=config["executable_identity"], own_thread_comm=own_thread_comm)
            if own_thread_comm:
                _register_comm(channel)
                _verify_descriptors()
        _restrict_syscalls(display is not None, captive_cuda=captive_cuda)
        evidence.update(namespaces=namespaces, parent_namespaces=config["parent_namespaces"],
                        workspace_identity=config["identities"][config["workspace"]],
                        pid=os.getpid(), capabilities={key: status[key].strip() for key in caps},
                        no_new_privs=True, stdio="eof-pipe,gate-pipe,gate-pipe", seccomp=True)
        if config.get("devices"):
            evidence["devices"] = config["devices"]
        log = os.open(config["log"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        error_log = log
        try:
            if "error_log" in config:
                error_log = os.open(config["error_log"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            if any(os.fstat(fd).st_dev != evidence["workspace_identity"][0] for fd in (log, error_log)):
                raise ContainmentError("log_device")
            with _private_display(display, config["log"] + ".xvfb") as display_evidence:
                if display_evidence is not None:
                    evidence["display"] = display_evidence
                print(json.dumps(dict(evidence, admitted=True), sort_keys=True), flush=True)
                os.dup2(log, 1)
                os.dup2(error_log, 2)
                if display_evidence is not None:
                    with subprocess.Popen(argv) as worker:
                        return worker.wait()
        finally:
            if error_log != log:
                os.close(error_log)
            os.close(log)
    except (ContainmentError, OSError, ValueError) as exc:
        print(json.dumps(dict(evidence, admitted=False, reason=str(exc)), sort_keys=True), flush=True)
        return 5
    os.execv(argv[0], argv)


if __name__ == "__main__":
    raise SystemExit(_run(json.loads(sys.argv[1]), sys.argv[2:]))
