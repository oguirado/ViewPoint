"""HBP-W2 lane N: book HB-VN-s0s1 (hb-hybrid/v1) and its Panel 3 = the dual_verify oracle (P3.v1).

Book checks (a); dual contract (b); reference identity (c); property legs on the Python reference (d); mutants killed by named tests (e).
No bare assert; no skip. Seeds come from SplitMix64 (the repo has no visioncore.rand: the generator is local, checked against a known vector).
"""
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

from visioncore import desc, frame as fr, grid as vgrid, layout
from visioncore.params import PARAMS
from vnative import backend, dual, run as vrun

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOK_DIR = os.path.join(ROOT, "hyperbooks", "HB-VN-s0s1")
BOOK = os.path.join(BOOK_DIR, "book.md")
M64 = (1 << 64) - 1
MUT_RC = (1, 5)

# ---- SplitMix64 -------------------------------------------------------------------------------------------------


def splitmix64(seed, n):
    out, x = [], seed & M64
    for _ in range(n):
        x = (x + 0x9E3779B97F4A7C15) & M64
        z = x
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & M64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M64
        out.append(z ^ (z >> 31))
    return out


def rand_u8(seed, shape):
    n = int(np.prod(shape))
    words = np.array(splitmix64(seed, (n + 7) // 8), dtype=np.uint64)
    return words.view(np.uint8)[:n].reshape(shape).copy()


# ---- book parsing -----------------------------------------------------------------------------------------------
PM_RE = re.compile(r"<!-- PM:BEGIN (pm\.[a-z_]+\.v\d+) -->\n```json\n(.*?)\n```\n<!-- PM:END \1 -->", re.S)
HB_RE = re.compile(r"<!-- HB:BEGIN (hb\.[a-z_]+\.v\d+) -->\n```json\n(.*?)\n```\n<!-- HB:END -->", re.S)
ANCHOR_RE = re.compile(r'<a id="(hb-s\d+)"></a>')
HDR_FIELDS = ("INPUTS", "PRECONDITIONS", "OUTPUTS", "TIME", "SPACE", "ARITHMETIC", "FAILURE")
UNIT_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?\s?(?:ms|us|µs|ns|KiB|MiB|GiB|bytes|B|%|fps|ppm)(?![\w])")
TAG_RE = re.compile(r"\[(?:MEASURED|CITED|TARGET)\b")
FORBIDDEN = ("ADV_RT1", "PLAN_mathcodec", "EX-MA02", "EX-MC01", "EX-OC03", "EX-OC05", "EX-ENG-KL", "EX-VG-0100-TIER",
             "EX-MC-0091-C2C", "EX-SW-0055-FENCE", "EX-SW-0128-RED", "EX-MA-211-SEQLOCK", "EX-MA-112-WORD64",
             "EX-OC-RUST09-FFI", "EX-OC-RUST04-RAYON", "EX-OC-ASM05-SVE2")
GOLD = {0: ("pm.node.v2", "pm.reading.v2"), 1: ("pm.claims.v2",), 2: ("pm.objects.v2", "pm.quantities.v2"), 3: ("pm.roadmap.v2",),
        4: ("pm.triptych.v2",), 5: ("pm.domains.v2",), 6: ("pm.limits.v2", "pm.search.v1"), 9: ("pm.edges.v2",)}
KERNEL_SYMBOLS = ("s0::pad_into", "s0::luma_of_padded", "s0::words_eq", "s1::compute_tiles", "isqrt::isqrt_u64")


def book_text():
    with open(BOOK, encoding="utf-8") as f:
        return f.read()


def section_slices(text):
    ms = list(ANCHOR_RE.finditer(text))
    out = {}
    for i, m in enumerate(ms):
        out.setdefault(m.group(1), text[m.start(): ms[i + 1].start() if i + 1 < len(ms) else len(text)])
    return out


class TestBookStructure(unittest.TestCase):
    def test_a_sections_gold_blocks_and_json(self):
        t = book_text()
        order = [m.group(1) for m in ANCHOR_RE.finditer(t)]
        top = [a for a in order]
        self.assertEqual(top[:11], ["hb-s%d" % i for i in range(11)])
        secs = section_slices(t)
        for n, schemas in GOLD.items():
            for sc in schemas:
                self.assertRegex(secs["hb-s%d" % n], r"<!-- PM:BEGIN %s -->" % re.escape(sc), "hb-s%d lacks %s" % (n, sc))
        for m in PM_RE.finditer(t):
            o = json.loads(m.group(2))
            self.assertEqual(o["schema"], m.group(1))
        self.assertEqual(len(PM_RE.findall(t)), len(re.findall(r"<!-- PM:BEGIN ", t)), "a PM block is malformed")
        for m in HB_RE.finditer(t):
            json.loads(m.group(2))
        for n in (8, 9, 10):
            self.assertIn("<!-- HB:GEN BEGIN hb-s%d -->" % n, secs["hb-s%d" % n])
            self.assertIn("<!-- HB:GEN END hb-s%d -->" % n, secs["hb-s%d" % n])
        self.assertIn("HB-VNS0S1-001", t)

    def test_a_states_the_contract_limits(self):
        t = book_text()
        for needle in ("KERNELS_REAL=False", "E_VN_NATIVE_MISSING", "Cargo.lock", "AUSENTE", "HB-VN-n1-native", "pm.edges.v2",
                       "[0, 2^52)", "[TARGET]", "describe_frame"):
            self.assertIn(needle, t)

    def test_b_gfloor(self):
        t = book_text()
        # 7 fields per listed symbol
        heads = re.findall(r"# PM-P3-HEADER: (\S+)\n((?:# [A-Z]+: .*\n)+)", t)
        seen = {}
        for sym, body in heads:
            fields = [l[2:].split(":", 1)[0] for l in body.splitlines()]
            self.assertEqual(tuple(fields), HDR_FIELDS, sym)
            seen[sym] = fields
        for s in KERNEL_SYMBOLS:
            self.assertIn(s, seen)
        # every module section: >=2 INV, >=1 AU
        mods = [json.loads(m.group(2)) for m in HB_RE.finditer(t) if m.group(1) == "hb.inv.v2"]
        self.assertGreaterEqual(len(mods), 2)
        for o in mods:
            self.assertGreaterEqual(len(o["inv"]), 2, o["module"])
            self.assertGreaterEqual(len(o["au"]), 1, o["module"])
        # non-empty pm.search.v1
        sr = [json.loads(m.group(2)) for m in PM_RE.finditer(t) if m.group(1) == "pm.search.v1"]
        self.assertEqual(len(sr), 1)
        self.assertTrue(sr[0]["questions_answered"])
        # every blocked_use present in pm.limits.v2
        lim = [json.loads(m.group(2)) for m in PM_RE.finditer(t) if m.group(1) == "pm.limits.v2"][0]
        self.assertTrue(lim["entries"])
        for e in lim["entries"]:
            self.assertTrue(e.get("blocked_use"), e)
        # untagged numbers: every unit-bearing number sits on a line (or JSON string line) that carries a tag
        bad = [(i + 1, l.strip()[:90]) for i, l in enumerate(t.splitlines()) if UNIT_RE.search(l) and not TAG_RE.search(l)]
        self.assertEqual(bad, [])
        for word in FORBIDDEN:
            self.assertNotIn(word, t)
        # engine-wins rulings: no FNV, no luma-only xh64 recipe, xh64 stays Python
        self.assertIn("xh64 stays Python", t)

    def test_b_companions(self):
        with open(os.path.join(BOOK_DIR, "mathlinks.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        self.assertTrue(rows)
        for r in rows:
            self.assertIn(r["status"], ("SEALED", "AUSENTE"))
        with open(os.path.join(BOOK_DIR, "seed", "slots.json"), encoding="utf-8") as f:
            slots = json.load(f)
        self.assertEqual(slots["book_id"], "HB-VNS0S1-001")
        with open(os.path.join(BOOK_DIR, "receipts", "R-vn-s0s1-unittest.json"), encoding="utf-8") as f:
            rc = json.load(f)
        self.assertEqual((rc["id"], rc["exit_code"], rc["skipped"], rc["schema"]), ("R-vn-s0s1-unittest", 0, 0, "hb.receipt.v1"))
        self.assertGreaterEqual(rc["metrics"]["tests_run"], 1)
        self.assertIn("tests/test_hb_vn_s0s1_panel3.py", rc["sources"])


# ---- dual contract (b) / reference identity (c) -----------------------------------------------------------------


@contextlib.contextmanager
def real_kernels(kernels=None):
    old = backend.KERNELS_REAL, backend.KERNELS
    backend.KERNELS_REAL = True
    if kernels is not None:
        backend.KERNELS = kernels
    with tempfile.NamedTemporaryFile(suffix=".so") as f:
        try:
            yield f.name
        finally:
            backend.KERNELS_REAL, backend.KERNELS = old


def d2_source(W, H, kind, n=4):
    frames = []
    for i in range(n):
        if kind == "rand":
            a = rand_u8(1000 + i, (H, W, 3))
        elif kind == "const":
            a = np.full((H, W, 3), 255 if i % 2 else 0, np.uint8)
        else:  # checker: 1 px
            yy, xx = np.mgrid[0:H, 0:W]
            a = np.repeat((((yy + xx + i) & 1) * 255).astype(np.uint8)[..., None], 3, axis=2)
        frames.append(fr.Frame(i, i * 33333, i, np.ascontiguousarray(a)))
    return fr.MemoryFrameSource(frames, {"width": W, "height": H})


D2 = [(1, 1, "rand"), (15, 15, "rand"), (17, 17, "rand"), (16, 16, "const"), (100, 40, "checker"), (4097, 1, "rand"), (1, 300, "const")]


class TestDualContract(unittest.TestCase):
    def test_b_dual_verify_both_branches(self):
        self.assertIs(backend.KERNELS_REAL, False)  # today: identity shims, no native library
        with tempfile.NamedTemporaryFile(suffix=".so") as lib:  # branch 1: refuses even with a library file present
            with self.assertRaises(backend.VNativeError) as c:
                vrun.run(d2_source(17, 17, "rand"), mode="dual_verify", lib=lib.name)
        self.assertEqual(c.exception.code, "E_VN_NATIVE_MISSING")
        self.assertIs(c.exception.detail.get("kernels_real"), False)
        with real_kernels():  # branch 2: KERNELS_REAL True but no library file
            with self.assertRaises(backend.VNativeError) as c:
                vrun.run(d2_source(17, 17, "rand"), mode="dual_verify", lib="/nonexistent/none.so")
            self.assertEqual(c.exception.code, "E_VN_NATIVE_MISSING")
        with real_kernels() as lib:  # branch 3: KERNELS_REAL True -> byte-identical over corpus D2
            for W, H, kind in D2:
                ref = vrun.run(d2_source(W, H, kind), mode="python")
                got = vrun.run(d2_source(W, H, kind), mode="dual_verify", lib=lib)
                vrun.compare_runs(ref, got)
                for k, (x, y) in enumerate(zip(ref.tiles, got.tiles)):
                    dual.assert_same("tiles[%d]" % k, x, y)

            def bad_compute(out, idx, padded, grid, p, y_plane):  # planted divergence in one TILE_FIELD family
                backend._ORIG["_compute"](out, idx, padded, grid, p, y_plane)
                out["sig64"][idx[0]] ^= np.uint64(1)

            tbl = dict(backend.KERNELS)
            tbl["_compute"] = bad_compute
            with real_kernels(tbl) as lib2:
                with self.assertRaises(backend.VNativeError) as c:
                    vrun.run(d2_source(100, 40, "rand"), mode="dual_verify", lib=lib2)
        self.assertEqual(c.exception.code, "E_VN_DIVERGE")
        self.assertEqual(sorted(c.exception.detail), ["field", "first_index", "name"])
        self.assertEqual(c.exception.detail["field"], "sig64")
        self.assertEqual(c.exception.detail["name"], "tiles[0]")
        self.assertIs(desc._compute, backend._ORIG["_compute"])  # scope restored

    def test_c_reference_is_orig(self):
        orig = backend.originals()
        self.assertIs(orig["luma_of_padded"], backend._ORIG["luma_of_padded"])
        with backend.reference():
            for n in ("luma_of_padded", "_compute"):
                self.assertIs(getattr(desc, n), backend._ORIG[n], n)
        self.assertEqual({n: getattr(backend._ORIG[n], "__module__", None) for n in backend._ORIG},
                         {"luma_of_padded": "visioncore.desc", "_compute": "visioncore.desc"})
        marker = {}

        def shim_luma(p):
            marker["hit"] = True
            return backend._ORIG["luma_of_padded"](p)

        with backend.installed({"luma_of_padded": shim_luma}):
            self.assertIs(desc.luma_of_padded, shim_luma)
            with backend.reference():  # nested reference leg still yields the originals
                self.assertIs(desc.luma_of_padded, backend._ORIG["luma_of_padded"])
            self.assertIs(desc.luma_of_padded, shim_luma)
        self.assertIs(desc.luma_of_padded, backend._ORIG["luma_of_padded"])
        for n in backend._ORIG:  # the reference never aliases a kernel-table entry
            self.assertIsNot(backend._ORIG[n], backend.KERNELS[n])
        with backend.installed():  # patched outside installed() -> reference refuses
            pass
        desc.luma_of_padded = shim_luma
        try:
            with self.assertRaises(backend.VNativeError) as c:
                with backend.reference():
                    pass
            self.assertEqual(c.exception.code, "E_VN_SCOPE")
        finally:
            desc.luma_of_padded = backend._ORIG["luma_of_padded"]


# ---- independent spec oracle for one padded tile (python ints) ----------------------------------------------------


def ref_tile(T, tau):
    """T: s x s x 3 nested ints (padded tile). -> dict of the 8 fields _compute writes, minus F_PAD."""
    s = len(T)
    n = s * s
    y = [[0] * s for _ in range(s)]
    sum_y = sum_y2 = sum_co = sum_cg = 0
    sat = 0
    for i in range(s):
        for j in range(s):
            r, g, b = T[i][j]
            co = r - b
            t = b + (co >> 1)
            cg = g - t
            yy = t + (cg >> 1)
            y[i][j] = yy
            sum_y += yy
            sum_y2 += yy * yy
            sum_co += co
            sum_cg += cg
            sat += any(c in (0, 255) for c in (r, g, b))
    grad = e = 0
    for i in range(s):
        for j in range(s):
            g_ = (abs(y[i][j + 1] - y[i][j]) if j < s - 1 else 0) + (abs(y[i + 1][j] - y[i][j]) if i < s - 1 else 0)
            grad += g_
            e += g_ > tau
    b = s // 8
    sig = 0
    for bi in range(8):
        for bj in range(8):
            sub = sum(y[i][j] for i in range(bi * b, bi * b + b) for j in range(bj * b, bj * b + b))
            if sub * n > sum_y * b * b:
                sig |= 1 << (bi * 8 + bj)
    flags = 0
    if 2 * e >= s and e < 4 * s:
        flags |= desc.F_EDGE
    if e >= 4 * s:
        flags |= desc.F_TEXTURE
    if 4 * sat >= n:
        flags |= desc.F_SAT
    return {"y_q4": 16 * sum_y // n, "co_h": max(-128, min(127, sum_co // (2 * n))), "cg_h": max(-128, min(127, sum_cg // (2 * n))),
            "sig_q4": 16 * math.isqrt(n * sum_y2 - sum_y * sum_y) // n, "grad_q6": 64 * grad // n, "edge": 255 * e // n,
            "sig64": sig, "flags": flags}


def check_frame_against_spec(tc, frame, p=None):
    p = PARAMS if p is None else p
    H, W = frame.shape[:2]
    g = vgrid.Grid(W, H)
    tiles, _ = desc.describe(frame, g, p=p)
    padded = g.pad(frame)
    s = g.s
    for r in range(g.N):
        ty, tx = int(g.ty[r]), int(g.tx[r])
        T = padded[ty * s:(ty + 1) * s, tx * s:(tx + 1) * s].astype(int).tolist()
        want = ref_tile(T, p["TAU_EDGE"])
        want["flags"] |= desc.F_PAD if g.pad_flags()[r] else 0
        for f, v in want.items():
            tc.assertEqual(int(tiles[f][r]), v, "tile %d field %s (W=%d H=%d)" % (r, f, W, H))


class TestSpecLegs(unittest.TestCase):
    def test_d2_negative_co_floor(self):  # CQ-N-1: floor, not truncate; sum_co = -1 per 2n gives co_h = -1
        f = np.zeros((16, 16, 3), np.uint8)
        f[..., 0], f[..., 2] = 100, 101  # R-B = -1 everywhere
        check_frame_against_spec(self, f)
        g = vgrid.Grid(16, 16)
        t, _ = desc.describe(f, g)
        self.assertEqual(int(t["co_h"][0]), -1)

    def test_d2_constant_tiles_sig64_strict(self):  # CQ-N-4: strict compare -> constant tile has sig64 == 0
        for v in (0, 77, 255):
            f = np.full((32, 32, 3), v, np.uint8)
            check_frame_against_spec(self, f)
            t, _ = desc.describe(f, vgrid.Grid(32, 32))
            self.assertEqual(int(t["sig64"][0]), 0)

    def test_d2_random_and_checker_every_s(self):
        for k, (W, H) in enumerate([(15, 15), (17, 17), (33, 31), (130, 70), (100, 40), (256, 160), (520, 260)]):
            check_frame_against_spec(self, rand_u8(500 + k, (H, W, 3)))
        yy, xx = np.mgrid[0:48, 0:48]
        check_frame_against_spec(self, np.repeat((((yy + xx) & 1) * 255).astype(np.uint8)[..., None], 3, axis=2))
        reached = {vgrid.Grid(*wh).s for wh in [(15, 15), (256, 160), (1280, 720), (1920, 1080), (3840, 2160), (8192, 4096)]}
        self.assertEqual(reached, {16, 32, 64, 128})

    def test_d2_tau_is_a_parameter(self):  # CQ-N-11: TAU_EDGE comes from p, never a literal
        f = rand_u8(9, (32, 32, 3))
        p10 = dict(PARAMS, TAU_EDGE=10)
        check_frame_against_spec(self, f, p10)
        a, _ = desc.describe(f, vgrid.Grid(32, 32), p=p10)
        b, _ = desc.describe(f, vgrid.Grid(32, 32))
        self.assertNotEqual(int(a["edge"][0]), int(b["edge"][0]))

    def test_d2_pad_is_index_clamp(self):  # CQ-N-5: S0 edge replication == virtual clamp min(x,W-1), min(y,H-1)
        for k, (W, H) in enumerate([(15, 15), (17, 33), (100, 40), (4097, 1), (1, 300)]):
            f = rand_u8(70 + k, (H, W, 3))
            g = vgrid.Grid(W, H)
            P = g.pad(f)
            self.assertEqual(P.shape, (g.gh * g.s, g.gw * g.s, 3))
            ys = np.minimum(np.arange(P.shape[0]), H - 1)
            xs = np.minimum(np.arange(P.shape[1]), W - 1)
            self.assertEqual(P.tobytes(), f[ys][:, xs].tobytes())

    def test_d2_layout_constants(self):
        self.assertEqual((layout.TILE_SIZE, len(layout.TILE_FIELDS), layout.CELL_MAX, layout.DIM_MAX), (32, 14, 4096, 8192))
        self.assertEqual(desc.TILE_DTYPE.itemsize, 32)


class TestPropertyLegs(unittest.TestCase):
    def test_splitmix_vector(self):
        self.assertEqual(splitmix64(0, 2), [0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4])

    def test_p1_isqrt_boundaries(self):
        chunk = 1 << 20
        for lo in range(1, 1 << 22, chunk):
            k = np.arange(lo, min(lo + chunk, 1 << 22), dtype=np.int64)
            for dv, want in ((-1, k - 1), (0, k), (1, k)):
                got = desc.isqrt(k * k + dv)
                bad = np.nonzero(got != want)[0]
                self.assertEqual(len(bad), 0, "isqrt(k^2%+d) wrong at k=%d" % (dv, int(k[bad[0]]) if len(bad) else 0))
        self.assertEqual(int(desc.isqrt(np.array([0, 1, 2, 3]))[0]), 0)
        self.assertEqual(desc.isqrt(np.array([0, 1, 2, 3, 4])).tolist(), [0, 1, 1, 1, 2])
        self.assertEqual(int(desc.isqrt(np.array([(1 << 52) - 1]))[0]), (1 << 26) - 1)
        v = np.array([w % (1 << 52) for w in splitmix64(2024, 20000)], dtype=np.int64)
        self.assertEqual(desc.isqrt(v).tolist(), [math.isqrt(int(x)) for x in v])

    def test_p2_ycocg_planes(self):
        a = np.arange(256, dtype=np.int16)
        R, B = np.meshgrid(a, a, indexing="ij")  # plane (r,b): 2^16
        co = R - B
        t = B + (co >> 1)
        self.assertEqual((int(co.min()), int(co.max()), int(t.min()), int(t.max())), (-255, 255, 0, 255))
        self.assertEqual(((t - (co >> 1)) != B).sum(), 0)  # b recovered
        self.assertEqual((((t - (co >> 1)) + co) != R).sum(), 0)  # r recovered
        G, Tt = np.meshgrid(a, a, indexing="ij")  # plane (g,t): 2^16, t reachable range is 0..255
        cg = G - Tt
        y = Tt + (cg >> 1)
        self.assertEqual((int(cg.min()), int(cg.max()), int(y.min()), int(y.max())), (-255, 255, 0, 255))
        self.assertEqual((((y - (cg >> 1)) != Tt)).sum(), 0)
        self.assertEqual(((cg + (y - (cg >> 1))) != G).sum(), 0)
        # luma_of_padded (the native slot) equals the factored composition on all 2^24 RGB triples, one r-slab per call
        gg, bb = np.meshgrid(np.arange(256, dtype=np.uint8), np.arange(256, dtype=np.uint8), indexing="ij")
        for r in range(256):
            slab = np.stack([np.full_like(gg, r), gg, bb], axis=-1)
            tt = t[r][bb.astype(np.intp)]  # t from the (r,b) plane
            want = y[gg.astype(np.intp), tt.astype(np.intp)]  # y from the (g,t) plane
            self.assertEqual(desc.luma_of_padded(slab).tobytes(), want.tobytes(), "r=%d" % r)
        s3 = rand_u8(5, (64, 64, 3))
        yy, cc, gg_ = desc.ycocg(s3)
        self.assertEqual(desc.luma_of_padded(s3).tobytes(), yy.tobytes())
        self.assertEqual(int(yy.min()) >= 0 and int(yy.max()) <= 255, True)

    def test_p3_morton_bijection(self):
        seen = set()
        for ty in range(256):
            for tx in range(256):
                m = layout.morton(tx, ty)
                self.assertEqual(layout.unmorton(m), (tx, ty))
                seen.add(m)
        self.assertEqual(len(seen), 65536)
        for W, H in ((1280, 720), (1920, 1080), (3840, 2160), (100, 40), (4097, 1)):
            g = vgrid.Grid(W, H)
            self.assertEqual(g.order[g.ty, g.tx].tolist(), list(range(g.N)), (W, H))
            self.assertEqual(g.mortons.tolist(), sorted(g.mortons.tolist()))


# ---- mutants (e) --------------------------------------------------------------------------------------------------
MUTANTS = {
    "m_co_h_trunc_div": ("visioncore/desc.py", "np.clip(sum_co // (2 * n), -128, 127)",
                         "np.clip(np.where(sum_co < 0, -((-sum_co) // (2 * n)), sum_co // (2 * n)), -128, 127)",
                         "TestSpecLegs.test_d2_negative_co_floor"),
    "m_sig64_ge": ("visioncore/desc.py", "bits = (sub * n > sum_y[:, None, None] * (b * b))", "bits = (sub * n >= sum_y[:, None, None] * (b * b))",
                   "TestSpecLegs.test_d2_constant_tiles_sig64_strict"),
    "m_isqrt_off_by_one": ("visioncore/desc.py", "r += ((r + 1) * (r + 1) <= v)", "r += ((r + 1) * (r + 1) <= v + 1)", "TestPropertyLegs.test_p1_isqrt_boundaries"),
    "m_pad_zero": ("visioncore/grid.py", 'mode="edge"', 'mode="constant"', "TestSpecLegs.test_d2_pad_is_index_clamp"),
    "m_tau_hardcoded": ("visioncore/desc.py", 'p["TAU_EDGE"]', "48", "TestSpecLegs.test_d2_tau_is_a_parameter"),
}
COPY = ("visioncore", "vnative", "vadapt", "hbtools_v2", "standards")


def _run_named(cwd, name):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", HBVN_MUTANT_RUN="1")
    r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_hb_vn_s0s1_panel3." + name], cwd=cwd, env=env, capture_output=True,
                       text=True, timeout=600)
    return r.returncode, (r.stderr + r.stdout)[-300:]


def run_mutants(scratch):
    """Clean control first (must pass), then each mutant on its own copy; a kill = the NAMED test fails with rc in {1,5}."""
    base = os.path.join(scratch, "ctl")
    for d in (scratch,):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(base, "tests"))
    for d in COPY:
        shutil.copytree(os.path.join(ROOT, d), os.path.join(base, d), ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(os.path.join(ROOT, "tests", "test_hb_vn_s0s1_panel3.py"), os.path.join(base, "tests"))
    with open(os.path.join(base, "tests", "__init__.py"), "w"):
        pass
    rows = []
    control = {n: _run_named(base, t[3]) for n, t in MUTANTS.items()}
    for n, (path, old, new, test) in MUTANTS.items():
        d = os.path.join(scratch, n)
        shutil.copytree(base, d)
        p = os.path.join(d, path)
        with open(p, encoding="utf-8") as f:
            src = f.read()
        if src.count(old) != 1:
            rows.append({"id": n, "oracle": test, "killed": False, "rc": None, "why": "anchor not unique"})
            continue
        with open(p, "w", encoding="utf-8") as f:
            f.write(src.replace(old, new))
        rc, tail = _run_named(d, test)
        rows.append({"id": n, "mutated": path, "oracle": test, "control_rc": control[n][0], "rc": rc,
                     "killed": control[n][0] == 0 and rc in MUT_RC})
    return {"schema": "vn.mutants/v1", "control_ok": all(c[0] == 0 for c in control.values()), "total": len(rows),
            "killed": sum(r["killed"] for r in rows), "mutants": rows}


class TestMutants(unittest.TestCase):
    def test_e_mutants_killed_by_named_tests(self):
        if os.environ.get("HBVN_MUTANT_RUN"):  # inside a mutant copy this class must not recurse
            return
        scratch = os.path.join(ROOT, "build", "room_N", "mutants")
        rec = run_mutants(scratch)
        out = os.path.join(ROOT, "build", "room_N", "mutants_receipt.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1, sort_keys=True)
        self.assertTrue(rec["control_ok"], rec)
        self.assertGreaterEqual(rec["total"], 3)
        self.assertEqual(rec["killed"], rec["total"], rec)


if __name__ == "__main__":
    unittest.main()
