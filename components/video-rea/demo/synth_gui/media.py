# SPDX-License-Identifier: AGPL-3.0-only

"""Run the fixture's media tools as supervised children of the scoped runner."""

import os
import subprocess
from pathlib import Path


def _namespace_pids(proc: Path) -> list[int]:
    status = (proc / "self/status").read_text(encoding="ascii")
    for line in status.splitlines():
        if line.startswith("NSpid:"):
            return [int(pid) for pid in line.split()[1:]]
    raise RuntimeError("Cannot establish the PID namespace")


def run_media_tool(tool: str, args: list[str], *, cwd: Path, timeout: float = 60) -> str:
    if tool not in {"ffmpeg", "ffprobe"}:
        raise ValueError(f"Unsupported media tool: {tool}")

    if type(timeout) not in (float, int) or not 0 < timeout <= 60:
        raise ValueError("Media tool timeout must be positive and at most 60 seconds")

    inherited_proc = os.environ.get("AF_PROC_PIDNS_PROC")
    if os.environ.get("AF_PROC_BACKEND") != "pidns" or not inherited_proc:
        raise RuntimeError("Run the synthetic GUI with scoped-run-abs.sh")
    inner_pids = _namespace_pids(Path("/proc"))
    outer_pids = _namespace_pids(Path(inherited_proc))
    if not inner_pids or len(outer_pids) <= len(inner_pids) or outer_pids[-1] != os.getpid():
        raise RuntimeError("The scoped runner PID namespace could not be verified")

    # subprocess.run reaps the registered child and kills only that child on timeout.
    result = subprocess.run(
        [f"/usr/bin/{tool}", *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"{tool} failed (rc {result.returncode}): {result.stderr.strip()}")
    return result.stdout
