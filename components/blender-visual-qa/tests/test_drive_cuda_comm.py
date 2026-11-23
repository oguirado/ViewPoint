"""Offline CUDA comm broker controls. No fixture opens a GPU device."""

from contextlib import ExitStack, nullcontext
import ctypes
import errno
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from blender_visual_qa.drive import containment, cuda_comm, worker


PROBE = r"""
import ctypes, errno, fcntl, os, pathlib, queue, sys, threading

enabled = sys.argv[-1] == "on"
assert os.getpid() == 1
assert not os.environ.get("BVQA_CUDA_COMM_CHANNEL")
assert {fd for fd in os.listdir("/proc/self/fd")
        if os.path.lexists("/proc/self/fd/" + fd)} == {"0", "1", "2"}
assert os.statvfs("/proc").f_flag & os.ST_RDONLY

def denied(path, flags, expected=errno.EROFS):
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        assert exc.errno == expected, (path, flags, exc)
    else:
        os.close(fd)
        raise AssertionError((path, flags, "opened"))

long_name = "ordinary-" + "w" * 140
long_path = pathlib.Path.cwd() / long_name
long_path.write_text("ordinary long RAM path")
assert long_path.read_text() == "ordinary long RAM path"
denied("/usr/" + long_name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
openat = ctypes.CDLL(None, use_errno=True).openat
openat.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
assert openat(-100, None, os.O_WRONLY) == -1 and ctypes.get_errno() == errno.EFAULT

ready = queue.Queue()
done = threading.Event()
def sibling():
    ready.put(threading.get_native_id())
    done.wait(8)

t = threading.Thread(target=sibling)
t.start()
try:
    tid = ready.get(timeout=5)
    path = f"/proc/self/task/{tid}/comm"
    original = pathlib.Path(path).read_text().strip()
    if enabled:
        with open(path, "w", encoding="ascii") as handle:
            assert fcntl.fcntl(handle, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY
            assert fcntl.fcntl(handle, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
            handle.write("bvqa-sibling")
        assert pathlib.Path(path).read_text().strip() == "bvqa-sibling"
        leader = "/proc/self/task/1/comm"
        def rename_leader():
            fd = os.open(leader, os.O_WRONLY)
            try:
                assert os.write(fd, b"bvqa-leader") == len(b"bvqa-leader")
            finally:
                os.close(fd)
        requester = threading.Thread(target=rename_leader)
        requester.start()
        requester.join(5)
        assert not requester.is_alive()
        assert pathlib.Path(leader).read_text().strip() == "bvqa-leader"
    else:
        denied(path, os.O_WRONLY)
        denied(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        assert pathlib.Path(path).read_text().strip() == original
    for flags in (os.O_RDWR, os.O_WRONLY | os.O_APPEND, os.O_WRONLY | os.O_CREAT):
        denied(path, flags)
    for alternate in (f"/proc/self/task/{tid}/stat", f"/proc/1/task/{tid}/comm", "/proc/self/status",
                      "/usr/bvqa-outside-ram"):
        denied(alternate, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
finally:
    done.set()
    t.join(5)
    assert not t.is_alive()

child = os.fork()
if child == 0:
    try:
        tid = threading.get_native_id()
        denied(f"/proc/self/task/{tid}/comm", os.O_WRONLY, errno.EROFS)
        pathlib.Path("descendant-ram.txt").write_text("ordinary output")
        with open("/dev/null", "w") as output:
            output.write("ordinary output")
    except BaseException:
        import traceback
        traceback.print_exc()
        os._exit(1)
    os._exit(0)
assert os.waitpid(child, 0) == (child, 0)
assert pathlib.Path("descendant-ram.txt").read_text() == "ordinary output"
pathlib.Path("comm-ready.tmp").write_text(str(os.getpid()))
os.replace("comm-ready.tmp", "comm-ready")
sys.stdin.readline()
"""


class CudaCommCustodyTests(unittest.TestCase):
    def test_namespace_local_tid_requires_the_enrolled_host_group(self):
        with patch.object(cuda_comm, "_fields", return_value=((71, 1), (89, 3), (30,))):
            self.assertEqual(cuda_comm._local_tid(9, (71, 1), 89), 3)
            for group, tid in (((72, 1), 89), ((71, 1), 90), ((71, 1), 3)):
                with self.subTest(group=group, tid=tid), self.assertRaises(cuda_comm.CommError):
                    cuda_comm._local_tid(9, group, tid)

    def test_only_existing_write_and_fopen_flag_sets_are_eligible(self):
        self.assertTrue(cuda_comm._allowed_flags(os.O_WRONLY | os.O_NOFOLLOW))
        self.assertTrue(cuda_comm._allowed_flags(os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC))
        for flags in (os.O_RDWR, os.O_RDONLY, os.O_WRONLY | os.O_APPEND,
                      os.O_WRONLY | os.O_TRUNC, os.O_WRONLY | os.O_EXCL, os.O_WRONLY | (1 << 40)):
            self.assertFalse(cuda_comm._allowed_flags(flags), flags)

    def test_duplicate_enrollment_closes_received_descriptors(self):
        duplicate = r"""
import array, socket, struct, sys
with socket.socket(fileno=int(sys.argv[1])) as channel:
    for _ in range(2):
        channel.sendmsg([struct.pack("=Q", 1)],
                        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [channel.fileno()]))])
"""
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with sender, receiver:
            receiver.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            before = set(os.listdir("/proc/self/fd"))
            subprocess.run([sys.executable, "-IBS", "-c", duplicate, str(sender.fileno())],
                           pass_fds=(sender.fileno(),), check=True, timeout=5)
            with self.assertRaisesRegex(cuda_comm.CommError, "duplicate"), ExitStack() as stack:
                cuda_comm._registration(receiver, stack)
            self.assertEqual(set(os.listdir("/proc/self/fd")), before)

    def test_substituted_listener_is_refused_and_owned_launcher_exits(self):
        sender = r"""
import array, os, socket, struct
with socket.socket(fileno=int(os.environ["BVQA_CUDA_COMM_CHANNEL"])) as channel:
    with open("/dev/null", "rb") as substitute:
        channel.sendmsg([struct.pack("=Q", 1)],
                        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [substitute.fileno()]))])
        assert channel.recv(1) != b"+"
"""
        result = subprocess.run([sys.executable, "-IBS", cuda_comm.__file__,
                                 sys.executable, "-IBS", "-c", sender],
                                capture_output=True, timeout=8, check=False)
        self.assertEqual(result.returncode, 5, result)
        self.assertIn(b"cuda_comm_listener_identity", result.stdout)

    def test_invalid_notification_after_target_open_cannot_deliver_fd(self):
        arch, nr, _ = cuda_comm._abi()
        request = (42, 19, 0, nr, arch, 0, (-100) & ((1 << 64) - 1), 1234, os.O_WRONLY, 0, 0, 0)
        with patch.object(cuda_comm, "_live", return_value=True), \
                patch.object(cuda_comm, "_open_proc", return_value=nullcontext(1)), \
                patch.object(cuda_comm, "_local_tid", return_value=3), \
                patch.object(cuda_comm, "_path", return_value=b"/proc/self/task/4/comm"), \
                patch.object(cuda_comm, "_target", return_value=nullcontext(2)), \
                patch.object(cuda_comm.fcntl, "ioctl") as ioctl:
            stale = OSError(errno.ENOENT, "notification disappeared")
            ioctl.side_effect = [0, 0, stale, stale]
            cuda_comm._handle(10, 12, 13, (18, 1), request)
        self.assertEqual([call.args[1] for call in ioctl.call_args_list], [cuda_comm.ID_VALID] * 4)

    def test_unreadable_or_unterminated_path_continues_without_injecting_a_descriptor(self):
        arch, nr, _ = cuda_comm._abi()
        for address, read in ((0, None), (1234, OSError(errno.EIO, "unmapped")), (1234, b"x" * 128)):
            with (
                self.subTest(address=address, read=read),
                patch.object(cuda_comm, "_live", return_value=True),
                patch.object(cuda_comm, "_open_proc", return_value=nullcontext(1)),
                patch.object(cuda_comm, "_local_tid", return_value=3),
                patch.object(cuda_comm, "_target") as target,
                patch.object(cuda_comm.fcntl, "ioctl") as ioctl,
                patch.object(cuda_comm.os, "pread") as pread,
            ):
                if isinstance(read, OSError):
                    pread.side_effect = read
                else:
                    pread.return_value = read
                request = (42, 19, 0, nr, arch, 0, (-100) & ((1 << 64) - 1), address,
                           os.O_WRONLY, 0, 0, 0)
                cuda_comm._handle(10, 12, 13, (18, 1), request)
                target.assert_not_called()
                self.assertNotIn(cuda_comm.ADD, [call.args[1] for call in ioctl.call_args_list])
                ioctl.assert_called_with(10, cuda_comm.SEND, cuda_comm.RESPONSE.pack(42, 0, 0, cuda_comm.CONTINUE))

    def test_target_lookup_skips_a_disappearing_non_target_thread(self):
        entries = [SimpleNamespace(name=name) for name in ("19", "20")]
        for gone in (FileNotFoundError(errno.ENOENT, "gone"), ProcessLookupError(errno.ESRCH, "gone")):
            with (
                self.subTest(error=gone.errno),
                patch.object(cuda_comm.os, "scandir", return_value=nullcontext(entries)),
                patch.object(cuda_comm, "_open_proc", side_effect=map(nullcontext, (1, 2, 3, 4))),
                patch.object(cuda_comm, "_local_tid", side_effect=[gone, 3, 3]),
                patch.object(cuda_comm.os, "fstat", return_value=SimpleNamespace(st_mode=0o100600)),
                patch.object(cuda_comm.fcntl, "fcntl", return_value=os.O_WRONLY),
                patch.object(cuda_comm, "_procfs") as procfs,
            ):
                with cuda_comm._target(9, (18, 1), 3) as fd:
                    self.assertEqual(fd, 4)
                procfs.assert_called_once_with(4)

    def test_syscall_and_fstatfs_use_native_abi_widths(self):
        abi = cuda_comm.LIBC
        self.assertEqual(abi.syscall.argtypes, (ctypes.c_long,))
        self.assertIs(abi.syscall.restype, ctypes.c_long)
        self.assertEqual(abi.fstatfs.argtypes, (ctypes.c_int, ctypes.c_void_p))
        self.assertIs(abi.fstatfs.restype, ctypes.c_int)
        with patch.object(cuda_comm, "LIBC") as libc:
            def sizes(_number, operation, _flags, output):
                if operation.value == 3:
                    output._obj[:] = (cuda_comm.NOTIFICATION.size, cuda_comm.RESPONSE.size, 64)
                    return 0
                return 9

            libc.syscall.side_effect = sizes
            with (
                patch.object(cuda_comm.os, "fstat", return_value=SimpleNamespace(st_dev=1)),
                patch.object(cuda_comm.os, "close") as close,
            ):
                self.assertEqual(cuda_comm._install_filter(), 9)
                libc.syscall.side_effect = None
                libc.syscall.return_value = 10
                with cuda_comm._open_proc(8, "status") as fd:
                    self.assertEqual(fd, 10)
                close.assert_called_once_with(10)
        get_sizes, install, open_proc = (call.args for call in libc.syscall.call_args_list)
        for args in (get_sizes, install):
            self.assertIs(type(args[0]), ctypes.c_long)
            self.assertIs(type(args[1]), ctypes.c_uint)
            self.assertIs(type(args[2]), ctypes.c_uint)
        self.assertEqual(tuple(map(type, open_proc[:3])), (ctypes.c_long, ctypes.c_int, ctypes.c_char_p))
        self.assertIs(type(open_proc[-1]), ctypes.c_size_t)

    def test_service_stops_before_custody_closes_and_late_failure_refuses(self):
        for fail in (False, True):
            events = []
            closed = threading.Event()
            owner = Mock(pid=23)
            owner.wait.return_value = 0
            owner.poll.return_value = 0

            def enroll(_parent, pid, custody):
                self.assertEqual(pid, owner.pid)
                def close():
                    events.append("closed")
                    closed.set()

                custody.callback(close)
                return (11, 12, 13, (14, 1))

            def service(_listener, _pidfd, _tasks, _group, stop=None):
                if stop is None:
                    self.assertTrue(closed.wait(2))
                else:
                    self.assertEqual(select.select([stop], [], [], 2)[0], [stop])
                events.append("stopped")
                if fail:
                    raise cuda_comm.CommError("late fixture failure")

            with (
                self.subTest(fail=fail),
                patch.object(cuda_comm.subprocess, "Popen", return_value=owner),
                patch.object(cuda_comm, "_enroll", side_effect=enroll),
                patch.object(cuda_comm, "_serve", side_effect=service),
            ):
                # Retain a receiver for the mocked owner's admission acknowledgement.
                pair = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                with (
                    pair[0],
                    pair[1],
                    socket.socket(fileno=os.dup(pair[1].fileno())) as peer,
                    patch.object(cuda_comm.socket, "socketpair", return_value=pair),
                ):
                    if fail:
                        with self.assertRaisesRegex(cuda_comm.CommError, "cuda_comm_service:late fixture failure"):
                            cuda_comm._supervise(["fixture"])
                    else:
                        self.assertEqual(cuda_comm._supervise(["fixture"]), 0)
                    self.assertEqual(peer.recv(1), b"+")
                self.assertEqual(events, ["stopped", "closed"])
                owner.kill.assert_not_called()


@unittest.skipUnless(os.environ.get("BVQA_CAPTIVE_CUDA_GATE_TESTS") == "1",
                     "opt-in offline captive gate controls")
class CudaCommGateTests(unittest.TestCase):
    def _admit(self, stack, enabled):
        run = stack.enter_context(containment.workspace())
        executable = run / "fixture-blender"
        executable.write_text("#!/usr/bin/python3 -IBS\n" + PROBE)
        executable.chmod(0o700)
        path = run / "err"
        process, _, _, receipt = stack.enter_context(worker.admitted_process(
            (executable, "-b", "on" if enabled else "off"), run, timeout=10, captive_cuda=True,
            own_thread_comm=enabled, devices=containment.CUDA_NODES, logs=(run / "out", path)))
        marker = run / "comm-ready"
        deadline = time.monotonic() + 8
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists(), path.read_text())
        self.assertEqual(marker.read_text(), "1")
        self.assertIs(receipt["own_thread_comm"], enabled)
        return process, receipt, run

    def _host_init(self, receipt):
        for path in Path("/proc").iterdir():
            if path.name.isdecimal():
                try:
                    if os.readlink(path / "ns/pid") != receipt["namespaces"]["pid"]:
                        continue
                    for line in (path / "status").read_text().splitlines():
                        if line.startswith("NStgid:") and line.split()[1:] == [path.name, "1"]:
                            return int(path.name)
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
        self.fail("no host identity for namespace PID one")

    def test_real_rename_negative_arm_and_distinct_host_tasks(self):
        with ExitStack() as stack:
            active, active_receipt, active_root = self._admit(stack, True)
            control, control_receipt, control_root = self._admit(stack, False)
            self.assertNotEqual(self._host_init(active_receipt), self._host_init(control_receipt))
            self.assertNotEqual(active_receipt["namespaces"]["pid"], control_receipt["namespaces"]["pid"])
            for key in ("devices", "capabilities", "landlock_abi", "no_new_privs", "seccomp"):
                self.assertEqual(active_receipt[key], control_receipt[key], key)
            for receipt, root in ((active_receipt, active_root), (control_receipt, control_root)):
                self.assertEqual([mount["target"] for mount in receipt["mounts"]
                                  if "rw" in mount["flags"] and mount["target"] not in receipt["devices"]],
                                 [str(root)])
            for process in (active, control):
                output, _ = process.communicate(b"done\n", timeout=5)
                self.assertEqual(process.returncode, 0, output)

    def test_supervisor_loss_reaps_namespace_init(self):
        with ExitStack() as stack:
            process, receipt, _ = self._admit(stack, True)
            fd = os.pidfd_open(self._host_init(receipt))
            stack.callback(os.close, fd)
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            self.assertFalse(poll.poll(0))
            process.kill()
            process.wait(timeout=5)
            self.assertTrue(poll.poll(5000))
