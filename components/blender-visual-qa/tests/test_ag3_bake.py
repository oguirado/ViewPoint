"""AG3 N3: bake_pbr host verifier + bake_worker Blender worker (router §3.7). Blender tests need BVQA_BLENDER=1;
the 2048 px bake also needs BVQA_SLOW=1."""
import contextlib
import io
import json
import math
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from blender_visual_qa.assets import bake_pbr as B
from blender_visual_qa.assets import glb_core as g
from blender_visual_qa.assets import orm_pack

Q = 65536
BLENDER = os.environ.get("BVQA_BLENDER") == "1" and shutil.which("blender")
SLOW = os.environ.get("BVQA_SLOW") == "1"


def sphere(name, seg, rings, r_um=500000, dx_um=0):
    """Closed UV sphere resting on Y=0, seam-split UVs, unit normals."""
    pos, uvs, nrm, idx, grid = [], [], [], [], {}

    def vert(i, j):
        th, ph = math.pi * i / rings, 2 * math.pi * (j % seg) / seg
        n = (math.sin(th) * math.cos(ph), math.cos(th), math.sin(th) * math.sin(ph))
        pos.append(tuple(int(round(c * r_um)) + (r_um if k == 1 else 0) + (dx_um if k == 0 else 0)
                         for k, c in enumerate(n)))
        nrm.append(tuple(int(round(c * Q)) for c in n))
        uvs.append((int(round(Q * j / seg)), int(round(Q * i / rings))))
        return len(pos) - 1
    for i in range(1, rings):
        for j in range(seg + 1):
            grid[i, j] = vert(i, j)
    for j in range(seg):
        top, bot = vert(0, j), vert(rings, j)
        idx += [top, grid[1, j + 1], grid[1, j], bot, grid[rings - 1, j], grid[rings - 1, j + 1]]
    for i in range(1, rings - 1):
        for j in range(seg):
            a, b, c, d = grid[i, j], grid[i, j + 1], grid[i + 1, j + 1], grid[i + 1, j]
            idx += [a, b, c, a, c, d]
    return g.Prim(name, "/" + name, g.IDENTITY_Q16, pos, idx, uvs, nrm, "paint")


def with_pbr(glb, base=(0.6, 0.3, 0.2, 1.0), metallic=0.0, roughness=0.6):
    """Give every material of a glb_core GLB a pbrMetallicRoughness block (glTF defaults would be metallic 1)."""
    jlen = struct.unpack("<I", glb[12:16])[0]
    doc = json.loads(glb[20:20 + jlen])
    for m in doc.get("materials", []):
        m["pbrMetallicRoughness"] = {"baseColorFactor": list(base), "metallicFactor": metallic,
                                     "roughnessFactor": roughness}
    j = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    j += b" " * ((4 - len(j) % 4) % 4)
    rest = glb[20 + jlen:]
    body = struct.pack("<II", len(j), g.CHUNK_JSON) + j + rest
    return g.GLB_MAGIC + struct.pack("<II", g.GLB_VERSION, g.GLB_HEADER_BYTES + len(body)) + body


def quad_uv_scene(overlap=False):
    """Low stand-in: one sphere whose UVs are replaced by a 2-triangle quad atlas per pair (non-overlapping grid)."""
    s = sphere("ball", 8, 4)
    n = len(s.indices) // 3
    pos, idx, uvs = [], [], []
    side = math.isqrt(n) + 1
    cell = Q // side
    for t in range(n):
        cx, cy = (t % side) * cell, (t // side) * cell
        corners = [(cx + 1, cy + 1), (cx + cell - 1, cy + 1), (cx + 1, cy + cell - 1)]
        if overlap and t == 1:
            corners = [(1, 1), (cell - 1, 1), (1, cell - 1)]  # same island as triangle 0
        for c in range(3):
            pos.append(s.positions_um[s.indices[3 * t + c]])
            uvs.append(corners[c])
            idx.append(len(pos) - 1)
    return g.Scene([g.Prim("ball", "/ball", g.IDENTITY_Q16, pos, idx, uvs, None, None)])


def png(res, rgb):
    return orm_pack.encode_png(res, res, bytes(rgb) * (res * res) if len(rgb) == 4 else bytes(list(rgb) + [255]) * (res * res))


GOOD = {"albedo": (150, 80, 50), "normal": (128, 128, 250), "ao": (230, 230, 230), "roughness": (153, 153, 153),
        "metal": (0, 0, 0)}


class FakeRunner:
    """Writes synthetic worker outputs; `maps`/`uv_scene` override them (fixture mutants)."""

    def __init__(self, res, maps=None, uv_scene=None, rc=0, skip=()):
        self.res, self.maps, self.uv_scene, self.rc, self.skip, self.calls = res, dict(GOOD, **(maps or {})), uv_scene, rc, skip, []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        out = Path(argv[argv.index("--out-dir") + 1])
        for m, rgb in self.maps.items():
            if m not in self.skip:
                (out / (m + ".png")).write_bytes(png(self.res, rgb))
        if "low_uv" not in self.skip:
            (out / "low_uv.glb").write_bytes(g.write_glb(self.uv_scene or quad_uv_scene()))
        return self.rc, "BVQA_BAKE {\"maps\":5}\n", ""


class BakeHostTest(unittest.TestCase):
    RES = 64

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="bvqa_ag3_bake_"))
        self.addCleanup(shutil.rmtree, self.td, True)
        self.high = self.td / "high.glb"
        self.low = self.td / "low.glb"
        self.high.write_bytes(g.write_glb(g.Scene([sphere("ball", 16, 8)])))
        self.low.write_bytes(g.write_glb(g.Scene([sphere("ball", 8, 4)])))
        self.out = self.td / "out"

    def run_bake(self, runner, *extra, low=None):
        return B.run(["--high", str(self.high), "--low", str(low or self.low), "--out-dir", str(self.out),
                      "--res", str(self.RES)] + list(extra), runner=runner)

    def test_pass_and_receipt(self):
        r = FakeRunner(self.RES)
        rc, rec = self.run_bake(r)
        self.assertEqual((rc, rec["verdict"]), (0, "PASS"), rec["reason"])
        self.assertEqual(rec["schema"], "bvqa.asset-bake-pbr/v1")
        self.assertEqual(rec["input_sha256"], g.sha256(self.low.read_bytes()))
        self.assertEqual(rec["high_sha256"], g.sha256(self.high.read_bytes()))
        self.assertGreater(rec["checks"]["coverage_ppm"], 0)
        self.assertEqual(sorted(rec["checks"]["maps"]), sorted(B.MAPS))
        self.assertGreater(rec["checks"]["maps"]["normal"]["mean_rgb"][2], B.NORMAL_BLUE_MIN)
        argv = r.calls[0]
        self.assertIn("--factory-startup", argv)
        self.assertEqual(argv[argv.index("--res") + 1], str(self.RES))

    def test_main_writes_receipt_and_chains(self):
        prev = self.td / "prev.json"
        prev.write_text("{}")
        rc, rec = self.run_bake(FakeRunner(self.RES), "--prev-receipt", str(prev))
        self.assertEqual(rc, 0)
        self.assertEqual(rec["prev_receipt_sha256"], g.sha256(b"{}"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc2 = B.main(["--verify-only", "--high", str(self.high), "--low", str(self.low), "--out-dir", str(self.out),
                          "--res", str(self.RES)])
        self.assertEqual(rc2, 0)
        self.assertEqual(json.loads(buf.getvalue())["verdict"], "PASS")
        self.assertTrue((self.out / "bake_receipt.json").is_file())

    # ---- mutants (each must flip rc)
    def test_mutant_aabb_misaligned(self):
        self.low.write_bytes(g.write_glb(g.Scene([sphere("ball", 8, 4, dx_um=50000)])))
        r = FakeRunner(self.RES)
        rc, rec = self.run_bake(r)
        self.assertEqual(rc, 5)
        self.assertEqual(rec["failed"], ["aabb_misaligned"])
        self.assertEqual(r.calls, [], "Blender must not run on a misaligned pair")

    def test_mutant_normal_blue_low(self):
        rc, rec = self.run_bake(FakeRunner(self.RES, maps={"normal": (128, 128, 100)}))
        self.assertEqual(rc, 5)
        self.assertIn("normal:blue_mean", rec["failed"])

    def test_mutant_unbaked_magenta(self):
        rc, rec = self.run_bake(FakeRunner(self.RES, maps={"ao": (255, 0, 255)}))
        self.assertEqual(rc, 5)
        self.assertIn("ao:holes", rec["failed"])

    def test_mutant_albedo_black(self):
        rc, rec = self.run_bake(FakeRunner(self.RES, maps={"albedo": (0, 0, 0)}))
        self.assertEqual(rc, 5)
        self.assertIn("albedo:black", rec["failed"])

    def test_mutant_uv_overlap(self):
        rc, rec = self.run_bake(FakeRunner(self.RES, uv_scene=quad_uv_scene(overlap=True)))
        self.assertEqual(rc, 5)
        self.assertIn("low_uv:uv_overlap", rec["failed"])

    def test_mutant_worker_swapped_low(self):
        rc, rec = self.run_bake(FakeRunner(self.RES, uv_scene=g.Scene([sphere("ball", 8, 4, r_um=300000)])))
        self.assertEqual(rc, 5)
        self.assertIn("low_uv:aabb", rec["failed"])

    def test_mutant_wrong_size(self):
        r = FakeRunner(self.RES)
        r.res = self.RES // 2
        rc, rec = self.run_bake(r)
        self.assertEqual(rc, 5)
        self.assertIn("normal:size", rec["failed"])

    def test_mutant_partial_holes(self):
        """A map with a magenta block over > HOLE_MAX_PPM of the atlas fails; a clean map passes."""
        self.run_bake(FakeRunner(self.RES))
        px = bytearray(bytes([128, 128, 250, 255]) * (self.RES * self.RES))
        for y in range(self.RES // 2):
            for x in range(self.RES):
                px[(y * self.RES + x) * 4:(y * self.RES + x) * 4 + 3] = bytes((255, 0, 255))
        (self.out / "normal.png").write_bytes(orm_pack.encode_png(self.RES, self.RES, bytes(px)))
        rc, rec = B.run(["--verify-only", "--high", str(self.high), "--low", str(self.low), "--out-dir", str(self.out),
                         "--res", str(self.RES)])
        self.assertEqual(rc, 5)
        self.assertIn("normal:holes", rec["failed"])
        self.assertGreater(rec["checks"]["maps"]["normal"]["holes_ppm"], B.HOLE_MAX_PPM)

    # ---- incomplete / usage / budget
    def test_missing_output_rc3(self):
        rc, rec = self.run_bake(FakeRunner(self.RES, skip=("metal",)))
        self.assertEqual((rc, rec["verdict"]), (3, "INCOMPLETE"))

    def test_stale_outputs_removed_before_bake(self):
        self.run_bake(FakeRunner(self.RES))
        rc, _ = self.run_bake(FakeRunner(self.RES, skip=("low_uv",)))
        self.assertEqual(rc, 3, "an old low_uv.glb must not satisfy a new bake")

    def test_worker_error_rc2_and_timeout_rc6(self):
        rc, _ = self.run_bake(FakeRunner(self.RES, rc=2))
        self.assertEqual(rc, 2)

        def slow(argv, timeout):
            raise B.BakeError(B.RC_BUDGET, "timeout")
        rc, rec = self.run_bake(slow)
        self.assertEqual((rc, rec["verdict"]), (6, "BUDGET"))

    def test_res_policy(self):
        r = FakeRunner(self.RES)
        self.assertEqual(self.run_bake(r, "--res", "2048")[0], 2, "2048 without --slow is usage")
        self.assertEqual(self.run_bake(r, "--res", "1000")[0], 2, "non power of two")
        self.assertEqual(self.run_bake(r, "--res", "32")[0], 2, "below minimum")
        self.assertEqual(r.calls, [])
        self.assertEqual(B.run(["--high", str(self.high)])[0], 2)

    def test_skinned_and_empty_rc3(self):
        s = sphere("ball", 8, 4)
        s.indices = []
        self.low.write_bytes(g.write_glb(g.Scene([s])))
        self.assertEqual(self.run_bake(FakeRunner(self.RES))[0], 3)

    def test_coverage_rasterizer(self):
        tri = [((0, 0), ((0, 0), (Q, 0), (0, Q)))]
        cov = B.coverage(tri, 8)
        self.assertEqual(len(cov), 36)  # texel centres strictly below the diagonal plus the 8 on it
        self.assertIn((7, 0), cov)  # glTF v = 0 is PNG row 0 (top-left origin, no flip)
        self.assertNotIn((7, 7), cov)

    def test_cli_dispatch_if_registered(self):
        from blender_visual_qa.assets import cli
        if "bake-pbr" not in cli.STAGES:
            self.skipTest("INT has not registered bake-pbr in assets/cli.py yet")
        self.assertEqual(cli.STAGES["bake-pbr"], "bake_pbr")


@unittest.skipUnless(BLENDER, "BVQA_BLENDER=1 and blender on PATH required")
class BakeBlenderTest(unittest.TestCase):
    """Real Cycles CPU bake: high sphere 64x32 -> low sphere 32x16 (sag 2.4 mm < 5 mm cage)."""

    @classmethod
    def setUpClass(cls):
        cls.td = Path(tempfile.mkdtemp(prefix="bvqa_ag3_bake_bl_"))
        cls.high = cls.td / "high.glb"
        cls.low = cls.td / "low.glb"
        cls.high.write_bytes(with_pbr(g.write_glb(g.Scene([sphere("ball", 64, 32)]))))
        cls.low.write_bytes(g.write_glb(g.Scene([sphere("ball", 32, 16)])))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, True)

    def bake(self, res, *extra):
        out = self.td / ("out_%d" % res)
        rc, rec = B.run(["--high", str(self.high), "--low", str(self.low), "--out-dir", str(out), "--res", str(res),
                         "--timeout", "1500"] + list(extra))
        return out, rc, rec

    def test_bake_1024_pass_then_mutants(self):
        out, rc, rec = self.bake(B.RES)
        self.assertEqual(rc, 0, rec["reason"])
        maps = rec["checks"]["maps"]
        self.assertGreater(rec["checks"]["coverage_ppm"], 100000)
        self.assertGreater(maps["normal"]["mean_rgb"][2], B.NORMAL_BLUE_MIN)
        r, gr, b = maps["albedo"]["mean_rgb"]
        self.assertGreater(r, b, "albedo keeps the high base colour (red > blue)")
        self.assertLess(maps["metal"]["mean_rgb"][0], 16)
        self.assertEqual(rec["checks"]["low_uv"]["uv_overlap_tris"], 0)
        verify = ["--verify-only", "--high", str(self.high), "--low", str(self.low), "--out-dir", str(out),
                  "--res", str(B.RES)]
        # mutant: inverted tangent Z on the real normal map
        w, h, ch, px = orm_pack.decode_png((out / "normal.png").read_bytes())
        inv = bytearray()
        for i in range(w * h):
            p = px[i * ch:i * ch + ch]
            inv += bytes((p[0], p[1], 255 - p[2], 255))
        (out / "normal.png").write_bytes(orm_pack.encode_png(w, h, bytes(inv)))
        rc2, rec2 = B.run(verify)
        self.assertEqual(rc2, 5)
        self.assertIn("normal:blue_mean", rec2["failed"])
        # mutant: albedo never baked (still the magenta fill)
        (out / "albedo.png").write_bytes(png(B.RES, (255, 0, 255)))
        rc3, rec3 = B.run(verify)
        self.assertIn("albedo:holes", rec3["failed"])

    @unittest.skipUnless(SLOW, "2048 px bake is slow-only (BVQA_SLOW=1)")
    def test_bake_2048_slow(self):
        _, rc, rec = self.bake(B.RES_SLOW, "--slow")
        self.assertEqual(rc, 0, rec["reason"])


if __name__ == "__main__":
    unittest.main()
