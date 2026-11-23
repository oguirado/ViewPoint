"""Opt-in namespace admission and benign extra-mount contrast on the host."""

from contextlib import contextmanager, nullcontext
from copy import deepcopy
import ctypes
import errno
from functools import partial
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from blender_visual_qa.drive import containment, launch, worker


PROBE = r"""
import errno, json, os, pathlib, socket, subprocess
workspace = pathlib.Path.cwd()
assert pathlib.Path(os.environ["HOME"]).is_relative_to(workspace)
assert pathlib.Path(os.environ["TMPDIR"]).is_relative_to(workspace)
assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
try:
    with open(workspace / "input", "r+b"):
        raise AssertionError("input opened writable")
except OSError as exc:
    assert exc.errno == errno.EROFS
try:
    socket.socket()
    raise AssertionError("external socket created")
except PermissionError:
    pass
try:
    socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
    raise AssertionError("captive socket created in stock headless mode")
except PermissionError:
    pass
try:
    socket.socketpair()
    raise AssertionError("socket pair created")
except PermissionError:
    pass
mount = os.readlink("/proc/self/ns/mnt")
code = '''
import os, pathlib, sys
assert os.readlink("/proc/self/ns/mnt") == sys.argv[1]
status = pathlib.Path("/proc/self/status").read_text()
assert "Seccomp:\\t2\\n" in status and "CapEff:\\t0000000000000000\\n" in status
pathlib.Path("marker").write_text("descendant")
'''
subprocess.run(["/usr/bin/python3", "-IBS", "-c", code, mount], check=True)
print(json.dumps({"mount": mount, "device": workspace.stat().st_dev}))
"""


WRITE_PROBE = """
import errno, json, os, pathlib, subprocess, sys

workspace = pathlib.Path.cwd()
if sys.argv[1:2] == ["--disable-autoexec"]:
    del sys.argv[1:4]
worktree, host_home, pyc = map(pathlib.Path, sys.argv[1:4])
parent_mount = sys.argv[4] if len(sys.argv) > 4 else None
mount = os.readlink("/proc/self/ns/mnt")
assert parent_mount is None or mount == parent_mount

denied = {}
for label, target in {
    "tmp": pathlib.Path("/tmp") / pyc.name,
    "worktree": worktree / pyc.name,
    "host_home": host_home / pyc.name,
    "bytecode": pyc,
}.items():
    try:
        with target.open("xb"):
            pass
    except OSError as exc:
        expected = (errno.EROFS, errno.ENOENT) if label == "host_home" else (errno.EROFS,)
        assert exc.errno in expected, (target, exc)
        denied[label] = errno.errorcode[exc.errno]
    else:
        raise AssertionError(f"write outside the RAM path succeeded: {label} ({target})")

assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1" and sys.dont_write_bytecode
writes = {}
for name in ("HOME", "TMPDIR"):
    directory = pathlib.Path(os.environ[name]).resolve(strict=True)
    assert directory.is_relative_to(workspace), (name, directory)
    assert directory.stat().st_dev == workspace.stat().st_dev
    filename = ("descendant" if parent_mount else "worker") + "-" + pyc.name
    with (directory / filename).open("xb") as output:
        output.write(b"RAM")
    writes[name] = str(directory.relative_to(workspace) / filename)

print(json.dumps({"writes_denied": denied, "writes_in_ram": writes,
                  "mount": mount, "descendant": bool(parent_mount)}), flush=True)
if parent_mount is None:
    subprocess.run([sys.executable, __file__, *sys.argv[1:4], mount], check=True)
"""


CAPTIVE_PROBE = r"""
import ctypes, errno, os, pathlib, socket, sys
name, host_name = ("\0" + label for label in sys.argv[-2:])
libc = ctypes.CDLL(None, use_errno=True)
kind = socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC

def refused(call, *args):
    assert call(*args) == -1 and ctypes.get_errno() == errno.EPERM, call

refused(libc.socket, socket.AF_INET, kind, 0)
refused(libc.socket, socket.AF_UNIX, socket.SOCK_SEQPACKET, 0)
refused(libc.socket, socket.AF_UNIX, kind | socket.SOCK_NONBLOCK, 0)
refused(libc.socket, socket.AF_UNIX, kind, 1)
refused(libc.socketpair, socket.AF_UNIX, kind, 0, None)
with socket.socket(socket.AF_UNIX, kind) as listener:
    pathname = pathlib.Path("pathname.sock")
    try:
        listener.bind(str(pathname))
    except OSError as exc:
        assert exc.errno == errno.EACCES
    else:
        raise AssertionError("pathname bind succeeded")
    assert not pathname.exists()
    listener.bind(name)
    listener.listen()
    for call in (libc.accept, libc.accept4, libc.sendto, libc.sendmsg, libc.sendmmsg):
        refused(call, listener.fileno(), None, 0, 0, None, 0)
    with socket.socket(socket.AF_UNIX, kind) as outgoing:
        try:
            outgoing.connect(host_name)
        except OSError as exc:
            assert exc.errno == errno.EPERM
        else:
            raise AssertionError("outbound connect succeeded")
    try:
        pathlib.Path("/forbidden-captive-write").write_text("outside")
    except OSError as exc:
        assert exc.errno == errno.EROFS
    else:
        raise AssertionError("write outside RAM succeeded")
    assert set(os.listdir("/dev")) == {"nvidia0", "nvidiactl", "nvidia-uvm", "null", "shm"}
    sys_root = pathlib.Path("/sys")
    modules = sys_root / "module"
    assert {path.name for path in sys_root.iterdir()} == {"module"}
    assert {path.name for path in modules.iterdir()} == {"nvidia", "nvidia_uvm"}
    for module in modules.iterdir():
        assert {path.name for path in module.iterdir()} == {"initstate"}
        state = module / "initstate"
        assert state.read_bytes() == b"live\n"
        assert state.stat().st_dev == pathlib.Path.cwd().stat().st_dev
        assert os.statvfs(state).f_flag & os.ST_RDONLY
        try:
            state.write_bytes(b"not live\n")
        except OSError as exc:
            assert exc.errno == errno.EROFS
        else:
            raise AssertionError("module snapshot opened writable")
    ready = pathlib.Path("ready.tmp")
    ready.write_text("ordinary RAM file")
    ready.replace("ready")
    assert sys.stdin.buffer.readline() == b"done\n"
"""


class CaptiveCudaAdmissionTests(unittest.TestCase):
    @contextmanager
    def _device_fixture(self, *, missing=False, foreign=False):
        numbers = dict(zip(containment.CUDA_NODES, (os.makedev(195, 0), os.makedev(195, 255),
                                                   os.makedev(511, 0), os.makedev(1, 3))))
        nodes = {name: SimpleNamespace(st_mode=stat.S_IFCHR, st_dev=2, st_ino=inode, st_rdev=number)
                 for inode, (name, number) in enumerate(numbers.items(), start=10)}
        original = Path.lstat

        def lstat(path):
            if str(path) not in nodes:
                return original(path)
            if path == Path("/dev/nvidia0"):
                if missing:
                    raise FileNotFoundError("missing node")
                if foreign:
                    return SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=os.makedev(1, 3))
            return nodes[str(path)]

        with patch.object(Path, "lstat", lstat), \
                patch.object(containment, "_captive_device_numbers", return_value=numbers), \
                patch.object(worker, "_cuda_module_state_aliases", return_value=[]):
            yield {name: [[node.st_dev, node.st_ino], node.st_rdev] for name, node in nodes.items()}

    def test_module_state_snapshot_refuses_unloaded_and_missing_modules(self):
        with containment.workspace() as root:
            sources = tuple(root / module / "initstate" for module in ("nvidia", "nvidia_uvm"))
            for source in sources:
                source.parent.mkdir()
                source.write_bytes(b"live\n")
            snapshots = root / "snapshots"
            snapshots.mkdir()
            with patch.object(worker, "CUDA_MODULE_STATES", sources):
                self.assertEqual(worker._cuda_module_state_aliases(snapshots),
                                 [(snapshots / target.parent.name, target)
                                  for target in containment.CUDA_MODULE_STATES])
                self.assertEqual({path.read_bytes() for path in snapshots.iterdir()}, {b"live\n"})
                for path in snapshots.iterdir():
                    path.unlink()
                sources[0].write_bytes(b"coming\n")
                with self.assertRaisesRegex(containment.ContainmentError, "module_not_live:nvidia"):
                    worker._cuda_module_state_aliases(snapshots)
                sources[0].unlink()
                with self.assertRaisesRegex(containment.ContainmentError, "module_state_unavailable:nvidia"):
                    worker._cuda_module_state_aliases(snapshots)

    def test_captive_mount_interface_refuses_sysfs_sources_targets_and_aliases(self):
        with containment.workspace() as run, self._device_fixture():
            executable = run / "executable"
            executable.touch(mode=0o700)
            link = run / "sys-link"
            link.symlink_to("/sys")
            alias = run / "effective-sysfs"
            alias.mkdir()
            host_mounts = [*containment._mounts(), {"target": str(alias), "fs": "sysfs"}]
            target = Path("/run/bvqa-source")
            for readonly, aliases in (
                ((Path("/sys"),), ()),
                ((Path("/"),), ()),
                ((link,), ()),
                ((alias,), ()),
                ((), ((Path("/sys/module"), target),)),
                ((), ((link / "module", target),)),
                ((), ((alias, target),)),
                ((), ((executable, Path("/")),)),
                ((), ((executable, Path("/sys")),)),
                ((), ((executable, Path("/run/../sys/module")),)),
                ((), ((executable, link / "module"),)),
                ((), ((executable, worker.CUDA_MODULE_STATES[0]),)),
            ):
                with (
                    self.subTest(readonly=readonly, aliases=aliases),
                    patch.object(containment, "_mounts", return_value=host_mounts),
                    self.assertRaisesRegex(containment.ContainmentError, "captive_cuda_external_sysfs"),
                ):
                    containment.command([str(executable)], run, readonly, run / "log",
                                        aliases=aliases, captive_cuda=True, devices=containment.CUDA_NODES)
            for settings in (
                {"runtime": (Path("/sys"),)},
                {"runtime_aliases": ((link, target),)},
                {"runtime_aliases": ((executable, worker.CUDA_MODULE_STATES[0]),)},
            ):
                with self.subTest(settings=settings), self.assertRaisesRegex(
                        containment.ContainmentError, "captive_cuda_external_sysfs"):
                    with worker.command([executable], run, captive_cuda=True, devices=containment.CUDA_NODES,
                                        **settings):
                        self.fail("external sysfs produced a launcher")

    def test_captive_effective_mounts_refuse_sysfs_or_a_nonexact_snapshot(self):
        snapshots = list(map(str, worker.CUDA_MODULE_STATES))
        host_device = os.makedev(0, 4)
        identities = {"/ram": [host_device, 1], **{target: [host_device, 2] for target in snapshots}}
        config = {"workspace": "/ram", "captive_cuda": True, "module_state": snapshots,
                  "identities": identities}

        def mount(target, fs, access):
            return dict(target=target, fs=fs, flags=[access, "nodev", "nosuid"],
                        device="0:4", propagation=[])

        base = [mount("/", "tmpfs", "ro"), mount("/proc", "proc", "ro"), mount("/ram", "tmpfs", "rw")]
        valid = [mount(target, "tmpfs", "ro") for target in snapshots]
        broad = "/run/aliased-sysfs"
        broad_identities = {"/ram": identities["/ram"], broad: [9, 5]}
        with (
            patch.object(containment, "_identity", side_effect=broad_identities.__getitem__),
            self.assertRaisesRegex(containment.ContainmentError, "external_sysfs"),
        ):
            containment._verify_mounts([*base, mount(broad, "sysfs", "ro")],
                                       dict(workspace="/ram", captive_cuda=True, identities=broad_identities))
        with (
            patch.object(containment, "_identity", side_effect=identities.__getitem__),
            patch.object(containment, "_verify_module_snapshot") as snapshot,
        ):
            containment._verify_mounts([*base, *valid], config)
            self.assertEqual([call.args for call in snapshot.call_args_list],
                             [(Path(target), host_device) for target in snapshots])
        for target, fs, access, reason in (
            ("/sys", "tmpfs", "ro", "external_sysfs"),
            ("/sys/module/nvidia", "tmpfs", "ro", "external_sysfs"),
            (snapshots[0], "sysfs", "ro", "external_sysfs"),
            (snapshots[0], "ext4", "ro", "module_snapshot_mount"),
            (snapshots[0], "tmpfs", "rw", "module_snapshot_mount"),
        ):
            with self.subTest(target=target, fs=fs, access=access), self.assertRaisesRegex(
                    containment.ContainmentError, reason):
                containment._verify_mounts([*base, valid[1], mount(target, fs, access)], config)
        with self.assertRaisesRegex(containment.ContainmentError, "module_snapshot_mount"):
            containment._verify_mounts([*base, valid[1]], config)

    def test_snapshots_are_pinned_outside_workspace_and_removed_on_context_exit(self):
        original = worker._cuda_module_state_aliases
        with containment.workspace() as run, self._device_fixture():
            executable = run / "executable"
            executable.touch(mode=0o700)
            # Use the real snapshot builder with offline synthetic module state.
            sources = tuple(run / module / "initstate" for module in ("nvidia", "nvidia_uvm"))
            for source in sources:
                source.parent.mkdir()
                source.write_bytes(b"live\n")
            with (
                patch.object(worker, "CUDA_MODULE_STATES", sources),
                patch.object(worker, "_cuda_module_state_aliases", wraps=original),
                worker.command([executable], run, captive_cuda=True, own_thread_comm=False,
                               devices=containment.CUDA_NODES) as (launcher, _, _, _),
            ):
                config = json.loads(launcher[launcher.index(str(containment.GATE), launcher.index("--")) + 1])
                self.assertEqual(config["module_state"], list(map(str, containment.CUDA_MODULE_STATES)))
                sources = []
                for target in config["module_state"]:
                    index = launcher.index(target)
                    source = Path(launcher[index - 1])
                    sources.append(source)
                    self.assertEqual(launcher[index - 2], "--ro-bind")
                    self.assertEqual(source.parent.parent, run.parent)
                    self.assertFalse(source.is_relative_to(run))
                    self.assertEqual(source.read_bytes(), b"live\n")
                    self.assertEqual(config["identities"][target], containment._identity(source))
                    self.assertFalse(source.stat().st_mode & 0o222)
            for source in sources:
                self.assertFalse(source.parent.exists())

    def test_missing_foreign_display_and_invalid_admission_are_refused(self):
        for devices, display, selected in (
            ((), None, True),
            (containment.CUDA_NODES[:-1], None, True),
            ((*containment.CUDA_NODES, "/dev/dri/renderD128"), None, True),
            (containment.CUDA_NODES, 90, True),
            (containment.CUDA_NODES, None, "true"),
        ):
            with self.subTest(devices=devices, display=display, selected=selected):
                with self.assertRaisesRegex(containment.ContainmentError, "captive_cuda"):
                    containment.command([], None, (), None, devices=devices, display=display, captive_cuda=selected)
        for key in ("DISPLAY", "WAYLAND_SOCKET", "DBUS_SESSION_BUS_ADDRESS", "LD_PRELOAD", "XDG_RUNTIME_DIR"):
            with self.subTest(key=key), self.assertRaisesRegex(containment.ContainmentError, "external_environment"):
                with worker.command([], None, env={key: "fixture"}, devices=containment.CUDA_NODES, captive_cuda=True):
                    self.fail("external environment admitted")

    def test_device_node_snapshot_refuses_missing_or_foreign_character_device(self):
        executable = str(Path("/usr/bin/python3").resolve(strict=True))
        with containment.workspace() as run:
            for options, reason in (({"missing": True}, "launcher_prerequisite"),
                                    ({"foreign": True}, "captive_cuda_device_number")):
                with self.subTest(reason=reason), self._device_fixture(**options), \
                        self.assertRaisesRegex(containment.ContainmentError, reason):
                    containment.command([executable], run, (), run / "log",
                                        devices=containment.CUDA_NODES, captive_cuda=True)

    def test_gpu_identity_inventory_and_uvm_guards(self):
        gpu = "Model: NVIDIA GB10\nDevice Minor: 0\n"
        uvm = "511 nvidia-uvm\n"
        cases = (
            ((gpu,), ("nvidia0",), uvm, None),
            ((), ("nvidia0",), uvm, "captive_cuda_requires_one_gb10"),
            ((gpu, gpu), ("nvidia0",), uvm, "captive_cuda_requires_one_gb10"),
            ((gpu,), (), uvm, "captive_cuda_requires_one_gb10"),
            ((gpu,), ("nvidia0", "nvidia1"), uvm, "captive_cuda_requires_one_gb10"),
            ((gpu.replace("GB10", "Other"),), ("nvidia0",), uvm, "captive_cuda_gpu_identity"),
            ((gpu.replace("Minor: 0", "Minor: 1"),), ("nvidia0",), uvm, "captive_cuda_gpu_identity"),
            ((gpu,), ("nvidia0",), "", "captive_cuda_uvm_identity"),
            ((gpu,), ("nvidia0",), uvm + "512 nvidia-uvm\n", "captive_cuda_uvm_identity"),
        )
        for information, adapters, uvm_entries, reason in cases:
            with self.subTest(information=information, adapters=adapters, uvm=uvm_entries), \
                    tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "dev").mkdir()
                (root / "proc/driver/nvidia/gpus").mkdir(parents=True)
                for index, text in enumerate(information):
                    path = root / f"proc/driver/nvidia/gpus/{index}/information"
                    path.parent.mkdir()
                    path.write_text(text)
                for name in adapters:
                    (root / "dev" / name).touch()
                (root / "proc/devices").write_text(
                    "Character devices:\n" + uvm_entries + "\nBlock devices:\n513 nvidia-uvm\n")
                with patch.object(containment, "Path", side_effect=lambda path: root / Path(path).relative_to("/")):
                    if reason:
                        with self.assertRaisesRegex(containment.ContainmentError, reason):
                            containment._captive_device_numbers()
                    else:
                        self.assertEqual(containment._captive_device_numbers(), {
                            "/dev/nvidia0": os.makedev(195, 0),
                            "/dev/nvidiactl": os.makedev(195, 255),
                            "/dev/nvidia-uvm": os.makedev(511, 0),
                            "/dev/null": os.makedev(1, 3),
                        })

    def test_captive_executable_requires_an_unambiguous_file_identity(self):
        with containment.workspace() as run, self._device_fixture():
            executable = run / "executable"
            executable.touch(mode=0o700)
            target = Path("/run/blender-input/bin/blender")
            link = run / "symlink"
            link.symlink_to(executable)
            directory_link = run / "directory-link"
            directory_link.symlink_to(run)

            def launch_command(argv, aliases=()):
                return containment.command(argv, run, (), run / "log", aliases=aliases,
                                           devices=containment.CUDA_NODES, captive_cuda=True)

            for path, aliases in ((executable, ()), (target, ((executable, target),))):
                with self.subTest(path=path):
                    launcher = launch_command([str(path)], aliases)
                    config = json.loads(launcher[-2])
                    info = executable.stat()
                    self.assertEqual(config["executable_identity"], [info.st_dev, info.st_ino])
                    self.assertEqual(launcher[-1], str(path))
            for argv, aliases in (
                ([], ()),
                ([""], ()),
                (["executable"], ()),
                ([str(run / "missing")], ()),
                ([str(link)], ()),
                ([str(directory_link / executable.name)], ()),
                ([str(target)], ((run / "missing", target),)),
                ([str(target)], ((link, target),)),
                ([str(target)], ((executable, target), (executable, str(target)))),
                ([str(target)], ((executable, target), (executable, "/" + str(target)))),
                ([str(target)], ((executable, target), (executable, target.relative_to("/")))),
                ([str(target)], ((executable, target), (executable, target.parent / ".." / "bin" / target.name))),
                ([str(target)], ((run, target.parent),)),
                ([str(target)], ((run, target.parent), (executable, target))),
            ):
                with self.subTest(argv=argv, aliases=aliases), \
                        self.assertRaisesRegex(containment.ContainmentError, "^captive_cuda_runtime_executable$"):
                    launch_command(argv, aliases)

            for argv, explicit in (([], None), ([""], None), ([str(run / "missing")], None),
                                   (["nice", "-n", "0"], executable)):
                with self.subTest(argv=argv, explicit=explicit), \
                        self.assertRaisesRegex(containment.ContainmentError, "^captive_cuda_runtime_executable$"):
                    with worker.command(argv, run, executable=explicit,
                                        devices=containment.CUDA_NODES, captive_cuda=True):
                        self.fail("missing executable produced a launcher")

    def test_captive_gate_checks_exact_device_mapping_before_mounts(self):
        with self._device_fixture() as devices:
            reordered = json.loads(json.dumps(devices, sort_keys=True))
            self.assertNotEqual(tuple(reordered), containment.CUDA_NODES)
            fixtures = (
                (reordered, "fixture_mounts_checked"),
                ({key: value for key, value in reordered.items() if key != "/dev/null"},
                 "captive_cuda_requires_headless_exact_devices"),
                ({**reordered, "/dev/nvidia1": reordered["/dev/nvidia0"]},
                 "captive_cuda_requires_headless_exact_devices"),
                (list(devices), "captive_cuda_requires_headless_exact_devices"),
                ({**reordered, "/dev/null": None}, "captive_cuda_requires_headless_exact_devices"),
            )
            for candidate, reason in fixtures:
                with self.subTest(reason=reason, candidate=candidate), io.StringIO() as output, \
                        patch.object(containment, "_mounts", return_value=[]), \
                        patch.object(containment, "_verify_mounts",
                                     side_effect=containment.ContainmentError("fixture_mounts_checked")) as verify, \
                        patch.object(containment.sys, "stdout", output):
                    self.assertEqual(containment._run({"captive_cuda": True, "devices": candidate}, []), 5)
                    output.seek(0)
                    self.assertEqual(json.load(output)["reason"], reason)
                    if reason == "fixture_mounts_checked":
                        verify.assert_called_once()
                    else:
                        verify.assert_not_called()

    def test_landlock_missing_and_enforcement_failure(self):
        for results, reason in (([-1], "abi"), ([1, -1], "ruleset"), ([1, 11, -1], "enforce")):
            with self.subTest(reason=reason):
                syscall = Mock(side_effect=results)
                with patch.object(containment.ctypes, "CDLL", return_value=SimpleNamespace(syscall=syscall)), \
                        patch.object(containment.os, "close") as close:
                    with self.assertRaisesRegex(containment.ContainmentError, "captive_cuda_landlock_" + reason):
                        containment._restrict_filesystem_sockets()
                if reason == "enforce":
                    close.assert_called_once_with(11)
                else:
                    close.assert_not_called()

    def test_landlock_uses_native_variadic_argument_types(self):
        syscall = Mock(side_effect=[8, 11, 0])
        with patch.object(containment.ctypes, "CDLL", return_value=SimpleNamespace(syscall=syscall)), \
                patch.object(containment.os, "close") as close:
            self.assertEqual(containment._restrict_filesystem_sockets(), 8)
        close.assert_called_once_with(11)
        self.assertEqual(syscall.argtypes, (ctypes.c_long,))
        self.assertIs(syscall.restype, ctypes.c_long)
        version, ruleset, enforce = [call.args for call in syscall.call_args_list]
        for number, pointer, size, flags in (version, ruleset):
            self.assertIsInstance(number, ctypes.c_long)
            self.assertEqual(number.value, 444)
            self.assertIsInstance(pointer, ctypes.c_void_p)
            self.assertIsInstance(size, ctypes.c_size_t)
            self.assertIsInstance(flags, ctypes.c_uint)
        self.assertIsNone(version[1].value)
        self.assertEqual((version[2].value, version[3].value), (0, 1))
        self.assertEqual((ruleset[2].value, ruleset[3].value), (ctypes.sizeof(ctypes.c_uint64), 0))
        self.assertEqual(ctypes.cast(ruleset[1], ctypes.POINTER(ctypes.c_uint64)).contents.value, 1 << 9)
        number, fd, flags = enforce
        self.assertIsInstance(number, ctypes.c_long)
        self.assertEqual(number.value, 446)
        self.assertIsInstance(fd, ctypes.c_int)
        self.assertIsInstance(flags, ctypes.c_uint)
        self.assertEqual((fd.value, flags.value), (11, 0))

    def test_admitted_process_checks_captive_receipt_against_launcher_pins(self):
        with containment.workspace() as run, self._device_fixture() as devices:
            executable = run / "executable"
            executable.touch(mode=0o700)
            link = run / "supported-executable-link"
            link.symlink_to(executable)
            info = executable.stat()
            identity = [info.st_dev, info.st_ino]
            admitted = {"admitted": True, "captive_cuda": True, "landlock_abi": 8, "own_thread_comm": True,
                        "devices": devices, "executable_identity": identity}

            @contextmanager
            def owner(launcher, **kwargs):
                if captive:
                    config = json.loads(launcher[launcher.index("--") + 4])
                    self.assertEqual(config["executable_identity"], identity)
                    self.assertEqual(config["devices"], devices)
                recv, send = os.pipe()
                with os.fdopen(recv, "rb") as stdout:
                    with os.fdopen(send, "wb") as writer:
                        writer.write(wire)
                    yield SimpleNamespace(stdout=stdout, poll=lambda: 0)

            missing_fields = [
                {key: value for key, value in admitted.items() if key != field}
                for field in ("captive_cuda", "landlock_abi", "executable_identity", "devices", "own_thread_comm")
            ]
            invalid_fields = (
                {"captive_cuda": False}, {"landlock_abi": True}, {"landlock_abi": 0}, {"own_thread_comm": False},
                {"executable_identity": None}, {"executable_identity": [identity[0]]},
                {"executable_identity": [identity[0], str(identity[1])]},
                {"executable_identity": [identity[0], True]}, {"executable_identity": [identity[0], -1]},
                {"executable_identity": [identity[0], identity[1] + 1]}, {"devices": None},
                {"devices": {}}, {"devices": dict.fromkeys(devices)},
                {"devices": {**devices, "/dev/nvidia1": devices["/dev/nvidia0"]}},
                {"devices": {key: value for key, value in devices.items() if key != "/dev/null"}},
            )
            invalid_receipts = [*missing_fields, *({**admitted, **fields} for fields in invalid_fields)]
            for name, (node, number) in devices.items():
                for value in ([[node[0], node[1] + 1], number], [node, number + 1],
                              [node, False], [[True, node[1]], number], [node, str(number)]):
                    record = deepcopy(admitted)
                    record["devices"][name] = value
                    invalid_receipts.append(record)
            fixtures = [(True, b"", "launcher_protocol"),
                        (True, b"invalid\n", "launcher_protocol"),
                        (True, json.dumps(admitted, sort_keys=True).encode(), None),
                        (False, b'{"admitted":true}', None)]
            fixtures.extend((True, json.dumps(record).encode(), "captive_cuda_admission_receipt")
                            for record in invalid_receipts)
            for captive, wire, reason in fixtures:
                with self.subTest(captive=captive, wire=wire, reason=reason):
                    process = worker.admitted_process(
                        [link], run, timeout=1, owner=owner, captive_cuda=captive,
                        devices=containment.CUDA_NODES if captive else ())
                    if reason:
                        with self.assertRaisesRegex(containment.ContainmentError, reason), process:
                            self.fail("invalid admission yielded a worker")
                    else:
                        with process as (_, _, _, evidence):
                            self.assertEqual(evidence, admitted if captive else {"admitted": True})


class LauncherPrerequisiteTests(unittest.TestCase):
    def test_asset_refuses_missing_launcher_tools(self):
        argv = ["blender", "--python", "bpy_side/scene_example.py", "--"]
        cases = (
            ("unshare", FileNotFoundError(errno.ENOENT, "missing", "/usr/bin/unshare"), "launcher_prerequisite"),
            ("bwrap", None, "launcher_protocol"),
        )
        for tool, launch_error, reason in cases:
            recv, send = os.pipe()
            with self.subTest(tool=tool), os.fdopen(recv, "rb") as output:
                with os.fdopen(send, "wb") as diagnostic:
                    if tool == "bwrap":
                        diagnostic.write(b"unshare: failed to execute /usr/bin/bwrap\n")
                child = SimpleNamespace(stdout=output, poll=lambda: 1)
                process = Mock(side_effect=launch_error, return_value=nullcontext(child))
                launcher = ["/usr/bin/unshare", "/usr/bin/bwrap"]
                with patch.object(worker, "command", return_value=nullcontext((launcher, {}, None, None))), \
                     patch.object(worker, "admitted_process", partial(worker.admitted_process, owner=process)):
                    rc, stdout, stderr = worker.asset(argv, 1, work=Path("."))
                self.assertEqual((rc, stdout), (2, ""))
                self.assertTrue(stderr.startswith(f"containment:{reason}:"), stderr)
                process.assert_called_once()


@unittest.skipUnless(os.environ.get("BVQA_CAPTIVE_CUDA_GATE_TESTS") == "1", "opt-in offline gate probe on GB10")
class CaptiveCudaGateTests(unittest.TestCase):
    def test_synthetic_worker_and_private_abstract_listener(self):
        name = "bvqa-captive-" + uuid.uuid4().hex
        host_name = name + "-host"
        kind = socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC
        with containment.workspace() as run, socket.socket(socket.AF_UNIX, kind) as host:
            host.bind("\0" + host_name)
            host.listen()
            with socket.socket(socket.AF_UNIX, kind) as client:
                client.connect("\0" + host_name)
            executable = run / "fixture-blender"
            executable.write_text("#!/usr/bin/python3 -IBS\n" + CAPTIVE_PROBE)
            executable.chmod(0o700)
            with worker.admitted_process((executable, "-b", name, host_name), run, timeout=20,
                                         devices=containment.CUDA_NODES, captive_cuda=True) as child:
                proc, _, error_log, record = child
                self.assertIs(record["captive_cuda"], True)
                self.assertGreaterEqual(record["landlock_abi"], 1)
                info = executable.stat()
                self.assertEqual(record["executable_identity"], [info.st_dev, info.st_ino])
                self.assertEqual(set(record["devices"]), set(containment.CUDA_NODES))
                self.assertTrue(all(record["namespaces"][key] != record["parent_namespaces"][key]
                                    for key in ("net", "ipc", "mnt", "pid")))
                deadline = time.monotonic() + 10
                ready = run / "ready"
                while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(ready.read_text() if ready.exists() else None, "ordinary RAM file",
                                 error_log.read_text() if error_log.exists() else "probe log missing")
                with socket.socket(socket.AF_UNIX, kind) as foreign:
                    with self.assertRaises(OSError) as refused:
                        foreign.connect("\0" + name)
                    self.assertEqual(refused.exception.errno, errno.ECONNREFUSED)
                    foreign.bind("\0" + name)
                proc.communicate(b"done\n", timeout=10)
                self.assertEqual(proc.returncode, 0, error_log.read_text())
            worker.verify_outputs(run)

    def test_pathname_socket_already_in_workspace_refuses_before_exec(self):
        with containment.workspace() as run, socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as existing:
            (run / "xdg" / "tmp").mkdir(parents=True)
            existing.bind(str(run / "host.sock"))
            log = run / "probe.log"
            argv = containment.command([str(Path("/usr/bin/python3").resolve()), "-IBS", "-c",
                                        "raise AssertionError('started')"], run, (), log,
                                       devices=containment.CUDA_NODES, captive_cuda=True)
            env = containment.worker_environment(launch.declared_env(run, 1), run)
            result = subprocess.run(argv, env=env, input=b"", stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=15)
            self.assertEqual(result.returncode, 5, result.stdout)
            self.assertEqual(json.loads(result.stdout)["reason"], "captive_cuda_external_socket")
            self.assertFalse(log.exists())


@unittest.skipUnless(os.environ.get("BVQA_DRIVE_BLENDER_TESTS") == "1", "opt-in Linux namespace probe")
class DriveContainmentTests(unittest.TestCase):
    def test_worker_and_descendant_deny_writes_outside_ram_paths(self):
        worktree = Path(__file__).resolve().parents[3]
        host_home = Path.home().resolve()
        pyc = containment.GATE.with_name(f"storage-probe-{uuid.uuid4().hex}.pyc")
        with containment.workspace() as run:
            executable = run / "fixture-blender"
            executable.write_text("#!/usr/bin/python3\n" + WRITE_PROBE)
            executable.chmod(0o700)
            env = dict(launch.declared_env(run, 1), HOME=str(host_home), TMPDIR="/tmp")
            rc, stdout, stderr, record = worker.run((executable, worktree, host_home, pyc), run, 15, env=env)
            self.assertEqual(rc, 0, stderr.decode())
            observed = [json.loads(line) for line in stdout.splitlines()]
            self.assertEqual([case["descendant"] for case in observed], [False, True])
            self.assertEqual({case["mount"] for case in observed}, {record["namespaces"]["mnt"]})
            print(stdout.decode().strip())

    @unittest.skipUnless(Path("/usr/bin/Xvfb").is_file(), "requires the stock software display")
    def test_stock_software_display_stays_on_private_unix_transport(self):
        probe = """
import errno, os, pathlib, socket
number = os.environ["DISPLAY"].removeprefix(":")
with socket.socket(socket.AF_UNIX) as client:
    client.connect(chr(0) + "/tmp/.X11-unix/X" + number)
assert not pathlib.Path("/tmp/.X11-unix").exists()
assert pathlib.Path("/var/lib/xkb").resolve() == pathlib.Path(os.environ["TMPDIR"]) / "xkb"
try:
    with open("/tmp/pr1-display-probe", "xb"):
        pass
except OSError as exc:
    assert exc.errno == errno.EROFS
else:
    raise AssertionError("literal /tmp writable in display mode")
try:
    socket.socket(socket.AF_INET)
except PermissionError:
    pass
else:
    raise AssertionError("IP socket created")
"""
        with containment.workspace() as run:
            executable = run / "fixture-blender"
            executable.write_text("#!/usr/bin/python3 -IBS\n" + probe)
            executable.chmod(0o700)
            rc, stdout, stderr, record = worker.run((executable, "-b"), run, 30, display=94)
            self.assertEqual(rc, 0, (stdout, stderr))
            self.assertEqual(record["display"]["transport"], "private-unix")
            self.assertEqual(record["display"]["socket"], "@/tmp/.X11-unix/X94")
            self.assertEqual(set(record["devices"]), {"/dev/null"})
            self.assertNotIn("captive_cuda", record)

    @unittest.skipUnless(Path("/usr/bin/Xvfb").is_file(), "requires the stock software display")
    def test_private_display_refuses_existing_workspace_socket(self):
        with containment.workspace() as run, socket.socket(socket.AF_UNIX) as existing:
            existing.bind(str(run / "foreign.sock"))
            executable = run / "fixture-blender"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            with self.assertRaisesRegex(containment.ContainmentError, "external_display_socket"):
                worker.run((executable, "-b"), run, 15, display=94)

    def test_single_writable_workspace_and_extra_tmpfs_contrast(self):
        for extra in (False, True):
            with self.subTest(extra=extra), containment.workspace() as run:
                (run / "xdg" / "tmp").mkdir(parents=True)
                source, log = run / "input", run / "probe.log"
                source.write_text("read-only fixture")
                argv = containment.command(["/usr/bin/python3", "-IBS", "-c", PROBE],
                                           run, (source,), log)
                if extra:
                    index = argv.index("--remount-ro", argv.index("--chdir"))
                    argv[index:index] = ["--tmpfs", "/extra-ram"]
                env = containment.worker_environment(launch.declared_env(run, 1), run)
                result = subprocess.run(argv, env=env,
                                        input=b"", stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, timeout=15)
                record = json.loads(result.stdout)
                writable = {m["target"]: m["fs"] for m in record["mounts"] if "rw" in m["flags"]}
                self.assertIn(writable[str(run)], containment.RAM_FILESYSTEMS)
                self.assertEqual(source.read_text(), "read-only fixture")
                if extra:
                    self.assertEqual(result.returncode, 5)
                    self.assertEqual(writable, {str(run): writable[str(run)], "/extra-ram": "tmpfs"})
                    self.assertFalse(record["admitted"])
                    self.assertIn("writable_mounts:", record["reason"])
                    self.assertFalse(log.exists())
                    self.assertFalse((run / "marker").exists())
                else:
                    self.assertEqual(result.returncode, 0, log.read_text())
                    self.assertEqual(list(writable), [str(run)])
                    containment.admission(result.stdout)
                    rendered = json.loads(log.read_text())
                    self.assertEqual(rendered["mount"], record["namespaces"]["mnt"])
                    self.assertEqual(rendered["device"], run.stat().st_dev)
                    self.assertEqual(record["workspace_identity"], [run.stat().st_dev, run.stat().st_ino])
                    self.assertEqual((run / "marker").read_text(), "descendant")
                print(json.dumps({"extra_ram": extra, "rc": result.returncode, **record}, sort_keys=True))
