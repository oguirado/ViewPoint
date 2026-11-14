"""Shared test helpers: synthetic wheels and subprocess runners (stdlib only)."""
import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRATCH = REPO / "build" / "room_L4" / "tests"


def sha256_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def make_wheel(path, files):
    """Deterministic synthetic wheel (zip) with fixed timestamps."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            zi = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, files[name])
    return path


FAKE_FILES = {
    "vadapt/__init__.py": "",
    "vadapt/ramws.py": "MARK = 'fake'\n",
    "visioncore/__init__.py": "",
    "vmem/__init__.py": "",
    "vision_engine-0.0.0.dist-info/RECORD": "x\n",
}


def run_py(code, *, cwd=None, pythonpath=None, timeout=60, extra_env=None):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra_env or {})
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(["taskset", "-c", "0,1", sys.executable, "-B", "-c", code], cwd=str(cwd or REPO), env=env,
                          capture_output=True, text=True, timeout=timeout)
