#!/usr/bin/env python3
"""Offline, reproducible build of rust/visioncore_native -> build/native/visioncore_native.abi3.so (+ build/native/receipt.json).
Usage: python3 tools/build_native.py [--out <dir>=build/native]
rc 0 built; rc 3 cargo, Cargo.lock or crates AUSENTE (receipt says which: "cargo AUSENTE" / "lock AUSENTE" / "crates AUSENTE", OPERATOR command printed). SOURCE_DATE_EPOCH defaults to HEAD commit time."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRATE = "rust/visioncore_native"
LIB = "visioncore_native.abi3.so"
OPERATOR = ("export PATH=$HOME/.cargo/bin:$PATH; cd rust/visioncore_native && ~/.cargo/bin/cargo fetch  "
            "(network; needs pyo3 0.22 + rayon 1.x in ~/.cargo/registry), then rerun tools/build_native.py")
RUSTFLAGS = "-C target-feature=+neon --remap-path-prefix=%s=."


def find_cargo(env=None):
    env = os.environ if env is None else env
    p = shutil.which("cargo", path=env.get("PATH", ""))
    if p:
        return p
    home = Path(env.get("HOME", "~")).expanduser() / ".cargo" / "bin" / "cargo"
    return str(home) if home.is_file() else None


def _epoch(root):
    if os.environ.get("SOURCE_DATE_EPOCH"):
        return os.environ["SOURCE_DATE_EPOCH"]
    r = subprocess.run(["git", "-C", str(root), "show", "-s", "--format=%ct", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "0"


def _write(out, rec):
    out.mkdir(parents=True, exist_ok=True)
    (out / "receipt.json").write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n")
    return rec


def build(root=ROOT, out=None, env=None):
    """-> (rc, receipt dict). Output only under `out` (default <root>/build/native)."""
    root = Path(root)
    out = Path(out) if out else root / "build" / "native"
    env = dict(os.environ if env is None else env)
    rec = {"status": "AUSENTE", "so": LIB, "sha256": None, "operator": OPERATOR}
    cargo = find_cargo(env)
    if not cargo:
        rec["reason"] = "cargo AUSENTE (PATH and ~/.cargo/bin)"
        return 3, _write(out, rec)
    manifest = root / CRATE / "Cargo.toml"
    if not (root / CRATE / "Cargo.lock").is_file():  # checked before cargo metadata, which would write the lock into the source tree
        rec["reason"] = "lock AUSENTE: %s/Cargo.lock missing; the build is --locked (OPERATOR fetch creates it, commit it)" % CRATE
        return 3, _write(out, rec)
    env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")
    rec["cargo"] = subprocess.run([cargo, "--version"], capture_output=True, text=True, env=env).stdout.strip()
    meta = subprocess.run([cargo, "metadata", "--offline", "--locked", "--format-version", "1", "--manifest-path", str(manifest)],
                          capture_output=True, text=True, env=env, cwd=str(root))
    if meta.returncode != 0:
        rec["reason"] = "crates AUSENTE offline: " + (meta.stderr.strip().splitlines() or ["cargo metadata failed"])[0][:300]
        return 3, _write(out, rec)
    epoch = _epoch(root)
    tgt = out / "target"
    env.update(SOURCE_DATE_EPOCH=epoch, CARGO_TARGET_DIR=str(tgt), CARGO_INCREMENTAL="0",
               RUSTFLAGS=RUSTFLAGS % (root.resolve()))
    r = subprocess.run([cargo, "build", "--release", "--offline", "--locked", "--manifest-path", str(manifest)],
                       capture_output=True, text=True, env=env, cwd=str(root))
    if r.returncode != 0:
        rec.update(status="FAILED", reason=r.stderr.strip()[-600:])
        return 1, _write(out, rec)
    built = tgt / "release" / "libvisioncore_native.so"
    data = built.read_bytes()
    (out / LIB).write_bytes(data)
    rec.update(status="OK", sha256=hashlib.sha256(data).hexdigest(), source_date_epoch=epoch, reason=None)
    return 0, _write(out, rec)


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    out = a[a.index("--out") + 1] if "--out" in a else None
    rc, rec = build(out=out)
    if rc == 0:
        print("%s  %s" % (rec["sha256"], LIB))
    else:
        print("%s: %s\nOPERATOR: %s" % (rec["status"], rec.get("reason"), OPERATOR), file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
