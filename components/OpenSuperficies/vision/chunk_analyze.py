"""Per-chunk engine analysis: one ramws chunk dir (frames.rgb24) -> ONE integer JSON row on stdout.

Imports vision.pin first (fail closed, rc 5 E_PIN). frames.rgb24 is mapped read-only; the tile classes are stepped in memory
(describe_padded + classify_tiles); GuiAdapter.adapt / VisionRun.write are never used, nothing is written.
rc: 0 ok, 2 usage / E_GRID, 5 gate / pin / E_RAM_NOT_TMPFS / unforeseen. Never 1.

  python3 vision/chunk_analyze.py analyze --chunk-dir D --w W --h H --frames N --grid 16|32|64
"""
import fcntl
import json
import os
import re
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vision import pin  # noqa: E402  (must precede every engine import)

pin.activate()
import numpy as np  # noqa: E402
from visioncore import change, decode, desc  # noqa: E402
from visioncore import grid as vgrid  # noqa: E402
from visioncore.errors import VqaError  # noqa: E402

RAMWS_ROOT = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0"
TEST_ROOT_PREFIX = "/dev/shm/vqa-ramws-test/"
CLASSES = ("UNCHANGED", "JITTER", "CHANGED", "MOVED", "FLICKER", "FROZEN", "DRIFT")
CHUNK_RE = re.compile(r"^chunk_([0-9]+)$")
GRIDS = (16, 32, 64)


class Fail(Exception):
    def __init__(self, code, rc, detail=""):
        super().__init__(detail)
        self.code, self.rc, self.detail = code, rc, detail


def _managed_run_dir(real):
    """Run dir of a managed ramws session: directly under the law root, owner.json, .lock held by the session (LOCK_SH
    fails while the host holds EX); OS_CU_TEST_ROOT=1 admits a dir under /dev/shm/vqa-ramws-test/ instead."""
    if os.environ.get("OS_CU_TEST_ROOT") == "1" and real.startswith(TEST_ROOT_PREFIX):
        return
    if os.path.dirname(real) != RAMWS_ROOT or not os.path.isfile(os.path.join(real, "owner.json")):
        raise Fail("E_RAM_NOT_TMPFS", 5, "unmanaged run dir")
    try:
        fd = os.open(os.path.join(real, ".lock"), os.O_RDONLY)
    except OSError:
        raise Fail("E_RAM_NOT_TMPFS", 5, "no session lock") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        fcntl.flock(fd, fcntl.LOCK_UN)
        raise Fail("E_RAM_NOT_TMPFS", 5, "session lock not held")
    finally:
        os.close(fd)


def check_chunk_dir(d):
    real = os.path.realpath(d)
    m = CHUNK_RE.match(os.path.basename(real))
    if not m or not os.path.isdir(real):
        raise Fail("E_RAM_NOT_TMPFS", 5, "not a chunk_<k> dir")
    _managed_run_dir(os.path.dirname(real))
    return real, int(m.group(1))


def open_frames(real):
    """frames.rgb24 must be a regular, non-symlink file whose realpath is itself (stays in the chunk dir); opened O_NOFOLLOW
    and verified on the fd (same inode as the lstat) so a swap between check and map is caught. Returns (fd, path to map)."""
    fp = os.path.join(real, "frames.rgb24")
    try:
        st = os.lstat(fp)
        if not stat.S_ISREG(st.st_mode) or os.path.realpath(fp) != fp:
            raise Fail("E_RAM_NOT_TMPFS", 5, "bad frames.rgb24")
        fd = os.open(fp, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Fail("E_RAM_NOT_TMPFS", 5, "bad frames.rgb24") from None
    fs = os.fstat(fd)
    if not stat.S_ISREG(fs.st_mode) or (fs.st_dev, fs.st_ino) != (st.st_dev, st.st_ino):
        os.close(fd)
        raise Fail("E_RAM_NOT_TMPFS", 5, "bad frames.rgb24")
    return fd, "/proc/self/fd/%d" % fd


def analyze(chunk_dir, w, h, frames, grid):
    real, chunk = check_chunk_dir(chunk_dir)
    g = vgrid.Grid(w, h)
    if g.s != grid:
        raise Fail("E_GRID", 2, "grid %d != Grid(%d,%d).s %d" % (grid, w, h, g.s))
    t0 = time.perf_counter_ns()
    fd, fpath = open_frames(real)
    try:
        fr = decode.rgb24_raw(fpath, w, h, frames)
    finally:
        os.close(fd)
    counts = np.zeros(len(CLASSES), np.int64)
    st = prev_pad = prev_tiles = None
    for i in range(frames):
        pad = g.pad(np.asarray(fr[i]))
        y = desc.luma_of_padded(pad)
        tiles, wk = desc.describe_padded(pad, g, prev_pad, None, prev_tiles=prev_tiles, y_plane=y, lazy_xh=True)
        if st is None:
            _, st, s = change.classify_first(change.ClassState.init(tiles), tiles, y)
        else:
            _, st, s = change.classify_tiles(st, tiles, wk, y, (g.s, g.gw, g.gh))
        prev_pad, prev_tiles = pad, tiles
        counts += np.asarray(s["n"], np.int64)
    del fr
    elapsed_us = (time.perf_counter_ns() - t0) // 1000
    return {"chunk": chunk, "frames": frames, "grid": g.s,
            "class_counts": {k: int(v) for k, v in zip(CLASSES, counts)},
            "analyze_us_per_frame": max(1, elapsed_us // frames),
            "engine_wheel_sha8": pin.EXPECTED_WHEEL_SHA256[:8]}


def _int(v, name, lo=1):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise Fail("E_USAGE", 2, "%s must be an integer" % name) from None
    if n < lo:
        raise Fail("E_USAGE", 2, "%s must be >= %d" % (name, lo))
    return n


def main(argv):
    try:
        if not argv or argv[0] != "analyze" or len(argv[1:]) % 2:
            raise Fail("E_USAGE", 2, "usage: analyze --chunk-dir D --w W --h H --frames N --grid G")
        opt = {}
        for k, v in zip(argv[1::2], argv[2::2]):
            if k not in ("--chunk-dir", "--w", "--h", "--frames", "--grid") or k in opt:
                raise Fail("E_USAGE", 2, "bad flag " + k)
            opt[k] = v
        if len(opt) != 5:
            raise Fail("E_USAGE", 2, "missing flag")
        row = analyze(opt["--chunk-dir"], _int(opt["--w"], "w"), _int(opt["--h"], "h"), _int(opt["--frames"], "frames"),
                      _int(opt["--grid"], "grid"))
        sys.stdout.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except Fail as e:
        sys.stdout.write(json.dumps({"code": e.code, "detail": e.detail}, sort_keys=True) + "\n")
        return e.rc
    except VqaError as e:
        sys.stdout.write(json.dumps({"code": "E_GATE", "detail": str(e)[:120]}, sort_keys=True) + "\n")
        return 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
