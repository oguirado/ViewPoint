"""eval/v2x/gm_proto_rs.py - V2EVAL2 variant calling rust/vqa_k_gm_proto binary via pipe."""

import atexit
import os
import struct
import subprocess
from pathlib import Path
import numpy as np
from visioncore.layout import grid_params

ROOT = Path(__file__).resolve().parents[2]
BIN_ENV = os.environ.get("VQA_K_GM_PROTO_BIN")
DEFAULT_BIN = ROOT / "rust/vqa_k_gm_proto/target/release/vqa_k_gm_proto"

if BIN_ENV and Path(BIN_ENV).exists():
    BINARY_PATH = Path(BIN_ENV)
elif DEFAULT_BIN.exists():
    BINARY_PATH = DEFAULT_BIN
else:
    raise ImportError(
        f"vqa_k_gm_proto binary not found at {DEFAULT_BIN} or via VQA_K_GM_PROTO_BIN. "
        "Build it with: cargo build --release --offline in rust/vqa_k_gm_proto"
    )


class RustBinaryRunner:
    def __init__(self, binary_path):
        self.process = subprocess.Popen(
            [str(binary_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout
        atexit.register(self.close)

    def close(self):
        if self.stdin and not self.stdin.closed:
            try:
                self.stdin.write(struct.pack("<i", 0))
                self.stdin.flush()
                self.stdin.close()
            except Exception:
                pass
        if self.process:
            try:
                self.process.wait(timeout=2)
            except Exception:
                self.process.kill()
                self.process.wait(timeout=2)

    def _read_exact(self, n):
        data = bytearray()
        while len(data) < n:
            chunk = self.stdout.read(n - len(data))
            if not chunk:
                raise RuntimeError("Rust binary process terminated unexpectedly")
            data.extend(chunk)
        return bytes(data)

    def compute(self, prev, cur, max_d, k_top=16, s=16):
        prev_arr = np.ascontiguousarray(prev, dtype=np.uint8)
        cur_arr = np.ascontiguousarray(cur, dtype=np.uint8)

        H, W = cur_arr.shape
        prev_bytes = prev_arr.tobytes()
        cur_bytes = cur_arr.tobytes()

        # op 1: op (i32), h (u32), w (u32), max_d (i32), k_top (u32), s (u32)
        req_hdr = struct.pack("<iIIiII", 1, H, W, max_d, k_top, s)
        self.stdin.write(req_hdr)
        self.stdin.write(prev_bytes)
        self.stdin.write(cur_bytes)
        self.stdin.flush()

        # Read resp: status (i32), has_gm (u8), dx (i32), dy (i32), score (i64), gh (u32), gw (u32)
        resp_hdr = self._read_exact(4 + 1 + 4 + 4 + 8 + 4 + 4)
        status, has_gm, dx, dy, score, gh, gw = struct.unpack("<iBiiqII", resp_hdr)

        if status != 0:
            raise RuntimeError(f"Rust binary returned error status {status}")

        mask_bytes = self._read_exact(gh * gw)
        mask = np.frombuffer(mask_bytes, dtype=np.uint8).reshape((gh, gw)).astype(bool)

        gm_res = (int(dx), int(dy), int(score)) if has_gm == 1 else None
        return gm_res, mask

    def consensus_explicit(self, prev, cur, s, dx, dy):
        prev_arr = np.ascontiguousarray(prev, dtype=np.uint8)
        cur_arr = np.ascontiguousarray(cur, dtype=np.uint8)

        H, W = cur_arr.shape
        prev_bytes = prev_arr.tobytes()
        cur_bytes = cur_arr.tobytes()

        # op 2: op (i32), h (u32), w (u32), s (u32), dx (i32), dy (i32)
        req_hdr = struct.pack("<iIIIii", 2, H, W, s, dx, dy)
        self.stdin.write(req_hdr)
        self.stdin.write(prev_bytes)
        self.stdin.write(cur_bytes)
        self.stdin.flush()

        resp_hdr = self._read_exact(4 + 4 + 4)
        status, gh, gw = struct.unpack("<iII", resp_hdr)
        if status != 0:
            raise RuntimeError(f"Rust binary returned error status {status}")

        mask_bytes = self._read_exact(gh * gw)
        mask = np.frombuffer(mask_bytes, dtype=np.uint8).reshape((gh, gw)).astype(bool)
        return mask


_RUNNER = None


def get_runner():
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = RustBinaryRunner(BINARY_PATH)
    return _RUNNER


_CACHE = None


def estimate_global_motion(prev_luma, cur_luma, max_d, k_top=16):
    global _CACHE
    runner = get_runner()
    prev_arr = np.ascontiguousarray(prev_luma, dtype=np.uint8)
    cur_arr = np.ascontiguousarray(cur_luma, dtype=np.uint8)
    H, W = cur_arr.shape
    s, gw, gh = grid_params(W, H)

    gm_res, mask = runner.compute(prev_arr, cur_arr, max_d, k_top, s)

    _CACHE = {
        "prev_bytes": prev_arr.tobytes(),
        "cur_bytes": cur_arr.tobytes(),
        "s": s,
        "dx": gm_res[0] if gm_res else 0,
        "dy": gm_res[1] if gm_res else 0,
        "mask": mask,
    }

    return gm_res


def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
    global _CACHE
    prev_arr = np.ascontiguousarray(prev_luma, dtype=np.uint8)
    cur_arr = np.ascontiguousarray(cur_luma, dtype=np.uint8)
    prev_bytes = prev_arr.tobytes()
    cur_bytes = cur_arr.tobytes()

    if _CACHE is not None:
        if (
            _CACHE["prev_bytes"] == prev_bytes
            and _CACHE["cur_bytes"] == cur_bytes
            and _CACHE["s"] == s
            and _CACHE["dx"] == dx
            and _CACHE["dy"] == dy
        ):
            return _CACHE["mask"]

    runner = get_runner()
    return runner.consensus_explicit(prev_arr, cur_arr, s, dx, dy)
