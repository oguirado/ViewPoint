"""Builds the real pinned wheel once (gate-grade; ~2 s) so shim tests can run against it."""
import subprocess
import sys

from vision.tests.helpers import REPO

WHEEL = REPO / "build" / "wheels" / "vision_engine-1.1.0-py3-none-any.whl"


def ensure_real_wheel():
    if not WHEEL.exists():
        subprocess.run(["taskset", "-c", "0,1", sys.executable, "-B", str(REPO / "vision" / "build_wheel.py")], cwd=str(REPO),
                       check=True, timeout=250)
    return WHEEL
