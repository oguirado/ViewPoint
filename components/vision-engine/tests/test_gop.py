"""S5 GOP tests (SPEC_TIME §2, §10): token codec bound, GOP roundtrip, cuts, tracks, cycles, header negatives."""
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from visioncore import canon, gop, layout
from visioncore.desc import TILE_DTYPE
from visioncore.errors import VqaError
from visioncore.grid import Grid

G = Grid(256, 160)  # 16 x 10 cells, s16


def tiles(n, seed=0):
    r = np.random.RandomState(seed)
    t = np.zeros(n, dtype=TILE_DTYPE)
    t["morton"] = G.mortons[:n] if n == G.N else np.arange(n)
    t["y_q4"] = r.randint(0, 4000, n)
    t["sig64"] = r.randint(0, 2 ** 62, n, dtype=np.int64).astype(np.uint64)
    t["xh64"] = r.randint(0, 2 ** 62, n, dtype=np.int64).astype(np.uint64)
    return t


def base():
    return tiles(G.N, 1)


def step(prev, dirty_idx, seed, cls=2, jitter_idx=()):
    t = prev.copy()
    r = np.random.RandomState(seed)
    for i in dirty_idx:
        t["cls"][i] = cls
        t["y_q4"][i] = r.randint(0, 4000)
        t["xh64"][i] = r.randint(0, 2 ** 62)
    for i in jitter_idx:
        t["cls"][i] = 1
        t["y_q4"][i] = (int(t["y_q4"][i]) + 1) & 0xFFFF
    return t


def raw(t):
    return np.ascontiguousarray(t).tobytes()


def expected_committed(seq, plan):
    """Independent model of the decoder output: per-GOP ref table updated by dirty tiles only."""
    out = []
    for pl in plan:
        ref = np.ascontiguousarray(seq[pl["start"]]).copy()
        out.append([raw(ref)])
        for i in range(pl["start"] + 1, pl["end"]):
            d = np.ascontiguousarray(seq[i])["cls"] >= 2
            ref[d] = np.ascontiguousarray(seq[i])[d]
            out[-1].append(raw(ref))
    return out


def run_seq(nf=25, seed=3):
    r = np.random.RandomState(seed)
    seq = [base()]
    for i in range(1, nf):
        k = int(r.randint(0, 30))
        d = sorted(r.choice(G.N, k, replace=False).tolist())
        j = sorted(r.choice(G.N, 10, replace=False).tolist())
        seq.append(step(seq[-1], d, 100 + i, jitter_idx=[x for x in j if x not in d]))
        seq[-1]["cls"][[x for x in range(G.N) if x not in d and x not in j]] = 0
    return seq


class TestRle(unittest.TestCase):
    def test_bound_tight(self):
        a = gop.rle_encode(bytes([1, 2, 3, 4]))
        self.assertEqual(len(a), 6)
        b = gop.rle_encode(bytes([7]) * 512)
        self.assertEqual(len(b), 518)
        self.assertEqual(len(b), gop.rle_bound(512))

    def test_alternating_zero_bytes(self):
        d = bytes([7, 0]) * 4096
        e = gop.rle_encode(d)
        self.assertLessEqual(len(e), 8258)
        self.assertEqual(gop.rle_decode(e, len(d)), d)

    def test_random_roundtrip_and_bound(self):
        r = np.random.RandomState(5)
        for i in range(2000):
            n = int(r.randint(0, 700))
            dens = [0.0, 0.05, 0.3, 0.7, 1.0][i % 5]
            d = bytes((r.randint(1, 256, n) * (r.rand(n) < dens)).astype(np.uint8).tolist())
            e = gop.rle_encode(d)
            self.assertLessEqual(len(e), gop.rle_bound(n))
            self.assertEqual(gop.rle_decode(e, n), d)

    def test_long_zero_runs(self):
        for n in (255, 256, 300, 5000):
            for tail in (b"", b"\x01", b"\x00\x00\x01"):
                d = bytes(n) + tail
                self.assertEqual(gop.rle_decode(gop.rle_encode(d), len(d)), d)

    def test_reject(self):
        good = gop.rle_encode(bytes([1, 2, 3, 0, 0, 0, 0, 5]))
        n = 8
        for bad, code in ((b"\x00", "E_DELTA_TOKEN"), (b"\x00\x05\x01", "E_DELTA_TOKEN"),
                          (good[:-1], "E_DELTA_TOKEN")):
            with self.assertRaises(VqaError) as c:
                gop.rle_decode(bad, n)
            self.assertEqual(c.exception.code, code)
        with self.assertRaises(VqaError) as c:
            gop.rle_decode(good, n + 1)
        self.assertEqual(c.exception.code, "E_DELTA_LEN")
        with self.assertRaises(VqaError) as c:
            gop.rle_decode(good, 2)
        self.assertIn(c.exception.code, ("E_DELTA_LEN", "E_DELTA_BOUND"))
        with self.assertRaises(VqaError) as c:
            gop.rle_decode(bytes(gop.rle_bound(4) + 1), 4)
        self.assertEqual(c.exception.code, "E_DELTA_BOUND")
        self.assertEqual(c.exception.rc, 2)


class TestMutants(unittest.TestCase):
    def test_m_rle_proto_violates_bound(self):
        """M-RLE-PROTO: the v12d [0x00,count]+literals prototype blows the bound; the bound test would kill it."""
        d = bytes([7, 0]) * 4096
        out = bytearray()
        i = 0
        while i < len(d):
            if d[i] == 0:
                j = i
                while j < len(d) and d[j] == 0 and j - i < 255:
                    j += 1
                out += bytes([0, j - i])
                i = j
            else:
                out.append(d[i])
                i += 1
        self.assertGreater(len(out), gop.rle_bound(len(d)))
        self.assertLessEqual(len(gop.rle_encode(d)), gop.rle_bound(len(d)))

    def test_m_cut_1frame_killed(self):
        """M-CUT-1FRAME: a single soft threshold would cut on a lone 600000 ppm burst."""
        a = [1000] * 10
        a[4] = 600000
        self.assertTrue(any(v >= 500000 for v in a))
        self.assertFalse(any(gop.cut_flags(a)))

    def test_m_xor_prev_killed(self):
        """M-XOR-PREV: XOR vs the previous frame would make a tile dirty once then JITTER cost bytes; here 0."""
        t0 = base()
        t1 = step(t0, [7], 9)
        t1["cls"][:] = 0
        t1["cls"][7] = 2
        t2 = t1.copy()
        t2["cls"][:] = 0
        t2["cls"][7] = 1
        t2["y_q4"][7] += 1
        g = gop.encode_run([t0, t1, t2], G)[0]
        _, fr = gop.decode_gop(g.blob)
        self.assertEqual(fr[2][1], fr[1][1])


class TestGop(unittest.TestCase):
    def test_roundtrip(self):
        seq = run_seq(25)
        gops = gop.encode_run(seq, G)
        ppm = [gop.dirty_ppm_of(int((s["cls"] >= 2).sum()), G.N) for s in seq]
        plan = gop.plan_gops(ppm)
        exp = expected_committed(seq, plan)
        self.assertEqual(len(gops), len(plan))
        for g, pl, e in zip(gops, plan, exp):
            h, frames = gop.decode_gop(g.blob)
            self.assertEqual((h["gw"], h["gh"], h["tile_px"], h["f0"], h["n_frames"]),
                             (G.gw, G.gh, G.s, pl["start"], pl["end"] - pl["start"]))
            self.assertEqual([f for f, _ in frames], list(range(pl["start"], pl["end"])))
            self.assertEqual([b for _, b in frames], e)

    def test_k_boundary(self):
        gops = gop.encode_run(run_seq(25), G)
        self.assertEqual([g.n_frames for g in gops], [20, 5])
        self.assertEqual(gops[-1].flags & gop.F_RUN_END, gop.F_RUN_END)

    def test_determinism(self):
        seq = run_seq(22)
        a = [g.blob for g in gop.encode_run(seq, G)]
        b = [g.blob for g in gop.encode_run(seq, G)]
        self.assertEqual(a, b)
        shas = set()
        for _ in range(3):
            with tempfile.TemporaryDirectory() as d:
                gop.write_gops(d, gop.encode_run(seq, G), G.ident, "0" * 64)
                shas.add((hashlib.sha256(Path(d, "gops.bin").read_bytes()).hexdigest(),
                          hashlib.sha256(Path(d, "gops.jsonl").read_bytes()).hexdigest()))
        self.assertEqual(len(shas), 1)

    def test_jitter_zero(self):
        t0 = base()
        seq = [t0, step(t0, [7], 9)]
        for i in range(19 - 1):
            seq.append(step(seq[-1], [], 50 + i, jitter_idx=[7]))
        seq[1]["cls"][:] = 0
        seq[1]["cls"][7] = 2
        for s in seq[2:]:
            s["cls"][:] = 0
            s["cls"][7] = 1
        g = gop.encode_run(seq, G)[0]
        body = g.blob[gop.HDR_SIZE + struct.unpack_from("<I", g.blob, 24)[0]:]
        n_mb = len(gop._cells(G.gw, G.gh)[3])
        pos, dl = 0, []
        for _ in range(len(seq) - 1):
            _, nd, _, _, _, _, dlen, _ = struct.unpack_from(gop.PHDR_FMT, body, pos)
            dl.append((nd, dlen))
            pos += gop.PHDR_SIZE + 8 * n_mb + dlen
        self.assertGreater(dl[0][0], 0)
        self.assertTrue(all(x == (0, 0) for x in dl[1:]), dl)
        _, fr = gop.decode_gop(g.blob)
        self.assertEqual(fr[-1][1], raw(seq[1]))

    def test_header_negatives(self):
        seq = run_seq(6)
        blob = gop.encode_run(seq, G)[0].blob
        cases = {"E_GOP_MAGIC": (0, b"XGOP"), "E_GOP_VERSION": (4, struct.pack("<H", 2)),
                 "E_GOP_GRID": (8, struct.pack("<H", 256)), "E_GOP_PAD": (22, struct.pack("<H", 8))}
        for code, (off, val) in cases.items():
            bad = bytearray(blob)
            bad[off:off + len(val)] = val
            with self.assertRaises(VqaError, msg=code) as c:
                gop.decode_gop(bytes(bad))
            self.assertEqual(c.exception.code, code)
            self.assertEqual(c.exception.rc, 2)
        # non-zero PHdr pad
        h = gop.parse_header(blob)
        body_off = gop.HDR_SIZE + h["izlen"]
        bad = bytearray(blob)
        bad[body_off + 6] = 1
        # body sha8 covers the pad byte -> integrity fires first unless header sha is also patched
        bad[40:48] = struct.pack("<Q", gop._sha8(bytes(bad[body_off:])))
        with self.assertRaises(VqaError) as c:
            gop.decode_gop(bytes(bad))
        self.assertEqual(c.exception.code, "E_GOP_PAD")

    def test_integrity_rc5(self):
        blob = bytearray(gop.encode_run(run_seq(6), G)[0].blob)
        blob[-1] ^= 1
        with self.assertRaises(VqaError) as c:
            gop.decode_gop(bytes(blob))
        self.assertEqual((c.exception.code, c.exception.rc), ("E_GATE", 5))

    def test_blob_sha_ignores_zlib(self):
        g = gop.encode_run(run_seq(6), G)[0]
        self.assertEqual(len(g.blob_sha256), 64)
        self.assertEqual(g.blob_sha256, hashlib.sha256(g.blob).hexdigest())  # stored bytes (FC-T5)
        self.assertNotEqual(g.canon_sha256, g.blob_sha256)
        rec = gop.gop_record(g, "grid:x", "ab" * 32, 0)
        self.assertEqual((rec["gop_blob_sha256"], rec["gop_canon_sha256"]), (g.blob_sha256, g.canon_sha256))

    def test_two_gop_hashes(self):
        import zlib
        a = gop.encode_run(run_seq(6), G)[0]
        real = zlib.compress
        try:
            gop.zlib.compress = lambda d, level=6, *x: real(d, 9)
            b = gop.encode_run(run_seq(6), G)[0]
        finally:
            gop.zlib.compress = real
        self.assertEqual(a.canon_sha256, b.canon_sha256)
        self.assertEqual(b.blob_sha256, hashlib.sha256(b.blob).hexdigest())

    def test_worst_case_bytes(self):
        self.assertEqual(gop.max_gop_bytes(4096, 20, 64, 64), 2651295)
        self.assertEqual(gop.rle_bound(131072), 132102)
        self.assertEqual(len(gop._cells(80, 45)[3]), 60)
        self.assertEqual(len(gop._cells(60, 34)[3]), 40)

    def test_worst_case_actual(self):
        r = np.random.RandomState(8)
        seq = [base()]
        for i in range(19):
            t = seq[-1].copy()
            t["cls"][:] = 2
            t["y_q4"] = r.randint(0, 65536, G.N)
            t["sig64"] = r.randint(1, 2 ** 62, G.N, dtype=np.int64).astype(np.uint64)
            t["xh64"] = r.randint(1, 2 ** 62, G.N, dtype=np.int64).astype(np.uint64)
            seq.append(t)
        seq[0]["cls"][:] = 2
        # 100 % dirty from frame 1 -> every frame is a cut, so also test the bound on a single GOP
        pm = {"CUT_PPM_1F": 1000001, "CUT_PPM_2F": 1000001}
        from visioncore import params
        p = dict(params.PARAMS)
        p.update(pm)
        gops = gop.encode_run(seq, G, params=p)
        self.assertEqual(len(gops), 1)
        self.assertLessEqual(len(gops[0].blob), gop.max_gop_bytes(G.N, 20, G.gw, G.gh))

    def test_global_rows(self):
        seq = run_seq(5)
        gm = [None, (3, -2), None, (0, 0), (-4, 0)]
        g = gop.encode_run(seq, G, global_motion=gm)[0]
        self.assertEqual([r[:3] for r in g.glob], [[1, 3, -2], [4, -4, 0]])


class TestCuts(unittest.TestCase):
    def ppm(self, n, over):
        a = [1000] * n
        for k, v in over.items():
            a[k] = v
        return a

    def test_black_frame_cuts(self):
        for t in (5, 57, 119):
            a = self.ppm(130, {t: 990000})
            c = gop.cut_flags(a)
            self.assertTrue(c[t])
        a = self.ppm(130, {5: 990000, 6: 990000})
        pl = gop.plan_gops(a)
        starts = [p["start"] for p in pl]
        self.assertIn(5, starts)
        self.assertIn(6, starts)
        self.assertTrue(next(p for p in pl if p["start"] == 5)["flash"])
        self.assertFalse(next(p for p in pl if p["start"] == 6)["flash"])

    def test_single_burst_no_cut(self):
        self.assertFalse(any(gop.cut_flags(self.ppm(10, {4: 600000}))))

    def test_two_soft_frames_cut(self):
        c = gop.cut_flags(self.ppm(10, {4: 500000, 5: 500000}))
        self.assertTrue(c[4])
        self.assertFalse(c[0])

    def test_k_plan(self):
        pl = gop.plan_gops([0] * 45)
        self.assertEqual([(p["start"], p["end"]) for p in pl], [(0, 20), (20, 40), (40, 45)])


def reg(f, m, cls="CHANGED", spans=None, n=1, mv=(0, 0), r_sum=10):
    h = "%04x" % m
    return {"id": "r%d:%s" % (f, h), "cls": cls, "n_tiles": n, "mv": list(mv), "r_sum": r_sum,
            "spans": spans or [[h, h]]}


def lk(f, pairs, split=(), merge=()):
    return {"frame": f, "links": [[q, r, 1000000] for q, r in pairs], "split": list(split), "merge": list(merge)}


class TestTracks(unittest.TestCase):
    def seq3(self):
        return [base() for _ in range(3)], [10, 11, 12]

    def test_move_one_track(self):
        tl, fr = self.seq3()
        regs = [[reg(10, 0)], [reg(11, 1, "MOVED", mv=(16, 0))], [reg(12, 2, "MOVED", mv=(16, 0))]]
        links = [None, lk(11, [("r10:0000", "r11:0001")]), lk(12, [("r11:0001", "r12:0002")])]
        tracks, ev = gop.build_tracks(fr, tl, regs, links, G)
        self.assertEqual(len(tracks), 1)
        out = gop._track_out(tracks[0])
        self.assertEqual((out["k"], out["first"], out["last"]), ("k10:0000", 10, 12))
        self.assertEqual(out["flags"], [])
        self.assertEqual(out["mv_mode"], [16, 0])
        self.assertEqual(ev, [])

    def test_teleport(self):
        tl, fr = self.seq3()
        regs = [[reg(10, 0)], [reg(11, 5)], []]
        links = [None, lk(11, []), lk(12, [])]
        tracks, ev = gop.build_tracks(fr, tl, regs, links, G)
        by = {t["k"]: t for t in tracks}
        self.assertIn("VANISH", by["k10:0000"]["flags"])
        self.assertIn("APPEAR", by["k11:0005"]["flags"])
        names = sorted((e[2], e[3]) for e in ev)
        self.assertEqual(names[:2], [("APPEAR", "k11:0005"), ("VANISH", "k10:0000")][:2])
        self.assertTrue(all(e[0] == 11 for e in ev[:2]))

    def test_no_links_no_appear(self):
        tl, fr = self.seq3()
        tracks, ev = gop.build_tracks(fr, tl, [[reg(10, 0)], [reg(11, 1)], []], None, G)
        self.assertTrue(all(not t["flags"] for t in tracks))

    def test_split_merge(self):
        tl, fr = self.seq3()
        regs = [[reg(10, 0)], [reg(11, 1), reg(11, 2)], []]
        links = [None, lk(11, [("r10:0000", "r11:0001")], split=[["r10:0000", "r11:0002"]]), lk(12, [])]
        tracks, _ = gop.build_tracks(fr, tl, regs, links, G)
        self.assertIn("SPLIT", {t["k"]: t for t in tracks}["k10:0000"]["flags"])

    def test_frozen_drift_flicker_flags(self):
        tl, fr = self.seq3()
        tl[1]["cls"][3] = 5
        tl[2]["cls"][8] = 6
        tl[0]["cls"][12] = 1
        regs = [[reg(10, 0), reg(10, 12)], [reg(11, 3, "FROZEN")], [reg(12, 8, "DRIFT")]]
        tracks, _ = gop.build_tracks(fr, tl, regs, None, G)
        by = {t["k"]: gop._track_out(t) for t in tracks}
        self.assertIn("FROZEN", by["k11:0003"]["flags"])
        self.assertIn("DRIFT", by["k12:0008"]["flags"])
        self.assertEqual(by["k10:000c"]["flags"], [])  # JITTER only -> no flag
        self.assertEqual(by["k10:0000"]["flags"], [])

    def test_frozen_ppm_threshold(self):
        tl, fr = self.seq3()
        tl[1]["cls"][0] = 5  # 1 of 3 tiles frozen < 500000 ppm
        r = {"id": "r11:0000", "cls": "FROZEN", "n_tiles": 3, "mv": [0, 0], "r_sum": 1,
             "spans": [["0000", "0002"]]}
        tracks, _ = gop.build_tracks(fr, tl, [[], [r], []], None, G)
        self.assertEqual(tracks[0]["flags"], set())
        tl[1]["cls"][1] = 5
        tracks, _ = gop.build_tracks(fr, tl, [[], [r], []], None, G)
        self.assertIn("FROZEN", tracks[0]["flags"])


class TestCycles(unittest.TestCase):
    def blink(self, period, n=12, tile=4):
        seq = [base() for _ in range(n)]
        for i, s in enumerate(seq):
            s["cls"][:] = 0
            s["xh64"][:] = 7
            s["cls"][tile] = 4
            s["xh64"][tile] = 100 + (i % period)
        return seq

    def test_p2_p3(self):
        for per in (2, 3, 4):
            seq = self.blink(per, 14)
            c = gop.find_cycles(list(range(14)), seq, G)
            self.assertEqual([(x["p"]) for x in c], [per])
            self.assertGreaterEqual(c[0]["n"], 3)
            self.assertTrue(c[0]["t"].startswith("f0:t"))

    def test_neg(self):
        seq = [base() for _ in range(10)]
        for s in seq:
            s["cls"][:] = 0
            s["xh64"][:] = 5
        self.assertEqual(gop.find_cycles(list(range(10)), seq, G), [])
        seq[5]["cls"][2] = 2
        for s in seq[5:]:
            s["xh64"][2] = 9
        self.assertEqual(gop.find_cycles(list(range(10)), seq, G), [])

    def test_track_cycle_flag(self):
        seq = self.blink(2, 12, tile=int(G.order[0, 4]))
        m = int(G.mortons[int(G.order[0, 4])])
        regs = [[reg(i, m, "FLICKER")] for i in range(12)]
        links = [None] + [lk(i, [("r%d:%04x" % (i - 1, m), "r%d:%04x" % (i, m))]) for i in range(1, 12)]
        g = gop.encode_run(seq, G, regions=regs, links=links)[0]
        t = g.tracks[0]
        self.assertEqual(t["period"], 2)
        self.assertIn("CYCLE", t["flags"])
        self.assertIn("FLICKER", t["flags"])
        self.assertEqual(g.cycles[0]["p"], 2)


class TestOutputs(unittest.TestCase):
    def test_record_and_offsets(self):
        seq = run_seq(25)
        gops = gop.encode_run(seq, G)
        with tempfile.TemporaryDirectory() as d:
            lines = gop.write_gops(d, gops, G.ident, "a" * 64)
            data = Path(d, "gops.bin").read_bytes()
            recs = [json.loads(x) for x in Path(d, "gops.jsonl").read_text().splitlines()]
            for x in Path(d, "gops.jsonl").read_text().splitlines(True):
                self.assertTrue(canon.is_canonical(x))
            spans = gop.split_blobs(data)
            self.assertEqual([(r["offset"], r["length"]) for r in recs], spans)
            self.assertEqual(recs[0]["schema"], "vqa.gop/v1")
            self.assertEqual(recs[0]["grid"], G.ident)
            self.assertEqual(recs[0]["gop"], 0)
            self.assertEqual(len(recs[0]["dirty_ppm"]), recs[0]["n_frames"])
            self.assertEqual(recs[0]["key_sha256"], gops[0].key_sha256)

    def test_events_flash(self):
        seq = run_seq(8)
        for i in (3, 4):
            seq[i] = seq[i].copy()
            seq[i]["cls"][:] = 2
        g = gop.encode_run(seq, G)
        evs = [e for x in g for e in x.events]
        self.assertIn(["FLASH", "f3:all"], evs)
        self.assertIn(["CUT", "f3:all"], evs)
        self.assertIn(["CUT", "f4:all"], evs)

    def test_bad_input(self):
        with self.assertRaises(VqaError):
            gop.encode_run([], G)
        with self.assertRaises(VqaError):
            gop.encode_run([base(), base()], G, frame_nums=[3, 3])


if __name__ == "__main__":
    unittest.main()


class TestCycleJitterMask(unittest.TestCase):
    def _tiles(self, hashes, cls):
        out = []
        for h, c in zip(hashes, cls):
            t = np.zeros(1, dtype=TILE_DTYPE)
            t["xh64"], t["cls"] = h, c
            out.append(t)
        return out

    class _G:
        gw = gh = 1

    def test_cycle_jitter_mask(self):
        grid = self._G()
        frames = list(range(8))
        ab = self._tiles([1, 2] * 4, [2] * 8)
        self.assertEqual([c["p"] for c in gop.find_cycles(frames, ab, grid)], [2])
        # copied hashes on JITTER frames never open or extend a cycle
        aj = self._tiles([1, 2] * 4, [2, 1] * 4)
        self.assertEqual(gop.find_cycles(frames, aj, grid), [])
