"""Independent reference oracle and differential tests for eval.v2proto_subtile and eval.v2x.st_block4."""

import json
import hashlib
import os
import subprocess
import tempfile
import unittest
import numpy as np

from visioncore.layout import grid_params
from eval.v2proto_subtile import subtile_motion as subtile_proto8
from eval.v2x.st_block4 import subtile_motion as subtile_block4


SEED_DIGESTS_PROTO8 = {
    "1001": "3f336ac0de281c2556a67f7ba556ff1211d1cd2c07d82a2bacaf329ba4f5a59e",
    "1002": "38bb97b7794fd933c5a5943b0764e89fb859142c52666c4f4339a9c4ce8a9e1d",
    "1003": "38bb97b7794fd933c5a5943b0764e89fb859142c52666c4f4339a9c4ce8a9e1d",
    "1004": "ee279759edf6a9e383aa9a33c851b1e9af968144ce5bb5a6f6a4bef24d9821e1",
    "1005": "8b1d3d05fe36b2cb19adf65987d6dc46e26f3c7aedde7859e5b623dd38564a01",
    "1006": "dfffc551e9319ab4f33eabca0d34a4b5df1693ee7a24476e13bcdca5ec8bed87",
}

SEED_DIGESTS_ST_BLOCK4 = {
    "1001": "83b40c6b488f458fc16f7ca1d899645e934308b7295e61f3c4d42dd5d5fc32d5",
    "1002": "38bb97b7794fd933c5a5943b0764e89fb859142c52666c4f4339a9c4ce8a9e1d",
    "1003": "38bb97b7794fd933c5a5943b0764e89fb859142c52666c4f4339a9c4ce8a9e1d",
    "1004": "2c95f943effca9f927b80adbdc7dc91cb8dfec742aad37fdb2df317b71744214",
    "1005": "b190ad5ec7e129417d51a2d8fc0854023bc140e50167731047216762383f3242",
    "1006": "fd36d69eeb97fc90203120b80ebbf7a4dcc4568bef284ffdf61f5f77c2609077",
}


def oracle_proto8(prev_luma, cur_luma, s, tile_xy, max_d):
    """Independent brute-force oracle for eval.v2proto_subtile subtile_motion."""
    prev_luma = np.asarray(prev_luma)
    cur_luma = np.asarray(cur_luma)
    H, W = cur_luma.shape

    s_grid, gw, gh = grid_params(W, H)
    if s is None:
        s = s_grid

    tx, ty = tile_xy
    if not (0 <= tx < gw and 0 <= ty < gh):
        return None

    tx_min = max(0, tx - 1)
    tx_max = min(gw - 1, tx + 1)
    ty_min = max(0, ty - 1)
    ty_max = min(gh - 1, ty + 1)

    tgt_x0 = tx * s
    tgt_x1 = min(W, (tx + 1) * s)
    tgt_y0 = ty * s
    tgt_y1 = min(H, (ty + 1) * s)

    x0 = tx_min * s
    x1 = min(W, (tx_max + 1) * s)
    y0 = ty_min * s
    y1 = min(H, (ty_max + 1) * s)

    tgt_cur = cur_luma[tgt_y0:tgt_y1, tgt_x0:tgt_x1]
    tgt_prev = prev_luma[tgt_y0:tgt_y1, tgt_x0:tgt_x1]
    tgt_diff_mask = (tgt_cur != tgt_prev)

    union_prev = prev_luma[y0:y1, x0:x1]
    bg_val = int(np.bincount(union_prev.ravel()).argmax())

    tgt_changed_fg_cur = tgt_diff_mask & (tgt_cur != bg_val)
    if not np.any(tgt_changed_fg_cur):
        return None

    union_cur = cur_luma[y0:y1, x0:x1]
    tgt_changed_fg_union = (union_cur != union_prev) & (
        (union_cur != bg_val) | (union_prev != bg_val)
    )
    if int(np.sum(tgt_changed_fg_union)) < 8:
        return None

    changed_blocks = []
    for by in range(y0, y1, 8):
        by_end = min(by + 8, H)
        for bx in range(x0, x1, 8):
            bx_end = min(bx + 8, W)
            cur_blk = cur_luma[by:by_end, bx:bx_end]
            prev_blk = prev_luma[by:by_end, bx:bx_end]
            if not np.array_equal(cur_blk, prev_blk):
                changed_blocks.append((bx, by, bx_end - bx, by_end - by))

    n_blocks = len(changed_blocks)
    if n_blocks == 0:
        return None

    prev_padded = np.pad(prev_luma, max_d, mode="edge")

    candidates = [
        (dx, dy)
        for dx in range(-max_d, max_d + 1)
        for dy in range(-max_d, max_d + 1)
        if not (dx == 0 and dy == 0)
    ]
    candidates.sort(
        key=lambda p: (p[0] ** 2 + p[1] ** 2, abs(p[0]), abs(p[1]), p[0], p[1])
    )

    cur_blks = [cur_luma[by : by + bh, bx : bx + bw] for bx, by, bw, bh in changed_blocks]

    for dx, dy in candidates:
        match = True
        for (bx, by, bw, bh), cur_blk in zip(changed_blocks, cur_blks):
            py0 = by - dy + max_d
            px0 = bx - dx + max_d
            prev_blk = prev_padded[py0 : py0 + bh, px0 : px0 + bw]
            if not np.array_equal(cur_blk, prev_blk):
                match = False
                break

        if not match:
            continue

        py_shifted = tgt_y0 - dy + max_d
        px_shifted = tgt_x0 - dx + max_d
        prev_target_shifted = prev_padded[
            py_shifted : py_shifted + (tgt_y1 - tgt_y0),
            px_shifted : px_shifted + (tgt_x1 - tgt_x0),
        ]

        if np.array_equal(tgt_cur[tgt_diff_mask], prev_target_shifted[tgt_diff_mask]):
            return (int(dx), int(dy), int(n_blocks))

    return None


def oracle_st_block4(prev_luma, cur_luma, s, tile_xy, max_d):
    """Independent brute-force oracle for eval.v2x.st_block4 subtile_motion."""
    prev_luma = np.asarray(prev_luma)
    cur_luma = np.asarray(cur_luma)
    H, W = cur_luma.shape

    s_grid, gw, gh = grid_params(W, H)
    if s is None:
        s = s_grid

    tx, ty = tile_xy
    if not (0 <= tx < gw and 0 <= ty < gh):
        return None

    tx_min = max(0, tx - 1)
    tx_max = min(gw - 1, tx + 1)
    ty_min = max(0, ty - 1)
    ty_max = min(gh - 1, ty + 1)

    x0 = tx_min * s
    x1 = min(W, (tx_max + 1) * s)
    y0 = ty_min * s
    y1 = min(H, (ty_max + 1) * s)

    changed_blocks = []
    for by in range(y0, y1, 4):
        by_end = min(H, by + 4)
        for bx in range(x0, x1, 4):
            bx_end = min(W, bx + 4)
            cur_blk = cur_luma[by:by_end, bx:bx_end]
            prev_blk = prev_luma[by:by_end, bx:bx_end]
            if not np.array_equal(cur_blk, prev_blk):
                changed_blocks.append((bx, by, bx_end, by_end))

    n_blocks = len(changed_blocks)
    if n_blocks == 0:
        return None

    ty0 = ty * s
    ty1 = min(H, (ty + 1) * s)
    tx0 = tx * s
    tx1 = min(W, (tx + 1) * s)
    cur_tile = cur_luma[ty0:ty1, tx0:tx1]
    prev_tile_zero = prev_luma[ty0:ty1, tx0:tx1]

    prev_padded = np.pad(prev_luma, max_d, mode="edge")

    candidates = [
        (dx, dy)
        for dx in range(-max_d, max_d + 1)
        for dy in range(-max_d, max_d + 1)
        if not (dx == 0 and dy == 0)
    ]
    candidates.sort(
        key=lambda p: (p[0] ** 2 + p[1] ** 2, abs(p[0]), abs(p[1]), p[0], p[1])
    )

    cur_blks = [cur_luma[by:by_end, bx:bx_end] for bx, by, bx_end, by_end in changed_blocks]

    for dx, dy in candidates:
        match = True
        for (bx, by, bx_end, by_end), cur_blk in zip(changed_blocks, cur_blks):
            py0 = by - dy + max_d
            py1 = by_end - dy + max_d
            px0 = bx - dx + max_d
            px1 = bx_end - dx + max_d
            prev_blk = prev_padded[py0:py1, px0:px1]
            if not np.array_equal(cur_blk, prev_blk):
                match = False
                break

        if match:
            if np.array_equal(cur_tile, prev_tile_zero):
                return None

            spy0 = ty0 - dy
            spy1 = ty1 - dy
            spx0 = tx0 - dx
            spx1 = tx1 - dx
            if spy0 < 0 or spy1 > H or spx0 < 0 or spx1 > W:
                return None

            prev_tile_trans = prev_luma[spy0:spy1, spx0:spx1]
            if np.array_equal(cur_tile, prev_tile_trans):
                return (int(dx), int(dy), int(n_blocks))
            else:
                return None

    return None


def generate_seed_frames(seed, W=128, H=128, s=16):
    """Generate deterministic RAM frames for seed digests."""
    rng = np.random.RandomState(seed)
    bg = int(rng.randint(150, 220))
    fg = int(rng.randint(10, 80))
    prev = np.full((H, W), bg, dtype=np.uint8)
    cur = prev.copy()

    dx_val = int(rng.randint(-10, 11))
    dy_val = int(rng.randint(-10, 11))
    if dx_val == 0 and dy_val == 0:
        dx_val = 3

    gx = int(rng.randint(32, 64))
    gy = int(rng.randint(32, 64))
    gw_box = int(rng.randint(8, 16))
    gh_box = int(rng.randint(8, 16))

    prev[gy : gy + gh_box, gx : gx + gw_box] = fg
    cur[gy + dy_val : gy + dy_val + gh_box, gx + dx_val : gx + dx_val + gw_box] = fg

    for _ in range(3):
        rx = int(rng.randint(0, W - 8))
        ry = int(rng.randint(0, H - 8))
        val = int(rng.randint(0, 256))
        prev[ry : ry + 8, rx : rx + 8] = val
        cur[ry : ry + 8, rx : rx + 8] = val

    return prev, cur, s, 16


class TestKrefStProto8(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_glyph_translated_by_vector_within_max_d(self):
        """A glyph translated by vectors within max_d matches oracle and reference."""
        W, H, s, max_d = 128, 128, 32, 16
        bg_color, fg_color = 200, 50

        test_vectors = [(3, 0), (0, 3), (-2, 2), (4, -4), (-3, -3)]

        for dx, dy in test_vectors:
            prev = np.full((H, W), bg_color, dtype=np.uint8)
            cur = np.full((H, W), bg_color, dtype=np.uint8)

            prev[40:52, 40:52] = fg_color
            cur[40 + dy : 52 + dy, 40 + dx : 52 + dx] = fg_color

            p8_ref = subtile_proto8(prev, cur, s=s, tile_xy=(1, 1), max_d=max_d)
            p8_orc = oracle_proto8(prev, cur, s=s, tile_xy=(1, 1), max_d=max_d)
            self.assertEqual(p8_ref, p8_orc)
            self.assertIsNotNone(p8_ref)
            self.assertEqual(p8_ref[:2], (dx, dy))

            b4_ref = subtile_block4(prev, cur, s=s, tile_xy=(1, 1), max_d=max_d)
            b4_orc = oracle_st_block4(prev, cur, s=s, tile_xy=(1, 1), max_d=max_d)
            self.assertEqual(b4_ref, b4_orc)
            self.assertIsNotNone(b4_ref)
            self.assertEqual(b4_ref[:2], (dx, dy))

    def test_candidate_tie_break_decision(self):
        """Two candidates explaining blocks equally rely on deterministic distance tie-break."""
        W, H, s, max_d = 128, 128, 16, 16
        bg_color, fg_color = 200, 50

        prev = np.full((H, W), bg_color, dtype=np.uint8)
        cur = np.full((H, W), bg_color, dtype=np.uint8)

        prev[40:50, 40:45] = fg_color
        prev[40:50, 50:55] = fg_color

        cur[40:50, 42:47] = fg_color
        cur[40:50, 52:57] = fg_color

        p8_ref = subtile_proto8(prev, cur, s=s, tile_xy=(2, 2), max_d=max_d)
        p8_orc = oracle_proto8(prev, cur, s=s, tile_xy=(2, 2), max_d=max_d)
        self.assertEqual(p8_ref, p8_orc)

        b4_ref = subtile_block4(prev, cur, s=s, tile_xy=(2, 2), max_d=max_d)
        b4_orc = oracle_st_block4(prev, cur, s=s, tile_xy=(2, 2), max_d=max_d)
        self.assertEqual(b4_ref, b4_orc)

    def test_exposed_background(self):
        """Exposed background tile after feature moves away returns None."""
        W, H, s, max_d = 128, 128, 16, 16
        bg_color, fg_color = 200, 50

        prev = np.full((H, W), bg_color, dtype=np.uint8)
        cur = np.full((H, W), bg_color, dtype=np.uint8)

        prev[40:52, 40:52] = fg_color
        cur[40:52, 70:82] = fg_color

        p8_ref = subtile_proto8(prev, cur, s=s, tile_xy=(2, 2), max_d=max_d)
        p8_orc = oracle_proto8(prev, cur, s=s, tile_xy=(2, 2), max_d=max_d)
        self.assertIsNone(p8_ref)
        self.assertEqual(p8_ref, p8_orc)

        b4_ref = subtile_block4(prev, cur, s=s, tile_xy=(2, 2), max_d=max_d)
        b4_orc = oracle_st_block4(prev, cur, s=s, tile_xy=(2, 2), max_d=max_d)
        self.assertIsNone(b4_ref)
        self.assertEqual(b4_ref, b4_orc)

    def test_partial_edge_tiles_1000x563(self):
        """Partial edge tiles on 1000x563 non-standard resolution frame."""
        W, H, s, max_d = 1000, 563, 16, 16
        bg_color, fg_color = 200, 50

        prev = np.full((H, W), bg_color, dtype=np.uint8)
        cur = np.full((H, W), bg_color, dtype=np.uint8)

        prev[548:558, 330:340] = fg_color
        cur[548:558, 335:345] = fg_color

        p8_ref = subtile_proto8(prev, cur, s=s, tile_xy=(21, 34), max_d=max_d)
        p8_orc = oracle_proto8(prev, cur, s=s, tile_xy=(21, 34), max_d=max_d)
        self.assertEqual(p8_ref, p8_orc)
        self.assertIsNotNone(p8_ref)
        self.assertEqual(p8_ref[:2], (5, 0))

        b4_ref = subtile_block4(prev, cur, s=s, tile_xy=(21, 34), max_d=max_d)
        b4_orc = oracle_st_block4(prev, cur, s=s, tile_xy=(21, 34), max_d=max_d)
        self.assertEqual(b4_ref, b4_orc)
        self.assertIsNotNone(b4_ref)
        self.assertEqual(b4_ref[:2], (5, 0))

    def test_thin_structures_under_8_changed_pixels(self):
        """Thin structures under 8 changed pixels are rejected by proto8 minimum-evidence rule."""
        W, H, s, max_d = 128, 128, 16, 16
        bg_color, fg_color = 200, 50

        prev = np.full((H, W), bg_color, dtype=np.uint8)
        cur = np.full((H, W), bg_color, dtype=np.uint8)

        # 1x3 caret: 3 changed pixels in prev, 3 in cur = 6 changed pixels in union (< 8)
        prev[40:43, 40:41] = fg_color
        cur[40:43, 45:46] = fg_color

        p8_ref = subtile_proto8(prev, cur, s=s, tile_xy=(2, 2), max_d=max_d)
        p8_orc = oracle_proto8(prev, cur, s=s, tile_xy=(2, 2), max_d=max_d)
        self.assertIsNone(p8_ref)
        self.assertEqual(p8_ref, p8_orc)

        b4_ref = subtile_block4(prev, cur, s=s, tile_xy=(2, 2), max_d=max_d)
        b4_orc = oracle_st_block4(prev, cur, s=s, tile_xy=(2, 2), max_d=max_d)
        self.assertEqual(b4_ref, b4_orc)

    def test_identical_frames(self):
        """Identical frames return None for both proto8 and st_block4."""
        W, H, s, max_d = 128, 128, 16, 16
        frame = np.full((H, W), 128, dtype=np.uint8)

        p8_ref = subtile_proto8(frame, frame, s=s, tile_xy=(2, 2), max_d=max_d)
        p8_orc = oracle_proto8(frame, frame, s=s, tile_xy=(2, 2), max_d=max_d)
        self.assertIsNone(p8_ref)
        self.assertEqual(p8_ref, p8_orc)

        b4_ref = subtile_block4(frame, frame, s=s, tile_xy=(2, 2), max_d=max_d)
        b4_orc = oracle_st_block4(frame, frame, s=s, tile_xy=(2, 2), max_d=max_d)
        self.assertIsNone(b4_ref)
        self.assertEqual(b4_ref, b4_orc)

    def test_reference_sha256_digests_proto8(self):
        """Verify sha256 digests of proto8 reference output for 6 fixed seeds match pinned constants."""
        for seed_str, expected_sha in SEED_DIGESTS_PROTO8.items():
            seed = int(seed_str)
            prev, cur, s, max_d = generate_seed_frames(seed)
            W, H = cur.shape
            s_grid, gw, gh = grid_params(W, H)

            out = []
            for ty in range(gh):
                for tx in range(gw):
                    res = subtile_proto8(prev, cur, s, (tx, ty), max_d)
                    out.append(((tx, ty), res))

            s_bytes = json.dumps(out, sort_keys=True).encode("utf-8")
            digest = hashlib.sha256(s_bytes).hexdigest()
            self.assertEqual(digest, expected_sha, f"Proto8 digest mismatch for seed {seed}")

    def test_reference_sha256_digests_st_block4(self):
        """Verify sha256 digests of st_block4 reference output for 6 fixed seeds match pinned constants."""
        for seed_str, expected_sha in SEED_DIGESTS_ST_BLOCK4.items():
            seed = int(seed_str)
            prev, cur, s, max_d = generate_seed_frames(seed)
            W, H = cur.shape
            s_grid, gw, gh = grid_params(W, H)

            out = []
            for ty in range(gh):
                for tx in range(gw):
                    res = subtile_block4(prev, cur, s, (tx, ty), max_d)
                    out.append(((tx, ty), res))

            s_bytes = json.dumps(out, sort_keys=True).encode("utf-8")
            digest = hashlib.sha256(s_bytes).hexdigest()
            self.assertEqual(digest, expected_sha, f"St_block4 digest mismatch for seed {seed}")

    @unittest.expectedFailure
    def test_bug_grid_bounds_mismatch_when_s_overridden(self):
        """grid_bounds_mismatch_when_s_overridden: eval.v2proto_subtile uses grid_params(W, H) gw/gh without recomputing for overridden s."""
        W, H, s = 128, 128, 32
        frame = np.full((H, W), 200, dtype=np.uint8)
        res = subtile_proto8(frame, frame, s=s, tile_xy=(7, 7), max_d=16)
        self.assertIsNone(res)

    @unittest.skipIf("VQA_K_ST_PROTO8_BIN" not in os.environ, "VQA_K_ST_PROTO8_BIN not set")
    def test_native_differential_proto8(self):
        """Native differential test against release binary of paired Spark port when VQA_K_ST_PROTO8_BIN is set."""
        bin_path = os.environ["VQA_K_ST_PROTO8_BIN"]
        self.assertTrue(os.path.exists(bin_path), f"Binary path {bin_path} does not exist")

        W, H, s, max_d = 128, 128, 16, 16
        bg_color, fg_color = 200, 50
        prev = np.full((H, W), bg_color, dtype=np.uint8)
        cur = np.full((H, W), bg_color, dtype=np.uint8)
        prev[40:52, 40:52] = fg_color
        cur[40:52, 45:57] = fg_color

        ref_res = subtile_proto8(prev, cur, s, (2, 2), max_d)
        proc = subprocess.run(
            [bin_path, "--proto8"],
            input=json.dumps({"s": s, "tx": 2, "ty": 2, "max_d": max_d}),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        native_res = json.loads(proc.stdout)
        self.assertEqual(tuple(native_res), tuple(ref_res))

    @unittest.skipIf("VQA_K_ST_BLOCK4_BIN" not in os.environ, "VQA_K_ST_BLOCK4_BIN not set")
    def test_native_differential_st_block4(self):
        """Native differential test against release binary of paired Spark port when VQA_K_ST_BLOCK4_BIN is set."""
        bin_path = os.environ["VQA_K_ST_BLOCK4_BIN"]
        self.assertTrue(os.path.exists(bin_path), f"Binary path {bin_path} does not exist")

        W, H, s, max_d = 128, 128, 16, 16
        bg_color, fg_color = 200, 50
        prev = np.full((H, W), bg_color, dtype=np.uint8)
        cur = np.full((H, W), bg_color, dtype=np.uint8)
        prev[40:52, 40:52] = fg_color
        cur[40:52, 45:57] = fg_color

        ref_res = subtile_block4(prev, cur, s, (2, 2), max_d)
        proc = subprocess.run(
            [bin_path, "--block4"],
            input=json.dumps({"s": s, "tx": 2, "ty": 2, "max_d": max_d}),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        native_res = json.loads(proc.stdout)
        self.assertEqual(tuple(native_res), tuple(ref_res))


if __name__ == "__main__":
    unittest.main()
