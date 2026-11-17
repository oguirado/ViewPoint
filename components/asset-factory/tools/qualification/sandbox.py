"""Trusted launcher for the qualification mount and syscall boundary."""

import ctypes
import errno
import json
import os
from pathlib import Path
import stat
import sys


OUTBOUND = ("connect", "sendto", "sendmsg", "sendmmsg", "io_uring_setup")
CLONE_UNTRACED = 0x00800000
WORK = Path("/work")
HIDDEN = tuple(Path(path) for path in ("/dev", "/proc", "/run", "/sys", "/work"))
SYSTEM_LINKS = ("bin", "lib", "lib64", "sbin")
BOOTSTRAP = (
    "import runpy, sys; entry, manifest, output, *roots = sys.argv[1:]; "
    "sys.path.extend(roots); sys.argv = [entry, manifest, output]; "
    "runpy.run_path(entry, run_name='__main__')"
)


class ArgComparison(ctypes.Structure):
    _fields_ = (("arg", ctypes.c_uint), ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64))


def seccomp_library_path():
    library = ctypes.CDLL("libseccomp.so.2")
    library.seccomp_init  # Validate the ABI and keep the library mapped.
    for row in Path("/proc/self/maps").read_text().splitlines():
        columns = row.split(maxsplit=5)
        if len(columns) == 6 and Path(columns[5]).name.startswith("libseccomp.so.2"):
            return Path(columns[5]).resolve(strict=True)
    raise OSError("seccomp_library_unavailable")


def export_filter(library_path):
    library = ctypes.CDLL(library_path)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
        ctypes.c_uint, ctypes.POINTER(ArgComparison),
    ]
    library.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW on the native ABI.
    if not context:
        raise OSError("seccomp_init_failed")
    fd = os.memfd_create("af-qualification-seccomp", os.MFD_CLOEXEC)
    try:
        def deny(name, error, comparison=None):
            syscall = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            action = 0x00050000 | error  # SCMP_ACT_ERRNO
            if syscall < 0 or library.seccomp_rule_add_array(
                context, action, syscall, int(comparison is not None),
                ctypes.byref(comparison) if comparison is not None else None,
            ) != 0:
                raise OSError(f"seccomp_rule_unavailable: {name}")

        for name in (*OUTBOUND, "ptrace", "process_vm_writev", "pidfd_getfd"):
            deny(name, errno.EACCES)
        # The legacy clone flags are a register argument on the supported ABIs.
        if os.uname().machine not in ("aarch64", "x86_64"):
            raise OSError("seccomp_clone_architecture_unqualified")
        deny("clone", errno.EACCES, ArgComparison(0, 7, CLONE_UNTRACED, CLONE_UNTRACED))
        # BPF cannot inspect clone_args behind clone3's pointer. ENOSYS lets
        # libc fall back to the screened clone syscall for ordinary workers.
        deny("clone3", errno.ENOSYS)
        if library.seccomp_export_bpf(context, fd) != 0:
            raise OSError("seccomp_export_failed")
        os.lseek(fd, 0, os.SEEK_SET)
        os.set_inheritable(fd, True)
        return fd
    except BaseException:
        os.close(fd)
        raise
    finally:
        library.seccomp_release(context)


def mount_pin(argv, seen, declared, source, expected, hidden):
    for path in dict.fromkeys((Path(declared), Path(source))):
        if any(path.is_relative_to(root) for root in hidden):
            raise OSError("sandbox_pin_on_reserved_path")
        if path in seen:
            if seen[path] != source:
                raise OSError("sandbox_pin_path_conflict")
            continue
        seen[path] = source
        # The read-only /usr tree and its standard aliases already expose
        # these symlinks. The resolved target still receives a pinned fd.
        if path != Path(source) and (
            path.is_relative_to("/usr") or path.parts[1] in SYSTEM_LINKS
        ):
            if path.resolve(strict=True) != Path(source):
                raise OSError("sandbox_system_alias_changed")
            continue
        fd = os.open(source, os.O_PATH | os.O_NOFOLLOW)
        info = os.fstat(fd)
        actual = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        if actual != expected or not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise OSError("sandbox_pin_changed")
        os.set_inheritable(fd, True)
        argv.extend(("--ro-bind-fd", str(fd), str(path)))


def launch(config_path):
    with open(config_path, encoding="utf-8") as stream:
        config = json.load(stream)
    filter_fd = export_filter(config["seccomp_library"])
    info_fd = os.open(config["mount_info"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.set_inheritable(info_fd, True)
    # Use the entry point as PID 1 instead of installing Bubblewrap's reaper.
    # It redirects and closes descriptors before executing candidate code.
    argv = [
        config["bwrap"], "--unshare-user", "--disable-userns", "--unshare-ipc",
        "--unshare-pid", "--as-pid-1", "--new-session", "--cap-drop", "ALL",
        "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--remount-ro", "/proc",
        "--dev", "/dev", "--remount-ro", "/dev",
        "--dir", "/tmp", "--dir", "/run", "--bind", config["work"], str(WORK),
        "--chdir", str(WORK), "--seccomp", str(filter_fd), "--info-fd", str(info_fd),
    ]
    for name in SYSTEM_LINKS:
        path = Path("/") / name
        if path.is_symlink() and path.resolve().is_relative_to("/usr"):
            argv.extend(("--symlink", os.readlink(path), str(path)))
        elif path.is_dir():
            argv.extend(("--ro-bind", str(path), str(path)))
    hidden = HIDDEN
    if config["runtime_dir"] is not None:
        hidden += (Path(config["runtime_dir"]),)
    seen = {}
    for declared, source, *expected in config["bindings"]:
        mount_pin(argv, seen, declared, source, expected, hidden)
    argv.extend(("--remount-ro", "/", "--", config["python"], "-I", "-S", "-B",
                 config["launcher"], "enter", *config["command"]))
    os.execv(argv[0], argv)


def enter(command):
    with open("/dev/null", "rb") as stdin, (WORK / ".stdout").open("xb") as stdout, (
        WORK / ".stderr"
    ).open("xb") as stderr:
        os.dup2(stdin.fileno(), 0)
        os.dup2(stdout.fileno(), 1)
        os.dup2(stderr.fileno(), 2)
    # Supervisor tools may carry writable fds through the namespace setup.
    # Clear them after redirecting the only streams the candidate receives.
    for name in os.listdir("/proc/self/fd"):
        fd = int(name)
        if fd >= 3:
            try:
                os.close(fd)
            except OSError as exc:
                # listdir's own descriptor has already closed.
                if exc.errno != errno.EBADF:
                    raise
    os.execv(command[0], command)


if __name__ == "__main__":
    if sys.argv[1] == "enter":
        enter(sys.argv[2:])
    else:
        launch(sys.argv[1])
