"""AG1 L3: mesh_gate topology + UV oracle. Fixtures are built in-test (no binaries); each mutant pairs with a passing base."""
import json
import os
import struct
import tempfile
import unittest
from types import SimpleNamespace as NS

from blender_visual_qa.assets import mesh_gate as mg

S = 1000000
CUBE_P = [(x * S, y * S, z * S) for z in (0, 1) for y in (0, 1) for x in (0, 1)]
CUBE_T = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
          (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
CELL = 20000


def prim(pos, tris, uv_tris=None, name="p", material="m", world=None):
    """Prim with one vertex per corner when uv_tris is given (so welding is exercised), else shared vertices."""
    if uv_tris is None:
        return NS(name=name, positions_um=list(pos), indices=[i for t in tris for i in t], uvs_q16=None,
                  material=material, world_q16=world, skinned=False)
    P, I, U = [], [], []
    for t, uv in zip(tris, uv_tris):
        for vi, c in zip(t, uv):
            I.append(len(P))
            P.append(pos[vi])
            U.append(c)
    return NS(name=name, positions_um=P, indices=I, uvs_q16=U, material=material, world_q16=world, skinned=False)


def cube_uvs(overlap=False):
    out = []
    for f in range(6):
        ox, oy = (f % 3) * 3 * CELL, (f // 3) * 3 * CELL
        if overlap and f == 1:
            ox, oy = 0, 0
        a, b, c, d = (ox, oy), (ox + CELL, oy), (ox + CELL, oy + CELL), (ox, oy + CELL)
        out += [(a, b, c), (a, c, d)]
    return out


def cube(tris=None, **kw):
    return prim(CUBE_P, tris or CUBE_T, cube_uvs(**kw))


def scene(*prims):
    return NS(prims=list(prims))


def torus():
    R, r = 4 * S, S
    u = [(1, 0), (0, 1), (-1, 0), (0, -1)]
    d = [(1, 0), (0, 1), (-1, 0), (0, -1)]
    pos = [((R + r * dx) * u[i][0], (R + r * dx) * u[i][1], r * dz) for i in range(4) for dx, dz in d]
    vid = lambda i, j: (i % 4) * 4 + (j % 4)
    tris = []
    for i in range(4):
        for j in range(4):
            a, b, c, e = vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)
            tris += [(a, b, c), (a, c, e)]
    return pos, tris


def glb_bytes(p):
    pos = b"".join(struct.pack("<fff", *(c / S for c in v)) for v in p.positions_um)
    idx = b"".join(struct.pack("<H", i) for i in p.indices)
    uv = b"".join(struct.pack("<ff", u / 65536, v / 65536) for u, v in p.uvs_q16) if p.uvs_q16 else b""
    idx += b"\0" * (-len(idx) % 4)
    blob = pos + idx + uv
    views = [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos)},
             {"buffer": 0, "byteOffset": len(pos), "byteLength": len(p.indices) * 2}]
    acc = [{"bufferView": 0, "componentType": 5126, "count": len(p.positions_um), "type": "VEC3",
            "min": [-9, -9, -9], "max": [9, 9, 9]},
           {"bufferView": 1, "componentType": 5123, "count": len(p.indices), "type": "SCALAR"}]
    attrs = {"POSITION": 0}
    if p.uvs_q16:
        views.append({"buffer": 0, "byteOffset": len(pos) + len(idx), "byteLength": len(uv)})
        acc.append({"bufferView": 2, "componentType": 5126, "count": len(p.uvs_q16), "type": "VEC2"})
        attrs["TEXCOORD_0"] = 2
    doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
           "meshes": [{"primitives": [{"attributes": attrs, "indices": 1}]}], "accessors": acc,
           "bufferViews": views, "buffers": [{"byteLength": len(blob)}]}
    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    blob += b"\0" * (-len(blob) % 4)
    total = 12 + 8 + len(js) + 8 + len(blob)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js +
            struct.pack("<II", len(blob), 0x004E4942) + blob)


def failed(*prims, **kw):
    rc, body = mg.gate_scene(scene(*prims), **kw)
    return rc, body["failed"]


class Base(unittest.TestCase):
    def test_cube_passes(self):
        rc, body = mg.gate_scene(scene(cube()))
        self.assertEqual((rc, body["failed"]), (0, []))
        m = body["prims"][0]
        self.assertEqual((m["vertices"], m["edges"], m["tris"], m["euler_characteristic"], m["genus"],
                          m["overlap_ppm"]), (8, 18, 12, 2, 0, 0))

    def test_deterministic_receipt(self):
        a = mg.canon(mg.make_receipt(*mg.gate_scene(scene(cube())), "x", ""))
        b = mg.canon(mg.make_receipt(*mg.gate_scene(scene(cube())), "x", ""))
        self.assertEqual(a, b)
        self.assertEqual(json.loads(a)["schema"], "bvqa.asset-mesh-gate/v1")


class Mutants(unittest.TestCase):
    def setUp(self):
        self.assertEqual(failed(cube())[0], 0)  # SQ-NONVAC: the base passes

    def test_torus_genus_rc5(self):
        pos, tris = torus()
        p = prim(pos, tris)
        rc, f = failed(p, require_uv=False)
        self.assertEqual((rc, f), (5, ["genus"]))
        rc, f = failed(p, require_uv=False, max_genus=1)
        self.assertEqual((rc, f), (0, []))
        self.assertEqual(mg.gate_prim(p, require_uv=False)[0]["genus"], 1)

    def test_uv_overlap_rc5(self):
        rc, f = failed(cube(overlap=True))
        self.assertEqual((rc, f), (5, ["uv_overlap"]))
        self.assertEqual(failed(cube(overlap=True), allow_uv_overlap=True), (0, []))
        self.assertGreater(mg.gate_prim(cube(overlap=True))[0]["overlap_ppm"], 0)

    def test_two_overlapping_uv_tris(self):
        p = prim([(0, 0, 0), (S, 0, 0), (0, S, 0), (S, S, 0)], [(0, 1, 2), (1, 3, 2)],
                 [((0, 0), (CELL, 0), (0, CELL)), ((1, 1), (CELL, 1), (1, CELL))])
        rc, f = failed(p, allow_open=True)
        self.assertEqual((rc, f), (5, ["uv_overlap"]))

    def test_edge_valence3_rc5(self):
        p = prim([(0, 0, 0), (S, 0, 0), (0, S, 0), (0, 0, S), (0, -S, 0), (-S, 0, 0)],
                 [(0, 1, 2), (1, 0, 3), (0, 1, 4)])
        rc, f = failed(p, require_uv=False, allow_open=True)
        self.assertEqual(rc, 5)
        self.assertIn("edge_multiplicity", f)

    def test_empty_rc3(self):
        self.assertEqual(mg.gate_scene(scene())[0], 3)
        self.assertEqual(mg.gate_scene(scene(prim([], [], None)))[0], 3)

    def test_twin_winding_rc5(self):
        t = list(CUBE_T)
        t[0] = (t[0][0], t[0][2], t[0][1])
        rc, f = failed(cube(t))
        self.assertEqual(rc, 5)
        self.assertIn("twin_winding", f)

    def test_pinched_bowtie_rc5(self):
        tet = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]
        pos = [(0, 0, 0), (S, 0, 0), (0, S, 0), (0, 0, S)]
        off = [(-x, -y, -z) for x, y, z in pos]
        tris = tet + [(a + 4, c + 4, b + 4) for a, b, c in tet]
        p = prim(pos + off, tris)
        self.assertEqual(failed(prim(pos, tet), require_uv=False), (0, []))
        rc, f = failed(p, require_uv=False)
        self.assertEqual(rc, 5)
        self.assertEqual(f, ["pinched_vertices"])

    def test_inward_winding_and_world_det(self):
        t = [(a, c, b) for a, b, c in CUBE_T]
        rc, f = failed(cube(t))
        self.assertEqual((rc, f), (5, ["inward_winding"]))
        mirror = (-1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
        flipped = cube(t)
        flipped.world_q16 = mirror
        self.assertEqual(failed(flipped), (0, []))

    def test_open_and_missing_uv(self):
        p = prim(CUBE_P, CUBE_T[:-1], cube_uvs()[:-1])
        self.assertEqual(failed(p), (5, ["open_boundary"]))
        self.assertEqual(failed(p, allow_open=True), (0, []))
        self.assertEqual(failed(prim(CUBE_P, CUBE_T), require_uv=True), (5, ["uv_missing"]))
        self.assertEqual(failed(prim(CUBE_P, CUBE_T), require_uv=False), (0, []))

    def test_duplicate_and_degenerate(self):
        self.assertIn("duplicate_faces", failed(prim(CUBE_P, CUBE_T + [CUBE_T[0]]), require_uv=False)[1])
        self.assertIn("degenerate_faces", failed(prim(CUBE_P, CUBE_T + [(0, 0, 1)]), require_uv=False)[1])

    def test_all_primitives_walked(self):
        """AUDIT F1: a bad second primitive must fail the scene (not only the first)."""
        bad = cube(overlap=True)
        self.assertEqual(failed(cube(), cube())[0], 0)
        self.assertEqual(failed(cube(), bad)[0], 5)


class Cli(unittest.TestCase):
    def run_cli(self, data, *extra):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.glb")
            with open(path, "wb") as fh:
                fh.write(data)
            return mg.run(["--mesh", path, "--out", os.path.join(d, "r.json")] + list(extra))

    def setUp(self):
        try:
            from blender_visual_qa.assets import glb_core  # noqa: F401
        except ImportError:
            self.skipTest("glb_core (L1) not present yet")

    def test_glb_roundtrip_pass_and_receipt(self):
        rc, rec = self.run_cli(glb_bytes(cube()))
        self.assertEqual(rc, 0, rec)
        self.assertEqual(len(rec["input_sha256"]), 64)
        self.assertEqual(rec["prev_sha256"], "")

    def test_glb_overlap_fails(self):
        self.assertEqual(self.run_cli(glb_bytes(cube(overlap=True)))[0], 5)

    def test_garbage_is_rc2(self):
        self.assertEqual(self.run_cli(b"not a glb")[0], 2)
        self.assertEqual(mg.run(["--mesh", "/nonexistent/x.glb"])[0], 2)
        self.assertEqual(mg.run([])[0], 2)

    def test_prev_receipt_chain(self):
        with tempfile.TemporaryDirectory() as d:
            prev = os.path.join(d, "prev.json")
            with open(prev, "wb") as fh:
                fh.write(b"{}\n")
            glb = os.path.join(d, "a.glb")
            with open(glb, "wb") as fh:
                fh.write(glb_bytes(cube()))
            rc, rec = mg.run(["--mesh", glb, "--prev-receipt", prev])
            self.assertEqual(rc, 0)
            self.assertEqual(rec["prev_sha256"], "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356")


if __name__ == "__main__":
    unittest.main()
