# SPDX-License-Identifier: AGPL-3.0-only
"""Shared helpers for the pose oracle CLI (system Python, numpy only)."""
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

ENV = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1"}
GIB = 1024 ** 3
MIN_HEADROOM = 20 * GIB
SCOPE_BYTES = 8 * GIB
CGROUP = Path("/sys/fs/cgroup")
BLENDER = os.environ.get("STUDIO_BLENDER", "blender")
RETRY_PRECONDITION = "correct the reported precondition and rerun"


class Refusal(Exception):
    """Harness error: the oracle could not deliver a verdict (rc 2)."""

    def __init__(self, reason, retry_when=RETRY_PRECONDITION, next_command=None):
        super().__init__(reason)
        self.reason = reason
        self.retry_when = retry_when
        self.next_command = next_command

    def payload(self, fallback):
        return {"reason": self.reason, "retry_when": self.retry_when,
                "next_command": self.next_command or fallback}


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def digest_obj(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump_json(path, obj, indent=1):
    text = json.dumps(obj, sort_keys=True, indent=indent, ensure_ascii=False) + "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as output:
        pending = Path(output.name)
        try:
            output.write(text)
            output.close()
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)
    return text


def child_env(work):
    """Pinned, GPU-free environment for every child process."""
    env = dict(ENV, PATH="/usr/bin:/bin", HOME=str(work), TMPDIR=str(work),
               XDG_CACHE_HOME=str(work), XDG_CONFIG_HOME=str(work),
               BLENDER_USER_CONFIG=str(work), CUDA_VISIBLE_DEVICES="",
               NVIDIA_VISIBLE_DEVICES="none", OMP_NUM_THREADS="4",
               OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "STUDIO_BLENDER"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def mem_available():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            _, count, unit = line.split()
            if unit == "kB":
                return int(count) * 1024
    raise Refusal("mem_available_unverified")


def ram_admission():
    """Measure projected memory headroom before starting a Blender scope."""
    scopes = {}
    for scope in CGROUP.rglob("*.scope"):
        try:
            if (scope / "memory.max").read_text().strip() == str(SCOPE_BYTES):
                scopes[str(scope)] = int((scope / "memory.current").read_text())
        except FileNotFoundError:
            continue
    available = mem_available()
    # Busy scopes reserve their unused balance; idle ones (< 256 MiB) count at current use only.
    reserved = sum(max(0, SCOPE_BYTES - used) if used >= GIB // 4 else used
                   for used in scopes.values())
    return {"measurement_tag": "[MEASURED]", "available_bytes": available,
            "outstanding_bytes": reserved, "new_scope_bytes": SCOPE_BYTES,
            "projected_headroom_bytes": available - reserved - SCOPE_BYTES,
            "live_8g_scopes_bytes": scopes}


def require_admission():
    ram = ram_admission()
    if ram["projected_headroom_bytes"] < MIN_HEADROOM:
        raise Refusal("ram_admission_hold", "MemAvailable minus outstanding 8G scopes minus 8G >= 20 GiB")
    return ram


def checkpoint():
    if mem_available() < MIN_HEADROOM:
        raise Refusal("ram_checkpoint_hold", "when MemAvailable is at least 20 GiB")


def effective_scope():
    group = CGROUP / Path("/proc/self/cgroup").read_text().strip().removeprefix("0::/")
    limits = {}
    for name in ("memory.high", "memory.max", "memory.swap.max"):
        path = group / name
        limits[name] = int(path.read_text()) if path.exists() else None
    cpus = sorted(os.sched_getaffinity(0))
    ok = (group.name.endswith(".scope") and cpus == list(range(16))
          and limits == {"memory.high": 6 * GIB, "memory.max": SCOPE_BYTES, "memory.swap.max": 0})
    return ok, {"cgroup": str(group), "limits_bytes": limits, "cpus": cpus, "nice": os.nice(0)}


def scope_command():
    return ["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect",
            "--expand-environment=no", "-p", "MemoryHigh=6G", "-p", "MemoryMax=8G",
            "-p", "MemorySwapMax=0", "--", "/usr/bin/taskset", "-c", "0-15"]


def blender_command(script, args, threads=8):
    return [BLENDER, "-b", "--factory-startup", "--disable-autoexec", "-noaudio",
            "-t", str(threads), "--python-exit-code", "2", "--python", str(script), "--", *args]


def invoke(argv, work, name, timeout=1800, outer=False):
    """Run a child with pinned env; stdout/stderr are spooled under ``work``."""
    out, err = Path(work) / f"{name}.stdout", Path(work) / f"{name}.stderr"
    env = child_env(work) if not outer else {"PATH": "/usr/bin:/bin", **ENV}
    for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        if outer and key in os.environ:
            env[key] = os.environ[key]
    with out.open("wb") as stdout, err.open("wb") as stderr:
        result = subprocess.run(argv, cwd=work, env=env, stdout=stdout, stderr=stderr,
                                timeout=timeout, check=False)
    return {"argv": [str(a) for a in argv], "rc": result.returncode,
            "stdout": str(out.resolve()), "stderr": str(err.resolve())}


def in_scope():
    """True when this process already runs inside the admitted 8G scope."""
    try:
        return effective_scope()[0]
    except (OSError, ValueError):
        return False

