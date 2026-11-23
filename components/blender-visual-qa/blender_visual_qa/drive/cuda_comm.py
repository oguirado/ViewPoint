"""Broker the captive CUDA worker's write-only thread-name opens."""

from contextlib import ExitStack, contextmanager
import array
import ctypes
import errno
import fcntl
import json
import os
import re
import select
import socket
import stat
import struct
import subprocess
import sys
import threading


CHANNEL = "BVQA_CUDA_COMM_CHANNEL"
MARKER = b"bvqa-comm-v1"
PROC_SUPER_MAGIC = 0x9FA0
DIR = os.O_PATH | os.O_DIRECTORY
ABIS = {"aarch64": (0xC00000B7, 56, 277), "x86_64": (0xC000003E, 257, 317)}
NOTIFICATION = struct.Struct("=QIIiIQ6Q")
RESPONSE = struct.Struct("=QqiI")
ADDFD = struct.Struct("=QIIII")
ID = struct.Struct("=Q")
CREDS = struct.Struct("=iII")
COMM_PATH = re.compile(rb"/proc/self/task/([1-9][0-9]{0,9})/comm")
OPEN_BITS = os.O_CLOEXEC | os.O_NOFOLLOW | os.O_LARGEFILE
FOPEN_BITS = os.O_CREAT | os.O_TRUNC
CONTINUE = 1
ADDFD_SEND = 2
ENROLL_TIMEOUT = 10
REAP_TIMEOUT = 5
MAX_THREADS = 4096
# Linux pread requires an offset representable as a signed 64-bit value.
_SIGNED_FILE_OFFSET_LIMIT = 1 << (ctypes.sizeof(ctypes.c_int64) * 8 - 1)
LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.syscall.argtypes = (ctypes.c_long,)
LIBC.syscall.restype = ctypes.c_long
LIBC.fstatfs.argtypes = (ctypes.c_int, ctypes.c_void_p)
LIBC.fstatfs.restype = ctypes.c_int


class CommError(RuntimeError):
    pass


def _ioctl_number(direction, number, size):
    return (direction << 30) | (size << 16) | (ord("!") << 8) | number


RECV = _ioctl_number(3, 0, NOTIFICATION.size)
SEND = _ioctl_number(3, 1, RESPONSE.size)
ID_VALID = _ioctl_number(1, 2, ID.size)
ADD = _ioctl_number(1, 3, ADDFD.size)


def _abi():
    abi = ABIS.get(os.uname().machine)
    if abi is None or sys.byteorder != "little" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise CommError("cuda_comm_unsupported_abi")
    return abi


def _checked(result):
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def _install_filter():
    arch, openat, seccomp = _abi()
    sizes = (ctypes.c_ushort * 3)()
    _checked(LIBC.syscall(ctypes.c_long(seccomp), ctypes.c_uint(3), ctypes.c_uint(0),
                          ctypes.byref(sizes)))  # GET_NOTIF_SIZES
    if tuple(sizes) != (NOTIFICATION.size, RESPONSE.size, 64):
        raise CommError("cuda_comm_notification_abi")

    class Instruction(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("instructions", ctypes.POINTER(Instruction))]

    load_word, eq, mask, ret = 0x20, 0x15, 0x54, 0x06
    instructions = (
        (load_word, 0, 0, 4),                   # seccomp_data.arch
        (eq, 1, 0, arch),
        (ret, 0, 0, 0x80000000),               # KILL_PROCESS
        (load_word, 0, 0, 0),                  # seccomp_data.nr
        (eq, 0, 4, openat),
        (load_word, 0, 0, 32),                 # low word of args[2]
        (mask, 0, 0, os.O_ACCMODE),
        (eq, 0, 1, os.O_WRONLY),
        (ret, 0, 0, 0x7FC00000),               # USER_NOTIF
        (ret, 0, 0, 0x7FFF0000),               # ALLOW
    )
    code = (Instruction * len(instructions))(*(Instruction(*item) for item in instructions))
    program = Program(len(code), code)
    # FILTER with NEW_LISTENER and WAIT_KILLABLE_RECV.
    return _checked(LIBC.syscall(ctypes.c_long(seccomp), ctypes.c_uint(1),
                                 ctypes.c_uint((1 << 3) | (1 << 5)), ctypes.byref(program)))


def register(channel):
    """Publish one listener from the trusted gate before it execs the worker."""
    channel.settimeout(ENROLL_TIMEOUT)
    marker = ctypes.create_string_buffer(MARKER)
    listener = _install_filter()
    try:
        payload = ID.pack(ctypes.addressof(marker))
        if channel.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                       array.array("i", [listener]))]) != len(payload):
            raise CommError("cuda_comm_registration_send")
    finally:
        os.close(listener)
    if channel.recv(2) != b"+":  # Timeout, EOF and every other packet refuse exec.
        raise CommError("cuda_comm_registration_ack")


@contextmanager
def _open_proc(parent, path, flags=os.O_RDONLY):
    # NO_XDEV | NO_MAGICLINKS | NO_SYMLINKS | BENEATH. Only fixed components
    # and decimal IDs selected from the pinned procfs are supplied here.
    how = (ctypes.c_uint64 * 3)(flags | os.O_CLOEXEC, 0, 0x0F)
    fd = _checked(LIBC.syscall(ctypes.c_long(437), ctypes.c_int(parent), ctypes.c_char_p(os.fsencode(path)),
                               ctypes.byref(how), ctypes.c_size_t(ctypes.sizeof(how))))
    try:
        if os.fstat(fd).st_dev != os.fstat(parent).st_dev:
            raise CommError("cuda_comm_proc_device")
        yield fd
    finally:
        os.close(fd)


def _procfs(fd):
    # struct statfs starts with a native long on the supported Linux ABIs.
    data = (ctypes.c_long * 32)()
    _checked(LIBC.fstatfs(fd, ctypes.byref(data)))
    if data[0] != PROC_SUPER_MAGIC:
        raise CommError("cuda_comm_procfs_type")


def _fields(task):
    with _open_proc(task, "status") as fd:
        fields = dict(line.split(b":", 1) for line in os.read(fd, 8192).splitlines() if b":" in line)
    try:
        return tuple(tuple(map(int, fields[key].split())) for key in (b"NStgid", b"NSpid", b"PPid"))
    except (KeyError, ValueError) as exc:
        raise CommError("cuda_comm_proc_status") from exc


def _live(pidfd):
    poll = select.poll()
    poll.register(pidfd, select.POLLIN)
    return not poll.poll(0)


def _owned(proc, pid, launcher):
    # The passed socket is also inherited by trusted launch wrappers. Require
    # the actual PID-namespace init from this invocation, not a wrapper PID.
    for _ in range(16):
        if pid == launcher:
            return True
        if pid <= 1:
            break
        with _open_proc(proc, str(pid), DIR) as task:
            groups, pids, (pid,) = _fields(task)
            if not groups or groups != pids:
                return False
    return False


def _expect_missing(listener, action, payload):
    try:
        fcntl.ioctl(listener, action, payload)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return
        raise
    raise CommError("cuda_comm_registration_notification")


def _registration(channel, stack):
    payload, ancillary, flags, _ = channel.recvmsg(
        ID.size, socket.CMSG_SPACE(CREDS.size) + socket.CMSG_SPACE(4 * array.array("i").itemsize),
        socket.MSG_CMSG_CLOEXEC)
    listeners, credentials = [], []
    for level, kind, data in ancillary:
        if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
            fds = array.array("i")
            fds.frombytes(data)
            for fd in fds:
                stack.callback(os.close, fd)
                listeners.append(fd)
        elif (level, kind) == (socket.SOL_SOCKET, socket.SCM_CREDENTIALS):
            credentials.append(CREDS.unpack(data))
        else:
            flags |= socket.MSG_CTRUNC
    if (flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(payload) != ID.size
            or len(listeners) != 1 or len(credentials) != 1):
        raise CommError("cuda_comm_registration_message")
    pid, uid, gid = credentials[0]
    if pid <= 1 or (uid, gid) != (os.getuid(), os.getgid()):
        raise CommError("cuda_comm_registration_credentials")
    poll = select.poll()
    poll.register(channel, select.POLLIN)
    if poll.poll(0):
        raise CommError("cuda_comm_duplicate_or_closed_registration")
    return pid, listeners[0], ID.unpack(payload)[0]


def _enroll(channel, launcher, stack):
    pid, listener, address = _registration(channel, stack)
    if not 0 < address < _SIGNED_FILE_OFFSET_LIMIT:
        raise CommError("cuda_comm_registration_address")
    proc = os.open("/proc", DIR | os.O_NOFOLLOW | os.O_CLOEXEC)
    stack.callback(os.close, proc)
    _procfs(proc)
    if os.readlink("self", dir_fd=proc) != str(os.getpid()):
        raise CommError("cuda_comm_parent_proc_namespace")
    if os.readlink(f"self/fd/{listener}", dir_fd=proc) != "anon_inode:seccomp notify":
        raise CommError("cuda_comm_listener_identity")
    _expect_missing(listener, ID_VALID, ID.pack(0))
    _expect_missing(listener, ADD, ADDFD.pack(0, ADDFD_SEND, listener, 0, os.O_CLOEXEC))
    pidfd = os.pidfd_open(pid)
    stack.callback(os.close, pidfd)
    group = stack.enter_context(_open_proc(proc, str(pid), DIR))
    groups, pids, _ = _fields(group)
    if (groups != (pid, 1) or pids != groups or not _owned(proc, pid, launcher)
            or not _live(pidfd)):
        raise CommError("cuda_comm_enrollment_custody")
    tasks = stack.enter_context(_open_proc(group, "task", os.O_RDONLY | os.O_DIRECTORY))
    try:
        with _open_proc(tasks, f"{pid}/mem") as mem:
            if os.pread(mem, len(MARKER), address) != MARKER or not _live(pidfd):
                raise CommError("cuda_comm_task_memory_custody")
    except OSError as exc:
        raise CommError(f"cuda_comm_task_memory_unavailable:{exc}") from exc
    try:
        with _target(tasks, groups, 1):
            pass
    except OSError as exc:
        raise CommError(f"cuda_comm_proc_comm_unavailable:{exc}") from exc
    return listener, pidfd, tasks, groups


def _allowed_flags(flags):
    return (flags & ~OPEN_BITS) in (os.O_WRONLY, os.O_WRONLY | FOPEN_BITS)


def _local_tid(task, group, host_tid):
    groups, pids, _ = _fields(task)
    if (groups != group or len(pids) != len(group) or pids[0] != host_tid
            or pids[-1] <= 0):
        raise CommError("cuda_comm_thread_custody")
    return pids[-1]


@contextmanager
def _target(tasks, group, tid):
    with _open_proc(tasks, ".", os.O_RDONLY | os.O_DIRECTORY) as scan, os.scandir(scan) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_THREADS:
                break
            if not entry.name.isascii() or not entry.name.isdecimal():
                continue
            with ExitStack() as candidate:
                try:
                    task = candidate.enter_context(_open_proc(tasks, entry.name, DIR))
                    if _local_tid(task, group, int(entry.name)) != tid:
                        continue
                    # This names an existing synthetic inode. Never pass through
                    # O_CREAT, O_TRUNC, or a path supplied by the request.
                    fd = candidate.enter_context(_open_proc(task, "comm", os.O_WRONLY | os.O_NOFOLLOW))
                    if (not stat.S_ISREG(os.fstat(fd).st_mode)
                            or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_WRONLY):
                        raise CommError("cuda_comm_target_inode")
                    _procfs(fd)
                    if _local_tid(task, group, int(entry.name)) != tid:
                        raise CommError("cuda_comm_target_changed")
                except (FileNotFoundError, ProcessLookupError):
                    continue
                yield fd
                return
    raise CommError("cuda_comm_target_missing")


def _path(mem, address):
    if not 0 < address < _SIGNED_FILE_OFFSET_LIMIT:
        return None
    try:
        path, end, _ = os.pread(mem, len(b"/proc/self/task/0000000000/comm\0"), address).partition(b"\0")
    except OSError:
        return None
    return path if end else None


def _handle(listener, pidfd, tasks, group, request):
    cookie, host_tid, notif_flags, syscall, arch, _, dirfd, address, flags, *_ = request
    native_arch, openat, _ = _abi()
    response = RESPONSE.pack(cookie, 0, -errno.EPERM, 0)
    try:
        if notif_flags or arch != native_arch or syscall != openat or not _live(pidfd) or host_tid <= 0:
            raise CommError("cuda_comm_notification_custody")
        with ExitStack() as caller:
            try:
                requester = caller.enter_context(_open_proc(tasks, str(host_tid), DIR))
            except FileNotFoundError:
                # Another thread group gets no descriptor from this broker.
                requester = None
            if requester is not None:
                _local_tid(requester, group, host_tid)
                fcntl.ioctl(listener, ID_VALID, ID.pack(cookie))
                if _allowed_flags(flags) and (dirfd & 0xFFFFFFFF) == (-100 & 0xFFFFFFFF):  # AT_FDCWD
                    path = None
                    try:
                        mem = caller.enter_context(_open_proc(requester, "mem"))
                    except OSError:
                        mem = None
                    if mem is not None:
                        fcntl.ioctl(listener, ID_VALID, ID.pack(cookie))
                        path = _path(mem, address)
                    match = COMM_PATH.fullmatch(path) if path is not None else None
                    if match:
                        with _target(tasks, group, int(match[1])) as fd:
                            _local_tid(requester, group, host_tid)
                            if not _live(pidfd):
                                raise CommError("cuda_comm_group_exited")
                            fcntl.ioctl(listener, ID_VALID, ID.pack(cookie))
                            fcntl.ioctl(listener, ADD, ADDFD.pack(cookie, ADDFD_SEND, fd, 0, os.O_CLOEXEC))
                            return
            # Ordinary opens still run under the sandbox's original kernel
            # policy. No broker pathname or permissions are used in this arm.
            response = RESPONSE.pack(cookie, 0, 0, CONTINUE)
    except (CommError, OSError):
        pass
    try:
        fcntl.ioctl(listener, ID_VALID, ID.pack(cookie))
        fcntl.ioctl(listener, SEND, response)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise


def _serve(listener, pidfd, tasks, group, stop):
    poll = select.poll()
    poll.register(listener, select.POLLIN)
    poll.register(pidfd, select.POLLIN)
    poll.register(stop, select.POLLIN)
    while True:
        events = dict(poll.poll())
        if pidfd in events or stop in events:
            return
        listener_events = events.get(listener, 0)
        if listener_events & (select.POLLERR | select.POLLNVAL):
            raise CommError("cuda_comm_listener_lost")
        if listener_events & select.POLLHUP:
            # The kernel can release the exiting task's filter just before it
            # reports the same exit on the pidfd.
            poll.unregister(listener)
            if poll.poll(1000):
                return
            raise CommError("cuda_comm_listener_lost")
        if listener_events & select.POLLIN:
            message = bytearray(NOTIFICATION.size)
            try:
                fcntl.ioctl(listener, RECV, message)
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.EINTR):
                    continue
                raise
            _handle(listener, pidfd, tasks, group, NOTIFICATION.unpack(message))


def _supervise(launcher):
    _abi()
    if not hasattr(os, "pidfd_open"):
        raise CommError("cuda_comm_pidfd_unavailable")
    with ExitStack() as stack, ExitStack() as custody:
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
        stack.enter_context(parent)
        stack.enter_context(child)
        parent.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        parent.settimeout(ENROLL_TIMEOUT)
        process = subprocess.Popen(launcher, pass_fds=(child.fileno(),),
                                   env={**os.environ, CHANNEL: str(child.fileno())})
        errors = []
        broker = None

        def serve(*registration):
            try:
                _serve(*registration)
            except Exception as exc:
                errors.append(exc)

        try:
            child.close()
            registration = _enroll(parent, process.pid, custody)
            stop, wake = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
            custody.callback(os.close, stop)
            custody.callback(os.close, wake)
            service = threading.Thread(target=serve, args=(*registration, stop), daemon=True, name="cuda-comm")
            service.start()
            broker = service
            parent.sendall(b"+")
            parent.close()
            while not errors:
                try:
                    code = process.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=REAP_TIMEOUT)
            finally:
                if broker is not None:
                    try:
                        os.write(wake, b"+")
                        broker.join(timeout=REAP_TIMEOUT)
                    finally:
                        if broker.is_alive():
                            # The failing supervisor exits with custody intact.
                            # The kernel closes the fds when the process exits.
                            custody.pop_all()
                            raise CommError("cuda_comm_service_stop_timeout")
        if errors:
            raise CommError(f"cuda_comm_service:{errors[0]}")
        return code if code >= 0 else 128 - code


if __name__ == "__main__":
    try:
        raise SystemExit(_supervise(sys.argv[1:]))
    except (CommError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"admitted": False, "reason": f"cuda_comm_supervisor:{exc}"}), flush=True)
        raise SystemExit(5)
