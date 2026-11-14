"""Fixed-seed property tests comparing S2 change kernels against naive pure-Python reference loops."""
import unittest
import numpy as np

from visioncore import change, layout


def ref_popcount(a):
    arr = np.asarray(a)
    flat = arr.ravel()
    res = []
    for x in flat:
        val = int(x)
        if val < 0:
            val = val & ((1 << (arr.dtype.itemsize * 8)) - 1)
        res.append(bin(val).count("1"))
    return np.array(res, dtype=np.uint8).reshape(arr.shape)


def ref_as_tiles(tiles):
    out = np.empty(len(tiles), dtype=change.TILE_DT)
    for name in layout.TILE_FIELDS:
        out[name] = tiles[name]
    return out


def ref_axis_shift(cur, prev, dmax):
    n = len(cur)
    best, cost0 = None, None
    for d in range(-dmax, dmax + 1):
        if abs(d) >= n:
            continue
        if d >= 0:
            sad = sum(abs(int(cur[x]) - int(prev[x - d])) for x in range(d, n))
        else:
            sad = sum(abs(int(cur[x]) - int(prev[x - d])) for x in range(0, n + d))
        cost = sad * n // (n - abs(d))
        if d == 0:
            cost0 = cost
        key = (cost, abs(d), d)
        if best is None or key < best:
            best = key
    if best is None or best[2] == 0 or not (3 * best[0] < cost0):
        return 0
    return best[2]


def ref_tile_sums(absdiff, s, gh, gw):
    sums = np.zeros((gh, gw), dtype=absdiff.dtype)
    for ty in range(gh):
        for tx in range(gw):
            tile_area = absdiff[ty * s:(ty + 1) * s, tx * s:(tx + 1) * s]
            sums[ty, tx] = int(tile_area.sum())
    return sums


def ref_morton_xy(m):
    m = np.asarray(m, dtype=np.int64)
    tx_list, ty_list = [], []
    for val in m.ravel():
        x, y = 0, 0
        for bit in range(16):
            if (val >> (2 * bit)) & 1:
                x |= (1 << bit)
            if (val >> (2 * bit + 1)) & 1:
                y |= (1 << bit)
        tx_list.append(x)
        ty_list.append(y)
    return np.array(tx_list, dtype=np.int64).reshape(m.shape), np.array(ty_list, dtype=np.int64).reshape(m.shape)


def ref_tile_sad_windows(y_cur, y_prev, c, s, tx, ty):
    dx, dy = c
    H, W = y_cur.shape
    tx_arr = np.asarray(tx, dtype=np.int64)
    ty_arr = np.asarray(ty, dtype=np.int64)
    tx_flat = np.where(tx_arr.ravel() < 0, tx_arr.ravel() + W // s, tx_arr.ravel())
    ty_flat = np.where(ty_arr.ravel() < 0, ty_arr.ravel() + H // s, ty_arr.ravel())

    sad_res, cnt_res = [], []
    for txi, tyi in zip(tx_flat, ty_flat):
        yy = tyi * s
        xx = txi * s
        sy = yy - dy
        sx = xx - dx

        ny = max(0, min(sy + s, H) - max(sy, 0))
        nx = max(0, min(sx + s, W) - max(sx, 0))
        count = ny * nx

        sad = 0
        if count > 0:
            y0, y1 = max(int(yy), dy), min(int(yy) + s, H + dy)
            x0, x1 = max(int(xx), dx), min(int(xx) + s, W + dx)
            cur_sub = y_cur[y0:y1, x0:x1].astype(np.int64)
            src_sub = y_prev[y0 - dy:y1 - dy, x0 - dx:x1 - dx].astype(np.int64)
            sad = int(np.abs(cur_sub - src_sub).sum())
        sad_res.append(sad)
        cnt_res.append(count)

    return np.array(sad_res, dtype=np.int64).reshape(tx_arr.shape), np.array(cnt_res, dtype=np.int64).reshape(tx_arr.shape)


def ref_islands(mask):
    gh, gw = mask.shape
    lab = np.zeros((gh, gw), dtype=np.int32)
    n = 0
    for r in range(gh):
        for c in range(gw):
            if mask[r, c] and lab[r, c] == 0:
                n += 1
                lab[r, c] = n
                stack = [(r, c)]
                while stack:
                    curr_r, curr_c = stack.pop()
                    for dr in (-1, 0, 1):
                        for dc in (-1, 0, 1):
                            if dr == 0 and dc == 0:
                                continue
                            nr, nc = curr_r + dr, curr_c + dc
                            if 0 <= nr < gh and 0 <= nc < gw:
                                if mask[nr, nc] and lab[nr, nc] == 0:
                                    lab[nr, nc] = n
                                    stack.append((nr, nc))
    return lab, n


class TestChangeTilesProps(unittest.TestCase):
    def test_popcount(self):
        # Explicit special values test (0 and all 1s)
        special = np.array([0, 0xFFFFFFFFFFFFFFFF], dtype=np.uint64)
        np.testing.assert_array_equal(change._popcount(special), np.array([0, 64], dtype=np.uint8))

        # Negative case: verify exact value output for uint64 zero (must be 0, not non-zero)
        self.assertEqual(int(change._popcount(np.uint64(0))), 0)
        self.assertEqual(int(change._popcount(np.uint64(0xFFFFFFFFFFFFFFFF))), 64)

        # Seeds 0..7
        for seed in range(8):
            rng = np.random.default_rng(seed)
            a = rng.integers(0, 2**64, size=(10, 10), dtype=np.uint64)
            expected = ref_popcount(a)
            actual = change._popcount(a)
            np.testing.assert_array_equal(actual, expected)

    def test_as_tiles(self):
        # Negative case: missing key/field raises IndexError, KeyError or ValueError
        invalid_tiles = np.zeros(2, dtype=[("y_q4", "<u2")])
        with self.assertRaises((IndexError, KeyError, ValueError, TypeError)):
            change._as_tiles(invalid_tiles)

        # Seeds 0..7
        for seed in range(8):
            rng = np.random.default_rng(seed)
            n_tiles = rng.integers(1, 20)
            tiles_arr = np.zeros(n_tiles, dtype=change.TILE_DT)
            tiles_arr["morton"] = rng.integers(0, 65536, n_tiles, dtype=np.uint64)
            tiles_arr["cls"] = rng.integers(0, 7, n_tiles, dtype=np.uint8)
            tiles_arr["flags"] = rng.integers(0, 256, n_tiles, dtype=np.uint8)
            tiles_arr["y_q4"] = rng.integers(0, 1024, n_tiles, dtype=np.uint16)
            tiles_arr["co_h"] = rng.integers(0, 256, n_tiles, dtype=np.uint8)
            tiles_arr["cg_h"] = rng.integers(0, 256, n_tiles, dtype=np.uint8)
            tiles_arr["sig_q4"] = rng.integers(0, 1024, n_tiles, dtype=np.uint16)
            tiles_arr["grad_q6"] = rng.integers(-64, 64, n_tiles, dtype=np.int8)
            tiles_arr["edge"] = rng.integers(0, 1000, n_tiles, dtype=np.uint16)
            tiles_arr["resid"] = rng.integers(0, 1000, n_tiles, dtype=np.uint16)
            tiles_arr["mv_dx"] = rng.integers(-10, 10, n_tiles, dtype=np.int8)
            tiles_arr["mv_dy"] = rng.integers(-10, 10, n_tiles, dtype=np.int8)
            tiles_arr["sig64"] = rng.integers(0, 2**64, n_tiles, dtype=np.uint64)
            tiles_arr["xh64"] = rng.integers(0, 2**64, n_tiles, dtype=np.uint64)

            expected = ref_as_tiles(tiles_arr)
            actual = change._as_tiles(tiles_arr)
            self.assertEqual(actual.dtype, change.TILE_DT)
            self.assertEqual(len(actual), n_tiles)
            for field in layout.TILE_FIELDS:
                np.testing.assert_array_equal(actual[field], expected[field])

    def test_axis_shift(self):
        # Negative case: flat constant projection yields 0 (no clear winner)
        flat_cur = np.full(20, 50, dtype=np.int64)
        flat_prev = np.full(20, 50, dtype=np.int64)
        self.assertEqual(change._axis_shift(flat_cur, flat_prev, dmax=4), 0)

        # Seeds 0..7
        for seed in range(8):
            rng = np.random.default_rng(seed)
            length = rng.integers(10, 30)
            dmax = rng.integers(1, 8)
            cur = rng.integers(0, 256, length, dtype=np.int64)
            prev = rng.integers(0, 256, length, dtype=np.int64)
            expected = ref_axis_shift(cur, prev, dmax)
            actual = change._axis_shift(cur, prev, dmax)
            self.assertEqual(actual, expected)

    def test_tile_sums(self):
        # Negative case: zero input difference plane yields all zero tile sums
        zero_diff = np.zeros((12, 16), dtype=np.int16)
        zero_sums = change._tile_sums(zero_diff, s=4, gh=3, gw=4)
        np.testing.assert_array_equal(zero_sums, np.zeros((3, 4), dtype=np.int16))

        # Seeds 0..7 across grid sizes gh=2..5, gw=2..4, tile sizes s in (4, 8)
        for seed in range(8):
            rng = np.random.default_rng(seed)
            gh = rng.integers(2, 6)
            gw = rng.integers(2, 5)
            for s in (4, 8):
                diff = rng.integers(0, 256, (gh * s, gw * s), dtype=np.int16)
                expected = ref_tile_sums(diff, s, gh, gw)
                actual = change._tile_sums(diff, s, gh, gw)
                np.testing.assert_array_equal(actual, expected)

    def test_morton_xy(self):
        # Negative case: index 0 gives (0, 0)
        tx, ty = change._morton_xy(np.array([0], dtype=np.uint64))
        self.assertEqual(int(tx[0]), 0)
        self.assertEqual(int(ty[0]), 0)

        # Seeds 0..7
        for seed in range(8):
            rng = np.random.default_rng(seed)
            m = rng.integers(0, 65536, size=(10,), dtype=np.uint64)
            exp_x, exp_y = ref_morton_xy(m)
            act_x, act_y = change._morton_xy(m)
            np.testing.assert_array_equal(act_x, exp_x)
            np.testing.assert_array_equal(act_y, exp_y)

    def test_tile_sad_windows(self):
        # Negative case: out-of-bounds tile index raises IndexError
        y1 = np.zeros((16, 16), dtype=np.int16)
        y2 = np.zeros((16, 16), dtype=np.int16)
        with self.assertRaises(IndexError):
            change._tile_sad_windows(y1, y2, (0, 0), s=4, tx=[10], ty=[0])

        # Seeds 0..7 across gh in 2..5, gw in 2..4, tile size s in (4, 8)
        for seed in range(8):
            rng = np.random.default_rng(seed)
            gh = rng.integers(2, 6)
            gw = rng.integers(2, 5)
            for s in (4, 8):
                y_cur = rng.integers(0, 256, (gh * s, gw * s), dtype=np.int16)
                y_prev = rng.integers(0, 256, (gh * s, gw * s), dtype=np.int16)
                c = (int(rng.integers(-s, s + 1)), int(rng.integers(-s, s + 1)))
                tx = rng.integers(0, gw, size=gh * gw, dtype=np.int64)
                ty = rng.integers(0, gh, size=gh * gw, dtype=np.int64)

                exp_sad, exp_cnt = ref_tile_sad_windows(y_cur, y_prev, c, s, tx, ty)
                act_sad, act_cnt = change._tile_sad_windows(y_cur, y_prev, c, s, tx, ty)
                np.testing.assert_array_equal(act_sad, exp_sad)
                np.testing.assert_array_equal(act_cnt, exp_cnt)

    def test_tile_sad(self):
        # Negative case: shift pushing tile window partially off-screen yields valid == False
        s = 4
        y_cur = np.zeros((8, 8), dtype=np.int16)
        y_prev = np.zeros((8, 8), dtype=np.int16)
        # Shift (1, 0) for tile at tx=0, ty=0 shifts source to sx = -1, off-screen
        sad, valid = change._tile_sad(y_cur, y_prev, (1, 0), s, gh=2, gw=2, tx=[0], ty=[0])
        self.assertFalse(bool(valid[0]))

        # Seeds 0..7 across gh in 2..5, gw in 2..4, tile size s in (4, 8)
        for seed in range(8):
            rng = np.random.default_rng(seed)
            gh = rng.integers(2, 6)
            gw = rng.integers(2, 5)
            for s in (4, 8):
                y_cur = rng.integers(0, 256, (gh * s, gw * s), dtype=np.int16)
                y_prev = rng.integers(0, 256, (gh * s, gw * s), dtype=np.int16)
                c = (int(rng.integers(-s, s + 1)), int(rng.integers(-s, s + 1)))
                tx = rng.integers(0, gw, size=gh * gw, dtype=np.int64)
                ty = rng.integers(0, gh, size=gh * gw, dtype=np.int64)

                exp_sad, exp_cnt = ref_tile_sad_windows(y_cur, y_prev, c, s, tx, ty)
                exp_valid = exp_cnt == s * s
                act_sad, act_valid = change._tile_sad(y_cur, y_prev, c, s, gh, gw, tx, ty)
                np.testing.assert_array_equal(act_sad, exp_sad)
                np.testing.assert_array_equal(act_valid, exp_valid)

    def test_tile_sad_clip(self):
        # Negative case: shift completely off-screen yields count == 0 and sad == 0
        s = 4
        y_cur = np.zeros((8, 8), dtype=np.int16)
        y_prev = np.zeros((8, 8), dtype=np.int16)
        sad, count = change._tile_sad_clip(y_cur, y_prev, (100, 100), s, gh=2, gw=2, tx=[0], ty=[0])
        self.assertEqual(int(sad[0]), 0)
        self.assertEqual(int(count[0]), 0)

        # Seeds 0..7 across gh in 2..5, gw in 2..4, tile size s in (4, 8)
        for seed in range(8):
            rng = np.random.default_rng(seed)
            gh = rng.integers(2, 6)
            gw = rng.integers(2, 5)
            for s in (4, 8):
                y_cur = rng.integers(0, 256, (gh * s, gw * s), dtype=np.int16)
                y_prev = rng.integers(0, 256, (gh * s, gw * s), dtype=np.int16)
                c = (int(rng.integers(-s, s + 1)), int(rng.integers(-s, s + 1)))
                tx = rng.integers(0, gw, size=gh * gw, dtype=np.int64)
                ty = rng.integers(0, gh, size=gh * gw, dtype=np.int64)

                exp_sad, exp_cnt = ref_tile_sad_windows(y_cur, y_prev, c, s, tx, ty)
                act_sad, act_cnt = change._tile_sad_clip(y_cur, y_prev, c, s, gh, gw, tx, ty)
                np.testing.assert_array_equal(act_sad, exp_sad)
                np.testing.assert_array_equal(act_cnt, exp_cnt)

    def test_islands(self):
        # Negative case: all-False mask returns 0 islands and zero label matrix
        empty_mask = np.zeros((4, 5), dtype=bool)
        lab, n = change._islands(empty_mask)
        self.assertEqual(n, 0)
        np.testing.assert_array_equal(lab, np.zeros((4, 5), dtype=np.int32))

        # Seeds 0..7 across gh in 2..5, gw in 2..4
        for seed in range(8):
            rng = np.random.default_rng(seed)
            gh = rng.integers(2, 6)
            gw = rng.integers(2, 5)
            mask = rng.random((gh, gw)) < 0.4
            exp_lab, exp_n = ref_islands(mask)
            act_lab, act_n = change._islands(mask)
            self.assertEqual(act_n, exp_n)
            np.testing.assert_array_equal(act_lab, exp_lab)


if __name__ == "__main__":
    unittest.main()
