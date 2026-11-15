#!/usr/bin/env python3
import json
import os
import sys

MASK64 = (1 << 64) - 1

class SplitMix64Ref:
    def __init__(self, seed: int):
        self.state = seed & MASK64

    def u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return (z ^ (z >> 31)) & MASK64

    def fill_bytes(self, length: int) -> bytes:
        out = bytearray()
        while len(out) < length:
            val = self.u64()
            out.extend(val.to_bytes(8, byteorder='little'))
        return bytes(out[:length])


def fnv1a_64_ref(data: bytes) -> str:
    h = 0xcbf29ce484222325
    prime = 0x100000001b3
    for b in data:
        h = ((h ^ b) * prime) & MASK64
    return f"{h:016x}"


def repack_k1_ref(W: int, H: int, src: bytes, src_stride: int, dst: bytes, dst_stride: int):
    if not (0 <= W <= 16384 and 0 <= H <= 16384):
        return "E_DIM", None
    if src_stride < 4 * W or dst_stride < 3 * W:
        return "E_STRIDE", None

    if H == 0:
        min_src = 0
        min_dst = 0
    else:
        min_src = (H - 1) * src_stride + 4 * W
        min_dst = (H - 1) * dst_stride + 3 * W

    if len(src) < min_src or len(dst) < min_dst:
        return "E_SHORT", None

    out_dst = bytearray(dst)
    for y in range(H):
        src_row_off = y * src_stride
        dst_row_off = y * dst_stride
        for x in range(W):
            sp = src_row_off + 4 * x
            dp = dst_row_off + 3 * x
            b = src[sp]
            g = src[sp + 1]
            r = src[sp + 2]
            out_dst[dp] = r
            out_dst[dp + 1] = g
            out_dst[dp + 2] = b

    return None, bytes(out_dst)


def damage_k2_ref(W: int, H: int, s: int, rects: list):
    if not (1 <= W <= 16384 and 1 <= H <= 16384):
        return "E_DIM", None, None
    if s not in (1, 2, 4, 8, 16, 32, 64, 128, 256):
        return "E_GRID", None, None

    for rect in rects:
        x, y, w, h = rect
        if w < 0 or h < 0:
            return "E_RECT", None, None

    gw = (W + s - 1) // s
    gh = (H + s - 1) // s
    total_tiles = gw * gh
    num_words = (total_tiles + 63) // 64
    words = [0] * num_words

    for x, y, w, h in rects:
        x0 = max(x, 0)
        y0 = max(y, 0)
        x1 = min(x + w, W)
        y1 = min(y + h, H)

        if x1 <= x0 or y1 <= y0:
            continue

        tx_start = x0 // s
        tx_end = (x1 + s - 1) // s
        ty_start = y0 // s
        ty_end = (y1 + s - 1) // s

        for ty in range(ty_start, ty_end):
            row_offset = ty * gw
            for tx in range(tx_start, tx_end):
                t = row_offset + tx
                word_idx = t // 64
                bit_idx = t % 64
                words[word_idx] |= (1 << bit_idx)

    popcount = sum(bin(w).count('1') for w in words)
    words_hex = [f"{w:016x}" for w in words]
    return None, words_hex, popcount


def check_all(fixtures_dir: str = None) -> tuple:
    if fixtures_dir is None:
        fixtures_dir = os.path.dirname(os.path.abspath(__file__))

    mismatches = []

    # Import gen_fixtures in memory to regenerate byte-for-byte
    try:
        import gen_fixtures
    except ImportError:
        sys.path.insert(0, fixtures_dir)
        import gen_fixtures

    # Check existence of files
    required_files = ["repack_cases.json", "repack_src.bin", "repack_dst.bin", "damage_cases.json"]
    for fname in required_files:
        fpath = os.path.join(fixtures_dir, fname)
        if not os.path.exists(fpath):
            mismatches.append(f"Missing required file: {fname}")

    if mismatches:
        return False, mismatches

    # Regenerate in memory by calling gen_fixtures helper logic
    # First, read actual file contents from disk
    with open(os.path.join(fixtures_dir, "repack_cases.json"), "rb") as f:
        repack_json_bytes_disk = f.read()
    with open(os.path.join(fixtures_dir, "repack_src.bin"), "rb") as f:
        repack_src_bytes_disk = f.read()
    with open(os.path.join(fixtures_dir, "repack_dst.bin"), "rb") as f:
        repack_dst_bytes_disk = f.read()
    with open(os.path.join(fixtures_dir, "damage_cases.json"), "rb") as f:
        damage_json_bytes_disk = f.read()

    # Regenerate into memory
    # To avoid writing to disk, we can capture or compute what gen_fixtures generates
    # We can invoke gen_fixtures.generate_all() or execute the logic.
    # Let's temporarily redirect file writing or run the generation logic.
    # Alternatively, gen_fixtures logic produces json strings and byte arrays.
    # Let's check JSON schema and structure first.

    try:
        repack_data = json.loads(repack_json_bytes_disk.decode('utf-8'))
        if repack_data.get("schema") != "langx.x1.fixtures/v1":
            mismatches.append(f"repack_cases.json schema mismatch: got {repack_data.get('schema')}")
    except Exception as e:
        mismatches.append(f"repack_cases.json invalid JSON: {e}")

    try:
        damage_data = json.loads(damage_json_bytes_disk.decode('utf-8'))
        if damage_data.get("schema") != "langx.x1.fixtures/v1":
            mismatches.append(f"damage_cases.json schema mismatch: got {damage_data.get('schema')}")
    except Exception as e:
        mismatches.append(f"damage_cases.json invalid JSON: {e}")

    # Check trailing newline in JSON files
    if not repack_json_bytes_disk.endswith(b"\n"):
        mismatches.append("repack_cases.json missing trailing newline")
    if not damage_json_bytes_disk.endswith(b"\n"):
        mismatches.append("damage_cases.json missing trailing newline")

    if mismatches:
        return False, mismatches

    # Validate Repack Cases with independent reference implementation
    repack_cases = repack_data.get("cases", [])
    if len(repack_cases) < 24:
        mismatches.append(f"repack_cases count too low: {len(repack_cases)} < 24")

    for c in repack_cases:
        ctype = c.get("type")
        W = c["width"]
        H = c["height"]
        src_stride = c["src_stride"]
        dst_stride = c["dst_stride"]
        src_len = c["src_buf_len"]
        dst_len = c["dst_buf_len"]

        if ctype == "small":
            src_off = c["src_offset"]
            dst_off = c["dst_offset"]

            if src_off + src_len > len(repack_src_bytes_disk):
                mismatches.append(f"Case {c['name']}: src offset/len out of bounds in repack_src.bin")
                continue
            if dst_off + dst_len > len(repack_dst_bytes_disk):
                mismatches.append(f"Case {c['name']}: dst offset/len out of bounds in repack_dst.bin")
                continue

            src_buf = repack_src_bytes_disk[src_off : src_off + src_len]
            expected_dst_buf = repack_dst_bytes_disk[dst_off : dst_off + dst_len]

            # Recompute
            dst_init = bytearray(b'\xEE' * dst_len)
            err, recomputed_dst = repack_k1_ref(W, H, src_buf, src_stride, dst_init, dst_stride)
            if err is not None:
                mismatches.append(f"Case {c['name']}: expected success but ref returned {err}")
            elif recomputed_dst != expected_dst_buf:
                mismatches.append(f"Case {c['name']}: dst buffer byte mismatch with ref implementation")

        elif ctype == "error":
            expected_err = c["error"]
            # Create dummy buffers of src_len and dst_len
            src_buf = b'\x00' * src_len
            dst_init = b'\xEE' * dst_len
            err, _ = repack_k1_ref(W, H, src_buf, src_stride, dst_init, dst_stride)
            if err != expected_err:
                mismatches.append(f"Case {c['name']}: expected error {expected_err}, got {err}")

        elif ctype == "large":
            seed = c["seed"]
            expected_fnv = c["expected_fnv64"]

            rng = SplitMix64Ref(seed)
            src_buf = rng.fill_bytes(src_len)
            dst_init = bytearray(b'\xEE' * dst_len)

            err, recomputed_dst = repack_k1_ref(W, H, src_buf, src_stride, dst_init, dst_stride)
            if err is not None:
                mismatches.append(f"Case {c['name']}: expected success but ref returned {err}")
            else:
                recomputed_fnv = fnv1a_64_ref(recomputed_dst)
                if recomputed_fnv != expected_fnv:
                    mismatches.append(f"Case {c['name']}: FNV-1a hash mismatch: expected {expected_fnv}, got {recomputed_fnv}")

    # Validate Damage Cases with independent reference implementation
    damage_cases = damage_data.get("cases", [])
    if len(damage_cases) < 40:
        mismatches.append(f"damage_cases count too low: {len(damage_cases)} < 40")

    for c in damage_cases:
        W = c["width"]
        H = c["height"]
        s = c["s"]
        rects = c["rects"]

        err, recomputed_words, recomputed_pop = damage_k2_ref(W, H, s, rects)
        if "error" in c:
            if err != c["error"]:
                mismatches.append(f"Damage Case {c['name']}: expected error {c['error']}, got {err}")
        else:
            if err is not None:
                mismatches.append(f"Damage Case {c['name']}: expected success but ref returned error {err}")
            else:
                if recomputed_words != c["expected_words"]:
                    mismatches.append(f"Damage Case {c['name']}: expected_words mismatch")
                if recomputed_pop != c["expected_popcount"]:
                    mismatches.append(f"Damage Case {c['name']}: expected_popcount mismatch: expected {c['expected_popcount']}, got {recomputed_pop}")

    # Byte-for-byte check against re-generated files in memory
    # Perform in-memory generation using gen_fixtures logic and compare exact bytes
    orig_open = open
    in_memory_files = {}

    class MemoryWriter:
        def __init__(self, path, mode):
            self.path = os.path.basename(path)
            self.mode = mode
            self.buf = bytearray()

        def write(self, data):
            if isinstance(data, str):
                self.buf.extend(data.encode('utf-8'))
            else:
                self.buf.extend(data)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            in_memory_files[self.path] = bytes(self.buf)

    # Monkeypatch open inside gen_fixtures to generate in memory
    def mock_open(path, mode="r", *args, **kwargs):
        if "w" in mode:
            return MemoryWriter(path, mode)
        return orig_open(path, mode, *args, **kwargs)

    gen_fixtures.open = mock_open
    try:
        gen_fixtures.generate_all()
    finally:
        gen_fixtures.open = orig_open

    # Check byte for byte against disk
    disk_files = {
        "repack_cases.json": repack_json_bytes_disk,
        "repack_src.bin": repack_src_bytes_disk,
        "repack_dst.bin": repack_dst_bytes_disk,
        "damage_cases.json": damage_json_bytes_disk,
    }

    for fname, disk_bytes in disk_files.items():
        gen_bytes = in_memory_files.get(fname)
        if gen_bytes != disk_bytes:
            mismatches.append(f"File {fname} is not byte-identical to regenerated fixture")

    if mismatches:
        return False, mismatches
    return True, []


def main():
    fixtures_dir = os.path.dirname(os.path.abspath(__file__))
    ok, mismatches = check_all(fixtures_dir)
    if not ok:
        print("Verification FAILED with mismatches:", file=sys.stderr)
        for m in mismatches:
            print(f" - {m}", file=sys.stderr)
        sys.exit(1)
    else:
        print("Verification SUCCESS: All fixtures verified byte-for-byte.")
        sys.exit(0)


if __name__ == "__main__":
    main()
