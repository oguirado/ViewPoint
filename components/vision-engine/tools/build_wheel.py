#!/usr/bin/env python3
"""Reproducible wheel: build from `git archive <rev>` with SOURCE_DATE_EPOCH = commit time of <rev>; prints the sha256.
Usage: python3 tools/build_wheel.py [<rev>=HEAD] [--out <dir>=build/wheels]   -> "<sha256>  <wheel path>"
Two builds of the same rev are byte-identical (stdlib + setuptools, no network, no build isolation)."""
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _git(*a):
    return subprocess.run(["git", "-C", str(ROOT), *a], check=True, capture_output=True).stdout


def build(rev="HEAD", out=None, root=ROOT):
    root = Path(root)
    out = Path(out) if out else root / "build" / "wheels"
    out.mkdir(parents=True, exist_ok=True)
    epoch = subprocess.run(["git", "-C", str(root), "show", "-s", "--format=%ct", rev + "^{commit}"], check=True,
                           capture_output=True, text=True).stdout.strip()
    tar = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", rev], check=True, capture_output=True).stdout
    with tempfile.TemporaryDirectory(prefix="vewheel_src_") as src, tempfile.TemporaryDirectory(prefix="vewheel_out_") as dst:
        with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
            tf.extractall(src, filter="data")
        env = dict(os.environ, SOURCE_DATE_EPOCH=epoch, PYTHONDONTWRITEBYTECODE="1", PIP_NO_INDEX="1",
                   PIP_DISABLE_PIP_VERSION_CHECK="1")
        subprocess.run([sys.executable, "-B", "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", dst, src],
                       check=True, env=env, cwd=src)
        wheels = sorted(Path(dst).glob("*.whl"))
        if len(wheels) != 1:
            raise SystemExit("expected one wheel, got %r" % [w.name for w in wheels])
        target = out / wheels[0].name
        data = wheels[0].read_bytes()
        target.write_bytes(data)
    return target, hashlib.sha256(data).hexdigest()


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    out = None
    if "--out" in a:
        k = a.index("--out")
        out = a[k + 1]
        del a[k:k + 2]
    rev = a[0] if a else "HEAD"
    path, sha = build(rev, out)
    print("%s  %s" % (sha, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
