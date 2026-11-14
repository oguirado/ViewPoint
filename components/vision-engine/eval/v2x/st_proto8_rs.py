"""V2EVAL2 post-pass variant adapter for subtile motion search via vqa_k_st_proto8 native binary."""

import atexit
import os
from pathlib import Path
import struct
import subprocess
import numpy as np


def _find_binary():
    env_bin = os.environ.get("VQA_K_ST_PROTO8_BIN")
    if env_bin:
        p = Path(env_bin)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        raise ImportError(f"VQA_K_ST_PROTO8_BIN set to '{env_bin}' but executable not found")

    repo_root = Path(__file__).resolve().parents[2]
    rel_bin = repo_root / "rust" / "vqa_k_st_proto8" / "target" / "release" / "vqa_k_st_proto8"
    if rel_bin.is_file() and os.access(rel_bin, os.X_OK):
        return str(rel_bin)

    raise ImportError(f"vqa_k_st_proto8 binary not found at '{rel_bin}'")


class NativeWorker:

    def __init__(self, bin_path):
        self.proc = subprocess.Popen(
            [bin_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )

    def subtile_motion(self, prev_luma, cur_luma, s, tile_xy, max_d):
        prev = np.asarray(prev_luma, dtype=np.uint8)
        cur = np.asarray(cur_luma, dtype=np.uint8)
        H, W = cur.shape
        s_val = 0 if s is None else int(s)
        tx, ty = tile_xy

        # Write Op 1 request (op=1, W, H, s, max_d, prev_bytes, cur_bytes, n_tiles=1, tx, ty)
        hdr = struct.pack("<BIIIi", 1, W, H, s_val, int(max_d))
        stdin = self.proc.stdin
        stdin.write(hdr)
        stdin.write(prev.tobytes())
        stdin.write(cur.tobytes())
        stdin.write(struct.pack("<Iii", 1, int(tx), int(ty)))
        stdin.flush()

        # Read response (status: i32)
        stdout = self.proc.stdout
        status_data = stdout.read(4)
        if len(status_data) < 4:
            raise RuntimeError("Unexpected EOF from vqa_k_st_proto8 process")
        status = struct.unpack("<i", status_data)[0]
        if status != 0:
            return None

        res_data = stdout.read(16)
        if len(res_data) < 16:
            raise RuntimeError("Unexpected EOF reading tile result")
        found, dx, dy, n_blocks = struct.unpack("<iiiI", res_data)
        if found == 1:
            return (int(dx), int(dy), int(n_blocks))
        return None

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"\x00")
                self.proc.stdin.flush()
                self.proc.wait(timeout=1)
            except Exception:
                self.proc.kill()


_worker_instance = None


def get_worker():
    global _worker_instance
    if _worker_instance is None or _worker_instance.proc.poll() is not None:
        bin_path = _find_binary()
        _worker_instance = NativeWorker(bin_path)
        atexit.register(_cleanup)
    return _worker_instance


def _cleanup():
    global _worker_instance
    if _worker_instance is not None:
        _worker_instance.close()
        _worker_instance = None


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    worker = get_worker()
    return worker.subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d)
