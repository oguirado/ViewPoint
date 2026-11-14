"""S6 pack tests (SPEC_TIME §3, §10: T-S6-*)."""
import os
import random
import shutil
import tempfile
import unittest
import zlib

import numpy as np

from visioncore import canon, ids, layout, pack as P
from visioncore.desc import TILE_DTYPE
from visioncore.errors import VqaError

W, H = 1280, 720
S, GW, GH = layout.grid_params(W, H)
N = GW * GH
PSHA = "0" * 64


def mk_tiles(cells):
    """cells: {(tx,ty): (cls, mvx, mvy)} -> TILE_DTYPE[N] ascending Morton."""
    ms = sorted(layout.morton(x, y) for y in range(GH) for x in range(GW))
    t = np.zeros(N, dtype=TILE_DTYPE)
    t["morton"] = ms
    pos = {m: i for i, m in enumerate(ms)}
    for (x, y), (c, dx, dy) in cells.items():
        i = pos[layout.morton(x, y)]
        t["cls"][i], t["mv_dx"][i], t["mv_dy"][i] = c, dx, dy
    return t


def track(first, last, mm, flags, mask, rrep_frame=None, mv=(0, 0), period=0):
    return {"k": ids.track_id(first, mm), "first": first, "last": last, "cls_mask": mask, "mv_mode": list(mv),
            "period": period, "flags": flags, "n_tiles_max": 3, "r_rep": ids.region_id(rrep_frame or first, mm)}


def region(frame, mm, bbox_t):
    tx0, ty0, tx1, ty1 = bbox_t
    return {"id": ids.region_id(frame, mm), "frame": frame, "bbox_t": list(bbox_t),
            "bbox_px": [tx0 * S, ty0 * S, (tx1 - tx0 + 1) * S, (ty1 - ty0 + 1) * S]}


def gop(g, f0, tracks=(), events=(), cut=False, glob=(), cycles=(), n=20):
    return {"schema": "vqa.gop/v1", "g": g, "f0": f0, "n_frames": n, "cut": cut, "tracks": list(tracks),
            "events": list(events), "cycles": list(cycles), "global": list(glob),
            "dirty_ppm": [(f0 + i) * 1000 % 400000 for i in range(n)]}


def fixture(ntracks=6):
    regs, gops = {}, []
    for g in range(3):
        trs = []
        for j in range(ntracks):
            # Distinct spatial tracks share an onset: exercise fitting without
            # exceeding the mandatory media capacity (SPEC_TIME §3.2 / JC-5).
            f = g * 20 + 2
            mm = layout.morton(3 * j + 1, g + 1)
            flags = [["APPEAR"], ["FROZEN"], ["FLICKER"], ["DRIFT"], [], ["VANISH"]][j % 6]
            mask = [4, 32, 16, 64, 8, 4][j % 6]
            trs.append(track(f, f + 3, mm, flags, mask, mv=(2, 0) if mask == 8 else (0, 0)))
            r = region(f, mm, (3 * j + 1, g + 1, 3 * j + 2, g + 2))
            regs[r["id"]] = r
        gops.append(gop(g, g * 20, trs, events=[["FLASH", ids.global_id(g * 20 + 7)]] if g == 1 else []))
    return gops, regs


def batch(gops, regs, **kw):
    b = {"W": W, "H": H, "params_sha256": PSHA, "gops": gops, "regions": regs}
    b.update(kw)
    return b


def tiles_fn(f):
    return mk_tiles({(5, 5): (2, 0, 0), (10, 10): (3, 4, 0), (20, 3): (4, 0, 0), (30, 30): (5, 0, 0),
                     (40, 20): (6, 0, 0)} if f % 2 == 0 else {(5, 5): (2, 0, 0)})


def frames_fn(f):
    rng = np.random.RandomState(f)
    return rng.randint(0, 256, size=(H, W, 3)).astype(np.uint8)


class PngTest(unittest.TestCase):
    def test_png_roundtrip(self):
        a = np.random.RandomState(1).randint(0, 256, size=(9, 13, 3)).astype(np.uint8)
        b = P.png_encode(a)
        self.assertEqual(b[:8], b"\x89PNG\r\n\x1a\n")
        i = b.index(b"IDAT")
        n = int.from_bytes(b[i - 4:i], "big")
        raw = zlib.decompress(b[i + 4:i + 4 + n])
        self.assertEqual(len(raw), 9 * (1 + 13 * 3))
        got = np.frombuffer(raw, np.uint8).reshape(9, 40)[:, 1:].reshape(9, 13, 3)
        self.assertTrue((got == a).all())
        self.assertEqual(P.png_encode(a), b)
        self.assertNotIn(b"tEXt", b)
        self.assertNotIn(b"tIME", b)

    def test_png_neg(self):
        with self.assertRaises(VqaError):
            P.png_encode(np.zeros((4, 4), np.uint8))


class HeatmapTest(unittest.TestCase):
    def test_size_720p_and_1080p(self):
        self.assertEqual(P.cell_px(80), 9)
        self.assertEqual(P.cell_px(60), 12)

    def test_no_magenta_and_deterministic(self):
        tf = [tiles_fn(f) for f in range(20)]
        png = P.heatmap_png(W, H, tf)
        self.assertEqual(png, P.heatmap_png(W, H, tf))
        i = png.index(b"IHDR")
        w, h = int.from_bytes(png[i + 4:i + 8], "big"), int.from_bytes(png[i + 8:i + 12], "big")
        self.assertEqual((w, h), (720, 405))
        j = png.index(b"IDAT")
        n = int.from_bytes(png[j - 4:j], "big")
        raw = np.frombuffer(zlib.decompress(png[j + 4:j + 4 + n]), np.uint8).reshape(h, 1 + w * 3)[:, 1:].reshape(h, w, 3)
        self.assertFalse(P.has_forbidden_colour(raw))
        # cell (5,5) active in all 20 frames CHANGED -> full colour
        self.assertEqual(tuple(raw[5 * 9 + 4, 5 * 9 + 4]), (230, 25, 25))
        # quiet cell is background
        self.assertEqual(tuple(raw[1 * 9 + 4, 1 * 9 + 4]), (32, 32, 32))

    def test_forbidden_detector(self):
        a = np.zeros((2, 2, 3), np.uint8)
        a[0, 0] = (255, 0, 255)
        self.assertTrue(P.has_forbidden_colour(a))
        a[0, 0] = (255, 140, 0)
        self.assertFalse(P.has_forbidden_colour(a))

    def test_bad_cls_rejected(self):
        t = mk_tiles({(1, 1): (7, 0, 0)})
        with self.assertRaises(VqaError) as c:
            P.heatmap_png(W, H, [t])
        self.assertEqual(c.exception.code, "E_SCHEMA")


class CropTest(unittest.TestCase):
    def test_downscale(self):
        rgb = frames_fn(1)
        png, box, f = P.crop_png(rgb, (2, 2, 30, 30), S)
        self.assertGreater(f, 1)
        i = png.index(b"IHDR")
        w = int.from_bytes(png[i + 4:i + 8], "big")
        self.assertLessEqual(w, 256)

    def test_small_crop_lossless(self):
        rgb = frames_fn(2)
        png, box, f = P.crop_png(rgb, (5, 5, 5, 5), S)
        self.assertEqual((f, box), (1, [64, 64, 48, 48]))

    def test_clamp(self):
        rgb = frames_fn(2)
        _, box, _ = P.crop_png(rgb, (0, 0, 0, 0), S)
        self.assertEqual(box[:2], [0, 0])


class SelectTest(unittest.TestCase):
    def test_must_keep_and_cap(self):
        gops, _ = fixture()
        sel, fm, must = P.select_frames(gops)
        self.assertLessEqual(len(sel), 12)
        self.assertEqual(sel, sorted(sel))
        self.assertIn(27, sel)  # FLASH at g1 f0+7
        self.assertEqual(len(sel), 12)
        self.assertEqual(fm, [[i, f] for i, f in enumerate(sel)])

    def test_black_frame_slot(self):
        for t in (5, 57, 119):
            g = [gop(t // 20, (t // 20) * 20, events=[["FLASH", ids.global_id(t)]])]
            self.assertIn(t, P.select_frames(g)[0])

    def test_priority_when_overfull(self):
        evs = [["FLASH", ids.global_id(i)] for i in range(0, 20)]
        g = [gop(0, 0, events=evs)]
        with self.assertRaises(VqaError) as c:
            P.select_frames(g, 12)
        self.assertEqual(c.exception.rc, 6)
        self.assertEqual(c.exception.code, "E_BUDGET")

    def test_cycle_first_frame(self):
        g = [gop(0, 0, cycles=[{"t": ids.field_id(9, 3), "p": 2, "n": 4, "end": 15}])]
        self.assertIn(9, P.select_frames(g)[0])


class PackTest(unittest.TestCase):
    def test_pack_basic_and_caps(self):
        gops, regs = fixture()
        r = P.pack(batch(gops, regs, tiles=tiles_fn, frames=frames_fn))
        pk = r.pack
        self.assertEqual(pk["schema"], "vqa.pack/v2")
        self.assertLessEqual(pk["v3_bytes"], 8000)
        for b in pk["v3"]["g"]:
            self.assertLessEqual(len(canon.dumps(b)) - 1, 2000)
        self.assertLessEqual(len(pk["v1"]), 4)
        self.assertLessEqual(len(pk["v2"]) * 2, 12)
        self.assertLessEqual(len(pk["frame_map"]), 12)
        self.assertEqual(pk["z1_sha256"], canon.sha256_bytes(r.files["z1.txt"]))
        self.assertTrue(canon.is_canonical(r.files["pack.json"].decode()))
        for v in pk["v1"]:
            self.assertEqual(canon.sha256_bytes(r.files[v["file"]]), v["sha256"])
        self.assertLessEqual(pk["est_tok"], 15500)

    def test_fit_cap_per_gop_all_budgets(self):
        gops, regs = fixture(ntracks=18)
        for cap in (400, 800, 1500, 3000, 8000):
            try:
                r = P.pack(batch(gops, regs), cap_bytes=cap)
            except VqaError as e:
                self.assertEqual(e.rc, 6)
                continue
            self.assertLessEqual(r.pack["v3_bytes"], cap)
            for b in r.pack["v3"]["g"]:
                self.assertLessEqual(len(canon.dumps(b)) - 1, 2000)

    def test_fit_order_independent(self):
        gops, regs = fixture(ntracks=12)
        ref = P.pack(batch(gops, regs), cap_bytes=1800).files["pack.json"]
        rnd = random.Random(7)
        for _ in range(100):
            g2 = [dict(g, tracks=rnd.sample(g["tracks"], len(g["tracks"])), events=list(g["events"])) for g in gops]
            rnd.shuffle(g2)
            self.assertEqual(P.pack(batch(g2, regs), cap_bytes=1800).files["pack.json"], ref)

    def test_promotion_uses_levels(self):
        gops, regs = fixture(ntracks=6)
        r = P.pack(batch(gops, regs))
        items = [i for b in r.pack["v3"]["g"] for i in b.get("i", [])]
        self.assertTrue(any(len(i) == 9 for i in items))  # L0 present when cap is ample

    def test_ebudget_cap_200_no_partial(self):
        gops, regs = fixture()
        d = tempfile.mkdtemp()
        try:
            guard = os.path.join(d, "guard")
            with open(guard, "wb") as fh:
                fh.write(b"G")
            out = os.path.join(d, "b0")
            before = sorted(os.listdir(d))
            with self.assertRaises(VqaError) as c:
                P.pack(batch(gops, regs, tiles=tiles_fn, frames=frames_fn), cap_bytes=100).write(out)
            self.assertEqual((c.exception.code, c.exception.rc), ("E_BUDGET", 6))
            self.assertEqual(sorted(os.listdir(d)), before)
            with open(guard, "rb") as fh:
                self.assertEqual(fh.read(), b"G")
        finally:
            shutil.rmtree(d)

    def test_write_atomic_and_refuse_existing(self):
        gops, regs = fixture()
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "b0")
            r = P.pack(batch(gops, regs, tiles=tiles_fn, frames=frames_fn))
            r.write(out)
            self.assertTrue(os.path.isfile(os.path.join(out, "pack.json")))
            self.assertFalse(os.path.exists(out + ".tmp"))
            pj = os.path.join(out, "pack.json")
            with open(pj, "rb") as fh:
                mark = fh.read()
            with self.assertRaises(VqaError) as c:
                r.write(out)
            self.assertEqual(c.exception.rc, 2)
            with open(pj, "rb") as fh:
                self.assertEqual(fh.read(), mark)
        finally:
            shutil.rmtree(d)

    def test_z1_stable_and_small(self):
        self.assertEqual(P.z1_text(), P.z1_text())
        self.assertLessEqual(len(P.z1_text()), 6000)
        self.assertNotRegex(P.z1_text().decode(), r"r_[0-9]{6}_|20[0-9]{2}-")
        g1, r1 = fixture(3)
        g2, r2 = fixture(9)
        self.assertEqual(P.pack(batch(g1, r1)).files["z1.txt"], P.pack(batch(g2, r2)).files["z1.txt"])

    def test_blind_and_norag(self):
        gops, regs = fixture()
        regs = {k: dict(v, adj=["x"]) for k, v in regs.items()}
        txt = canon.dumps(P.pack(batch(gops, regs)).pack["v3"])
        for w in ("PASS", "FAIL", "adj", "oracle"):
            self.assertNotIn(w, txt)

    def test_placebo(self):
        gops, regs = fixture()
        a = P.pack(batch(gops, regs, tiles=tiles_fn))
        b = P.pack(batch(gops, regs, tiles=tiles_fn), shuffle_window=0)
        self.assertNotEqual(a.pack["v3"], b.pack["v3"])
        self.assertEqual(a.pack["frame_map"], b.pack["frame_map"])
        self.assertTrue(b.pack["placebo"])
        self.assertNotIn("placebo", a.pack)
        self.assertNotIn("placebo", canon.dumps(b.pack["v3"]))
        self.assertNotIn(b"placebo", b.files["z1.txt"])
        one = P.pack(batch(gops[:1], regs), shuffle_window=1)  # 1-GOP batch works (FC-T4)
        self.assertEqual(one.pack["placebo"], {"sx": 2, "sy": 0})
        self.assertEqual(P.pack(batch(gops, regs), shuffle_window=0).pack["placebo"]["sx"], 1)  # never identity

    def test_placebo_same_frames_shifted_ids(self):
        gops, regs = fixture()
        a = P.pack(batch(gops, regs, tiles=tiles_fn))
        b = P.pack(batch(gops, regs, tiles=tiles_fn), shuffle_window=3)
        self.assertEqual((a.pack["frame_map"], a.pack["crop_frames"]), (b.pack["frame_map"], b.pack["crop_frames"]))
        sh = b.pack["placebo"]
        ta = {t["k"] for g in gops for t in g["tracks"]}
        _, gw, gh = layout.grid_params(W, H)
        want = set()
        for k in ta:
            d = ids.parse_any(k)[1]
            tx, ty = layout.unmorton(d["morton"])
            want.add(ids.track_id(d["frame"], layout.morton((tx + sh["sx"]) % gw, (ty + sh["sy"]) % gh)))
        printed = {it[0] for blk in b.pack["v3"]["g"] for it in blk.get("i", []) if it[0][0] == "k"}
        self.assertTrue(printed and printed <= want)
        self.assertFalse(printed & ta)
        self.assertNotEqual(a.files["heatmap_g0.png"], b.files["heatmap_g0.png"])

    def test_pack_crop_frames(self):
        gops, regs = fixture()
        r = P.pack(batch(gops, regs, frames=frames_fn))
        want = sorted({v["frame"] for rec in r.pack["v2"] for v in (rec["before"], rec["after"])})
        self.assertEqual(r.pack["crop_frames"], want)
        self.assertEqual(want, sorted(set(want)))
        self.assertTrue(len(want) >= 2)
        self.assertTrue(all(isinstance(x, str) for x in r.pack["gops"]))

    def test_est_tok_tiling(self):
        self.assertEqual([P.tok_image(*x) for x in ((1280, 720), (1920, 1080), (720, 405), (256, 256))],
                         [516, 1548, 258, 258])
        gops, regs = fixture()
        wh = [(1920, 1080)] * 8
        r = P.pack(batch(gops, regs, check_png_wh=wh))
        self.assertEqual(r.pack.get("check_png_max_side"), 768)  # ladder touched the check PNGs
        r2 = P.pack(batch(gops, regs, check_png_wh=[(640, 360)] * 2))
        self.assertNotIn("check_png_max_side", r2.pack)

    def test_starvation_accumulator(self):
        gops, regs = fixture(ntracks=12)
        r1 = P.pack(batch(gops, regs), cap_bytes=1500)
        low = [k for k, v in r1.acc.items() if v > 0]
        self.assertTrue(low)  # shown below L0 -> accumulates
        r2 = P.pack(batch(gops, regs, acc=r1.acc), cap_bytes=1500)
        self.assertNotEqual(r1.pack["v3"], r2.pack["v3"])

    def test_missing_region_fail_closed(self):
        gops, regs = fixture()
        with self.assertRaises(VqaError) as c:
            P.pack(batch(gops, {}))
        self.assertEqual(c.exception.rc, 2)

    def test_determinism(self):
        gops, regs = fixture()
        a = P.pack(batch(gops, regs, tiles=tiles_fn, frames=frames_fn)).files
        b = P.pack(batch(gops, regs, tiles=tiles_fn, frames=frames_fn)).files
        self.assertEqual(a, b)

    def test_tokens_hard_cap(self):
        self.assertEqual(P.est_tokens(4000, 3000, 2, 3, 400), 1000 + 1000 + 258 * 5 + 100)

    def test_ids_in_pack_parse(self):
        gops, regs = fixture()
        for b in P.pack(batch(gops, regs)).pack["v3"]["g"]:
            for it in b.get("i", []):
                ids.parse_any(it[0])


if __name__ == "__main__":
    unittest.main()
