"""Shared pure-stdlib canonical helpers (R-B1, R-B9). Host and Blender may both import this module."""
import json
import os


# @anchor:bpy.canon.canonical_json
def canonical_json(value):
    """Sorted-key compact JSON with trailing newline; floats are refused (R-B8)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"


# @anchor:bpy.canon.cycles_seed
def cycles_seed(bench_no, k, ri):
    """R-B9: SplitMix64 finalizer over an injective (bench_no, k, ri) pre-image, masked to int31."""
    for v, bits in ((bench_no, 24), (k, 20), (ri, 20)):
        if type(v) is not int or not 0 <= v < (1 << bits):
            raise ValueError("seed coordinate")
    mask = (1 << 64) - 1
    x = (((bench_no << 40) | (k << 20) | ri) * 0x9E3779B97F4A7C15) & mask
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & mask
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & mask
    return (x ^ (x >> 31)) & 0x7FFFFFFF


# STORAGE LAW L2: single RAM workspace root definition (overridable via VQA_RAMWS_ROOT).
RAMWS_ROOT = os.environ.get("VQA_RAMWS_ROOT", "/dev/shm/vqa-ramws")
import re  # noqa: E402
import stat  # noqa: E402
import sys  # noqa: E402

RAM_FS = ("tmpfs", "ramfs")


def _fail_ram(msg):
    sys.stderr.write("E_RAM_NOT_TMPFS: %s\n" % msg)
    sys.exit(5)


def _unesc_mount(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def is_ram_fs(p, mountinfo="/proc/self/mountinfo"):
    """Return fstype of the longest mount-point prefix of realpath(p), or None if not tmpfs/ramfs."""
    real = os.path.realpath(str(p))
    best, fst = "", None
    with open(mountinfo, encoding="utf-8") as f:
        for line in f:
            head, sep, tail = line.partition(" - ")
            parts = head.split()
            if not sep or len(parts) < 5:
                continue
            mp = _unesc_mount(parts[4])
            if (real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                best, fst = mp, tail.split()[0]
    return fst if fst in RAM_FS else None


def _nearest_existing_ancestor_stat(path):
    anc = path
    while True:
        try:
            return anc, os.stat(anc)
        except FileNotFoundError:
            parent = os.path.dirname(anc)
            if parent == anc:
                _fail_ram("no existing ancestor for %s" % path)
            anc = parent
        except OSError as exc:
            _fail_ram("stat failed for %s: %s" % (anc, exc))


def _reject_if_symlink(path):
    probe = path.rstrip(os.sep) or os.sep if isinstance(path, str) else path
    try:
        st = os.lstat(probe)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _fail_ram("lstat failed for %s: %s" % (path, exc))
    if stat.S_ISLNK(st.st_mode):
        _fail_ram("symlink not allowed at %s" % path)
    return st


def _verified_root(ramws_root=None, mountinfo="/proc/self/mountinfo", check_mountinfo=True):
    root_raw = os.environ.get("VQA_RAMWS_ROOT", RAMWS_ROOT) if ramws_root is None else ramws_root
    if not isinstance(root_raw, str) or not root_raw or "\x00" in root_raw:
        _fail_ram("invalid ramws root %r" % (root_raw,))
    real_root = os.path.realpath(root_raw)
    root_anc, root_st = _nearest_existing_ancestor_stat(real_root)
    if check_mountinfo and is_ram_fs(root_anc, mountinfo=mountinfo) is None:
        _fail_ram("%s is not tmpfs/ramfs" % real_root)
    return real_root, root_st.st_dev


def require_ram_out(path, ramws_root=None, mountinfo="/proc/self/mountinfo"):
    """STORAGE LAW L2: verify root is tmpfs/ramfs, path stays inside it on root_dev, and final path is no symlink."""
    if not isinstance(path, str) or not path or "\x00" in path:
        _fail_ram("invalid output path %r" % (path,))
    real_root, root_dev = _verified_root(ramws_root=ramws_root, mountinfo=mountinfo, check_mountinfo=True)
    _reject_if_symlink(path)
    real = os.path.realpath(path)
    if not (real + os.sep).startswith(real_root + os.sep):
        _fail_ram("%s is outside %s" % (path, real_root))
    _reject_if_symlink(real)
    anc, anc_st = _nearest_existing_ancestor_stat(real)
    _reject_if_symlink(anc)
    if anc_st.st_dev != root_dev:
        _fail_ram("%s device %s != root device %s" % (real, anc_st.st_dev, root_dev))
    if is_ram_fs(anc, mountinfo=mountinfo) is None:
        _fail_ram("%s is not tmpfs/ramfs" % real)
    return real


def ram_child(base_canonical, *names, ramws_root=None):
    """STORAGE LAW L2: join child components under a canonical RAM dir on root_dev without symlinks."""
    if not isinstance(base_canonical, str) or not base_canonical or "\x00" in base_canonical:
        _fail_ram("invalid base %r" % (base_canonical,))
    _reject_if_symlink(base_canonical)
    real_base = os.path.realpath(base_canonical)
    _, root_dev = _verified_root(ramws_root=ramws_root, check_mountinfo=False)
    _, base_st = _nearest_existing_ancestor_stat(real_base)
    if base_st.st_dev != root_dev:
        _fail_ram("%s device %s != root device %s" % (real_base, base_st.st_dev, root_dev))
    if not names:
        _fail_ram("empty child name under %s" % real_base)
    cur = real_base
    for name in names:
        if (not isinstance(name, str) or not name or name in (".", "..")
                or "\x00" in name or os.path.isabs(name)
                or os.sep in name or (os.altsep and os.altsep in name)):
            _fail_ram("unsafe child %r under %s" % (name, real_base))
        cur = os.path.join(cur, name)
        _reject_if_symlink(cur)
    real = os.path.realpath(cur)
    prefix = real_base if real_base.endswith(os.sep) else real_base + os.sep
    if real == real_base or not real.startswith(prefix):
        _fail_ram("%s is outside %s" % (real, real_base))
    _reject_if_symlink(real)
    anc, anc_st = _nearest_existing_ancestor_stat(real)
    _reject_if_symlink(anc)
    if anc_st.st_dev != root_dev:
        _fail_ram("%s device %s != root device %s" % (real, anc_st.st_dev, root_dev))
    return real


def verify_written(paths_or_dir, ramws_root=None):
    """STORAGE LAW L2: walk output paths/dir with os.lstat and exit 5 if any entry is a symlink or off root_dev."""
    _, root_dev = _verified_root(ramws_root=ramws_root, check_mountinfo=False)
    if isinstance(paths_or_dir, (str, os.PathLike)):
        targets = [os.fspath(paths_or_dir)]
    else:
        targets = [os.fspath(p) for p in paths_or_dir]
    if not targets:
        _fail_ram("empty verify_written target list")

    def _lstat_check(p):
        if not isinstance(p, str) or not p or "\x00" in p:
            _fail_ram("invalid written path %r" % (p,))
        probe = p.rstrip(os.sep) or os.sep
        try:
            st = os.lstat(probe)
        except FileNotFoundError:
            anc = os.path.dirname(probe)
            while True:
                try:
                    st_anc = os.lstat(anc)
                    break
                except FileNotFoundError:
                    parent = os.path.dirname(anc)
                    if parent == anc:
                        _fail_ram("no existing ancestor for %s" % p)
                    anc = parent
                except OSError as exc:
                    _fail_ram("lstat failed for %s: %s" % (anc, exc))
            if stat.S_ISLNK(st_anc.st_mode) or st_anc.st_dev != root_dev:
                _fail_ram("%s ancestor failed device/symlink check" % p)
            return None
        except OSError as exc:
            _fail_ram("lstat failed for %s: %s" % (p, exc))
        if stat.S_ISLNK(st.st_mode):
            _fail_ram("symlink not allowed at %s" % p)
        if st.st_dev != root_dev:
            _fail_ram("%s device %s != root device %s" % (p, st.st_dev, root_dev))
        return st

    stack = list(targets)
    while stack:
        cur = stack.pop()
        st = _lstat_check(cur)
        if st is not None and stat.S_ISDIR(st.st_mode):
            try:
                entries = sorted(os.listdir(cur))
            except OSError as exc:
                _fail_ram("listdir failed for %s: %s" % (cur, exc))
            for entry in entries:
                stack.append(os.path.join(cur, entry))
