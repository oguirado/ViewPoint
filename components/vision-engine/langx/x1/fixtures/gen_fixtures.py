#!/usr/bin/env python3
import json
import os

MASK64 = (1 << 64) - 1

class SplitMix64:
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


def fnv1a_64(data: bytes) -> str:
    h = 0xcbf29ce484222325
    prime = 0x100000001b3
    for b in data:
        h = ((h ^ b) * prime) & MASK64
    return f"{h:016x}"


def repack_k1_ref(W: int, H: int, src: bytes, src_stride: int, dst: bytes, dst_stride: int):
    # Error checking
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
            # output receives R, G, B
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

    # Popcount
    popcount = sum(bin(w).count('1') for w in words)
    words_hex = [f"{w:016x}" for w in words]
    return None, words_hex, popcount


def generate_all():
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # -------------------------------------------------------------
    # 1. Repack Cases
    # -------------------------------------------------------------
    repack_src_bytes = bytearray()
    repack_dst_bytes = bytearray()

    repack_cases = []

    # Small cases configuration
    # W in 1, 2, 3, 5, 7, 8, 15, 16, 17, 33 and 0; H in 1, 2, 3, 7 and 0
    # Variations: tight strides, padded strides, last row with/without padding
    small_specs = [
        # (name, W, H, src_stride, dst_stride, src_pad_last, dst_pad_last, seed)
        ("1x1_tight", 1, 1, 4, 3, False, False, 101),
        ("1x1_padded", 1, 1, 8, 6, True, True, 102),
        ("2x1_tight", 2, 1, 8, 6, False, False, 103),
        ("2x2_padded_no_last_pad", 2, 2, 12, 9, False, False, 104),
        ("3x3_tight", 3, 3, 12, 9, False, False, 105),
        ("3x2_padded", 3, 2, 16, 12, True, True, 106),
        ("5x1_tight", 5, 1, 20, 15, False, False, 107),
        ("5x3_padded_no_last_pad", 5, 3, 24, 18, False, False, 108),
        ("7x2_tight", 7, 2, 28, 21, False, False, 109),
        ("7x7_padded", 7, 7, 32, 25, True, True, 110),
        ("8x1_tight", 8, 1, 32, 24, False, False, 111),
        ("8x3_padded", 8, 3, 40, 30, True, True, 112),
        ("15x2_tight", 15, 2, 60, 45, False, False, 113),
        ("15x3_padded_no_last_pad", 15, 3, 64, 48, False, False, 114),
        ("16x1_tight", 16, 1, 64, 48, False, False, 115),
        ("16x2_padded", 16, 2, 72, 54, True, True, 116),
        ("17x1_tight", 17, 1, 68, 51, False, False, 117),
        ("17x3_padded", 17, 3, 80, 60, True, True, 118),
        ("33x1_tight", 33, 1, 132, 99, False, False, 119),
        ("33x2_padded_no_last_pad", 33, 2, 140, 105, False, False, 120),
        ("0x0_zero_dim", 0, 0, 0, 0, False, False, 121),
        ("5x0_zero_height", 5, 0, 20, 15, False, False, 122),
        ("0x5_zero_width", 0, 5, 0, 0, False, False, 123),
        ("1x7_tight", 1, 7, 4, 3, False, False, 124),
        ("2x7_padded", 2, 7, 12, 10, True, True, 125),
        ("8x7_padded_no_last_pad", 8, 7, 36, 28, False, False, 126),
    ]

    for name, W, H, src_stride, dst_stride, src_pad_last, dst_pad_last, seed in small_specs:
        rng = SplitMix64(seed)
        if H == 0:
            src_len = 0
            dst_len = 0
        else:
            src_len = (H - 1) * src_stride + 4 * W + (src_stride - 4 * W if src_pad_last else 0)
            dst_len = (H - 1) * dst_stride + 3 * W + (dst_stride - 3 * W if dst_pad_last else 0)

        src_data = rng.fill_bytes(src_len)
        dst_init = bytearray(b'\xEE' * dst_len)

        err, expected_dst = repack_k1_ref(W, H, src_data, src_stride, dst_init, dst_stride)
        assert err is None

        src_off = len(repack_src_bytes)
        repack_src_bytes.extend(src_data)

        dst_off = len(repack_dst_bytes)
        repack_dst_bytes.extend(expected_dst)

        repack_cases.append({
            "dst_buf_len": dst_len,
            "dst_offset": dst_off,
            "dst_stride": dst_stride,
            "height": H,
            "name": name,
            "src_buf_len": src_len,
            "src_offset": src_off,
            "src_stride": src_stride,
            "type": "small",
            "width": W
        })

    # Error cases
    error_specs = [
        ("err_dim_w_negative", -1, 10, 4, 3, "E_DIM"),
        ("err_dim_w_too_large", 16385, 10, 65540, 49155, "E_DIM"),
        ("err_dim_h_negative", 10, -1, 40, 30, "E_DIM"),
        ("err_dim_h_too_large", 10, 16385, 40, 30, "E_DIM"),
        ("err_stride_src_too_small", 10, 10, 39, 30, "E_STRIDE"),
        ("err_stride_dst_too_small", 10, 10, 40, 29, "E_STRIDE"),
        ("err_short_src", 10, 2, 40, 30, "E_SHORT", 79, 60), # min src is 40*1 + 40 = 80
        ("err_short_dst", 10, 2, 40, 30, "E_SHORT", 80, 59), # min dst is 30*1 + 30 = 60
    ]

    for item in error_specs:
        if len(item) == 6:
            name, W, H, src_stride, dst_stride, expected_err = item
            src_len = (H - 1) * src_stride + 4 * W if H > 0 else 0
            dst_len = (H - 1) * dst_stride + 3 * W if H > 0 else 0
        else:
            name, W, H, src_stride, dst_stride, expected_err, src_len, dst_len = item

        repack_cases.append({
            "dst_buf_len": dst_len,
            "dst_stride": dst_stride,
            "error": expected_err,
            "height": H,
            "name": name,
            "src_buf_len": src_len,
            "src_stride": src_stride,
            "type": "error",
            "width": W
        })

    # 4 Large synthetic cases given only by seed, W, H, strides with FNV-1a 64
    large_specs = [
        ("large_1920x1080_tight", 1920, 1080, 1920 * 4, 1920 * 3, 2001),
        ("large_1920x1080_padded", 1920, 1080, 7744, 1920 * 3, 2002),
        ("large_3840x2160_tight", 3840, 2160, 3840 * 4, 3840 * 3, 2003),
        ("large_1366x768_tight", 1366, 768, 1366 * 4, 1366 * 3, 2004),
    ]

    for name, W, H, src_stride, dst_stride, seed in large_specs:
        rng = SplitMix64(seed)
        src_len = (H - 1) * src_stride + 4 * W
        dst_len = (H - 1) * dst_stride + 3 * W

        src_data = rng.fill_bytes(src_len)
        dst_init = bytearray(b'\xEE' * dst_len)

        err, expected_dst = repack_k1_ref(W, H, src_data, src_stride, dst_init, dst_stride)
        assert err is None

        fnv_hash = fnv1a_64(expected_dst)

        repack_cases.append({
            "dst_buf_len": dst_len,
            "dst_stride": dst_stride,
            "expected_fnv64": fnv_hash,
            "height": H,
            "name": name,
            "seed": seed,
            "src_buf_len": src_len,
            "src_stride": src_stride,
            "type": "large",
            "width": W
        })

    repack_json_data = {
        "cases": repack_cases,
        "schema": "langx.x1.fixtures/v1"
    }

    # Write repack_cases.json, repack_src.bin, repack_dst.bin
    with open(os.path.join(base_dir, "repack_cases.json"), "w") as f:
        json.dump(repack_json_data, f, indent=2, sort_keys=True)
        f.write("\n")

    with open(os.path.join(base_dir, "repack_src.bin"), "wb") as f:
        f.write(repack_src_bytes)

    with open(os.path.join(base_dir, "repack_dst.bin"), "wb") as f:
        f.write(repack_dst_bytes)

    # -------------------------------------------------------------
    # 2. Damage Cases
    # -------------------------------------------------------------
    # At least 40 cases covering various dimensions, grid sizes, rectangles
    damage_cases = []

    # Worked check case
    err, words, popcount = damage_k2_ref(1920, 1080, 32, [[96, 0, 192, 32]])
    damage_cases.append({
        "expected_popcount": popcount,
        "expected_words": words,
        "height": 1080,
        "name": "worked_check_1920x1080_s32",
        "rects": [[96, 0, 192, 32]],
        "s": 32,
        "width": 1920
    })

    # Additional damage cases
    # Border / alignment checks at x = 31, 32, 33 with w=1 at s=32
    damage_cases.append({
        "name": "align_x31_w1",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[31, 0, 1, 1]]
    })
    damage_cases.append({
        "name": "align_x32_w1",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[32, 0, 1, 1]]
    })
    damage_cases.append({
        "name": "align_x33_w1",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[33, 0, 1, 1]]
    })

    # Zero-width, zero-height
    damage_cases.append({
        "name": "zero_width",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[10, 10, 0, 50]]
    })
    damage_cases.append({
        "name": "zero_height",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[10, 10, 50, 0]]
    })
    damage_cases.append({
        "name": "x33_w0_skipped",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[33, 0, 0, 10]]
    })

    # Negative x and y
    damage_cases.append({
        "name": "negative_x_y_partial_onscreen",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[-10, -10, 50, 50]]
    })
    damage_cases.append({
        "name": "negative_x_y_fully_offscreen",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[-100, -100, 50, 50]]
    })

    # Fully off-screen right/bottom
    damage_cases.append({
        "name": "fully_offscreen_right_bottom",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[2000, 2000, 100, 100]]
    })

    # Rectangles crossing partial bottom row and right column
    # 1920 x 1080 with s=32: 1920 / 32 = 60 (exact), 1080 / 32 = 33.75 -> 34 rows (partial bottom row 33 has height 24)
    # 1366 x 768 with s=32: 1366 / 32 = 42.6875 -> 43 cols, 768 / 32 = 24 rows
    damage_cases.append({
        "name": "cross_partial_bottom_row_1080",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[0, 1050, 100, 50]]
    })
    damage_cases.append({
        "name": "cross_partial_right_col_1366",
        "width": 1366, "height": 768, "s": 32,
        "rects": [[1350, 0, 50, 100]]
    })
    damage_cases.append({
        "name": "cross_partial_corner_1366x768",
        "width": 1366, "height": 768, "s": 32,
        "rects": [[1350, 750, 50, 50]]
    })

    # w/h near int32 limit (2147483647)
    damage_cases.append({
        "name": "near_int32_max_width_height",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[0, 0, 2147483647, 2147483647]]
    })
    damage_cases.append({
        "name": "near_int32_max_with_offset",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[100, 100, 2147483600, 2147483600]]
    })

    # Overlapping rectangles
    damage_cases.append({
        "name": "overlapping_rectangles",
        "width": 1920, "height": 1080, "s": 32,
        "rects": [[0, 0, 64, 64], [32, 32, 64, 64], [16, 16, 32, 32]]
    })

    # Empty list
    damage_cases.append({
        "name": "empty_rect_list",
        "width": 1920, "height": 1080, "s": 32,
        "rects": []
    })

    # Grid sizes: s = 1, 16, 256
    damage_cases.append({
        "name": "grid_s1_small_screen",
        "width": 10, "height": 10, "s": 1,
        "rects": [[2, 2, 3, 3]]
    })
    damage_cases.append({
        "name": "grid_s16_1920x1080",
        "width": 1920, "height": 1080, "s": 16,
        "rects": [[10, 10, 100, 100]]
    })
    damage_cases.append({
        "name": "grid_s256_3840x2160",
        "width": 3840, "height": 2160, "s": 256,
        "rects": [[100, 100, 1000, 1000]]
    })

    # 3840x2160 at s 32
    damage_cases.append({
        "name": "3840x2160_s32_full_screen_rect",
        "width": 3840, "height": 2160, "s": 32,
        "rects": [[0, 0, 3840, 2160]]
    })
    damage_cases.append({
        "name": "3840x2160_s32_scattered_rects",
        "width": 3840, "height": 2160, "s": 32,
        "rects": [[0, 0, 32, 32], [3808, 0, 32, 32], [0, 2128, 32, 32], [3808, 2128, 32, 32]]
    })

    # Additional dimensions & cases to reach > 40 cases total
    more_specs = [
        ("small_1x1_s1", 1, 1, 1, [[0, 0, 1, 1]]),
        ("small_1x1_s1_off", 1, 1, 1, [[1, 0, 1, 1]]),
        ("small_2x2_s2", 2, 2, 2, [[1, 1, 1, 1]]),
        ("rect_single_tile_s64", 1920, 1080, 64, [[64, 64, 1, 1]]),
        ("rect_span_words", 1920, 1080, 32, [[0, 0, 1920, 2]]), # row 0 has 60 tiles (< 64 bits = 1 word), row 1 starts at tile 60
        ("rect_multi_row_col", 1920, 1080, 32, [[100, 100, 500, 300]]),
        ("rect_entire_row", 1920, 1080, 32, [[0, 64, 1920, 32]]),
        ("rect_entire_col", 1920, 1080, 32, [[64, 0, 32, 1080]]),
        ("rect_negative_w", 1920, 1080, 32, [[10, 10, -5, 10]]),
        ("rect_negative_h", 1920, 1080, 32, [[10, 10, 10, -5]]),
        ("err_dim_w_zero", 0, 1080, 32, [[0, 0, 10, 10]]),
        ("err_dim_h_zero", 1920, 0, 32, [[0, 0, 10, 10]]),
        ("err_dim_w_too_large", 16385, 1080, 32, [[0, 0, 10, 10]]),
        ("err_dim_h_too_large", 1920, 16385, 32, [[0, 0, 10, 10]]),
        ("err_grid_s0", 1920, 1080, 0, [[0, 0, 10, 10]]),
        ("err_grid_s3", 1920, 1080, 3, [[0, 0, 10, 10]]),
        ("err_grid_s512", 1920, 1080, 512, [[0, 0, 10, 10]]),
        ("err_grid_s_negative", 1920, 1080, -32, [[0, 0, 10, 10]]),
        ("multiple_rects_with_one_invalid_w", 1920, 1080, 32, [[0, 0, 10, 10], [5, 5, -1, 5]]),
    ]

    for item in more_specs:
        name, W, H, s, rects = item
        damage_cases.append({
            "height": H,
            "name": name,
            "rects": rects,
            "s": s,
            "width": W
        })

    # Evaluate expected values for all damage cases if not already present
    final_damage_cases = []
    for case in damage_cases:
        c = dict(case)
        err, words, popcount = damage_k2_ref(c["width"], c["height"], c["s"], c["rects"])
        if err is not None:
            c["error"] = err
        else:
            c["expected_words"] = words
            c["expected_popcount"] = popcount
        final_damage_cases.append(c)

    damage_json_data = {
        "cases": final_damage_cases,
        "schema": "langx.x1.fixtures/v1"
    }

    with open(os.path.join(base_dir, "damage_cases.json"), "w") as f:
        json.dump(damage_json_data, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"Generated {len(repack_cases)} repack cases, {len(final_damage_cases)} damage cases.")
    print(f"repack_src.bin size: {len(repack_src_bytes)} bytes, repack_dst.bin size: {len(repack_dst_bytes)} bytes.")


if __name__ == "__main__":
    generate_all()
