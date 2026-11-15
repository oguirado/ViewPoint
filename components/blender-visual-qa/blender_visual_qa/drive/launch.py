"""Owned-process Blender launch with argv admission and atomic run publication."""
import argparse
import contextlib
import fcntl
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import threading
import time
from . import encode as mp4
from .finish import finish
from .journal import (DriveError, canonical, file_sha, frame_list, integer, load, sha,
                      validate, write)

ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
BLENDER = Path("/usr/bin/blender")
ARTIFACT_CAP = 512 * 1024 * 1024


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DriveError("E_USAGE", message)


def declared_env(run, threads):
    xdg = str(Path(run).resolve() / "xdg")
    return dict(sorted({"BLENDER_USER_SCRIPTS": xdg + "/empty", "HOME": xdg, "LC_ALL": "C",
                        "OMP_NUM_THREADS": str(threads), "PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1",
                        "TZ": "UTC", "XDG_CACHE_HOME": xdg, "XDG_CONFIG_HOME": xdg,
                        "XDG_DATA_HOME": xdg}.items()))


def env_sha256(env, run):
    prefix = str(Path(run).resolve())
    return sha("".join(k + "=" + v.replace(prefix, "@RUN") + "\n" for k, v in sorted(env.items())).encode())


# @anchor:bv.launch.validate_render_argv

def _validate_render_argv(argv, run):
    """Admission mask b0..b6. Never launch any argv with a nonzero mask."""
    mask = 0
    if any(a in argv for a in ("-a", "--render-anim")):
        mask |= 1 << 0
    if any(a in argv for a in ("--enable-autoexec", "-y")):
        mask |= 1 << 1
    if "--factory-startup" not in argv:
        mask |= 1 << 2
    if "--python-exit-code" not in argv or argv.count("--python-exit-code") != 1:
        mask |= 1 << 3
    else:
        i = argv.index("--python-exit-code")
        if i+1 >= len(argv) or argv[i+1] != "9":
            mask |= 1 << 3
    scratch = (Path(run) / "scratch").resolve()
    # Only inspect Blender-side argv; journal/output arguments may end in .blend too.
    before = argv[:argv.index("--")] if "--" in argv else argv
    blends = [Path(a).resolve() for a in before if a.lower().endswith(".blend")]
    if len(blends) != 1 or not blends[0].is_relative_to(scratch):
        mask |= 1 << 4
    if any(a == "--addons" or a.startswith("--addons=") for a in argv):
        mask |= 1 << 5
    if any(a in ("-E", "--engine") or a.startswith("--engine=") for a in argv):
        mask |= 1 << 6
    if mask:
        raise DriveError("E_GATE", "argv_forbidden", 5)
    return mask


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # Reap descendants even when their direct parent exited early.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired as e:
        raise DriveError("E_DRIVE", "process_reap") from e


@contextlib.contextmanager
# @anchor:bv.launch.owned_process

def _owned_process(argv, **kwargs):
    if threading.current_thread() is not threading.main_thread():
        raise DriveError("E_USAGE", "main_thread_required")
    proc, previous = None, {}
    spawning, pending = True, None
    def interrupted(signum, frame):
        nonlocal pending
        if spawning:
            pending = signum
            return
        raise DriveError("E_DRIVE", "signal:" + str(signum))
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, interrupted)
        # Defer handler exceptions during spawn without blocking signals: a blocked
        # mask would be inherited by the child and defeat its SIGTERM cleanup.
        proc = subprocess.Popen(argv, start_new_session=True, **kwargs)
        spawning = False
        if pending is not None:
            interrupted(pending, None)
        yield proc
    finally:
        for sig in previous:
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
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def _last_stage(path):
    # Read only a bounded tail: Blender's progress log can be very large.
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 65536))
        lines = f.read().decode("utf-8", "replace").splitlines()
    import json
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "code" in row and "rc" in row:
            return row
    return None


def profile_load(identifier):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,17}", identifier):
        raise DriveError("E_PROFILE", "id")
    path = PROFILE_DIR / (identifier + ".json")
    if not path.is_file():
        raise DriveError("E_PROFILE", "unknown profile")
    p = load(path)
    required = {"schema", "id", "engine", "samples", "threads_any", "view_transform", "look", "exposure_milli", "gamma_milli", "comparable_scope"}
    if not isinstance(p, dict) or set(p) != required or p.get("schema") != "bvqa.render-profile/v1" or p.get("id") != identifier:
        raise DriveError("E_SCHEMA", "profile")
    if p["engine"] not in ("CYCLES", "BLENDER_WORKBENCH"):
        raise DriveError("E_PROFILE", "gpu_cycles")
    integer(p["samples"], 1, 4096, "samples")
    integer(p["exposure_milli"], -10000, 10000, "exposure_milli")
    integer(p["gamma_milli"], 1, 10000, "gamma_milli")
    if p["threads_any"] is not True or p["comparable_scope"] != "machine":
        raise DriveError("E_PROFILE", "scope")
    if identifier != "cycles-cpu-look" and (p["view_transform"], p["look"], p["exposure_milli"], p["gamma_milli"]) != ("Standard", "None", 0, 1000):
        raise DriveError("E_PROFILE", "view_transform")
    return p


def _artifact_bytes(run):
    total = 0
    for path in Path(run).rglob("*"):
        if path.is_file():
            total += path.stat().st_size
            if total > ARTIFACT_CAP:
                raise DriveError("E_BUDGET", "artifact_bytes", 6)
    return total


def drive(args):
    src, out = Path(args.blend).resolve(), Path(args.out).absolute()
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
    p = profile_load(args.profile)
    if args.mutant is not None:
        # ORACLES-owned mutator is outside the current build wave. Fail before launching.
        raise DriveError("E_INCOMPLETE", "mutator_unavailable", 3)
    tmp = out.with_name(out.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        raise DriveError("E_IO", "staging exists")
    source_sha = file_sha(src)
    source_stat = src.stat()
    tmp.mkdir(parents=True)
    try:
        for part in ("scratch", "xdg/empty", "logs", "render"):
            (tmp / part).mkdir(parents=True, exist_ok=True)
        scratch = tmp / "scratch" / "scene.blend"
        shutil.copyfile(src, scratch)
        # Local relative // assets remain relative to the source location in Blender.
        # Packing is a source precondition; report missing assets from the inspector.
        write(tmp / "journal.json", j)
        write(tmp / "profile.json", p)
        env = declared_env(tmp, args.threads)
        wall = {"s1": 0, "s2": 0, "s3": 0, "s4": 0}
        def execute(argv, log, timeout, blender=True):
            if blender:
                _validate_render_argv(argv, tmp)
                argv = ["/usr/bin/taskset", "-c", "15-19"] + argv
            with open(log, "wb") as f:
                with _owned_process(argv, env=env, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT) as proc:
                    deadline = time.monotonic() + timeout
                    for _ in range(timeout * 4 + 2):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise DriveError("E_BUDGET", "timeout", 6)
                        try:
                            rc = proc.wait(timeout=min(remaining, 0.25))
                            break
                        except subprocess.TimeoutExpired:
                            _artifact_bytes(tmp)
                    else:
                        raise DriveError("E_BUDGET", "timeout", 6)
            row = _last_stage(log) if blender else None
            if row is not None and row["rc"] != 0:
                if row["rc"] not in (2, 3, 4, 5, 6):
                    raise DriveError("E_DRIVE", "invalid_stage_rc")
                raise DriveError(row["code"], row.get("reason", "stage"), row["rc"])
            if rc:
                raise DriveError("E_DRIVE", "py_exception" if rc == 9 else "blender_rc")
            if blender and row is None:
                raise DriveError("E_DRIVE", "no_output")
        base = [str(BLENDER), "-b", "--factory-startup", "--disable-autoexec", "-noaudio", "--python-exit-code", "9", "-t", str(args.threads), str(scratch)]
        start = time.monotonic_ns()
        execute(base + ["--python", str(ROOT / "bpy_side" / "drive_manifest.py"), "--", "--out", str(tmp / "scene.manifest.json"), "--journal", str(tmp / "journal.json")], tmp / "logs" / "inspect.log", 120)
        wall["s1"] = (time.monotonic_ns() - start) // 1000000
        manifest = load(tmp / "scene.manifest.json")
        if args.strict and any(w.startswith(("unbaked_sim:", "py_driver:", "handler:", "autoexec_blocked")) for w in manifest["warnings"]):
            raise DriveError("E_INCOMPLETE", manifest["warnings"][0], 3)
        start = time.monotonic_ns()
        n = len(frame_list(j)) + next((op["count"] for op in j["ops"] if op["op"] == "still"), 0)
        timeout = args.timeout_s or min(3600, 3 * n * 2 + 60)
        execute(base + ["--python", str(ROOT / "bpy_side" / "drive_scene.py"), "--", "--journal", str(tmp / "journal.json"), "--profile", str(tmp / "profile.json"), "--out", str(tmp / "render"), "--threads", str(args.threads)], tmp / "logs" / "render.log", timeout)
        wall["s3"] = (time.monotonic_ns() - start) // 1000000
        write(tmp / "capture.inputs.json", {"blend_sha256": source_sha, "scratch_sha256_after": file_sha(scratch),
              "mutant_id": None, "fault_receipt_sha256": None, "journal_sha256": sha(canonical(j)),
              "profile_sha256": sha(canonical(p)), "env_sha256": env_sha256(env, tmp), "argv_mask": 0,
              "blender_binary_sha256": file_sha(BLENDER), "threads": args.threads})
        start = time.monotonic_ns()
        receipt = finish(tmp, keep_raw=args.keep_raw)
        mp4.encode(tmp, j, frame_list(j), execute)
        wall["s4"] = (time.monotonic_ns() - start) // 1000000
        receipt["wall_ms"] = wall
        receipt["mp4_sha256"] = file_sha(tmp / "clip.mp4")
        receipt["artifact_bytes"] = _artifact_bytes(tmp)
        # Resolve the receipt's self-size without counting it recursively by content hash.
        for _ in range(8):
            write(tmp / "capture.receipt.json", receipt)
            size = _artifact_bytes(tmp)
            if size == receipt["artifact_bytes"]:
                break
            receipt["artifact_bytes"] = size
        else:
            raise DriveError("E_IO", "artifact size convergence")
        if file_sha(src) != source_sha or src.stat().st_mtime_ns != source_stat.st_mtime_ns:
            raise DriveError("E_GATE", "src_mutated", 5)
        if out.exists():
            raise DriveError("E_IO", "output appeared")
        os.replace(tmp, out)
        return {"code": "OK", "rc": 0, "run": str(out), "frames_digest": receipt["frames_digest"]}
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def _dispatch(call):
    try:
        result = call()
    except DriveError as e:
        result = e.result()
    except (OSError, UnicodeError) as e:
        result = {"code": "E_IO", "rc": 2, "reason": str(e)}
    except (KeyError, TypeError, ValueError) as e:
        result = {"code": "E_SCHEMA", "rc": 2, "reason": str(e)}
    print(canonical(result).decode(), end="")
    return result["rc"]


def main(argv=None):
    def call():
        p = Parser()
        for arg in ("blend", "journal", "out"):
            p.add_argument("--" + arg, required=True)
        p.add_argument("--profile", default="cycles-cpu-det")
        p.add_argument("--threads", type=int, default=1)
        p.add_argument("--timeout-s", type=int)
        p.add_argument("--still", type=int)
        p.add_argument("--keep-raw", action="store_true")
        p.add_argument("--strict", action="store_true")
        p.add_argument("--mutant")
        args = p.parse_args(argv)
        # Serialize all captures from this launcher; lock waits are bounded too.
        lock_path = ROOT / "build" / "room_SB1" / "blender.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise DriveError("E_BUDGET", "blender_busy", 6) from e
            return drive(args)
    return _dispatch(call)


def check(run, golden):
    actual, expected = load(Path(run) / "capture.receipt.json"), load(golden)
    key = ["blend_sha256", "journal_sha256", "profile_sha256", "machine", "backend_fp"]
    for field in key:
        if field not in actual or field not in expected:
            raise DriveError("E_SCHEMA", "golden:" + field)
        if actual[field] != expected[field]:
            raise DriveError("E_STALE", field, 4)
    for field in ("version", "build_hash", "binary_sha256"):
        if field not in actual.get("blender", {}) or field not in expected.get("blender", {}):
            raise DriveError("E_SCHEMA", "golden:blender")
        if actual["blender"][field] != expected["blender"][field]:
            raise DriveError("E_STALE", "blender_version", 4)
    if not isinstance(actual.get("frames"), list) or not isinstance(expected.get("frames"), list):
        raise DriveError("E_SCHEMA", "frames")
    from itertools import zip_longest
    for a, b in zip_longest(actual["frames"], expected["frames"]):
        if a is None or b is None or any(a.get(k) != b.get(k) for k in ("render_index", "sha256", "seed")):
            ri = a["render_index"] if a else b["render_index"]
            raise DriveError("E_DETERMINISM", "first_div_ri:" + str(ri), 5)
    if not actual["frames"]:
        raise DriveError("E_SCHEMA", "empty frames")
    from .png import decode
    for row in actual["frames"]:
        data = (Path(run) / "render" / ("frame_%06d.png" % row["render_index"])).read_bytes()
        _, _, rgb = decode(data)
        if sha(rgb) != row["sha256"] or sha(data) != row.get("file_sha256"):
            raise DriveError("E_DETERMINISM", "first_div_ri:" + str(row["render_index"]), 5)
    return {"code": "OK", "rc": 0, "frames": len(actual["frames"])}


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
