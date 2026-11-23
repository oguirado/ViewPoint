"""Managed Blender launches outside DRIVE, with trusted output collection."""

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import tempfile
import time

from . import containment
from .launch import declared_env


ROOT = Path(__file__).resolve().parents[2]
MESA_EGL = Path("/usr/share/glvnd/egl_vendor.d/50_mesa.json")
CUDA_MODULE_STATES = containment.CUDA_MODULE_STATES


def _cuda_module_state_aliases(directory):
    """Snapshot the two driver availability markers as regular read-only inputs."""
    aliases = []
    for state, target in zip(CUDA_MODULE_STATES, containment.CUDA_MODULE_STATES, strict=True):
        try:
            with os.fdopen(os.open(state, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
                value = source.read(16)
        except OSError as exc:
            raise containment.ContainmentError("captive_cuda_module_state_unavailable:" + state.parent.name) from exc
        if value != b"live\n":
            raise containment.ContainmentError("captive_cuda_module_not_live:" + state.parent.name)
        snapshot = directory / state.parent.name
        with snapshot.open("xb") as output:
            output.write(value)
        snapshot.chmod(0o400)
        aliases.append((snapshot, target))
    return aliases


def publish(path, data):
    """Publish a validated artifact through an adjacent staging file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)


def read_output(path, *, offset=0, limit=None):
    """Read worker output without following a worker-created file alias."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != path.parent.stat().st_dev:
            raise containment.ContainmentError("worker_output_identity")
        stream.seek(max(offset, info.st_size - limit) if limit is not None else offset)
        return stream.read() if limit is None else stream.read(limit)


def verify_outputs(work):
    """Reject aliases and non-regular storage before host artifact handling."""
    work = Path(work)
    device = work.stat().st_dev

    def fail(error):
        raise error

    for directory, dirs, files in os.walk(work, onerror=fail):
        for name in dirs + files:
            info = (Path(directory) / name).lstat()
            if (info.st_dev != device or not (stat.S_ISDIR(info.st_mode)
                    or stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
                relative = (Path(directory) / name).relative_to(work)
                raise containment.ContainmentError("worker_output_tree:" + str(relative))


def _gate_output(pipe, timeout):
    deadline = time.monotonic() + timeout
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(pipe, selectors.EVENT_READ)
        while len(output) <= 1024 * 1024:
            if not selector.select(max(0, deadline - time.monotonic())):
                raise containment.ContainmentError("launcher_admission_timeout")
            block = os.read(pipe.fileno(), 65536)
            if not block:
                return json.loads(containment.admission(bytes(output)))
            output.extend(block)
            if b"\n" in output:
                return json.loads(containment.admission(bytes(output).splitlines()[0]))
    raise containment.ContainmentError("launcher_admission_size")


def _arguments(argv, work, inputs, executable, *, captive_cuda=False):
    argv = list(map(str, argv))
    runtime_error = "captive_cuda_runtime_executable"
    if captive_cuda and (not argv or not argv[0]):
        raise containment.ContainmentError(runtime_error)
    try:
        binary = Path(executable or argv[0])
        if not binary.is_absolute():
            if binary.parent != Path("."):
                binary = binary.absolute()
            else:
                binary = Path("/usr/bin") / binary
        binary = binary.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        if captive_cuda:
            raise containment.ContainmentError(runtime_error) from exc
        raise
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise containment.ContainmentError(runtime_error if captive_cuda else "blender_binary_unavailable")
    try:
        position = argv.index(str(executable)) if executable else 0
    except ValueError as exc:
        if captive_cuda:
            raise containment.ContainmentError(runtime_error) from exc
        raise
    prefix, argv = argv[:position], argv[position:]
    for index in range(0, len(prefix), 3):
        wrapper = prefix[index].removeprefix("/usr/bin/")
        if wrapper not in {"nice", "taskset"} or len(prefix[index:index + 3]) != 3:
            raise containment.ContainmentError("unsupported_blender_wrapper")
        prefix[index] = "/usr/bin/" + wrapper
    argv[0] = str(binary)
    argv[1:1] = ["--disable-autoexec", "--python-expr", "import sys; sys.dont_write_bytecode = True"]
    readonly, aliases = [], []
    if not binary.is_relative_to("/usr"):
        target = Path("/run/blender-input/bin/blender")
        aliases.append((binary, target))
        argv[0] = str(target)
    for value in dict.fromkeys(map(str, inputs)):
        source = Path(value).resolve(strict=True)
        if not source.is_file():
            raise containment.ContainmentError("worker_input_not_file")
        if source.is_relative_to(work) and Path(value) == source:
            readonly.append(source)
            continue
        target = Path("/run/blender-input") / str(len(aliases)) / source.name
        aliases.append((source, target))
        argv = [str(target) if arg == value else arg for arg in argv]
    return prefix, argv, readonly, aliases


@contextmanager
def command(argv, work, *, inputs=(), runtime=(), env=None, executable=None, logs=None, devices=(), display=None,
            runtime_aliases=(), captive_cuda=False, own_thread_comm=None, _captive_pins=None):
    """Prepare a declared Blender command and its private gate/worker channels."""
    devices = tuple(devices)
    containment._check_captive_cuda(captive_cuda, devices, display)
    if own_thread_comm is None:
        own_thread_comm = captive_cuda
    if type(own_thread_comm) is not bool or own_thread_comm and not captive_cuda:
        raise containment.ContainmentError("invalid_cuda_comm_selection")
    if captive_cuda and env is not None:
        containment._check_captive_environment(env)
    work = Path(work).resolve(strict=True)
    info = containment.ram_mount(work)
    if work.is_relative_to("/tmp") or info.st_uid != os.getuid():
        raise containment.ContainmentError("invalid_worker_workspace")
    for name in ("empty", "tmp"):
        path = work / "xdg" / name
        path.mkdir(parents=True, exist_ok=True)
        if path.resolve(strict=True) != path or path.stat().st_dev != work.stat().st_dev:
            raise containment.ContainmentError("worker_environment_path")
    declared = containment.worker_environment(declared_env(work, 1) if env is None else env, work)
    declared["VQA_RAMWS_ROOT"] = str(work)
    if devices:
        declared["CUDA_CACHE_PATH"] = str(work / "xdg" / "cuda")
    if {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"} & declared.keys():
        raise containment.ContainmentError("external_display_environment")
    with ExitStack() as stack:
        control = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="boundary-", dir=work)))
        stdout, stderr = logs or (control / "stdout", control / "stderr")
        prefix, argv, readonly, aliases = _arguments(argv, work, inputs, executable, captive_cuda=captive_cuda)
        aliases.extend(runtime_aliases)
        module_state = ()
        if captive_cuda:
            parent = work.parent
            if containment.ram_mount(parent).st_dev != info.st_dev:
                raise containment.ContainmentError("captive_cuda_module_snapshot_ram")
            # The source files must be outside the writable worker bind.
            snapshots = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="cuda-state-", dir=parent)))
            module_state = _cuda_module_state_aliases(snapshots)
        if display is not None:
            if devices:
                raise containment.ContainmentError("gpu_display_admission_unavailable")
            if not MESA_EGL.is_file():
                raise containment.ContainmentError("private_display_mesa_egl_unavailable")
            declared["LIBGL_ALWAYS_SOFTWARE"] = "1"
            declared["__EGL_VENDOR_LIBRARY_FILENAMES"] = str(MESA_EGL)
            declared["__GLX_VENDOR_LIBRARY_NAME"] = "mesa"
            declared["PULSE_SERVER"] = "unix:" + str(work / "xdg" / "pulse-disabled")
            passwd = control / "passwd"
            passwd.write_text(f"blender:x:{os.getuid()}:{os.getgid()}:Blender:{declared['HOME']}:/bin/false\n")
            aliases.append((passwd, Path("/etc/passwd")))
        launcher = containment.command(argv, work, [*runtime, *readonly], stdout,
                                       error_log=stderr, aliases=aliases,
                                       devices=devices if display is None else ("/dev/null",),
                                       display=display, captive_cuda=captive_cuda,
                                       own_thread_comm=own_thread_comm, _captive_pins=_captive_pins,
                                       _module_state=module_state)
        yield prefix + launcher, declared, stdout, stderr


@contextmanager
def admitted_process(argv, work, *, timeout, owner=subprocess.Popen, **settings):
    """Yield only after the gate has issued its pre-exec admission record."""
    pins = {} if settings.get("captive_cuda") else None
    if pins is not None:
        settings["_captive_pins"] = pins
    with command(argv, work, **settings) as (launcher, env, stdout, stderr):
        session = {"start_new_session": True} if owner is subprocess.Popen else {}
        try:
            child = owner(launcher, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env=env, close_fds=True, umask=0o077, **session)
        except FileNotFoundError as exc:
            raise containment.ContainmentError(f"launcher_prerequisite:{exc}") from exc
        with child as process:
            try:
                evidence = _gate_output(process.stdout, timeout)
                if pins is not None:
                    abi = evidence.get("landlock_abi")
                    executable = evidence.get("executable_identity")
                    devices = evidence.get("devices")
                    comm = settings.get("own_thread_comm")
                    if comm is None:
                        comm = True
                    if (evidence.get("captive_cuda") is not True or type(abi) is not int or abi < 1
                            or evidence.get("own_thread_comm") is not comm
                            or not containment._valid_identity(executable)
                            or not containment._valid_captive_devices(devices)
                            or executable != pins.get("executable_identity") or devices != pins.get("devices")):
                        raise containment.ContainmentError("captive_cuda_admission_receipt")
                yield process, stdout, stderr, evidence
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


def run(argv, work, timeout, **settings):
    deadline = time.monotonic() + timeout
    with admitted_process(argv, work, timeout=timeout, **settings) as (proc, stdout, stderr, evidence):
        proc.stdin.close()
        rc = proc.wait(timeout=max(0, deadline - time.monotonic()))
        verify_outputs(work)
        return rc, read_output(stdout), read_output(stderr), evidence


def cuda_device_nodes():
    """Select the individually identified nodes for the local single-device CUDA mode."""
    adapters = sorted(Path("/dev").glob("nvidia[0-9]*"))
    if [item.name for item in adapters] != ["nvidia0"]:
        raise containment.ContainmentError("cuda_requires_one_identified_nvidia_device")
    return containment.CUDA_NODES


def asset(argv, timeout, *, input_flags=(), work=None, output_flag=None):
    """Run an asset CLI's declared inputs and scratch outputs through admission."""
    try:
        split = argv.index("--")
        options = argv[split + 1:]
        inputs = [options[options.index(flag) + 1] for flag in input_flags]
        inputs.extend(value for value in argv[1:split] if str(value).endswith(".blend"))
        script = argv[argv.index("--python") + 1]
        if not Path(script).resolve().is_relative_to(ROOT / "bpy_side"):
            inputs.append(script)
        if output_flag is None and work is None:
            with containment.workspace() as scratch:
                rc, out, err, _ = run(argv, scratch, timeout, inputs=inputs, runtime=(ROOT / "bpy_side",))
        else:
            scratch = work
            if scratch is None:
                output = Path(options[options.index(output_flag) + 1]).absolute()
                scratch = output if output_flag in ("--run", "--out-dir") else output.parent
            rc, out, err, _ = run(argv, scratch, timeout, inputs=inputs, runtime=(ROOT / "bpy_side",))
        return rc, out.decode(errors="replace"), err.decode(errors="replace")
    except containment.ContainmentError as exc:
        return 2, "", "containment:" + str(exc)
