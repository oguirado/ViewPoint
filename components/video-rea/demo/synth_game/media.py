# SPDX-License-Identifier: AGPL-3.0-only
"""Supervise the game fixture's media tools inside the scoped PID namespace."""
import math
import os
import subprocess
from pathlib import Path


OUTER_PROC = Path("/proc/1/fd/9")
MEDIA_TIMEOUT_S = 60


def _namespace_pids(proc: Path) -> list[int]:
    for line in (proc / "self/status").read_text(encoding="ascii").splitlines():
        if line.startswith("NSpid:"):
            return [int(pid) for pid in line.split()[1:]]
    raise ValueError(f"NSpid missing from {proc}/self/status")


def _require_pid_namespace() -> None:
    if (os.environ.get("AF_PROC_BACKEND") != "pidns"
            or os.environ.get("AF_PROC_PIDNS_PROC") != str(OUTER_PROC)):
        raise RuntimeError("Run the synthetic game in the scoped runner PID namespace")
    try:
        inner = _namespace_pids(Path("/proc"))
        outer = _namespace_pids(OUTER_PROC)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Cannot read the scoped runner PID namespace") from exc
    if inner != [os.getpid()] or len(outer) <= 1 or outer[-1] != inner[0]:
        raise RuntimeError("Cannot verify the scoped runner PID namespace")


def _run_child(argv: list[str], *, cwd: Path, timeout: float) -> str:
    _require_pid_namespace()
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Child timeout must be positive and finite")
    # On timeout subprocess.run kills and waits for this Popen child by PID.
    result = subprocess.run(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
        encoding="utf-8", errors="replace", timeout=timeout, start_new_session=True,
    )
    if result.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} failed (rc {result.returncode}): {result.stderr.strip()}")
    return result.stdout


def run_media_tool(tool: str, args: list[str], *, cwd: Path, timeout: float) -> str:
    if tool not in {"ffmpeg", "ffprobe"}:
        raise ValueError(f"Unsupported media tool: {tool}")
    return _run_child([f"/usr/bin/{tool}", *args], cwd=cwd, timeout=timeout)
