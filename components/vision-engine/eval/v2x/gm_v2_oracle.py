"""eval/v2x/gm_v2_oracle.py - Test oracle and conformance harness for rust/vqa_k_gm_v2."""

import hashlib
import importlib
import io
import json
import shlex
import struct
import subprocess
import numpy as np


class SplitMix64:
    """Deterministic SplitMix64 PRNG matching Rust implementation."""

    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFFFFFFFFFF

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        return (z ^ (z >> 31)) & 0xFFFFFFFFFFFFFFFF

    def fill_bytes(self, n: int) -> bytes:
        buf = bytearray(n)
        i = 0
        while i < n:
            val = self.next_u64()
            val_bytes = val.to_bytes(8, byteorder="little")
            take = min(n - i, 8)
            buf[i : i + take] = val_bytes[:take]
            i += take
        return bytes(buf)


def conformance_cases(seed: int = 0x5EED) -> list:
    """Return deterministic RAM conformance cases.

    Returns 7 card cases plus 64 random SplitMix64 cases with sizes from 16x16
    to 272x480 and random integer shifts.
    Each case is a dict with keys: case_id, h, w, s, max_d, prev, cur.
    """
    cases = []

    # 1. Flat window drag
    w, h, s, max_d = 640, 480, 16, 200
    rng1 = np.random.default_rng(seed + 1)
    prev1 = rng1.integers(0, 256, (h, w), dtype=np.uint8)
    cur1 = rng1.integers(0, 256, (h, w), dtype=np.uint8)
    dx1, dy1 = 16, 24
    cur1[100 + dy1 : 200 + dy1, 100 + dx1 : 250 + dx1] = prev1[100:200, 100:250]
    cases.append({
        "case_id": "flat_window_drag",
        "h": h,
        "w": w,
        "s": s,
        "max_d": max_d,
        "prev": prev1,
        "cur": cur1,
    })

    # 2. Two motions
    w, h, s, max_d = 640, 480, 16, 200
    rng2 = np.random.default_rng(seed + 2)
    prev2 = rng2.integers(0, 256, (h, w), dtype=np.uint8)
    cur2 = rng2.integers(0, 256, (h, w), dtype=np.uint8)
    cur2[100 + 16 : 180 + 16, 100 + 16 : 180 + 16] = prev2[100:180, 100:180]
    cur2[250 - 16 : 330 - 16, 300 - 16 : 380 - 16] = prev2[250:330, 300:380]
    cases.append({
        "case_id": "two_motions",
        "h": h,
        "w": w,
        "s": s,
        "max_d": max_d,
        "prev": prev2,
        "cur": cur2,
    })

    # 3. Large dy at 1088 by 1920
    w, h, s, max_d = 1920, 1088, 16, 600
    dy3 = 300
    rng3 = np.random.default_rng(seed + 3)
    prev3 = rng3.integers(0, 256, (h, w), dtype=np.uint8)
    cur3 = np.empty_like(prev3)
    cur3[dy3:, :] = prev3[:-dy3, :]
    cur3[:dy3, :] = rng3.integers(0, 256, (dy3, w), dtype=np.uint8)
    cases.append({
        "case_id": "large_dy_1088_1920",
        "h": h,
        "w": w,
        "s": s,
        "max_d": max_d,
        "prev": prev3,
        "cur": cur3,
    })

    # 4. Static chrome with clock
    w, h, s, max_d = 640, 480, 16, 100
    rng4 = np.random.default_rng(seed + 4)
    prev4 = rng4.integers(0, 256, (h, w), dtype=np.uint8)
    cur4 = prev4.copy()
    cur4[100:120, 200:250] = rng4.integers(0, 256, (20, 50), dtype=np.uint8)
    cases.append({
        "case_id": "static_chrome_clock",
        "h": h,
        "w": w,
        "s": s,
        "max_d": max_d,
        "prev": prev4,
        "cur": cur4,
    })

    # 5. Periodic text
    w, h, s, max_d = 640, 480, 16, 100
    x_coords = np.arange(w)
    stripes = np.where((x_coords // 4) % 2 == 0, 200, 50).astype(np.uint8)
    prev5 = np.tile(stripes, (h, 1))
    cur5 = np.roll(prev5, shift=8, axis=1)
    cases.append({
        "case_id": "periodic_text",
        "h": h,
        "w": w,
        "s": s,
        "max_d": max_d,
        "prev": prev5,
        "cur": cur5,
    })

    # 6. Bare caret
    w, h, s, max_d = 640, 480, 16, 50
    rng6 = np.random.default_rng(seed + 6)
    prev6 = rng6.integers(200, 240, (h, w), dtype=np.uint8)
    cur6 = prev6.copy()
    prev6[100:116, 100:102] = 0
    cur6[100:116, 102:104] = 0
    cases.append({
        "case_id": "bare_caret",
        "h": h,
        "w": w,
        "s": s,
        "max_d": max_d,
        "prev": prev6,
        "cur": cur6,
    })

    # 7. Identical frames
    w, h, s, max_d = 640, 480, 16, 100
    rng7 = np.random.default_rng(seed + 7)
    prev7 = rng7.integers(0, 256, (h, w), dtype=np.uint8)
    cur7 = prev7.copy()
    cases.append({
        "case_id": "identical_frames",
        "h": h,
        "w": w,
        "s": s,
        "max_d": max_d,
        "prev": prev7,
        "cur": cur7,
    })

    # 64 random SplitMix64 cases with sizes from 16x16 to 272x480
    for idx in range(64):
        sm = SplitMix64((seed ^ 0x5A5A5A5A) + idx * 101)
        w_rand = 16 + int(sm.next_u64() % (480 - 16 + 1))
        h_rand = 16 + int(sm.next_u64() % (272 - 16 + 1))
        s_rand = 16
        max_d_rand = 8 + int(sm.next_u64() % 25)
        dx_rand = int(sm.next_u64() % (2 * max_d_rand + 1)) - max_d_rand
        dy_rand = int(sm.next_u64() % (2 * max_d_rand + 1)) - max_d_rand

        prev_bytes = sm.fill_bytes(w_rand * h_rand)
        prev_r = np.frombuffer(prev_bytes, dtype=np.uint8).reshape((h_rand, w_rand)).copy()

        cur_bytes = sm.fill_bytes(w_rand * h_rand)
        cur_r = np.frombuffer(cur_bytes, dtype=np.uint8).reshape((h_rand, w_rand)).copy()

        cy0, cy1 = max(0, dy_rand), min(h_rand, h_rand + dy_rand)
        cx0, cx1 = max(0, dx_rand), min(w_rand, w_rand + dx_rand)
        if cy0 < cy1 and cx0 < cx1:
            py0, py1 = cy0 - dy_rand, cy1 - dy_rand
            px0, px1 = cx0 - dx_rand, cx1 - dx_rand
            cur_r[cy0:cy1, cx0:cx1] = prev_r[py0:py1, px0:px1]

        cases.append({
            "case_id": f"rand_{idx}",
            "h": h_rand,
            "w": w_rand,
            "s": s_rand,
            "max_d": max_d_rand,
            "prev": prev_r,
            "cur": cur_r,
        })

    return cases


def expected(module_name, case: dict) -> tuple:
    """Return canonical reference tuple (has_gm, dx, dy, score, gh, gw) and 2D mask."""
    if isinstance(module_name, str):
        mod = importlib.import_module(module_name)
    else:
        mod = module_name

    prev = np.ascontiguousarray(case["prev"], dtype=np.uint8)
    cur = np.ascontiguousarray(case["cur"], dtype=np.uint8)
    max_d = int(case["max_d"])
    s = int(case.get("s", 16))

    res = mod.estimate_global_motion(prev, cur, max_d)
    if res is not None:
        has_gm = 1
        dx = int(res[0])
        dy = int(res[1])
        score = int(res[2])
    else:
        has_gm = 0
        dx = 0
        dy = 0
        score = 0

    mask_arr = mod.consensus_tiles(prev, cur, s, dx, dy)
    gh = int(mask_arr.shape[0])
    gw = int(mask_arr.shape[1])

    ref_tuple = (has_gm, dx, dy, score, gh, gw)
    mask_uint8 = np.ascontiguousarray(mask_arr, dtype=np.uint8)
    return ref_tuple, mask_uint8


def encode_request(case: dict, op: int = 1) -> bytes:
    """Encode request framing byte layout per rust/vqa_k_gm_proto/README.md."""
    h = int(case["h"])
    w = int(case["w"])
    s = int(case.get("s", 16))
    max_d = int(case.get("max_d", 0))
    prev_bytes = np.ascontiguousarray(case["prev"], dtype=np.uint8).tobytes()
    cur_bytes = np.ascontiguousarray(case["cur"], dtype=np.uint8).tobytes()

    if op == 1:
        k_top = 8
        hdr = struct.pack("<iIIiII", 1, h, w, max_d, k_top, s)
        return hdr + prev_bytes + cur_bytes
    elif op == 2:
        dx = int(case.get("dx", 0))
        dy = int(case.get("dy", 0))
        hdr = struct.pack("<iIIIii", 2, h, w, s, dx, dy)
        return hdr + prev_bytes + cur_bytes
    elif op == 0:
        return struct.pack("<i", 0)
    else:
        raise ValueError(f"Unsupported op: {op}")


def decode_response(data_or_stream, op: int = 1) -> dict:
    """Decode response byte layout per rust/vqa_k_gm_proto/README.md."""
    if isinstance(data_or_stream, (bytes, bytearray)):
        stream = io.BytesIO(data_or_stream)
    else:
        stream = data_or_stream

    def _read_exact(n):
        buf = bytearray()
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                raise RuntimeError("Premature end of stream while reading response")
            buf.extend(chunk)
        return bytes(buf)

    if op == 1:
        hdr = _read_exact(4 + 1 + 4 + 4 + 8 + 4 + 4)
        status, has_gm, dx, dy, score, gh, gw = struct.unpack("<iBiiqII", hdr)
        mask_bytes = _read_exact(gh * gw)
        mask = np.frombuffer(mask_bytes, dtype=np.uint8).reshape((gh, gw))
        return {
            "status": int(status),
            "has_gm": int(has_gm),
            "dx": int(dx),
            "dy": int(dy),
            "score": int(score),
            "gh": int(gh),
            "gw": int(gw),
            "mask": mask,
            "mask_bytes": mask_bytes,
        }
    elif op == 2:
        hdr = _read_exact(4 + 4 + 4)
        status, gh, gw = struct.unpack("<iII", hdr)
        mask_bytes = _read_exact(gh * gw)
        mask = np.frombuffer(mask_bytes, dtype=np.uint8).reshape((gh, gw))
        return {
            "status": int(status),
            "gh": int(gh),
            "gw": int(gw),
            "mask": mask,
            "mask_bytes": mask_bytes,
        }
    else:
        raise ValueError(f"Unsupported op: {op}")


def check_binary(path, module_name) -> dict:
    """Spawn binary, stream all conformance cases over pipe, and compare bit-exactly."""
    if isinstance(path, (list, tuple)):
        cmd = [str(x) for x in path]
    else:
        cmd = shlex.split(str(path))

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    cases = conformance_cases()
    results = []
    all_pass = True

    try:
        for case in cases:
            ref_tuple, ref_mask = expected(module_name, case)
            exp_has_gm, exp_dx, exp_dy, exp_score, exp_gh, exp_gw = ref_tuple

            req = encode_request(case, op=1)
            proc.stdin.write(req)
            proc.stdin.flush()

            resp = decode_response(proc.stdout, op=1)

            if resp["status"] != 0:
                mismatch = ("status", 0, int(resp["status"]))
            elif resp["has_gm"] != exp_has_gm:
                mismatch = ("has_gm", int(exp_has_gm), int(resp["has_gm"]))
            elif resp["dx"] != exp_dx:
                mismatch = ("dx", int(exp_dx), int(resp["dx"]))
            elif resp["dy"] != exp_dy:
                mismatch = ("dy", int(exp_dy), int(resp["dy"]))
            elif resp["score"] != exp_score:
                mismatch = ("score", int(exp_score), int(resp["score"]))
            elif resp["gh"] != exp_gh:
                mismatch = ("gh", int(exp_gh), int(resp["gh"]))
            elif resp["gw"] != exp_gw:
                mismatch = ("gw", int(exp_gw), int(resp["gw"]))
            elif not np.array_equal(resp["mask"], ref_mask):
                mismatch = ("mask", "mask_match", "mask_mismatch")
            else:
                mismatch = None

            if mismatch is None:
                results.append({"case_id": case["case_id"], "pass": True})
            else:
                all_pass = False
                results.append({
                    "case_id": case["case_id"],
                    "pass": False,
                    "first_mismatch": mismatch[0],
                    "expected": mismatch[1],
                    "actual": mismatch[2],
                })

        # Send quit op 0
        proc.stdin.write(struct.pack("<i", 0))
        proc.stdin.flush()
    finally:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        if proc.stdout and not proc.stdout.closed:
            proc.stdout.close()
        proc.wait(timeout=5)

    return {
        "schema": "vqa.gm_v2_oracle.check/v1",
        "all_pass": all_pass,
        "total_cases": len(cases),
        "passed_cases": sum(1 for r in results if r["pass"]),
        "cases": results,
    }


def digest(module_name) -> str:
    """Return sha256 hex string of canonical JSON of all expected outputs."""
    cases = conformance_cases()
    records = []
    for case in cases:
        ref_tuple, ref_mask = expected(module_name, case)
        has_gm, dx, dy, score, gh, gw = ref_tuple
        records.append({
            "case_id": str(case["case_id"]),
            "has_gm": int(has_gm),
            "dx": int(dx),
            "dy": int(dy),
            "score": int(score),
            "gh": int(gh),
            "gw": int(gw),
            "mask": np.ascontiguousarray(ref_mask, dtype=np.uint8).tolist(),
        })
    canonical_json = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
