# SPDX-License-Identifier: AGPL-3.0-only
"""Shared refusal, artifact and background Blender helpers."""
from hashlib import file_digest
import json
import os
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile

ENV = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1",
       "CUDA_VISIBLE_DEVICES": "", "NVIDIA_VISIBLE_DEVICES": "none", "OPENBLAS_NUM_THREADS": "1"}


class Hold(ValueError):
    """Input or measurement rejected by a rig gate."""


def need(condition, reason):
    if not condition:
        raise Hold(reason)


def document(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def pin(path):
    path = Path(path)
    with path.open("rb") as stream:
        return {"sha256": file_digest(stream, "sha256").hexdigest(), "bytes": stream.tell()}


def write_bytes(path, data):
    path = Path(path)
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as output:
        pending = Path(output.name)
        try:
            output.write(data)
            output.close()
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)


def write(path, value):
    write_bytes(path, (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())
    return Path(path)


def invoke_blender(work, script, args, *, blender="blender", timeout=900):
    work = Path(work).resolve()
    env = os.environ | ENV | {"TMPDIR": str(work), "XDG_CACHE_HOME": str(work),
                              "XDG_CONFIG_HOME": str(work), "BLENDER_USER_CONFIG": str(work)}
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    binary = str(Path(blender).resolve()) if os.sep in blender else blender
    command = [binary, "-b", "--factory-startup", "--disable-autoexec", "-noaudio",
               "-t", "8", "--python-exit-code", "2", "--python", str(script), "--", *map(str, args)]
    result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True,
                            errors="replace", timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"blender_rc_{result.returncode}: {(result.stderr or result.stdout)[-550:].strip()}")
