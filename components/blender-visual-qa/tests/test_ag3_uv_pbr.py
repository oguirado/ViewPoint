"""AG3 N4: uv_gate (flip/stretch) + pbr_validate (BRDF parameters). Fixtures are built in-test (no binaries).
Each mutant pairs with a passing base; exact-value tests pin the integer stretch maths."""
import hashlib
import json
import os
import struct
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from blender_visual_qa.assets import glb_core, orm_pack, pbr_validate as pv, uv_gate as ug

S = 1000000
Q = 65536


def plane(n=2, su=1, sv=1, mutate=None):
    """n x n quad grid in z=0, 1 m cells; UV = (x*su, y*sv) * 2**13 per metre. One vertex per corner."""
    P, I, U = [], [], []
    for j in range(n):
        for i in range(n):
            quad = [(i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1)]
            for tri in ((0, 1, 2), (0, 2, 3)):
                for k in tri:
                    x, y = quad[k]
                    I.append(len(P))
                    P.append((x * S, y * S, 0))
                    U.append((x * su * 8192, y * sv * 8192))
    if mutate:
        mutate(U)
    return NS(name="plane", positions_um=P, indices=I, uvs_q16=U, material="m", world_q16=None, skinned=False)


def fsha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def scene(*prims):
    return NS(prims=list(prims))


def gate(p, **kw):
    rc, body = ug.gate_scene(scene(p), **kw)
    return rc, body


def flip_tri(tri_no):
    def m(U):
        for k in range(3 * tri_no, 3 * tri_no + 3):
            U[k] = (-U[k][0], U[k][1])
    return m


def glb_bytes(p):
    return glb_core.write_glb(glb_core.Scene(prims=[glb_core.Prim(
        name=p.name, node_path="/" + p.name, world_q16=glb_core.IDENTITY_Q16, positions_um=p.positions_um,
        indices=p.indices, uvs_q16=p.uvs_q16, material=p.material)]))


def with_materials(raw, mats):
    doc, binbuf = glb_core._split(raw)
    doc["materials"] = mats
    js = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    binbuf += b"\0" * (-len(binbuf) % 4)
    total = 12 + 8 + len(js) + 8 + len(binbuf)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js +
            struct.pack("<II", len(binbuf), 0x004E4942) + binbuf)


def mat(base=(0.2, 0.2, 0.2, 1), metal=0.0, rough=0.5, name="m"):
    return {"name": name, "pbrMetallicRoughness": {"baseColorFactor": list(base), "metallicFactor": metal,
                                                   "roughnessFactor": rough}}


class UvGate(unittest.TestCase):
    def test_base_passes_and_is_isometric(self):
        rc, body = gate(plane())
        self.assertEqual((rc, body["failed"]), (0, []))
        m = body["prims"][0]
        self.assertEqual((m["flipped_tris"], m["uv_zero_area"], m["stretch_max_permille"]), (0, 0, 1000))

    def test_exact_stretch_values(self):
        for su, sv, want in ((3, 1, 3000), (1, 5, 5000), (2, 2, 1000)):
            m = gate(plane(su=su, sv=sv), max_stretch=10 ** 5)[1]["prims"][0]
            self.assertEqual(m["stretch_max_permille"], want, (su, sv))

    def test_mutant_stretch(self):
        rc, body = gate(plane(su=10))
        self.assertEqual((rc, body["failed"]), (5, ["uv_stretch"]))
        self.assertEqual(body["prims"][0]["stretch_over_ppm"], 1000000)
        self.assertEqual(gate(plane(su=10), max_stretch=10000)[0], 0)  # the limit is the only difference

    def test_mutant_flip_one_tri(self):
        rc, body = gate(plane(mutate=flip_tri(3)))
        self.assertEqual((rc, body["failed"]), (5, ["uv_flipped"]))
        self.assertEqual(body["prims"][0]["flipped_tris"], 1)
        self.assertEqual(body["prims"][0]["flipped_ppm"], 1000000 // 8)

    def test_uniformly_mirrored_prim_is_not_flipped(self):
        def all_flip(U):
            U[:] = [(-u, v) for u, v in U]
        self.assertEqual(gate(plane(mutate=all_flip))[0], 0)

    def test_allow_mirror_suppresses_flip_only(self):
        self.assertEqual(gate(plane(mutate=flip_tri(0)), allow_mirror=True)[0], 0)
        self.assertEqual(gate(plane(su=10), allow_mirror=True)[1]["failed"], ["uv_stretch"])

    def test_mutant_collapsed_uv(self):
        def collapse(U):
            U[0] = U[1] = U[2]
        rc, body = gate(plane(mutate=collapse))
        self.assertEqual(rc, 5)
        self.assertIn("uv_degenerate", body["failed"])

    def test_missing_uv_and_empty(self):
        p = plane()
        p.uvs_q16 = None
        self.assertEqual(gate(p)[1]["failed"], ["uv_missing"])
        self.assertEqual(gate(p, require_uv=False)[0], 0)
        e = plane()
        e.indices = []
        self.assertEqual(ug.gate_scene(scene(e))[0], 3)

    def test_3d_degenerate_tri_is_not_counted(self):
        p = plane()
        p.positions_um[1] = p.positions_um[0]
        p.positions_um[2] = p.positions_um[0]
        m = gate(p)[1]["prims"][0]
        self.assertEqual(m["uv_tris"], m["tris"] - 1)

    def test_nonvacuous_abs_area_mutant_hides_flip(self):
        orig = ug.tri_uv_metrics

        def absarea(*a):
            area, st = orig(*a)
            return abs(area), st
        with mock.patch.object(ug, "tri_uv_metrics", absarea):
            self.assertEqual(gate(plane(mutate=flip_tri(3)))[0], 0)  # mutant passes -> the real test is not vacuous
        self.assertEqual(gate(plane(mutate=flip_tri(3)))[0], 5)

    def test_nonvacuous_no_stretch_mutant(self):
        with mock.patch.object(ug, "STRETCH_TRIS_MAX_PPM", 10 ** 7):
            self.assertEqual(gate(plane(su=10))[0], 0)
        self.assertEqual(gate(plane(su=10))[0], 5)


class UvCli(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

    def path(self, name, data=b""):
        p = os.path.join(self.td.name, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_cli_rc_receipt_chain_determinism(self):
        good = self.path("g.glb", glb_bytes(plane()))
        bad = self.path("b.glb", glb_bytes(plane(su=10)))
        r1 = os.path.join(self.td.name, "r1.json")
        rc, rec = ug.run(["--mesh", good, "--out", r1])
        self.assertEqual((rc, rec["schema"], rec["status"]), (0, "bvqa.asset-uv-gate/v1", "PASS"))
        self.assertEqual(rec["input_sha256"], fsha(good))
        rc2, rec2 = ug.run(["--mesh", bad, "--prev-receipt", r1])
        self.assertEqual((rc2, rec2["failed"]), (5, ["uv_stretch"]))
        self.assertEqual(rec2["prev_sha256"], fsha(r1))
        self.assertEqual(ug.canon(ug.run(["--mesh", good])[1]), ug.canon(rec))
        with open(r1) as fh:
            self.assertEqual(fh.read(), ug.canon(rec))

    def test_cli_usage_errors(self):
        self.assertEqual(ug.run([])[0], 2)
        self.assertEqual(ug.run(["--mesh", os.path.join(self.td.name, "none.glb")])[0], 2)
        self.assertEqual(ug.run(["--mesh", self.path("junk.glb", b"not a glb at all")])[0], 2)


class PbrParams(unittest.TestCase):
    def failed(self, **kw):
        return check(mat(**kw))

    def test_base_passes(self):
        self.assertEqual(self.failed(), [])
        self.assertEqual(self.failed(base=(0.9, 0.8, 0.5, 1), metal=1.0, rough=0.3), [])

    def test_rough_floor_boundary(self):
        self.assertEqual(self.failed(rough=0.043), [])  # byte 11
        self.assertEqual(self.failed(rough=0.040), ["rough_floor"])  # byte 10
        self.assertEqual(self.failed(rough=0.0), ["rough_floor"])

    def test_albedo_window_dielectric(self):
        self.assertEqual(self.failed(base=(0.001, 0.2, 0.2, 1)), ["albedo_range"])
        self.assertEqual(self.failed(base=(1, 1, 1, 1)), ["albedo_range"])

    def test_metal_mid(self):
        self.assertEqual(self.failed(metal=0.5), ["metal_mid"])
        self.assertEqual(self.failed(metal=0.1), [])
        self.assertEqual(self.failed(base=(0.9, 0.8, 0.5, 1), metal=0.9), [])

    def test_f0_low_for_dark_metal(self):
        self.assertEqual(self.failed(base=(0.05, 0.05, 0.05, 1), metal=1.0), ["f0_low"])

    def test_param_range(self):
        self.assertIn("param_range", self.failed(metal=1.5))
        self.assertIn("param_range", self.failed(base=(1.2, 0.2, 0.2, 1)))

    def test_defaults_applied(self):
        m, f = pv.check_material({"name": "bare"})
        self.assertEqual((m["metal_permille"], m["rough_permille"]), (1000, 1000))

    def test_nonvacuous_floor_mutant(self):
        with mock.patch.object(pv, "ROUGH_MIN_BYTE", 0):
            self.assertEqual(self.failed(rough=0.0), [])
        self.assertEqual(self.failed(rough=0.0), ["rough_floor"])


def check(m):
    return sorted(pv.check_material(m)[1])



class PbrCli(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.base = glb_bytes(plane())

    def path(self, name, data):
        p = os.path.join(self.td.name, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_glb_pass_and_fail_with_chain(self):
        good = self.path("g.glb", with_materials(self.base, [mat()]))
        bad = self.path("b.glb", with_materials(self.base, [mat(), mat(rough=0.0, name="slick")]))
        r1 = os.path.join(self.td.name, "r1.json")
        rc, rec = pv.run(["--glb", good, "--out", r1])
        self.assertEqual((rc, rec["schema"], rec["checks"]), (0, "bvqa.asset-pbr-validate/v1", 1))
        rc2, rec2 = pv.run(["--glb", bad, "--prev-receipt", r1])
        self.assertEqual((rc2, rec2["failed"]), (5, ["rough_floor"]))
        self.assertEqual(rec2["materials"][1]["name"], "slick")
        self.assertEqual(rec2["prev_sha256"], fsha(r1))
        self.assertEqual(pv.canon(pv.run(["--glb", good])[1]), pv.canon(rec))

    def test_no_materials_is_incomplete(self):
        nomat = plane()
        nomat.material = None
        rc, rec = pv.run(["--glb", self.path("n.glb", glb_bytes(nomat))])
        self.assertEqual((rc, rec["status"]), (3, "INCOMPLETE_ZERO_CHECKS"))

    def test_albedo_png(self):
        ok = self.path("ok.png", orm_pack.encode_png(8, 8, b"\x80\x70\x60\xff" * 64))
        dark = self.path("dark.png", orm_pack.encode_png(8, 8, b"\x05\x70\x60\xff" * 64))
        self.assertEqual(pv.run(["--albedo", ok])[0], 0)
        rc, rec = pv.run(["--albedo", dark])
        self.assertEqual((rc, rec["failed"]), (5, ["albedo_texture"]))
        self.assertEqual(rec["albedo"]["out_of_range_ppm"], 1000000)

    def test_orm_png_floor(self):
        ok = self.path("ok.png", orm_pack.encode_png(4, 4, b"\xff\x40\x00\xff" * 16))
        low = self.path("low.png", orm_pack.encode_png(4, 4, b"\xff\x05\x00\xff" * 16))
        self.assertEqual(pv.run(["--orm", ok])[0], 0)
        rc, rec = pv.run(["--orm", low])
        self.assertEqual((rc, rec["failed"], rec["orm"]["violations"]), (5, ["orm_floor"], 16))

    def test_usage_errors(self):
        self.assertEqual(pv.run([])[0], 2)
        self.assertEqual(pv.run(["--glb", os.path.join(self.td.name, "none.glb")])[0], 2)
        self.assertEqual(pv.run(["--glb", self.path("junk.glb", b"zzzz")])[0], 2)
        self.assertEqual(pv.run(["--glb", self.path("bm.glb", with_materials(self.base, [{"pbrMetallicRoughness": {"baseColorFactor": [1]}}]))])[0], 2)


if __name__ == "__main__":
    unittest.main()
