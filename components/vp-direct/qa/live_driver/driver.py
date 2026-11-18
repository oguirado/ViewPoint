#!/usr/bin/env python3
"""Run a trusted vp-direct LIVE check with an owned desktop and fixture browser."""

import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time

HERE = Path(__file__).resolve().parent
DIRECT = HERE.parents[1]
ROOT = DIRECT.parents[1]
sys.path[:0] = [str(HERE), str(DIRECT), str(DIRECT.parent / "wender-agent"),
                str(DIRECT.parent / "vision-engine"), str(ROOT / "agents")]

from runtime import Display, Refused, WORK, choose_display, choose_displays, display_number, remove_private_tree, require

CHECKS = {
    "smoke": None,
    "privacy/tests/LIVE_login_fixture.py": "privacy",
    "frames/tests/LIVE_hidden_fixture.py": "frames",
    "vision/tests/LIVE_vision_fixture.py": "vision",
    "channel/core/LIVE_channel.py": "channel",
    "channel/atspi/live_check.py": "atspi",
    "attach/live_check.py": "attach",
    "channel/cdp/tests/LIVE_s2_fixture_click.py": "cdp",
    "window/tests/LIVE_private_window.py": "window",
    "focus/tests/LIVE_focus_trace.py": "focus",
    "recorder/timeline/tests/LIVE_record_fixture.py": "timeline",
    "recorder/compiler/tests/LIVE_record_compile_replay.py": "compiler",
    "loop/tests/LIVE_selector_repair.py": "selector",
    "perception/lifecycle/tests/LIVE_chrome_cycle.py": "lifecycle",
    "perception/demand/tests/LIVE_recording_sleep.py": "demand",
    "perception/pool/tests/LIVE_pool_close.py": "pool",
    "perception/stream/tests/LIVE_fixture_cycles.py": "stream",
    "perception/receipts/tests/LIVE_transition_receipts.py": "receipts",
    "accept/t1/LIVE_t1.py": "t1",
    "accept/t2/LIVE_t2.py": "t2",
}
XPRA_LABEL = {"frames", "vision", "channel", "atspi", "cdp", "window", "focus", "lifecycle", "receipts"}
SEPARATE_RUNNER = {"lifecycle", "receipts", "vision"}
ACCEPTANCE = {"t1", "t2"}
CGROUP = Path("/sys/fs/cgroup")
WINDOW_TOOLS = {"window", "focus"}
H1_REQUIRED = {
    "timeline": "U2a requires an approved H1 DemoEvent and verified RAM capture producer",
    "compiler": "U2b requires H1 plus an admitted ReplayOwner and verified workflow transport",
    "selector": "R1 requires an admitted H1 goal, transport and broker",
}


def get_ports():
    runtime = sys.modules.get("_vp_qd_invocation")
    require(runtime is not None, "launch the LIVE entry point through the QD driver")
    return runtime.ports


def verify_private_display(display):
    runtime = sys.modules.get("_vp_qd_invocation")
    return runtime is not None and runtime.ports.display.verify(display)


def open_frame_fixture(url, display):
    return get_ports().open_frame_fixture(url, display)


def open_login_fixture(url, display):
    return get_ports().open_login_fixture(url, display)


def open_vision_fixture(display):
    return get_ports().open_vision_fixture(display)


def open_fixture(backend):
    return get_ports().open_fixture(backend)


def bind(url):
    raise Refused(H1_REQUIRED["timeline"])


def record_demo(url, recorder):
    raise Refused(H1_REQUIRED["timeline"])


def review(url, timeline):
    raise Refused(H1_REQUIRED["compiler"])


def replay(url, compilation, *, dry_run):
    raise Refused(H1_REQUIRED["compiler"])


def provision(url, case):
    raise Refused(H1_REQUIRED["selector"])


def dispatches(loop):
    raise Refused(H1_REQUIRED["selector"])


def break_selector(loop, sequence, *, repairable):
    raise Refused(H1_REQUIRED["selector"])


def broker_events(loop):
    raise Refused(H1_REQUIRED["selector"])


def prepare(plan=None):
    if plan is not None:
        from t2_workload import prepare as prepare_acceptance
        require("_vp_qd_invocation" in sys.modules, "T2 multi-combination workload requires an active QD run")
        return prepare_acceptance(plan)
    return get_ports().prepare_attachment()


def require_scope():
    line = Path("/proc/self/cgroup").read_text().strip()
    require(line.startswith("0::/") and line.endswith(".scope"), "run QD in a capped systemd scope")
    directory = CGROUP / line[4:]
    for name, maximum in (("memory.high", 6 << 30), ("memory.max", 8 << 30)):
        try:
            value = (directory / name).read_text().strip()
        except OSError as exc:
            raise Refused("could not read the systemd memory caps") from exc
        require(value.isdecimal() and 0 < int(value) <= maximum, "systemd memory caps exceed QD limits")

    return directory


def sandbox(work, worker_args, tool_dir, *, group=None, ocr_prefix=None, acceptance=False):
    # Keep host X11, D-Bus, user data, and Unix sockets outside the mount namespace.
    cmd = ["bwrap", "--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc",
           "--unshare-uts", "--cap-drop", "ALL", "--die-with-parent", "--new-session",
           "--clearenv", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
           "--tmpfs", "/run", "--tmpfs", "/dev/shm", "--dir", "/var/lib/xkb",
           "--dir", "/tmp/.X11-unix", "--chmod", "1777", "/tmp/.X11-unix",
           "--bind", str(work), str(WORK), "--chdir", str(WORK)]
    for root in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt/google/chrome",
                 "/etc/fonts", "/etc/ld.so.cache", "/etc/passwd", "/etc/group", "/etc/machine-id",
                 "/etc/X11", "/etc/xdg", "/etc/openbox", "/etc/ssl/certs"):
        if Path(root).exists() and (root != "/bin" or ocr_prefix is None):
            cmd += ["--ro-bind", root, root]
    # Debian's NumPy links through alternatives outside the mounted library tree.
    lib_root = Path("/usr/lib")
    multiarch = sysconfig.get_config_var("MULTIARCH")
    require(multiarch, "Python did not report its multiarch library directory")
    for soname in ("libblas.so.3", "liblapack.so.3"):
        library = lib_root / multiarch / soname
        if library.is_symlink():
            alternative = library.readlink()
            if alternative.parent == Path("/etc/alternatives"):
                target = alternative.resolve(strict=True)
                require(target.is_relative_to(lib_root) and target.is_file(),
                        f"{soname} alternative must resolve to a file under {lib_root}")
                cmd += ["--ro-bind", str(target), str(alternative)]
    for rel in ("components/vp-direct", "components/wender-agent", "components/vision-engine", "agents"):
        cmd += ["--ro-bind", str(ROOT / rel), f"/checkout/{rel}"]
    cmd += ["--ro-bind", str(ROOT / "deps.lock"), "/checkout/deps.lock"]
    if acceptance:
        cmd += ["--ro-bind", "/sys", "/sys", "--ro-bind", str(work / "checkout-git"), "/checkout/.git"]
        for rel in ("daemon-adapter/internal/daemon/environment.go", "ui/electron/profilePicker.js"):
            cmd += ["--ro-bind", str(ROOT / rel), f"/checkout/{rel}"]
    if group is not None:
        cmd += ["--bind", str(group), str(group)]
    if ocr_prefix is not None:
        # The provider fixes PATH to /usr/bin:/bin. Supply a private /bin view
        # when the system's read-only /usr has no Tesseract executable.
        bin_view = work / "bin"
        bin_view.mkdir()
        require(Path("/bin").samefile("/usr/bin"), "the optional OCR prefix requires a merged /usr")
        for binary in Path("/bin").iterdir():
            if binary.name != "tesseract":
                (bin_view / binary.name).symlink_to(f"/usr/bin/{binary.name}")
        (bin_view / "tesseract").symlink_to("/qd-ocr/tesseract")
        cmd += ["--ro-bind", str(bin_view), "/bin",
                "--ro-bind", str((ocr_prefix / "bin/tesseract").resolve()), "/qd-ocr/tesseract",
                "--ro-bind", str((ocr_prefix / "share/tesseract-ocr/5/tessdata/eng.traineddata").resolve()),
                "/qd-ocr/tessdata/eng.traineddata",
                "--setenv", "QD_TESSDATA", "/qd-ocr/tessdata"]
    if tool_dir is not None:
        cmd += ["--ro-bind", str((tool_dir / "srf-host-window").resolve(strict=True)), "/srf-bin/srf-host-window"]
    for name, value in {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": "/work/home",
        "TMPDIR": "/tmp",
        "XDG_CONFIG_HOME": "/work/config",
        "XDG_CACHE_HOME": "/work/cache",
        "XDG_DATA_HOME": "/work/data",
        "XDG_RUNTIME_DIR": "/work/run",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
    }.items():
        cmd += ["--setenv", name, value]
    return [*cmd, "--", "/usr/bin/python3", "-IB",
            "/checkout/components/vp-direct/qa/live_driver/driver.py", "--worker", *worker_args]


def scoped_pids(group):
    processes = set((group / "cgroup.procs").read_text().split())
    for path in group.rglob("*/cgroup.procs"):
        processes.update(path.read_text().split())
    return processes


def partition_scope(group, *, acceptance=False):
    require(scoped_pids(group) == {str(os.getpid())},
            "private fixture needs an exclusive delegated systemd scope")
    try:
        runner = group / ("qd-runner.scope" if acceptance else "qd-runner")
        fixture = group / "qd-fixture"
        supervisor = group / "qd-control" if acceptance else fixture
        for role in {fixture, runner, supervisor}:
            role.mkdir()
        (supervisor / "cgroup.procs").write_text(str(os.getpid()))
        (group / "cgroup.subtree_control").write_text("+memory")
        require((runner / "memory.current").is_file(), "runner memory accounting is unavailable")
        if acceptance:
            for name in ("memory.high", "memory.max"):
                (runner / name).write_text((group / name).read_text())
    except OSError as exc:
        raise Refused("qualification requires a scoped memory controller; add -p Delegate=yes") from exc


def move_private_worker(source, destination):
    membership = Path("/proc/self/cgroup").read_text().strip()
    require(membership.startswith("0::/") and membership.endswith(f".scope/{source}"),
            "fixture and runner scopes were not provisioned")
    group = CGROUP / membership[4:]
    target = group.with_name(destination)
    (target / "cgroup.procs").write_text(str(os.getpid()))
    require((target / "cgroup.procs").read_text().split() == [str(os.getpid())],
            "private worker cgroup contains unexpected processes")


def enter_runner(*, acceptance=False):
    move_private_worker("qd-fixture", "qd-runner.scope" if acceptance else "qd-runner")


def acceptance_args(args, kind):
    from acceptance import CPUS

    require(os.sched_getaffinity(0) == CPUS, "acceptance requires taskset -c 16-19")
    default = 2 if args.smoke_workload or kind == "t2" else 16
    apps = default if args.apps is None else args.apps
    if kind == "t1":
        require(apps in ({2, 16, 32} if args.smoke_workload else {16, 32}), "unsupported T1 app count")
        require(0 <= args.order_index <= 3
                and ("baseline", "candidate", "candidate", "baseline")[args.order_index] == args.arm,
                "invalid T1 ABBA arm or order index")
    else:
        require(1 <= apps <= 16 and args.wake_samples > 0, "invalid T2 app or wake sample count")
        require(args.backend == "cdp", "T2 fixture readback requires the CDP backend")
        require(args.order_index == 0, "T2 order is recorded by the paired receipt inputs")
    require(args.warmups >= 0, "warmups must be nonnegative")
    return apps


def acceptance_checkout(work):
    commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"], text=True).strip()
    git = work / "checkout-git"
    # Export only the observed HEAD to the qualification scripts, without mounting host Git configuration.
    git.mkdir(mode=0o700)
    (git / "refs").mkdir()
    (git / "objects").mkdir()
    (git / "HEAD").write_text(commit + "\n")


def export_acceptance(directory, destination):
    # Only local benchmark inputs and captures leave the disposable work tree.
    destination.mkdir(mode=0o700)
    for name in ("manifest.json", "t1-plan.json", "t2-plan.json", "timer.html", "login.html", "capture.json"):
        source = directory / name
        if source.is_file():
            shutil.copyfile(source, destination / name)
    for contract in ("session", "binding", "agent_demand"):
        for record in directory.glob(f"[0-9]*-{contract}.json"):
            shutil.copyfile(record, destination / record.name)
    if (destination / "t2-plan.json").is_file():
        from acceptance import T2_DRIVER
        shutil.copyfile(T2_DRIVER, destination / T2_DRIVER.name)
    capture = destination / "capture.json"
    if capture.is_file():
        print(json.dumps({"receipt": str(capture), "sha256": sha256(capture.read_bytes()).hexdigest()}), flush=True)


def launch(args):
    kind = CHECKS[args.check]
    is_acceptance = kind in ACCEPTANCE
    ambient = os.environ.get("DISPLAY")
    if is_acceptance:
        args.apps = acceptance_args(args, kind)
        require(args.receipt_dir is None or not os.path.lexists(args.receipt_dir),
                "acceptance evidence requires a fresh directory")
        count = min(args.apps, 16) if kind == "t1" else args.apps + 1
        displays = choose_displays(args.display, ambient, count)
        private_display = displays[0]
    else:
        private_display = choose_display(args.display, ambient)
    group = require_scope()
    if kind in H1_REQUIRED:
        raise Refused(H1_REQUIRED[kind])
    if args.check == "smoke":
        require(os.sched_getaffinity(0) == set(range(16, 20)), "smoke requires taskset -c 16-19")
    package = subprocess.check_output(["dpkg-query", "-S", "/opt/google/chrome/chrome"], text=True)
    require(package.strip() == "google-chrome-stable: /opt/google/chrome/chrome",
            "google-chrome-stable DEB is required")
    chrome_deb = subprocess.check_output(["dpkg-query", "-W", "-f=${Version}", "google-chrome-stable"],
                                        text=True).strip()
    tool_dir = args.srf_bin_dir.resolve(strict=True) if args.srf_bin_dir else None
    require(kind not in WINDOW_TOOLS or (tool_dir and (tool_dir / "srf-host-window").is_file()),
            "this check needs --srf-bin-dir pointing to the provisioned SRF tools")
    ocr_prefix = args.ocr_prefix.resolve(strict=True) if args.ocr_prefix else None
    if kind == "vision" and ocr_prefix is None:
        require(Path("/usr/bin/tesseract").is_file()
                and Path("/usr/share/tesseract-ocr/5/tessdata/eng.traineddata").is_file(),
                "vision requires Tesseract with English data or an explicit --ocr-prefix")
    if ocr_prefix is not None:
        require(kind == "vision" and (ocr_prefix / "bin/tesseract").is_file()
                and (ocr_prefix / "share/tesseract-ocr/5/tessdata/eng.traineddata").is_file(),
                "OCR prefix must provide bin/tesseract and English tessdata")
    if kind in SEPARATE_RUNNER or is_acceptance:
        partition_scope(group, acceptance=is_acceptance)
    forwarded = ["--display", private_display, "--backend", args.backend, args.check]
    if is_acceptance:
        forwarded.extend(["--apps", str(args.apps), "--arm", args.arm, "--order-index", str(args.order_index),
                          "--warmups", str(args.warmups), "--wake-samples", str(args.wake_samples)])
        for display in displays:
            forwarded.extend(["--fixture-display", display])
        if args.smoke_workload:
            forwarded.append("--smoke-workload")
    initial = scoped_pids(group)
    work = Path(tempfile.mkdtemp(prefix="vp-qd-"))
    try:
        with ExitStack() as outer:
            if is_acceptance:
                acceptance_checkout(work)
                if args.controls is not None:
                    shutil.copyfile(args.controls, work / "controls.json")
                    forwarded.extend(["--controls", "/work/controls.json"])
                if kind == "t2":
                    from inventory import serve
                    outer.enter_context(serve(work))
            command = sandbox(work, forwarded, tool_dir,
                              group=group if kind in SEPARATE_RUNNER or is_acceptance else None,
                              ocr_prefix=ocr_prefix, acceptance=is_acceptance)
            child = outer.enter_context(subprocess.Popen(command, stdin=subprocess.DEVNULL))

            def terminate_owned(_sig, _frame):
                if child.poll() is None:
                    child.terminate()

            previous = signal.signal(signal.SIGTERM, terminate_owned)
            try:
                rc = child.wait()
            except KeyboardInterrupt:
                terminate_owned(None, None)
                child.wait(timeout=10)
                raise
            finally:
                signal.signal(signal.SIGTERM, previous)
        if is_acceptance and args.receipt_dir is not None:
            export_acceptance(work / "acceptance", args.receipt_dir)
    finally:
        deadline = time.monotonic() + 3
        while (remaining := scoped_pids(group) - initial) and time.monotonic() < deadline:
            time.sleep(0.05)
        require(not remaining, "owned scope processes remained before private root removal")
        remove_private_tree(work)
    require(not work.exists(), "private profile root remained")
    print(json.dumps({"check": args.check, "display": private_display, "chrome_deb": chrome_deb,
                      "private_root_removed": True, "leftover_scoped_processes": len(remaining),
                      "worker_rc": rc, "evidence": "MEASURED"}, sort_keys=True), flush=True)
    return rc if rc >= 0 else 128 - rc


def worker(args):
    from ports import Ports, run_check

    require(not os.environ.get("DISPLAY") and WORK.is_dir(), "private worker requires a scrubbed environment")
    require(Path("/proc/1/comm").read_text().strip() == "bwrap", "private PID namespace was not established")
    links = {line.split(":", 1)[0].strip() for line in Path("/proc/net/dev").read_text().splitlines()[2:]}
    require(links == {"lo"} and not Path("/proc/net/route").read_text().splitlines()[1:],
            "fixture worker has a non-loopback network path")
    for name in ("home", "config", "cache", "data", "run"):
        (WORK / name).mkdir(mode=0o700)
    display_number(args.display)
    display = Display(args.display)
    script = CHECKS[args.check]
    if script in ACCEPTANCE:
        from acceptance import run_acceptance
        acceptance_args(args, script)
        move_private_worker("qd-control", "qd-fixture")
        this_module = sys.modules[__name__]
        this_module.ports = Ports(display, None)
        sys.modules["_vp_qd_invocation"] = this_module
        rc = 0
        try:
            numbers = [display_number(value) for value in args.fixture_display]
            count = min(args.apps, 16) if script == "t1" else args.apps + 1
            require(len(numbers) == len(set(numbers)) == count and numbers[0] == display.number,
                    "private fixture display allocation differs")
            run_acceptance(args, numbers, script)
        except SystemExit as exc:
            rc = exc.code
        finally:
            sys.modules.pop("_vp_qd_invocation", None)
        print(json.dumps({"x11_lock_removed": True, "x11_socket_removed": True}, sort_keys=True))
        return rc
    with ExitStack() as stack:
        display.start(stack, framebuffer=script in {"stream", "demand", "pool", *SEPARATE_RUNNER})
        if script in XPRA_LABEL:
            display.start_xpra(stack)
        if script in {"atspi", "attach"} or (script == "channel" and args.backend == "atspi"):
            display.start_bus(stack)
        ports = Ports(display, stack)
        this_module = sys.modules[__name__]
        this_module.ports = ports
        sys.modules["_vp_qd_invocation"] = this_module
        os.environ.update(display.env)
        try:
            run_check(args.check, script, ports, args.backend)
        finally:
            sys.modules.pop("_vp_qd_invocation", None)
    require(not display.lock.exists() and not display.socket.exists(), "private X11 endpoint was not removed")
    print(json.dumps({"x11_lock_removed": True, "x11_socket_removed": True}, sort_keys=True))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display", help="explicit free local display at or above :90")
    parser.add_argument("--backend", choices=("cdp", "atspi"), default="cdp")
    parser.add_argument("--srf-bin-dir", type=Path)
    parser.add_argument("--ocr-prefix", type=Path, help="read-only Tesseract installation for the vision fixture")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture-display", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument("--apps", type=int, help="number of requested acceptance fixture apps")
    parser.add_argument("--arm", choices=("baseline", "candidate"), default="baseline")
    parser.add_argument("--order-index", type=int, default=0, help="T1 ABBA position")
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--wake-samples", type=int, default=1, help="T2 wakes per stratum")
    parser.add_argument("--controls", type=Path, help="T1 pair plan or T2 telemetry controls")
    parser.add_argument("--receipt-dir", type=Path, help="new directory for raw acceptance evidence")
    parser.add_argument("--smoke-workload", action="store_true", help="short producer exercise without qualification")
    parser.add_argument("check", choices=CHECKS)
    args = parser.parse_args()
    try:
        return worker(args) if args.worker else launch(args)
    except (Refused, OSError, subprocess.SubprocessError) as exc:
        print(f"QD refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
