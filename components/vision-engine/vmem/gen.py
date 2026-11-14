# @module {"id":"vm.gen","layer":5,"stage":"S7","owner":"VMEM","deps":["vc.canon","vc.errors","vm.blobs"],"book":"HB-VC-s7-vmem"}
"""@module vm.gen - generations and the atomic CURRENT flip (SPEC_VMEM §2, §6.1-6.3, §7)."""
import os
import re
import shutil

from visioncore import canon
from visioncore.errors import VqaError
from .blobs import fsync_dir, write_all

GEN_MAX = 999999
VM_GEN_KEEP = 4
API_VERSION = "1.1.0"
KIT_RELEASE = "release_5faf6cece458"
_GEN = re.compile(r"^(\d{6})$")
POINTS = ("stage_written", "gen_dir_fsynced", "gen_renamed", "gens_dir_fsynced",
          "current_tmp_created", "current_replaced", "root_fsynced")


def gen_name(n):
    if not isinstance(n, int) or n < 1:
        raise VqaError("E_USAGE", {"reason": "bad_gen", "gen": str(n)})
    if n > GEN_MAX:
        raise VqaError("E_GATE", {"reason": "gen_exhausted", "gen": n})
    return "%06d" % n


def gen_dir(root, n):
    return os.path.join(str(root), "gen", gen_name(n))


def list_gens(root):
    base = os.path.join(str(root), "gen")
    if not os.path.isdir(base):
        return []
    return sorted(int(m.group(1)) for x in os.listdir(base) for m in [_GEN.match(x)] if m)


def current_gen(root):
    """Gen number CURRENT points to, or None for an empty store."""
    p = os.path.join(str(root), "CURRENT")
    try:
        tgt = os.readlink(p)
    except FileNotFoundError:
        return None
    except OSError:
        raise VqaError("E_STALE", {"reason": "current_not_symlink"})
    m = re.match(r"^gen/(\d{6})$", tgt)
    if not m:
        raise VqaError("E_STALE", {"reason": "current_bad_target", "target": tgt[:40]})
    return int(m.group(1))


def in_window(root, n, keep=VM_GEN_KEEP):
    cur = current_gen(root)
    return cur is not None and cur - keep + 1 <= n <= cur and os.path.isdir(gen_dir(root, n))


def read_manifest(root, n):
    try:
        with open(os.path.join(gen_dir(root, n), "manifest.json"), "r", encoding="utf-8") as f:
            return canon.loads(f.read())
    except OSError:
        raise VqaError("E_STALE", {"reason": "manifest_missing", "gen": n})


def manifest_sha(root, n):
    try:
        with open(os.path.join(gen_dir(root, n), "manifest.json"), "rb") as f:
            return canon.sha256_bytes(f.read())
    except OSError:
        raise VqaError("E_STALE", {"reason": "manifest_missing", "gen": n})


def _write_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def build_manifest(n, parent, files, extra=None):
    m = {"schema": "vmem.manifest/v1", "gen": n, "parent_gen": parent, "api_version": API_VERSION,
         "kit_release": KIT_RELEASE,
         "files": {rel: canon.sha256_bytes(d) for rel, d in sorted(files.items())}}
    for k, v in (extra or {}).items():
        if k in m:
            raise VqaError("E_USAGE", {"reason": "manifest_key_reserved", "key": k})
        m[k] = v
    return m


def publish_gen(root, n, parent, files, extra=None, point=None):  # @anchor:vm.gen.publish_gen
    """Stage gen/<n>.tmp, fsync everything, rename to gen/<n>. Returns manifest sha256.
    files: {relpath: bytes}; a failure leaves gen/<n> absent."""
    point = point or (lambda name: None)
    gname = gen_name(n)
    base = os.path.join(str(root), "gen")
    os.makedirs(base, exist_ok=True)
    final, tmp = os.path.join(base, gname), os.path.join(base, gname + ".tmp")
    if os.path.exists(final):
        raise VqaError("E_STALE", {"reason": "gen_exists", "gen": n})
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    for rel in files:
        if rel.startswith("/") or ".." in rel.split("/") or rel == "manifest.json":
            raise VqaError("E_USAGE", {"reason": "bad_relpath", "path": rel[:60]})
    m = build_manifest(n, parent, files, extra)
    mbytes = canon.dumps(m).encode("utf-8")
    for rel, data in sorted(files.items()):
        _write_file(os.path.join(tmp, rel), data)
    _write_file(os.path.join(tmp, "manifest.json"), mbytes)
    for dp, _dn, _fn in os.walk(tmp):
        fsync_dir(dp)
    point("stage_written")
    fsync_dir(tmp)
    point("gen_dir_fsynced")
    os.rename(tmp, final)
    point("gen_renamed")
    fsync_dir(base)
    point("gens_dir_fsynced")
    return canon.sha256_bytes(mbytes)


def flip(root, n, point=None):  # @anchor:vm.gen.flip
    """CURRENT.tmp symlink -> os.replace(CURRENT) -> fsync store dir."""
    point = point or (lambda name: None)
    if not os.path.isdir(gen_dir(root, n)):
        raise VqaError("E_STALE", {"reason": "flip_target_missing", "gen": n})
    tmp, cur = os.path.join(str(root), "CURRENT.tmp"), os.path.join(str(root), "CURRENT")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    os.symlink("gen/" + gen_name(n), tmp)
    point("current_tmp_created")
    os.replace(tmp, cur)
    point("current_replaced")
    fsync_dir(root)
    point("root_fsynced")


def cleanup_unacked(root, acked_max):
    """Remove *.tmp leftovers and gen dirs above the highest acknowledged gen. Returns removed names."""
    removed = []
    base = os.path.join(str(root), "gen")
    if os.path.isdir(base):
        for x in sorted(os.listdir(base)):
            m = _GEN.match(x)
            if x.endswith(".tmp") or (m and int(m.group(1)) > acked_max):
                shutil.rmtree(os.path.join(base, x), ignore_errors=True)
                removed.append(x)
    try:
        os.unlink(os.path.join(str(root), "CURRENT.tmp"))
        removed.append("CURRENT.tmp")
    except FileNotFoundError:
        pass
    return removed


def delete_gen(root, n):
    shutil.rmtree(gen_dir(root, n), ignore_errors=True)
