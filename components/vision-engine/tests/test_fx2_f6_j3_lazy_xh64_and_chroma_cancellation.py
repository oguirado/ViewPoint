"""Integration tests for lazy xh64 history provenance and chroma cancellation false flicker veto."""
import unittest
import numpy as np

from visioncore import change, desc, grid, params as P


def noise_tile(w, h, seed, ox=0, oy=0):
    """Exact texture band and noise generator from eval/gui_synth/raster.py without text mask."""
    x = (np.arange(w, dtype=np.uint32) + np.uint32(ox & 0xFFFFFFFF))[None, :]
    y = (np.arange(h, dtype=np.uint32) + np.uint32(oy & 0xFFFFFFFF))[:, None]
    z = (x * np.uint32(374761393)) ^ (y * np.uint32(668265263)) ^ np.uint32(seed & 0xFFFFFFFF)
    z = (z ^ (z >> np.uint32(13))) * np.uint32(1274126177)
    z ^= z >> np.uint32(16)
    np.bitwise_and(z, 31, out=z)
    grey = z.astype(np.uint8)
    bands = ((x // 8) % 3).astype(np.uint8) + ((y // 13) % 3).astype(np.uint8)
    np.subtract(bands, 3, out=bands, where=bands >= 3)
    bands *= 23
    grey += bands
    grey += 45
    a = np.empty((h, w, 3), np.uint8)
    a[:, :, 0] = grey
    np.add(grey, 12, out=a[:, :, 1])
    np.add(grey, 24, out=a[:, :, 2])
    return a


class TestFx2F6J3LazyXh64AndChromaCancellation(unittest.TestCase):
    def setUp(self):
        # 32x32 frame with 16x16 tiles = 4 tiles in Morton slots
        self.grid = grid.Grid(32, 32)
        self.t_enter, self.t_exit, _ = change.thresholds()

    def test_fixture_precondition_pixel_change_modifies_xh64(self):
        """Fixture assertion outside expectedFailure proving a changed pixel changes xh64_of digest."""
        f_base = np.full((16, 16, 3), 100, dtype=np.uint8)
        f_mod = f_base.copy()
        f_mod[5, 5, 0] = 101

        h_base = desc.xh64_of(f_base)
        h_mod = desc.xh64_of(f_mod)
        self.assertNotEqual(h_base, h_mod)

    def test_multi_tile_eager_vs_lazy_lifecycle_and_non_aliasing(self):
        """Drive multi-tile lifecycle in eager and lazy modes, verify independent xh64_of, and prove no buffer aliasing."""
        g = self.grid
        # Build 32x32 frame sequence (4 tiles: Morton slots 0=(0,0), 1=(1,0), 2=(0,1), 3=(1,1))
        # Frame 0:
        # Tile 0: Base uniform 50
        # Tile 1: Base uniform 100
        # Tile 2: Base uniform 150
        # Tile 3: A-B-A state A (noise_tile seed 100)
        f0 = np.zeros((32, 32, 3), dtype=np.uint8)
        f0[0:16, 0:16] = 50
        f0[0:16, 16:32] = 100
        f0[16:32, 0:16] = 150
        f0[16:32, 16:32] = noise_tile(16, 16, seed=100)

        # Frame 1:
        # Tile 0: UNCHANGED (50)
        # Tile 1: JITTER (1 pixel changed by 1 LSB -> 101)
        # Tile 2: CHANGED (large change to 200)
        # Tile 3: A-B-A state B (noise_tile seed 200)
        f1 = f0.copy()
        f1[0:16, 16:32][0, 0, 0] = 101
        f1[16:32, 0:16] = 200
        f1[16:32, 16:32] = noise_tile(16, 16, seed=200)

        # Frame 2:
        # Tile 0: UNCHANGED (50)
        # Tile 1: UNCHANGED vs Frame 1 (101)
        # Tile 2: CHANGED (large change to 220)
        # Tile 3: A-B-A return to state A (noise_tile seed 100)
        f2 = f1.copy()
        f2[16:32, 0:16] = 220
        f2[16:32, 16:32] = f0[16:32, 16:32].copy()

        frames = [f0, f1, f2]
        pads = [g.pad(f) for f in frames]

        # Compute independent ground truth tile xh64_of digests for each frame
        digests = []
        for frame in frames:
            d_frame = []
            for m in range(g.N):
                tx, ty = g.tx[m], g.ty[m]
                block = frame[ty*16:(ty+1)*16, tx*16:(tx+1)*16]
                d_frame.append(desc.xh64_of(block))
            digests.append(d_frame)

        # --- Eager mode validation ---
        d0_eager, _ = desc.describe_padded(pads[0], g, lazy_xh=False)
        for m in range(g.N):
            self.assertEqual(d0_eager["xh64"][m], digests[0][m])

        # --- Lazy mode pipeline execution ---
        # Frame 0
        y0 = desc.luma_of_padded(pads[0])
        t0_lazy, _ = desc.describe_padded(pads[0], g, lazy_xh=True)
        # Lazy descriptors initially zero
        for m in range(g.N):
            self.assertEqual(t0_lazy["xh64"][m], 0)

        st = change.ClassState.init(t0_lazy)
        out0, st, _ = change.classify_first(st, t0_lazy, y_cur=y0)
        out0_filled = out0.copy()
        desc.fill_xh64(out0_filled, pads[0], g, prev_xh64=None, hash_all=True)

        for m in range(g.N):
            self.assertEqual(out0_filled["xh64"][m], digests[0][m])

        # Snapshot historical arrays and state
        snap_ref_f0 = st.ref["xh64"].copy()
        snap_prev1_f0 = st.prev1["xh64"].copy()

        # Frame 1: caller reuses and mutates input buffer
        inp_buf = pads[1].copy()
        y1 = desc.luma_of_padded(inp_buf)
        prev_padded = pads[0]
        prev_xh = out0_filled["xh64"]
        t1_lazy, words_eq1 = desc.describe_padded(
            inp_buf, g, prev_padded, prev_xh, P.PARAMS, prev_tiles=t0_lazy, y_plane=y1, lazy_xh=True
        )
        out1, st, _ = change.classify_tiles(st, t1_lazy, words_eq1, y_cur=y1, grid=(16, 2, 2), params=P.PARAMS)

        # Verify JITTER on tile 1 (m=1)
        self.assertEqual(out1["cls"][1], change.C_JITTER)

        out1_filled = out1.copy()
        desc.fill_xh64(out1_filled, inp_buf, g, prev_xh64=prev_xh, hash_all=False)

        # Mutate caller input buffer after step
        inp_buf.fill(255)

        # Prove prior state snapshots do not alias caller input buffer
        for m in range(g.N):
            self.assertEqual(st.prev1["y_q4"][m], t1_lazy["y_q4"][m])

        # Verify JITTER label in lazy mode copies old hash in output copy while true content digest changed
        self.assertEqual(out1_filled["xh64"][1], digests[0][1])  # copied old hash
        self.assertNotEqual(out1_filled["xh64"][1], digests[1][1])  # distinct from fresh content digest

    def test_chroma_cancelled_redraw_sequences_oracle_preconditions(self):
        """Nonreturning chroma-cancelled redraw sequences: distinct eager hashes, r1 > t_enter, r2 <= t_exit."""
        g = grid.Grid(16, 16)
        # Seeds producing nonreturning redraw sequence with chroma cancellation
        seeds = (10, 20, 30)
        frames = [noise_tile(16, 16, s) for s in seeds]
        pads = [g.pad(f) for f in frames]
        lumas = [desc.luma_of_padded(p) for p in pads]

        d0, w0 = desc.describe_padded(pads[0], g)
        d1, w1 = desc.describe_padded(pads[1], g, words_prev=pads[0], prev_tiles=d0)
        d2, w2 = desc.describe_padded(pads[2], g, words_prev=pads[1], prev_tiles=d1)

        # Verify distinct eager xh64 hashes
        hashes = [d0["xh64"][0], d1["xh64"][0], d2["xh64"][0]]
        self.assertEqual(len(set(hashes)), 3)

        cf0 = change.sig_conf(lumas[0], g.s, g.gw, g.gh, d0["morton"], 24)
        cf1 = change.sig_conf(lumas[1], g.s, g.gw, g.gh, d1["morton"], 24)
        cf2 = change.sig_conf(lumas[2], g.s, g.gw, g.gh, d2["morton"], 24)

        r1 = change.residual(d2, d1, mask=cf2 | cf1)[0]
        r2 = change.residual(d2, d0, mask=cf2 | cf0)[0]

        # Verify residual inequalities
        self.assertGreater(r1, self.t_enter)
        self.assertLessEqual(r2, self.t_exit)

    def test_exact_byte_return_control_retains_flicker(self):
        """Paired exact-byte-return control (A-B-A with exact byte identity) retains FLICKER."""
        g = grid.Grid(16, 16)
        f0 = noise_tile(16, 16, seed=100)
        f1 = noise_tile(16, 16, seed=200)
        f2 = f0.copy()  # Exact byte return

        pads = [g.pad(f) for f in (f0, f1, f2)]
        lumas = [desc.luma_of_padded(p) for p in pads]

        d0, w0 = desc.describe_padded(pads[0], g)
        d1, w1 = desc.describe_padded(pads[1], g, words_prev=pads[0], prev_tiles=d0)
        d2, w2 = desc.describe_padded(pads[2], g, words_prev=pads[1], prev_tiles=d1)

        st = change.ClassState.init(d0)
        o0, st, _ = change.classify_first(st, d0, y_cur=lumas[0])
        o1, st, _ = change.classify_tiles(st, d1, w1, y_cur=lumas[1], grid=(16, 1, 1), params=P.PARAMS)
        o2, st, _ = change.classify_tiles(st, d2, w2, y_cur=lumas[2], grid=(16, 1, 1), params=P.PARAMS)

        self.assertEqual(o0["cls"][0], change.C_UNCHANGED)
        self.assertEqual(o1["cls"][0], change.C_CHANGED)
        self.assertEqual(o2["cls"][0], change.C_FLICKER)

    @unittest.expectedFailure
    def test_contract_lazy_xh64_history_provenance(self):
        # fx2-f6-s2-flicker-veto-and-fl-v3
        """Contract: In lazy mode, ClassState history (prev1, prev2) holds true tile content digests across frames."""
        g = self.grid
        f0 = np.full((32, 32, 3), 10, dtype=np.uint8)
        f1 = np.full((32, 32, 3), 80, dtype=np.uint8)
        f2 = np.full((32, 32, 3), 160, dtype=np.uint8)

        pads = [g.pad(f) for f in (f0, f1, f2)]

        # Independent integer content digests for each tile in each frame
        dig0 = [desc.xh64_of(f0[g.ty[m]*16:(g.ty[m]+1)*16, g.tx[m]*16:(g.tx[m]+1)*16]) for m in range(g.N)]
        dig1 = [desc.xh64_of(f1[g.ty[m]*16:(g.ty[m]+1)*16, g.tx[m]*16:(g.tx[m]+1)*16]) for m in range(g.N)]
        dig2 = [desc.xh64_of(f2[g.ty[m]*16:(g.ty[m]+1)*16, g.tx[m]*16:(g.tx[m]+1)*16]) for m in range(g.N)]

        # Frame 0
        y0 = desc.luma_of_padded(pads[0])
        t0, w0 = desc.describe_padded(pads[0], g, lazy_xh=True)
        st = change.ClassState.init(t0)
        o0, st, _ = change.classify_first(st, t0, y_cur=y0)
        o0_filled = o0.copy()
        desc.fill_xh64(o0_filled, pads[0], g, prev_xh64=None, hash_all=True)

        for m in range(g.N):
            self.assertEqual(st.prev1["xh64"][m], dig0[m])
            self.assertEqual(st.prev2["xh64"][m], dig0[m])

        # Frame 1
        y1 = desc.luma_of_padded(pads[1])
        t1, w1 = desc.describe_padded(pads[1], g, pads[0], o0_filled["xh64"], P.PARAMS, prev_tiles=t0, y_plane=y1, lazy_xh=True)
        o1, st, _ = change.classify_tiles(st, t1, w1, y_cur=y1, grid=(16, 2, 2), params=P.PARAMS)
        o1_filled = o1.copy()
        desc.fill_xh64(o1_filled, pads[1], g, prev_xh64=o0_filled["xh64"], hash_all=False)

        for m in range(g.N):
            self.assertEqual(st.prev1["xh64"][m], dig1[m])
            self.assertEqual(st.prev2["xh64"][m], dig0[m])

        # Frame 2
        y2 = desc.luma_of_padded(pads[2])
        t2, w2 = desc.describe_padded(pads[2], g, pads[1], o1_filled["xh64"], P.PARAMS, prev_tiles=t1, y_plane=y2, lazy_xh=True)
        o2, st, _ = change.classify_tiles(st, t2, w2, y_cur=y2, grid=(16, 2, 2), params=P.PARAMS)
        o2_filled = o2.copy()
        desc.fill_xh64(o2_filled, pads[2], g, prev_xh64=o1_filled["xh64"], hash_all=False)

        for m in range(g.N):
            self.assertEqual(st.prev1["xh64"][m], dig2[m])
            self.assertEqual(st.prev2["xh64"][m], dig1[m])

    @unittest.expectedFailure
    def test_contract_chroma_cancellation_nonreturning_redraw_changed(self):
        # fx2-f6-s2-flicker-veto-and-fl-v3
        """Contract: Nonreturning chroma-cancelled redraw sequence with distinct hashes must be classified as CHANGED, not FLICKER."""
        g = grid.Grid(16, 16)
        seeds = (10, 20, 30)
        frames = [noise_tile(16, 16, s) for s in seeds]
        pads = [g.pad(f) for f in frames]
        lumas = [desc.luma_of_padded(p) for p in pads]

        d0, w0 = desc.describe_padded(pads[0], g)
        d1, w1 = desc.describe_padded(pads[1], g, words_prev=pads[0], prev_tiles=d0)
        d2, w2 = desc.describe_padded(pads[2], g, words_prev=pads[1], prev_tiles=d1)

        st = change.ClassState.init(d0)
        o0, st, _ = change.classify_first(st, d0, y_cur=lumas[0])
        o1, st, _ = change.classify_tiles(st, d1, w1, y_cur=lumas[1], grid=(16, 1, 1), params=P.PARAMS)
        o2, st, _ = change.classify_tiles(st, d2, w2, y_cur=lumas[2], grid=(16, 1, 1), params=P.PARAMS)

        self.assertEqual(o0["cls"][0], change.C_UNCHANGED)
        self.assertEqual(o1["cls"][0], change.C_CHANGED)
        # Nonreturning redraw sequence: d2 differs in content digest from d0, so frame 2 must be CHANGED (2), not FLICKER (4)
        self.assertEqual(o2["cls"][0], change.C_CHANGED)


if __name__ == "__main__":
    unittest.main()
