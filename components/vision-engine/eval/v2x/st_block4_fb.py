"""V2EVAL2 frame-batched post-pass variant adapter calling vqa_k_st_block4 binary."""

import atexit
import os
from pathlib import Path
import struct
import subprocess
import zlib
import numpy as np
from visioncore.layout import grid_params


def _find_binary():
    env_bin = os.environ.get("VQA_K_ST_BLOCK4_BIN")
    if env_bin is not None:
        if env_bin:
            p = Path(env_bin)
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
        raise ImportError(f"VQA_K_ST_BLOCK4_BIN set to '{env_bin}' but executable not found")

    repo_root = Path(__file__).resolve().parents[2]
    rel_bin = repo_root / "rust" / "vqa_k_st_block4" / "target" / "release" / "vqa_k_st_block4"
    if rel_bin.is_file() and os.access(rel_bin, os.X_OK):
        return str(rel_bin)

    raise ImportError(f"vqa_k_st_block4 binary not found at '{rel_bin}'")


BIN_PATH = _find_binary()


def _content_digest(arr: np.ndarray) -> int:
    if arr.size <= 65536:
        return zlib.crc32(arr)
    h = arr.shape[0]
    c = zlib.crc32(arr[:1])
    c = zlib.crc32(arr[h >> 1 : (h >> 1) + 1], c)
    return zlib.crc32(arr[-1:], c)


class SubprocessWorker:

    def __init__(self, bin_path, env_bin=None):
        self.bin_path = bin_path
        self.env_bin = env_bin
        self.proc = subprocess.Popen(
            [bin_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )
        self._prev_ref = None
        self._cur_ref = None
        self._prev_row_crcs = None
        self._cur_row_crcs = None
        self._cache_key = None
        self._cache_results = None
        self._grid_cache = {}
        self.batch_count = 0

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        stdout = self.proc.stdout
        while len(buf) < n:
            chunk = stdout.read(n - len(buf))
            if not chunk:
                raise RuntimeError("Unexpected EOF from vqa_k_st_block4 binary")
            buf.extend(chunk)
        return bytes(buf)

    def _run_batch(self, prev: np.ndarray, cur: np.ndarray, W: int, H: int, tile_s: int, max_d_val: int, gw: int, gh: int, coords_bytes: bytes):
        n_tiles = gw * gh
        frame_bytes = W * H

        hdr = struct.pack("<BiiiiI", 1, W, H, tile_s, max_d_val, n_tiles)
        size_buf = struct.pack("<Q", frame_bytes)

        stdin = self.proc.stdin
        stdin.write(hdr)
        stdin.write(coords_bytes)
        stdin.write(size_buf)
        stdin.write(memoryview(prev))
        stdin.write(memoryview(cur))
        stdin.flush()

        header = self._read_exact(8)
        status, n_outputs = struct.unpack("<iI", header)
        if status != 0:
            raise RuntimeError(f"vqa_k_st_block4 failed with status {status}")

        raw_results = self._read_exact(16 * n_outputs)
        return np.frombuffer(raw_results, dtype="<i4").reshape(gh, gw, 4).tolist()

    def subtile_motion(self, prev_luma, cur_luma, s=None, tile_xy=(0, 0), max_d=16):
        if isinstance(prev_luma, np.ndarray) and prev_luma.dtype == np.uint8 and prev_luma.flags.c_contiguous:
            prev = prev_luma
        else:
            prev = np.ascontiguousarray(prev_luma, dtype=np.uint8)

        if isinstance(cur_luma, np.ndarray) and cur_luma.dtype == np.uint8 and cur_luma.flags.c_contiguous:
            cur = cur_luma
        else:
            cur = np.ascontiguousarray(cur_luma, dtype=np.uint8)

        if prev.ndim != 2 or cur.ndim != 2 or prev.shape != cur.shape:
            return None

        H, W = cur.shape
        wh = (W, H)
        ginfo = self._grid_cache.get(wh)
        if ginfo is None:
            try:
                s_grid, gw, gh = grid_params(W, H)
            except Exception:
                return None
            tx_g, ty_g = np.meshgrid(np.arange(gw, dtype="<i4"), np.arange(gh, dtype="<i4"))
            coords = np.empty((gh * gw, 2), dtype="<i4")
            coords[:, 0] = tx_g.ravel()
            coords[:, 1] = ty_g.ravel()
            coords_bytes = coords.tobytes()
            ginfo = (s_grid, gw, gh, coords_bytes)
            self._grid_cache[wh] = ginfo
        else:
            s_grid, gw, gh, coords_bytes = ginfo

        tile_s = s_grid if (s is None or int(s) <= 0) else int(s)
        max_d_val = int(max_d)
        tx, ty = int(tile_xy[0]), int(tile_xy[1])

        prev_dig = _content_digest(prev)
        cur_dig = _content_digest(cur)
        key = (
            id(prev_luma),
            id(cur_luma),
            prev.shape,
            cur.shape,
            tile_s,
            max_d_val,
            prev_dig,
            cur_dig,
        )

        cache_hit = (
            self._prev_ref is prev_luma
            and self._cur_ref is cur_luma
            and self._cache_key == key
            and self._cache_results is not None
        )

        if cache_hit and prev.size > 65536 and 0 <= ty < gh:
            y0 = ty * tile_s
            y1 = min(H, (ty + 1) * tile_s)
            if prev.flags.writeable and self._prev_row_crcs is not None:
                if zlib.crc32(prev[y0:y1]) != self._prev_row_crcs[ty]:
                    cache_hit = False
            if cache_hit and cur.flags.writeable and self._cur_row_crcs is not None:
                if zlib.crc32(cur[y0:y1]) != self._cur_row_crcs[ty]:
                    cache_hit = False

        if not cache_hit:
            results = self._run_batch(prev, cur, W, H, tile_s, max_d_val, gw, gh, coords_bytes)
            self._prev_ref = prev_luma
            self._cur_ref = cur_luma
            if prev.size > 65536 and (prev.flags.writeable or cur.flags.writeable):
                self._prev_row_crcs = tuple(
                    zlib.crc32(prev[ry * tile_s : min(H, (ry + 1) * tile_s)]) for ry in range(gh)
                )
                self._cur_row_crcs = tuple(
                    zlib.crc32(cur[ry * tile_s : min(H, (ry + 1) * tile_s)]) for ry in range(gh)
                )
            else:
                self._prev_row_crcs = None
                self._cur_row_crcs = None
            self._cache_key = key
            self._cache_results = results
            self.batch_count += 1

        if self._cache_results is None or tx < 0 or tx >= gw or ty < 0 or ty >= gh:
            return None
        r = self._cache_results[ty][tx]
        if r[3] != 0:
            return (r[0], r[1], r[2])
        return None

    def close(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.write(b"\x00")
                    self.proc.stdin.flush()
                    self.proc.wait(timeout=1.0)
                except Exception:
                    self.proc.kill()
                    self.proc.wait(timeout=1.0)
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except Exception:
                pass
            try:
                if self.proc.stdout:
                    self.proc.stdout.close()
            except Exception:
                pass


if "_WORKER" not in globals():
    _WORKER = None
if "_ATEXIT_REGISTERED" not in globals():
    _ATEXIT_REGISTERED = False


def get_worker():
    global _WORKER, _ATEXIT_REGISTERED
    env_bin = os.environ.get("VQA_K_ST_BLOCK4_BIN")
    if (
        _WORKER is None
        or _WORKER.proc.poll() is not None
        or _WORKER.env_bin != env_bin
    ):
        bin_path = _find_binary()
        if _WORKER is not None:
            _WORKER.close()
        _WORKER = SubprocessWorker(bin_path, env_bin)
        if not _ATEXIT_REGISTERED:
            atexit.register(_cleanup)
            _ATEXIT_REGISTERED = True
    return _WORKER


def _cleanup():
    global _WORKER
    if _WORKER is not None:
        _WORKER.close()
        _WORKER = None


def subtile_motion(prev_luma, cur_luma, s=None, tile_xy=(0, 0), max_d=16):
    """Subtile motion API implementation forwarding frame-batched requests to vqa_k_st_block4 binary."""
    worker = get_worker()
    return worker.subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d)
