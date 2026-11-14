"""V2EVAL2 post-pass variant adapter calling vqa_k_st_block4 binary."""

import os
import struct
import subprocess
import sys
import numpy as np
from visioncore.layout import grid_params

# Find release binary path
DEFAULT_BIN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "rust",
    "vqa_k_st_block4",
    "target",
    "release",
    "vqa_k_st_block4",
)
BIN_PATH = os.environ.get("VQA_K_ST_BLOCK4_BIN", DEFAULT_BIN_PATH)

if not os.path.exists(BIN_PATH) or not os.path.isfile(BIN_PATH):
    raise ImportError(f"vqa_k_st_block4 binary not found at {BIN_PATH}")


class SubprocessWorker:
    def __init__(self, bin_path):
        self.proc = subprocess.Popen(
            [bin_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )

    def _read_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                raise RuntimeError("Unexpected EOF from vqa_k_st_block4 binary")
            buf.extend(chunk)
        return bytes(buf)

    def compute_batch(self, prev_luma, cur_luma, s, tiles, max_d):
        prev_luma = np.asarray(prev_luma, dtype=np.uint8)
        cur_luma = np.asarray(cur_luma, dtype=np.uint8)
        H, W = cur_luma.shape

        s_grid, gw, gh = grid_params(W, H)
        if s is None:
            s = s_grid

        n_tiles = len(tiles)
        prev_bytes = prev_luma.tobytes()
        cur_bytes = cur_luma.tobytes()
        frame_bytes = len(cur_bytes)

        req_buf = bytearray()
        req_buf.extend(b"\x01")  # op 1
        req_buf.extend(struct.pack("<iiiiI", W, H, s, max_d, n_tiles))
        for tx, ty in tiles:
            req_buf.extend(struct.pack("<ii", tx, ty))
        req_buf.extend(struct.pack("<Q", frame_bytes))
        req_buf.extend(prev_bytes)
        req_buf.extend(cur_bytes)

        self.proc.stdin.write(req_buf)
        self.proc.stdin.flush()

        header = self._read_exact(8)
        status, n_outputs = struct.unpack("<iI", header)
        if status != 0:
            raise RuntimeError(f"vqa_k_st_block4 failed with status {status}")

        raw_results = self._read_exact(16 * n_outputs)
        results = []
        for i in range(n_outputs):
            dx, dy, n_blocks, valid = struct.unpack_from("<iiii", raw_results, i * 16)
            if valid != 0:
                results.append((int(dx), int(dy), int(n_blocks)))
            else:
                results.append(None)
        return results

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"\x00")
                self.proc.stdin.flush()
                self.proc.wait(timeout=1.0)
            except Exception:
                self.proc.kill()


_WORKER = None


def get_worker():
    global _WORKER
    if _WORKER is None or _WORKER.proc.poll() is not None:
        _WORKER = SubprocessWorker(BIN_PATH)
    return _WORKER


def subtile_motion(prev_luma, cur_luma, s=None, tile_xy=(0, 0), max_d=16):
    """Subtile motion API implementation forwarding to vqa_k_st_block4 binary."""
    worker = get_worker()
    results = worker.compute_batch(prev_luma, cur_luma, s, [tile_xy], max_d)
    return results[0]
