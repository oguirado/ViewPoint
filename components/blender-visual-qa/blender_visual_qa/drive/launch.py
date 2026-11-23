"""Clean-room AGPL-3.0-only owned-process Blender launcher, argv gate, and golden verifier."""
import argparse
import contextlib
import ctypes
import errno
import fcntl
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import threading
import time

from . import encode as mp4
from . import containment
from .finish import finish
from .journal import (
    DriveError,
    canonical,
    file_sha,
    frame_list,
    integer,
    load,
    sha,
    validate,
    write,
)
from .png import decode as png_decode
from ..package_layout import is_source_layout

ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
BLENDER = Path("/usr/bin/blender")
ARTIFACT_CAP = 512 * 1024 * 1024
AT_FDCWD = -100
RENAME_NOREPLACE = 1
_PROFILE_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,17}")
_PROFILE_FIELDS = {
    "schema",
    "id",
    "engine",
    "samples",
    "threads_any",
    "view_transform",
    "look",
    "exposure_milli",
    "gamma_milli",
    "comparable_scope",
}
_STRICT_WARN_PREFIXES = ("unbaked_sim:", "py_driver:", "handler:", "autoexec_blocked")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DriveError("E_USAGE", message)


# @anchor:bv.drive.launch.declared_env
def declared_env(run, threads):
    """Return deterministic child environment anchored under the run XDG directory."""
    xdg = str(Path(run).resolve() / "xdg")
    return dict(sorted({"BLENDER_USER_SCRIPTS": f"{xdg}/empty",
                        "HOME": xdg,
                        "LC_ALL": "C",
                        "OMP_NUM_THREADS": str(threads),
                        "PATH": "/usr/bin:/bin",
                        "PYTHONNOUSERSITE": "1",
                        "TZ": "UTC",
                        "XDG_CACHE_HOME": xdg,
                        "XDG_CONFIG_HOME": xdg,
                        "XDG_DATA_HOME": xdg}.items()))


# @anchor:bv.drive.launch.env_sha256
def env_sha256(env, run):
    """Hash declared environment with run-specific prefix normalized to @RUN."""
    prefix = str(Path(run).resolve())
    lines = "".join(f"{k}={v.replace(prefix, '@RUN')}\n" for k, v in sorted(env.items()))
    return sha(lines.encode())


# @anchor:bv.launch.validate_render_argv
def _validate_render_argv(argv, run):
    """Enforce argv admission mask b0..b6 before spawning Blender."""
    mask = 0
    if any(arg in ("-a", "--render-anim") for arg in argv):
        mask |= 1 << 0
    if any(arg in ("--enable-autoexec", "-y") for arg in argv):
        mask |= 1 << 1
    if "--factory-startup" not in argv:
        mask |= 1 << 2
    if argv.count("--python-exit-code") != 1:
        mask |= 1 << 3
    else:
        idx = argv.index("--python-exit-code")
        if idx + 1 >= len(argv) or argv[idx + 1] != "9":
            mask |= 1 << 3

    scratch_dir = (Path(run) / "scratch").resolve()
    blender_argv = argv[:argv.index("--")] if "--" in argv else argv
    blend_paths = [Path(arg).resolve() for arg in blender_argv if arg.lower().endswith(".blend")]
    if len(blend_paths) != 1 or not blend_paths[0].is_relative_to(scratch_dir):
        mask |= 1 << 4

    if any(arg == "--addons" or arg.startswith("--addons=") for arg in argv):
        mask |= 1 << 5
    if any(arg in ("-E", "--engine") or arg.startswith("--engine=") for arg in argv):
        mask |= 1 << 6

    if mask:
        raise DriveError("E_GATE", "argv_forbidden", 5)
    return mask


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    proc_root = Path("/proc")
    if proc_root.is_dir():
        saw_any = False
        pgid_s = str(pgid)
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(errors="replace")
            except OSError:
                continue
            rparen = stat.rfind(")")
            if rparen == -1:
                continue
            fields = stat[rparen + 2:].split()
            if len(fields) >= 3 and fields[2] == pgid_s:
                saw_any = True
                if fields[0] != "Z":
                    return True
        if saw_any:
            return False
    return True


def _drain_group(pgid, timeout):
    deadline = time.monotonic() + timeout
    while _group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _kill_group(proc):
    """Terminate and reap the child and every descendant in its process group."""
    pgid = proc.pid
    for sig, wait_s, drain_s in ((signal.SIGTERM, 1.0, 0.5), (signal.SIGKILL, 5.0, 2.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            continue
        if _drain_group(pgid, drain_s):
            return
    raise DriveError("E_DRIVE", "process_reap")


@contextlib.contextmanager
# @anchor:bv.launch.owned_process
def _owned_process(argv, **kwargs):
    """Run subprocess in a new session and guarantee group teardown on exit or signal."""
    if threading.current_thread() is not threading.main_thread():
        raise DriveError("E_USAGE", "main_thread_required")

    proc = None
    saved = {}
    in_spawn = True
    deferred_sig = None

    def _on_signal(signum, _frame):
        nonlocal deferred_sig
        if in_spawn:
            deferred_sig = signum
            return
        raise DriveError("E_DRIVE", f"signal:{signum}")

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            saved[sig] = signal.signal(sig, _on_signal)
        proc = subprocess.Popen(argv, start_new_session=True, **kwargs)
        in_spawn = False
        if deferred_sig is not None:
            _on_signal(deferred_sig, None)
        yield proc
    finally:
        for sig in saved:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if proc is not None:
                try:
                    _kill_group(proc)
                finally:
                    for stream in (proc.stdin, proc.stdout, proc.stderr):
                        if stream is not None:
                            stream.close()
        finally:
            for sig, prev_handler in saved.items():
                signal.signal(sig, prev_handler)


def _last_stage(path):
    """Parse the final JSON status object from the tail of a Blender stage log."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - 65536))
        tail = fh.read().decode("utf-8", "replace").splitlines()
    for line in reversed(tail):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "code" in obj and "rc" in obj:
            return obj
    return None


def profile_load(identifier):
    """Load and validate a declared render profile JSON."""
    if not _PROFILE_ID_RE.fullmatch(identifier):
        raise DriveError("E_PROFILE", "id")
    path = PROFILE_DIR / f"{identifier}.json"
    if not path.is_file():
        raise DriveError("E_PROFILE", "unknown profile")
    prof = load(path)
    if (
        not isinstance(prof, dict)
        or set(prof) != _PROFILE_FIELDS
        or prof.get("schema") != "bvqa.render-profile/v1"
        or prof.get("id") != identifier
    ):
        raise DriveError("E_SCHEMA", "profile")
    if prof["engine"] not in ("CYCLES", "BLENDER_WORKBENCH"):
        raise DriveError("E_PROFILE", "gpu_cycles")
    integer(prof["samples"], 1, 4096, "samples")
    integer(prof["exposure_milli"], -10000, 10000, "exposure_milli")
    integer(prof["gamma_milli"], 1, 10000, "gamma_milli")
    if prof["threads_any"] is not True or prof["comparable_scope"] != "machine":
        raise DriveError("E_PROFILE", "scope")
    if identifier != "cycles-cpu-look" and (
        prof["view_transform"],
        prof["look"],
        prof["exposure_milli"],
        prof["gamma_milli"],
    ) != ("Standard", "None", 0, 1000):
        raise DriveError("E_PROFILE", "view_transform")
    return prof


def _artifact_bytes(run, *, running=False):
    def walk_error(exc):
        if not running or not isinstance(exc, FileNotFoundError):
            raise exc

    total = 0
    device = run.stat().st_dev
    for _, dirs, files, fd in os.fwalk(run, onerror=walk_error):
        for name in dirs + files:
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if running:
                    continue
                raise
            regular = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            if info.st_dev != device or not (regular or stat.S_ISDIR(info.st_mode)):
                raise DriveError("E_GATE", "artifact_type", 5)
            if regular:
                total += info.st_size
            if total > ARTIFACT_CAP:
                raise DriveError("E_BUDGET", "artifact_bytes", 6)
    return total


def drive(args):
    """Run Blender inspect and render on RAM, then validate and publish from the parent."""
    src = Path(args.blend).resolve()
    out = Path(args.out).absolute()
    if not src.is_file():
        raise DriveError("E_IO", "blend missing")
    if src.suffix.lower() != ".blend":
        raise DriveError("E_USAGE", "blend suffix")
    if out.exists() or out.is_symlink():
        raise DriveError("E_IO", "output exists")
    integer(args.threads, 1, 1024, "threads")
    if args.timeout_s is not None:
        integer(args.timeout_s, 1, 3600, "timeout_s")

    j = validate(load(args.journal))
    if args.still is not None:
        integer(args.still, 0, 30, "still")
        j = dict(j, ops=[op for op in j["ops"] if op["op"] != "still"] + [{"op": "still", "count": args.still}])
    prof = profile_load(args.profile)
    if args.mutant is not None:
        raise DriveError("E_INCOMPLETE", "mutator_unavailable", 3)

    publishing = out.with_name(f"{out.name}.tmp")
    if publishing.exists() or publishing.is_symlink():
        raise DriveError("E_IO", "staging exists")

    try:
        with containment.workspace() as staging:
            return _capture(args, src, out, publishing, staging, j, prof)
    except containment.ContainmentError as exc:
        raise DriveError("E_GATE", f"containment:{exc}", 5) from exc


def _publish(staging, publishing, out, files):
    """Copy only the capture bundle, then install it atomically without clobbering."""
    publishing.mkdir(parents=True)
    try:
        (publishing / "xdg" / "empty").mkdir(parents=True)
        for relative in files:
            target = publishing / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staging / relative, target)
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        if renameat2(AT_FDCWD, os.fsencode(publishing), AT_FDCWD, os.fsencode(out), RENAME_NOREPLACE):
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), out)
    finally:
        if publishing.exists():
            shutil.rmtree(publishing)


def _capture(args, src, out, publishing, staging, j, prof):
    source_sha = file_sha(src)
    source_stat = src.stat()
    for sub in ("scratch", "xdg/empty", "xdg/tmp", "logs", "render"):
        (staging / sub).mkdir(parents=True, exist_ok=True)
    scratch = staging / "scratch" / "scene.blend"
    shutil.copyfile(src, scratch)
    write(staging / "journal.json", j)
    write(staging / "profile.json", prof)
    readonly = (ROOT / "bpy_side", scratch.parent, staging / "journal.json", staging / "profile.json")
    env = containment.worker_environment(declared_env(staging, args.threads), staging)
    wall = {"s1": 0, "s2": 0, "s3": 0, "s4": 0}

    def execute(argv, log_path, timeout, blender=True):
        if blender:
            _validate_render_argv(argv, staging)
            argv = ["/usr/bin/taskset", "-c", "15-19"] + argv
            argv = containment.command(argv, staging, readonly, log_path)
        output = subprocess.PIPE if blender else open(log_path, "wb")
        with contextlib.ExitStack() as stack:
            if not blender:
                stack.enter_context(output)
            stdin = subprocess.PIPE if blender else subprocess.DEVNULL
            with _owned_process(argv, env=env, stdin=stdin, stdout=output,
                                stderr=subprocess.STDOUT, close_fds=True) as proc:
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DriveError("E_BUDGET", "timeout", 6)
                    try:
                        if blender:
                            evidence, _ = proc.communicate(timeout=min(remaining, 0.25))
                            rc = proc.returncode
                        else:
                            rc = proc.wait(timeout=min(remaining, 0.25))
                        break
                    except subprocess.TimeoutExpired:
                        _artifact_bytes(staging, running=True)
        if blender:
            admitted = containment.admission(evidence)
            _artifact_bytes(staging)
            with open(log_path, "ab") as log:
                log.write(b"\nCONTAINMENT " + admitted + b"\n")
        row = _last_stage(log_path) if blender else None
        if row is not None and row["rc"] != 0:
            code = row["code"]
            row_rc = row["rc"]
            if row_rc not in (2, 3, 4, 5, 6):
                code, row_rc = "E_DRIVE", 2
            raise DriveError(code, row.get("reason", "blender_stage"), row_rc)
        if rc != 0:
            raise DriveError("E_DRIVE", f"rc:{rc}")
        if blender and row is None:
            raise DriveError("E_DRIVE", "no_output")

    base_cmd = [
        str(BLENDER),
        "-b",
        "--factory-startup",
        "--disable-autoexec",
        "-noaudio",
        "--python-exit-code",
        "9",
        "--python-expr",
        "import sys; sys.dont_write_bytecode = True",
        "-t",
        str(args.threads),
        str(scratch),
    ]
    t0 = time.monotonic_ns()
    execute(
        base_cmd + [
            "--python",
            str(ROOT / "bpy_side" / "drive_manifest.py"),
            "--",
            "--out",
            str(staging / "scene.manifest.json"),
            "--journal",
            str(staging / "journal.json"),
        ],
        staging / "logs" / "inspect.log",
        120,
    )
    wall["s1"] = (time.monotonic_ns() - t0) // 1000000

    manifest = load(staging / "scene.manifest.json")
    if args.strict and any(w.startswith(_STRICT_WARN_PREFIXES) for w in manifest["warnings"]):
        raise DriveError("E_INCOMPLETE", manifest["warnings"][0], 3)

    t1 = time.monotonic_ns()
    frame_count = len(frame_list(j)) + next((op["count"] for op in j["ops"] if op["op"] == "still"), 0)
    render_timeout = args.timeout_s or min(3600, 3 * frame_count * 2 + 60)
    execute(
        base_cmd + [
            "--python",
            str(ROOT / "bpy_side" / "drive_scene.py"),
            "--",
            "--journal",
            str(staging / "journal.json"),
            "--profile",
            str(staging / "profile.json"),
            "--out",
            str(staging / "render"),
            "--threads",
            str(args.threads),
        ],
        staging / "logs" / "render.log",
        render_timeout,
    )
    wall["s3"] = (time.monotonic_ns() - t1) // 1000000

    write(
        staging / "capture.inputs.json",
        {
            "blend_sha256": source_sha,
            "scratch_sha256_after": file_sha(scratch),
            "mutant_id": None,
            "fault_receipt_sha256": None,
            "journal_sha256": sha(canonical(j)),
            "profile_sha256": sha(canonical(prof)),
            "env_sha256": env_sha256(env, staging),
            "argv_mask": 0,
            "blender_binary_sha256": file_sha(BLENDER),
            "threads": args.threads,
        },
    )
    t2 = time.monotonic_ns()
    receipt = finish(staging, keep_raw=args.keep_raw)
    mp4.encode(staging, j, frame_list(j), execute)
    wall["s4"] = (time.monotonic_ns() - t2) // 1000000
    receipt["wall_ms"] = wall
    receipt["mp4_sha256"] = file_sha(staging / "clip.mp4")
    shutil.rmtree(staging / "xdg")
    receipt["artifact_bytes"] = _artifact_bytes(staging)
    for _ in range(8):
        write(staging / "capture.receipt.json", receipt)
        new_size = _artifact_bytes(staging)
        if new_size == receipt["artifact_bytes"]:
            break
        receipt["artifact_bytes"] = new_size
    else:
        raise DriveError("E_IO", "artifact size convergence")

    if file_sha(src) != source_sha or src.stat().st_mtime_ns != source_stat.st_mtime_ns:
        raise DriveError("E_GATE", "src_mutated", 5)
    files = {"scratch/scene.blend", "journal.json", "profile.json", "scene.manifest.json",
             "capture.inputs.json", "capture.receipt.json", "clip.mp4", "render/scene.json",
             "render/drive.meta.json", "render/bounds.jsonl", "logs/inspect.log", "logs/render.log",
             "logs/ffmpeg.log", "logs/frames.concat.txt"}
    images = [f"render/frame_{frame['render_index']:06d}.png" for frame in receipt["frames"]]
    images += [f"render/still/still_{frame['ri']:06d}.png" for frame in receipt["still"]]
    files.update(images)
    if args.keep_raw:
        files.update(str(Path(name).parent / "raw" / Path(name).name) for name in images)
    if {str(path.relative_to(staging)) for path in staging.rglob("*") if path.is_file()} != files:
        raise DriveError("E_GATE", "capture_bundle_inventory", 5)
    _publish(staging, publishing, out, sorted(files))
    return {"code": "OK", "rc": 0, "run": str(out), "frames_digest": receipt["frames_digest"]}


def _dispatch(call):
    try:
        res = call()
    except DriveError as exc:
        res = exc.result()
    except (OSError, UnicodeError) as exc:
        res = {"code": "E_IO", "rc": 2, "reason": str(exc)}
    except (KeyError, TypeError, ValueError) as exc:
        res = {"code": "E_SCHEMA", "rc": 2, "reason": str(exc)}
    print(canonical(res).decode(), end="")
    return res["rc"]


_LOCK_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_LOCK_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK


def _open_lock_dir(stack, path, parent=None):
    fd = os.open(path, _LOCK_DIR_FLAGS, dir_fd=parent)
    stack.callback(os.close, fd)
    return fd


def _check_lock_owner(info, name, uid, unsafe_bits, mode=None):
    actual_mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != uid or actual_mode & unsafe_bits or (mode is not None and actual_mode != mode):
        raise OSError(f"Unsafe DRIVE lock permissions or owner: {name}")


def _runtime_parent(stack):
    fd = _open_lock_dir(stack, "/")
    for name in ("run", "user"):
        fd = _open_lock_dir(stack, name, fd)
        _check_lock_owner(os.fstat(fd), name, 0, 0o022)
    return fd


def _lock_dir(stack, parent, name, create):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    return _open_lock_dir(stack, name, parent)


def _flock_file(stack, parent, name, uid, unsafe_bits, mode=None, create=False):
    # Inspect existing entries before opening them. Verify the opened file too,
    # including when a second caller created it after our lookup.
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise
        flags = os.O_RDWR | os.O_CREAT
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(f"Invalid DRIVE lock file: {name}")
        flags = os.O_RDWR if mode is not None else os.O_RDONLY
    fd = os.open(name, flags | _LOCK_FILE_FLAGS, 0o600, dir_fd=parent)
    stack.callback(os.close, fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError(f"Invalid DRIVE lock file: {name}")
    _check_lock_owner(info, name, uid, unsafe_bits, mode)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise DriveError("E_BUDGET", "blender_busy", 6) from exc
        raise


def _directory_writable(fd):
    return (not (os.fstatvfs(fd).f_flag & os.ST_RDONLY)
            and os.access(".", os.W_OK | os.X_OK, dir_fd=fd, effective_ids=True))


def _legacy_lock(stack, parent, uid, checkout):
    # Existing checkouts can have legacy group-writable state from their umask.
    unsafe_bits = 0o002 if checkout else 0o022
    for name in ("build", "room_SB1"):
        try:
            child = _open_lock_dir(stack, name, parent)
        except FileNotFoundError:
            if not (checkout or _directory_writable(parent)):
                return
            child = _lock_dir(stack, parent, name, create=True)
        _check_lock_owner(os.fstat(child), name, uid, unsafe_bits)
        parent = child
    name = "blender.lock"
    try:
        _flock_file(stack, parent, name, uid, unsafe_bits)
    except FileNotFoundError:
        if checkout or _directory_writable(parent):
            _flock_file(stack, parent, name, uid, unsafe_bits, create=True)


@contextlib.contextmanager
def _drive_lock():
    root = ROOT.resolve(strict=True)
    uid = os.geteuid()
    with contextlib.ExitStack() as stack:
        root_fd = _open_lock_dir(stack, root)
        checkout = is_source_layout(".", dir_fd=root_fd)
        if not checkout:
            runtime = _open_lock_dir(stack, str(uid), _runtime_parent(stack))
            _check_lock_owner(os.fstat(runtime), str(uid), uid, 0o077, 0o700)
            if not _directory_writable(runtime):
                raise OSError("DRIVE user runtime directory is not writable")
            state = _lock_dir(stack, runtime, "viewpoint-drive", create=True)
            _check_lock_owner(os.fstat(state), "viewpoint-drive", uid, 0o077, 0o700)
            key = sha(os.fsencode(root))
            _flock_file(stack, state, f"{key}.lock", uid, 0o077, 0o600, create=True)
        # Hold the old lock as well on writable installs where an old process
        # could create it. Read-only installs can only have a pre-existing lock.
        _legacy_lock(stack, root_fd, uid, checkout)
        yield


def main(argv=None):
    def call():
        p = Parser()
        p.add_argument("--blend", required=True)
        p.add_argument("--journal", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--profile", default="cycles-cpu-det")
        p.add_argument("--threads", type=int, default=1)
        p.add_argument("--timeout-s", type=int)
        p.add_argument("--still", type=int)
        p.add_argument("--keep-raw", action="store_true")
        p.add_argument("--strict", action="store_true")
        p.add_argument("--mutant")
        args = p.parse_args(argv)
        with _drive_lock():
            return drive(args)

    return _dispatch(call)


# @anchor:bv.drive.launch.check
def check(run, golden):
    """Compare a capture run receipt and PNG frames against a golden reference."""
    actual = load(Path(run) / "capture.receipt.json")
    expected = load(golden)
    for field in ("blend_sha256", "journal_sha256", "profile_sha256", "machine", "backend_fp"):
        if field not in actual or field not in expected:
            raise DriveError("E_SCHEMA", f"golden:{field}")
        if actual[field] != expected[field]:
            raise DriveError("E_STALE", field, 4)
    for field in ("version", "build_hash", "binary_sha256"):
        if field not in actual.get("blender", {}) or field not in expected.get("blender", {}):
            raise DriveError("E_SCHEMA", "golden:blender")
        if actual["blender"][field] != expected["blender"][field]:
            raise DriveError("E_STALE", "blender_version", 4)
    if not isinstance(actual.get("frames"), list) or not isinstance(expected.get("frames"), list):
        raise DriveError("E_SCHEMA", "frames")
    for a_row, b_row in zip_longest(actual["frames"], expected["frames"]):
        if a_row is None or b_row is None or any(a_row.get(k) != b_row.get(k) for k in ("render_index", "sha256", "seed")):
            ri = a_row["render_index"] if a_row else b_row["render_index"]
            raise DriveError("E_DETERMINISM", f"first_div_ri:{ri}", 5)
    if not actual["frames"]:
        raise DriveError("E_SCHEMA", "empty frames")
    for row in actual["frames"]:
        frame_bytes = (Path(run) / "render" / f"frame_{row['render_index']:06d}.png").read_bytes()
        _, _, rgb = png_decode(frame_bytes)
        if sha(rgb) != row["sha256"] or sha(frame_bytes) != row.get("file_sha256"):
            raise DriveError("E_DETERMINISM", f"first_div_ri:{row['render_index']}", 5)
    return {"code": "OK", "rc": 0, "frames": len(actual["frames"])}


# @anchor:bv.drive.launch.check_main
def check_main(argv=None):
    def call():
        p = Parser()
        p.add_argument("--run", required=True)
        p.add_argument("--golden", required=True)
        args = p.parse_args(argv)
        return check(args.run, args.golden)

    return _dispatch(call)


if __name__ == "__main__":
    raise SystemExit(main())
