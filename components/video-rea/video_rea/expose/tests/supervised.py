# SPDX-License-Identifier: AGPL-3.0-only
"""Launch the component's CLI and MCP entry points under scoped-run-abs.sh only."""

import os
from pathlib import Path
import subprocess
import sys


def _pids(proc):
    for line in (proc / "self/status").read_text().splitlines():
        if line.startswith("NSpid:"):
            return [int(pid) for pid in line.split()[1:]]
    raise RuntimeError("missing PID namespace witness")


def run_module(name, *args, stdin=""):
    if name not in {"cli", "mcp"}:
        raise ValueError("unexpected exposure module")
    outer_proc = os.getenv("AF_PROC_PIDNS_PROC")
    if os.getenv("AF_PROC_BACKEND") != "pidns" or not outer_proc:
        raise RuntimeError("use scoped-run-abs.sh")
    inner, outer = _pids(Path("/proc")), _pids(Path(outer_proc))
    if not inner or len(outer) <= len(inner) or outer[-len(inner):] != inner or inner[-1] != os.getpid():
        raise RuntimeError("missing outer PID namespace witness")
    return subprocess.run(
        [sys.executable, "-B", "-m", f"video_rea.expose.{name}", *args],
        cwd=Path(__file__).resolve().parents[3], input=stdin,
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
